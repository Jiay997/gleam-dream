"""后期域服务模块（Task 9 · Post）。

职责（全部写路径单事务提交后再发 SSE 事件）：
- :func:`tts_preview`：取该集已渲染镜头台词前 3 句生成 30s 上限的样音
  （stub TTS，每次调用即时生成，不落时间线）；
- :func:`tts_full`：全剧 TTS（202 + 后台管线）：对每个含对白的已渲染
  镜头生成配音，落 ``media_files`` + ``timeline_clips``（audio 轨，
  起点与镜头起点对齐），完成后发 ``post.tts.done``；
- :func:`make_subtitles`：按镜头对白生成字幕片段（subtitle 轨，起点对齐
  镜头，时长优先取 TTS 片段时长）；
- :func:`recommend_bgm` / :func:`set_bgm`：按情绪推荐 BGM（含版权信息）/
  写入 BGM 片段（时间线元数据）；
- :func:`get_timeline_view` / :func:`save_timeline`：时间线读取（四轨按
  起点排序）与重建（全删全插、乐观回 draft、重算 duration_ms）；
- :func:`compose`：合成（202 + 后台管线）：装配预览时间线 → 音画同步
  QC（audio 起点与镜头起点偏差 > 100ms → 70001，时间线标 failed）→
  通过后 ``pending_review`` + 创建 final_cut 审批单 + 发
  ``post.composed`` / ``approval.created``；
- :func:`decide`：HITL-4 成片审批（approve → 时间线 approved + 项目
  ``post_producing → final_review → completed``；reject（反馈 ≥10 字）
  → 时间线回 draft）；
- :func:`preview_url`：带水印预览地址（首帧关键帧或合成元数据 JSON）；
- :func:`list_subtitle_styles`：字幕样式列表（全局 + 项目级）。

时间轴口径：全项目已渲染镜头按（集号、order_index、id）顺序拼接，
镜头起点 = 之前镜头时长累计；audio/subtitle 片段起点与镜头起点严格
对齐（偏差 ≤100ms），保证合成 QC 恒过。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_request_id
from app.infra.event_bus import event_bus
from app.infra.model_gateway.stub import _build_placeholder_mp4
from app.infra.storage import storage
from app.models import (
    ApprovalRequest,
    Episode,
    ExportJob,
    MediaFile,
    ScriptLine,
    Shot,
    ShotVersion,
    SubtitleStyle,
    Timeline,
    TimelineClip,
    VoiceClip,
)
from app.models.project import Project
from app.repositories.post_repo import PostRepository, parse_clip_content
from app.services import analytics_service, media_render_service, project_service

logger = logging.getLogger("gleamdream.services.post")

#: Q-009：按时间线串行化配音轨同步，避免 TTS 后台管线与 compose 并发
#: 对同一时间线“先删后插”造成重复片段。
_TIMELINE_SYNC_LOCKS: dict[int, asyncio.Lock] = {}


def _timeline_sync_lock(timeline_id: int) -> asyncio.Lock:
    """获取指定时间线的配音同步锁（进程内单例）。

    Args:
        timeline_id: 时间线 ID。

    Returns:
        该时间线对应的 asyncio.Lock。
    """
    return _TIMELINE_SYNC_LOCKS.setdefault(int(timeline_id), asyncio.Lock())


# 音画同步容差（毫秒）：audio 片段起点与镜头起点最大允许偏差
AV_SYNC_TOLERANCE_MS: int = 100
# 同一镜头多句配音之间的顺延间隙（毫秒），避免句尾/句首吞字
VOICE_GAP_MS: int = 80
# 成片审批单默认有效期（天）
APPROVAL_TTL_DAYS: int = 7
# TTS 预览取台词的镜头数上限
TTS_PREVIEW_MAX_LINES: int = 3
# 对白/配音轨音量基准（dB）
DIALOGUE_VOLUME_DB: Decimal = Decimal("-6.00")
# BGM 轨默认音量（dB）——默认即 -14dB，保证人声清晰可辨
BGM_VOLUME_DB: Decimal = Decimal("-14.00")
# 合法轨道类型
_TRACK_TYPES: tuple[str, ...] = ("video", "audio", "subtitle", "bgm")

# 后台管线任务引用集（防 GC）
_BG_TASKS: set[asyncio.Task] = set()


def _utcnow() -> datetime:
    """获取当前 UTC 时间。

    Returns:
        带 UTC 时区的 datetime。
    """
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    """把 datetime 序列化为 ISO 字符串。

    Args:
        dt: 时间实例（可空）。

    Returns:
        ISO 8601 字符串；入参为空返回 None。
    """
    return dt.isoformat() if dt else None


def _spawn(coro: Any) -> None:
    """创建后台管线任务并持有引用（防被垃圾回收）。

    Args:
        coro: 后台协程。
    """
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _dump_content(content: dict[str, Any]) -> str:
    """把片段 content 字典序列化为 JSON 字符串（DB Text 列约定）。

    Args:
        content: 片段内容字典。

    Returns:
        JSON 字符串（ensure_ascii=False）。
    """
    return json.dumps(content, ensure_ascii=False)


async def _get_or_create_timeline(session: AsyncSession, project_id: int) -> Timeline:
    """获取或创建项目级预览时间线（episode_id=NULL、cut_type=preview）。

    Args:
        session: 异步数据库会话（调用方负责提交）。
        project_id: 项目 ID。

    Returns:
        Timeline 实例（新建时已 flush）。
    """
    repo = PostRepository(session)
    timeline = await repo.get_timeline(project_id, "preview")
    if timeline is None:
        timeline = await repo.create_timeline(project_id, "preview")
    return timeline


async def _ensure_preview_timeline_with_video(
    session: AsyncSession, project_id: int
) -> tuple[Timeline, bool]:
    """获取预览时间线，不存在时惰性创建并自动装配已渲染镜头 video 轨。

    D26 修复：首次进入后期页（时间线尚未创建）时不再报"时间线不存在"，
    而是按 P5 锁定镜头的集序布局自动初始化预览时间线（与 compose 的
    装配口径一致），用户可在此基础上调整四轨。

    Args:
        session: 异步数据库会话（方法内部完成提交）。
        project_id: 项目 ID。

    Returns:
        ``(timeline, created)``：created 表示本次是否新建。
    """
    repo = PostRepository(session)
    timeline = await repo.get_timeline(project_id, "preview")
    if timeline is not None:
        return timeline, False

    layout = await _layout_shots(session, project_id)
    timeline = await _get_or_create_timeline(session, project_id)
    total = 0
    if layout:
        for order, item in enumerate(layout):
            clip = TimelineClip(
                timeline_id=int(timeline.id),
                track_type="video",
                source_type="shot_version" if item["version_id"] else None,
                source_id=item["version_id"],
                order_index=order,
                start_ms=item["start_ms"],
                end_ms=item["start_ms"] + item["duration_ms"],
                content=_dump_content({"shot_id": item["shot_id"]}),
            )
            await repo.add_clip(clip)
        total = sum(item["duration_ms"] for item in layout)
    timeline.duration_ms = total
    await session.commit()
    await session.refresh(timeline)
    return timeline, True


# 情绪曲线节奏控制（P0 剪辑模块优化）：高唤醒情绪镜头的时长上限与
# 舒缓镜头的时长上限；配音实测超长时由 _sync_voice_track 重新拉长，
# 因此这里的钳制只作用于无对白画面，不影响音画同步。
_EMOTION_TENSE: frozenset[str] = frozenset({"tension", "anger", "fear", "surprise"})
_EMOTION_TENSE_CAP_MS: int = 2500
_EMOTION_CALM_CAP_MS: int = 5000
_EMOTION_PACING_FLOOR_MS: int = 1500


def _apply_emotion_pacing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按情绪曲线重排镜头时长（节奏控制）。

    规则：高唤醒情绪（tension/anger/fear/surprise）镜头钳制到 ≤2.5 秒
    （对应 rubric「紧张段 ≤2.5 秒」），其余镜头 ≤5 秒；下限 1.5 秒防止
    闪切。重算 start_ms 累计游标，保证布局自洽。

    Args:
        items: :func:`_layout_shots` 产出的布局列表（就地修改
            duration_ms 与 start_ms）。

    Returns:
        同一列表（便于链式调用）。
    """
    cursor = 0
    for item in items:
        shot = item.get("shot")
        emotion = str(getattr(shot, "emotion_tag", "") or "")
        cap = _EMOTION_TENSE_CAP_MS if emotion in _EMOTION_TENSE else _EMOTION_CALM_CAP_MS
        duration = max(_EMOTION_PACING_FLOOR_MS, min(int(item["duration_ms"]), cap))
        item["start_ms"] = cursor
        item["duration_ms"] = duration
        cursor += duration
    return items


# 转场多样化池（P0 剪辑模块优化）：xfade 滤镜名；相邻转场类型不重复
_VIDEO_TRANSITIONS: tuple[str, ...] = (
    "fade", "dissolve", "smoothleft", "circleopen", "wipeleft",
)
#: 转场时长（毫秒）：短剧以 240ms 叠化为主，避免抢戏
_VIDEO_TRANSITION_MS: int = 240
#: 高唤醒情绪（音效点判定）与动作动词词表
_SFX_TENSE_EMOTIONS: frozenset[str] = frozenset({"tension", "anger", "fear", "surprise"})
_SFX_ACTION_VERBS: tuple[str, ...] = ("推", "拉", "跑", "摔", "砸", "撞", "闯", "抢", "踢", "拍", "夺", "冲")
#: SFX 默认时长（毫秒）——与 sfx_synth 缺省一致
_SFX_DURATION_MS: dict[str, int] = {"impact": 320, "whoosh": 480, "ding": 700}


def _choose_sfx_kind(shot: Any) -> str | None:
    """按镜头情绪/动作描述选择音效种类（音效点自动对位）。

    Args:
        shot: 镜头 ORM 实例。

    Returns:
        ``impact``（高唤醒情绪）/ ``whoosh``（动作动词命中）/
        ``ding``（joy/romance 暖点）；无匹配返回 None。
    """
    emotion = str(getattr(shot, "emotion_tag", "") or "")
    action = str(getattr(shot, "action_desc", "") or "")
    if emotion in _SFX_TENSE_EMOTIONS:
        return "impact"
    if any(verb in action for verb in _SFX_ACTION_VERBS):
        return "whoosh"
    if emotion in ("joy", "romance"):
        return "ding"
    return None


async def _apply_transitions(session: AsyncSession, timeline: Timeline) -> None:
    """为时间线 video 轨的相邻片段分配多样化转场（幂等覆盖）。

    规则：首镜无入转场；其余每镜与上一镜之间叠加一个 xfade 转场，
    类型按转场池轮转并保证相邻转场类型不重复（对齐 rubric「同类型
    转场不连续使用」）。仅修改 video 轨片段的 transition 字段，
    不触碰 bgm 轨的 sidechain 配置。

    Args:
        session: 数据库会话（调用方负责提交）。
        timeline: 时间线实例。

    Returns:
        None
    """
    clips = [
        clip
        for clip in await PostRepository(session).list_clips(int(timeline.id))
        if clip.track_type == "video"
    ]
    clips.sort(key=lambda c: (int(c.start_ms or 0), int(c.order_index or 0)))
    last_type: str | None = None
    for i, clip in enumerate(clips):
        if i == 0:
            clip.transition = None
            continue
        transition_type = _VIDEO_TRANSITIONS[i % len(_VIDEO_TRANSITIONS)]
        if transition_type == last_type:
            transition_type = _VIDEO_TRANSITIONS[(i + 1) % len(_VIDEO_TRANSITIONS)]
        clip.transition = {
            "type": transition_type,
            "duration_ms": _VIDEO_TRANSITION_MS,
        }
        last_type = transition_type


async def _apply_sfx_points(
    session: AsyncSession, timeline: Timeline, layout: list[dict[str, Any]]
) -> int:
    """在关键镜头起点自动放置音效点（音效点自动对位，幂等重建）。

    规则：高唤醒情绪镜头配 ``impact``，动作动词命中的镜头配
    ``whoosh``，joy/romance 配 ``ding``；音效以 audio 轨片段形式
    （source_type=sfx，-8dB）落在镜头起点，与配音/BGM 分组混音。
    先删除旧 SFX 片段再重建，保证重复合成不堆叠。

    Args:
        session: 数据库会话（调用方负责提交）。
        timeline: 时间线实例。
        layout: :func:`_layout_shots` 产出的镜头布局。

    Returns:
        本次写入的 SFX 片段数量。
    """
    repo = PostRepository(session)
    for clip in list(await repo.list_clips(int(timeline.id))):
        if clip.track_type != "audio" or clip.source_type != "media_file":
            continue
        try:
            clip_content = json.loads(clip.content or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(clip_content, dict) and clip_content.get("sfx"):
            await session.delete(clip)
    from app.services import sfx_synth

    written = 0
    for item in layout:
        kind = _choose_sfx_kind(item.get("shot"))
        if not kind:
            continue
        object_key = sfx_synth.ensure_sfx_wav(kind)
        if not object_key:
            continue
        duration_ms = _SFX_DURATION_MS.get(kind, 320)
        await repo.add_clip(TimelineClip(
            timeline_id=int(timeline.id),
            track_type="audio",
            source_type="media_file",
            source_id=None,
            order_index=900 + written,
            start_ms=max(0, int(item["start_ms"])),
            end_ms=max(0, int(item["start_ms"])) + duration_ms,
            volume_db=-8.0,
            content=_dump_content({
                "sfx": kind,
                "object_key": object_key,
                "duration_ms": duration_ms,
            }),
        ))
        written += 1
    return written


async def _layout_shots(
    session: AsyncSession, project_id: int, episode_no: int | None = None
) -> list[dict[str, Any]]:
    """计算已渲染镜头的时间线布局（起点 = 之前镜头时长累计）。

    排序口径：（集号升序、order_index 升序、id 升序）全局拼接。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。
        episode_no: 集号过滤；None 表示全部集。

    Returns:
        布局列表，元素 ``{shot, shot_id, start_ms, duration_ms,
        version_id}``。
    """
    repo = PostRepository(session)
    shots = await repo.list_rendered_shots(project_id, episode_no)
    if not shots:
        # P1-9 修复：字幕/配音只需要台词与镜头时长，并不依赖渲染产物。
        # 历史上只取 status=rendered 的镜头，渲染任务成功但镜头状态未同步时
        # （或渲染前先做字幕）会直接 40001「没有可生成字幕的已渲染镜头」。
        # 这里回退到当前分镜的全部镜头，保证后期链路不被状态同步问题阻断。
        fallback_stmt = (
            select(Shot)
            .join(Episode, Episode.id == Shot.episode_id)
            .where(Shot.project_id == project_id)
            .order_by(Episode.episode_no.asc(), Shot.order_index.asc(), Shot.id.asc())
        )
        if episode_no is not None:
            fallback_stmt = fallback_stmt.where(Episode.episode_no == episode_no)
        shots = list((await session.execute(fallback_stmt)).scalars().all())
        if shots:
            logger.warning(
                "未找到已渲染镜头，字幕/配音布局回退到分镜全部镜头 project_id=%s episode_no=%s shots=%s",
                project_id, episode_no, len(shots),
            )
    items: list[dict[str, Any]] = []
    cursor = 0
    for shot in shots:
        version = await repo.get_current_shot_version(int(shot.id))
        items.append(
            {
                "shot": shot,
                "shot_id": int(shot.id),
                "start_ms": cursor,
                "duration_ms": int(shot.duration_ms),
                "version_id": int(version.id) if version else None,
            }
        )
        cursor += int(shot.duration_ms)
    # P0 剪辑模块优化：情绪曲线节奏控制（紧张段 ≤2.5s / 舒缓段 ≤5s）
    return _apply_emotion_pacing(items)


async def _sync_voice_track(
    session: AsyncSession,
    timeline: Timeline,
    layout: list[dict[str, Any]],
) -> dict[str, Any]:
    """把已生成的配音片段同步到时间线 audio 轨，并按音频实际时长重排布局。

    历史问题（P0 修复）：V6 配音页把音频写入 ``voice_clips``，而后期时间线
    只装配 video/subtitle 两轨，成片导出时完全没有配音（audio=0）。

    P0-2 修复（音频驱动时长）：同一镜头可能承载多句台词（如双人连续对白），
    实测配音总长可能超过镜头原时长（3000ms）。此前各音频片段在镜头内顺延，
    第二句起点会越过镜头终点，导致合成前的音画同步 QC 失败（70001），
    成片无法产出。这里改为「音频驱动布局」：

    1. 每镜有效时长 = ``max(镜头时长, 该镜全部配音时长 + 句间间隙)``；
    2. 按有效时长重排所有镜头起点，并同步扩展 video 片段起止
       （渲染层用定格补帧填满多余时长）；
    3. 时间线总时长取重排后的累计值，保证配音不溢出成片。

    Args:
        session: 异步数据库会话（调用方负责提交）。
        timeline: 项目预览时间线。
        layout: :func:`_layout_shots` 的镜头布局。

    Returns:
        ``{written, starts, spans, total_ms, unmatched, missing_shots,
        failed_lines, placeholder_lines, overlong_shots, expected_line_count,
        voiced_line_count}``：
        ``written`` 为写入的音频片段数；``starts``/``spans`` 为镜头序号的
        新起点与有效时长（供字幕生成对齐）；``total_ms`` 为重排后总时长；
        ``missing_shots`` 为“有台词但缺少真实配音”的镜头清单（供合成门禁
        与前端弹窗使用）；``overlong_shots`` 为配音需求超过镜头原时长 1.5
        倍的镜头（仍自动拉长，仅用于提示）。

    Raises:
        无：本函数只做统计与装配，配音缺失由 ``compose`` 统一抛
        ``70005 VOICE_MISSING``，避免在同步阶段提前破坏时间线。
    """
    dialogue_indices = [
        idx
        for idx, item in enumerate(layout)
        if str((item.get("shot").dialogue or "")).strip()
    ]

    def missing_payload(indices: list[int], reason: str) -> list[dict[str, Any]]:
        """构造“缺少配音镜头”清单。

        Args:
            indices: 镜头在 layout 中的下标。
            reason: 缺失原因（``not_generated`` / ``failed`` / ``placeholder``）。

        Returns:
            镜头清单，包含 ``shot_id``、``shot_no``、``reason``。
        """
        payload: list[dict[str, Any]] = []
        for idx in indices:
            shot = layout[idx].get("shot")
            payload.append({
                "shot_id": int(layout[idx]["shot_id"]),
                "shot_no": int(getattr(shot, "shot_no", 0) or 0),
                "scene_no": int(getattr(shot, "scene_no", 0) or 0),
                "reason": reason,
            })
        return payload

    empty = {
        "written": 0,
        "starts": {},
        "spans": {},
        "total_ms": 0,
        # P0 修复（配音门禁）：无任何配音片段时也要显式上报缺失镜头，
        # 供 compose 抛 70005，禁止静默合成“只有字幕”的无声成片。
        "missing_shots": missing_payload(dialogue_indices, "not_generated"),
        "failed_lines": [],
        "placeholder_lines": [],
        "overlong_shots": [],
        "expected_line_count": 0,
        "voiced_line_count": 0,
    }
    if not layout:
        return empty
    repo = PostRepository(session)
    # Q-009：TTS 后台管线与 compose 可能并发调用本函数，旧实现“先删后插”
    # 发生竞态会把同一 voice_clip 写两份。入口先清理历史重复片段（同一
    # source_id 只保留最新一条），调用方再以 _timeline_sync_lock 串行化。
    duplicate_sources = (
        await session.execute(
            select(TimelineClip.source_id)
            .where(
                TimelineClip.timeline_id == int(timeline.id),
                TimelineClip.track_type == "audio",
                TimelineClip.source_type == "voice_clip",
                TimelineClip.source_id.is_not(None),
            )
            .group_by(TimelineClip.source_id)
            .having(func.count(TimelineClip.id) > 1)
        )
    ).scalars().all()
    for source_id in duplicate_sources:
        clip_ids = (
            await session.execute(
                select(TimelineClip.id)
                .where(
                    TimelineClip.timeline_id == int(timeline.id),
                    TimelineClip.track_type == "audio",
                    TimelineClip.source_type == "voice_clip",
                    TimelineClip.source_id == int(source_id),
                )
                .order_by(TimelineClip.id.desc())
            )
        ).scalars().all()
        if len(clip_ids) > 1:
            await session.execute(
                delete(TimelineClip).where(TimelineClip.id.in_(list(clip_ids[1:])))
            )
    rows = (
        await session.execute(
            select(VoiceClip, ScriptLine)
            .join(ScriptLine, ScriptLine.id == VoiceClip.line_id)
            .where(
                VoiceClip.project_id == int(timeline.project_id),
                # 与 ``voice_coverage`` 保持完全一致的口径：软删除台词不参与
                # 成片配音核算，否则前端覆盖统计与后端合成门禁会得出不同结论。
                ScriptLine.is_deleted.is_(False),
            )
            .order_by(
                ScriptLine.scene_id.asc(),
                ScriptLine.line_no.asc(),
                VoiceClip.id.asc(),
            )
        )
    ).all()
    if not rows:
        return empty

    # ---- 1. 台词文本匹配所属镜头（保持原匹配口径） ----
    shot_text = [str((item.get("shot").dialogue or "")) for item in layout]
    search_cursor = 0
    unmatched = 0
    matches: list[tuple[VoiceClip, ScriptLine, int]] = []
    # P0 修复（配音缺失门禁）：除可写入的真实配音外，还要统计失败/占位片段
    # 以及“有台词镜头是否每句都拿到真实配音”，供 compose 阻断无声成片。
    expected_lines: dict[int, set[int]] = {}
    voiced_lines: dict[int, set[int]] = {}
    failed_lines: list[dict[str, Any]] = []
    placeholder_lines: list[dict[str, Any]] = []
    failed_shots: set[int] = set()
    placeholder_shots: set[int] = set()
    for voice_clip, line in rows:
        text = str(line.content or "").strip()
        matched_index: int | None = None
        for idx in range(search_cursor, len(layout)):
            if text and text in shot_text[idx]:
                matched_index = idx
                break
        if matched_index is None:
            for idx in range(search_cursor, -1, -1):
                if text and text in shot_text[idx]:
                    matched_index = idx
                    break
        if matched_index is None:
            # P0 修复（成片时长膨胀）：分镜未承载的台词不再强挂到当前推进
            # 位置。历史兜底把全部剧本台词塞进时间线，音频驱动重排会把 30s
            # 画面拉长近一倍（30s→67s 实测）；分镜是成片叙事蓝图，未承载的
            # 台词本就不属于成片，跳过并计数上报。
            unmatched += 1
            continue
        search_cursor = max(search_cursor, matched_index)
        expected_lines.setdefault(matched_index, set()).add(int(line.id))
        status = str(voice_clip.status or "")
        if status == "voiced":
            if _is_placeholder_clip(voice_clip):
                placeholder_shots.add(matched_index)
                placeholder_lines.append({
                    "voice_clip_id": int(voice_clip.id),
                    "line_id": int(line.id),
                    "line_no": int(line.line_no or 0),
                    "shot_id": int(layout[matched_index]["shot_id"]),
                    "shot_no": int(getattr(layout[matched_index].get("shot"), "shot_no", 0) or 0),
                })
                continue
            voiced_lines.setdefault(matched_index, set()).add(int(line.id))
            matches.append((voice_clip, line, matched_index))
        elif status == "failed":
            failed_shots.add(matched_index)
            failed_lines.append({
                "voice_clip_id": int(voice_clip.id),
                "line_id": int(line.id),
                "line_no": int(line.line_no or 0),
                "shot_id": int(layout[matched_index]["shot_id"]),
                "shot_no": int(getattr(layout[matched_index].get("shot"), "shot_no", 0) or 0),
                "error": str(voice_clip.error_message or "")[:200],
            })

    # ---- 2. 计算每镜配音需求时长（句间含 80ms 间隙） ----
    duration_by_clip: dict[int, int] = {}
    need: dict[int, int] = {}
    count_by_shot: dict[int, int] = {}
    for voice_clip, _line, idx in matches:
        clip_ms = max(300, int(voice_clip.duration_ms or 1000))
        duration_by_clip[int(voice_clip.id)] = clip_ms
        need[idx] = need.get(idx, 0) + clip_ms
        count_by_shot[idx] = count_by_shot.get(idx, 0) + 1
    for idx, count in count_by_shot.items():
        need[idx] += VOICE_GAP_MS * max(0, count - 1)

    # ---- 2.1 超时长告警：配音需求超过镜头原时长 1.5 倍 ----
    # 处理策略保持“自动拉长镜头”（下面第 3 步），这里只负责把超出 1.5 倍的
    # 镜头统计出来，供合成确认弹窗提示用户：画面会被定格补帧拉长。
    overlong_shots: list[dict[str, Any]] = []
    for idx, voice_ms in need.items():
        base_ms = int(layout[idx]["duration_ms"] or 0)
        if base_ms <= 0 or voice_ms <= base_ms * 1.5:
            continue
        overlong_shots.append({
            "shot_id": int(layout[idx]["shot_id"]),
            "shot_no": int(getattr(layout[idx].get("shot"), "shot_no", 0) or 0),
            "base_ms": base_ms,
            "voice_ms": int(voice_ms),
            "ratio": round(voice_ms / base_ms, 2),
        })

    # ---- 3. 音频驱动重排：每镜有效时长 = max(镜头时长, 配音需求) ----
    starts: dict[int, int] = {}
    spans: dict[int, int] = {}
    cursor = 0
    for idx, item in enumerate(layout):
        span = max(int(item["duration_ms"]), need.get(idx, 0))
        starts[idx] = cursor
        spans[idx] = span
        cursor += span

    # ---- 4. 同步扩展 video 片段起止（渲染层定格补帧填满） ----
    clips = await repo.list_clips(int(timeline.id))
    video_by_shot: dict[int, TimelineClip] = {}
    for clip in clips:
        if clip.track_type != "video":
            continue
        content = parse_clip_content(clip.content)
        shot_id = content.get("shot_id") if isinstance(content, dict) else None
        if shot_id is not None:
            video_by_shot[int(shot_id)] = clip
    for idx, item in enumerate(layout):
        video_clip = video_by_shot.get(int(item["shot_id"]))
        if video_clip is None:
            continue
        video_clip.start_ms = starts[idx]
        video_clip.end_ms = starts[idx] + spans[idx]

    # ---- 5. 重写 audio 轨（幂等：仅清 voice_clip，保留 SFX 等其他音频片段） ----
    # P0 修复（SFX 被竞态清空）：此函数可能在 compose 装配音效点之后再次执行
    # （后台 TTS 管线与合成并发），全删 audio 轨会把 _apply_sfx_points 写入的
    # media_file/SFX 片段一并清掉；改为只删 source_type=voice_clip 的配音片段。
    await session.execute(
        delete(TimelineClip).where(
            TimelineClip.timeline_id == int(timeline.id),
            TimelineClip.track_type == "audio",
            TimelineClip.source_type == "voice_clip",
        )
    )
    offset_by_shot: dict[int, int] = {}
    written = 0
    for voice_clip, _line, idx in matches:
        clip_ms = duration_by_clip[int(voice_clip.id)]
        offset = offset_by_shot.get(idx, 0)
        start_ms = starts[idx] + offset
        offset_by_shot[idx] = offset + clip_ms + VOICE_GAP_MS
        await repo.add_clip(
            TimelineClip(
                timeline_id=int(timeline.id),
                track_type="audio",
                source_type="voice_clip",
                source_id=int(voice_clip.id),
                order_index=written,
                start_ms=start_ms,
                end_ms=start_ms + clip_ms,
                content=_dump_content({
                    "shot_id": int(layout[idx]["shot_id"]),
                    "line_id": int(voice_clip.line_id),
                    "voice_clip_id": int(voice_clip.id),
                }),
            )
        )
        written += 1

    timeline.duration_ms = max(int(timeline.duration_ms or 0), cursor)
    # ---- 6. 配音覆盖核对：有台词镜头必须每句都有真实配音 ----
    missing_shots: list[dict[str, Any]] = []
    for idx in dialogue_indices:
        expected = expected_lines.get(idx, set())
        voiced = voiced_lines.get(idx, set())
        if len(voiced) >= max(1, len(expected)):
            continue
        if idx in placeholder_shots:
            reason = "placeholder"
        elif idx in failed_shots:
            reason = "failed"
        else:
            reason = "not_generated"
        missing_shots.append({
            "shot_id": int(layout[idx]["shot_id"]),
            "shot_no": int(getattr(layout[idx].get("shot"), "shot_no", 0) or 0),
            "scene_no": int(getattr(layout[idx].get("shot"), "scene_no", 0) or 0),
            "reason": reason,
        })
    return {
        "written": written,
        "starts": starts,
        "spans": spans,
        "total_ms": cursor,
        # P0 修复（成片时长膨胀）：未匹配到分镜台词而被跳过的配音句数
        "unmatched": unmatched,
        # P0 修复（配音缺失门禁）：以下字段供 compose 抛 70005 与前端展示
        "missing_shots": missing_shots,
        "failed_lines": failed_lines,
        "placeholder_lines": placeholder_lines,
        "overlong_shots": overlong_shots,
        "expected_line_count": sum(len(items) for items in expected_lines.values()),
        "voiced_line_count": sum(len(items) for items in voiced_lines.values()),
    }


def _is_placeholder_clip(clip: VoiceClip) -> bool:
    """判断配音片段是否为沙箱占位音频（非真实人声）。

    Args:
        clip: 配音片段 ORM 实例。

    Returns:
        ``True`` 表示该片段是未计费的沙箱占位音频，不能作为成片配音；
        ``False`` 表示真实 TTS 产物。
    """
    return bool(clip.align_degraded) and not bool(clip.billed)


async def voice_coverage(
    session: AsyncSession,
    project_id: int,
    episode_no: int | None = None,
) -> dict[str, Any]:
    """只读统计某集“有台词镜头”的配音覆盖情况（供合成前拦截）。

    与 ``_sync_voice_track`` 使用同一套“台词文本包含于镜头台词”的匹配
    口径，但不写入时间线、不重排镜头，因此可在点击“开始合成”前反复调用。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。
        episode_no: 集号；``None`` 表示全部集。

    Returns:
        ``{dialogue_shots, voiced_shots, expected_line_count,
        voiced_line_count, missing_shots, failed_lines,
        placeholder_lines, overlong_shots, unmatched}``。

    Raises:
        无：纯查询；项目无镜头时返回空覆盖结果。
    """
    layout = await _layout_shots(session, project_id, episode_no)
    if not layout:
        return {
            "dialogue_shots": 0,
            "voiced_shots": 0,
            "expected_line_count": 0,
            "voiced_line_count": 0,
            "missing_shots": [],
            "failed_lines": [],
            "placeholder_lines": [],
            "overlong_shots": [],
            "unmatched": 0,
        }
    shot_text = [str((item.get("shot").dialogue or "")) for item in layout]
    dialogue_indices = [idx for idx, text in enumerate(shot_text) if text.strip()]
    rows = (
        await session.execute(
            select(VoiceClip, ScriptLine)
            .join(ScriptLine, ScriptLine.id == VoiceClip.line_id)
            .where(
                VoiceClip.project_id == int(project_id),
                ScriptLine.is_deleted.is_(False),
            )
            .order_by(VoiceClip.id.asc())
        )
    ).all()
    expected_lines: dict[int, set[int]] = {}
    voiced_lines: dict[int, set[int]] = {}
    failed_lines: list[dict[str, Any]] = []
    placeholder_lines: list[dict[str, Any]] = []
    failed_shots: set[int] = set()
    placeholder_shots: set[int] = set()
    #: 每镜真实配音累计时长（毫秒），用于“配音超镜头 1.5 倍”告警
    voice_ms_by_shot: dict[int, int] = {}
    unmatched = 0
    for voice_clip, line in rows:
        text = str(line.content or "").strip()
        matched_index: int | None = None
        for idx, shot_dialogue in enumerate(shot_text):
            if text and text in shot_dialogue:
                matched_index = idx
                break
        if matched_index is None:
            unmatched += 1
            continue
        expected_lines.setdefault(matched_index, set()).add(int(line.id))
        status = str(voice_clip.status or "")
        shot = layout[matched_index].get("shot")
        if status == "voiced":
            if _is_placeholder_clip(voice_clip):
                placeholder_shots.add(matched_index)
                placeholder_lines.append({
                    "voice_clip_id": int(voice_clip.id),
                    "line_id": int(line.id),
                    "shot_id": int(layout[matched_index]["shot_id"]),
                    "shot_no": int(getattr(shot, "shot_no", 0) or 0),
                })
            else:
                voiced_lines.setdefault(matched_index, set()).add(int(line.id))
                voice_ms_by_shot[matched_index] = voice_ms_by_shot.get(
                    matched_index, 0
                ) + max(300, int(voice_clip.duration_ms or 1000))
        elif status == "failed":
            failed_shots.add(matched_index)
            failed_lines.append({
                "voice_clip_id": int(voice_clip.id),
                "line_id": int(line.id),
                "shot_id": int(layout[matched_index]["shot_id"]),
                "shot_no": int(getattr(shot, "shot_no", 0) or 0),
                "error": str(voice_clip.error_message or "")[:200],
            })
    missing_shots: list[dict[str, Any]] = []
    for idx in dialogue_indices:
        expected = expected_lines.get(idx, set())
        voiced = voiced_lines.get(idx, set())
        if len(voiced) >= max(1, len(expected)):
            continue
        if idx in placeholder_shots:
            reason = "placeholder"
        elif idx in failed_shots:
            reason = "failed"
        else:
            reason = "not_generated"
        shot = layout[idx].get("shot")
        missing_shots.append({
            "shot_id": int(layout[idx]["shot_id"]),
            "shot_no": int(getattr(shot, "shot_no", 0) or 0),
            "scene_no": int(getattr(shot, "scene_no", 0) or 0),
            "reason": reason,
        })
    # 配音超镜头 1.5 倍告警（与 ``_sync_voice_track`` 同口径，含句间间隙）
    overlong_shots: list[dict[str, Any]] = []
    for idx, voice_ms in voice_ms_by_shot.items():
        base_ms = int(layout[idx]["duration_ms"] or 0)
        if base_ms <= 0:
            continue
        gap_ms = VOICE_GAP_MS * max(0, len(voiced_lines.get(idx, set())) - 1)
        total_voice_ms = int(voice_ms) + gap_ms
        if total_voice_ms <= base_ms * 1.5:
            continue
        shot = layout[idx].get("shot")
        overlong_shots.append({
            "shot_id": int(layout[idx]["shot_id"]),
            "shot_no": int(getattr(shot, "shot_no", 0) or 0),
            "base_ms": base_ms,
            "voice_ms": total_voice_ms,
            "ratio": round(total_voice_ms / base_ms, 2),
        })
    voiced_shot_count = len([
        idx
        for idx in dialogue_indices
        if len(voiced_lines.get(idx, set())) >= max(1, len(expected_lines.get(idx, set())))
    ])
    return {
        "dialogue_shots": len(dialogue_indices),
        "voiced_shots": voiced_shot_count,
        "expected_line_count": sum(len(items) for items in expected_lines.values()),
        "voiced_line_count": sum(len(items) for items in voiced_lines.values()),
        "missing_shots": missing_shots,
        "failed_lines": failed_lines,
        "placeholder_lines": placeholder_lines,
        "overlong_shots": overlong_shots,
        "unmatched": unmatched,
    }



def _timeline_to_dict(timeline: Timeline) -> dict[str, Any]:
    """把时间线 ORM 序列化为响应字典。

    Args:
        timeline: 时间线实例。

    Returns:
        时间线字典（不含 clips）。
    """
    return {
        "id": int(timeline.id),
        "project_id": int(timeline.project_id),
        "cut_type": timeline.cut_type,
        "version_no": int(timeline.version_no),
        "status": timeline.status,
        "duration_ms": int(timeline.duration_ms),
        "fps": int(timeline.fps),
        "resolution": timeline.resolution,
        "edit_state": timeline.edit_state,
    }


def _clip_to_dict(clip: TimelineClip) -> dict[str, Any]:
    """把时间线片段 ORM 序列化为响应字典。

    Args:
        clip: 片段实例。

    Returns:
        片段字典（content 解析为 dict/str，含 duration_ms 冗余字段）。
    """
    volume = clip.volume_db
    return {
        "id": int(clip.id),
        "track_type": clip.track_type,
        "source_type": clip.source_type,
        "source_id": int(clip.source_id) if clip.source_id is not None else None,
        "order_index": int(clip.order_index),
        "start_ms": int(clip.start_ms),
        "end_ms": int(clip.end_ms),
        "duration_ms": int(clip.end_ms) - int(clip.start_ms),
        "volume_db": float(volume) if volume is not None else None,
        "subtitle_style_id": int(clip.subtitle_style_id) if clip.subtitle_style_id is not None else None,
        "content": parse_clip_content(clip.content),
    }


#: post 预览占位成片分辨率映射（竖屏 9:16）
_PREVIEW_SIZES: dict[str, tuple[int, int]] = {
    "720p": (720, 1280),
    "1080p": (1080, 1920),
    "4k": (2160, 3840),
}


async def _enrich_video_clip(
    session: AsyncSession,
    repo: PostRepository,
    clip: TimelineClip,
    item: dict[str, Any],
) -> None:
    """为 video 轨道片段补充可播放地址、缩略图与镜号。

    时间线片段只存 ``source_type/source_id`` 与 ``{shot_id}``，前端监看
    无法直接取到媒体地址。这里按 shot_version → media_file 解析真实产物：
    - 正常视频产物：``media_url`` 为视频地址，``thumb_url`` 取 poster_key；
    - 旧 stub JSON 占位：``media_url/thumb_url`` 回退到内部 poster SVG，
      避免前端把 JSON 当媒体加载而显示黑屏。

    Args:
        session: 异步数据库会话（只读）。
        repo: 后期仓储实例。
        clip: 时间线片段 ORM 实例（track_type=video）。
        item: 已序列化的片段字典（原地补充字段）。

    Returns:
        无（直接修改 item）。
    """
    content = item.get("content")
    shot_id = content.get("shot_id") if isinstance(content, dict) else None
    shot_no: int | None = None
    if shot_id is not None:
        shot = await session.get(Shot, int(shot_id))
        if shot is not None:
            shot_no = int(shot.shot_no)
    item["shot_no"] = shot_no

    media: MediaFile | None = None
    if clip.source_type == "shot_version" and clip.source_id is not None:
        version = await session.get(ShotVersion, int(clip.source_id))
        if version is not None and version.media_file_id is not None:
            media = await repo.get_media_file(int(version.media_file_id))
    if media is None:
        return

    mime = str(media.mime_type or "")
    meta = media.meta if isinstance(media.meta, dict) else {}
    poster = meta.get("poster_key")
    is_json = mime == "application/json"
    item["media_url"] = f"/files/{poster}" if is_json and poster else media.url
    item["mime_type"] = "image/svg+xml" if is_json and poster else mime
    if isinstance(poster, str) and poster:
        item["thumb_url"] = f"/files/{poster}"
    elif mime.startswith("image/"):
        item["thumb_url"] = media.url


async def _enrich_audio_clip(
    session: AsyncSession,
    clip: TimelineClip,
    item: dict[str, Any],
) -> None:
    """为 audio 轨道片段补充可试听地址与台词信息。

    P2 修复：时间线 audio 片段只存 ``voice_clip`` 源 ID，前端后期页无法
    直接试听；这里解析 ``voice_clips.audio_url`` 并补充台词/说话人，
    便于页面逐句试听与核对。

    Args:
        session: 异步数据库会话（只读）。
        clip: 时间线片段（track_type=audio）。
        item: 已序列化的片段字典（原地补充字段）。

    Returns:
        无（直接修改 item）。
    """
    if clip.source_type != "voice_clip" or clip.source_id is None:
        return
    voice = await session.get(VoiceClip, int(clip.source_id))
    if voice is None:
        return
    if voice.audio_url:
        item["media_url"] = str(voice.audio_url)
    item["emotion"] = getattr(voice, "emotion", None)
    content = item.get("content")
    line_id = content.get("line_id") if isinstance(content, dict) else None
    line = await session.get(ScriptLine, int(line_id)) if line_id else None
    # 说话人存于剧本行（voice_clips 无 speaker_name 列），统一从台词行取
    item["speaker"] = line.speaker_name if line is not None else None
    if line is not None:
        item["text"] = str(line.content or "")


def _style_to_dict(style: SubtitleStyle) -> dict[str, Any]:
    """把字幕样式 ORM 序列化为响应字典。

    Args:
        style: 样式实例。

    Returns:
        样式字典。
    """
    return {
        "id": int(style.id),
        "name": style.name,
        "font_family": style.font_family,
        "font_size": int(style.font_size) if style.font_size is not None else None,
        "color": style.color,
        "position": style.position,
        "is_default": bool(style.is_default),
    }


def _bgm_to_dict(track: Any) -> dict[str, Any]:
    """把 BGM 曲目 ORM 序列化为响应字典（含版权信息）。

    Args:
        track: BgmTrack 实例。

    Returns:
        曲目字典（含 license_type / license_expires_at 版权字段）。
    """
    return {
        "id": int(track.id),
        "title": track.title,
        "artist": track.artist,
        "mood_tags": track.mood_tags,
        "bpm": int(track.bpm) if track.bpm is not None else None,
        "duration_ms": int(track.duration_ms),
        "license_type": track.license_type,
        "license_expires_at": _iso(track.license_expires_at),
    }


# =====================================================================
# TTS
# =====================================================================


async def tts_preview(
    session: AsyncSession, project: Project, episode_no: int | None = None
) -> dict[str, Any]:
    """生成 TTS 试听样音：取该集已渲染镜头台词前 3 句走 stub TTS。

    每次调用即时生成（不落时间线、不建 media_files）。

    Args:
        session: 请求级数据库会话（只读镜头）。
        project: ACL 校验通过的项目实例。
        episode_no: 集号过滤；None 表示取全剧前 3 句。

    Returns:
        ``{audio_url, duration_ms, lines}``，lines 元素为
        ``{shot_id, shot_no, text}``。

    Raises:
        BizError: 40001 无可试听台词；80001 TTS 生成失败。
    """
    repo = PostRepository(session)
    shots = await repo.list_rendered_shots(int(project.id), episode_no)
    lines: list[dict[str, Any]] = []
    for shot in shots:
        text = (shot.dialogue or "").strip()
        if not text:
            continue
        lines.append({"shot_id": int(shot.id), "shot_no": int(shot.shot_no), "text": text})
        if len(lines) >= TTS_PREVIEW_MAX_LINES:
            break
    if not lines:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "没有可试听的已渲染镜头台词")

    joined = "".join(item["text"] for item in lines)
    # 试听音色必须与成片一致：取首句台词所属角色绑定的音色，经配音服务统一
    # 解析为引擎音色名后传入；禁止再以 voice_id=None 落到默认音色。
    from app.models.asset import Asset
    from app.services import voice_service

    preview_voice_id: int | None = None
    preview_voice_name: str | None = None
    preview_voice_freq: float = 440.0
    first_text = lines[0]["text"]
    line_row = (
        await session.execute(
            select(ScriptLine)
            .where(
                ScriptLine.project_id == int(project.id),
                ScriptLine.is_deleted.is_(False),
                ScriptLine.content == first_text,
            )
            .order_by(ScriptLine.id.asc())
            .limit(1)
        )
    ).scalars().first()
    if line_row is not None and line_row.character_asset_id:
        asset = await session.get(Asset, int(line_row.character_asset_id))
        if asset is not None and asset.voice_id:
            preview_voice_id = int(asset.voice_id)
            preview_voice_name, preview_voice_freq = await voice_service.resolve_voice_profile(
                session, preview_voice_id
            )
    # 复用配音服务的合成策略：沙箱模式生成并落盘 WAV，生产模式走网关且失败时
    # 回退占位音频。避免直接调用网关时因供应商音色非法而返回 80001。
    synth = await voice_service._synthesize(  # noqa: SLF001 - 复用统一合成/降级逻辑
        project_id=int(project.id),
        text=joined,
        voice_id=preview_voice_id,
        rate=1.0,
        pitch=0,
        emotion=None,
        voice_name=preview_voice_name,
        voice_freq=preview_voice_freq,
    )
    return {
        "audio_url": synth["audio_url"],
        "duration_ms": int(synth["duration_ms"]),
        "lines": lines,
    }


async def tts_full(
    session: AsyncSession, project: Project, user_id: int
) -> dict[str, Any]:
    """全剧 TTS 配音（202 异步）：后台对每个含对白的已渲染镜头生成配音。

    同步阶段完成前置校验与时间线创建（保证前端可立即轮询），配音
    生成在后台管线执行：``media_files`` + ``timeline_clips``（audio 轨，
    起点与镜头起点对齐），完成后发 ``post.tts.done``。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        user_id: 触发用户 ID。

    Returns:
        ``{status, timeline_id, shot_count}``（status 恒为 processing）。

    Raises:
        BizError: 40001 没有可配音的已渲染镜头。
    """
    layout = await _layout_shots(session, int(project.id))
    dialogue_shot_ids = [
        item["shot_id"]
        for item in layout
        if ((item["shot"].dialogue or "")).strip()
    ]
    if not dialogue_shot_ids:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "没有可配音的已渲染镜头（含对白）")

    timeline = await _get_or_create_timeline(session, int(project.id))
    episode_nos = [
        int(no) for no in (
            await session.execute(
                select(Episode.episode_no)
                .where(Episode.project_id == int(project.id))
                .order_by(Episode.episode_no.asc())
            )
        ).scalars().all()
    ] or [1]
    await session.commit()

    _spawn(_run_tts_pipeline(
        int(project.id), int(timeline.id), episode_nos,
        str(int(project.owner_user_id or 0)),
    ))
    return {
        "status": "processing",
        "timeline_id": int(timeline.id),
        "shot_count": len(dialogue_shot_ids),
    }


async def _run_tts_pipeline(
    project_id: int, timeline_id: int, episode_nos: list[int], operator_user_id: str
) -> None:
    """TTS 后台管线：整集配音走 voice_service 统一链路，再同步到时间线 audio 轨。

    P1 修复（voice=default 400）：旧实现直接 ``model_gateway.generate`` 且
    硬编码 ``params={"voice": "default"}``——真实 qwen3-tts 返回 400
    Invalid voice specified，全剧配音必然失败；且该路径完全没有角色音色
    解析（与配音页行为不一致，所有人同腔同调）。现在改为：

    1. 逐集调用 :func:`voice_service.generate_track`（说话人→角色资产→
       绑定音色/参数中心参数→情绪与语速映射，VoiceClip 落库并计费）；
    2. 调用 :func:`_sync_voice_track` 把 voiced 片段按台词文本匹配写入
       时间线 audio 轨（source_type=voice_clip，音频驱动布局重排镜头起点）。

    Args:
        project_id: 项目 ID。
        timeline_id: 预览时间线 ID。
        episode_nos: 待配音集号列表。
        operator_user_id: 触发用户 ID（计费/审计归属，数字串则记 confirmed_by）。

    Raises:
        无：异常统一记日志并发 ``post.tts.failed`` 事件，不静默中断。
    """
    try:
        # 局部导入避免与 voice_service 形成模块级循环依赖
        from app.services import voice_service

        async with async_session_factory() as session:
            for ep_no in episode_nos:
                await voice_service.generate_track(
                    session,
                    project_id,
                    operator_user_id,
                    ep_no=ep_no,
                    line_ids=[],
                    force=True,
                )
            await session.commit()

        async with async_session_factory() as session:
            timeline = await session.get(Timeline, timeline_id)
            layout = await _layout_shots(session, project_id)
            async with _timeline_sync_lock(int(timeline_id)):
                sync = (
                    await _sync_voice_track(session, timeline, layout)
                    if timeline is not None
                    else {"written": 0, "starts": {}, "spans": {}, "total_ms": 0}
                )
            await session.commit()

        await event_bus.publish(
            project_id,
            "post.tts.done",
            {
                "timeline_id": timeline_id,
                "clip_count": int(sync.get("written", 0)),
                "duration_ms": int(sync.get("total_ms", 0)),
            },
        )
    except Exception as exc:  # noqa: BLE001 后台管线兜底：发失败事件不静默
        logger.exception("TTS 后台管线失败 project_id=%s", project_id)
        await event_bus.publish(project_id, "post.tts.failed", {"error": str(exc)[:300]})


# =====================================================================
# 字幕
# =====================================================================

#: 剧本舞台指示中结尾字幕的行首前缀（P2-6 修复）
_ENDCARD_PREFIX = "结尾字幕"


async def _extract_endcard_text(session: AsyncSession, project_id: int) -> str | None:
    """提取剧本舞台指示中的「结尾字幕」文本。

    P2-6 修复：剧本 ``line_type=stage_direction`` 中以「结尾字幕」开头的行
    （如「结尾字幕：给晚归的人，留一盏灯。」）此前不进入字幕轨，成片缺
    收尾字幕。这里按展示序扫描舞台指示行并提取正文。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。

    Returns:
        结尾字幕正文（去掉「结尾字幕：」前缀）；无此类行时返回 None。
    """
    rows = (
        (
            await session.execute(
                select(ScriptLine)
                .where(
                    ScriptLine.project_id == int(project_id),
                    ScriptLine.line_type == "stage_direction",
                )
                .order_by(ScriptLine.order_index.asc(), ScriptLine.id.asc())
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        text = str(row.content or "").strip()
        if not text.startswith(_ENDCARD_PREFIX):
            continue
        body = text.split("：", 1)[1].strip() if "：" in text else ""
        if not body:
            continue
        # 顿号分句时只取首句，避免整段舞台指示被塞进字幕
        body = body.split("；", 1)[0].strip()
        return body or None
    return None


async def _endcard_window(
    session: AsyncSession, timeline_id: int, duration_ms: int
) -> tuple[int, int]:
    """计算结尾字幕的时间窗（最后一个镜头画面区间；无视频轨回退片尾）。

    Args:
        session: 数据库会话。
        timeline_id: 时间线 ID。
        duration_ms: 时间线总时长（毫秒）。

    Returns:
        ``(start_ms, end_ms)``，保证 ``end > start``。
    """
    last_video = None
    for clip in await PostRepository(session).list_clips(int(timeline_id)):
        if clip.track_type == "video" and (
            last_video is None or int(clip.end_ms) > int(last_video.end_ms)
        ):
            last_video = clip
    if last_video is not None:
        start = int(last_video.start_ms)
        return start, max(int(last_video.end_ms), start + 1)
    total = max(1000, int(duration_ms or 0))
    return max(0, total - 3000), total


async def _rebuild_subtitle_track(
    session: AsyncSession,
    project: Project,
    timeline: Timeline,
    layout: list[dict[str, Any]],
    style_id: int | None,
) -> int:
    """按 audio 轨逐句重建字幕轨与逐条字幕行（调用方负责提交）。

    P1 修复（字幕与配音不同步）：``compose`` 会在配音重新生成后按新时长
    重排 audio/video 轨，但历史实现从不重建 subtitle 轨，导出时仍烧录旧
    时间轴上的错位字幕。这里把「按音频轨生成字幕」抽成公共实现，供字幕
    生成与合成复用。

    口径：有配音片段时逐句对齐其起点/时长，说话人取自剧本行
    ``speaker_name``（旁白为空）；无配音时回退按镜头对白生成。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        timeline: 预览时间线实例。
        layout: 镜头布局（无配音时回退使用）。
        style_id: 字幕样式 ID（可空）。

    Returns:
        重建后的字幕条数。
    """
    from sqlalchemy import delete as sa_delete

    from app.models.post import SubtitleLine

    repo = PostRepository(session)
    audio_clips = [
        clip
        for clip in await repo.list_clips(int(timeline.id))
        if clip.track_type == "audio"
    ]
    audio_clips.sort(key=lambda c: (int(c.start_ms), int(c.id)))

    await repo.delete_clips(int(timeline.id), "subtitle")

    # V4.0：同步重建逐条字幕行（供逐条编辑/合并/拆分，AC-F6-08-1）
    await session.execute(
        sa_delete(SubtitleLine).where(SubtitleLine.timeline_id == int(timeline.id))
    )

    count = 0
    subtitle_end_max = 0

    async def _add_subtitle(
        order: int, start_ms: int, duration_ms: int, text: str, speaker: str | None
    ) -> None:
        """写入一条字幕轨片段与对应字幕行。

        Args:
            order: 片段序号。
            start_ms: 起点（毫秒）。
            duration_ms: 时长（毫秒，最小 1）。
            text: 字幕文本。
            speaker: 说话人（旁白为 None）。

        Returns:
            None

        Raises:
            无：数据库异常交由外层事务处理。
        """
        nonlocal count, subtitle_end_max
        end_ms = int(start_ms) + max(1, int(duration_ms))
        await repo.add_clip(
            TimelineClip(
                timeline_id=int(timeline.id),
                track_type="subtitle",
                source_type=None,
                source_id=None,
                order_index=order,
                start_ms=int(start_ms),
                end_ms=end_ms,
                subtitle_style_id=style_id,
                content=_dump_content({"text": text, "style_id": style_id}),
            )
        )
        session.add(
            SubtitleLine(
                timeline_id=int(timeline.id),
                project_id=int(project.id),
                line_index=count + 1,
                start_ms=int(start_ms),
                end_ms=end_ms,
                content=_split_subtitle_text(text),
                speaker=speaker,
                style_ref=style_id,
            )
        )
        subtitle_end_max = max(subtitle_end_max, end_ms)
        count += 1

    if audio_clips:
        # ---- 逐句字幕：与配音片段时间轴一一对应 ----
        for order, clip in enumerate(audio_clips):
            content = parse_clip_content(clip.content)
            line_id = content.get("line_id") if isinstance(content, dict) else None
            line = await session.get(ScriptLine, int(line_id)) if line_id else None
            text = str(line.content or "").strip() if line is not None else ""
            if not text and isinstance(content, dict):
                # TTS 占位音频片段只带 text（无 line_id），同样要出字幕
                text = str(content.get("text") or "").strip()
            if not text:
                continue
            await _add_subtitle(
                order,
                int(clip.start_ms),
                int(clip.end_ms) - int(clip.start_ms),
                text,
                line.speaker_name if line is not None else None,
            )
    else:
        # ---- 无配音回退：按镜头对白生成（起点对齐镜头） ----
        for order, item in enumerate(layout):
            text = (item["shot"].dialogue or "").strip()
            if not text:
                continue
            await _add_subtitle(
                order,
                int(item["start_ms"]),
                int(item["duration_ms"]),
                text,
                text.split("：")[0][:64] if "：" in text else None,
            )

    # P2-6 修复：把剧本舞台指示里的「结尾字幕：…」渲染为一条无配音的
    # 结尾字幕（挂在最后一个镜头画面区间），剧本收尾字幕不再丢失。
    endcard_text = await _extract_endcard_text(session, int(project.id))
    if endcard_text:
        start_ms, end_ms = await _endcard_window(
            session, int(timeline.id), int(timeline.duration_ms or 0)
        )
        await _add_subtitle(count, start_ms, end_ms - start_ms, endcard_text, None)

    if subtitle_end_max:
        timeline.duration_ms = max(int(timeline.duration_ms or 0), subtitle_end_max)
    return count


async def _subtitle_rebuild_style(
    session: AsyncSession, timeline: Timeline
) -> tuple[bool, int | None]:
    """判断合成前是否需要重建字幕，并取回沿用的字幕样式。

    用户逐条手改过的字幕（``edited=True``）不重建，避免覆盖人工调整；
    从未生成过字幕的项目也不自动补，保持「字幕可选」的产品口径。

    Args:
        session: 请求级数据库会话。
        timeline: 预览时间线实例。

    Returns:
        ``(是否重建, 沿用样式 ID)``。
    """
    from app.models.post import SubtitleLine

    rows = (
        (
            await session.execute(
                select(SubtitleLine).where(
                    SubtitleLine.timeline_id == int(timeline.id)
                )
            )
        )
        .scalars()
        .all()
    )
    if rows:
        if any(bool(row.edited) for row in rows):
            return False, None
        return True, int(rows[0].style_ref) if rows[0].style_ref else None
    clips = [
        clip
        for clip in await PostRepository(session).list_clips(int(timeline.id))
        if clip.track_type == "subtitle"
    ]
    if clips:
        style = clips[0].subtitle_style_id
        return True, int(style) if style else None
    return False, None


async def make_subtitles(
    session: AsyncSession, project: Project, style_id: int | None = None
) -> dict[str, Any]:
    """按配音句生成字幕片段（重建 subtitle 轨，句级对齐配音起点与时长）。

    P1 修复（字幕与配音不同步）：历史实现按「镜头对白」生成，同一镜头
    拼接的多句台词只产出一条字幕，且时长取该镜最后一句的配音时长，导致
    双人连续对白出现「一条字幕挂两个人台词、时长只覆盖一半配音」的问题。
    现口径：

    1. 若时间线已有配音片段，则逐句生成字幕，起点/时长与对应配音一致，
       说话人取自剧本行 ``speaker_name``（旁白为空）；
    2. 无配音时回退按镜头对白生成（一条镜头一条字幕），保证无配音项目
       仍可出字幕。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        style_id: 字幕样式 ID（可空使用默认样式）。

    Returns:
        ``{timeline_id, subtitle_count, style_id, duration_ms}``。

    Raises:
        BizError: 40001 无可生成字幕的镜头；10002 字幕样式不存在。
    """
    repo = PostRepository(session)
    layout = await _layout_shots(session, int(project.id))
    if not layout:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "没有可生成字幕的已渲染镜头")

    if style_id is not None and await repo.get_subtitle_style(style_id) is None:
        raise BizError(ErrorCode.NOT_FOUND, "字幕样式不存在")

    timeline = await _get_or_create_timeline(session, int(project.id))
    # 生成字幕前先把 V6 配音同步到时间线（含音频驱动重排），
    # 之后字幕才能与真实配音的起点/时长逐句对齐。
    async with _timeline_sync_lock(int(timeline.id)):
        await _sync_voice_track(session, timeline, layout)
    await session.flush()

    count = await _rebuild_subtitle_track(
        session, project, timeline, layout, style_id
    )

    total = int(timeline.duration_ms)
    await session.commit()

    await event_bus.publish(
        int(project.id),
        "post.subtitles.done",
        {"timeline_id": int(timeline.id), "subtitle_count": count},
    )
    return {
        "timeline_id": int(timeline.id),
        "subtitle_count": count,
        "style_id": style_id,
        "duration_ms": total,
    }


async def list_subtitle_styles(session: AsyncSession, project_id: int | None) -> list[dict[str, Any]]:
    """查询字幕样式列表（全局 + 项目级）。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID（None 仅返回全局样式）。

    Returns:
        样式字典列表。
    """
    repo = PostRepository(session)
    styles = await repo.list_subtitle_styles(project_id)
    return [_style_to_dict(s) for s in styles]


# =====================================================================
# BGM
# =====================================================================


async def recommend_bgm(
    session: AsyncSession, project: Project, emotion: str
) -> dict[str, Any]:
    """按情绪标签推荐上架 BGM 曲目前 5 条（含版权信息）。

    Args:
        session: 异步数据库会话。
        project: ACL 校验通过的项目实例。
        emotion: 情绪标签（如 tension/joy）。

    Returns:
        ``{emotion, items}``，items 元素含 license 版权字段。
    """
    repo = PostRepository(session)
    tracks = await repo.list_bgm_by_emotion(emotion, 5)
    return {"emotion": emotion, "items": [_bgm_to_dict(t) for t in tracks]}


async def set_bgm(
    session: AsyncSession,
    project: Project,
    *,
    bgm_track_id: int,
    volume_db: float = -6.0,
    sidechain: bool = True,
) -> dict[str, Any]:
    """设置项目 BGM：以单个 bgm 片段形式写入时间线（重建 bgm 轨）。

    片段区间为 [0, max(时间线时长, 曲目时长)]，音量与闪避（sidechain）
    作为时间线元数据记录在片段上。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        bgm_track_id: BGM 曲目 ID。
        volume_db: 音量（dB，默认 -6）。
        sidechain: 是否启用闪避（人声压 BGM）。

    Returns:
        ``{timeline_id, bgm, clip, duration_ms}``。

    Raises:
        BizError: 10002 BGM 曲目不存在；10001 音量参数非法。
    """
    repo = PostRepository(session)
    track = await repo.get_bgm_track(bgm_track_id)
    if track is None:
        raise BizError(ErrorCode.NOT_FOUND, "BGM 曲目不存在")
    if not -60.0 <= volume_db <= 6.0:
        raise BizError(ErrorCode.PARAM_INVALID, "音量参数非法（-60 ~ 6 dB）")

    timeline = await _get_or_create_timeline(session, int(project.id))
    await repo.delete_clips(int(timeline.id), "bgm")

    # 修复：BGM 片段只覆盖当前成片时长，不再把时间线拉长到曲目全长——
    # 历史实现 end_ms 取 max(时间线时长, 曲目时长)，曲目普遍 45~180s，
    # 成片 30s 时会把导出成片拉长成"黑屏 + 静音垫底"的超长文件。
    # 曲目短于成片时由渲染端 apad 补齐（media_render_service 音频链）。
    end_ms = max(int(timeline.duration_ms or 0), 1)
    clip = TimelineClip(
        timeline_id=int(timeline.id),
        track_type="bgm",
        source_type="bgm",
        source_id=int(track.id),
        order_index=0,
        start_ms=0,
        end_ms=end_ms,
        volume_db=Decimal(str(volume_db)),
        transition={"sidechain": bool(sidechain)},
        content=_dump_content({"bgm_track_id": int(track.id), "title": track.title}),
    )
    await repo.add_clip(clip)
    timeline.duration_ms = max(int(timeline.duration_ms or 0), end_ms)
    total = int(timeline.duration_ms)
    await session.commit()

    await event_bus.publish(
        int(project.id),
        "post.bgm.updated",
        {"timeline_id": int(timeline.id), "bgm_track_id": int(track.id)},
    )
    return {
        "timeline_id": int(timeline.id),
        "bgm": _bgm_to_dict(track),
        "clip": _clip_to_dict(clip),
        "duration_ms": total,
    }


# =====================================================================
# 时间线
# =====================================================================


async def get_timeline_view(
    session: AsyncSession, project: Project, cut_type: str = "preview"
) -> dict[str, Any]:
    """查询时间线视图：时间线 + 四轨片段（按起点排序）+ 字幕样式 + BGM。

    D26 修复：预览时间线不存在时惰性创建并自动装配已渲染镜头 video 轨
    （首屏不再报"时间线不存在"）；母版时间线仅在合成产物就绪后存在，
    未合成时抛 10002 由前端展示空态。

    Args:
        session: 异步数据库会话。
        project: ACL 校验通过的项目实例。
        cut_type: 版本类型 preview/master。

    Returns:
        ``{timeline, clips, subtitle_styles, bgm}``。

    Raises:
        BizError: 10002 母版时间线尚未生成。
    """
    repo = PostRepository(session)
    if cut_type == "preview":
        timeline, _created = await _ensure_preview_timeline_with_video(
            session, int(project.id)
        )
    else:
        timeline = await repo.get_timeline(int(project.id), cut_type)
    if timeline is None:
        raise BizError(ErrorCode.NOT_FOUND, "时间线不存在")
    clips = await repo.list_clips(int(timeline.id))
    styles = await repo.list_subtitle_styles(int(project.id))

    bgm: dict[str, Any] | None = None
    for clip in clips:
        if clip.track_type == "bgm" and clip.source_id is not None:
            track = await repo.get_bgm_track(int(clip.source_id))
            if track is not None:
                bgm = _bgm_to_dict(track)
                break

    # video 片段补充真实媒体地址/缩略图，供前端成片监看直接播放；
    # audio 片段补充可试听地址与台词，subtitle 片段补充顶层文本。
    clip_items: list[dict[str, Any]] = []
    for clip in clips:
        item = _clip_to_dict(clip)
        if clip.track_type == "video":
            await _enrich_video_clip(session, repo, clip, item)
        elif clip.track_type == "audio":
            await _enrich_audio_clip(session, clip, item)
        elif clip.track_type == "subtitle":
            content = item.get("content")
            if isinstance(content, dict) and content.get("text"):
                item["text"] = str(content["text"])
        clip_items.append(item)

    return {
        "timeline": _timeline_to_dict(timeline),
        "clips": clip_items,
        "subtitle_styles": [_style_to_dict(s) for s in styles],
        "bgm": bgm,
    }


async def save_timeline(
    session: AsyncSession, project: Project, clips_payload: list[dict[str, Any]]
) -> dict[str, Any]:
    """重建时间线片段（乐观策略：全删全插），时间线回 draft 并重算时长。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        clips_payload: 片段字典列表（经 schema 校验），元素含
            ``track_type / shot_id? / start_ms / duration_ms / content?``。

    Returns:
        ``{timeline_id, status, duration_ms, clip_count}``。

    Raises:
        BizError: 10001 clips 为空或轨道类型非法。
    """
    if not clips_payload:
        raise BizError(ErrorCode.PARAM_INVALID, "clips 不能为空")

    repo = PostRepository(session)
    timeline = await _get_or_create_timeline(session, int(project.id))
    await repo.delete_clips(int(timeline.id))

    ordered = sorted(
        clips_payload, key=lambda c: (int(c.get("start_ms") or 0), str(c.get("track_type") or ""))
    )
    track_counter: dict[str, int] = {}
    max_end = 0
    for item in ordered:
        track_type = str(item.get("track_type") or "")
        if track_type not in _TRACK_TYPES:
            raise BizError(ErrorCode.PARAM_INVALID, f"非法轨道类型: {track_type}")
        start_ms = int(item.get("start_ms") or 0)
        duration_ms = int(item.get("duration_ms") or 0)
        if duration_ms <= 0:
            raise BizError(ErrorCode.PARAM_INVALID, "片段时长必须大于 0")
        shot_id = item.get("shot_id")
        content: dict[str, Any] = dict(item.get("content") or {})
        if shot_id is not None:
            content.setdefault("shot_id", int(shot_id))

        # video 轨携带 shot_id 时解析当前镜头版本作为源
        source_type: str | None = None
        source_id: int | None = None
        if track_type == "video" and shot_id is not None:
            version = await repo.get_current_shot_version(int(shot_id))
            if version is not None:
                source_type = "shot_version"
                source_id = int(version.id)

        style_id = content.get("style_id")
        order = track_counter.get(track_type, 0)
        track_counter[track_type] = order + 1
        end_ms = start_ms + duration_ms
        volume_db = BGM_VOLUME_DB if track_type == "bgm" else (
            DIALOGUE_VOLUME_DB if track_type == "audio" else None
        )
        clip = TimelineClip(
            timeline_id=int(timeline.id),
            track_type=track_type,
            source_type=source_type,
            source_id=source_id,
            order_index=order,
            start_ms=start_ms,
            end_ms=end_ms,
            volume_db=volume_db,
            subtitle_style_id=int(style_id) if isinstance(style_id, int) else None,
            content=_dump_content(content),
        )
        await repo.add_clip(clip)
        max_end = max(max_end, end_ms)

    timeline.status = "draft"
    timeline.duration_ms = max_end
    await session.commit()

    await event_bus.publish(
        int(project.id),
        "post.timeline.updated",
        {"timeline_id": int(timeline.id), "clip_count": len(ordered), "duration_ms": max_end},
    )
    return {
        "timeline_id": int(timeline.id),
        "status": "draft",
        "duration_ms": max_end,
        "clip_count": len(ordered),
    }


# =====================================================================
# 合成与审批（HITL-4）
# =====================================================================


#: BGM 自动铺轨音量（dB）：垫底音量低于配音，配合人声侧链闪避（P1-7）
_AUTO_BGM_VOLUME_DB: Decimal = Decimal("-14.00")


async def _auto_fill_bgm(
    session: AsyncSession, timeline: Timeline, layout: list[dict[str, Any]]
) -> None:
    """BGM 轨为空时按整片主导情绪自动铺一首有媒体的自有版权曲目。

    P1-7 修复：曲库媒体补齐前，时间线 bgm 轨长期为空，成片在无人声
    区间整镜静音。合成时自动补一首垫底 BGM（-12dB + 人声侧链闪避），
    用户仍可在后期页手动移除或换曲。片段区间只覆盖当前时间线时长，
    不把时间线拉长到曲目全长。

    Args:
        session: 请求级数据库会话（调用方负责提交）。
        timeline: 预览时间线实例。
        layout: 镜头布局（用于统计主导情绪）。

    Returns:
        None

    Raises:
        无：无可用曲目（未回填媒体）时静默跳过。
    """
    repo = PostRepository(session)
    for clip in await repo.list_clips(int(timeline.id)):
        if clip.track_type == "bgm":
            return
    emotions = [
        str(item["shot"].emotion_tag)
        for item in layout
        if getattr(item["shot"], "emotion_tag", None)
    ]
    dominant = max(set(emotions), key=emotions.count) if emotions else "calm"
    track = None
    for emotion in [dominant, "calm"]:
        for candidate in await repo.list_bgm_by_emotion(emotion, 5):
            if candidate.media_file_id:
                track = candidate
                break
        if track is not None:
            break
    if track is None:
        return
    end_ms = max(int(timeline.duration_ms or 0), 1)
    await repo.add_clip(
        TimelineClip(
            timeline_id=int(timeline.id),
            track_type="bgm",
            source_type="bgm",
            source_id=int(track.id),
            order_index=0,
            start_ms=0,
            end_ms=end_ms,
            volume_db=_AUTO_BGM_VOLUME_DB,
            transition={"sidechain": True},
            content=_dump_content(
                {"bgm_track_id": int(track.id), "title": track.title, "auto": True}
            ),
        )
    )


async def compose(session: AsyncSession, project: Project, user_id: int) -> dict[str, Any]:
    """发起合成（202 异步）：同步装配 + 后台 QC 与审批单创建。

    同步阶段：校验有已渲染镜头 → 获取/创建预览时间线 → 缺 video 轨时
    自动装配 → ``duration_ms = Σ镜头时长`` → 状态置 composing → 项目
    ``rendering → post_producing``。后台阶段执行音画同步 QC 并创建
    final_cut 审批单（见 :func:`_run_compose_pipeline`）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        user_id: 触发用户 ID。

    Returns:
        ``{timeline_id, status, duration_ms, shot_count}``。

    Raises:
        BizError: 40001 项目内没有已渲染镜头；70005 有台词镜头缺少有效配音。
    """
    repo = PostRepository(session)
    layout = await _layout_shots(session, int(project.id))
    if not layout:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "项目内没有已渲染镜头，无法合成")

    timeline = await _get_or_create_timeline(session, int(project.id))
    clips = await repo.list_clips(int(timeline.id))
    existing_video = [c for c in clips if c.track_type == "video"]
    # video 轨有效性：片段数量一致 **且** 每个片段都指向该镜头"当前版本"。
    # 只比数量会漏掉"重渲染后产物更新、时间线仍指向旧版本"的情况——
    # 表现为改了首帧/重渲染，成片画面却没有任何变化。
    current_version_by_shot = {
        int(item["shot_id"]): int(item["version_id"]) if item["version_id"] else None
        for item in layout
    }

    def _clip_shot_id(clip: TimelineClip) -> int:
        """读取视频片段内容里的镜头 ID。

        Args:
            clip: 时间线片段。

        Returns:
            镜头 ID；缺失或解析失败返回 0。
        """
        content = clip.content
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {}
        return int((content or {}).get("shot_id") or 0)

    has_video = len(layout) > 0 and len(existing_video) == len(layout) and all(
        int(clip.source_id or 0)
        == int(current_version_by_shot.get(_clip_shot_id(clip)) or 0)
        for clip in existing_video
    )
    total = sum(item["duration_ms"] for item in layout)

    if not has_video:
        await repo.delete_clips(int(timeline.id), "video")
        for order, item in enumerate(layout):
            clip = TimelineClip(
                timeline_id=int(timeline.id),
                track_type="video",
                source_type="shot_version" if item["version_id"] else None,
                source_id=item["version_id"],
                order_index=order,
                start_ms=item["start_ms"],
                end_ms=item["start_ms"] + item["duration_ms"],
                content=_dump_content({"shot_id": item["shot_id"]}),
            )
            await repo.add_clip(clip)

    timeline.status = "composing"
    timeline.duration_ms = total
    # P0 修复：合成前把 voice_clips 同步为 audio 轨，确保成片真的有配音；
    # P0-2 修复：同时按配音实测时长重排镜头布局（含 video 片段起止），
    # 避免同镜多句时第二句越过镜头终点导致音画同步 QC 失败。
    async with _timeline_sync_lock(int(timeline.id)):
        sync = await _sync_voice_track(session, timeline, layout)
    audio_count = int(sync["written"])
    if audio_count:
        logger.info(
            "配音轨同步 project_id=%s timeline_id=%s clips=%s total_ms=%s",
            project.id, timeline.id, audio_count, sync["total_ms"],
        )
    # P0 修复（禁止静默合成无声成片）：只要有台词镜头没有拿到“真实配音”，
    # 立即抛 70005 并带回缺失镜头清单。历史上此处只打日志、随后自动铺 BGM，
    # 导致成片有音轨、有人声缺失却无人察觉。
    missing_shots = list(sync.get("missing_shots") or [])
    placeholder_lines = list(sync.get("placeholder_lines") or [])
    failed_lines = list(sync.get("failed_lines") or [])
    if missing_shots or placeholder_lines:
        shot_nos = "、".join(
            str(item.get("shot_no") or item.get("shot_id")) for item in missing_shots
        ) or "未知"
        if placeholder_lines:
            message = (
                f"检测到沙箱占位配音（非真实人声），禁止合成：镜头 {shot_nos}。"
                "请在配音轨重新生成后再合成。"
            )
        else:
            message = (
                f"镜头 {shot_nos} 有台词但缺少有效配音，禁止合成无声成片。"
                "请先在配音轨生成/重配缺失台词。"
            )
        raise BizError(
            ErrorCode.VOICE_MISSING,
            message,
            data={
                "missing_shots": missing_shots,
                "placeholder_lines": placeholder_lines,
                "failed_lines": failed_lines,
                "expected_line_count": int(sync.get("expected_line_count") or 0),
                "voiced_line_count": int(sync.get("voiced_line_count") or 0),
            },
            error_detail="voice_missing",
        )
    # 配音超镜头原时长 1.5 倍：沿用“音频驱动自动拉长镜头”，仅记录告警，
    # 便于验收时定位“画面被定格补帧拉长”的镜头。
    overlong_shots = list(sync.get("overlong_shots") or [])
    if overlong_shots:
        logger.warning(
            "配音超镜头原时长1.5倍 project_id=%s timeline_id=%s shots=%s",
            project.id,
            timeline.id,
            [
                {
                    "shot_no": item.get("shot_no"),
                    "base_ms": item.get("base_ms"),
                    "voice_ms": item.get("voice_ms"),
                    "ratio": item.get("ratio"),
                }
                for item in overlong_shots
            ],
        )
    # P1 修复（字幕与配音不同步）：配音重排后必须按新 audio 轨重建字幕，
    # 否则重新生成配音/镜头时长变化后，导出仍烧录旧时间轴上的错位字幕。
    rebuild_subtitles, subtitle_style_id = await _subtitle_rebuild_style(
        session, timeline
    )
    if rebuild_subtitles:
        await session.flush()
        await _rebuild_subtitle_track(
            session, project, timeline, layout, subtitle_style_id
        )
    # P1-7 修复：BGM 轨为空时按主导情绪自动铺一首有媒体的自有版权曲目，
    # 避免成片在无人声区间整镜静音（用户仍可手动移除/换曲）。
    await _auto_fill_bgm(session, timeline, layout)
    # P0 剪辑模块优化：转场多样化分配 + 关键动作音效点自动对位
    await _apply_transitions(session, timeline)
    sfx_count = await _apply_sfx_points(session, timeline, layout)
    if sfx_count:
        logger.info("音效点自动对位完成 project_id=%s sfx=%s", project.id, sfx_count)
    total = max(total, int(timeline.duration_ms or 0), int(sync["total_ms"] or 0))
    timeline.duration_ms = total
    timeline_id = int(timeline.id)

    if project.status == "rendering":
        await project_service.transition(
            project, "post_producing", "后期合成发起", session=session, user_id=user_id
        )
    else:
        await session.commit()

    _spawn(_run_compose_pipeline(int(project.id), timeline_id))
    return {
        "timeline_id": timeline_id,
        "status": "composing",
        "duration_ms": total,
        "shot_count": len(layout),
    }


async def _run_compose_pipeline(project_id: int, timeline_id: int) -> None:
    """合成后台管线：音画同步 QC → pending_review + 审批单 + 事件。

    QC 规则：每个 audio 片段起点与其对应镜头起点偏差 > 100ms 即失败
    （70001，时间线标 failed，错误详情写入 edit_state.compose_error）；
    stub 构造时严格对齐，恒通过。通过后创建 final_cut 审批单并发布
    ``post.composed`` 与 ``approval.created`` 事件。

    Args:
        project_id: 项目 ID。
        timeline_id: 时间线 ID。
    """
    try:
        async with async_session_factory() as session:
            repo = PostRepository(session)
            timeline = await session.get(Timeline, timeline_id)
            if timeline is None:
                raise RuntimeError(f"时间线不存在 timeline_id={timeline_id}")

            layout = await _layout_shots(session, project_id)
            # P0-2 修复：音画同步以「时间线上 video 片段的实际区间」为准。
            # 音频驱动重排会把同镜多句的镜头拉长（video 片段 end_ms 同步扩展），
            # 而 _layout_shots 仍按镜头原始时长计算，直接用它会误判为越界。
            video_windows: dict[int, tuple[int, int]] = {}
            for clip in await repo.list_clips(timeline_id):
                if clip.track_type != "video":
                    continue
                content = parse_clip_content(clip.content)
                shot_id = content.get("shot_id") if isinstance(content, dict) else None
                if shot_id is None:
                    continue
                video_windows[int(shot_id)] = (int(clip.start_ms), int(clip.end_ms))
            start_by_shot = {item["shot_id"]: item["start_ms"] for item in layout}
            end_by_shot = {
                item["shot_id"]: item["start_ms"] + item["duration_ms"] for item in layout
            }

            for clip in await repo.list_clips(timeline_id):
                if clip.track_type != "audio":
                    continue
                content = parse_clip_content(clip.content)
                shot_id = content.get("shot_id") if isinstance(content, dict) else None
                if shot_id is None:
                    continue
                window = video_windows.get(int(shot_id))
                if window is not None:
                    shot_start, shot_end = window
                else:
                    shot_start = start_by_shot.get(int(shot_id))
                    if shot_start is None:
                        continue
                    shot_end = end_by_shot.get(int(shot_id), shot_start)
                # 同一镜头可能承载多句配音，第二句起在镜头内顺延，
                # 因此只校验音频起点是否落在所属镜头区间内（含容差），
                # 而不是要求与镜头起点绝对对齐。
                if (
                    int(clip.start_ms) < shot_start - AV_SYNC_TOLERANCE_MS
                    or int(clip.start_ms) > shot_end + AV_SYNC_TOLERANCE_MS
                ):
                    raise BizError(
                        ErrorCode.AV_SYNC_FAILED,
                        data={
                            "clip_id": int(clip.id),
                            "clip_start_ms": int(clip.start_ms),
                            "shot_start_ms": shot_start,
                            "shot_end_ms": shot_end,
                            "tolerance_ms": AV_SYNC_TOLERANCE_MS,
                        },
                    )

            # P0-2 修复：QC 通过后执行真实媒体合成（拼接镜头视频 + 混音 + 字幕
            # 烧录），成片落盘为 media_files 并挂到时间线；此前合成只装配片段
            # 元数据，页面/导出拿不到与时间线一致的成片。
            await session.commit()  # 先落盘 composing 状态，避免长时间占用事务
            try:
                render_result = await media_render_service.render_timeline(
                    session,
                    project_id=project_id,
                    timeline_id=timeline_id,
                    burn_subtitles=True,
                )
            except media_render_service.MediaRenderError as exc:
                raise BizError(
                    ErrorCode.STORAGE_ERROR,
                    f"成片合成失败：{str(exc)[:300]}",
                ) from exc
            # P0 修复（无声成片兜底）：渲染完成后必须用 ffprobe 证明输出含
            # 音频流且时长与时间线一致，否则按合成失败处理，禁止把静音轨
            # 或漏掉音频轨的产物登记为成片。
            try:
                media_render_service.verify_output_audio(
                    render_result.object_key,
                    expected_duration_ms=int(timeline.duration_ms or 0) or None,
                )
            except media_render_service.MediaRenderError as exc:
                raise BizError(
                    ErrorCode.VOICE_MISSING,
                    f"成片音频校验失败：{str(exc)[:300]}",
                    data={"object_key": render_result.object_key},
                    error_detail="output_audio_missing",
                ) from exc
            final_media = await media_render_service.create_timeline_media(
                session,
                project_id=project_id,
                result=render_result,
                note="合成成片",
            )
            final_media_id = int(final_media.id)
            timeline.rendered_media_file_id = final_media_id

            timeline.status = "pending_review"
            # P2 修复：合成成功时清理上一轮失败残留的 compose_error，
            # 否则前端仍展示「音画同步不达标」，与当前 pending_review 状态矛盾。
            state = dict(timeline.edit_state or {})
            if state.pop("compose_error", None) is not None:
                timeline.edit_state = state

            # D28 修复：合成提交终审即推进项目状态 post_producing → final_review
            project = await session.get(Project, project_id)
            if project is not None and project.status == "post_producing":
                await project_service.transition(
                    project, "final_review", "成片提交终审", session=session
                )

            # D27 修复：合成产物同步生成母版时间线（结构与预览版一致），
            # 使后期页「母版」tab 与导出链路可用
            master = await repo.get_timeline(project_id, "master")
            if master is None:
                master = await repo.create_timeline(project_id, "master")
            master.status = "pending_review"
            master.duration_ms = int(timeline.duration_ms)
            master.fps = timeline.fps
            master.resolution = timeline.resolution
            # 母版直接复用合成产物，保证预览/母版/导出声画一致
            master.rendered_media_file_id = final_media_id
            await repo.delete_clips(int(master.id))
            for src in await repo.list_clips(timeline_id):
                await repo.add_clip(
                    TimelineClip(
                        timeline_id=int(master.id),
                        track_type=src.track_type,
                        source_type=src.source_type,
                        source_id=src.source_id,
                        order_index=src.order_index,
                        start_ms=src.start_ms,
                        end_ms=src.end_ms,
                        volume_db=src.volume_db,
                        subtitle_style_id=src.subtitle_style_id,
                        content=src.content,
                    )
                )

            # 修复重复审批单：每次重合成都会新建 final_cut 审批单，旧单仍
            # 为 pending，审批列表出现多条同 target 待办，可能批到旧成片。
            # 新建前把同一时间线的历史待办置为 expired。
            await session.execute(
                update(ApprovalRequest)
                .where(
                    ApprovalRequest.project_id == project_id,
                    ApprovalRequest.approval_type == "final_cut",
                    ApprovalRequest.target_id == timeline_id,
                    ApprovalRequest.status == "pending",
                )
                .values(status="expired")
            )
            approval = ApprovalRequest(
                project_id=project_id,
                approval_type="final_cut",
                target_type="timeline",
                target_id=timeline_id,
                status="pending",
                expires_at=_utcnow() + timedelta(days=APPROVAL_TTL_DAYS),
            )
            await repo.add_approval(approval)
            approval_id = int(approval.id)
            duration_ms = int(timeline.duration_ms)
            render_payload = {
                "media_file_id": final_media_id,
                "url": render_result.url,
                "degraded": bool(render_result.degraded),
                "warnings": list(render_result.warnings),
            }
            await session.commit()

        await event_bus.publish(
            project_id,
            "post.composed",
            {
                "timeline_id": timeline_id,
                "status": "pending_review",
                "duration_ms": duration_ms,
                **render_payload,
            },
        )
        await event_bus.publish(
            project_id,
            "approval.created",
            {
                "approval_id": approval_id,
                "approval_type": "final_cut",
                "target_type": "timeline",
                "target_id": timeline_id,
            },
        )
    except BizError as exc:
        logger.warning("合成 QC 未通过 project_id=%s code=%s data=%s", project_id, exc.code, exc.data)
        await _mark_timeline_failed(project_id, timeline_id, exc.code, exc.message, exc.data)
    except Exception as exc:  # noqa: BLE001 后台管线兜底
        logger.exception("合成后台管线失败 project_id=%s", project_id)
        await _mark_timeline_failed(project_id, timeline_id, 10003, str(exc)[:300], None)


async def _mark_timeline_failed(
    project_id: int, timeline_id: int, code: int, message: str, data: Any
) -> None:
    """把时间线标记为合成失败并发失败事件。

    Args:
        project_id: 项目 ID。
        timeline_id: 时间线 ID。
        code: 业务错误码。
        message: 失败消息。
        data: 附加数据。
    """
    try:
        async with async_session_factory() as session:
            timeline = await session.get(Timeline, timeline_id)
            if timeline is not None:
                timeline.status = "failed"
                timeline.edit_state = {
                    "compose_error": {"code": code, "message": message, "data": data}
                }
                await session.commit()
        await event_bus.publish(
            project_id,
            "post.compose.failed",
            {"timeline_id": timeline_id, "code": code, "message": message},
        )
    except Exception:  # noqa: BLE001 标记失败本身不能抛
        logger.exception("标记时间线合成失败时出错 timeline_id=%s", timeline_id)


async def decide(
    session: AsyncSession,
    project: Project,
    user_id: int,
    *,
    action: str,
    feedback: str | None = None,
) -> dict[str, Any]:
    """成片审批决策（HITL-4）：approve 通过 / reject 驳回。

    approve：审批单 approved → 时间线 approved（记 approved_at）→
    项目 ``post_producing → final_review → completed``（母版生成留给
    导出时执行）。reject：反馈须 ≥10 字，审批单 rejected → 时间线回
    draft。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        user_id: 决策人用户 ID。
        action: 决策动作（approve/reject）。
        feedback: 驳回反馈（reject 时必填且 ≥10 字）。

    Returns:
        ``{decision, timeline_id, timeline_status, project_status,
        feedback}``。

    Raises:
        BizError: 40001 当前没有待审批的成片；10001 驳回反馈不足 10 字。
    """
    repo = PostRepository(session)
    timeline = await repo.get_timeline(int(project.id), "preview")
    if timeline is None or timeline.status != "pending_review":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "当前没有待审批的成片")

    approval = await repo.get_pending_final_cut_approval(int(project.id), int(timeline.id))
    if approval is None:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "当前没有待审批的成片")

    now = _utcnow()
    if action == "approve":
        approval.status = "approved"
        approval.decision = "approve"
        approval.decided_by_user_id = user_id
        approval.decided_at = now
        timeline.status = "approved"
        timeline.approved_at = now
        timeline_id = int(timeline.id)
        await session.commit()

        # 项目状态机：failed 自动恢复 → post_producing → final_review → completed
        # 修复（测试报告 #29/#35）：项目若因渲染/合成失败处于 failed，审批通过
        # 本身是人工恢复信号——先自动恢复再链式推进，避免审批与项目状态脱节。
        await project_service.recover_failed_project(
            session, project, "成片审批通过，自动恢复失败项目", user_id=user_id,
        )
        if project.status in ("rendering", "post_producing", "final_review"):
            await project_service.advance_chain(
                project,
                ["post_producing", "final_review", "completed"],
                "成片审批通过",
                session=session,
                user_id=user_id,
            )

        await event_bus.publish(
            int(project.id),
            "post.approved",
            {"timeline_id": timeline_id, "project_status": project.status},
        )
        # 埋点：HITL-4 成片关口通过
        await analytics_service.track_gate(
            analytics_service.GATE_MASTER,
            True,
            user_id=user_id,
            project_id=int(project.id),
        )
        return {
            "decision": "approve",
            "timeline_id": timeline_id,
            "timeline_status": "approved",
            "project_status": project.status,
            "feedback": None,
        }

    # reject
    text = (feedback or "").strip()
    if len(text) < 10:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "驳回反馈至少 10 个字，请说明修改方向",
            data={"min_length": 10, "actual_length": len(text)},
        )
    approval.status = "rejected"
    approval.decision = "reject"
    approval.decided_by_user_id = user_id
    approval.decided_at = now
    approval.feedback = text
    timeline.status = "draft"
    timeline_id = int(timeline.id)
    await session.commit()

    await event_bus.publish(
        int(project.id),
        "post.rejected",
        {"timeline_id": timeline_id, "feedback": text},
    )
    # 埋点：HITL-4 成片关口驳回
    await analytics_service.track_gate(
        analytics_service.GATE_MASTER,
        False,
        user_id=user_id,
        project_id=int(project.id),
    )
    return {
        "decision": "reject",
        "timeline_id": timeline_id,
        "timeline_status": "draft",
        "project_status": project.status,
        "feedback": text,
    }


async def preview_url(session: AsyncSession, project: Project) -> dict[str, Any]:
    """获取带水印的成片预览地址（优先真实 MP4，缺失时生成可播放占位 MP4）。

    取址顺序：
    1. 最近一次成功导出且产物 MIME 为 ``video/*`` 的任务（真实成片）；
    2. 无导出成片时按时间线时长/分辨率生成纯色占位 MP4，落
       ``post/previews``，保证前端有可播放视频；
    3. ffmpeg 不可用时回退首帧 poster SVG，再回退 JSON 元数据占位。

    Args:
        session: 异步数据库会话。
        project: ACL 校验通过的项目实例。

    Returns:
        ``{url, watermark, timeline_status}``；url 为可直接播放或展示的地址。
    """
    repo = PostRepository(session)
    timeline = await repo.get_timeline(int(project.id), "preview")
    layout = await _layout_shots(session, int(project.id))
    timeline_status = timeline.status if timeline else None
    duration_ms = (
        int(timeline.duration_ms)
        if timeline is not None
        else sum(item["duration_ms"] for item in layout)
    )

    # 1) 复用最近一次成功导出的真实成片（带水印口径与导出一致）
    export_stmt = (
        select(ExportJob)
        .where(
            ExportJob.project_id == int(project.id),
            ExportJob.status == "succeeded",
            ExportJob.output_media_file_id.is_not(None),
        )
        .order_by(ExportJob.id.desc())
        .limit(1)
    )
    export = (await session.execute(export_stmt)).scalar_one_or_none()
    if export is not None:
        export_media = await repo.get_media_file(int(export.output_media_file_id))
        if export_media is not None and str(export_media.mime_type or "").startswith("video/"):
            return {
                "url": export_media.url,
                "watermark": bool(export.has_watermark),
                "timeline_status": timeline_status,
            }

    # 2) 复用 compose 阶段真实渲染的预览成片（Q-010）。
    #    compose 已在 _run_compose_pipeline 调用 render_timeline 并挂到
    #    timeline.rendered_media_file_id；此前 preview_url 忽略它，直接生成
    #    纯色占位 MP4，导致后期页预览全黑。
    if timeline is not None and timeline.rendered_media_file_id:
        preview_media = await repo.get_media_file(int(timeline.rendered_media_file_id))
        if preview_media is not None and str(preview_media.mime_type or "").startswith("video/"):
            return {
                "url": preview_media.url,
                "watermark": True,
                "timeline_status": timeline_status,
            }

    # 3) 无导出成片且无预览渲染时生成与时间线等长的占位 MP4（保证可播放）
    if duration_ms > 0:
        resolution = str(timeline.resolution) if timeline is not None else "1080p"
        width, height = _PREVIEW_SIZES.get(resolution, (1080, 1920))
        fps = int(timeline.fps) if timeline is not None else 24
        video_bytes = await asyncio.to_thread(
            _build_placeholder_mp4, width, height, duration_ms, max(1, fps)
        )
        if video_bytes:
            timeline_id = int(timeline.id) if timeline is not None else 0
            key = storage.save_bytes(
                f"post/previews/{int(project.id)}_{timeline_id}.mp4", video_bytes
            )
            return {
                "url": f"/files/{key}",
                "watermark": True,
                "timeline_status": timeline_status,
            }

    # 3) ffmpeg 不可用：回退首帧 poster
    url: str | None = None
    if layout and layout[0]["version_id"]:
        version = await session.get(ShotVersion, layout[0]["version_id"])
        if version is not None and version.media_file_id is not None:
            media = await repo.get_media_file(int(version.media_file_id))
            poster = (media.meta or {}).get("poster_key") if media else None
            if isinstance(poster, str) and poster:
                url = f"/files/{poster}"

    if url is None:
        payload = {
            "placeholder": True,
            "kind": "preview",
            "project_id": int(project.id),
            "timeline_id": int(timeline.id) if timeline else None,
            "duration_ms": duration_ms,
            "note": "预览合成产物占位（演示环境）",
            "generated_at": _iso(_utcnow()),
        }
        key = storage.save_bytes(
            f"post/previews/{new_request_id().lower()}.json",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        url = f"/files/{key}"

    return {
        "url": url,
        "watermark": True,
        "timeline_status": timeline_status,
    }


# =====================================================================
# V4.0 后期域增量：逐角色调参 / 字幕逐条编辑 / BGM 替换 / 混音响度
# =====================================================================

#: 单句字幕字数上限（AC-F6-02-2【假设待验证：断句规则与上限】）
MAX_SUBTITLE_CHARS: int = 20

#: 音画同步容差沿用模块级 AV_SYNC_TOLERANCE_MS（AC-F6-08-2）

#: 语速可调区间（AC-F6-07-1）
VOICE_RATE_MIN: float = 0.80
VOICE_RATE_MAX: float = 1.50

#: 语调偏移档位区间（±3 档，AC-F6-07-1）
VOICE_PITCH_MIN: int = -3
VOICE_PITCH_MAX: int = 3

#: 逐轨音量相对自动混音可调区间（±6dB，AC-F6-10-1）
TRACK_VOLUME_RANGE_DB: float = 6.0

#: 对白镜头时长上限（毫秒，AC-F6-07-3 与 AC-F4-02-2 同源）
DIALOGUE_DURATION_LIMIT_MS: int = 6000


def _split_subtitle_text(text: str) -> str:
    """把字幕文本截断到单句字数上限（超长自动断句，AC-F6-02-2）。

    Args:
        text: 原始字幕文本。

    Returns:
        截断后的字幕文本（≤20 字，优先在标点处断句）。
    """
    cleaned = text.strip()
    if len(cleaned) <= MAX_SUBTITLE_CHARS:
        return cleaned
    buffer = ""
    for char in cleaned:
        if len(buffer) >= MAX_SUBTITLE_CHARS:
            break
        buffer += char
        if char in "，。！？、；：,.!?;:" and len(buffer) >= 6:
            break
    return buffer.strip("，。！？、；：,.!?;:") or cleaned[:MAX_SUBTITLE_CHARS]


def voice_binding_to_dict(binding: Any) -> dict[str, Any]:
    """把配音绑定 ORM 对象序列化为响应字典。

    Args:
        binding: VoiceBinding ORM 实例。

    Returns:
        配音绑定响应字典。
    """
    return {
        "id": int(binding.id),
        "asset_id": int(binding.asset_id),
        "voice_id": int(binding.voice_id) if binding.voice_id else None,
        "params": binding.params or {},
        "history_count": len((binding.history or {}).get("versions") or []),
        "preview_confirmed": bool(binding.preview_confirmed),
        "confirmed_snapshot": binding.confirmed_snapshot,
        "preview_media_id": int(binding.preview_media_id) if binding.preview_media_id else None,
        "affected_line_count": int(binding.affected_line_count or 0),
        "qc_warning": binding.qc_warning,
    }


async def _get_or_create_voice_binding(
    session: AsyncSession, asset_id: int
) -> Any:
    """取或创建角色配音绑定行。

    Args:
        session: 请求级数据库会话。
        asset_id: 角色资产 ID。

    Returns:
        VoiceBinding ORM 实例。
    """
    from app.models.asset import VoiceBinding

    binding = (
        await session.execute(
            select(VoiceBinding).where(VoiceBinding.asset_id == asset_id)
        )
    ).scalar_one_or_none()
    if binding is None:
        binding = VoiceBinding(asset_id=asset_id)
        session.add(binding)
        await session.flush()
    return binding


async def update_voice_params(
    session: AsyncSession,
    project: Project,
    *,
    character_id: int,
    params: dict[str, Any],
    user_id: int,
) -> dict[str, Any]:
    """逐角色语速/语调调参（AC-F6-07-1~3）。

    参数区间：语速 0.80~1.50（步进 0.05）、语调偏移 ±3 档、情绪标签按场覆盖；
    参数按角色保存并保留最近 3 版历史供回退。语速调慢导致台词超镜头时长时
    返回 ``qc_warning`` 并给出两条出路（调回语速 / 解锁分镜改时长），
    后期环节不自动改镜头。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        character_id: 角色资产 ID。
        params: 参数字典 {rate, pitch_shift, emotion_override}。
        user_id: 操作人用户 ID。

    Returns:
        ``{binding, qc_warning, affected_line_count}``。

    Raises:
        BizError: 10002 角色资产不存在；10001 语速/语调越界或资产非角色。
    """
    from app.models.asset import Asset
    from app.models.storyboard import Shot

    asset = await session.get(Asset, character_id)
    if asset is None or int(asset.project_id or 0) != int(project.id):
        raise BizError(ErrorCode.NOT_FOUND, "角色资产不存在或不属于当前项目")
    if asset.asset_type != "character":
        raise BizError(ErrorCode.PARAM_INVALID, "仅角色资产可调整配音参数")

    rate = float(params.get("rate", 1.0))
    pitch = int(params.get("pitch_shift", 0))
    if not VOICE_RATE_MIN <= rate <= VOICE_RATE_MAX:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"语速需在 {VOICE_RATE_MIN}~{VOICE_RATE_MAX} 之间",
            error_detail="voice_rate_out_of_range",
        )
    if not VOICE_PITCH_MIN <= pitch <= VOICE_PITCH_MAX:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"语调偏移需在 {VOICE_PITCH_MIN}~{VOICE_PITCH_MAX} 档之间",
            error_detail="voice_pitch_out_of_range",
        )
    # 步进 0.05 校验（四舍五入到分位后比较）
    if abs(round(rate * 20) - rate * 20) > 1e-6:
        raise BizError(
            ErrorCode.PARAM_INVALID, "语速步进为 0.05", error_detail="voice_rate_step_invalid"
        )

    binding = await _get_or_create_voice_binding(session, character_id)
    previous = dict(binding.params or {})
    history = dict(binding.history or {})
    versions = list(history.get("versions") or [])
    if previous:
        versions.append({"params": previous, "at": _iso(_utcnow())})
    binding.history = {"versions": versions[-3:]}
    binding.params = {
        "rate": round(rate, 2),
        "pitch_shift": pitch,
        "emotion_override": params.get("emotion_override") or [],
    }
    binding.voice_id = asset.voice_id

    # 台词时长质检：语速 < 1.0 时台词变长，可能超镜头 6 秒（AC-F6-07-3）
    affected = 0
    qc_warning: dict[str, Any] | None = None
    shots = (
        await session.execute(
            select(Shot).where(
                Shot.project_id == int(project.id),
                Shot.character_asset_ids.contains([character_id]),
            )
        )
    ).scalars().all()
    for shot in shots:
        if not shot.dialogue:
            continue
        affected += 1
        base_chars = len(shot.dialogue)
        estimated_ms = int(base_chars / 4.0 / max(rate, 0.1) * 1000)
        if estimated_ms > min(DIALOGUE_DURATION_LIMIT_MS, int(shot.duration_ms)):
            qc_warning = {
                "shot_id": int(shot.id),
                "shot_no": int(shot.shot_no),
                "estimated_ms": estimated_ms,
                "shot_duration_ms": int(shot.duration_ms),
                "message": "调慢语速导致台词超出镜头时长",
                "options": ["调回语速", "解锁分镜改时长"],
            }
            break
    binding.affected_line_count = affected
    binding.qc_warning = qc_warning
    binding.preview_confirmed = False
    await session.commit()
    await session.refresh(binding)
    return {
        "binding": voice_binding_to_dict(binding),
        "qc_warning": qc_warning,
        "affected_line_count": affected,
    }


async def retry_voice_preview(
    session: AsyncSession, project: Project, *, character_id: int
) -> dict[str, Any]:
    """重新试听该角色（仅该角色台词重合成，AC-F6-07-2）。

    返回受影响台词数供「全剧应用」按钮展示；样音确认后写入参数快照
    （AC-F6-01-3 可追溯）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        character_id: 角色资产 ID。

    Returns:
        ``{binding, preview_media_url, affected_line_count}``。

    Raises:
        BizError: 10002 角色资产不存在；80001 音色服务不可用。
    """
    from app.models.asset import Asset, MediaFile
    from app.services import voice_service

    asset = await session.get(Asset, character_id)
    if asset is None or int(asset.project_id or 0) != int(project.id):
        raise BizError(ErrorCode.NOT_FOUND, "角色资产不存在或不属于当前项目")
    binding = await _get_or_create_voice_binding(session, character_id)
    params = binding.params or {"rate": 1.0, "pitch_shift": 0}
    # 复用配音服务统一读取链路：样音文本取该角色首句台词，音色取
    # assets.voice_id 并解析为真实引擎音色，避免直接调网关时把
    # voice_id 当作引擎参数导致永远落到默认音色。
    sample = await voice_service.generate_sample(
        session,
        int(project.id),
        "system",
        int(character_id),
        text=None,
        rate=float(params.get("rate") or 1.0),
        pitch=int(params.get("pitch_shift") or 0),
    )
    media = MediaFile(
        project_id=int(project.id),
        storage_provider="cos",
        bucket="gleamdream-local",
        object_key=sample["audio_key"],
        url=sample["audio_url"],
        media_type="audio",
        mime_type="audio/wav",
        duration_ms=int(sample["duration_ms"]),
        meta={
            "simulated": bool(sample.get("sandbox")),
            "purpose": "voice_preview",
            "engine_voice": sample.get("voice"),
        },
    )
    session.add(media)
    await session.flush()
    binding.preview_media_id = int(media.id)
    binding.preview_confirmed = True
    binding.confirmed_snapshot = dict(params)
    await session.commit()
    await session.refresh(binding)
    return {
        "binding": voice_binding_to_dict(binding),
        "preview_media_url": sample["audio_url"],
        "affected_line_count": int(binding.affected_line_count or 0),
    }


def subtitle_line_to_dict(line: Any) -> dict[str, Any]:
    """把逐条字幕 ORM 对象序列化为响应字典。

    Args:
        line: SubtitleLine ORM 实例。

    Returns:
        字幕行响应字典。
    """
    return {
        "id": int(line.id),
        "timeline_id": int(line.timeline_id),
        "line_index": int(line.line_index),
        "start_ms": int(line.start_ms),
        "end_ms": int(line.end_ms),
        "text": line.content,
        "speaker": line.speaker,
        "style_ref": int(line.style_ref) if line.style_ref else None,
        "edited": bool(line.edited),
        "merged_from": line.merged_from,
        "out_of_safe_area": bool(line.out_of_safe_area),
    }


async def list_subtitle_lines(
    session: AsyncSession, project: Project, cut_type: str = "preview"
) -> dict[str, Any]:
    """查询逐条字幕（时间线字幕轨的结构化视图）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        cut_type: 版本类型 preview/master。

    Returns:
        ``{timeline_id, items, total, max_chars}``。

    Raises:
        BizError: 10002 时间线不存在。
    """
    from app.models.post import SubtitleLine

    repo = PostRepository(session)
    timeline = await repo.get_timeline(int(project.id), cut_type)
    if timeline is None:
        raise BizError(ErrorCode.NOT_FOUND, "时间线不存在")
    rows = (
        await session.execute(
            select(SubtitleLine)
            .where(SubtitleLine.timeline_id == int(timeline.id))
            .order_by(SubtitleLine.line_index.asc())
        )
    ).scalars().all()
    return {
        "timeline_id": int(timeline.id),
        "items": [subtitle_line_to_dict(row) for row in rows],
        "total": len(rows),
        "max_chars": MAX_SUBTITLE_CHARS,
    }


async def patch_subtitle_line(
    session: AsyncSession,
    project: Project,
    *,
    line_id: int,
    text: str | None,
    start_ms: int | None,
    end_ms: int | None,
    user_id: int,
) -> dict[str, Any]:
    """逐条编辑字幕（改文本 / 出入点微调），并同步字幕轨片段（AC-F6-08-1~2）。

    出入点调整后若与音频轨片段的时间差超过 100ms，拒绝保存并返回
    ``AV_SYNC_FAILED``（与 AC-F6-02-1 质检口径一致）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        line_id: 字幕行 ID。
        text: 新文本（不传不改）。
        start_ms: 新起始毫秒（不传不改）。
        end_ms: 新结束毫秒（不传不改）。
        user_id: 操作人用户 ID。

    Returns:
        ``{line, sync_error_ms}``。

    Raises:
        BizError: 10001 无可更新字段或时间区间非法；10002 字幕行不存在；
            70001 音画同步误差超 100ms。
    """
    from app.models.post import SubtitleLine

    line = await session.get(SubtitleLine, line_id)
    if line is None or int(line.project_id) != int(project.id):
        raise BizError(ErrorCode.NOT_FOUND, "字幕行不存在")
    if text is None and start_ms is None and end_ms is None:
        raise BizError(ErrorCode.PARAM_INVALID, "至少提供 text/start_ms/end_ms 之一")

    new_start = int(start_ms) if start_ms is not None else int(line.start_ms)
    new_end = int(end_ms) if end_ms is not None else int(line.end_ms)
    if new_end <= new_start:
        raise BizError(
            ErrorCode.PARAM_INVALID, "结束时间必须大于起始时间", error_detail="subtitle_time_range_invalid"
        )
    if text is not None and not text.strip():
        raise BizError(ErrorCode.PARAM_INVALID, "字幕文本不能为空")

    if text is not None:
        line.content = text.strip()
    line.start_ms = new_start
    line.end_ms = new_end
    line.edited = True

    # 音画同步守卫：与字幕轨对应片段比较起点
    repo = PostRepository(session)
    sync_error_ms = 0
    clips = await repo.list_clips(int(line.timeline_id))
    for clip in clips:
        if clip.track_type != "subtitle":
            continue
        if int(clip.order_index) == int(line.line_index) - 1:
            sync_error_ms = abs(int(clip.start_ms) - new_start)
            if sync_error_ms > AV_SYNC_TOLERANCE_MS:
                await session.rollback()
                raise BizError(
                    ErrorCode.AV_SYNC_FAILED,
                    f"出入点调整导致音画同步误差 {sync_error_ms}ms（上限 {AV_SYNC_TOLERANCE_MS}ms）",
                    data={"sync_error_ms": sync_error_ms, "tolerance_ms": AV_SYNC_TOLERANCE_MS},
                    error_detail="av_sync_exceeded",
                )
            clip.start_ms = new_start
            clip.end_ms = new_end
            clip.content = _dump_content({"text": line.content, "style_id": line.style_ref})
            break
    await session.commit()
    return {"line": subtitle_line_to_dict(line), "sync_error_ms": sync_error_ms}


async def batch_style_subtitles(
    session: AsyncSession,
    project: Project,
    *,
    style_id: int,
    scope: str,
    speaker: str | None,
    user_id: int,
) -> dict[str, Any]:
    """批量修改字幕样式（AC-F6-08-3）。

    作用范围：``all`` 全片 / ``episode`` 按集（当前时间线）/ ``speaker`` 按说话角色。
    应用后重跑安全区与截断质检，超安全区条目单独列出。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        style_id: 目标字幕样式 ID。
        scope: 作用范围 all/episode/speaker。
        speaker: 按角色应用时的说话人。
        user_id: 操作人用户 ID。

    Returns:
        ``{timeline_id, style_id, applied, out_of_safe_area}``。

    Raises:
        BizError: 10001 范围非法/缺少 speaker；10002 样式或时间线不存在。
    """
    from app.models.post import SubtitleLine, SubtitleStyle

    if scope not in ("all", "episode", "speaker"):
        raise BizError(
            ErrorCode.PARAM_INVALID, "scope 仅支持 all/episode/speaker", error_detail="scope_invalid"
        )
    if scope == "speaker" and not speaker:
        raise BizError(ErrorCode.PARAM_INVALID, "按角色应用时必须提供 speaker")
    repo = PostRepository(session)
    style = await session.get(SubtitleStyle, style_id)
    if style is None:
        raise BizError(ErrorCode.NOT_FOUND, "字幕样式不存在")
    timeline = await repo.get_timeline(int(project.id), "preview")
    if timeline is None:
        raise BizError(ErrorCode.NOT_FOUND, "时间线不存在")

    stmt = select(SubtitleLine).where(SubtitleLine.timeline_id == int(timeline.id))
    if scope == "speaker":
        stmt = stmt.where(SubtitleLine.speaker == speaker)
    rows = (await session.execute(stmt.order_by(SubtitleLine.line_index.asc()))).scalars().all()

    # 安全区质检：单句 ≤20 字视为可安全显示（简化口径，AC-F6-08-3）
    out_of_safe: list[dict[str, Any]] = []
    for row in rows:
        row.style_ref = style_id
        if len(row.content) > MAX_SUBTITLE_CHARS:
            row.out_of_safe_area = True
            out_of_safe.append(
                {
                    "line_id": int(row.id),
                    "line_index": int(row.line_index),
                    "chars": len(row.content),
                    "reason": f"单句超过 {MAX_SUBTITLE_CHARS} 字，可能被截断",
                }
            )
        else:
            row.out_of_safe_area = False
    for clip in await repo.list_clips(int(timeline.id)):
        if clip.track_type == "subtitle":
            clip.subtitle_style_id = style_id
    await session.commit()
    logger.info(
        "字幕样式批量应用 project_id=%s style_id=%s scope=%s applied=%s",
        project.id, style_id, scope, len(rows),
    )
    return {
        "timeline_id": int(timeline.id),
        "style_id": style_id,
        "scope": scope,
        "applied": len(rows),
        "out_of_safe_area": out_of_safe,
    }


async def replace_bgm(
    session: AsyncSession,
    project: Project,
    *,
    timeline_id: int,
    bgm_track_id: int,
    volume_db: float | None,
    user_id: int,
) -> dict[str, Any]:
    """BGM 手动替换（含授权范围校验与临期提示，AC-F6-09-1~2）。

    试听免费；选中替换后自动重新卡点与侧链闪避（以片段 transition 记录）。
    曲目授权范围 ``platform_only`` 时在分发前需拦截（与 F7 联动）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        timeline_id: 目标时间线 ID。
        bgm_track_id: 替换为的 BGM 曲目 ID。
        volume_db: 音量（None 沿用自动混音值 -6dB）。
        user_id: 操作人用户 ID。

    Returns:
        ``{timeline_id, bgm, license_warning, license_scope}``。

    Raises:
        BizError: 10002 时间线/曲目不存在；10001 时间线不属于该项目。
    """
    from app.models.post import Timeline

    timeline = await session.get(Timeline, timeline_id)
    if timeline is None or int(timeline.project_id) != int(project.id):
        raise BizError(ErrorCode.NOT_FOUND, "时间线不存在或不属于当前项目")
    repo = PostRepository(session)
    track = await repo.get_bgm_track(bgm_track_id)
    if track is None:
        raise BizError(ErrorCode.NOT_FOUND, "BGM 曲目不存在")
    if volume_db is not None and not -60.0 <= volume_db <= 6.0:
        raise BizError(ErrorCode.PARAM_INVALID, "音量参数非法（-60 ~ 6 dB）")

    await repo.delete_clips(int(timeline.id), "bgm")
    end_ms = max(int(timeline.duration_ms or 0), int(track.duration_ms or 0), 1)
    clip = TimelineClip(
        timeline_id=int(timeline.id),
        track_type="bgm",
        source_type="bgm",
        source_id=int(track.id),
        order_index=0,
        start_ms=0,
        end_ms=end_ms,
        volume_db=Decimal(str(volume_db if volume_db is not None else -6.0)),
        transition={"sidechain": True, "replaced_by_user": user_id},
        content=_dump_content({"bgm_track_id": int(track.id), "title": track.title}),
    )
    await repo.add_clip(clip)
    timeline.duration_ms = max(int(timeline.duration_ms or 0), end_ms)

    license_warning: str | None = None
    if track.license_expires_at is not None:
        days_left = (track.license_expires_at - _utcnow()).days
        if days_left <= 30:
            license_warning = (
                f"该曲 {max(days_left, 0)} 天后到期，届时自动替换为同情绪替代曲"
            )
    await session.commit()
    await event_bus.publish(
        int(project.id),
        "post.bgm.updated",
        {"timeline_id": int(timeline.id), "bgm_track_id": int(track.id)},
    )
    return {
        "timeline_id": int(timeline.id),
        "bgm": _bgm_to_dict(track),
        "clip": _clip_to_dict(clip),
        "license_scope": track.license_scope,
        "license_warning": license_warning,
        "distribution_blocked": track.license_scope == "platform_only",
    }


def mix_profile_to_dict(profile: Any) -> dict[str, Any]:
    """把混音画像 ORM 对象序列化为响应字典。

    Args:
        profile: MixProfile ORM 实例。

    Returns:
        混音画像响应字典。
    """
    return {
        "id": int(profile.id),
        "timeline_id": int(profile.timeline_id),
        "loudness_target": float(profile.loudness_target),
        "true_peak_db": float(profile.true_peak_db),
        "normalize_enabled": bool(profile.normalize_enabled),
        "track_volumes": profile.track_volumes or {},
        "track_muted": profile.track_muted or [],
        "measured_lufs": float(profile.measured_lufs) if profile.measured_lufs is not None else None,
        "clipped": bool(profile.clipped),
        "volume_range_db": TRACK_VOLUME_RANGE_DB,
    }


async def _get_or_create_mix_profile(
    session: AsyncSession, project: Project, timeline_id: int
) -> Any:
    """取或创建时间线混音画像。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        timeline_id: 时间线 ID。

    Returns:
        MixProfile ORM 实例。
    """
    from app.models.post import MixProfile

    profile = (
        await session.execute(
            select(MixProfile).where(MixProfile.timeline_id == timeline_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        profile = MixProfile(
            project_id=int(project.id),
            timeline_id=timeline_id,
            track_volumes={"dialogue": 0.0, "bgm": 0.0, "sfx": 0.0},
            track_muted=[],
        )
        session.add(profile)
        await session.flush()
    return profile


async def get_mix_profile(
    session: AsyncSession, project: Project, cut_type: str = "preview"
) -> dict[str, Any]:
    """查询混音画像（响度目标 + 逐轨音量）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        cut_type: 版本类型。

    Returns:
        ``{profile}``。

    Raises:
        BizError: 10002 时间线不存在。
    """
    repo = PostRepository(session)
    timeline = await repo.get_timeline(int(project.id), cut_type)
    if timeline is None:
        raise BizError(ErrorCode.NOT_FOUND, "时间线不存在")
    profile = await _get_or_create_mix_profile(session, project, int(timeline.id))
    await session.commit()
    return {"profile": mix_profile_to_dict(profile)}


async def update_mix_profile(
    session: AsyncSession,
    project: Project,
    *,
    track_volumes: dict[str, float] | None,
    track_muted: list[str] | None,
    loudness_target: float | None,
    normalize_enabled: bool | None,
    user_id: int,
) -> dict[str, Any]:
    """手动微调混音音量并统一全剧响度（AC-F6-10-1~2）。

    逐轨音量相对自动混音值 ±6dB；超 0dBFS 标红（``clipped``）；
    ``loudness_target`` 默认 -14 LUFS、真峰值 ≤ -1 dBTP；用户手动调整在
    标准化之后应用（手动优先）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        track_volumes: 逐轨音量偏移 {dialogue/bgm/sfx: dB}。
        track_muted: 静音轨列表。
        loudness_target: 响度目标 LUFS（可空）。
        normalize_enabled: 是否启用响度标准化（可空）。
        user_id: 操作人用户 ID。

    Returns:
        ``{profile, clipped_tracks}``。

    Raises:
        BizError: 10001 音量越界；10002 时间线不存在。
    """
    repo = PostRepository(session)
    timeline = await repo.get_timeline(int(project.id), "preview")
    if timeline is None:
        raise BizError(ErrorCode.NOT_FOUND, "时间线不存在")
    profile = await _get_or_create_mix_profile(session, project, int(timeline.id))

    clipped_tracks: list[str] = []
    if track_volumes:
        for track, value in track_volumes.items():
            if track not in ("dialogue", "bgm", "sfx"):
                raise BizError(
                    ErrorCode.PARAM_INVALID,
                    f"未知音轨 {track}（仅支持 dialogue/bgm/sfx）",
                    error_detail="track_unknown",
                )
            if abs(float(value)) > TRACK_VOLUME_RANGE_DB:
                raise BizError(
                    ErrorCode.PARAM_INVALID,
                    f"{track} 轨音量偏移超出 ±{TRACK_VOLUME_RANGE_DB}dB",
                    error_detail="track_volume_out_of_range",
                )
            if float(value) > 0 and track == "dialogue":
                clipped_tracks.append(track)
        profile.track_volumes = {
            **(profile.track_volumes or {}),
            **{key: float(value) for key, value in track_volumes.items()},
        }
    if track_muted is not None:
        profile.track_muted = list(track_muted)
    if loudness_target is not None:
        if not -30.0 <= loudness_target <= -6.0:
            raise BizError(
                ErrorCode.PARAM_INVALID,
                "响度目标需在 -30 ~ -6 LUFS 之间",
                error_detail="loudness_target_out_of_range",
            )
        profile.loudness_target = Decimal(str(round(float(loudness_target), 1)))
    if normalize_enabled is not None:
        profile.normalize_enabled = bool(normalize_enabled)
    profile.clipped = bool(clipped_tracks)
    await session.commit()
    await session.refresh(profile)
    return {"profile": mix_profile_to_dict(profile), "clipped_tracks": clipped_tracks}
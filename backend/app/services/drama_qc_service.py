"""成片多维质检 rubric 服务（P0 质检模块优化：独立质检 Agent）。

背景：此前的质检能力分散在生成链路内部（帧级五检、动态性验收、
AV 对齐 QC），缺少一个独立于生成流程、按交付 rubric 逐维度打分的
质检 Agent。本模块在渲染与合成完成后，对项目按 7 大维度量化评分：

- 剧本（20）：前 3 秒钩子 / ≥2 个冲突升级点 / 结尾反转或悬念；
- 分镜（15）：相邻镜头景别变化率 / 越轴风险 / 静止运镜占比；
- 节奏（15）：紧张段 ≤2.5s、舒缓段 ≤5s / 高潮落在 20-27 秒；
- 画面（20）：无静态镜头（三帧检测）/ 角色一致性分 / 无降级渲染；
- 配音（15）：台词全覆盖 / 情绪标注驱动 / 语速与画面节奏匹配；
- 剪辑（10）：转场类型不连续 / BGM 情绪铺底 / 音效点缀 / 字幕对齐；
- 合规（5）：台词与画面描述无违规词、无未处理合规标记。

输出 :func:`evaluate_project` 报告：总分 + 维度分 + 逐镜头评分表 +
不合格项（定位到 剧本/分镜/生成 prompt/配音/合成代码 环节与修复建议），
供 ``GET /projects/{id}/drama-qc`` 与验收报告使用。
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.post import SubtitleLine, Timeline, TimelineClip
from app.models.project import Project
from app.models.quality import ComplianceFlag, QcReport
from app.models.render import RenderTask
from app.models.script import Episode, Scene, ScriptLine
from app.models.storyboard import Shot, ShotVersion
from app.models.v6 import VoiceClip
# 词表与剧本自评（script_quality_service）同源（F20）：避免"自评达标但整片
# 体检扣分"的口径分裂——同一套钩子/冲突/悬念特征词贯穿生成与验收两端。
from app.services.script_quality_service import (
    _CONFLICT_VERBS as SQ_CONFLICT_VERBS,
    _HOOK_MARKERS as SQ_HOOK_MARKERS,
    _SUSPENSE_MARKERS as SQ_SUSPENSE_MARKERS,
)

logger = logging.getLogger("gleamdream.drama_qc")

__all__ = ["evaluate_project"]

#: 高唤醒情绪（冲突点/紧张段判定）
_TENSE_EMOTIONS: frozenset[str] = frozenset({"tension", "anger", "fear", "surprise"})
#: 钩子特征词（开场 3 秒）——与剧本自评同源
_HOOK_MARKERS: tuple[str, ...] = SQ_HOOK_MARKERS
#: 冲突动词词表——与剧本自评同源
_CONFLICT_VERBS: tuple[str, ...] = SQ_CONFLICT_VERBS
#: 结尾悬念特征词——与剧本自评同源
_SUSPENSE_MARKERS: tuple[str, ...] = SQ_SUSPENSE_MARKERS
#: 合规违禁词（低成本规则扫描；血腥/低俗/真实人名侵权风险）
_BLOCKLIST_WORDS: tuple[str, ...] = ("血", "杀", "死", "尸体", "裸", "胸", "床戏", "毒品", "赌博", "自杀")

#: 紧张段镜头时长上限 / 舒缓段上限（毫秒）
_TENSE_CAP_MS: int = 2500
_CALM_CAP_MS: int = 5000
#: 高潮窗口（秒）：30 秒片取 [20, 27]；其余按总时长 2/3~0.9 比例
_CLIMAX_WINDOW_S: tuple[float, float] = (20.0, 27.0)


def _json(value: Any) -> dict[str, Any]:
    """安全解析 JSON 字段。

    Args:
        value: 数据库 JSONB 字段值（dict/str/None）。

    Returns:
        解析后的字典；解析失败返回空字典。
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _clip_content(clip: TimelineClip) -> dict[str, Any]:
    """解析时间线片段 content 字段。

    Args:
        clip: 时间线片段 ORM。

    Returns:
        content 字典。
    """
    return _json(clip.content)


async def evaluate_project(session: AsyncSession, project: Project) -> dict[str, Any]:
    """对项目执行 7 维度成片质检（独立于生成流程的 rubric 评分）。

    Args:
        session: 数据库会话。
        project: 项目 ORM 实例。

    Returns:
        质检报告字典::

            {
              "project_id", "total_score", "passed",
              "dimensions": {name: {score, max, issues}},
              "shots": [逐镜头评分行],
              "failures": [{dimension, stage, issue, suggestion}],
            }

    Raises:
        无：数据缺失按 0 分计并写入 failures。
    """
    project_id = int(project.id)
    failures: list[dict[str, str]] = []

    shots = (await session.execute(
        select(Shot, Episode.episode_no)
        .join(Episode, Shot.episode_id == Episode.id)
        .where(Shot.project_id == project_id)
        .order_by(Episode.episode_no, Shot.order_index)
    )).all()
    if not shots:
        return {
            "project_id": project_id, "total_score": 0, "passed": False,
            "dimensions": {}, "shots": [],
            "failures": [{"dimension": "all", "stage": "分镜",
                          "issue": "项目还没有分镜数据", "suggestion": "先生成分镜再质检"}],
        }
    shot_ids = [int(shot.id) for shot, _ in shots]

    # ---- 数据收集：镜头版本媒体 / 质检报告 / 渲染任务重试史 ----
    version_rows = (await session.execute(
        select(ShotVersion).where(ShotVersion.shot_id.in_(shot_ids), ShotVersion.is_current == True)  # noqa: E712
    )).scalars().all()
    version_by_shot = {int(v.shot_id): v for v in version_rows}
    media_ids = [int(v.media_file_id) for v in version_rows if v.media_file_id]
    media_meta: dict[int, dict[str, Any]] = {}
    if media_ids:
        from app.models.asset import MediaFile
        for media in (await session.execute(
            select(MediaFile).where(MediaFile.id.in_(media_ids))
        )).scalars():
            media_meta[int(media.id)] = {
                "width": media.width, "height": media.height,
                "meta": _json(media.meta),
            }

    qc_rows = (await session.execute(
        select(QcReport).where(
            QcReport.project_id == project_id, QcReport.target_type == "shot"
        ).order_by(QcReport.round_no)
    )).scalars().all()
    qc_by_shot: dict[int, dict[str, Any]] = {}
    for report in qc_rows:
        qc_by_shot[int(report.target_id)] = {
            "result": report.result,
            "consistency_score": float(report.consistency_score) if report.consistency_score is not None else None,
            "detail": _json(report.detail),
        }

    task_rows = (await session.execute(
        select(RenderTask).where(RenderTask.project_id == project_id)
    )).scalars().all()
    retries_by_shot: dict[int, list[dict[str, Any]]] = {}
    for task in task_rows:
        if task.shot_id is None:
            continue
        history = (_json(task.input_params) or {}).get("motion_retry_history")
        if history:
            retries_by_shot[int(task.shot_id)] = history

    # ---- 剧本数据 ----
    episodes = (await session.execute(
        select(Episode).where(Episode.project_id == project_id).order_by(Episode.episode_no)
    )).scalars().all()
    episode_ids = [int(ep.id) for ep in episodes]
    scenes = (await session.execute(
        select(Scene).where(Scene.episode_id.in_(episode_ids))
    )).scalars().all() if episode_ids else []
    scene_ids = [int(s.id) for s in scenes]
    lines = (await session.execute(
        # P0 修复（F18 笛卡尔积）：应为 ScriptLine.scene_id（台词行归属场次外键），
        # 原写法 Scene.id.in_(...) 对 ScriptLine 实体做隐式交叉连接，
        # 行数 = 全库台词行 × 匹配场次数（本例 1096×4=4384，配音覆盖/字幕校验随之全错）。
        select(ScriptLine).where(ScriptLine.scene_id.in_(scene_ids)).order_by(ScriptLine.order_index)
    )).scalars().all() if scene_ids else []
    spoken = [line for line in lines if line.line_type in ("dialogue", "narration")]
    dialogue = [line for line in spoken if line.line_type == "dialogue"]

    # ---- 配音 / 时间线 / 字幕 ----
    voice_clips = (await session.execute(
        select(VoiceClip).where(VoiceClip.project_id == project_id, VoiceClip.status == "voiced")
    )).scalars().all()
    voiced_line_ids = {int(clip.line_id) for clip in voice_clips if clip.audio_url}

    timeline = (await session.execute(
        select(Timeline).where(Timeline.project_id == project_id).order_by(Timeline.id.desc())
    )).scalars().first()
    clips = (await session.execute(
        select(TimelineClip).where(TimelineClip.timeline_id == int(timeline.id))
    )).scalars().all() if timeline else []
    video_clips = sorted(
        [c for c in clips if c.track_type == "video"], key=lambda c: (c.start_ms or 0)
    )
    audio_clips = [c for c in clips if c.track_type == "audio"]
    bgm_clips = [c for c in clips if c.track_type == "bgm"]
    sfx_contents = [
        _clip_content(c) for c in audio_clips if _clip_content(c).get("sfx")
    ]
    subtitles = (await session.execute(
        select(SubtitleLine).where(SubtitleLine.timeline_id == int(timeline.id))
    )).scalars().all() if timeline else []

    compliance_flags = (await session.execute(
        select(ComplianceFlag).where(
            ComplianceFlag.project_id == project_id, ComplianceFlag.status != "resolved"
        )
    )).scalars().all()

    # ================= 维度评分 =================
    dimensions: dict[str, dict[str, Any]] = {}

    def _add(name: str, score: float, max_score: float, issues: list[str]) -> None:
        """登记一个维度的得分与问题。

        Args:
            name: 维度名。
            score: 得分。
            max_score: 满分。
            issues: 扣分原因列表。
        """
        dimensions[name] = {"score": round(score, 1), "max": max_score, "issues": issues}

    # ---- 1. 剧本（20）----
    first_text = spoken[0].content if spoken else ""
    hook_ok = bool(first_text) and any(m in first_text for m in _HOOK_MARKERS)
    hook_issue = "" if hook_ok else f"开场首句无钩子特征（{first_text[:20] or '无台词'}）"
    conflict_points = 0
    prev_emotion: str | None = None
    for line in spoken:
        if any(verb in (line.content or "") for verb in _CONFLICT_VERBS):
            conflict_points += 1
        elif line.emotion_tag in _TENSE_EMOTIONS and prev_emotion not in (None, line.emotion_tag):
            conflict_points += 0.5
        prev_emotion = line.emotion_tag
    conflict_ok = conflict_points >= 2
    last_text = spoken[-1].content if spoken else ""
    twist_ok = bool(last_text) and any(m in last_text for m in _SUSPENSE_MARKERS)
    twist_issue = "" if twist_ok else "结尾缺反转/悬念特征"
    script_score = (8 if hook_ok else 2) + (7 if conflict_ok else round(min(3.5, conflict_points * 1.75), 1)) + (5 if twist_ok else 1)
    _add("剧本", script_score, 20, [i for i in (hook_issue, "" if conflict_ok else f"冲突升级点 {conflict_points:g} < 2", twist_issue) if i])
    if not hook_ok:
        failures.append({"dimension": "剧本", "stage": "剧本", "issue": hook_issue, "suggestion": "重写开场首句，12 字内抛出问句/突发/悬念钩子"})
    if not conflict_ok:
        failures.append({"dimension": "剧本", "stage": "剧本", "issue": f"冲突升级点不足（{conflict_points:g} < 2）", "suggestion": "在剧本中增加情绪迁移或冲突动词驱动的对抗节拍"})
    if not twist_ok:
        failures.append({"dimension": "剧本", "stage": "剧本", "issue": twist_issue, "suggestion": "结尾台词改为 ？/…/悬念词收尾"})

    # ---- 2. 分镜（15）----
    sizes = [str(shot.shot_size or "") for shot, _ in shots]
    if len(sizes) >= 2:
        adjacent_changed = sum(1 for a, b in zip(sizes, sizes[1:]) if a != b)
        variety_ratio = adjacent_changed / (len(sizes) - 1)
    else:
        variety_ratio = 0.0
    axis_warnings = sum(
        1 for q in qc_by_shot.values()
        for w in (q["detail"].get("warnings") or [])
        if isinstance(w, dict) and w.get("type") == "axis_cross_risk"
    )
    static_ratio = sum(1 for m in (str(s.camera_move or "") for s, _ in shots) if m == "static") / len(shots)
    storyboard_score = round(variety_ratio * 7, 1) + (4 if axis_warnings == 0 else max(0, 4 - axis_warnings)) + (4 if static_ratio <= 0.5 else max(0, 4 - (static_ratio - 0.5) * 8))
    storyboard_issues = []
    if variety_ratio < 0.8:
        storyboard_issues.append(f"相邻镜头景别变化率 {variety_ratio:.0%} < 80%")
        failures.append({"dimension": "分镜", "stage": "分镜", "issue": "同景别连续堆砌", "suggestion": "启用景别序列规划后重新生成分镜"})
    if axis_warnings:
        storyboard_issues.append(f"越轴风险镜头 {axis_warnings} 个")
        failures.append({"dimension": "分镜", "stage": "分镜", "issue": "疑似越轴", "suggestion": "调整人物 facing 或插入中性过渡镜"})
    _add("分镜", storyboard_score, 15, storyboard_issues)

    # ---- 3. 节奏（15）----
    # 口径（F19）：以最终合成时间线的 video 窗口为准（_apply_emotion_pacing
    # 钳制 + 音驱布局重排之后的真实时长），而非分镜基准 duration_ms；
    # 配音驱动的跨度扩展（为完整播放台词而撑长镜头）豁免上限校验。
    voice_shot_ids = {
        int(_clip_content(c).get("shot_id") or 0)
        for c in audio_clips
        if c.source_type == "voice_clip" and _clip_content(c).get("shot_id")
    }
    video_window: dict[int, tuple[int, int]] = {}
    for c in video_clips:
        sid = int(_clip_content(c).get("shot_id") or 0)
        if sid:
            video_window[sid] = (int(c.start_ms or 0), int(c.end_ms or 0))
    tense_violations = 0
    calm_violations = 0
    max_arousal_shot = None
    total_ms = sum(
        end - start for start, end in video_window.values()
    ) or sum(int(shot.duration_ms or 0) for shot, _ in shots)
    shot_pace: list[dict[str, Any]] = []
    for shot, _ in shots:
        span = video_window.get(int(shot.id))
        start, end = span if span is not None else (0, int(shot.duration_ms or 0))
        duration = max(1, end - start)
        emotion = str(shot.emotion_tag or "")
        voiced = int(shot.id) in voice_shot_ids
        if emotion in _TENSE_EMOTIONS:
            if duration > _TENSE_CAP_MS and not voiced:
                tense_violations += 1
        elif duration > _CALM_CAP_MS and not voiced:
            calm_violations += 1
        shot_pace.append({"shot_id": int(shot.id), "start": start, "duration": duration, "emotion": emotion})
        if emotion in _TENSE_EMOTIONS and (max_arousal_shot is None or duration > 0):
            if max_arousal_shot is None:
                max_arousal_shot = shot_pace[-1]
    # 高潮取最后一个高唤醒镜头的中心点（短剧高潮常在尾段冲突）
    climax_center_s = None
    for pace in reversed(shot_pace):
        if pace["emotion"] in _TENSE_EMOTIONS:
            climax_center_s = (pace["start"] + pace["duration"] / 2) / 1000
            break
    window = (
        _CLIMAX_WINDOW_S if abs(total_ms / 1000 - 30) < 5
        else (total_ms / 1000 * 0.6, total_ms / 1000 * 0.9)
    )
    climax_ok = climax_center_s is not None and window[0] <= climax_center_s <= window[1]
    pacing_score = 5 - min(5, tense_violations) - min(2, calm_violations) + (7 if climax_ok else 2)
    pacing_score = max(0.0, min(15.0, pacing_score))
    pacing_issues = []
    if tense_violations or calm_violations:
        pacing_issues.append(f"节奏超限镜头：紧张段 {tense_violations} / 舒缓段 {calm_violations}")
        failures.append({"dimension": "节奏", "stage": "合成代码", "issue": "镜头时长超出情绪曲线约束", "suggestion": "确认 _apply_emotion_pacing 已生效后重新合成"})
    if not climax_ok:
        pacing_issues.append(f"高潮落点 {climax_center_s and round(climax_center_s, 1)}s 不在 {window[0]:.0f}-{window[1]:.0f}s")
        failures.append({"dimension": "节奏", "stage": "剧本", "issue": "高潮落点偏移", "suggestion": "调整冲突场次位置，使最后高唤醒镜头落在 20-27 秒"})
    _add("节奏", pacing_score, 15, pacing_issues)

    # ---- 4. 画面（20）+ 逐镜头行 ----
    shot_rows: list[dict[str, Any]] = []
    motion_fail = 0
    for idx, (shot, episode_no) in enumerate(shots, start=1):
        version = version_by_shot.get(int(shot.id))
        media = media_meta.get(int(version.media_file_id)) if version and version.media_file_id else None
        motion = ((media or {}).get("meta") or {}).get("motion") or (qc_by_shot.get(int(shot.id)) or {}).get("detail", {}).get("motion") or {}
        motion_passed = bool(motion.get("passed"))
        if version is not None and not motion_passed:
            motion_fail += 1
        retries = len(retries_by_shot.get(int(shot.id), []))
        consistency = (qc_by_shot.get(int(shot.id)) or {}).get("consistency_score")
        # 该镜头最终时间线上的配音信息（voice_clip 片段 content.shot_id 映射）
        shot_voice = [c for c in audio_clips if c.source_type == "voice_clip" and int(_clip_content(c).get("shot_id") or 0) == int(shot.id)]
        shot_video_window = video_window.get(int(shot.id))
        shot_rows.append({
            "no": idx, "shot_id": int(shot.id), "episode": int(episode_no),
            "shot_size": shot.shot_size, "camera_move": shot.camera_move,
            "emotion": shot.emotion_tag, "duration_ms": int(shot.duration_ms or 0),
            "final_window_ms": (shot_video_window[1] - shot_video_window[0]) if shot_video_window else int(shot.duration_ms or 0),
            "motion_passed": motion_passed,
            "motion_mean_diff": motion.get("mean_diff"),
            "static_ms": motion.get("longest_static_ms"),
            "motion_retries": retries,
            "consistency_score": consistency,
            "voice_clips": len(shot_voice),
            "voice_duration_ms": sum(int(c.end_ms or 0) - int(c.start_ms or 0) for c in shot_voice),
        })
    # 渲染完成度按"有当前版本"的镜头统计
    rendered_count = sum(1 for shot, _ in shots if shot.id in version_by_shot)
    motion_score = 0.0 if not version_by_shot else round(12 * (rendered_count - motion_fail) / max(1, rendered_count), 1)
    consistency_scores = [
        q["consistency_score"] for q in qc_by_shot.values() if q["consistency_score"] is not None
    ]
    consistency_score = sum(consistency_scores) / len(consistency_scores) if consistency_scores else None
    consistency_points = 5 if consistency_score is None else round(5 * min(1.0, consistency_score), 1)
    degraded_count = sum(
        1 for meta in media_meta.values()
        if str((meta.get("meta") or {}).get("provider") or "") == "simulated"
    )
    visual_score = motion_score + consistency_points + (3 if degraded_count == 0 else 0)
    visual_issues = []
    if motion_fail:
        visual_issues.append(f"静态/动态性未达标镜头 {motion_fail} 个")
        failures.append({"dimension": "画面", "stage": "生成 prompt", "issue": f"{motion_fail} 个镜头动态性验收未过", "suggestion": "查看 motion_retry_history 与三帧存证，强化动作描述后重渲染"})
    if degraded_count:
        visual_issues.append(f"降级渲染 {degraded_count} 镜")
    _add("画面", visual_score, 20, visual_issues)

    # ---- 5. 配音（15）----
    dialogue_count = len(dialogue)
    voiced_count = sum(1 for line in dialogue if line.id in voiced_line_ids)
    coverage = voiced_count / dialogue_count if dialogue_count else None
    emotion_labeled = sum(1 for line in dialogue if line.emotion_tag)
    emotion_ratio = emotion_labeled / dialogue_count if dialogue_count else None
    voice_score = 0.0
    voice_issues = []
    if dialogue_count == 0:
        voice_score = 15.0
    else:
        voice_score += round(7 * (coverage or 0), 1)
        voice_score += round(4 * (emotion_ratio or 0), 1)
        # 语速匹配：以台词字数 / 配音时长 ≈ 3~6 字/秒为合理带
        over_speed = 0
        for clip in voice_clips:
            if not clip.duration_ms:
                continue
            line = next((l for l in dialogue if int(l.id) == int(clip.line_id)), None)
            if line is None:
                continue
            chars = len(line.content or "")
            speed = chars / (int(clip.duration_ms) / 1000)
            if not (2.0 <= speed <= 7.0):
                over_speed += 1
        voice_score += max(0.0, 4 - over_speed * 2)
        if (coverage or 1) < 1:
            voice_issues.append(f"台词配音覆盖 {voiced_count}/{dialogue_count}")
            failures.append({"dimension": "配音", "stage": "配音", "issue": "存在未配音台词", "suggestion": "执行 post/tts 全量合成并检查失败行"})
        if (emotion_ratio or 1) < 0.8:
            voice_issues.append(f"台词情绪标注率 {emotion_ratio:.0%} < 80%")
    _add("配音", voice_score, 15, voice_issues)

    # ---- 6. 剪辑（10）----
    transitions = []
    for clip in video_clips:
        raw = clip.transition
        if isinstance(raw, str):
            raw = _json(raw)
        if isinstance(raw, dict) and raw.get("type"):
            transitions.append(str(raw["type"]))
    same_consecutive = sum(1 for a, b in zip(transitions, transitions[1:]) if a == b)
    transition_ratio = (len(transitions) / max(1, len(video_clips) - 1)) if len(video_clips) >= 2 else 0.0
    editing_score = 0.0
    editing_issues = []
    if len(video_clips) >= 2:
        editing_score += round(4 * min(1.0, transition_ratio) - (1 if same_consecutive else 0), 1)
        editing_score = max(0.0, editing_score)
        if transition_ratio < 0.9:
            editing_issues.append(f"转场覆盖率 {transition_ratio:.0%} < 90%")
            failures.append({"dimension": "剪辑", "stage": "合成代码", "issue": "转场未覆盖全部镜头边界", "suggestion": "检查 _apply_transitions 是否在 compose 中执行"})
        if same_consecutive:
            editing_issues.append("同类型转场连续使用")
    else:
        editing_score += 2.0
    if bgm_clips:
        editing_score += 2.0
    else:
        editing_issues.append("缺少 BGM")
        failures.append({"dimension": "剪辑", "stage": "合成代码", "issue": "BGM 缺失", "suggestion": "执行 _auto_fill_bgm 或手动设置 BGM"})
    editing_score += min(2.0, len(sfx_contents) * 1.0) if sfx_contents else 0.0
    if not sfx_contents:
        editing_issues.append("无音效点缀")
    subtitle_ok = (
        len(subtitles) >= max(0, dialogue_count - 1)
        and all(int(s.end_ms or 0) > int(s.start_ms or 0) for s in subtitles)
    )
    editing_score += 2.0 if subtitle_ok else 0.0
    if not subtitle_ok:
        editing_issues.append("字幕缺失或时间轴异常")
        failures.append({"dimension": "剪辑", "stage": "合成代码", "issue": "字幕轨异常", "suggestion": "重新执行字幕同步"})
    _add("剪辑", min(10.0, editing_score), 10, editing_issues)

    # ---- 7. 合规（5）----
    text_all = " ".join(
        [line.content or "" for line in lines]
        + [str(shot.action_desc or "") for shot, _ in shots]
        + [str(shot.dialogue or "") for shot, _ in shots]
    )
    banned_hits = sorted({w for w in _BLOCKLIST_WORDS if w in text_all})
    open_flags = [f for f in compliance_flags if f.status != "resolved"]
    compliance_score = 5 - min(5, len(banned_hits)) - min(2, len(open_flags))
    compliance_score = max(0.0, compliance_score)
    compliance_issues = []
    if banned_hits:
        compliance_issues.append(f"疑似违规词：{'、'.join(banned_hits)}")
        failures.append({"dimension": "合规", "stage": "剧本", "issue": "台词/描述含风险词", "suggestion": "人工复核 ComplianceFlag 并改写表述"})
    if open_flags:
        compliance_issues.append(f"未处理合规标记 {len(open_flags)} 条")
    _add("合规", compliance_score, 5, compliance_issues)

    total = round(sum(float(d["score"]) for d in dimensions.values()), 1)
    report = {
        "project_id": project_id,
        "total_score": total,
        "passed": total >= 75 and not any(f["stage"] in ("剧本", "生成 prompt") for f in failures),
        "dimensions": dimensions,
        "shots": shot_rows,
        "failures": failures,
    }
    logger.info("成片质检完成 project_id=%s total=%s failures=%s", project_id, total, len(failures))
    return report

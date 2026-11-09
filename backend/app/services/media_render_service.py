"""时间线成片合成服务：把后期时间线渲染为可播放的 MP4 成片。

职责（P0-1 / P0-2 修复）：
- **视频轨拼接**：按 ``timeline_clips`` 的视频片段顺序取镜头产物（MP4）或
  首帧图片，统一缩放/补边到目标分辨率后串接；
- **音频混音**：把配音（TTS）与 BGM 片段按时间线起点延迟后混音，缺失音频
  时补静音轨，保证成片始终带音轨；
- **字幕烧录**：优先使用逐条字幕（``subtitle_lines``），其次使用字幕轨片段
  文本，生成 ASS 后烧录进画面；
- **水印/标识**：可选叠加文字水印（导出链路使用），字体不可用时自动跳过。

与既有实现的关系：此前的「合成」只装配片段元数据、导出只生成纯色占位
MP4，两者都没有真正的媒体合成，成片时长/内容与页面所见不一致。本模块把
真实合成收敛到一处，供 :mod:`app.services.post_service`（合成）与
:mod:`app.services.export_service`（导出）共用。

降级策略：ffmpeg 不可用、单个片段文件缺失或损坏时不中断整条链路——
缺失片段以纯色垫片替代、字幕/水印烧录失败自动重试不带该滤镜，并在返回值
``warnings`` 中留痕（``degraded=True``），避免再次出现「静默伪装成功」。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.ids import new_request_id
from app.infra.storage import storage
from app.models import MediaFile, ShotVersion, SubtitleLine, Timeline, TimelineClip

logger = logging.getLogger("gleamdream.services.media_render")

#: 分辨率档位 → 竖屏像素尺寸（短剧默认 9:16）
RESOLUTION_SIZES: dict[str, tuple[int, int]] = {
    "720p": (720, 1280),
    "1080p": (1080, 1920),
    "4k": (2160, 3840),
}
#: 单次 ffmpeg 执行超时（秒），超时视为合成失败
_FFMPEG_TIMEOUT_S: int = 900
#: 视频垫片底色（与前端深色底一致，避免黑屏突兀）
_PLACEHOLDER_COLOR: str = "0x14141f"
#: Windows 常见中文字体（用于水印 drawtext，找不到时自动跳过水印）
_WATERMARK_FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


@dataclass
class TimelineRenderResult:
    """成片渲染结果。

    Attributes:
        object_key: 落盘后的对象键。
        url: 可访问地址（``/files/{object_key}``）。
        mime_type: MIME 类型。
        size_bytes: 文件字节数。
        duration_ms: 成片时长（毫秒）。
        width: 成片宽度。
        height: 成片高度。
        fps: 成片帧率。
        degraded: 是否发生降级（片段缺失/字幕或水印未烧录）。
        warnings: 降级原因清单。
        video_segment_count: 实际参与拼接的视频片段数。
        audio_segment_count: 实际参与混音的音频片段数。
        subtitle_count: 实际烧录的字幕条数。
    """

    object_key: str
    url: str
    mime_type: str
    size_bytes: int
    duration_ms: int
    width: int
    height: int
    fps: int
    degraded: bool = False
    warnings: list[str] = field(default_factory=list)
    video_segment_count: int = 0
    audio_segment_count: int = 0
    subtitle_count: int = 0


class MediaRenderError(RuntimeError):
    """成片合成失败异常（ffmpeg 不可用或所有片段均不可渲染）。"""


@dataclass
class _VideoSegment:
    """视频片段合成规格。

    Attributes:
        path: 源文件绝对路径；None 表示使用纯色垫片。
        kind: 源类型 video/image/color。
        duration_ms: 片段时长（毫秒）。
        in_point_ms: 源入点（毫秒）。
        transition_type: 与上一片段衔接的转场类型（xfade 名称）；
            None 表示硬切。
        transition_ms: 转场时长（毫秒）。
    """

    path: Path | None
    kind: str
    duration_ms: int
    in_point_ms: int = 0
    transition_type: str | None = None
    transition_ms: int = 240


@dataclass
class _AudioSegment:
    """音频片段合成规格。

    Attributes:
        path: 源文件绝对路径。
        start_ms: 时间线起点（毫秒）。
        duration_ms: 片段时长（毫秒）。
        volume_db: 音量（dB）。
        is_bgm: 是否为 BGM 轨片段（与人声分开混音并做闪避）。
        sidechain: 是否启用闪避（人声压低 BGM，P1-7 修复）。
    """

    path: Path
    start_ms: int
    duration_ms: int
    volume_db: float
    is_bgm: bool = False
    sidechain: bool = True


@dataclass
class _SubtitleCue:
    """字幕条目。

    Attributes:
        start_ms: 起始毫秒。
        end_ms: 结束毫秒。
        text: 字幕文本。
    """

    start_ms: int
    end_ms: int
    text: str


@dataclass
class _RenderSpec:
    """一次合成的完整输入规格（在事件循环内装配，交给线程执行）。

    Attributes:
        videos: 视频片段列表。
        audios: 音频片段列表。
        subtitles: 字幕条目列表。
        total_ms: 成片总时长（毫秒）。
        width: 目标宽度。
        height: 目标高度。
        fps: 目标帧率。
        burn_subtitles: 是否烧录字幕。
        watermark_text: 水印文本（空表示不加水印）。
        watermark_corner: 水印角位 left/right；时间线含道具全幅特写镜时
            自动切到左下角，避让右下角内容（P2-8 修复）。
    """

    videos: list[_VideoSegment]
    audios: list[_AudioSegment]
    subtitles: list[_SubtitleCue]
    total_ms: int
    width: int
    height: int
    fps: int
    burn_subtitles: bool
    watermark_text: str | None
    watermark_corner: str = "right"
    #: xfade 转场链造成的总时长收缩（毫秒）；0 表示走纯 concat。
    #: 音频/字幕时间已按此量前移补偿，视频尾部用 tpad 补齐。
    xfade_shrink_ms: int = 0


def resolution_size(resolution: str | None) -> tuple[int, int]:
    """把分辨率档位解析为像素尺寸。

    Args:
        resolution: 分辨率档位（720p/1080p/4k，可空）。

    Returns:
        ``(width, height)``，未知档位回退 1080p。
    """
    return RESOLUTION_SIZES.get(str(resolution or ""), RESOLUTION_SIZES["1080p"])


def _existing_file(object_key: str | None) -> Path | None:
    """把对象键解析为存在的磁盘文件路径。

    Args:
        object_key: 媒体对象键。

    Returns:
        存在的文件路径；对象键为空、越权或文件不存在时返回 None。
    """
    if not object_key:
        return None
    try:
        path = storage.object_path(str(object_key))
    except Exception:  # noqa: BLE001 路径非法视为不可用
        return None
    return path if path.is_file() else None


async def _media_file(session: AsyncSession, media_file_id: int | None) -> MediaFile | None:
    """按主键查询媒体文件。

    Args:
        session: 数据库会话。
        media_file_id: 媒体文件 ID（可空）。

    Returns:
        MediaFile 实例或 None。
    """
    if not media_file_id:
        return None
    return await session.get(MediaFile, int(media_file_id))


def _is_video(media: MediaFile) -> bool:
    """判断媒体文件是否可作为视频输入。

    Args:
        media: 媒体文件实例。

    Returns:
        True 表示 MIME 或扩展名表明是视频。
    """
    mime = str(media.mime_type or "").lower()
    key = str(media.object_key or "").lower()
    return mime.startswith("video/") or key.endswith((".mp4", ".mov", ".webm", ".mkv"))


def _is_audio(media: MediaFile) -> bool:
    """判断媒体文件是否可作为音频输入。

    Args:
        media: 媒体文件实例。

    Returns:
        True 表示 MIME 或扩展名表明是音频。
    """
    mime = str(media.mime_type or "").lower()
    key = str(media.object_key or "").lower()
    return mime.startswith("audio/") or key.endswith((".wav", ".mp3", ".m4a", ".aac", ".ogg"))


def _is_image(media: MediaFile) -> bool:
    """判断媒体文件是否可作为静态图输入。

    Args:
        media: 媒体文件实例。

    Returns:
        True 表示 MIME 或扩展名表明是图片（SVG 由 ffmpeg 探测失败后回退垫片）。
    """
    mime = str(media.mime_type or "").lower()
    key = str(media.object_key or "").lower()
    return mime.startswith("image/") or key.endswith((".png", ".jpg", ".jpeg", ".webp"))


def _decode_clip_content(clip: TimelineClip) -> dict[str, Any]:
    """解析片段 content 的 JSON 内容。

    Args:
        clip: 时间线片段。

    Returns:
        解析后的字典；非法 JSON 或非对象时返回空字典。
    """
    raw = clip.content
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _clip_source_media(session: AsyncSession, clip: TimelineClip) -> MediaFile | None:
    """解析片段对应的媒体文件。

    源类型口径：``shot_version`` 指向镜头版本，``media_file``/``tts`` 指向媒体
    文件；历史数据缺失 source_id 时回退 content.media_file_id。

    Args:
        session: 数据库会话。
        clip: 时间线片段。

    Returns:
        MediaFile 实例或 None。
    """
    content = _decode_clip_content(clip)
    # P0 剪辑模块优化：SFX 音效片段（sfx_synth 合成产物，无 media_files 记录，
    # 以 content.object_key 定位存储对象；source_type 走 media_file 白名单
    # 以满足 DB CHECK 约束），构造瞬态 MediaFile 参与混音。
    if content.get("sfx") and content.get("object_key"):
        object_key = str(content.get("object_key"))
        return MediaFile(
            project_id=None,
            storage_provider="local",
            bucket="gleamdream-local",
            object_key=object_key,
            url=f"/files/{object_key}",
            media_type="audio",
            mime_type="audio/wav",
            file_size_bytes=0,
            duration_ms=int(content.get("duration_ms") or 0),
            meta={"kind": "sfx", "virtual": True},
        )
    if clip.source_type == "shot_version" and clip.source_id:
        version = await session.get(ShotVersion, int(clip.source_id))
        return await _media_file(session, version.media_file_id if version else None)
    if clip.source_type == "media_file" and clip.source_id:
        media = await _media_file(session, int(clip.source_id))
        if media is not None:
            return media
    if clip.source_type == "tts":
        if clip.source_id:
            media = await _media_file(session, int(clip.source_id))
            if media is not None:
                return media
        return await _media_file(session, content.get("media_file_id"))
    # P0 修复：V6 配音片段以 voice_clip 为源，此前合成无法解析其媒体，
    # 导致成片音轨为 0 条。这里按 audio_key 反查 media_files。
    if clip.source_type == "voice_clip" and clip.source_id:
        from app.models import VoiceClip

        voice_clip = await session.get(VoiceClip, int(clip.source_id))
        if voice_clip is not None and voice_clip.audio_key:
            media = (
                await session.execute(
                    select(MediaFile)
                    .where(MediaFile.object_key == str(voice_clip.audio_key))
                    .limit(1)
                )
            ).scalars().first()
            if media is not None:
                return media
            # 兼容历史配音数据：文件已落盘但缺少 media_files 记录，
            # 构造瞬态对象让合成仍能读取音频，而不是静默丢弃整条音轨。
            return MediaFile(
                project_id=None,
                storage_provider="local",
                bucket="gleamdream-local",
                object_key=str(voice_clip.audio_key),
                url=voice_clip.audio_url or f"/files/{voice_clip.audio_key}",
                media_type="audio",
                mime_type="audio/wav",
                file_size_bytes=0,
                duration_ms=int(voice_clip.duration_ms or 0),
                meta={"kind": "voice", "virtual": True},
            )
        return await _media_file(session, content.get("media_file_id"))
    return await _media_file(session, content.get("media_file_id"))


async def _shot_current_media(session: AsyncSession, shot_id: int) -> MediaFile | None:
    """按镜头查询当前版本的产物媒体（视频镜头缺 video 轨时的兜底）。

    Args:
        session: 数据库会话。
        shot_id: 镜头 ID。

    Returns:
        MediaFile 实例或 None。
    """
    from app.models import Shot

    shot = await session.get(Shot, int(shot_id))
    if shot is None:
        return None
    version = (
        await session.execute(
            select(ShotVersion)
            .where(ShotVersion.shot_id == int(shot_id), ShotVersion.is_current.is_(True))
            .order_by(ShotVersion.version_no.desc())
            .limit(1)
        )
    ).scalars().first()
    if version is None:
        return None
    media = await _media_file(session, version.media_file_id)
    if media is not None:
        return media
    return await _media_file(session, shot.first_frame_media_id)


async def _build_spec(
    session: AsyncSession,
    *,
    timeline: Timeline,
    width: int,
    height: int,
    fps: int,
    burn_subtitles: bool,
    watermark_text: str | None,
    episode_no: int | None = None,
) -> _RenderSpec:
    """装配一次成片合成的输入规格。

    Args:
        session: 数据库会话。
        timeline: 时间线实例。
        width: 目标宽度。
        height: 目标高度。
        fps: 目标帧率。
        burn_subtitles: 是否烧录字幕。
        watermark_text: 可选水印文本。
        episode_no: 仅合成某一集时传入（按镜头集的渲染任务过滤）。

    Returns:
        :class:`_RenderSpec` 实例（已按时间线顺序排序）。

    Raises:
        无：不可渲染的片段以垫片/跳过方式降级，原因写入 warnings。
    """
    warnings: list[str] = []
    clips = list(
        (
            await session.execute(
                select(TimelineClip)
                .where(TimelineClip.timeline_id == int(timeline.id))
                .order_by(TimelineClip.track_type.asc(), TimelineClip.order_index.asc(), TimelineClip.id.asc())
            )
        ).scalars().all()
    )
    if episode_no is not None:
        episode_shot_ids = await _episode_shot_ids(session, int(timeline.project_id), int(episode_no))
        if episode_shot_ids:
            filtered: list[TimelineClip] = []
            for clip in clips:
                if clip.track_type not in ("video", "audio", "subtitle"):
                    filtered.append(clip)
                    continue
                content = _decode_clip_content(clip)
                shot_id = content.get("shot_id")
                if shot_id is None:
                    filtered.append(clip)
                elif int(shot_id) in episode_shot_ids:
                    filtered.append(clip)
            clips = filtered

    videos: list[_VideoSegment] = []
    audios: list[_AudioSegment] = []
    video_shot_ids: set[int] = set()
    for clip in clips:
        if clip.track_type == "video":
            duration_ms = max(1, int(clip.end_ms) - int(clip.start_ms))
            shot_id = _decode_clip_content(clip).get("shot_id")
            if shot_id is not None:
                video_shot_ids.add(int(shot_id))
            # P0 剪辑模块优化：解析片段上的视频转场（xfade）配置
            v_transition: dict[str, Any] | None = None
            raw_transition = clip.transition
            if isinstance(raw_transition, str):
                try:
                    raw_transition = json.loads(raw_transition)
                except json.JSONDecodeError:
                    raw_transition = None
            if isinstance(raw_transition, dict) and raw_transition.get("type"):
                v_transition = raw_transition
            media = await _clip_source_media(session, clip)
            path = _existing_file(media.object_key) if media is not None else None
            if path is not None and media is not None and _is_video(media):
                videos.append(
                    _VideoSegment(
                        path=path,
                        kind="video",
                        duration_ms=duration_ms,
                        in_point_ms=int(clip.in_point_ms or 0),
                        transition_type=str(v_transition.get("type")) if v_transition else None,
                        transition_ms=max(120, min(600, int((v_transition or {}).get("duration_ms") or 240))),
                    )
                )
                continue
            if path is not None and media is not None and _is_image(media):
                videos.append(_VideoSegment(path=path, kind="image", duration_ms=duration_ms))
                continue
            # 片段缺失/历史 JSON 占位：仍保留时间轴长度，用纯色垫片占位
            content = _decode_clip_content(clip)
            shot_id = content.get("shot_id")
            fallback = await _shot_current_media(session, int(shot_id)) if shot_id else None
            fallback_path = _existing_file(fallback.object_key) if fallback is not None else None
            if fallback_path is not None and fallback is not None and _is_video(fallback):
                videos.append(
                    _VideoSegment(path=fallback_path, kind="video", duration_ms=duration_ms)
                )
            else:
                videos.append(_VideoSegment(path=None, kind="color", duration_ms=duration_ms))
                warnings.append(f"视频片段不可用（clip={clip.id}），已用纯色垫片占位")
        elif clip.track_type in ("audio", "bgm"):
            media = await _clip_source_media(session, clip)
            path = _existing_file(media.object_key) if media is not None else None
            if path is None or media is None or not _is_audio(media):
                warnings.append(f"音频片段不可用（clip={clip.id}），已跳过")
                continue
            transition = clip.transition
            if isinstance(transition, str):
                try:
                    transition = json.loads(transition)
                except json.JSONDecodeError:
                    transition = None
            audios.append(
                _AudioSegment(
                    path=path,
                    start_ms=max(0, int(clip.start_ms)),
                    duration_ms=max(1, int(clip.end_ms) - int(clip.start_ms)),
                    volume_db=float(clip.volume_db) if clip.volume_db is not None else -6.0,
                    is_bgm=(clip.track_type == "bgm"),
                    sidechain=bool((transition or {}).get("sidechain", True))
                    if isinstance(transition, dict)
                    else True,
                )
            )

    # 总时长以时间线为权威；片段缺失导致时长变短时兜底取最大片段终点
    clip_end = max((int(c.end_ms) for c in clips), default=0)
    total_ms = max(int(timeline.duration_ms or 0), clip_end, 1000)

    # 字幕优先取逐条字幕表；没有时回退字幕轨片段文本
    cues: list[_SubtitleCue] = []
    if burn_subtitles:
        lines = list(
            (
                await session.execute(
                    select(SubtitleLine)
                    .where(SubtitleLine.timeline_id == int(timeline.id))
                    .order_by(SubtitleLine.start_ms.asc(), SubtitleLine.line_index.asc())
                )
            ).scalars().all()
        )
        for line in lines:
            text = " ".join(str(x) for x in (line.content or []) if str(x).strip()).strip()
            if not text:
                continue
            cues.append(
                _SubtitleCue(
                    start_ms=max(0, int(line.start_ms)),
                    end_ms=max(int(line.start_ms) + 1, int(line.end_ms)),
                    text=text,
                )
            )
        if not cues:
            for clip in clips:
                if clip.track_type != "subtitle":
                    continue
                content = _decode_clip_content(clip)
                text = str(content.get("text") or clip.content or "").strip()
                if not text or text.startswith("{"):
                    continue
                cues.append(
                    _SubtitleCue(
                        start_ms=max(0, int(clip.start_ms)),
                        end_ms=max(int(clip.start_ms) + 1, int(clip.end_ms)),
                        text=text,
                    )
                )

    # P2-8 修复：时间线中存在「道具全幅特写镜」（旧照片/锅等铺满画面）时，
    # 右下角水印会压住画面主体内容，自动把水印切到左下角。
    watermark_corner = "right"
    if video_shot_ids and (watermark_text or "").strip():
        watermark_corner = await _detect_full_prop_watermark_corner(
            session, video_shot_ids
        )

    return _RenderSpec(
        videos=videos,
        audios=audios,
        subtitles=cues,
        total_ms=total_ms,
        width=max(2, width - width % 2),
        height=max(2, height - height % 2),
        fps=max(1, fps),
        burn_subtitles=bool(cues),
        watermark_text=(watermark_text or "").strip() or None,
        watermark_corner=watermark_corner,
    )


async def _detect_full_prop_watermark_corner(
    session: AsyncSession, shot_ids: set[int]
) -> str:
    """检测镜头集中是否存在「道具全幅特写镜」并给出水印角位。

    Args:
        session: 数据库会话。
        shot_ids: 时间线 video 片段涉及的镜头 ID 集合。

    Returns:
        ``"left"`` 表示存在道具全幅镜（水印切左下角），否则 ``"right"``。

    Raises:
        无：构图 spec 缺失或结构异常时按右下角处理。
    """
    from app.models import Shot

    shots = (
        (await session.execute(select(Shot).where(Shot.id.in_(shot_ids))))
        .scalars()
        .all()
    )
    for shot in shots:
        spec = shot.composition_spec
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError:
                spec = None
        if not isinstance(spec, dict):
            continue
        for prop in spec.get("props") or []:
            if isinstance(prop, dict) and prop.get("mode") == "full":
                return "left"
    return "right"


async def _episode_shot_ids(session: AsyncSession, project_id: int, episode_no: int) -> set[int]:
    """查询某集在项目内的已渲染镜头 ID 集合（批量导出按集合成用）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        episode_no: 集号。

    Returns:
        镜头 ID 集合；集不存在时为空集合。
    """
    from sqlalchemy import select as sa_select

    from app.models import Episode, Shot

    rows = (
        await session.execute(
            sa_select(Shot.id)
            .join(Episode, Episode.id == Shot.episode_id)
            .where(
                Shot.project_id == project_id,
                Episode.episode_no == episode_no,
                Shot.status == "rendered",
            )
        )
    ).scalars().all()
    return {int(row) for row in rows}


def _ass_time(ms: int) -> str:
    """把毫秒格式化为 ASS 时间（h:mm:ss.cc）。

    Args:
        ms: 毫秒（≥0）。

    Returns:
        ASS 时间字符串。
    """
    total_cs = max(0, int(ms)) // 10
    hours, remainder = divmod(total_cs, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, centi = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centi:02d}"


def _build_ass(spec: _RenderSpec) -> str:
    """根据字幕条目生成 ASS 字幕文件内容。

    Args:
        spec: 合成规格（含字幕与目标分辨率）。

    Returns:
        ASS 文本（UTF-8 可写入文件）。
    """
    # 竖屏短剧字幕：略大字号 + 粗描边 + 底部居中，保证手机端可读且不压人物面部
    font_size = max(30, min(128, spec.height // 26))
    margin_v = max(48, spec.height // 10)
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {spec.width}\n"
        f"PlayResY: {spec.height}\n"
        # WrapStyle 0 = 智能自动换行：旧值 2 只在显式 \N 处换行，19 字长句
        # 会直接溢出画面右侧（实测镜 7 字幕被裁掉结尾）。
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Microsoft YaHei,{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,"
        f"&H5A000000,-1,0,0,0,100,100,0,0,1,3,1,2,72,72,{margin_v},1\n"
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    for cue in spec.subtitles:
        if cue.end_ms <= cue.start_ms:
            continue
        text = cue.text.replace("\r", "").replace("\n", "\\N").replace("{", "(").replace("}", ")")
        events.append(
            "Dialogue: 0,"
            f"{_ass_time(cue.start_ms)},{_ass_time(cue.end_ms)},Default,,0,0,0,,{text}"
        )
    return header + "\n".join(events) + "\n"


#: 低于该帧率的源视频触发运动插帧（缺陷二：成片流畅度 ≥24fps）
_MIN_SMOOTH_FPS: float = 24.0


def _source_fps(path: Path) -> float:
    """用 ffprobe 读取视频源的帧率。

    Args:
        path: 视频文件路径。

    Returns:
        平均帧率（fps）；ffprobe 不可用、解析失败或非视频时返回 0.0
        （调用方按"无需插帧"处理）。
    """
    if not shutil.which("ffprobe"):
        return 0.0
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=avg_frame_rate",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        raw = (completed.stdout or "").strip()
        if not raw or raw == "N/A" or "/" not in raw:
            return 0.0
        num_s, den_s = raw.split("/", 1)
        num, den = float(num_s), float(den_s)
        if den <= 0 or num <= 0:
            return 0.0
        return num / den
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _interpolation_filter_part(path: Path) -> str:
    """按源帧率返回滤镜链前缀（低帧率源加运动插帧，否则空串）。

    缺陷二修复（画面流畅度）：wan2.6 等视频模型输出可能低于 24fps，
    直接重采样只做丢帧/重复帧。本函数探测源 fps，低于 24fps 时返回
    ``minterpolate`` 运动补偿插帧前缀（mci 模式），把源提升到 24fps
    再进入时间线；探测失败时返回空串（保守不过滤）。

    Args:
        path: 视频源文件路径。

    Returns:
        以逗号结尾的滤镜片段（如 ``"minterpolate=fps=24:mi_mode=mci:mc_mode=aobmc:vsbmc=1,"``）
        或空字符串。
    """
    src_fps = _source_fps(path)
    if src_fps <= 0 or src_fps >= _MIN_SMOOTH_FPS:
        return ""
    return (
        f"minterpolate=fps={_MIN_SMOOTH_FPS:.0f}:mi_mode=mci:"
        "mc_mode=aobmc:me_mode=bidir:vsbmc=1,"
    )


def _probe_stream(path: Path, stream: str) -> bool:
    """用 ffprobe 判断文件是否包含指定类型的流。

    Args:
        path: 媒体文件路径。
        stream: 流类型选择器（``v:0`` 或 ``a:0``）。

    Returns:
        True 表示探测到该流；ffprobe 不可用或文件损坏时返回 False。
    """
    if not shutil.which("ffprobe"):
        # ffprobe 缺失时乐观放行，交给 ffmpeg 实际解码兜底（失败会回退垫片）
        return True
    needle = b"video" if stream.startswith("v") else b"audio"
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", stream,
                "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path),
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and needle in completed.stdout


def _video_input_args(segment: _VideoSegment, fps: int) -> tuple[list[str], str]:
    """构造单个视频片段的输入参数与滤镜链前缀。

    Args:
        segment: 视频片段规格。
        fps: 目标帧率。

    Returns:
        ``(ffmpeg 输入参数列表, 输入标签类型)``；标签类型为 ``v``/``color``。
    """
    if segment.path is None:
        # 垫片源时长必须覆盖片段时长，否则 concat 后总时长短于音频（音画错位）
        duration_s = max(0.04, segment.duration_ms / 1000)
        return (
            [
                "-f", "lavfi",
                "-i", f"color=c={_PLACEHOLDER_COLOR}:s=16x16:d={duration_s:.3f}:r={fps}",
            ],
            "color",
        )
    if segment.kind == "image":
        return ["-loop", "1", "-framerate", str(fps), "-i", str(segment.path)], "image"
    if _probe_stream(segment.path, "v:0"):
        return ["-i", str(segment.path)], "video"
    return (
        ["-f", "lavfi", "-i", f"color=c={_PLACEHOLDER_COLOR}:s=16x16:d=1:r={fps}"],
        "color",
    )


def _plan_xfade(spec: _RenderSpec) -> list[float] | None:
    """判定本片能否启用 xfade 转场链，并给出每段衔接的转场时长（秒）。

    启用条件：全部视频片段均为真实视频（image/color 不支持 xfade）、
    至少 2 段、除首段外每段都配置了转场、且每个转场时长不越过相邻
    任一片段的时长（留 100ms 余量）。

    Args:
        spec: 合成规格。

    Returns:
        转场时长（秒）列表（长度 = 片段数-1）；不可启用时返回 None。
    """
    videos = spec.videos
    if len(videos) < 2:
        return None
    if any(seg.kind != "video" or seg.path is None for seg in videos):
        return None
    if any(not seg.transition_type for seg in videos[1:]):
        return None
    fades: list[float] = []
    for prev_seg, seg in zip(videos, videos[1:]):
        fade = max(0.12, min(0.6, seg.transition_ms / 1000))
        if fade > prev_seg.duration_ms / 1000 - 0.1 or fade > seg.duration_ms / 1000 - 0.1:
            return None
        fades.append(round(fade, 3))
    return fades


def _xfade_shrink_ms_at(boundaries_ms: list[int], fades_ms: list[float], t_ms: int) -> int:
    """计算时间线上 t_ms 点在 xfade 后的实际输出时刻（前移补偿量）。

    Args:
        boundaries_ms: 各转场边界在原始时间线上的位置（毫秒，Σd[:k]）。
        fades_ms: 各转场时长（毫秒）。
        t_ms: 原始时间线时刻（毫秒）。

    Returns:
        该点之前发生的转场造成的累计前移量（毫秒，≥0）。
    """
    shrink = 0
    for boundary, fade in zip(boundaries_ms, fades_ms):
        if t_ms >= boundary:
            shrink += fade
    return int(shrink)


def _apply_xfade_time_warp(spec: _RenderSpec, fades: list[float]) -> None:
    """把 xfade 转场的时间收缩同步到音频与字幕，并记录总收缩量。

    转场会让第 k 个片段的输出起点比原时间线提前 Σf；为保持音画/字幕
    对齐，音频片段的起点与字幕条的起止时间按同一时间扭曲函数前移，
    视频尾部由 tpad 补齐（见 _build_ffmpeg_command）。

    Args:
        spec: 合成规格（就地修改 audios[].start_ms、subtitles[].start_ms/
            end_ms 与 xfade_shrink_ms）。
        fades: :func:`_plan_xfade` 返回的转场时长（秒）列表。

    Returns:
        None
    """
    boundaries_ms: list[int] = []
    cursor = 0
    for seg, fade in zip(spec.videos, fades):
        cursor += seg.duration_ms
        boundaries_ms.append(cursor)
    # 边界 b_k = Σd[:k]（k≥1），zip 后 boundaries_ms[k-1] = Σd[:k]，正确；
    # fades 为秒，统一换算为毫秒参与计算
    fades_ms = [int(round(f * 1000)) for f in fades]
    for seg in spec.audios:
        seg.start_ms = max(
            0, int(seg.start_ms) - _xfade_shrink_ms_at(boundaries_ms, fades_ms, int(seg.start_ms))
        )
    for cue in spec.subtitles:
        cue.start_ms = max(
            0, int(cue.start_ms) - _xfade_shrink_ms_at(boundaries_ms, fades_ms, int(cue.start_ms))
        )
        cue.end_ms = max(
            1, int(cue.end_ms) - _xfade_shrink_ms_at(boundaries_ms, fades_ms, int(cue.end_ms))
        )
    spec.xfade_shrink_ms = int(sum(fades) * 1000)


def _build_ffmpeg_command(
    spec: _RenderSpec,
    *,
    output: Path,
    ass_name: str | None,
    label_name: str | None,
    font_name: str | None,
) -> list[str]:
    """构造完整的 ffmpeg 命令（单次转码完成拼接/混音/字幕/水印）。

    Args:
        spec: 合成规格。
        output: 输出文件路径。
        ass_name: 临时目录内的 ASS 文件名（不烧字幕时为空）。
        label_name: 临时目录内的水印文本文件名（不加水印时为空）。
        font_name: 临时目录内的字体文件名（水印使用；为空表示无可用字体）。

    Returns:
        ffmpeg 参数列表。
    """
    command: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]
    filters: list[str] = []
    video_labels: list[str] = []
    # ffmpeg 输入流索引从 0 连续编号（视频/音频/垫片输入共用一个计数器）
    input_size = 0

    # ---- 视频输入与规范化 ----
    for index, segment in enumerate(spec.videos):
        args, kind = _video_input_args(segment, spec.fps)
        command.extend(args)
        input_index = input_size
        input_size += 1
        duration_s = max(0.04, segment.duration_ms / 1000)
        in_s = max(0.0, segment.in_point_ms / 1000)
        if kind == "color":
            # 纯色源先缩放到目标尺寸，再按时长裁剪
            filters.append(
                f"[{input_index}:v]scale={spec.width}:{spec.height},setsar=1,"
                f"fps={spec.fps},trim=duration={duration_s:.3f},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
        else:
            # 缺陷二修复（画面流畅度）：源视频帧率低于 24fps 时先做运动插帧
            # （minterpolate 运动补偿插帧到 24fps），再做时间线重采样，避免
            # 低帧率源直接 fps 丢帧/重复帧导致的卡顿感。
            interp_part = _interpolation_filter_part(segment.path)
            # P0-2 修复：镜头片段可能因「同镜多句台词」被音频驱动拉长，
            # 源视频（3s）短于片段时长时，直接 trim 会在拼接处缺帧/黑场。
            # 先 tpad 克隆末帧补足，再按目标时长裁剪，保证音画时长一致。
            pad_part = (
                f",tpad=stop_mode=clone:stop_duration={duration_s + 1:.3f}"
                if kind == "video"
                else ""
            )
            filters.append(
                f"[{input_index}:v]{interp_part}scale={spec.width}:{spec.height}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={spec.width}:{spec.height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"fps={spec.fps},trim=start={in_s:.3f}:duration={duration_s:.3f},"
                f"setpts=PTS-STARTPTS{pad_part},"
                f"trim=duration={duration_s:.3f},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
        video_labels.append(f"[v{index}]")

    # ---- 音频输入、延迟与混音 ----
    # P1-7 修复：人声（audio 轨）与 BGM（bgm 轨）分开混音；开启闪避的 BGM
    # 用 sidechaincompress 以人声为侧链信号自动压低，避免盖住对白。
    voice_labels: list[str] = []
    bgm_labels: list[str] = []
    bgm_duck = False
    for index, segment in enumerate(spec.audios):
        command.extend(["-i", str(segment.path)])
        input_index = input_size
        input_size += 1
        duration_s = max(0.04, segment.duration_ms / 1000)
        delay_ms = max(0, int(segment.start_ms))
        out_label = f"[a{index}]"
        filters.append(
            f"[{input_index}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"atrim=0:{duration_s:.3f},asetpts=PTS-STARTPTS,"
            f"apad=whole_dur={duration_s:.3f},volume={segment.volume_db:.2f}dB,"
            f"adelay={delay_ms}|{delay_ms}{out_label}"
        )
        if segment.is_bgm:
            bgm_labels.append(out_label)
            bgm_duck = bgm_duck or segment.sidechain
        else:
            voice_labels.append(out_label)

    def _mix_group(labels: list[str], out_label: str) -> None:
        """把一组已延迟/调音的音频流混成单一流（单流时直通）。

        Args:
            labels: 输入流标签列表（非空）。
            out_label: 输出流标签。

        Returns:
            None
        """
        if len(labels) == 1:
            filters.append(f"{labels[0]}anull{out_label}")
        else:
            filters.append(
                f"{''.join(labels)}amix=inputs={len(labels)}:"
                f"duration=longest:normalize=0{out_label}"
            )

    if not voice_labels and not bgm_labels:
        duration_s = max(0.2, spec.total_ms / 1000)
        command.extend(
            ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100"]
        )
        input_index = input_size
        input_size += 1
        filters.append(
            f"[{input_index}:a]atrim=0:{duration_s:.3f},asetpts=PTS-STARTPTS[aout]"
        )
    else:
        # 修复音轨比视频短：amix 只混到最长音频片段（最后一句台词的终点），
        # 视频尾部会变成"无声段"，导出后 ffprobe 的 audio/video 时长不一致。
        # 先混音再用 apad 补静音到整片时长，保证音轨与画面等长。
        if voice_labels:
            _mix_group(voice_labels, "[voiceMix]")
        if bgm_labels:
            _mix_group(bgm_labels, "[bgmMix]")
        if voice_labels and bgm_labels and bgm_duck:
            # 人声分两路：一路参与最终混音，一路作为 BGM 的侧链键控信号
            filters.append("[voiceMix]asplit=2[voiceMain][voiceKey]")
            filters.append(
                "[bgmMix][voiceKey]sidechaincompress=threshold=0.03:ratio=6:"
                "attack=200:release=900[bgmDuck]"
            )
            filters.append(
                "[voiceMain][bgmDuck]amix=inputs=2:duration=longest:normalize=0[amixed]"
            )
        elif voice_labels and bgm_labels:
            filters.append(
                "[voiceMix][bgmMix]amix=inputs=2:duration=longest:normalize=0[amixed]"
            )
        elif voice_labels:
            filters.append("[voiceMix]anull[amixed]")
        else:
            filters.append("[bgmMix]anull[amixed]")
        # P3-3 修复：EBU R128 响度归一（对白/混音整体 -16 LUFS、
        # true peak ≤ -1.5 dBTP），解决移动端外放偏轻；loudnorm 输出
        # 为 192kHz，需要 aresample 回 44100。
        duration_s = max(0.2, spec.total_ms / 1000)
        filters.append(
            f"[amixed]apad=whole_dur={duration_s:.3f},"
            "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100[aout]"
        )

    if not video_labels:
        # 无视频片段：用整片纯色垫片，保证成片可播放
        duration_s = max(0.05, spec.total_ms / 1000)
        command.extend(
            [
                "-f", "lavfi",
                "-i",
                f"color=c={_PLACEHOLDER_COLOR}:s={spec.width}x{spec.height}:"
                f"d={duration_s:.3f}:r={spec.fps}",
            ]
        )
        input_index = input_size
        input_size += 1
        filters.append(
            f"[{input_index}:v]scale={spec.width}:{spec.height},setsar=1,"
            f"fps={spec.fps},setpts=PTS-STARTPTS[vpad]"
        )
        video_labels = ["[vpad]"]

    if len(video_labels) == 1:
        filters.append(f"{video_labels[0]}null[vcat]")
    elif spec.xfade_shrink_ms > 0 and all(
        seg.transition_type for seg in spec.videos[1:]
    ):
        # P0 剪辑模块优化：xfade 转场链替代硬切 concat。
        # 第 k 次衔接的 offset = 前 k 段原始总时长 - 前 k 次转场时长；
        # 链后视频总时长比音频短 Σf，用 tpad 克隆末帧补齐整片时长。
        cur_label = video_labels[0]
        acc_s = spec.videos[0].duration_ms / 1000
        for k in range(1, len(video_labels)):
            seg = spec.videos[k]
            fade = max(0.12, min(0.6, seg.transition_ms / 1000))
            offset = max(0.0, acc_s - fade)
            out_label = f"[vx{k}]"
            filters.append(
                f"{cur_label}{video_labels[k]}xfade=transition={seg.transition_type}:"
                f"duration={fade:.3f}:offset={offset:.3f}{out_label}"
            )
            cur_label = out_label
            acc_s = acc_s + seg.duration_ms / 1000 - fade
        pad_s = spec.xfade_shrink_ms / 1000
        total_s = max(0.2, spec.total_ms / 1000)
        filters.append(
            f"{cur_label}tpad=stop_mode=clone:stop_duration={pad_s + 0.5:.3f},"
            f"trim=duration={total_s:.3f},setpts=PTS-STARTPTS[vcat]"
        )
    else:
        filters.append(f"{''.join(video_labels)}concat=n={len(video_labels)}:v=1:a=0[vcat]")
    last_video_label = "[vcat]"

    # ---- 字幕烧录 / 水印 ----
    if ass_name:
        filters.append(f"{last_video_label}ass={ass_name}[vsub]")
        last_video_label = "[vsub]"
    if label_name and font_name:
        # AI 标识小字号、无实底框：旧版放右上角且带黑底框，
        # 在纸条/照片特写镜会遮住画面主体文字（P2 遗留），同时抢视觉重心。
        # P2-8 修复：时间线含道具全幅特写镜时水印自动切到左下角，
        # 避免压住右下角的道具主体（锅角/照片下部）。
        font_size = max(16, spec.height // 64)
        margin_x = max(12, spec.width // 60)
        margin_y = max(12, spec.height // 110)
        x_expr = f"{margin_x}" if spec.watermark_corner == "left" else f"w-tw-{margin_x}"
        filters.append(
            f"{last_video_label}drawtext=fontfile={font_name}:textfile={label_name}:"
            f"x={x_expr}:"
            f"y=h-th-{margin_y}:"
            f"fontsize={font_size}:fontcolor=white@0.50:box=1:boxcolor=black@0.22:"
            "boxborderw=6[vwm]"
        )
        last_video_label = "[vwm]"

    duration_s = max(0.2, spec.total_ms / 1000)
    command.extend(
        [
            "-filter_complex", ";".join(filters),
            "-map", last_video_label,
            "-map", "[aout]",
            "-t", f"{duration_s:.3f}",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-r", str(spec.fps),
            "-c:a", "aac",
            "-b:a", "160k",
            "-ar", "44100",
            "-movflags", "+faststart",
            str(output),
        ]
    )
    return command


def _run_ffmpeg(command: list[str], cwd: Path) -> tuple[bool, str]:
    """在线程中执行 ffmpeg 命令。

    Args:
        command: ffmpeg 参数列表。
        cwd: 工作目录（临时目录，滤镜内使用相对文件名）。

    Returns:
        ``(是否成功, 失败时的 stderr 摘要)``。
    """
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")[-1200:]
        return False, stderr
    return True, ""


def _probe_duration_ms(path: Path) -> int:
    """用 ffprobe 读取输出文件时长。

    Args:
        path: 成片文件路径。

    Returns:
        时长（毫秒）；探测失败返回 0。
    """
    if not shutil.which("ffprobe"):
        return 0
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    try:
        return max(0, int(float(completed.stdout.decode().strip()) * 1000))
    except (ValueError, AttributeError):
        return 0


def _execute(spec: _RenderSpec, target: Path) -> TimelineRenderResult:
    """同步执行合成（含字幕/水印失败重试策略）。

    Args:
        spec: 合成规格。
        target: 成片最终落盘路径。

    Returns:
        :class:`TimelineRenderResult` 实例。

    Raises:
        MediaRenderError: ffmpeg 不可用或所有重试尝试均失败。
    """
    warnings: list[str] = []
    if not shutil.which("ffmpeg"):
        raise MediaRenderError("ffmpeg 不可用，无法合成成片")

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gd_render_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        output = tmpdir / "out.mp4"

        ass_name: str | None = None
        if spec.burn_subtitles and spec.subtitles:
            (tmpdir / "sub.ass").write_text(_build_ass(spec), encoding="utf-8")
            ass_name = "sub.ass"

        label_name: str | None = None
        font_name: str | None = None
        if spec.watermark_text:
            font_source = next(
                (Path(p) for p in _WATERMARK_FONT_CANDIDATES if Path(p).is_file()), None
            )
            if font_source is not None:
                shutil.copyfile(font_source, tmpdir / "wm_font.ttc")
                font_name = "wm_font.ttc"
                (tmpdir / "wm_label.txt").write_text(spec.watermark_text, encoding="utf-8")
                label_name = "wm_label.txt"
            else:
                warnings.append("未找到可用中文字体，已跳过水印叠加")

        # 尝试顺序：全量（字幕+水印）→ 仅字幕 → 基础画面；
        # 滤镜组合不兼容时逐级降级，避免整条合成链路失败。
        # P0 剪辑模块优化：xfade 转场链规划——可用时对音频/字幕做时间
        # 前移补偿；全部尝试失败时清空转场降级为纯 concat 再跑一轮。
        fades = _plan_xfade(spec)
        if fades is not None:
            _apply_xfade_time_warp(spec, fades)
            logger.info(
                "成片启用 xfade 转场链 segments=%s fades=%s shrink_ms=%s",
                len(spec.videos), fades, spec.xfade_shrink_ms,
            )

        def _attempts() -> list[tuple[str | None, str | None, str | None, str]]:
            """构建本轮（转场状态下的）合成尝试清单。

            Returns:
                ``(ass_name, label_name, font_name, 描述)`` 元组列表。
            """
            attempts_local: list[tuple[str | None, str | None, str | None, str]] = []
            if ass_name or (label_name and font_name):
                attempts_local.append((ass_name, label_name if font_name else None, font_name, "字幕+水印"))
            if ass_name:
                attempts_local.append((ass_name, None, None, "仅字幕"))
            attempts_local.append((None, None, None, "基础画面"))
            return attempts_local

        last_error = ""
        for allow_xfade in (True, False):
            if not allow_xfade:
                if not spec.xfade_shrink_ms:
                    break
                # xfade 链下全部尝试失败：清空转场并还原音频/字幕时间，
                # 以纯 concat 拼接兜底（保证出片优先）。
                logger.warning("xfade 转场链合成失败，降级为硬切拼接重试")
                warnings.append("转场合成失败，已降级为硬切拼接")
                for seg in spec.videos:
                    seg.transition_type = None
                spec.xfade_shrink_ms = 0
            for use_ass, use_label, use_font, label in _attempts():
                command = _build_ffmpeg_command(
                    spec,
                    output=output,
                    ass_name=use_ass,
                    label_name=use_label,
                    font_name=use_font,
                )
                ok, error = _run_ffmpeg(command, tmpdir)
                if ok and output.is_file() and output.stat().st_size > 0:
                    attempt_warnings = list(warnings)
                    if ass_name and not use_ass:
                        attempt_warnings.append("字幕烧录失败，成片不含烧录字幕")
                    if label_name and not use_label:
                        attempt_warnings.append("水印叠加失败，成片不含水印")
                    shutil.copyfile(output, target)
                    size_bytes = target.stat().st_size
                    duration_ms = _probe_duration_ms(target) or spec.total_ms
                    return TimelineRenderResult(
                        object_key="",
                        url="",
                        mime_type="video/mp4",
                        size_bytes=size_bytes,
                        duration_ms=duration_ms,
                        width=spec.width,
                        height=spec.height,
                        fps=spec.fps,
                        degraded=bool(attempt_warnings),
                        warnings=attempt_warnings,
                        video_segment_count=len(spec.videos),
                        audio_segment_count=len(spec.audios),
                        subtitle_count=len(spec.subtitles) if use_ass else 0,
                    )
                last_error = f"{label}: {error}"
                logger.warning("成片合成尝试失败（%s）：%s", label, error[:600])

    raise MediaRenderError(f"成片合成失败：{last_error[:4000]}")


async def render_timeline(
    session: AsyncSession,
    *,
    project_id: int,
    timeline_id: int,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    burn_subtitles: bool = True,
    watermark_text: str | None = None,
    episode_no: int | None = None,
    object_key: str | None = None,
) -> TimelineRenderResult:
    """把时间线渲染为 MP4 成片并落盘。

    Args:
        session: 数据库会话（只读，调用方负责提交自身状态变更）。
        project_id: 项目 ID。
        timeline_id: 时间线 ID。
        width: 目标宽度（空则按时间线分辨率）。
        height: 目标高度（空则按时间线分辨率）。
        fps: 目标帧率（空则按时间线帧率）。
        burn_subtitles: 是否烧录字幕。
        watermark_text: 可选水印文本（导出链路传入 AI 标识）。
        episode_no: 仅渲染某一集（批量导出使用）。
        object_key: 指定落盘对象键（空则自动生成）。

    Returns:
        :class:`TimelineRenderResult`（含对象键、大小、时长与降级告警）。

    Raises:
        MediaRenderError: 时间线不存在、ffmpeg 不可用或合成失败。
    """
    timeline = await session.get(Timeline, int(timeline_id))
    if timeline is None or int(timeline.project_id) != int(project_id):
        raise MediaRenderError(f"时间线不存在 timeline_id={timeline_id}")

    default_width, default_height = resolution_size(timeline.resolution)
    spec = await _build_spec(
        session,
        timeline=timeline,
        width=int(width or default_width),
        height=int(height or default_height),
        fps=int(fps or timeline.fps or 24),
        burn_subtitles=burn_subtitles,
        watermark_text=watermark_text,
        episode_no=episode_no,
    )

    key = object_key or f"renders/final/{project_id}/{new_request_id().lower()}.mp4"
    target = storage.object_path(key)
    result = await asyncio.to_thread(_execute, spec, target)
    result.object_key = key.replace("\\", "/")
    result.url = f"/files/{result.object_key}"
    logger.info(
        "成片合成完成 project_id=%s timeline_id=%s key=%s size=%s duration_ms=%s "
        "video=%s audio=%s subtitle=%s degraded=%s",
        project_id, timeline_id, result.object_key, result.size_bytes, result.duration_ms,
        result.video_segment_count, result.audio_segment_count, result.subtitle_count,
        result.degraded,
    )
    return result


def _probe_stream_duration_ms(path: Path, stream: str) -> int:
    """用 ffprobe 读取指定流（``a:0`` / ``v:0``）的时长。

    Args:
        path: 媒体文件路径。
        stream: 流选择器。

    Returns:
        流时长（毫秒）；读取失败或该流无 duration 元数据时返回 0。
    """
    if not shutil.which("ffprobe"):
        return 0
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", stream,
                "-show_entries", "stream=duration", "-of", "csv=p=0", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            return 0
        token = (completed.stdout or "").strip().splitlines()
        if not token:
            return 0
        value = token[0].strip().rstrip(",")
        return int(float(value) * 1000) if value else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def verify_output_audio(
    target: Path | str,
    expected_duration_ms: int | None = None,
    *,
    duration_tolerance_ms: int = 100,
) -> dict[str, Any]:
    """校验成片产物确实含音频流，且音视频时长基本一致。

    这是合成链路的最后一道闸门：避免时间线装配成功、但输出被误加 ``-an``
    或被静音轨覆盖时，仍然把“无声成片”当成成功产物。

    Args:
        target: 成片本地路径或对象键（``renders/final/...``）。
        expected_duration_ms: 期望总时长（毫秒）；为空时跳过时长比对。
        duration_tolerance_ms: 音视频时长允许误差（毫秒）。

    Returns:
        ``{audio_ms, video_ms, format_ms}``，便于写入 QC 日志。

    Raises:
        MediaRenderError: 输出文件不存在、无音频流，或音视频时长超出容差。
    """
    path = target if isinstance(target, Path) else storage.object_path(str(target))
    if not path.exists():
        raise MediaRenderError(f"成片文件不存在：{path}")
    if not _probe_stream(path, "a:0"):
        raise MediaRenderError("成片不含音频流（audio:0 缺失），判定合成失败")
    audio_ms = _probe_stream_duration_ms(path, "a:0")
    video_ms = _probe_stream_duration_ms(path, "v:0")
    format_ms = _probe_duration_ms(path)
    # format_ms 为 0 表示 ffprobe 缺失或无法读取容器时长（见 ``_probe_duration_ms``
    # 的降级返回），此时不做时长比对——否则会把“探测不可用”误判成“时长不一致”，
    # 在未安装 ffprobe 的环境里拦截本可成功的合成。
    if expected_duration_ms and format_ms and abs(int(format_ms) - int(expected_duration_ms)) > int(
        duration_tolerance_ms
    ):
        raise MediaRenderError(
            "成片时长与时间线不一致："
            f"输出 {format_ms}ms / 期望 {expected_duration_ms}ms"
        )
    if audio_ms and video_ms and abs(audio_ms - video_ms) > int(duration_tolerance_ms):
        raise MediaRenderError(
            f"成片音视频时长偏差过大：audio {audio_ms}ms / video {video_ms}ms"
        )
    return {"audio_ms": audio_ms, "video_ms": video_ms, "format_ms": format_ms}


def _build_segment_command(
    *,
    source: Path,
    output: Path,
    start_ms: int,
    duration_ms: int,
    width: int,
    height: int,
    fps: int,
    label_name: str | None,
    font_name: str | None,
) -> list[str]:
    """构造「从成片中截取一段」的 ffmpeg 命令（批量导出按集切分用）。

    Args:
        source: 母版成片路径。
        output: 输出路径。
        start_ms: 片段起点（毫秒）。
        duration_ms: 片段时长（毫秒）。
        width: 目标宽度。
        height: 目标高度。
        fps: 目标帧率。
        label_name: 水印文本文件（相对临时目录，可空）。
        font_name: 字体文件（相对临时目录，可空）。

    Returns:
        ffmpeg 参数列表。
    """
    start_s = max(0.0, start_ms / 1000)
    duration_s = max(0.2, duration_ms / 1000)
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        f"fps={fps}",
    ]
    if label_name and font_name:
        font_size = max(20, height // 48)
        filters.append(
            f"drawtext=fontfile={font_name}:textfile={label_name}:"
            f"x=w-tw-{max(16, width // 45)}:y={max(16, height // 90)}:"
            f"fontsize={font_size}:fontcolor=white@0.55:box=1:boxcolor=black@0.35:"
            "boxborderw=10"
        )
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{start_s:.3f}",
        "-i", str(source),
        "-t", f"{duration_s:.3f}",
        "-vf", ",".join(filters),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "24",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-c:a", "aac",
        "-b:a", "160k",
        "-ar", "44100",
        "-movflags", "+faststart",
        str(output),
    ]


async def extract_segment(
    *,
    source_key: str,
    start_ms: int,
    duration_ms: int,
    width: int,
    height: int,
    fps: int = 24,
    watermark_text: str | None = None,
    object_key: str | None = None,
) -> TimelineRenderResult | None:
    """从已有成片中截取一集并落盘（批量导出快速路径）。

    Args:
        source_key: 母版成片对象键。
        start_ms: 该集在成片中的起点（毫秒）。
        duration_ms: 该集时长（毫秒）。
        width: 目标宽度。
        height: 目标高度。
        fps: 目标帧率。
        watermark_text: 可选水印文本。
        object_key: 输出对象键（空则自动生成）。

    Returns:
        截取结果；源文件缺失或 ffmpeg 失败时返回 None（由调用方回退）。

    Raises:
        无：异常内部消化，避免批量导出整体失败。
    """
    source = _existing_file(source_key)
    if source is None:
        return None
    key = object_key or f"exports/segments/{new_request_id().lower()}.mp4"
    target = storage.object_path(key)
    target.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="gd_segment_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        output = tmpdir / "segment.mp4"

        label_name: str | None = None
        font_name: str | None = None
        if watermark_text:
            font_source = next(
                (Path(p) for p in _WATERMARK_FONT_CANDIDATES if Path(p).is_file()), None
            )
            if font_source is not None:
                shutil.copyfile(font_source, tmpdir / "wm_font.ttc")
                font_name = "wm_font.ttc"
                (tmpdir / "wm_label.txt").write_text(watermark_text, encoding="utf-8")
                label_name = "wm_label.txt"

        attempts = [(label_name, font_name)] if label_name else [(None, None), (None, None)]
        if label_name:
            attempts.append((None, None))
        for use_label, use_font in attempts:
            command = _build_segment_command(
                source=source,
                output=output,
                start_ms=int(start_ms),
                duration_ms=int(duration_ms),
                width=max(2, width - width % 2),
                height=max(2, height - height % 2),
                fps=max(1, fps),
                label_name=use_label,
                font_name=use_font,
            )
            ok, error = _run_ffmpeg(command, tmpdir)
            if ok and output.is_file() and output.stat().st_size > 0:
                shutil.copyfile(output, target)
                return TimelineRenderResult(
                    object_key=key.replace("\\", "/"),
                    url=f"/files/{key.replace('\\', '/')}",
                    mime_type="video/mp4",
                    size_bytes=target.stat().st_size,
                    duration_ms=max(200, int(duration_ms)),
                    width=int(width),
                    height=int(height),
                    fps=int(fps),
                    degraded=use_label is None and label_name is not None,
                    warnings=["水印叠加失败，导出段不含水印"] if (label_name and not use_label) else [],
                )
            logger.warning("成片切分失败 key=%s：%s", source_key, error[:400])
    return None


async def create_timeline_media(
    session: AsyncSession,
    *,
    project_id: int,
    result: TimelineRenderResult,
    note: str | None = None,
) -> MediaFile:
    """把合成结果登记为 ``media_files`` 记录（P2-14：回写真实文件大小）。

    Args:
        session: 数据库会话（调用方负责提交）。
        project_id: 项目 ID。
        result: 合成结果。
        note: 附加备注（写入 meta）。

    Returns:
        已 flush 的 MediaFile 实例。
    """
    media = MediaFile(
        project_id=int(project_id),
        storage_provider="cos",
        bucket="gleamdream-local",
        object_key=result.object_key,
        url=result.url,
        media_type="video",
        mime_type=result.mime_type,
        file_size_bytes=int(result.size_bytes),
        width=result.width,
        height=result.height,
        duration_ms=int(result.duration_ms),
        meta={
            "kind": "timeline_render",
            "note": note,
            "degraded": bool(result.degraded),
            "warnings": list(result.warnings),
            "video_segments": result.video_segment_count,
            "audio_segments": result.audio_segment_count,
            "subtitles": result.subtitle_count,
            "fps": result.fps,
        },
    )
    session.add(media)
    await session.flush()
    return media

"""导出分发域服务模块（Task 9 · Export & Distribution）。

职责（全部写路径单事务提交后再发 SSE 事件）：
- :func:`create_export`：创建单集导出（202 + 后台管线）：套餐权益校验
  （70003）→ 时间线必须 approved（40001）→ 幂等（Idempotency-Key）→
  export_jobs(pending) → 后台按时间线真实合成 MP4（母版无水印且分辨率
  一致时直接复用，否则 ffmpeg 重渲并烧录 AI 标识）→ succeeded +
  media_files → 发 ``export.done``；仅 ffmpeg 不可用时回退占位产物；
- :func:`get_export_detail`：导出任务详情 + 下载 URL（/files/...）；
- :func:`list_exports`：项目导出任务列表；
- :func:`batch_export`：批量导出（每集一个 export_job，ZIP 打包逐集
  MP4 成片，文件名 ``{项目名}-EP{n}-{标题}.mp4``；优先从母版切分，
  缺失时按集现场合成）；
- :func:`generate_metadata`：发行 Agent（真实 LLM，中文五段式提示词）
  生成发行元数据（标题/简介/话题标签/封面建议，不落库）；
- :func:`list_platform_accounts` / :func:`authorize_platform` /
  :func:`delete_platform_account`：平台授权 Stub OAuth；
- :func:`distribute`：模拟上传分发（校验授权有效 → 2s 同步模拟 →
  distribution_records 落库）；
- :func:`list_distributions`：分发记录列表。

权益判定约定（契约）：free → 仅 720p + 强制水印；pro → 最高 1080p；
studio → 4k + 可去水印；超权益 → 70003（data 携带 current_plan 与
required_benefit）。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_request_id
from app.infra.event_bus import event_bus
from app.infra.model_gateway import GenRequest, model_gateway
from app.infra.model_gateway.stub import _build_placeholder_mp4
from app.infra.storage import storage
from app.models import (
    DistributionRecord,
    ExportJob,
    ExportPreset,
    MediaFile,
    PlatformAccount,
    SubtitleLine,
    Timeline,
    TimelineClip,
)
from app.models.project import Project
from app.repositories.export_repo import ExportRepository
from app.repositories.post_repo import PostRepository
from app.services import (
    analytics_service,
    media_render_service,
    message_mapping,
    plan_benefit_service,
)
from app.services.ai_label import (
    ai_label_metadata,
    build_ai_label,
    visible_label_text,
)
from app.services.template_packs import MAX_EXPORT_PRESETS

logger = logging.getLogger("gleamdream.services.export")

# 导出幂等键前缀（项目内唯一）
_IDEM_PREFIX: str = "export"
#: 导出产物保留天数（AC-F7-06-1：历史保留 90 天）
EXPORT_RETENTION_DAYS: int = 90

#: 平台规格规则库（AC-F7-07-1【假设待验证：各平台数值需按平台规则表校准】）
PLATFORM_RULES: dict[str, dict[str, Any]] = {
    "douyin": {
        "label": "抖音",
        "max_duration_sec": 180,
        "max_size_mb": 500,
        "resolutions": ["720p", "1080p"],
        "aspects": ["9:16", "16:9", "1:1"],
        "cover_size": "1080x1920",
    },
    "kuaishou": {
        "label": "快手",
        "max_duration_sec": 180,
        "max_size_mb": 500,
        "resolutions": ["720p", "1080p"],
        "aspects": ["9:16", "16:9", "1:1"],
        "cover_size": "1080x1920",
    },
    "wechat_channels": {
        "label": "视频号",
        "max_duration_sec": 180,
        "max_size_mb": 1024,
        "resolutions": ["720p", "1080p"],
        "aspects": ["9:16"],
        "cover_size": "1080x1260",
    },
    "jianying": {
        "label": "剪映草稿",
        "max_duration_sec": 600,
        "max_size_mb": 2048,
        "resolutions": ["720p", "1080p", "4k"],
        "aspects": ["9:16", "16:9", "1:1", "4:3"],
        "cover_size": None,
    },
}

#: 平台规则库不可用时的通用基线（AC-F7-07-2【假设待验证：基线值】）
GENERIC_BASELINE_RULES: dict[str, Any] = {
    "label": "通用基线",
    "max_duration_sec": 180,
    "max_size_mb": 500,
    "resolutions": ["720p", "1080p", "4k"],
    "aspects": ["9:16", "16:9", "1:1", "4:3"],
    "cover_size": None,
}
# 成片文件占位提示文案（写入导出元数据与 ZIP 条目）
_PLACEHOLDER_NOTE: str = "成片文件（演示环境占位，内容为导出元数据 JSON）"
# 单条导出占位视频的提示文案（已生成可播放 MP4，但非真实镜头合成）
_PLACEHOLDER_VIDEO_NOTE: str = "演示环境占位成片（纯色 MP4，未拼接真实镜头）"
# 导出分辨率 → 竖屏像素尺寸（短剧默认 9:16）
_EXPORT_SIZES: dict[str, tuple[int, int]] = {
    "720p": (720, 1280),
    "1080p": (1080, 1920),
    "4k": (2160, 3840),
}
# 发行 Agent 提示词（中文五段式：角色/任务/上下文/输出格式/约束）
_DISTRIBUTION_PROMPT_TEMPLATE: str = """# 角色
你是「浮光造梦」的发行 Agent，精通抖音、快手、视频号等短视频平台的标题包装、话题运营与封面策划，擅长用高钩子文案提升短剧完播率与转化。

# 任务
基于项目信息为短剧生成一套可直接使用的发行元数据，包括：发行标题、平台简介、话题标签、封面建议。

# 上下文
- 剧名：{title}
- 题材：{genre}
- 一句话创意：{synopsis}
- 总集数：{episodes}
- 剧情概要（镜头动作描述节选）：{plot}

# 输出格式
仅输出一个 JSON 对象（不要 markdown 代码块、不要多余解释）：
{{"title": "发行标题", "description": "平台简介", "tags": ["#标签1", "#标签2", "#标签3", "#标签4", "#标签5"], "cover_suggestions": ["封面建议1", "封面建议2"]}}

# 约束
1. 标题不超过 20 个字，须带悬念或冲突钩子，禁止标题党夸大事实；
2. 简介不超过 80 个字，突出核心冲突与看点，口语化有网感；
3. 话题标签恰好 5 个，均以 # 开头，覆盖题材词与流量词；
4. 封面建议 2~3 条，每条描述画面主体、人物情绪与叠加文字；
5. 内容须合规，不得出现敏感、低俗、侵权表述。
"""

# 后台管线任务引用集（防 GC）
_BG_TASKS: set[asyncio.Task] = set()

# 平台合法取值（对齐 platform_accounts CHECK 约束）
_PLATFORMS: tuple[str, ...] = ("douyin", "kuaishou", "wechat_channels", "jianying")


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


def build_idempotency_key(project_id: int, raw_key: str | None) -> str | None:
    """清洗并拼接导出幂等键：``export:{project_id}:{key}``。

    Args:
        project_id: 项目 ID。
        raw_key: 请求头 Idempotency-Key 原始值（可空）。

    Returns:
        拼接后的完整幂等键；原始键为空返回 None。
    """
    if raw_key is None:
        return None
    cleaned = raw_key.strip()[:64]
    if not cleaned:
        return None
    return f"{_IDEM_PREFIX}:{project_id}:{cleaned}"


def sanitize_filename(name: str) -> str:
    """清洗文件名：去除跨平台非法字符与空白。

    Args:
        name: 原始名称（项目名/集标题）。

    Returns:
        可安全用作 ZIP 条目名的字符串（空输入返回 ``untitled``）。
    """
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "").strip())
    return cleaned[:64] or "untitled"


def _job_to_dict(job: ExportJob, media: MediaFile | None = None) -> dict[str, Any]:
    """把导出任务 ORM 序列化为响应字典。

    Args:
        job: 导出任务实例。
        media: 产物媒体文件实例（可空）。

    Returns:
        任务字典（含 output 产物子结构）。
    """
    output: dict[str, Any] | None = None
    if media is not None:
        output = {
            "media_file_id": int(media.id),
            "object_key": media.object_key,
            "media_type": media.media_type,
            "size_bytes": int(media.file_size_bytes or 0),
            "download_url": media.url or f"/files/{media.object_key}",
        }
    return {
        "id": int(job.id),
        "project_id": int(job.project_id),
        "timeline_id": int(job.timeline_id) if job.timeline_id is not None else None,
        "resolution": job.resolution,
        "has_watermark": bool(job.has_watermark),
        "format": job.format,
        "params": job.params,
        "status": job.status,
        "progress": int(job.progress),
        "error_reason": job.error_reason,
        "idempotency_key": job.idempotency_key,
        "cache_key": job.cache_key,
        "preset_id": int(job.preset_id) if job.preset_id else None,
        "name_template": job.name_template,
        "expires_at": _iso(job.expires_at),
        "ai_label": job.ai_label
        or build_ai_label(int(job.project_id), (job.params or {}).get("episode_no")),
        "ai_label_applied": bool(job.ai_label_applied),
        "ai_label_text": visible_label_text(job.ai_label),
        "created_at": _iso(job.created_at),
        "finished_at": _iso(job.finished_at),
        "output": output,
    }


async def _get_media(session: AsyncSession, media_file_id: int | None) -> MediaFile | None:
    """按主键查询媒体文件。

    Args:
        session: 异步数据库会话。
        media_file_id: 媒体文件 ID（可空）。

    Returns:
        MediaFile 实例；不存在返回 None。
    """
    if media_file_id is None:
        return None
    return await session.get(MediaFile, media_file_id)


# =====================================================================
# 导出
# =====================================================================


async def create_export(
    session: AsyncSession,
    project: Project,
    *,
    resolution: str,
    with_watermark: bool = True,
    episode_no: int | None = None,
    idempotency_key: str | None = None,
    preset_id: int | None = None,
    name_template: str | None = None,
) -> dict[str, Any]:
    """创建单集导出任务（202 异步）：权益校验 → approved 校验 → 幂等 → 落库。

    V4.0：落 AI 标识配置快照（合规强制，NFR-C6 / AC-F7-08-1，**无关闭入口**）、
    转码缓存键（成片版本 + 参数 hash，AC-F7-06-2）与产物过期时间（90 天）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（export 权限）。
        resolution: 分辨率（720p/1080p/4k）。
        with_watermark: 是否带水印（False 需 no_watermark 权益）。
        episode_no: 集号过滤（可空导出全剧拼接）。
        idempotency_key: 幂等键（请求头 Idempotency-Key，可空）。
        preset_id: 套用的导出预设 ID（可空）。
        name_template: 命名模板（可空）。

    Returns:
        ``{export_job, idempotent}``，export_job 为任务字典。

    Raises:
        BizError: 70003 权益不足；40001 时间线未审批；10002 时间线/预设不存在。
    """
    repo = ExportRepository(session)
    benefits = await plan_benefit_service.resolve_benefits(session, int(project.owner_user_id))
    plan_benefit_service.assert_export_allowed(benefits, resolution, with_watermark)

    if preset_id is not None:
        preset = await session.get(ExportPreset, preset_id)
        if preset is None or int(preset.owner_user_id) != int(project.owner_user_id):
            raise BizError(ErrorCode.NOT_FOUND, "导出预设不存在")
        # 套用预设时权益项按当下会员状态实时校验（不缓存权益结论）
        preset_params = preset.params or {}
        if preset_params.get("resolution"):
            resolution = str(preset_params["resolution"])
            plan_benefit_service.assert_export_allowed(benefits, resolution, with_watermark)
        if name_template is None and preset_params.get("name_template"):
            name_template = str(preset_params["name_template"])

    full_key = build_idempotency_key(int(project.id), idempotency_key)
    if full_key:
        existed = await repo.find_by_idempotency_key(int(project.id), full_key)
        if existed is not None:
            media = await _get_media(session, existed.output_media_file_id)
            return {"export_job": _job_to_dict(existed, media), "idempotent": True}

    post_repo = PostRepository(session)
    timeline = None
    for cut_type in ("preview", "master"):
        candidate = await post_repo.get_timeline(int(project.id), cut_type)
        if candidate is not None and candidate.status == "approved":
            timeline = candidate
            break
    if timeline is None:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            "时间线尚未审批通过，无法导出（请先完成合成与成片审批）",
        )

    ai_label = build_ai_label(int(project.id), episode_no)
    # P3-1 修复：缓存键纳入时间线内容签名，时间线内容变化即换新键
    cache_key = _build_cache_key(
        int(timeline.id),
        resolution,
        bool(with_watermark),
        episode_no,
        content_signature=await _timeline_content_signature(session, int(timeline.id)),
    )
    job = ExportJob(
        project_id=int(project.id),
        timeline_id=int(timeline.id),
        resolution=resolution,
        has_watermark=bool(with_watermark),
        format="mp4",
        params={
            "episode_no": episode_no,
            "plan_key": benefits.get("plan_key"),
            "preset_id": preset_id,
        },
        status="pending",
        progress=0,
        idempotency_key=full_key,
        cache_key=cache_key,
        preset_id=preset_id,
        name_template=name_template,
        expires_at=_utcnow() + timedelta(days=EXPORT_RETENTION_DAYS),
        ai_label=ai_label,
        ai_label_applied=True,
    )
    await repo.add_export_job(job)
    await session.commit()

    # 埋点：导出漏斗起点（幂等命中不重复计）
    await analytics_service.track(
        analytics_service.EVENT_EXPORT_STARTED,
        user_id=int(project.owner_user_id),
        project_id=int(project.id),
        props={"resolution": resolution, "with_watermark": bool(with_watermark), "episode_no": episode_no},
    )

    _spawn(_run_export_pipeline(int(job.id)))
    return {"export_job": _job_to_dict(job), "idempotent": False}


async def _render_export_video(
    session: AsyncSession,
    *,
    job: ExportJob,
    project: Project | None,
    master: Timeline | None,
    width: int,
    height: int,
    episode_no: int | None = None,
) -> media_render_service.TimelineRenderResult | None:
    """生成导出成片（优先复用合成产物，否则按时间线现场合成）。

    Args:
        session: 数据库会话（调用方需先提交自身事务）。
        job: 导出任务。
        project: 项目实例（可空，仅用于日志）。
        master: 母版时间线（可空）。
        width: 目标宽度。
        height: 目标高度。
        episode_no: 单集导出时的集号（批量/整片导出为空）。

    Returns:
        合成结果；时间线缺失、ffmpeg 不可用或合成失败时返回 None（由调用方回退）。

    Raises:
        无：合成异常内部捕获并记录日志，避免导出任务直接失败。
    """
    if master is None:
        return None
    label = (
        visible_label_text(build_ai_label(int(job.project_id), episode_no))
        if job.has_watermark
        else None
    )

    # 快速路径：母版已有同分辨率、无水印的成片，直接复制文件引用（不改源文件）
    if master.rendered_media_file_id and not job.has_watermark and episode_no is None:
        source = await session.get(MediaFile, int(master.rendered_media_file_id))
        if source is not None and str(source.mime_type).startswith("video/"):
            try:
                source_path = storage.object_path(str(source.object_key))
            except Exception:  # noqa: BLE001 非法对象键按缺失处理
                source_path = None
            if (
                source_path is not None
                and source_path.is_file()
                and int(source.width or 0) == int(width)
                and int(source.height or 0) == int(height)
            ):
                key = f"exports/{job.id}/{new_request_id().lower()}.mp4"
                target = storage.object_path(key)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target)
                logger.info(
                    "导出复用合成成片 job_id=%s timeline_id=%s key=%s",
                    job.id, master.id, key,
                )
                return media_render_service.TimelineRenderResult(
                    object_key=key,
                    url=f"/files/{key}",
                    mime_type="video/mp4",
                    size_bytes=target.stat().st_size,
                    duration_ms=int(source.duration_ms or master.duration_ms or 0),
                    width=int(width),
                    height=int(height),
                    fps=int((source.meta or {}).get("fps") or 24),
                )

    key = f"exports/{job.id}/{new_request_id().lower()}.mp4"
    try:
        return await media_render_service.render_timeline(
            session,
            project_id=int(job.project_id),
            timeline_id=int(master.id),
            width=int(width),
            height=int(height),
            fps=24,
            burn_subtitles=True,
            watermark_text=label,
            episode_no=episode_no,
            object_key=key,
        )
    except media_render_service.MediaRenderError as exc:
        logger.warning(
            "导出成片合成失败，回退占位产物 job_id=%s project=%s：%s",
            job.id, getattr(project, "id", None), str(exc)[:400],
        )
        return None


async def _run_export_pipeline(job_id: int) -> None:
    """导出后台管线：生成 master 时间线 + 成片产物 → succeeded。

    步骤：running → 复制 approved 时间线为 master（含全部片段）→
    生成导出元数据 JSON（object_key，内容含「成片文件」占位说明）落
    存储与 media_files → 任务 succeeded（progress=100）→ 提交 →
    发 ``export.done``。失败 → 任务 failed（error_reason）。

    Args:
        job_id: 导出任务 ID。
    """
    try:
        async with async_session_factory() as session:
            repo = ExportRepository(session)
            job = await session.get(ExportJob, job_id)
            if job is None:
                raise RuntimeError(f"导出任务不存在 job_id={job_id}")
            job.status = "running"
            job.started_at = _utcnow()
            job.progress = 10
            await session.commit()

            post_repo = PostRepository(session)
            project = await session.get(Project, int(job.project_id))
            source = await post_repo.get_timeline(int(job.project_id), "preview")

            # ---- 1. 生成 master 时间线（复制 approved 预览时间线与片段）----
            master = await post_repo.get_timeline(int(job.project_id), "master")
            if master is None:
                master = await post_repo.create_timeline(int(job.project_id), "master")
            if source is not None:
                await post_repo.delete_clips(int(master.id))
                for clip in await post_repo.list_clips(int(source.id)):
                    await post_repo.add_clip(
                        TimelineClip(
                            timeline_id=int(master.id),
                            track_type=clip.track_type,
                            source_type=clip.source_type,
                            source_id=clip.source_id,
                            order_index=clip.order_index,
                            start_ms=clip.start_ms,
                            end_ms=clip.end_ms,
                            in_point_ms=clip.in_point_ms,
                            out_point_ms=clip.out_point_ms,
                            transition=clip.transition,
                            volume_db=clip.volume_db,
                            subtitle_style_id=clip.subtitle_style_id,
                            content=clip.content,
                        )
                    )
                master.duration_ms = int(source.duration_ms)
                master.status = "approved"
                master.approved_at = _utcnow()
                if job.timeline_id is None:
                    job.timeline_id = int(master.id)

            # ---- 2. 成片产物：复用时间线合成成片/现场合成 ----
            # P0-1 / P0-2 修复：历史实现只写一份 JSON 元数据并声明 mime=video/mp4，
            # 浏览器与播放器都无法播放；后来虽改为纯色 MP4，但内容与时间线无关。
            # 现在优先复用合成阶段的成片，缺失时按母版时间线现场合成（拼接镜头
            # 视频 + 混音 + 字幕 + 可选水印）；仅在 ffmpeg 不可用时回退占位文件。
            export_meta = {
                "kind": "export",
                "export_job_id": int(job.id),
                "project_id": int(job.project_id),
                "project_title": project.title if project else None,
                "resolution": job.resolution,
                "with_watermark": bool(job.has_watermark),
                "duration_ms": int(master.duration_ms) if master else 0,
                "timeline_id": int(master.id) if master else None,
                "generated_at": _iso(_utcnow()),
            }
            width, height = _EXPORT_SIZES.get(job.resolution or "1080p", (1080, 1920))
            # 合成为阻塞操作：先提交当前事务，避免长时间占用数据库事务
            await session.commit()
            export_episode = None
            if job.params and isinstance(job.params, dict):
                try:
                    export_episode = int(job.params.get("episode_no") or 0) or None
                except (TypeError, ValueError):
                    export_episode = None
            render_result = await _render_export_video(
                session,
                job=job,
                project=project,
                master=master,
                width=width,
                height=height,
                episode_no=export_episode,
            )
            if render_result is not None:
                object_key = render_result.object_key
                mime_type = render_result.mime_type
                size_bytes = int(render_result.size_bytes)
                duration_ms = int(render_result.duration_ms)
                media_meta = {
                    "kind": "export_video",
                    "width": width,
                    "height": height,
                    "fps": render_result.fps,
                    "degraded": bool(render_result.degraded),
                    "warnings": list(render_result.warnings),
                    "export_meta": export_meta,
                }
                export_meta["placeholder"] = False
                export_meta["note"] = None
            else:
                # ffmpeg 不可用：回退可播放的纯色 MP4；再不行才写 JSON 审计信息
                export_meta["placeholder"] = True
                export_meta["note"] = _PLACEHOLDER_VIDEO_NOTE
                mp4_bytes = await asyncio.to_thread(
                    _build_placeholder_mp4,
                    width,
                    height,
                    max(int(export_meta["duration_ms"]), 1000),
                    24,
                )
                duration_ms = int(export_meta["duration_ms"])
                if mp4_bytes is not None:
                    object_key = storage.save_bytes(
                        f"exports/{job.id}/{new_request_id().lower()}.mp4", mp4_bytes,
                    )
                    mime_type = "video/mp4"
                    size_bytes = len(mp4_bytes)
                    media_meta = {
                        "placeholder": "export",
                        "note": export_meta["note"],
                        "width": width,
                        "height": height,
                        "fps": 24,
                        "export_meta": export_meta,
                    }
                else:
                    object_key = storage.save_bytes(
                        f"exports/{job.id}/{new_request_id().lower()}.json",
                        json.dumps(export_meta, ensure_ascii=False, indent=2).encode("utf-8"),
                    )
                    mime_type = "application/json"
                    size_bytes = 0
                    media_meta = {
                        "placeholder": "export",
                        "note": "ffmpeg 不可用，产物为导出元数据 JSON",
                        "export_meta": export_meta,
                    }
            media = MediaFile(
                project_id=int(job.project_id),
                storage_provider="cos",
                bucket="gleamdream-local",
                object_key=object_key,
                url=f"/files/{object_key}",
                media_type="video",
                mime_type=mime_type,
                file_size_bytes=size_bytes,
                duration_ms=duration_ms or None,
                meta=media_meta,
            )
            session.add(media)
            await session.flush()

            job.output_media_file_id = int(media.id)
            # 修复（E2E 报告 P-14）：回填产物真实字节数，供分发中心
            # 「总文件大小」与平台体积合规校验使用（此前恒为 NULL）。
            job.output_size_bytes = int(size_bytes)
            job.status = "succeeded"
            job.progress = 100
            job.finished_at = _utcnow()
            # P3-2 修复：导出完成后把实际成片产物与参数回写母版时间线，
            # 避免审阅页展示/引用的分辨率、帧率与真实成片不一致。
            if master is not None:
                master.rendered_media_file_id = int(media.id)
                meta_width = int(media_meta.get("width") or 0)
                meta_height = int(media_meta.get("height") or 0)
                meta_fps = int(media_meta.get("fps") or 0)
                if meta_height >= 1080:
                    master.resolution = "1080p"
                elif meta_height >= 720:
                    master.resolution = "720p"
                if meta_fps in (24, 25, 30):
                    master.fps = meta_fps
                state = dict(master.edit_state or {})
                state["export_render"] = {
                    "media_file_id": int(media.id),
                    "width": meta_width or None,
                    "height": meta_height or None,
                    "fps": meta_fps or None,
                    "exported_at": _utcnow().isoformat(),
                }
                master.edit_state = state
            await session.commit()

        await event_bus.publish(
            int(job.project_id),
            "export.done",
            {
                "export_job_id": job_id,
                "status": "succeeded",
                "resolution": job.resolution,
                "output_object_key": object_key,
            },
        )
        # 埋点：导出漏斗终点（成功）
        await analytics_service.track(
            analytics_service.EVENT_EXPORT_COMPLETED,
            user_id=int(project.owner_user_id) if project else None,
            project_id=int(job.project_id),
            props={"resolution": job.resolution, "export_job_id": job_id},
        )
    except Exception as exc:  # noqa: BLE001 后台管线兜底：任务标 failed
        logger.exception("导出后台管线失败 job_id=%s", job_id)
        async with async_session_factory() as session:
            job = await session.get(ExportJob, job_id)
            if job is not None:
                job.status = "failed"
                job.error_reason = str(exc)[:500]
                job.finished_at = _utcnow()
                await session.commit()
        await event_bus.publish(
            int(job.project_id) if job is not None else 0,
            "export.failed",
            {"export_job_id": job_id, "error": str(exc)[:300]},
        )
        # 埋点：导出漏斗终点（失败）
        await analytics_service.track(
            analytics_service.EVENT_EXPORT_FAILED,
            project_id=int(job.project_id) if job is not None else None,
            props={"export_job_id": job_id, "error": str(exc)[:200]},
        )


async def get_export_detail(
    session: AsyncSession, export_job_id: int
) -> dict[str, Any]:
    """查询导出任务详情（含产物下载 URL）。

    Args:
        session: 异步数据库会话。
        export_job_id: 导出任务 ID。

    Returns:
        任务详情字典（output.download_url 为 /files/... 链接）。

    Raises:
        BizError: 10002 导出任务不存在。
    """
    repo = ExportRepository(session)
    job = await repo.get_export_job(export_job_id)
    if job is None:
        raise BizError(ErrorCode.NOT_FOUND, "导出任务不存在")
    media = await _get_media(session, job.output_media_file_id)
    return _job_to_dict(job, media)


async def list_exports(session: AsyncSession, project: Project) -> dict[str, Any]:
    """查询项目导出任务列表（创建时间降序）。

    Args:
        session: 异步数据库会话。
        project: ACL 校验通过的项目实例。

    Returns:
        ``{items, total}``。
    """
    repo = ExportRepository(session)
    jobs = await repo.list_export_jobs(int(project.id))
    items: list[dict[str, Any]] = []
    for job in jobs:
        media = await _get_media(session, job.output_media_file_id)
        items.append(_job_to_dict(job, media))
    return {"items": items, "total": len(items)}


async def batch_export(
    session: AsyncSession,
    project: Project,
    *,
    resolution: str,
    with_watermark: bool = True,
) -> dict[str, Any]:
    """批量导出：每集一个 export_job（同步 succeeded）+ ZIP 打包成片 MP4。

    优先从母版成片按集时间区间切分（ffmpeg ``-ss``/``-t`` 流复制）；无母版
    或切分失败时按集现场合成；两者均不可用才回退 JSON 审计信息，保证
    ZIP 条目仍可与 ``export_jobs`` 一一对应。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例（export 权限）。
        resolution: 分辨率（720p/1080p/4k）。
        with_watermark: 是否带水印。

    Returns:
        ``{zip_object_key, download_url, file_count, jobs}``。

    Raises:
        BizError: 70003 权益不足；40001 时间线未审批；40001 项目内没有
            已渲染镜头。
    """
    benefits = await plan_benefit_service.resolve_benefits(session, int(project.owner_user_id))
    plan_benefit_service.assert_export_allowed(benefits, resolution, with_watermark)

    post_repo = PostRepository(session)
    timeline = None
    for cut_type in ("preview", "master"):
        candidate = await post_repo.get_timeline(int(project.id), cut_type)
        if candidate is not None and candidate.status == "approved":
            timeline = candidate
            break
    if timeline is None:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            "时间线尚未审批通过，无法导出（请先完成合成与成片审批）",
        )

    # 按集分组已渲染镜头
    repo = ExportRepository(session)
    layout = await _layout_by_episode(session, int(project.id))
    if not layout:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "项目内没有已渲染镜头，无法导出")

    # 查集标题（episode_no -> title）
    episode_titles = await _episode_titles(session, int(project.id))

    width, height = _EXPORT_SIZES.get(resolution or "1080p", (1080, 1920))
    # 批量导出优先复用已审批时间线的成片，按集切分（毫秒级切分，避免逐集重合成）
    master_media = await _get_media(session, timeline.rendered_media_file_id)
    master_source_key = (
        str(master_media.object_key)
        if master_media is not None and str(master_media.mime_type).startswith("video/")
        else None
    )
    # 合成/切分为阻塞操作，先提交事务，避免长时间占用数据库事务
    await session.commit()

    repo_post = PostRepository(session)
    jobs: list[dict[str, Any]] = []
    zip_entries: list[tuple[str, bytes | None, str | None]] = []
    for episode_no in sorted(layout.keys()):
        items = layout[episode_no]
        duration_ms = sum(item["duration_ms"] for item in items)
        job = ExportJob(
            project_id=int(project.id),
            timeline_id=int(timeline.id),
            resolution=resolution,
            has_watermark=bool(with_watermark),
            format="mp4",
            params={"episode_no": episode_no, "batch": True},
            status="running",
            progress=50,
            started_at=_utcnow(),
        )
        await repo.add_export_job(job)

        entry_title = episode_titles.get(episode_no) or f"第{episode_no}集"
        entry_name = (
            f"{sanitize_filename(project.title)}-EP{episode_no}-{sanitize_filename(entry_title)}.mp4"
        )
        label = (
            visible_label_text(build_ai_label(int(project.id), episode_no))
            if with_watermark
            else None
        )

        # 1) 母版成片按集切分；2) 无母版成片时按时间线现场合成；3) 均失败回退 JSON
        segment: media_render_service.TimelineRenderResult | None = None
        if master_source_key:
            episode_start = min((int(item["start_ms"]) for item in items), default=0)
            episode_end = max(
                (int(item["start_ms"]) + int(item["duration_ms"]) for item in items), default=0
            )
            segment = await media_render_service.extract_segment(
                source_key=master_source_key,
                start_ms=episode_start,
                duration_ms=max(200, episode_end - episode_start or duration_ms),
                width=width,
                height=height,
                fps=24,
                watermark_text=label,
                object_key=f"exports/{job.id}/{new_request_id().lower()}.mp4",
            )
        if segment is None:
            try:
                segment = await media_render_service.render_timeline(
                    session,
                    project_id=int(project.id),
                    timeline_id=int(timeline.id),
                    width=width,
                    height=height,
                    fps=24,
                    burn_subtitles=True,
                    watermark_text=label,
                    episode_no=episode_no,
                    object_key=f"exports/{job.id}/{new_request_id().lower()}.mp4",
                )
            except media_render_service.MediaRenderError as exc:
                logger.warning(
                    "批量导出单集合成失败 project=%s episode=%s：%s",
                    project.id, episode_no, str(exc)[:300],
                )
                segment = None

        if segment is not None:
            object_key = segment.object_key
            zip_entries.append((entry_name, None, str(storage.object_path(object_key))))
            media = MediaFile(
                project_id=int(project.id),
                storage_provider="cos",
                bucket="gleamdream-local",
                object_key=object_key,
                url=f"/files/{object_key}",
                media_type="video",
                mime_type="video/mp4",
                file_size_bytes=int(segment.size_bytes),
                duration_ms=int(segment.duration_ms or duration_ms),
                meta={
                    "kind": "batch_export_segment",
                    "episode_no": episode_no,
                    "degraded": bool(segment.degraded),
                    "warnings": list(segment.warnings),
                },
            )
        else:
            episode_meta = {
                "placeholder": True,
                "kind": "export",
                "note": _PLACEHOLDER_NOTE,
                "export_job_id": int(job.id),
                "project_id": int(project.id),
                "project_title": project.title,
                "episode_no": episode_no,
                "episode_title": entry_title,
                "resolution": resolution,
                "with_watermark": bool(with_watermark),
                "duration_ms": duration_ms,
                "shot_count": len(items),
                "generated_at": _iso(_utcnow()),
            }
            entry_bytes = json.dumps(episode_meta, ensure_ascii=False, indent=2).encode("utf-8")
            zip_entries.append((entry_name, entry_bytes, None))
            object_key = storage.save_bytes(
                f"exports/{job.id}/{new_request_id().lower()}.json", entry_bytes
            )
            media = MediaFile(
                project_id=int(project.id),
                storage_provider="cos",
                bucket="gleamdream-local",
                object_key=object_key,
                url=f"/files/{object_key}",
                media_type="video",
                mime_type="application/json",
                file_size_bytes=len(entry_bytes),
                duration_ms=duration_ms,
                meta={"placeholder": "export", "episode_no": episode_no},
            )
        session.add(media)
        await session.flush()

        job.output_media_file_id = int(media.id)
        job.status = "succeeded"
        job.progress = 100
        job.finished_at = _utcnow()
        jobs.append(_job_to_dict(job, media))

    # ---- ZIP 打包：MP4 直接写文件流，回退 JSON 写内存字节 ----
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data, path in zip_entries:
            if path is not None:
                zf.write(path, name)
            elif data is not None:
                zf.writestr(name, data)
    zip_bytes = buffer.getvalue()
    zip_key = storage.save_bytes(
        f"exports/batch/{int(project.id)}/{new_request_id().lower()}.zip", zip_bytes
    )
    zip_media = MediaFile(
        project_id=int(project.id),
        storage_provider="cos",
        bucket="gleamdream-local",
        object_key=zip_key,
        url=f"/files/{zip_key}",
        media_type="zip",
        mime_type="application/zip",
        file_size_bytes=len(zip_bytes),
        meta={"kind": "batch_export", "file_count": len(zip_entries)},
    )
    session.add(zip_media)
    await session.flush()
    await session.commit()

    await event_bus.publish(
        int(project.id),
        "export.done",
        {
            "batch": True,
            "zip_object_key": zip_key,
            "file_count": len(zip_entries),
            "job_count": len(jobs),
        },
    )
    return {
        "zip_object_key": zip_key,
        "download_url": f"/files/{zip_key}",
        "file_count": len(zip_entries),
        "jobs": jobs,
    }


async def _layout_by_episode(
    session: AsyncSession, project_id: int
) -> dict[int, list[dict[str, Any]]]:
    """按集分组计算已渲染镜头布局（用于批量导出）。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        ``{episode_no: [{shot_id, start_ms, duration_ms}]}``。
    """
    from sqlalchemy import select

    from app.models import Episode, Shot

    stmt = (
        select(Shot, Episode.episode_no)
        .join(Episode, Episode.id == Shot.episode_id)
        .where(Shot.project_id == project_id, Shot.status == "rendered")
        .order_by(Episode.episode_no.asc(), Shot.order_index.asc(), Shot.id.asc())
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    # 每集内按镜头顺序累加起点（此前全为 0，导致批量导出时长/切分口径错误）
    cursor_by_episode: dict[int, int] = {}
    for shot, episode_no in (await session.execute(stmt)).all():
        episode_key = int(episode_no)
        start_ms = cursor_by_episode.get(episode_key, 0)
        duration_ms = max(0, int(shot.duration_ms))
        grouped.setdefault(episode_key, []).append(
            {
                "shot_id": int(shot.id),
                "start_ms": start_ms,
                "duration_ms": duration_ms,
            }
        )
        cursor_by_episode[episode_key] = start_ms + duration_ms
    return grouped


async def _episode_titles(session: AsyncSession, project_id: int) -> dict[int, str]:
    """查询项目各集标题映射。

    Args:
        session: 异步数据库会话。
        project_id: 项目 ID。

    Returns:
        ``{episode_no: title}``（无标题回退 ``第{n}集``）。
    """
    from sqlalchemy import select

    from app.models import Episode

    stmt = select(Episode.episode_no, Episode.title).where(Episode.project_id == project_id)
    return {int(no): (title or f"第{int(no)}集") for no, title in (await session.execute(stmt)).all()}


# =====================================================================
# 发行元数据（发行 Agent · 真实 LLM）
# =====================================================================


def _build_distribution_prompt(project: Project, plot: str, episodes: int) -> str:
    """构造发行 Agent 的中文五段式提示词。

    Args:
        project: 项目实例。
        plot: 剧情概要（镜头动作描述节选）。
        episodes: 总集数。

    Returns:
        完整提示词字符串。
    """
    return _DISTRIBUTION_PROMPT_TEMPLATE.format(
        title=project.title or "未命名短剧",
        genre=project.genre or "都市情感",
        synopsis=(project.synopsis or project.title or "短剧作品")[:200],
        episodes=episodes,
        plot=plot[:600] or "（暂无剧情概要）",
    )


def _fallback_metadata(project: Project, episodes: int) -> dict[str, Any]:
    """LLM 不可用时的确定性兜底元数据（保证字段齐全）。

    Args:
        project: 项目实例。
        episodes: 总集数。

    Returns:
        ``{title, description, tags, cover_suggestions}``。
    """
    title = (project.title or "浮光短剧").strip()
    synopsis = (project.synopsis or "一段由 AI 多智能体创作的短剧故事").strip()
    return {
        "title": title[:20],
        "description": synopsis[:80],
        "tags": ["#短剧推荐", "#AI创作", "#都市情感", "#一口气看完", "#浮光造梦"],
        "cover_suggestions": [
            "主角面部特写，眼神坚定略带忧伤，叠加标题文字置于下方三分之一处",
            "冲突爆发的对峙瞬间，冷暖对撞色调，左上角叠加悬念式短句",
        ],
        "episodes": episodes,
    }


def _normalize_metadata(raw: dict[str, Any], project: Project, episodes: int) -> dict[str, Any]:
    """校验并规范化 LLM 返回的元数据（字段缺失回退兜底值）。

    Args:
        raw: LLM 返回的原始字典。
        project: 项目实例（兜底取值）。
        episodes: 总集数。

    Returns:
        规范化后的 ``{title, description, tags, cover_suggestions}``。
    """
    fallback = _fallback_metadata(project, episodes)

    def _clean_str(value: Any, limit: int) -> str | None:
        """清洗字符串字段并限长。

        Args:
            value: 原始值。
            limit: 最大长度。

        Returns:
            清洗后的字符串；空值返回 None。
        """
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned[:limit] or None

    def _clean_list(value: Any, limit: int, count: int, prefix: str) -> list[str]:
        """清洗字符串数组字段（限长限量，缺失项以占位补齐）。

        Args:
            value: 原始值。
            limit: 单项最大长度。
            count: 期望数量下限。
            prefix: 缺失项占位前缀。

        Returns:
            清洗后的字符串列表。
        """
        items: list[str] = []
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    cleaned = item.strip()[:limit]
                    if cleaned not in items:
                        items.append(cleaned)
        while len(items) < count:
            items.append(f"{prefix}{len(items) + 1}")
        return items[: max(count, len(items))]

    title = _clean_str(raw.get("title"), 20) or fallback["title"]
    description = _clean_str(raw.get("description"), 80) or fallback["description"]
    tags = _clean_list(raw.get("tags"), 24, 5, "#标签")
    covers = _clean_list(raw.get("cover_suggestions"), 120, 2, "封面建议")
    return {
        "title": title,
        "description": description,
        "tags": tags[:5] if len(tags) >= 5 else tags,
        "cover_suggestions": covers[:3] if len(covers) >= 3 else covers,
    }


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 输出中提取 JSON 对象（容忍 markdown 代码块包裹）。

    Args:
        text: LLM 原始输出文本。

    Returns:
        解析出的字典。

    Raises:
        ValueError: 未找到或解析不出 JSON 对象。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出中未找到 JSON 对象")
    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("输出不是 JSON 对象")
    return parsed


async def generate_metadata(session: AsyncSession, project: Project) -> dict[str, Any]:
    """发行 Agent 生成发行元数据（真实 LLM，五段式提示词，不落库）。

    LLM 输出经 JSON 解析与字段规范化；模型不可用/解析失败时回退
    确定性兜底元数据（保证响应字段齐全，前端可编辑）。

    Args:
        session: 请求级数据库会话（只读项目与镜头概要）。
        project: ACL 校验通过的项目实例。

    Returns:
        ``{title, description, tags, cover_suggestions, episodes,
        source}``（source = llm | fallback）。
    """
    from sqlalchemy import func, select

    from app.models import Episode, Shot

    episode_count = int(
        (
            await session.execute(
                select(func.count()).select_from(Episode).where(Episode.project_id == int(project.id))
            )
        ).scalar_one()
    )
    plot_rows = (
        (
            await session.execute(
                select(Shot.action_desc)
                .where(Shot.project_id == int(project.id), Shot.action_desc.isnot(None))
                .order_by(Shot.id.asc())
                .limit(6)
            )
        )
        .scalars()
        .all()
    )
    plot = "；".join(str(row) for row in plot_rows)

    prompt = _build_distribution_prompt(project, plot, episode_count)
    try:
        result = await model_gateway.generate(
            "llm",
            "llm_text",
            GenRequest(
                capability="llm",
                prompt=prompt,
                params={"temperature": 0.7, "response_json": True},
            ),
            project_id=int(project.id),
        )
        if not result.ok or not result.text:
            raise RuntimeError(result.error or "LLM 空响应")
        raw = _extract_json(result.text)
        data = _normalize_metadata(raw, project, episode_count)
        data["source"] = "llm"
    except Exception as exc:  # noqa: BLE001 LLM 失败回退兜底元数据
        logger.warning("发行元数据 LLM 生成失败，使用兜底 project_id=%s err=%s", project.id, exc)
        data = _fallback_metadata(project, episode_count)
        data["source"] = "fallback"
    return data


# =====================================================================
# 平台授权（Stub OAuth）与分发
# =====================================================================


def _account_to_dict(account: PlatformAccount, *, mask_token: bool = True) -> dict[str, Any]:
    """把平台授权 ORM 序列化为响应字典。

    Args:
        account: 授权实例。
        mask_token: 是否脱敏 token（响应恒脱敏）。

    Returns:
        授权字典（token 摘要化，不泄露明文）。
    """
    token_display = None
    if account.access_token_encrypted:
        token_display = (
            f"{account.access_token_encrypted[:6]}…{account.access_token_encrypted[-4:]}"
            if not mask_token
            else "已加密存储"
        )
    return {
        "id": int(account.id),
        "platform": account.platform,
        "platform_user_id": account.platform_user_id,
        "status": account.status,
        "scope": account.scope,
        "token_expires_at": _iso(account.token_expires_at),
        "authorized_at": _iso(account.authorized_at),
        "token_masked": token_display,
    }


async def list_platform_accounts(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """查询当前用户全部平台授权。

    Args:
        session: 异步数据库会话。
        user_id: 用户 ID。

    Returns:
        ``{items, total}``。
    """
    repo = ExportRepository(session)
    accounts = await repo.list_platform_accounts(user_id)
    return {"items": [_account_to_dict(a) for a in accounts], "total": len(accounts)}


async def authorize_platform(
    session: AsyncSession, user_id: int, *, platform: str, mock_authorize: bool = True
) -> dict[str, Any]:
    """Stub OAuth 平台授权：创建 active 授权记录（token 为 sha256 占位）。

    Args:
        session: 请求级数据库会话。
        user_id: 用户 ID。
        platform: 平台键（douyin/kuaishou/wechat_channels/jianying）。
        mock_authorize: 是否走模拟授权（演示环境恒 True）。

    Returns:
        授权字典。

    Raises:
        BizError: 10001 平台非法或未走模拟授权。
    """
    if platform not in _PLATFORMS:
        raise BizError(ErrorCode.PARAM_INVALID, f"不支持的平台: {platform}")
    if not mock_authorize:
        raise BizError(ErrorCode.PARAM_INVALID, "演示环境仅支持模拟授权（mock_authorize=true）")

    now = _utcnow()
    nonce = new_request_id()
    token_hash = hashlib.sha256(f"{platform}:{nonce}:{user_id}".encode("utf-8")).hexdigest()
    repo = ExportRepository(session)
    account = PlatformAccount(
        user_id=user_id,
        platform=platform,
        platform_user_id=f"DEMO-{nonce}",
        access_token_encrypted=token_hash,
        refresh_token_encrypted=hashlib.sha256(token_hash.encode("utf-8")).hexdigest(),
        token_expires_at=now + timedelta(days=30),
        scope="video.publish",
        status="active",
        authorized_at=now,
    )
    await repo.add_platform_account(account)
    await session.commit()
    return _account_to_dict(account)


async def delete_platform_account(session: AsyncSession, user_id: int, account_id: int) -> dict[str, Any]:
    """删除平台授权（校验归属）。

    Args:
        session: 请求级数据库会话。
        user_id: 当前用户 ID。
        account_id: 授权 ID。

    Returns:
        ``{id, deleted}``。

    Raises:
        BizError: 10002 授权不存在；20003 无权限删除他人授权。
    """
    repo = ExportRepository(session)
    account = await repo.get_platform_account(account_id)
    if account is None:
        raise BizError(ErrorCode.NOT_FOUND, "平台授权不存在")
    if int(account.user_id) != int(user_id):
        raise BizError(ErrorCode.FORBIDDEN)
    await repo.delete_platform_account(account_id)
    await session.commit()
    return {"id": account_id, "deleted": True}


async def distribute(
    session: AsyncSession,
    project: Project,
    user_id: int,
    *,
    platform: str,
    metadata: dict[str, Any],
    export_job_id: int | None = None,
) -> dict[str, Any]:
    """分发到三方平台（模拟上传，同步 2s 延迟）。

    校验平台授权有效（过期 → 40001「授权已失效，请重新授权」并标记
    expired）→ 模拟上传 2s → distribution_records(succeeded) 落库 →
    发 ``distribution.completed`` 事件。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        user_id: 当前用户 ID。
        platform: 平台键。
        metadata: 发行元数据（title/description/tags）。
        export_job_id: 关联导出任务 ID（可空）。

    Returns:
        分发记录字典。

    Raises:
        BizError: 10001 平台非法；40001 无有效授权/授权已失效；
            10002 导出任务不存在。
    """
    if platform not in _PLATFORMS:
        raise BizError(ErrorCode.PARAM_INVALID, f"不支持的平台: {platform}")

    repo = ExportRepository(session)
    if export_job_id is not None:
        job = await repo.get_export_job(export_job_id)
        if job is None or int(job.project_id) != int(project.id):
            raise BizError(ErrorCode.NOT_FOUND, "导出任务不存在")

    account = await repo.get_latest_active_platform_account(int(user_id), platform)
    if account is None:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"尚未授权 {platform} 平台，请先完成平台授权",
        )
    now = _utcnow()
    if account.token_expires_at is not None and account.token_expires_at <= now:
        await repo.expire_platform_account(account, now)
        await session.commit()
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "授权已失效，请重新授权")

    # 模拟上传（演示环境同步 2s 延迟）
    await asyncio.sleep(2.0)

    external_video_id = f"DEMO-{new_request_id()}"
    record = DistributionRecord(
        project_id=int(project.id),
        export_job_id=export_job_id,
        platform_account_id=int(account.id),
        platform=platform,
        external_video_id=external_video_id,
        publish_url=f"https://demo.{platform}.example.com/video/{external_video_id}",
        title=str(metadata.get("title") or project.title or "未命名短剧")[:255],
        status="succeeded",
        distributed_at=_utcnow(),
    )
    await repo.add_distribution_record(record)
    record_id = int(record.id)
    publish_url = record.publish_url
    title = record.title
    await session.commit()

    await event_bus.publish(
        int(project.id),
        "distribution.completed",
        {
            "distribution_id": record_id,
            "platform": platform,
            "external_video_id": external_video_id,
            "publish_url": publish_url,
        },
    )
    # 埋点：导出漏斗末端（平台分发完成）
    await analytics_service.track(
        analytics_service.EVENT_PROJECT_DISTRIBUTED,
        user_id=int(user_id),
        project_id=int(project.id),
        props={"platform": platform, "distribution_id": record_id},
    )
    return {
        "id": record_id,
        "platform": platform,
        "external_video_id": external_video_id,
        "publish_url": publish_url,
        "title": title,
        "status": "succeeded",
        "distributed_at": _iso(record.distributed_at),
    }


async def list_distributions(session: AsyncSession, project: Project) -> dict[str, Any]:
    """查询项目分发记录列表。

    Args:
        session: 异步数据库会话。
        project: ACL 校验通过的项目实例。

    Returns:
        ``{items, total}``。
    """
    repo = ExportRepository(session)
    records = await repo.list_distribution_records(int(project.id))
    return {
        "items": [
            {
                "id": int(r.id),
                "platform": r.platform,
                "external_video_id": r.external_video_id,
                "publish_url": r.publish_url,
                "title": r.title,
                "status": r.status,
                "export_job_id": int(r.export_job_id) if r.export_job_id is not None else None,
                "distributed_at": _iso(r.distributed_at),
            }
            for r in records
        ],
        "total": len(records),
    }


# =====================================================================
# V4.0 导出域增量：历史复用 / 规格自检 / AI 标识 / 预设 / 分发重试撤回
# =====================================================================


async def _timeline_content_signature(
    session: AsyncSession, timeline_id: int | None
) -> str:
    """计算时间线内容签名（片段数/终点/更新时间/字幕行数）。

    P3-1 修复：旧缓存键只含（时间线 ID, 分辨率, 水印, 分集），时间线
    内容变化（字幕重排、配音重做、BGM 自动铺轨）后仍命中旧键，导致
    导出复用旧成片。签名纳入内容要素后，内容一变即换新键。

    Args:
        session: 数据库会话。
        timeline_id: 时间线 ID（空返回占位值）。

    Returns:
        64 位十六进制签名；查询异常时退化为 ``"nosig"``（不阻断导出）。

    Raises:
        无：内部捕获查询异常并退化为旧行为。
    """
    if not timeline_id:
        return "nosig"
    try:
        clip_row = (
            await session.execute(
                select(
                    func.count(TimelineClip.id),
                    func.max(TimelineClip.end_ms),
                    func.max(TimelineClip.updated_at),
                ).where(TimelineClip.timeline_id == int(timeline_id))
            )
        ).one()
        sub_count = (
            await session.execute(
                select(func.count(SubtitleLine.id)).where(
                    SubtitleLine.timeline_id == int(timeline_id)
                )
            )
        ).scalar_one()
        updated = clip_row[2].isoformat() if clip_row[2] else ""
        raw = (
            f"{int(clip_row[0] or 0)}|{int(clip_row[1] or 0)}"
            f"|{updated}|{int(sub_count or 0)}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]
    except Exception:  # noqa: BLE001 签名失败退化为旧行为，不阻断导出
        return "nosig"


def _build_cache_key(
    timeline_id: int,
    resolution: str,
    with_watermark: bool,
    episode_no: int | None,
    content_signature: str = "nosig",
) -> str:
    """构造转码缓存键（成片版本 ID + 参数 hash，AC-F7-06-2 / DR-12）。

    P3-1 修复：键纳入时间线内容签名——字幕重排、配音重做、BGM 自动
    铺轨等时间线内容变化后不再命中旧缓存，避免导出复用旧成片。

    Args:
        timeline_id: 成片时间线 ID（作为成片版本标识）。
        resolution: 分辨率。
        with_watermark: 是否带水印。
        episode_no: 集号（可空）。
        content_signature: 时间线内容签名（P3-1 修复新增）。

    Returns:
        64 位十六进制缓存键。

    Raises:
        无。
    """
    raw = (
        f"tl:{timeline_id}|res:{resolution}|wm:{int(with_watermark)}"
        f"|ep:{episode_no or 'all'}|sig:{content_signature}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


async def list_export_history(
    session: AsyncSession, project: Project, *, history: bool = False
) -> dict[str, Any]:
    """查询导出历史（AC-F7-06-1：时间/集数/参数摘要/文件状态/消耗，保留 90 天）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        history: 是否为「历史」视图（True 时按过期标记 ``file_available``）。

    Returns:
        ``{items, total, retention_days}``。
    """
    repo = ExportRepository(session)
    jobs = await repo.list_export_jobs(int(project.id))
    now = _utcnow()
    items: list[dict[str, Any]] = []
    for job in jobs:
        media = await _get_media(session, job.output_media_file_id)
        data = _job_to_dict(job, media)
        expired = job.expires_at is not None and job.expires_at <= now
        data.update(
            {
                "episode_no": (job.params or {}).get("episode_no"),
                "preset_id": int(job.preset_id) if job.preset_id else None,
                "name_template": job.name_template,
                "cache_key": job.cache_key,
                "ai_label_applied": bool(job.ai_label_applied),
                "expires_at": _iso(job.expires_at),
                "file_available": bool(media and not expired),
                "consumed_credits": 0,
                "parameter_summary": (
                    f"{job.resolution} · "
                    f"{'带水印' if job.has_watermark else '无水印'} · "
                    f"第 {(job.params or {}).get('episode_no') or '全部'} 集"
                ),
            }
        )
        items.append(data)
    if history:
        items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {"items": items, "total": len(items), "retention_days": EXPORT_RETENTION_DAYS}


async def reuse_export_job(
    session: AsyncSession,
    project: Project,
    *,
    job_id: int,
    resolution: str | None,
    name_template: str | None,
    idempotency_key: str | None,
) -> dict[str, Any]:
    """复用历史导出参数重新导出（AC-F7-06-2）。

    同参数命中转码缓存时 ``cache_hit=True``（仅重打包不重转码）；
    成片版本已更新时返回 ``latest_timeline_changed=True`` 提示
    「将导出当前最新成片」。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        job_id: 历史导出任务 ID。
        resolution: 覆写分辨率（可空）。
        name_template: 覆写命名模板（可空）。
        idempotency_key: 幂等键（可空）。

    Returns:
        ``{export_job, reused_from, cache_hit, latest_timeline_changed}``。

    Raises:
        BizError: 10002 历史记录不存在/不属于该项目。
    """
    repo = ExportRepository(session)
    source = await repo.get_export_job(job_id)
    if source is None or int(source.project_id) != int(project.id):
        raise BizError(ErrorCode.NOT_FOUND, "历史导出记录不存在")
    params = source.params or {}
    target_resolution = resolution or source.resolution
    target_template = name_template if name_template is not None else source.name_template
    episode_no = params.get("episode_no")

    new_key = _build_cache_key(
        int(source.timeline_id) if source.timeline_id else 0,
        target_resolution,
        bool(source.has_watermark),
        episode_no,
        content_signature=await _timeline_content_signature(
            session, source.timeline_id
        ),
    )
    cached = await repo.find_by_cache_key(new_key)
    cache_hit = cached is not None and int(cached.id) != int(source.id)

    post_repo = PostRepository(session)
    latest_preview = await post_repo.get_timeline(int(project.id), "preview")
    latest_changed = (
        latest_preview is not None
        and source.timeline_id is not None
        and int(latest_preview.id) != int(source.timeline_id)
    )

    result = await create_export(
        session,
        project,
        resolution=target_resolution,
        with_watermark=bool(source.has_watermark),
        episode_no=int(episode_no) if episode_no else None,
        idempotency_key=idempotency_key,
        preset_id=int(source.preset_id) if source.preset_id else None,
        name_template=target_template,
    )
    return {
        "export_job": result["export_job"],
        "reused_from": int(source.id),
        "cache_hit": cache_hit,
        "latest_timeline_changed": latest_changed,
        "message": (
            "参数已复用；命中转码缓存，仅重新打包"
            if cache_hit
            else "参数已复用，将按最新成片重新转码"
        ),
    }


async def prespec_check(
    session: AsyncSession,
    project: Project,
    *,
    platform: str,
    resolution: str,
    episode_no: int | None,
) -> dict[str, Any]:
    """目标平台规格自检（AC-F7-07-1~2）。

    校验项：时长上限 / 文件大小 / 分辨率与码率区间 / 封面尺寸；阻断项
    （``blocked=True``）时导出按钮应禁用并列出修改建议。平台规则库不可用时
    降级为通用基线并提示「规则库未更新，结果仅供参考」。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        platform: 目标平台键。
        resolution: 计划分辨率。
        episode_no: 目标集号（可空校验全剧）。

    Returns:
        ``{platform, rules, checks, blocked, recommendations, degraded}``。

    Raises:
        BizError: 10001 平台非法。
    """
    if platform not in _PLATFORMS:
        raise BizError(
            ErrorCode.PARAM_INVALID, f"不支持的平台: {platform}", error_detail="platform_invalid"
        )
    rules = PLATFORM_RULES.get(platform)
    degraded = False
    if rules is None:
        rules = GENERIC_BASELINE_RULES
        degraded = True

    post_repo = PostRepository(session)
    timeline = await post_repo.get_timeline(int(project.id), "preview")
    duration_ms = int(timeline.duration_ms or 0) if timeline is not None else 0
    duration_sec = duration_ms / 1000 if duration_ms else 0

    checks: list[dict[str, Any]] = []

    def _add(item: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
        """追加一条自检结论。

        Args:
            item: 检查项名。
            ok: 是否通过。
            detail: 说明。
            blocking: 未通过时是否阻断导出。
        """
        checks.append(
            {
                "item": item,
                "ok": ok,
                "blocking": blocking,
                "detail": detail,
            }
        )

    max_sec = int(rules["max_duration_sec"])
    _add(
        "时长上限",
        0 < duration_sec <= max_sec,
        (
            f"成片 {duration_sec:.0f} 秒，平台上限 {max_sec} 秒"
            if duration_sec
            else "尚无已审批成片，无法复核时长"
        ),
    )
    _add(
        "分辨率",
        resolution in rules["resolutions"],
        f"{resolution} {'在' if resolution in rules['resolutions'] else '不在'}平台支持范围 {rules['resolutions']}",
    )
    _add(
        "画面比例",
        project.aspect_ratio in rules["aspects"],
        f"项目比例 {project.aspect_ratio}，平台支持 {rules['aspects']}",
    )
    _add(
        "文件大小",
        True,
        f"平台上限 {rules['max_size_mb']}MB（成片打包后复核）",
        blocking=False,
    )
    _add(
        "封面尺寸",
        rules.get("cover_size") is not None,
        f"平台封面要求 {rules.get('cover_size') or '无硬性要求'}",
        blocking=False,
    )

    blocked_items = [c for c in checks if c["blocking"] and not c["ok"]]
    recommendations: list[str] = []
    for item in blocked_items:
        if item["item"] == "时长上限":
            recommendations.append(
                f"第 {(episode_no or 1)} 集成片 {duration_sec:.0f} 秒超平台 {max_sec} 秒上限，请拆分或压缩"
            )
        elif item["item"] == "分辨率":
            recommendations.append(f"将分辨率调整为 {rules['resolutions']} 之一")
        elif item["item"] == "画面比例":
            recommendations.append(f"将画面比例调整为 {rules['aspects']} 之一")
    return {
        "platform": platform,
        "platform_label": rules["label"],
        "rules": rules,
        "checks": checks,
        "blocked": bool(blocked_items),
        "blocked_items": [c["item"] for c in blocked_items],
        "recommendations": recommendations,
        "degraded": degraded,
        "degraded_hint": "规则库未更新，结果仅供参考" if degraded else None,
    }


def _preset_to_dict(preset: ExportPreset) -> dict[str, Any]:
    """把导出预设 ORM 序列化为响应字典。

    Args:
        preset: ExportPreset 实例。

    Returns:
        预设字典。
    """
    return {
        "id": int(preset.id),
        "name": preset.name,
        "params": preset.params or {},
        "is_default": bool(preset.is_default),
        "created_at": _iso(preset.created_at),
    }


async def list_export_presets(
    session: AsyncSession, user_id: int
) -> dict[str, Any]:
    """列出用户的导出预设（上限 10 个，AC-F7-01-3）。

    Args:
        session: 请求级数据库会话。
        user_id: 当前用户 ID。

    Returns:
        ``{items, total, limit}``。
    """
    rows = (
        await session.execute(
            select(ExportPreset)
            .where(ExportPreset.owner_user_id == user_id)
            .order_by(ExportPreset.id.asc())
        )
    ).scalars().all()
    return {
        "items": [_preset_to_dict(row) for row in rows],
        "total": len(rows),
        "limit": MAX_EXPORT_PRESETS,
    }


async def save_export_preset(
    session: AsyncSession, user_id: int, body: Any
) -> dict[str, Any]:
    """保存导出预设（同名覆盖，上限 10 个，AC-F7-01-3）。

    Args:
        session: 请求级数据库会话。
        user_id: 当前用户 ID。
        body: ExportPresetRequest 实例。

    Returns:
        ``{preset, total}``。

    Raises:
        BizError: 70004 预设数量达上限。
    """
    rows = (
        await session.execute(
            select(ExportPreset).where(ExportPreset.owner_user_id == user_id)
        )
    ).scalars().all()
    existing = next((row for row in rows if row.name == body.name), None)
    if existing is None and len(rows) >= MAX_EXPORT_PRESETS:
        raise BizError(
            ErrorCode.EXPORT_LIMIT_EXCEEDED,
            f"导出预设最多 {MAX_EXPORT_PRESETS} 个",
            error_detail="export_preset_limit_exceeded",
        )
    preset = existing or ExportPreset(owner_user_id=user_id, name=body.name)
    preset.name = body.name
    preset.params = {
        "resolution": body.resolution,
        "aspect": body.aspect,
        "watermark": bool(body.watermark),
        "name_template": body.name_template,
    }
    preset.is_default = bool(body.is_default)
    if existing is None:
        session.add(preset)
    await session.commit()
    await session.refresh(preset)
    return {
        "preset": _preset_to_dict(preset),
        "total": len(rows) + (0 if existing else 1),
    }


async def delete_export_preset(
    session: AsyncSession, user_id: int, preset_id: int
) -> dict[str, Any]:
    """删除导出预设。

    Args:
        session: 请求级数据库会话。
        user_id: 当前用户 ID。
        preset_id: 预设 ID。

    Returns:
        ``{preset_id, deleted}``。

    Raises:
        BizError: 10002 预设不存在或非本人所有。
    """
    preset = await session.get(ExportPreset, preset_id)
    if preset is None or int(preset.owner_user_id) != user_id:
        raise BizError(ErrorCode.NOT_FOUND, "导出预设不存在")
    await session.delete(preset)
    await session.commit()
    return {"preset_id": preset_id, "deleted": True}


async def _get_distribution_or_error(
    session: AsyncSession, project: Project, record_id: int
) -> DistributionRecord:
    """加载分发记录并校验归属项目。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        record_id: 分发记录 ID。

    Returns:
        DistributionRecord 实例。

    Raises:
        BizError: 10002 分发记录不存在或不属于该项目。
    """
    record = await session.get(DistributionRecord, record_id)
    if record is None or int(record.project_id) != int(project.id):
        raise BizError(ErrorCode.NOT_FOUND, "分发记录不存在")
    return record


async def retry_distribution(
    session: AsyncSession, project: Project, *, record_id: int, user_id: int
) -> dict[str, Any]:
    """分发重试（AC-F7-09-1）。

    授权过期类失败需先重新授权（``need_reauth=True``）；每次重试累加
    ``retry_count``，失败原因同步刷新为用户语言文案。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        record_id: 分发记录 ID。
        user_id: 操作人用户 ID。

    Returns:
        ``{record, need_reauth, retry_count}``。

    Raises:
        BizError: 10002 记录不存在；40001 状态不允许重试。
    """
    record = await _get_distribution_or_error(session, project, record_id)
    if record.status not in ("failed", "withdrawn", "pending", "draft"):
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"分发状态 {record.status} 不允许重试",
            data={"status": record.status},
        )
    error_key = (record.error_reason or "").strip().lower()
    need_reauth = error_key in ("auth_expired", "401", "403")
    if need_reauth:
        record.fail_reason_user = message_mapping.distribution_failure_message(
            "auth_expired", record.error_reason
        )
        await session.commit()
        return {
            "record": _distribution_to_dict(record),
            "need_reauth": True,
            "retry_count": int(record.retry_count or 0),
            "message": "平台授权已过期，请重新授权后再分发",
        }

    record.retry_count = int(record.retry_count or 0) + 1
    record.status = "submitting"
    record.error_reason = None
    record.fail_reason_user = None
    await session.commit()
    await event_bus.publish(
        int(project.id),
        "distribution.retried",
        {"distribution_id": int(record.id), "retry_count": int(record.retry_count)},
    )
    return {
        "record": _distribution_to_dict(record),
        "need_reauth": False,
        "retry_count": int(record.retry_count),
        "message": "已重新提交分发",
    }


async def withdraw_distribution(
    session: AsyncSession, project: Project, *, record_id: int, user_id: int
) -> dict[str, Any]:
    """分发撤回（AC-F7-09-1，依平台能力）。

    Args:
        session: 请求级数据库会话。
        project: ACL 校验通过的项目实例。
        record_id: 分发记录 ID。
        user_id: 操作人用户 ID。

    Returns:
        ``{record, platform_capable, message}``。

    Raises:
        BizError: 10002 记录不存在；40001 状态不允许撤回。
    """
    record = await _get_distribution_or_error(session, project, record_id)
    if record.status in ("withdrawn",):
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "该分发记录已撤回")
    if record.status not in ("succeeded", "failed", "confirming", "submitting"):
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"分发状态 {record.status} 不允许撤回",
            data={"status": record.status},
        )
    # 平台能力：仅剪映草稿与视频号支持撤回（其余平台需手动后台删除）
    platform_capable = record.platform in ("jianying", "wechat_channels")
    record.status = "withdrawn"
    if not platform_capable:
        record.fail_reason_user = "该平台不支持接口撤回，请在平台后台手动删除"
    await session.commit()
    await event_bus.publish(
        int(project.id),
        "distribution.withdrawn",
        {"distribution_id": int(record.id), "platform": record.platform},
    )
    return {
        "record": _distribution_to_dict(record),
        "platform_capable": platform_capable,
        "message": (
            "已撤回分发"
            if platform_capable
            else "平台不支持接口撤回，已标记撤回状态，请在平台后台手动删除"
        ),
    }


def _distribution_to_dict(record: DistributionRecord) -> dict[str, Any]:
    """把分发记录 ORM 序列化为响应字典（含 V4 扩展字段）。

    Args:
        record: DistributionRecord 实例。

    Returns:
        分发记录字典。
    """
    return {
        "id": int(record.id),
        "project_id": int(record.project_id),
        "platform": record.platform,
        "external_video_id": record.external_video_id,
        "publish_url": record.publish_url,
        "title": record.title,
        "status": record.status,
        "status_label": _DISTRIBUTION_STATUS_LABELS.get(record.status, record.status),
        "error_reason": record.error_reason,
        "fail_reason_user": record.fail_reason_user
        or (
            message_mapping.distribution_failure_message(
                record.error_reason, record.error_reason
            )
            if record.error_reason
            else None
        ),
        "metadata_snapshot": record.metadata_snapshot,
        "retry_count": int(record.retry_count or 0),
        "retryable": message_mapping.is_distribution_retryable(record.error_reason),
        "export_job_id": int(record.export_job_id) if record.export_job_id is not None else None,
        "distributed_at": _iso(record.distributed_at),
    }


#: 分发状态中文标签（AC-F7-09-1 状态机展示）
_DISTRIBUTION_STATUS_LABELS: dict[str, str] = {
    "draft": "草稿",
    "pending": "待提交",
    "submitting": "提交中",
    "uploading": "上传中",
    "confirming": "确认中",
    "succeeded": "成功",
    "failed": "失败",
    "withdrawn": "已撤回",
}
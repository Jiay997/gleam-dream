"""后期域路由模块（Task 9 · Post）。

提供 TTS 试听、全剧 TTS 配音、字幕生成、BGM 推荐/设置、时间线读取/
重建、合成（音画同步 QC + 成片审批单）、成片审批决策、预览地址与
字幕样式列表共 11 个端点。所有响应统一 ``success_envelope`` 包络；
写操作经项目级 ACL（owner 或 project_members 三级权限，edit+）。

路由变量 ``router = APIRouter(tags=["post"])``（无 prefix，端点自带
完整路径），由 ``app/api/__init__.py`` 聚合引入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    get_current_user,
    get_db,
    get_request_id,
    project_guard,
    require_project_acl,
)
from app.core.errors import BizError, ErrorCode, success_envelope
from app.models.post import Timeline
from app.models.project import Project
from app.schemas.post import (
    BgmReplaceRequest,
    BgmSetRequest,
    MixProfileRequest,
    PostDecideRequest,
    SubtitleBatchStyleRequest,
    SubtitleLinePatchRequest,
    SubtitlesRequest,
    TimelineSaveRequest,
    TtsPreviewRequest,
    VoiceParamsRequest,
)
from app.services import post_service

#: 后期域路由（主控聚合使用：tags=[post]，路径自带前缀）
router = APIRouter(tags=["post"])


@router.post("/projects/{project_id}/post/tts-preview")
async def tts_preview(
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    payload: TtsPreviewRequest = TtsPreviewRequest(),
) -> dict[str, Any]:
    """TTS 试听：取该集已渲染镜头台词前 3 句生成样音（stub，≤30s）。

    每次调用即时生成，不落时间线。请求体整体可省略：前端无参调用时
    使用默认空请求体（episode_no=None），避免 FastAPI 因缺少 body 返回 422。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        payload: 请求体（episode_no 可选；缺省取全剧前 3 句台词）。

    Returns:
        成功包络，data 为 ``{audio_url, duration_ms, lines}``。

    Raises:
        BizError: 40001 无可试听台词；80001 TTS 生成失败。
    """
    data = await post_service.tts_preview(session, project, payload.episode_no)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/post/tts")
async def tts_full(
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """全剧 TTS 配音（202 异步）：后台对每个含对白的已渲染镜头生成配音。

    生成产物落 media_files + audio 轨片段（起点与镜头起点对齐），
    完成后发 ``post.tts.done`` 事件。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 包络，data 为 ``{status, timeline_id, shot_count}``。

    Raises:
        BizError: 40001 没有可配音的已渲染镜头。
    """
    data = await post_service.tts_full(session, project, int(user_id))
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.post("/projects/{project_id}/post/subtitles")
async def make_subtitles(
    payload: SubtitlesRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """按镜头对白生成字幕片段（起点对齐镜头，时长优先取 TTS 时长）。

    Args:
        payload: 请求体（style_id 可选）。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{timeline_id, subtitle_count, style_id,
        duration_ms}``。

    Raises:
        BizError: 40001 无可生成字幕的镜头；10002 字幕样式不存在。
    """
    data = await post_service.make_subtitles(session, project, payload.style_id)
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/post/bgm-recommend")
async def recommend_bgm(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    emotion: Annotated[str, Query(description="情绪标签（如 tension/joy）")],
) -> dict[str, Any]:
    """按情绪标签推荐 BGM 曲目前 5 条（含版权信息）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        emotion: 情绪标签查询参数。

    Returns:
        成功包络，data 为 ``{emotion, items}``。
    """
    data = await post_service.recommend_bgm(session, project, emotion)
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/post/bgm")
async def set_bgm(
    payload: BgmSetRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """设置项目 BGM（时间线 bgm 轨元数据记录：音量 + 闪避）。

    Args:
        payload: 请求体（bgm_track_id/volume_db/sidechain）。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{timeline_id, bgm, clip, duration_ms}``。

    Raises:
        BizError: 10002 BGM 曲目不存在；10001 音量参数非法。
    """
    data = await post_service.set_bgm(
        session,
        project,
        bgm_track_id=payload.bgm_track_id,
        volume_db=payload.volume_db,
        sidechain=payload.sidechain,
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/post/timeline")
async def get_timeline(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    cut_type: Annotated[str, Query(description="版本类型 preview/master")] = "preview",
) -> dict[str, Any]:
    """查询时间线视图：时间线 + 四轨片段（按起点排序）+ 字幕样式 + BGM。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        cut_type: 版本类型查询参数（preview/master）。

    Returns:
        成功包络，data 为 ``{timeline, clips, subtitle_styles, bgm}``。

    Raises:
        BizError: 10002 时间线不存在。
    """
    data = await post_service.get_timeline_view(session, project, cut_type)
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/post/timeline")
async def save_timeline(
    payload: TimelineSaveRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """重建时间线片段（乐观：全删全插），时间线回 draft 并重算时长。

    Args:
        payload: 请求体（clips 片段列表）。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{timeline_id, status, duration_ms,
        clip_count}``。

    Raises:
        BizError: 10001 clips 为空或轨道类型/时长非法。
    """
    data = await post_service.save_timeline(
        session, project, [c.model_dump() for c in payload.clips]
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/post/voice-coverage")
async def voice_coverage(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    episode_no: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """查询有台词镜头的配音覆盖情况（只读，用于合成前拦截）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        episode_no: 集号；为空表示全部集。

    Returns:
        成功包络，data 为 ``{dialogue_shots, voiced_shots,
        expected_line_count, voiced_line_count, missing_shots,
        failed_lines, placeholder_lines, unmatched}``。

    Raises:
        BizError: 项目不存在或无访问权限（由 ``project_guard`` 抛出）。
    """
    data = await post_service.voice_coverage(session, int(project.id), episode_no)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/post/compose")
async def compose(
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> JSONResponse:
    """发起合成（202 异步）：后台执行音画同步 QC 与成片审批单创建。

    QC 通过：时间线 ``pending_review`` + final_cut 审批单 + 事件
    ``post.composed`` / ``approval.created``；QC 失败：时间线 failed
    （70001）。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        202 包络，data 为 ``{timeline_id, status, duration_ms,
        shot_count}``。

    Raises:
        BizError: 40001 项目内没有已渲染镜头。
    """
    data = await post_service.compose(session, project, int(user_id))
    return JSONResponse(status_code=202, content=success_envelope(data, request_id))


@router.post("/projects/{project_id}/post/decide")
async def decide(
    payload: PostDecideRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """成片审批决策（HITL-4）：approve 通过 / reject 驳回（反馈 ≥10 字）。

    approve：时间线 approved，项目 ``post_producing → final_review →
    completed``；reject：时间线回 draft。

    Args:
        payload: 请求体（action/feedback）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{decision, timeline_id, timeline_status,
        project_status, feedback}``。

    Raises:
        BizError: 40001 当前没有待审批的成片；10001 驳回反馈不足 10 字。
    """
    data = await post_service.decide(
        session,
        project,
        int(user_id),
        action=payload.action,
        feedback=payload.feedback,
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/post/preview-url")
async def preview_url(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """获取带水印的成片预览地址（首帧关键帧或合成元数据占位 JSON）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{url, watermark, timeline_status}``。
    """
    data = await post_service.preview_url(session, project)
    return success_envelope(data, request_id)


@router.get("/subtitle-styles")
async def list_subtitle_styles(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    project_id: Annotated[int | None, Query(description="项目 ID（可选，含项目级样式）")] = None,
) -> dict[str, Any]:
    """查询字幕样式列表（全局 + 可选项目级）。

    Args:
        user_id: 当前用户 ID（仅需登录）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        project_id: 项目 ID 查询参数（可选）。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await post_service.list_subtitle_styles(session, project_id)
    return success_envelope({"items": items, "total": len(items)}, request_id)


# =====================================================================
# V4.0 后期域增量端点
# =====================================================================


async def _character_guard(
    character_id: int, user_id: str, session: AsyncSession, need: str
) -> Project:
    """角色资产 ACL 守卫：加载角色并按归属项目校验权限。

    Args:
        character_id: 角色资产 ID。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        need: 所需权限等级 view/edit。

    Returns:
        校验通过的项目 ORM 实例。

    Raises:
        BizError: 10002 角色资产不存在；20003 无权限。
    """
    from app.models.asset import Asset

    asset = await session.get(Asset, character_id)
    if asset is None or asset.deleted_at is not None:
        raise BizError(ErrorCode.NOT_FOUND, "角色资产不存在")
    if asset.project_id is None:
        raise BizError(ErrorCode.PARAM_INVALID, "跨项目资产不支持该操作")
    return await require_project_acl(int(asset.project_id), user_id, session, need)


@router.patch("/voices/{character_id}/params")
async def patch_voice_params(
    character_id: int,
    payload: VoiceParamsRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """逐角色语速/语调调参（V4.0 P0 SF-F6-03 / AC-F6-07-1~3）。

    语速 0.80~1.50（步进 0.05）、语调偏移 ±3 档、情绪标签按场覆盖；参数按
    角色保存并保留最近 3 版。语速调慢导致台词超镜头时长时返回 ``qc_warning``
    并给出两条出路（调回语速 / 解锁分镜改时长），后期不自动改镜头。

    项目上下文由角色资产反查（ACL 由路由层保证）。

    Args:
        character_id: 角色资产 ID。
        payload: 调参请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {binding, qc_warning, affected_line_count}。

    Raises:
        BizError: 10001 语速/语调越界；10002 角色资产不存在；20003 无权限。
    """
    project = await _character_guard(character_id, user_id, session, "edit")
    data = await post_service.update_voice_params(
        session,
        project,
        character_id=character_id,
        params=payload.model_dump(),
        user_id=int(user_id),
    )
    return success_envelope(data, request_id)


@router.post("/voices/{character_id}/preview")
async def post_voice_preview(
    character_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """重新试听该角色并展示受影响台词数（AC-F6-07-2）。

    仅该角色台词重合成，其余角色复用；确认后写入参数快照供全剧 TTS 追溯。

    Args:
        character_id: 角色资产 ID。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {binding, preview_media_url, affected_line_count}。

    Raises:
        BizError: 10002 角色资产不存在；80001 音色服务不可用；20003 无权限。
    """
    project = await _character_guard(character_id, user_id, session, "edit")
    data = await post_service.retry_voice_preview(
        session, project, character_id=character_id
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/post/subtitle-lines")
async def get_subtitle_lines(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    cut_type: Annotated[str, Query(description="preview / master")] = "preview",
) -> dict[str, Any]:
    """查询逐条字幕（供字幕面板逐条编辑）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        cut_type: 版本类型。

    Returns:
        成功包络，data 为 {timeline_id, items, total, max_chars}。

    Raises:
        BizError: 10002 时间线不存在。
    """
    data = await post_service.list_subtitle_lines(session, project, cut_type)
    return success_envelope(data, request_id)


async def _subtitle_line_guard(
    line_id: int, user_id: str, session: AsyncSession, need: str
) -> Project:
    """字幕行 ACL 守卫：加载字幕行并按归属项目校验权限。

    Args:
        line_id: 字幕行 ID。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        need: 所需权限等级 view/edit。

    Returns:
        校验通过的项目 ORM 实例。

    Raises:
        BizError: 10002 字幕行不存在；20003 无权限。
    """
    from app.models.post import SubtitleLine

    line = await session.get(SubtitleLine, line_id)
    if line is None:
        raise BizError(ErrorCode.NOT_FOUND, "字幕行不存在")
    return await require_project_acl(int(line.project_id), user_id, session, need)


@router.patch("/subtitles/{line_id}")
async def patch_subtitle(
    line_id: int,
    payload: SubtitleLinePatchRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """逐条编辑字幕（改文本 / 出入点微调，AC-F6-08-1~2）。

    出入点调整导致音画同步误差 > 100ms 时拒绝保存（70001）。

    项目上下文由字幕行反查（ACL 由路由层保证）。

    Args:
        line_id: 字幕行 ID。
        payload: 编辑请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {line, sync_error_ms}。

    Raises:
        BizError: 10001 无可更新字段/区间非法；10002 字幕行不存在；
            70001 音画同步超差；20003 无权限。
    """
    project = await _subtitle_line_guard(line_id, user_id, session, "edit")
    data = await post_service.patch_subtitle_line(
        session,
        project,
        line_id=line_id,
        text=payload.text,
        start_ms=payload.start_ms,
        end_ms=payload.end_ms,
        user_id=int(user_id),
    )
    return success_envelope(data, request_id)


@router.post("/subtitles/batch-style")
async def post_subtitle_batch_style(
    payload: SubtitleBatchStyleRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    project_id: Annotated[
        int | None, Query(description="项目 ID（样式为全局样式时必填）")
    ] = None,
) -> dict[str, Any]:
    """批量修改字幕样式（全片/按集/按说话角色，AC-F6-08-3）。

    项目上下文优先由样式归属反查；样式为全局样式时须显式传 ``project_id``。

    Args:
        payload: 批量样式请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        project_id: 项目 ID（样式为全局样式时必填）。

    Returns:
        成功包络，data 为 {timeline_id, style_id, applied, out_of_safe_area}。

    Raises:
        BizError: 10001 范围非法/缺 speaker/无法确定项目；10002 样式或时间线不存在；
            20003 无权限。
    """
    from app.models.post import SubtitleStyle

    style = await session.get(SubtitleStyle, payload.style_id)
    if style is None:
        raise BizError(ErrorCode.NOT_FOUND, "字幕样式不存在")
    target_project_id = int(style.project_id) if style.project_id else project_id
    if target_project_id is None:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "全局字幕样式需显式指定 project_id",
            error_detail="project_id_required",
        )
    project = await require_project_acl(target_project_id, user_id, session, "edit")
    data = await post_service.batch_style_subtitles(
        session,
        project,
        style_id=payload.style_id,
        scope=payload.scope,
        speaker=payload.speaker,
        user_id=int(user_id),
    )
    return success_envelope(data, request_id)


async def _timeline_guard(
    timeline_id: int, user_id: str, session: AsyncSession, need: str
) -> Project:
    """时间线 ACL 守卫：加载时间线并按归属项目校验权限。

    Args:
        timeline_id: 时间线 ID。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        need: 所需权限等级 view/edit。

    Returns:
        校验通过的项目 ORM 实例。

    Raises:
        BizError: 10002 时间线不存在；20003 无权限。
    """
    timeline = await session.get(Timeline, timeline_id)
    if timeline is None:
        raise BizError(ErrorCode.NOT_FOUND, "时间线不存在")
    return await require_project_acl(int(timeline.project_id), user_id, session, need)


@router.post("/timeline/{timeline_id}/bgm/replace")
async def post_timeline_bgm_replace(
    timeline_id: int,
    payload: BgmReplaceRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """BGM 手动替换（含授权范围校验与临期提示，AC-F6-09-1~2）。

    试听免费；替换后自动重新卡点与侧链闪避；``platform_only`` 曲目在分发前
    会被拦截（``distribution_blocked=True``）。

    项目上下文由时间线反查（ACL 由路由层保证）。

    Args:
        timeline_id: 时间线 ID。
        payload: 替换请求体。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {timeline_id, bgm, license_scope, license_warning,
        distribution_blocked}。

    Raises:
        BizError: 10002 时间线/曲目不存在；20003 无权限。
    """
    project = await _timeline_guard(timeline_id, user_id, session, "edit")
    data = await post_service.replace_bgm(
        session,
        project,
        timeline_id=timeline_id,
        bgm_track_id=payload.bgm_track_id,
        volume_db=payload.volume_db,
        user_id=int(user_id),
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/post/mix-profile")
async def get_mix_profile(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    cut_type: Annotated[str, Query(description="preview / master")] = "preview",
) -> dict[str, Any]:
    """查询混音画像（响度目标 -14 LUFS + 逐轨音量）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        cut_type: 版本类型。

    Returns:
        成功包络，data 为 {profile}。

    Raises:
        BizError: 10002 时间线不存在。
    """
    data = await post_service.get_mix_profile(session, project, cut_type)
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/post/mix-profile")
async def put_mix_profile(
    payload: MixProfileRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """手动微调混音音量并统一全剧响度（AC-F6-10-1~2）。

    逐轨音量相对自动混音值 ±6dB；超 0dBFS 标红；响度标准化默认开启
    （-14 LUFS / 真峰值 ≤ -1 dBTP），手动调整在标准化之后应用。

    Args:
        payload: 混音请求体。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 {profile, clipped_tracks}。

    Raises:
        BizError: 10001 音量/响度越界；10002 时间线不存在；20003 无权限。
    """
    data = await post_service.update_mix_profile(
        session,
        project,
        track_volumes=payload.track_volumes,
        track_muted=payload.track_muted,
        loudness_target=payload.loudness_target,
        normalize_enabled=payload.normalize_enabled,
        user_id=int(user_id),
    )
    return success_envelope(data, request_id)
"""F9 配音轨路由模块（V6.0 增量）。

提供角色样音试听、音色参数确认/查询、整集逐句配音、配音轨详情与气口
调整、单句重配、时长漂移处理、关口 V 审批与配音报告共 10 个端点。
所有响应统一 ``success_envelope`` 包络；写操作经项目级 ACL（edit+）。

路径前缀约定：``/projects/{project_id}/...``，端点自带完整路径，
由 ``app/api/__init__.py`` 聚合引入。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    get_current_user,
    get_db,
    get_request_id,
    project_guard,
)
from app.core.errors import success_envelope
from app.models.project import Project
from app.schemas.v6 import (
    VoiceClipRegenerateRequest,
    VoiceDriftDecisionRequest,
    VoiceGateDecisionRequest,
    VoiceGenerateRequest,
    VoiceParamConfirmRequest,
    VoiceSampleRequest,
    VoiceTrackUpdateRequest,
)
from app.services import voice_service

#: 配音轨路由
router = APIRouter(tags=["voice"])


@router.post("/projects/{project_id}/characters/{character_asset_id}/voice/sample")
async def generate_voice_sample(
    character_asset_id: int,
    payload: VoiceSampleRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """生成角色样音供试听（≤30 秒，不落时间线）。

    Args:
        character_asset_id: 角色资产 ID。
        payload: 试听文本、语速与音调。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{audio_key, audio_url, duration_ms, align_degraded,
        words}``。
    """
    data = await voice_service.generate_sample(
        session,
        project.id,
        user_id,
        character_asset_id,
        payload.text,
        payload.rate,
        payload.pitch,
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/characters/{character_asset_id}/voice-params")
async def get_voice_params(
    character_asset_id: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询角色当前生效的配音参数。

    Args:
        character_asset_id: 角色资产 ID。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为参数对象或 None。
    """
    param = await voice_service.active_params(session, project.id, character_asset_id)
    data = (
        {
            "id": param.id,
            "character_asset_id": param.character_asset_id,
            "voice_id": param.voice_id,
            "rate": float(param.rate),
            "pitch": param.pitch,
            "emotion_default": param.emotion_default,
            "sample_audio_key": param.sample_audio_key,
            "sample_duration_ms": param.sample_duration_ms,
            "status": param.status,
        }
        if param is not None
        else None
    )
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/characters/{character_asset_id}/voice-params")
async def confirm_voice_params(
    character_asset_id: int,
    payload: VoiceParamConfirmRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """确认角色音色参数（旧版本置 superseded，新版本生效）。

    Args:
        character_asset_id: 角色资产 ID。
        payload: 音色、语速、音调与默认情绪。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为新的参数版本。
    """
    data = await voice_service.confirm_params(
        session,
        project.id,
        user_id,
        character_asset_id,
        payload.voice_id,
        payload.rate,
        payload.pitch,
        payload.emotion_default,
        payload.sample_audio_key,
        payload.sample_duration_ms,
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/episodes/{ep_no}/voice/track")
async def get_voice_track(
    ep_no: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询整集配音轨详情（逐句片段、状态与对齐信息）。

    Args:
        ep_no: 集号。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为轨道详情（含 clips 与统计）。
    """
    data = await voice_service.track_detail(session, project.id, ep_no)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/voice/generate")
async def generate_voice_track(
    ep_no: int,
    payload: VoiceGenerateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """逐句生成整集（或指定子集）配音并同步台词实测时长。

    Args:
        ep_no: 集号。
        payload: 台词 ID 子集与强制重配开关。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为生成统计与轨道摘要。
    """
    data = await voice_service.generate_track(
        session, project.id, user_id, ep_no, payload.line_ids, payload.force
    )
    return success_envelope(data, request_id)


@router.patch("/projects/{project_id}/episodes/{ep_no}/voice/track")
async def update_voice_track(
    ep_no: int,
    payload: VoiceTrackUpdateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """调整配音轨气口或项目制作流程模式。

    Args:
        ep_no: 集号。
        payload: 气口毫秒与/或流程模式。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的轨道详情。
    """
    data = await voice_service.update_track(
        session, project.id, ep_no, payload.gap_ms, payload.pipeline_mode, user_id
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/voice/clips/{clip_id}/regenerate")
async def regenerate_voice_clip(
    clip_id: int,
    payload: VoiceClipRegenerateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """单句重配并记录时长漂移分级。

    Args:
        clip_id: 配音片段 ID。
        payload: 临时语速、音调与重配原因。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为片段与漂移记录。
    """
    data = await voice_service.regenerate_clip(
        session,
        project.id,
        user_id,
        clip_id,
        payload.rate,
        payload.pitch,
        payload.reason,
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/voice/drifts/{drift_id}/decide")
async def decide_voice_drift(
    drift_id: int,
    payload: VoiceDriftDecisionRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """处理音频时长漂移（吸收 / 重算镜头 / 忽略）。

    Args:
        drift_id: 漂移记录 ID。
        payload: 动作（apply/dismiss）与可选级别。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为处理结果与受影响镜头列表。
    """
    data = await voice_service.decide_drift(
        session, project.id, user_id, drift_id, payload.action, payload.level
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/voice/approve")
async def approve_voice_track(
    ep_no: int,
    payload: VoiceGateDecisionRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """关口 V 审批：整集全部完成才可通过，通过后轨道锁定。

    Args:
        ep_no: 集号。
        payload: 审批结论与备注。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为审批后的轨道状态。
    """
    data = await voice_service.approve_track(
        session, project.id, user_id, ep_no, payload.decision, payload.comment
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/episodes/{ep_no}/voice/report")
async def get_voice_report(
    ep_no: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询配音完成度与时长偏差报告。

    Args:
        ep_no: 集号。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为报告（句数、完成率、对齐降级与漂移统计）。
    """
    data = await voice_service.build_report(session, project.id, ep_no)
    return success_envelope(data, request_id)

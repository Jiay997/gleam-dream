"""F3.14 照片合成角色路由模块（V6.3 增量）。

提供照片登记（四道闸）、批次列表/详情、照片移除、身份档创建与列表、
M1 合成发起、合成列表与确认落资产共 9 个端点。所有响应统一
``success_envelope`` 包络；写操作经项目级 ACL（edit+）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
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
    CompositionCreateRequest,
    IdentityProfileRequest,
    PhotoUploadRequest,
)
from app.services import photo_service

#: 照片合成角色路由
router = APIRouter(tags=["photo-characters"])


@router.post("/projects/{project_id}/photo-batches/uploads")
async def stage_photo_upload(
    payload: PhotoUploadRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """登记照片并串行执行静态/内容/合规/去重四道闸。

    Args:
        payload: 照片元数据、用途声明与模型分析结果。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{photo, batch}``（批次含可用数与阻断状态）。
    """
    data = await photo_service.stage_upload(
        session, project.id, int(user_id), payload
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/photo-batches")
async def list_photo_uploads(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    batch_key: Annotated[str | None, Query(max_length=32, description="批次键")] = None,
) -> dict[str, Any]:
    """列出项目照片（可按批次过滤）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        batch_key: 可选批次键。

    Returns:
        成功包络，data 为 ``{items, total, batches}``。
    """
    data = await photo_service.list_uploads(session, project.id, batch_key)
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/photo-batches/{batch_key}")
async def get_photo_batch(
    batch_key: str,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询批次照片可用数、拒绝原因与阻断状态。

    Args:
        batch_key: 批次键。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为批次统计与照片明细。
    """
    data = await photo_service.batch_status(session, project.id, batch_key)
    return success_envelope(data, request_id)


@router.delete("/projects/{project_id}/photo-uploads/{photo_id}")
async def remove_photo_upload(
    photo_id: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """移除照片（状态标记，保留审计痕迹）。

    Args:
        photo_id: 照片 ID。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{id, status}``。
    """
    data = await photo_service.remove_upload(session, project.id, photo_id)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/identity-profiles")
async def create_identity_profile(
    payload: IdentityProfileRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """创建身份档（主图缺省取质量分最高者）。

    Args:
        payload: 批次键、名称与可选主图/照片集。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为身份档。
    """
    data = await photo_service.create_profile(
        session, project.id, int(user_id), payload
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/identity-profiles")
async def list_identity_profiles(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """列出项目身份档。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    data = await photo_service.list_profiles(session, project.id)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/character-compositions")
async def create_character_composition(
    payload: CompositionCreateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """发起 M1 照片合成角色（含自检与人脸相似度判定）。

    Args:
        payload: 身份档、画风预设与风格化强度。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为合成任务（含预览与自检明细）。
    """
    data = await photo_service.create_composition(
        session, project.id, int(user_id), payload
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/character-compositions")
async def list_character_compositions(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """列出项目合成任务。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    data = await photo_service.list_compositions(session, project.id)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/character-compositions/{composition_id}/confirm")
async def confirm_character_composition(
    composition_id: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    name: Annotated[str | None, Query(max_length=128, description="角色资产名称")] = None,
) -> dict[str, Any]:
    """确认合成结果并落角色资产（待审核状态）。

    Args:
        composition_id: 合成任务 ID。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        name: 可选角色资产名称。

    Returns:
        成功包络，data 为 ``{composition, asset_id}``。
    """
    data = await photo_service.confirm_composition(
        session, project.id, int(user_id), composition_id, name
    )
    return success_envelope(data, request_id)

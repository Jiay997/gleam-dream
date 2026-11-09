"""F11 参数中心路由模块（V6.2 增量）。

提供生效参数读取（含定义分组）、参数保存、版本列表、回滚、恢复默认、
审计查询、重评估与采纳共 8 个端点。所有响应统一 ``success_envelope``
包络；写操作经项目级 ACL（edit+），读操作需 view+。
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
    ParamAdoptRequest,
    ParamReassessRequest,
    ParamRestoreRequest,
    ParamRollbackRequest,
    ParamUpdateRequest,
)
from app.services import param_service

#: 参数中心路由
router = APIRouter(tags=["params"])


@router.get("/projects/{project_id}/params")
async def get_effective_params(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    ep_no: Annotated[int | None, Query(ge=1, description="集号（可选，集级覆盖）")] = None,
    include_defs: Annotated[bool, Query(description="是否附带参数定义与分组")] = True,
) -> dict[str, Any]:
    """读取项目（及可选集级）生效参数与定义分组。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        ep_no: 可选集号，用于叠加集级覆盖。
        include_defs: 是否附带定义与分组。

    Returns:
        成功包络，data 为 ``{values, groups, definitions?, config_version,
        scope}``。
    """
    await param_service.ensure_seed(session)
    data = await param_service.effective_config(session, project.id, ep_no, include_defs)
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/params")
async def save_project_params(
    payload: ParamUpdateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """保存参数变更（危险参数须提供 ≥10 字理由），生成版本与审计。

    Args:
        payload: 变更键值、理由、作用域。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{version, changed, ignored, warnings}``。
    """
    await param_service.ensure_seed(session)
    data = await param_service.save_params(
        session,
        project.id,
        user_id,
        payload.changes,
        payload.reason,
        payload.scope,
        payload.scope_id,
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/params/versions")
async def list_param_versions(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    limit: Annotated[int, Query(ge=1, le=100, description="返回条数")] = 50,
) -> dict[str, Any]:
    """查询配置版本列表（倒序，历史不可变）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        limit: 返回条数上限。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await param_service.list_versions(session, project.id, limit)
    return success_envelope({"items": items, "total": len(items)}, request_id)


@router.post("/projects/{project_id}/params/rollback")
async def rollback_params(
    payload: ParamRollbackRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """回滚到历史配置版本（以新版本形式落库）。

    Args:
        payload: 目标版本号与理由。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为新版本信息。
    """
    await param_service.ensure_seed(session)
    data = await param_service.rollback(
        session, project.id, user_id, payload.version_no, payload.reason
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/params/restore")
async def restore_default_params(
    payload: ParamRestoreRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """恢复默认参数（删除项目覆盖并生成新版本）。

    Args:
        payload: 参数键列表（空表示全部）与理由。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为恢复结果与新版本号。
    """
    await param_service.ensure_seed(session)
    data = await param_service.restore_defaults(
        session, project.id, user_id, payload.param_keys, payload.reason
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/params/audits")
async def list_param_audits(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    limit: Annotated[int, Query(ge=1, le=200, description="返回条数")] = 100,
) -> dict[str, Any]:
    """查询参数变更审计（倒序）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        limit: 返回条数上限。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await param_service.list_audits(session, project.id, limit)
    return success_envelope({"items": items, "total": len(items)}, request_id)


@router.post("/projects/{project_id}/params/reassess")
async def reassess_with_params(
    payload: ParamReassessRequest,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """按当前参数重评估历史校验裁决（只读对照，不自动改写）。

    Args:
        payload: 限定集号与对象类型。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为对照结果（含差异条数与明细）。
    """
    data = await param_service.reassess(session, project.id, payload.ep_no, payload.target)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/params/reassess/adopt")
async def adopt_reassess_result(
    payload: ParamAdoptRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """采纳重评估结论（写回阈值与裁决，标注人工覆盖）。

    Args:
        payload: 待采纳的校验结果 ID 列表。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为采纳数量与明细。
    """
    data = await param_service.adopt_reassess(session, project.id, payload.check_ids)
    return success_envelope(data, request_id)

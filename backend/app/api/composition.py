"""F10 合成编排路由模块（V6.1 增量）。

提供编排图同步/详情读取、双视图切换、节点与边增删改、结构守卫校验、
确定性编译（``plan_hash`` 幂等）、主路径回写分镜与历史计划查询共 12 个
端点。所有响应统一 ``success_envelope`` 包络；写操作经项目级 ACL（edit+）。
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
    ApplyOrderRequest,
    CompileRequest,
    ComposeFromPlanRequest,
    EdgeCreateRequest,
    NodeCreateRequest,
    NodeUpdateRequest,
    SwitchViewRequest,
)
from app.services import composition_service

#: 合成编排路由
router = APIRouter(tags=["composition"])


@router.post("/projects/{project_id}/episodes/{ep_no}/composition/sync")
async def sync_composition_graph(
    ep_no: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    force: Annotated[bool, Query(description="已有图时是否强制重建")] = False,
) -> dict[str, Any]:
    """从分镜板镜头序列生成/重建编排图（线性主路径，cut 转场）。

    Args:
        ep_no: 集号。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        force: 已有图时是否强制重建（旧图归档）。

    Returns:
        成功包络，data 为图摘要（版本、节点数与哈希）。
    """
    data = await composition_service.sync_from_storyboard(
        session, project.id, user_id, ep_no, force
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/episodes/{ep_no}/composition")
async def get_composition_graph(
    ep_no: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    view: Annotated[str, Query(pattern="^(track|node)$", description="视图")] = "track",
) -> dict[str, Any]:
    """获取编排图双视图详情（轨道/节点），并附结构守卫校验结果。

    Args:
        ep_no: 集号。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        view: track（轨道）或 node（节点）。

    Returns:
        成功包络，data 为图详情（nodes/edges/layout/validation/total_duration_ms）。
    """
    data = await composition_service.graph_detail(session, project.id, ep_no, view)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/composition/view")
async def switch_composition_view(
    ep_no: int,
    payload: SwitchViewRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """切换编排视图并记录无损性（节点→轨道可能折叠结构节点）。

    Args:
        ep_no: 集号。
        payload: 目标视图。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为目标视图详情与 ``lossless`` 标记。
    """
    data = await composition_service.switch_view(
        session, project.id, user_id, ep_no, payload.to_view
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/composition/nodes")
async def create_composition_node(
    ep_no: int,
    payload: NodeCreateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """新增编排节点（渲染锁定期拒绝修改）。

    Args:
        ep_no: 集号。
        payload: 节点字段。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为新节点。
    """
    data = await composition_service.create_node(
        session, project.id, ep_no, payload.model_dump()
    )
    return success_envelope(data, request_id)


@router.patch("/projects/{project_id}/composition/nodes/{node_id}")
async def update_composition_node(
    node_id: int,
    payload: NodeUpdateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """部分更新编排节点（参数/标签/坐标/排序/状态）。

    Args:
        node_id: 节点 ID。
        payload: 待更新字段。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的节点。
    """
    data = await composition_service.update_node(
        session, project.id, node_id, payload.model_dump(exclude_none=True)
    )
    return success_envelope(data, request_id)


@router.delete("/projects/{project_id}/composition/nodes/{node_id}")
async def delete_composition_node(
    node_id: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """删除编排节点并级联删除其关联边。

    Args:
        node_id: 节点 ID。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{deleted_node, deleted_edges}``。
    """
    data = await composition_service.delete_node(session, project.id, node_id)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/composition/edges")
async def create_composition_edge(
    ep_no: int,
    payload: EdgeCreateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """新增编排边（转场类型与时长）。

    Args:
        ep_no: 集号。
        payload: 边字段。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为新边。
    """
    data = await composition_service.create_edge(
        session, project.id, ep_no, payload.model_dump()
    )
    return success_envelope(data, request_id)


@router.delete("/projects/{project_id}/composition/edges/{edge_id}")
async def delete_composition_edge(
    edge_id: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """删除编排边。

    Args:
        edge_id: 边 ID。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{deleted: true, edge_id}``。
    """
    data = await composition_service.delete_edge(session, project.id, edge_id)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/composition/compile")
async def compile_composition_plan(
    ep_no: int,
    payload: CompileRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """编译编排图为不可变渲染计划（``plan_hash`` 幂等）。

    Args:
        ep_no: 集号。
        payload: 试渲与强制编译开关。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为计划（含 segments/resources/plan_hash/reused）。
    """
    data = await composition_service.compile_plan(
        session, project.id, user_id, ep_no, payload.preview, payload.force
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/composition/apply-order")
async def apply_composition_order(
    ep_no: int,
    payload: ApplyOrderRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """把主路径节点顺序回写分镜板（需确认影响镜头）。

    Args:
        ep_no: 集号。
        payload: 是否确认接受影响。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{requires_confirmation, impacts}`` 或回写结果。
    """
    data = await composition_service.apply_main_order(
        session, project.id, ep_no, payload.confirm_impacts
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/composition/compose")
async def compose_from_composition_plan(
    ep_no: int,
    payload: ComposeFromPlanRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """按编排图编译计划发起合成（V6.12 合成入口迁移）。

    Args:
        ep_no: 集号。
        payload: 可选期望计划哈希（与当前图不一致时拒绝）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{episode_no, plan_hash, reused, compose}``。
    """
    data = await composition_service.compose_from_plan(
        session, project, int(user_id), ep_no, payload.plan_hash
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/episodes/{ep_no}/composition/plans")
async def list_composition_plans(
    ep_no: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    limit: Annotated[int, Query(ge=1, le=100, description="返回条数")] = 20,
) -> dict[str, Any]:
    """查询某集历史渲染计划。

    Args:
        ep_no: 集号。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        limit: 返回条数上限。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await composition_service.list_plans(session, project.id, ep_no, limit)
    return success_envelope({"items": items, "total": len(items)}, request_id)

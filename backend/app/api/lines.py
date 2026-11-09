"""F2.12 台词清单与 F4.13 台词绑定路由模块（V6.0 增量）。

提供台词清单查询、台词编辑/拆分/时长预估、镜头↔台词绑定读写、镜头与
集级音频驱动时长重算、集级时长报告共 9 个端点。所有响应统一
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
    LineEstimateRequest,
    LineSplitRequest,
    LineUpdateRequest,
    ShotDurationRecalcRequest,
    ShotLineBindingsRequest,
)
from app.services import line_service

#: 台词与绑定路由
router = APIRouter(tags=["lines"])


@router.get("/projects/{project_id}/episodes/{ep_no}/lines")
async def list_script_lines(
    ep_no: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    scene_no: Annotated[int | None, Query(ge=0, description="场号过滤")] = None,
    include_deleted: Annotated[bool, Query(description="是否包含已删除行")] = False,
) -> dict[str, Any]:
    """查询台词清单（含预估/实测时长与配音状态）。

    Args:
        ep_no: 集号。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        scene_no: 可选场号过滤。
        include_deleted: 是否包含已删除行。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await line_service.list_lines(
        session, project.id, ep_no, scene_no, include_deleted
    )
    return success_envelope({"items": items, "total": len(items)}, request_id)


@router.patch("/projects/{project_id}/lines/{line_id}")
async def update_script_line(
    line_id: int,
    payload: LineUpdateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """编辑台词文本/说话人/角色/情绪，文本变化时重算预估时长。

    Args:
        line_id: 台词 ID。
        payload: 待更新字段（仅传入非 None 项生效）。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的台词。
    """
    data = await line_service.update_line(
        session,
        project.id,
        line_id,
        payload.content,
        payload.speaker_name,
        payload.character_asset_id,
        payload.emotion_tag,
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/lines/{line_id}/split")
async def split_script_line(
    line_id: int,
    payload: LineSplitRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """拆分超长台词为两条并顺延后续行号。

    Args:
        line_id: 台词 ID。
        payload: 拆分位置或自定义后半段文本。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{first, second}`` 两条台词。
    """
    data = await line_service.split_line(
        session, project.id, line_id, payload.position, payload.content
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/lines/{line_id}/estimate")
async def estimate_script_line(
    line_id: int,
    payload: LineEstimateRequest,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """预估单句朗读时长（支持未保存文本试算）。

    Args:
        line_id: 台词 ID。
        payload: 待估文本与语速。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{line_id, est_ms, actual_ms}``。
    """
    data = await line_service.estimate(
        session, project.id, line_id, payload.content, payload.rate
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/shots/{shot_id}/line-bindings")
async def get_shot_line_bindings(
    shot_id: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询镜头绑定的台词与未绑定台词候选。

    Args:
        shot_id: 镜头 ID。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{bindings, unbound_lines, duration_ms,
        line_coverage_pct}``。
    """
    data = await line_service.get_shot_bindings(session, project.id, shot_id)
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/shots/{shot_id}/line-bindings")
async def replace_shot_line_bindings(
    shot_id: int,
    payload: ShotLineBindingsRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """全量重建镜头台词绑定并即时重算镜头时长。

    Args:
        shot_id: 镜头 ID。
        payload: 绑定项数组（顺序即镜头内顺序）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的绑定与时长信息。
    """
    data = await line_service.replace_shot_bindings(
        session,
        project.id,
        user_id,
        shot_id,
        [item.model_dump() for item in payload.bindings],
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/shots/{shot_id}/recalculate-duration")
async def recalculate_shot_duration(
    shot_id: int,
    payload: ShotDurationRecalcRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """按绑定台词音频时长重算单个镜头时长。

    Args:
        shot_id: 镜头 ID。
        payload: 是否覆盖手工时长与单镜头上限。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{shot_id, duration_ms, long_line,
        line_coverage_pct}``。
    """
    changed = await line_service.recalculate_shot_duration(
        session, project.id, shot_id, payload.force_manual
    )
    data = await line_service.get_shot_bindings(session, project.id, shot_id)
    return success_envelope({"changed": changed, **data}, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/recalculate-duration")
async def recalculate_episode_duration(
    ep_no: int,
    payload: ShotDurationRecalcRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """批量重算某集全部有绑定镜头的时长。

    Args:
        ep_no: 集号。
        payload: 是否覆盖手工时长与单镜头上限。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为重算统计与集级时长偏差。
    """
    data = await line_service.recalc_episode(
        session, project.id, ep_no, payload.force_manual, payload.max_shot_ms
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/episodes/{ep_no}/duration-report")
async def get_episode_duration_report(
    ep_no: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询集级时长/台词覆盖率报告。

    Args:
        ep_no: 集号。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为时长报告（含超长句与未绑定台词提示）。
    """
    data = await line_service.episode_duration_report(session, project.id, ep_no)
    return success_envelope(data, request_id)

"""5A Story Bible / 连续性校验 / 结构化反馈路由模块（V6.3 增量）。

提供 Story Bible 读取与角色/场景/道具写入、连续性事实重建/查询/人工修正、
L1+L2 校验、连续性报告、F2.13 逻辑规则检查、豁免管理、结构化反馈与反例
库共 16 个端点。所有响应统一 ``success_envelope`` 包络；写操作经项目级
ACL（edit+），读操作需 view+。
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
    ContinuityFactUpsertRequest,
    ContinuityRunRequest,
    FactRebuildRequest,
    HumanFeedbackRequest,
    LogicCheckRequest,
    LogicWaiverRequest,
    NegativeExampleRequest,
    StoryBibleCharacterUpsertRequest,
    StoryBiblePropUpsertRequest,
    StoryBibleSceneUpsertRequest,
)
from app.services import continuity_service

#: 连续性与 Story Bible 路由
router = APIRouter(tags=["continuity"])


@router.get("/projects/{project_id}/story-bible")
async def get_story_bible(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    ep_no: Annotated[int | None, Query(ge=1, description="集号（场景筛选）")] = None,
) -> dict[str, Any]:
    """读取 Story Bible 全量设定与完整度检查结果。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        ep_no: 可选集号，用于场景筛选。

    Returns:
        成功包络，data 为 ``{characters, scenes, props, completeness, warning}``。
    """
    data = await continuity_service.story_bible(session, project.id, ep_no)
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/story-bible/characters")
async def upsert_story_bible_character(
    payload: StoryBibleCharacterUpsertRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """写入/更新 Story Bible 角色档案。

    Args:
        payload: 角色字段（char_key 为幂等键）。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为角色记录与最新完整度。
    """
    data = await continuity_service.upsert_character(
        session, project.id, payload.model_dump()
    )
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/episodes/{ep_no}/scenes/{scene_no}/story-bible")
async def upsert_story_bible_scene(
    ep_no: int,
    scene_no: int,
    payload: StoryBibleSceneUpsertRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """写入/更新 Story Bible 场景档案（空间布局、光线基线、闪回标记）。

    Args:
        ep_no: 集号。
        scene_no: 场号。
        payload: 场景字段。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为场景记录与最新完整度。
    """
    data = await continuity_service.upsert_scene(
        session, project.id, ep_no, scene_no, payload.model_dump()
    )
    return success_envelope(data, request_id)


@router.put("/projects/{project_id}/story-bible/props")
async def upsert_story_bible_prop(
    payload: StoryBiblePropUpsertRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """写入/更新 Story Bible 道具档案（状态链与持有人映射）。

    Args:
        payload: 道具字段（prop_key 为幂等键）。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为道具记录与最新完整度。
    """
    data = await continuity_service.upsert_prop(
        session, project.id, payload.model_dump()
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/continuity/facts/rebuild")
async def rebuild_continuity_facts(
    payload: FactRebuildRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """从剧本重建场次连续性事实（版本递增，可按人工来源保护）。

    Args:
        payload: 集号与是否覆盖人工编辑。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为重建统计与版本信息。
    """
    data = await continuity_service.rebuild_facts(
        session, project.id, payload.ep_no, payload.overwrite
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/continuity/facts")
async def list_continuity_facts(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    ep_no: Annotated[int | None, Query(ge=1, description="集号")] = None,
    latest_only: Annotated[bool, Query(description="仅返回每场最新版本")] = True,
) -> dict[str, Any]:
    """查询场次连续性事实。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        ep_no: 可选集号。
        latest_only: 是否仅返回每场最新版本。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await continuity_service.list_facts(session, project.id, ep_no, latest_only)
    return success_envelope({"items": items, "total": len(items)}, request_id)


@router.put("/projects/{project_id}/episodes/{ep_no}/scenes/{scene_no}/continuity-facts")
async def upsert_continuity_fact(
    ep_no: int,
    scene_no: int,
    payload: ContinuityFactUpsertRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """人工写入/修正场次连续性事实（版本号递增）。

    Args:
        ep_no: 集号。
        scene_no: 场号。
        payload: 事实主体与产出方式。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为写入后的事实版本。
    """
    data = await continuity_service.upsert_fact(
        session, project.id, ep_no, scene_no, payload.facts, payload.source
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/continuity/checks")
async def run_continuity_checks(
    payload: ContinuityRunRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """执行 L1 规则校验与 L2 视觉连续性校验（缺检记 unavailable）。

    Args:
        payload: 集号、镜头子集与是否启用 Bible 比对。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{summary, checks, degraded, config_version}``。
    """
    data = await continuity_service.run_checks(
        session, project.id, payload.ep_no, payload.shot_ids, payload.use_bible
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/continuity/report")
async def get_continuity_report(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    ep_no: Annotated[int | None, Query(ge=1, description="集号")] = None,
) -> dict[str, Any]:
    """汇总连续性报告（按维度与层级）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        ep_no: 可选集号。

    Returns:
        成功包络，data 为维度汇总与最近校验明细。
    """
    data = await continuity_service.report(session, project.id, ep_no)
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/episodes/{ep_no}/logic-check")
async def run_logic_check(
    ep_no: int,
    payload: LogicCheckRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """执行 F2.13 逻辑/连续性规则检查（LR-1~LR-6，自动过滤豁免）。

    Args:
        ep_no: 集号。
        payload: 指定规则编号（空表示全部）。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{issues, counts, waived}``。
    """
    data = await continuity_service.logic_check(
        session, project.id, ep_no, payload.rule_codes
    )
    return success_envelope(data, request_id)


@router.post("/projects/{project_id}/logic-waivers")
async def create_logic_waiver(
    payload: LogicWaiverRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """创建逻辑规则豁免（须提供 ≥10 字理由并留痕）。

    Args:
        payload: 规则编号、定位引用与理由。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为豁免记录。
    """
    data = await continuity_service.create_waiver(
        session, project.id, user_id, payload.rule_code, payload.target_ref, payload.reason
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/logic-waivers")
async def list_logic_waivers(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """列出项目逻辑规则豁免。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await continuity_service.list_waivers(session, project.id)
    return success_envelope({"items": items, "total": len(items)}, request_id)


@router.post("/projects/{project_id}/feedback")
async def create_structured_feedback(
    payload: HumanFeedbackRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """写入结构化反馈并按维度路由修正策略（同维度 ≥3 次自动沉淀反例）。

    Args:
        payload: 对象、维度、期望与来源关口。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为反馈记录与路由结果。
    """
    data = await continuity_service.create_feedback(
        session, project.id, user_id, payload.model_dump()
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/feedback")
async def list_structured_feedback(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    target_type: Annotated[str | None, Query(description="对象类型")] = None,
    target_id: Annotated[int | None, Query(description="对象 ID")] = None,
) -> dict[str, Any]:
    """查询结构化反馈。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        target_type: 可选对象类型过滤。
        target_id: 可选对象 ID 过滤。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await continuity_service.list_feedback(
        session, project.id, target_type, target_id
    )
    return success_envelope({"items": items, "total": len(items)}, request_id)


@router.post("/projects/{project_id}/negative-examples")
async def create_negative_example(
    payload: NegativeExampleRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """写入反例库（错-改-对三元组）。

    Args:
        payload: 反例字段。
        project: ACL 校验通过的项目实例（需 edit+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为反例记录。
    """
    data = await continuity_service.create_negative_example(
        session, project.id, payload.model_dump()
    )
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/negative-examples")
async def list_negative_examples(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    dimension: Annotated[str | None, Query(description="维度过滤")] = None,
) -> dict[str, Any]:
    """查询反例库。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        dimension: 可选维度过滤。

    Returns:
        成功包络，data 为 ``{items, total}``。
    """
    items = await continuity_service.list_negative_examples(session, project.id, dimension)
    return success_envelope({"items": items, "total": len(items)}, request_id)

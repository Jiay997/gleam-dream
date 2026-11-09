"""编排路由模块（Task 6a）。

挂载点：主控在 api_router 上以 ``settings.API_V1_PREFIX``（/api/v1）
聚合，最终路径形如 ``POST /api/v1/orchestration/projects/1/script-generate``。

端点：
- POST /orchestration/projects/{project_id}/script-generate
  发起剧本生成（202 异步任务模型，进度经项目事件流推送）；
- POST /orchestration/approvals/{approval_id}/decide
  审批决策（approve/reject/edit，202，流程从 checkpoint 断点恢复）；
- GET  /orchestration/approvals
  审批单列表（待办/历史，全局 + 按项目，惰性过期）；
- GET  /orchestration/approvals/{approval_id}
  审批单详情（含审批台摘要）。

路由变量名：``router = APIRouter(prefix="/orchestration", tags=["orchestration"])``。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, require_project_acl
from app.core.errors import BizError, ErrorCode, success_envelope
from app.models.collab import ApprovalRequest
from app.models.iam import ProjectMember
from app.models.project import Project
from app.services import orchestration_service

logger = logging.getLogger("gleamdream.api.orchestration")

#: 编排子路由（主控聚合：include_router(orchestration.router)）
router = APIRouter(prefix="/orchestration", tags=["orchestration"])


# ---- 请求体模型 ----


class ScriptGenerateRequest(BaseModel):
    """剧本生成发起请求体。

    Attributes:
        idea: 一句话创意（可空，缺省取项目 synopsis）。
    """

    model_config = ConfigDict(extra="forbid")

    idea: str | None = Field(default=None, max_length=1000, description="一句话创意")


class ApprovalDecisionRequest(BaseModel):
    """审批决策请求体。

    Attributes:
        action: 决策 approve/reject/edit。
        feedback: 反馈原文（reject/edit 时建议填写，DR-4 留痕）。
        edited_payload: 人工编辑内容（edit 必填，结构为
            {episodes: [...]} 或集数组，元素同编剧输出契约）。
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject", "edit"] = Field(description="决策动作")
    feedback: str | None = Field(default=None, max_length=2000, description="反馈原文")
    edited_payload: dict[str, Any] | None = Field(default=None, description="人工编辑内容")


# ---- 工具 ----


def _approval_out(approval: ApprovalRequest, *, include_summary: bool = False) -> dict[str, Any]:
    """把审批单 ORM 序列化为响应 dict。

    Args:
        approval: 审批单实例。
        include_summary: 是否附带审批台摘要（详情接口用）。

    Returns:
        响应字典。
    """
    data: dict[str, Any] = {
        "id": int(approval.id),
        "project_id": int(approval.project_id),
        "approval_type": approval.approval_type,
        "target_type": approval.target_type,
        "target_id": approval.target_id,
        "status": approval.status,
        "decision": approval.decision,
        "feedback": approval.feedback,
        "requested_at": approval.requested_at.isoformat() if approval.requested_at else None,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "decided_by_user_id": approval.decided_by_user_id,
        "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        "has_edited_payload": approval.edited_payload is not None,
        "created_at": approval.created_at.isoformat() if approval.created_at else None,
    }
    if include_summary and approval.edited_payload is not None:
        data["edited_payload"] = approval.edited_payload
    return data


async def _expire_stale_approvals(session: AsyncSession) -> None:
    """惰性过期：把全部 pending 且已过 expires_at 的审批单置 expired。

    Args:
        session: 请求级数据库会话（存在变更时提交）。

    Raises:
        无（失败仅告警，不影响查询主流程）。
    """
    try:
        stale = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.status == "pending",
                    ApprovalRequest.expires_at < datetime.now(timezone.utc),
                )
            )
        ).scalars().all()
        for row in stale:
            row.status = "expired"
        if stale:
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("惰性过期审批单失败", exc_info=True)


# ---- 端点 ----


@router.post("/projects/{project_id}/script-generate")
async def post_script_generate(
    project_id: int,
    body: ScriptGenerateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """发起剧本生成（编剧子图异步执行）。

    执行模型：202 + 后台任务。进度经项目事件流（SSE ``progress``）
    推送；大纲/逐集/审核完成后挂起等待审批（``approval.created`` 事件）。

    Args:
        project_id: 项目 ID。
        body: 请求体（idea 可选）。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。

    Returns:
        202 成功包络，data 含受理信息与预估积分。

    Raises:
        BizError: 10002 项目不存在；20003 无权限；40001 并发冲突或
            状态不允许；30001 积分不足（冻结失败经 run.failed 兜底）。
    """
    project = await require_project_acl(project_id, user_id, session, "edit")
    # 2026-11 QA 修复：idea 为空且项目无 synopsis/genre 时，此前会带着
    # 「（用户未提供创意，请自行发挥）」照常受理，LLM 自行虚构题材并产出
    # 一整集剧本，浪费 50 积分且产出与用户意图无关。这里在入口显式拒绝。
    idea_text = str(body.idea or "").strip()
    project_context = " ".join(
        str(value or "").strip()
        for value in (getattr(project, "synopsis", None), getattr(project, "genre", None))
        if str(value or "").strip()
    )
    if not idea_text and not project_context:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "请填写一句话创意，或先为项目补充简介/题材后再生成剧本",
        )
    data = await orchestration_service.invoke_script_generation(
        session,
        project_id=int(project.id),
        user_id=int(user_id),
        idea=body.idea,
    )
    return JSONResponse(status_code=202, content=success_envelope(data))


@router.post("/approvals/{approval_id}/decide")
async def post_approval_decide(
    approval_id: int,
    body: ApprovalDecisionRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """审批决策：approve（结算+锁定+状态推进）/ reject（局部改写）/ edit（直改剧本）。

    权限：审批单所属项目的 edit+ 权限；越权 20003 并落审计日志。
    同一审批单不可重复决策（40001）；过期审批单（7 天）置 expired 后拒绝。

    Args:
        approval_id: 审批单 ID。
        body: 决策请求体。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。

    Returns:
        202 成功包络，data 含决策受理信息。

    Raises:
        BizError: 10002 审批单不存在；20003 无权限；40001 已处理/已过期/
            并发冲突；10001 参数非法。
    """
    approval = await session.get(ApprovalRequest, approval_id)
    if approval is None:
        raise BizError(ErrorCode.NOT_FOUND, "审批单不存在")
    project = await require_project_acl(int(approval.project_id), user_id, session, "edit")
    data = await orchestration_service.resume_from_approval(
        session,
        approval_id=approval_id,
        action=body.action,
        feedback=body.feedback,
        edited_payload=body.edited_payload,
        user_id=int(user_id),
    )
    _ = project  # ACL 已校验
    return JSONResponse(status_code=202, content=success_envelope(data))


@router.get("/approvals")
async def get_approvals(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    status: Annotated[str | None, Query(description="状态过滤，默认 pending")] = "pending",
    project_id: Annotated[int | None, Query(description="按项目过滤")] = None,
    approval_type: Annotated[
        str | None,
        Query(description="按关口类型过滤：script/asset/storyboard/final_cut"),
    ] = None,
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="页大小")] = 20,
) -> dict[str, Any]:
    """查询当前用户可见的审批单列表（全局 + 按项目 + 按关口类型）。

    可见范围：用户为 Owner 或项目成员的项目。status 默认 pending
    （待办）；传 "all" 查全部。查询前惰性把过期 pending 单置 expired。

    Args:
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        status: 状态过滤（pending/approved/rejected/edited/expired/all）。
        project_id: 项目过滤（须在可见范围内，否则返回空集）。
        approval_type: 关口类型过滤（script/asset/storyboard/final_cut）。
        page: 页码（≥1）。
        page_size: 页大小（1~100）。

    Returns:
        分页结构 {items, page, page_size, total}。
    """
    uid = int(user_id)
    await _expire_stale_approvals(session)

    # 可见项目集合：owner 或成员
    owned = (
        await session.execute(
            select(Project.id).where(Project.owner_user_id == uid, Project.deleted_at.is_(None))
        )
    ).scalars().all()
    joined = (
        await session.execute(select(ProjectMember.project_id).where(ProjectMember.user_id == uid))
    ).scalars().all()
    visible = set(owned) | set(joined)
    if project_id is not None and project_id not in visible:
        return {"items": [], "page": page, "page_size": page_size, "total": 0}

    stmt = select(ApprovalRequest).where(ApprovalRequest.project_id.in_(visible))
    count_stmt = select(ApprovalRequest.id).where(ApprovalRequest.project_id.in_(visible))
    # 修复：旧实现只校验 project_id 可见性，却没有把它加入查询条件，
    # 导致“按项目过滤”仍返回该用户其他项目的待办（实测 849 列表混入 844/845）。
    if project_id is not None:
        stmt = stmt.where(ApprovalRequest.project_id == int(project_id))
        count_stmt = count_stmt.where(ApprovalRequest.project_id == int(project_id))
    if status and status != "all":
        stmt = stmt.where(ApprovalRequest.status == status)
        count_stmt = count_stmt.where(ApprovalRequest.status == status)
    if approval_type:
        # 关口类型过滤（P17 等页面取单必须锚定类型，避免串关口）
        stmt = stmt.where(ApprovalRequest.approval_type == approval_type)
        count_stmt = count_stmt.where(ApprovalRequest.approval_type == approval_type)
    total = len((await session.execute(count_stmt)).all())
    rows = (
        await session.execute(
            stmt.order_by(ApprovalRequest.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    # 统一包络（契约 §8.1）：此前直接返回裸分页 dict，前端 http.ts 信封
    # 解包（isEnvelope 判定）会失效导致 items 取不到。
    return success_envelope(
        {
            "items": [_approval_out(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }
    )


@router.get("/approvals/{approval_id}")
async def get_approval_detail(
    approval_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """查询审批单详情（含审批台摘要所需的项目/剧本引用）。

    Args:
        approval_id: 审批单 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含审批单详情。

    Raises:
        BizError: 10002 审批单不存在；20003 无权限。
    """
    approval = await session.get(ApprovalRequest, approval_id)
    if approval is None:
        raise BizError(ErrorCode.NOT_FOUND, "审批单不存在")
    await require_project_acl(int(approval.project_id), user_id, session, "view")
    return success_envelope(_approval_out(approval, include_summary=True))

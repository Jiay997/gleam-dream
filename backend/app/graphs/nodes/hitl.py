"""HITL 人机协同节点模块（interrupt + approval_requests 双写）。

按 LangGraph 官方模式把审批关口拆为两个节点，规避 interrupt() 恢复
重放副作用的问题：

- :func:`create_approval`：确定性节点——惰性清理本项目过期审批单、
  scripting→script_review 流转、创建 pending 审批单（expires_at =
  now + 7 天）、发布 ``approval.created`` 与站内通知事件；**无
  interrupt，不依赖恢复重放语义**；
- :func:`wait_for_decision`：纯中断节点——仅调用 ``interrupt(payload)``
  挂起图执行并把恢复值写入 State（``decision``）；恢复重放时无任何
  副作用（审批单状态更新由 decide API 在恢复前完成）。

thread_id = str(project_id)，interrupt 状态持久化于 checkpoint 表，
服务重启后凭 pending 审批单 + checkpoint 即可断点续跑（E2E-01）。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from langgraph.types import interrupt
from sqlalchemy import select

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.infra.event_bus import event_bus
from app.models.collab import ApprovalRequest
from app.models.project import Project
from app.models.script import Script
from app.services.project_service import transition

from app.graphs.nodes.deterministic import APPROVAL_TTL_DAYS, utcnow

logger = logging.getLogger("gleamdream.graphs.hitl")


def _approval_summary(script: Script | None, project: Project) -> dict[str, Any]:
    """构造审批台展示摘要（前端渲染用）。

    Args:
        script: 当前剧本版本（可空）。
        project: 项目实例。

    Returns:
        摘要 dict（剧名/梗概/集数/各集概览）。
    """
    episode_briefs: list[dict[str, Any]] = []
    structure = (script.structure or {}) if script is not None else {}
    for ep in structure.get("episodes", []):
        episode_briefs.append(
            {
                "episode_no": ep.get("episode_no"),
                "title": ep.get("title"),
                "hook": ep.get("hook"),
                "summary": ep.get("summary"),
                "scene_count": len(ep.get("scenes", [])),
            }
        )
    return {
        "project_title": project.title,
        "script_title": (script.title if script is not None else None),
        "logline": (script.logline if script is not None else None),
        "total_episodes": (script.total_episodes if script is not None else 0),
        "version_no": (script.version_no if script is not None else None),
        "episodes": episode_briefs,
    }


async def _expire_stale_project_approvals(project_id: int) -> None:
    """惰性过期：把本项目 pending 且已过 expires_at 的审批单置 expired。

    Args:
        project_id: 项目 ID。

    Raises:
        无（失败仅告警，不阻断主流程）。
    """
    try:
        async with async_session_factory() as session:
            stale = (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.project_id == project_id,
                        ApprovalRequest.status == "pending",
                        ApprovalRequest.expires_at < utcnow(),
                    )
                )
            ).scalars().all()
            for row in stale:
                row.status = "expired"
            if stale:
                await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("惰性过期审批单失败 project_id=%s", project_id, exc_info=True)


async def create_approval(state: dict[str, Any]) -> dict[str, Any]:
    """审批单创建节点（确定性，双写之"写库"）。

    动作序列：
    1. 惰性把本项目过期 pending 单置 expired；
    2. 项目状态 scripting → script_review（edit 分支重入时已是
       script_review，跳过流转）；
    3. 创建 pending 审批单（approval_type=script，target=当前剧本版本，
       expires_at = now + 7 天）；
    4. 发布 ``approval.created`` 与 ``notification.approval_pending`` 事件。

    Args:
        state: 编排状态（需 project_id/user_id/script_id）。

    Returns:
        状态更新 {approval_id}。

    Raises:
        BizError: 10002 项目/剧本不存在；40001 状态不允许流转。
    """
    project_id = int(state["project_id"])
    user_id = int(state["user_id"])
    await _expire_stale_project_approvals(project_id)

    async with async_session_factory() as session:
        project = (
            await session.execute(
                select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
            )
        ).scalar_one_or_none()
        if project is None:
            raise BizError(ErrorCode.NOT_FOUND, "项目不存在")

        script_id = state.get("script_id")
        script = await session.get(Script, int(script_id)) if script_id else None

        # 状态流转：仅 scripting 进入 script_review（审批关口）
        if project.status == "scripting":
            await transition(
                project, "script_review", "剧本待人工确认", session=session, user_id=None
            )

        approval = ApprovalRequest(
            project_id=project_id,
            approval_type="script",
            target_type="script",
            target_id=int(script_id) if script_id else None,
            status="pending",
            requested_at=utcnow(),
            expires_at=utcnow() + timedelta(days=APPROVAL_TTL_DAYS),
        )
        session.add(approval)
        await session.commit()
        approval_id = int(approval.id)

    summary = _approval_summary(script, project)
    await event_bus.publish(
        project_id,
        "approval.created",
        {
            "approval_id": approval_id,
            "approval_type": "script",
            "status": "pending",
            "summary": summary,
        },
    )
    await event_bus.publish(
        project_id,
        "notification.approval_pending",
        {
            "user_id": user_id,
            "title": "剧本待确认",
            "content": f"《{summary.get('script_title') or summary.get('project_title')}》"
            f"v{summary.get('version_no')} 已生成，请前往审批台确认",
            "payload": {"approval_id": approval_id, "project_id": project_id},
        },
    )
    return {"approval_id": approval_id}


async def wait_for_decision(state: dict[str, Any]) -> dict[str, Any]:
    """等待审批决策节点（纯 interrupt，无副作用）。

    挂起载荷供前端渲染审批台；resume 值形如
    ``{"action": "approve|reject|edit", "feedback": str|None,
    "edited_payload": dict|None}``，由 decide API 在恢复前更新审批单
    状态后经 ``Command(resume=...)`` 传入。

    Args:
        state: 编排状态（需 approval_id）。

    Returns:
        状态更新 {decision}（含 action/feedback/edited_payload）。

    Raises:
        无（interrupt 语义：挂起时节点不返回，恢复重放时返回 decision）。
    """
    approval_id = state.get("approval_id")
    payload: dict[str, Any] = {
        "approval_id": approval_id,
        "approval_type": "script",
        "options": ["approve", "reject", "edit"],
    }
    decision = interrupt(payload)
    if not isinstance(decision, dict) or not decision.get("action"):
        # 防御：resume 载荷非法时构造显式错误，避免下游节点误读
        decision = {"action": "reject", "feedback": "无效的审批决策载荷"}
    return {"decision": decision}


def decision_branch(state: dict[str, Any]) -> str:
    """HITL 条件边：按 decision.action 路由到 approve/reject/edit 分支。

    Args:
        state: 编排状态（需 decision.action）。

    Returns:
        "approve" | "reject" | "edit"（未知 action 保守按 reject 回写）。
    """
    action = str((state.get("decision") or {}).get("action") or "reject")
    return action if action in ("approve", "reject", "edit") else "reject"


def compliance_branch(state: dict[str, Any]) -> str:
    """审核条件边：pass/末轮 flag → 落库；首轮 flag → 弱化重审。

    Args:
        state: 编排状态（需 qc_rounds；findings 已在 compliance_review
            返回值中清空/保留）。

    Returns:
        "split"（落库）或 "revise"（回编剧弱化重生成）。
    """
    qc_rounds = int(state.get("qc_rounds") or 0)
    if state.get("compliance_findings") and qc_rounds <= 1:
        return "revise"
    return "split"

"""编排服务模块（LangGraph 图在 FastAPI 进程内的后台任务执行模型）。

职责：
- :func:`invoke_script_generation`：发起剧本生成（202 异步模型）——
  校验并发/状态 → 后台任务消费 ``graph.astream(initial_state)``；
- :func:`resume_from_approval`：审批决策恢复——更新审批单状态后后台
  任务消费 ``graph.astream(Command(resume=decision))``；
- :func:`get_run_status`：运行状态查询（进程内执行标记 + 项目状态 +
  pending 审批单 + 最近 AgentRun）；
- 失败兜底：BizError/异常 → refund 冻结 + 项目 failed + ``run.failed``
  事件（LLM 节点绝不接触资金，退款只发生在本服务兜底层）。

工程决策：
- **并发防护**：同一 project 同时只有一个图执行——进程内
  ``project_id → asyncio.Lock``；锁占用或存在 pending 审批单时重复
  invoke/resume → BizError(40001)；
- **服务重启恢复**：不主动重放（interrupt 状态已持久化在 checkpoint
  表），审批单在库，resume 时从 checkpoint 继续（E2E-01）；
- **进度推送**：astream(stream_mode="updates") 逐节点发布
  ``progress`` 事件（步骤名/百分比），interrupt 时由 create_approval
  节点发布 ``approval.created``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_request_id
from app.graphs.checkpointer import graph_config
from app.graphs.script_graph import get_compiled_script_graph
from app.infra.event_bus import event_bus
from app.models.collab import AgentRun, ApprovalRequest
from app.models.project import Project
from app.repositories.billing_repo import BillingRepository
from app.services import analytics_service
from app.services import script_run_service
from app.services.billing_service import refund
from app.services import project_service
from app.services.project_service import transition

logger = logging.getLogger("gleamdream.orchestration")

# ---- 进程内并发防护：project_id -> 执行锁 ----
_PROJECT_LOCKS: dict[int, asyncio.Lock] = {}

# ---- 进程内运行标记：project_id -> 运行信息（get_run_status 诊断用） ----
_RUNNING: dict[int, dict[str, Any]] = {}

# ---- 后台图任务强引用集：create_task 返回的 Task 必须持强引用 ----
# CPython 事件循环仅对运行中的 Task 持弱引用（官方文档要求调用方自行
# 保存返回值），否则任务可能在执行中途被垃圾回收——症状为「202 受理
# 成功但图从未启动、无任何节点日志」。任务完成后从集合移除。
_BG_TASKS: set[asyncio.Task[None]] = set()

#: 单次剧本图执行的总时限（秒）。超过后强制失败并退款，避免节点挂起
#: 导致项目永久处于 scripting（Q-006）。
_RUN_TIMEOUT_SECONDS: float = 1800.0


def _spawn_graph_task(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """创建图执行后台任务并持有强引用（防 GC 丢失）。

    Args:
        coro: _run_graph 协程（invoke 或 resume）。

    Returns:
        已调度的 asyncio.Task。
    """
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task

# ---- 节点 -> 进度百分比（SSE progress 事件） ----
_NODE_PROGRESS: dict[str, int] = {
    "billing_precheck": 5,
    "generate_outline": 20,
    "generate_episodes": 45,
    "compliance_review": 60,
    "split_episodes_hook": 70,
    "create_approval": 85,
    "wait_for_decision": 85,
    "apply_human_edit": 75,
    "finalize_approve": 100,
}

# ---- 节点 -> 计划节点映射（WRITE_TODOS 任务规划状态同步，E2E 问题 5） ----
_NODE_TO_TODO: dict[str, str] = {
    "generate_outline": "outline",
    "generate_characters": "characters",
    "generate_episodes": "episodes",
    "generate_dialogues": "dialogues",
    "split_episodes_hook": "dialogues",
}

# 决策合法取值
_VALID_ACTIONS = frozenset({"approve", "reject", "edit"})


def _get_lock(project_id: int) -> asyncio.Lock:
    """获取项目级进程内执行锁（懒创建）。

    Args:
        project_id: 项目 ID。

    Returns:
        该项目的 asyncio.Lock 实例。
    """
    if project_id not in _PROJECT_LOCKS:
        _PROJECT_LOCKS[project_id] = asyncio.Lock()
    return _PROJECT_LOCKS[project_id]


def _utcnow() -> datetime:
    """获取当前 UTC 时间（timezone-aware）。

    Returns:
        带 UTC 时区的 datetime。
    """
    return datetime.now(timezone.utc)


async def _has_pending_approval(project_id: int) -> ApprovalRequest | None:
    """查询项目是否存在 pending 审批单（并发防护 + 状态查询）。

    Args:
        project_id: 项目 ID。

    Returns:
        pending 审批单实例（无则 None）。
    """
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.project_id == project_id,
                    ApprovalRequest.status == "pending",
                )
                .order_by(ApprovalRequest.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row


def _initial_state(project: Project, user_id: int, idea: str | None) -> dict[str, Any]:
    """构造编剧子图初始状态。

    Args:
        project: 项目 ORM 实例。
        user_id: 发起用户 ID。
        idea: 用户一句话创意（None 时取项目 synopsis）。

    Returns:
        ProjectState 初始 dict。
    """
    return {
        "project_id": int(project.id),
        "user_id": int(user_id),
        "action": "generate_script",
        "idea": (idea or project.synopsis or "（用户未提供创意，请自行发挥）"),
        "style_preset_key": project.style_preset_key,
        "aspect_ratio": project.aspect_ratio,
        "genre": project.genre,
        "target_episode_count": int(project.target_episode_count),
        "write_todos": [],
        "qc_rounds": 0,
    }


async def _refund_if_frozen(freeze_id: int | None, reason: str) -> None:
    """兜底退款：冻结仍为 frozen 时全额释放（幂等：状态校验兜底）。

    Args:
        freeze_id: 冻结记录 ID（可空）。
        reason: 释放原因（写入流水备注）。

    Raises:
        无（退款失败仅告警——资金兜底失败需要人工介入，见日志）。
    """
    if not freeze_id:
        return
    try:
        async with async_session_factory() as session:
            freeze = await BillingRepository(session).get_freeze(int(freeze_id))
            if freeze is not None and freeze.status == "frozen":
                await refund(session, freeze_id=int(freeze_id), reason=reason)
    except Exception:  # noqa: BLE001
        logger.error("失败兜底退款异常 freeze_id=%s", freeze_id, exc_info=True)


async def _fail_run(project_id: int, exc: Exception) -> None:
    """图运行失败兜底：退款 + 项目 failed 流转 + run.failed 事件。

    项目状态仅 scripting → failed（白名单合法路径）；script_review
    等其他状态保持原状并仅发布事件（由用户决定后续）。

    Args:
        project_id: 项目 ID。
        exc: 引发失败的异常（BizError 或未知异常）。

    Raises:
        无（兜底流程自身异常全部吞掉并记日志）。
    """
    code = getattr(exc, "code", ErrorCode.INTERNAL_ERROR)
    message = getattr(exc, "message", str(exc))
    logger.error("图执行失败 project_id=%s code=%s err=%s", project_id, code, message, exc_info=exc)

    # 1. 读取 checkpoint 中的 freeze_id（存在则退款）
    freeze_id: int | None = None
    try:
        graph = await get_compiled_script_graph()
        snapshot = await graph.aget_state(graph_config(project_id))
        freeze_id = (snapshot.values or {}).get("freeze_id")
    except Exception:  # noqa: BLE001
        logger.warning("读取 checkpoint 失败，跳过 state 级退款 project_id=%s", project_id)

    await _refund_if_frozen(freeze_id, reason=f"生成失败退款:{message[:60]}")

    # 2. 项目状态：scripting → failed；同时把失败原因持久化到 config，
    #    供前端在 SSE 断线/刷新后仍能展示具体原因（Q-018）。
    try:
        async with async_session_factory() as session:
            project = (
                await session.execute(
                    select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
                )
            ).scalar_one_or_none()
            if project is not None:
                config = dict(project.config or {})
                config["_last_script_failure"] = {
                    "code": str(code),
                    "message": message[:500],
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                project.config = config
                if project.status == "scripting":
                    await transition(project, "failed", message[:120], session=session, user_id=None)
                else:
                    await session.commit()
                await event_bus.publish(project_id, "script.failed", config["_last_script_failure"])
    except Exception:  # noqa: BLE001
        logger.warning("失败状态流转异常 project_id=%s", project_id, exc_info=True)

    await event_bus.publish(
        project_id,
        "run.failed",
        {"code": code, "message": message[:300], "refunded": bool(freeze_id)},
    )
    # V4.0：运行态收尾为 failed（保留已完成集数供前端提示）
    await script_run_service.finalize_run(
        project_id,
        status="failed",
        completed_episodes=None,
        failure_type=str(code),
        failure_reason=message[:300],
    )


async def _run_graph(
    project_id: int,
    *,
    initial_state: dict[str, Any] | None = None,
    resume_decision: dict[str, Any] | None = None,
) -> None:
    """后台任务主体：持有项目锁，消费 astream 到完成或 interrupt。

    Args:
        project_id: 项目 ID（同时是 thread_id）。
        initial_state: 首次 invoke 的初始状态（与 resume 二选一）。
        resume_decision: 审批恢复决策（与 initial_state 二选一）。

    Raises:
        无（异常在内部兜底，不向 create_task 调用方传播）。
    """
    lock = _get_lock(project_id)
    if lock.locked():
        logger.warning("项目图已在执行，忽略重复任务 project_id=%s", project_id)
        return
    async with lock:
        _RUNNING[project_id] = {
            "started_at": _utcnow().isoformat(),
            "mode": "resume" if resume_decision is not None else "invoke",
            "trace_id": new_request_id(),
        }
        try:
            graph = await get_compiled_script_graph()
            config = graph_config(project_id)
            payload: Any = (
                resume_decision if resume_decision is not None else initial_state
            )
            # LangGraph 1.x：resume 载荷须包 Command(resume=...)
            from langgraph.types import Command

            if resume_decision is not None:
                payload = Command(resume=resume_decision)

            async def _consume_stream() -> None:
                """消费图事件流并处理 interrupt/完成收尾。"""
                async for chunk in graph.astream(payload, config, stream_mode="updates"):
                    for node_name, update in chunk.items():
                        if node_name.startswith("__"):
                            continue
                        await event_bus.publish(
                            project_id,
                            "progress",
                            {
                                "step": node_name,
                                "progress": _NODE_PROGRESS.get(node_name, 50),
                                "summary": _summarize_update(update),
                            },
                        )
                        # 修复（E2E 报告问题 5）：任务规划（WRITE_TODOS）此前恒
                        # 显示 0/5 步完成——节点内部的 mark_plan_node 调用未覆盖
                        # 所有执行路径。这里在图节点完成处按 node→todo 映射兜底
                        # 同步一次，保证计划状态与真实进度一致。
                        todo_id = _NODE_TO_TODO.get(node_name)
                        if todo_id:
                            await script_run_service.mark_plan_node(project_id, todo_id, "done")
                        logger.info("图节点完成 project_id=%s node=%s", project_id, node_name)

                # 正常流结束：区分「interrupt 挂起等待审批」与「跑完」
                snapshot = await graph.aget_state(config)
                if snapshot.next:
                    _RUNNING[project_id]["status"] = "waiting_approval"
                    logger.info("图挂起等待审批 project_id=%s next=%s", project_id, snapshot.next)
                    # V4.0：逐集生成已完成（进入 HITL 等待），运行态收尾为 succeeded
                    await script_run_service.finalize_run(
                        project_id,
                        status="succeeded",
                        script_id=(snapshot.values or {}).get("script_id"),
                        completed_episodes=len((snapshot.values or {}).get("episodes_script") or []),
                    )
                else:
                    _RUNNING[project_id]["status"] = "completed"
                    logger.info("图执行完成 project_id=%s", project_id)
                    await script_run_service.finalize_run(
                        project_id,
                        status="succeeded",
                        script_id=(snapshot.values or {}).get("script_id"),
                        completed_episodes=len((snapshot.values or {}).get("episodes_script") or []),
                    )

            # Q-006：整轮执行总时限，避免节点内部卡死导致项目永久 scripting。
            async with asyncio.timeout(_RUN_TIMEOUT_SECONDS):
                await _consume_stream()
        except TimeoutError:
            _RUNNING[project_id]["status"] = "failed"
            logger.error("剧本生成超时 project_id=%s timeout=%ss", project_id, _RUN_TIMEOUT_SECONDS)
            await _fail_run(
                project_id,
                BizError(
                    ErrorCode.MODEL_UNAVAILABLE,
                    f"剧本生成超时（超过 {int(_RUN_TIMEOUT_SECONDS / 60)} 分钟），请重试",
                ),
            )
        except BizError as exc:
            _RUNNING[project_id]["status"] = "failed"
            await _fail_run(project_id, exc)
        except Exception as exc:  # noqa: BLE001
            _RUNNING[project_id]["status"] = "failed"
            await _fail_run(project_id, exc)
        finally:
            _RUNNING.pop(project_id, None)


def _summarize_update(update: Any) -> dict[str, Any] | None:
    """压缩节点输出为事件摘要（避免把大纲/剧本全文推给前端）。

    Args:
        update: astream updates 中该节点的输出 dict。

    Returns:
        轻量摘要 dict（outline 标题/集数/审批 id 等）；无输出返回 None。
    """
    if not isinstance(update, dict):
        return None
    summary: dict[str, Any] = {}
    outline = update.get("outline")
    if isinstance(outline, dict):
        summary["title"] = outline.get("title")
    if "episodes_script" in update:
        eps = update.get("episodes_script") or []
        summary["episode_count"] = len(eps) if isinstance(eps, list) else 0
    if "script_id" in update and update.get("script_id"):
        summary["script_id"] = update.get("script_id")
    if "approval_id" in update and update.get("approval_id"):
        summary["approval_id"] = update.get("approval_id")
    if "estimated_credits" in update:
        summary["estimated_credits"] = update.get("estimated_credits")
    return summary or None


# =====================================================================
# 对外服务入口
# =====================================================================


async def invoke_script_generation(
    session: AsyncSession,
    *,
    project_id: int,
    user_id: int,
    idea: str | None = None,
) -> dict[str, Any]:
    """发起剧本生成（异步任务模型，HTTP 202 语义）。

    校验链：
    1. 项目存在（调用方已做 ACL）；
    2. 进程内锁占用 → 40001（已有执行中的流程）；
    3. 存在 pending 审批单 → 40001（等待剧本审批）；
    4. 项目状态：draft 直接发起；scripting（重启恢复场景）允许重新
       发起；其余状态 → 40001。

    Args:
        session: 请求级数据库会话（校验用，图在独立短会话中运行）。
        project_id: 项目 ID。
        user_id: 发起用户 ID。
        idea: 一句话创意（None 时取项目 synopsis）。

    Returns:
        202 data：{project_id, status, estimated_credits, idea, message}。

    Raises:
        BizError: 10002 项目不存在；40001 并发冲突或状态不允许。
    """
    project = (
        await session.execute(
            select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")

    lock = _get_lock(project_id)
    if lock.locked():
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "已有执行中的流程，请稍后再试")
    if await _has_pending_approval(project_id) is not None:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "已有执行中的流程：等待剧本审批")
    if project.status == "failed":
        # 修复（测试报告 #11/#7）：failed 项目重试剧本生成——仅当失败前处于
        # 剧本阶段（draft/scripting/script_review）时自动恢复并放行；失败前
        # 已进入下游环节（资产/分镜/渲染等）则给出明确指引，避免用户在
        # 失败项目上盲目重试生成剧本破坏既有产物。
        failed_before = str((project.config or {}).get("_failed_before") or "")
        if failed_before not in ("draft", "scripting", "script_review"):
            raise BizError(
                ErrorCode.PROJECT_STATE_INVALID,
                "项目在剧本之后的环节失败，请先在项目列表使用「恢复项目」回到失败环节",
                data={"from": project.status, "failed_before": failed_before},
            )
        project = await project_service.recover_failed_project(
            session, project, "重试剧本生成，自动恢复失败项目", user_id=user_id,
        )
    elif project.status not in ("draft", "scripting"):
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"项目状态 {project.status} 不允许发起剧本生成",
            data={"from": project.status, "allowed": ["draft", "scripting"]},
        )

    # Q-005 修复：上一次失败/取消可能留下旧 checkpoint，导致新一轮图从
    # 上一次的断点直接续跑、跳过 billing_precheck（不创建新 ScriptRun，
    # 前端进度恒 0）。这里在无 pending 审批且项目仍处于剧本阶段时清理
    # thread checkpoint，保证本轮从预算预检节点重新开始。
    if project.status in ("draft", "scripting"):
        from app.graphs.checkpointer import delete_project_checkpoints

        await delete_project_checkpoints(project_id)

    state = _initial_state(project, user_id, idea)
    _spawn_graph_task(_run_graph(project_id, initial_state=state))
    await event_bus.publish(
        project_id,
        "run.started",
        {"action": "generate_script", "target_episode_count": int(project.target_episode_count)},
    )
    return {
        "project_id": project_id,
        "status": "accepted",
        "estimated_credits": int(project.target_episode_count) * 50,
        "idea": state["idea"],
        "message": "剧本生成任务已受理，进度请订阅项目事件流",
    }


async def resume_from_approval(
    session: AsyncSession,
    *,
    approval_id: int,
    action: str,
    feedback: str | None = None,
    edited_payload: dict[str, Any] | None = None,
    user_id: int,
) -> dict[str, Any]:
    """审批决策恢复（HITL resume，HTTP 202 语义）。

    动作序列：
    1. 审批单存在性 + pending 校验 + 惰性过期检查（过期置 expired 后
       抛 40001）；
    2. 审批单状态更新（approved/rejected/edited + decided_by/at +
       feedback/edited_payload，DR-4 留痕）；
    3. 进程内锁占用 → 40001；
    4. 后台任务 ``Command(resume=decision)`` 从 checkpoint 继续。

    Args:
        session: 请求级数据库会话（审批单更新在此提交）。
        approval_id: 审批单 ID。
        action: 决策 approve/reject/edit。
        feedback: 反馈原文（reject/edit 时建议填写）。
        edited_payload: 人工编辑内容（edit 时必填）。
        user_id: 决策人用户 ID（ACL 由调用方保证）。

    Returns:
        202 data：{approval_id, action, project_id, status}。

    Raises:
        BizError: 10002 审批单不存在；40001 已处理/已过期/并发冲突；
            10001 edit 缺 edited_payload 或 action 非法。
    """
    if action not in _VALID_ACTIONS:
        raise BizError(ErrorCode.PARAM_INVALID, f"action 仅支持 {'/'.join(sorted(_VALID_ACTIONS))}")
    if action == "edit" and not edited_payload:
        raise BizError(ErrorCode.PARAM_INVALID, "edit 决策必须携带 edited_payload")

    approval = await session.get(ApprovalRequest, approval_id)
    if approval is None:
        raise BizError(ErrorCode.NOT_FOUND, "审批单不存在")
    if approval.status != "pending":
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"审批单已处理（status={approval.status}），无法重复决策",
        )
    if approval.expires_at < _utcnow():
        approval.status = "expired"
        await session.commit()
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "审批单已过期")

    project_id = int(approval.project_id)
    lock = _get_lock(project_id)
    if lock.locked():
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "已有执行中的流程，请稍后再试")

    # 决策留痕（DR-4）：状态 + 决策人 + 反馈原文
    approval.status = "approved" if action == "approve" else ("edited" if action == "edit" else "rejected")
    approval.decision = action
    approval.decided_by_user_id = user_id
    approval.decided_at = _utcnow()
    if feedback:
        approval.feedback = feedback
    if edited_payload is not None:
        approval.edited_payload = edited_payload
    await session.commit()

    decision: dict[str, Any] = {"action": action, "feedback": feedback, "edited_payload": edited_payload}
    if approval.approval_type == "script":
        # 2026-11 修复（遗留 D）：pending 审批单可能已被 _ensure_script_approval
        # 重指到重新生成的最新剧本版本（target_id 更新），而 LangGraph
        # checkpoint 中的 state.script_id 仍是首次生成的旧版本。把审批单
        # 实际指向的剧本 ID 随决策载荷传入图中，finalize_approve 据此锁定
        # 用户真正批准的版本并更新 projects.current_script_id。
        if approval.target_id is not None:
            decision["target_script_id"] = int(approval.target_id)
        # 上传剧本没有 LangGraph checkpoint（不经过生成图），若仍按图 resume
        # 会启动一次完整的剧本生成并覆盖用户上传内容。这里按审批单来源分流：
        # upload → 直接确认剧本 + 推进项目状态；generated → 原图断点续跑。
        source = (
            str((approval.feedback_scope or {}).get("source") or "")
            if isinstance(approval.feedback_scope, dict)
            else ""
        )
        if source == "upload":
            _spawn_graph_task(_finalize_uploaded_script_decision(approval_id, action, user_id))
        else:
            _spawn_graph_task(_run_graph(project_id, resume_decision=decision))
    else:
        # D30/D28 修复：分镜与成片关口不属于剧本图 interrupt，
        # 走领域回写后处理（时间线状态/分镜解锁/项目状态推进）
        _spawn_graph_task(_finalize_gate_decision(approval_id, action, user_id))
    await event_bus.publish(
        project_id,
        "approval.decided",
        {"approval_id": approval_id, "action": action, "user_id": user_id},
    )
    # 埋点：HITL 关口决策（approve→通过 / reject→驳回；edit 不计入通过率）
    if action in ("approve", "reject") and approval.approval_type == "script":
        await analytics_service.track_gate(
            analytics_service.GATE_SCRIPT,
            action == "approve",
            user_id=user_id,
            project_id=project_id,
        )
    return {
        "approval_id": approval_id,
        "action": action,
        "project_id": project_id,
        "status": "accepted",
        "message": "决策已受理，流程从断点恢复",
    }


async def _finalize_uploaded_script_decision(approval_id: int, action: str, user_id: int) -> None:
    """上传剧本 HITL-1 决策后处理（无 LangGraph checkpoint）。

    - approve：剧本置 confirmed，项目 script_review → asset_preparing；
    - reject：剧本退回 draft，项目 script_review → scripting（可继续编辑）；
    - edit：保持待确认，等待用户修订后重新提交。

    Args:
        approval_id: 审批单 ID。
        action: 决策动作 approve/reject/edit。
        user_id: 决策人（仅用于日志追溯）。

    Returns:
        无。

    Raises:
        无：后台任务异常仅记日志，不影响已受理的决策。
    """
    from sqlalchemy import select

    from app.models.script import Script

    try:
        async with async_session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if approval is None or approval.target_id is None:
                return
            project = await session.get(Project, int(approval.project_id))
            script = await session.get(Script, int(approval.target_id))
            if project is None or script is None:
                return
            if action == "approve":
                # 审批单可能在用户行级编辑前创建；编辑会生成新版本（version+1）。
                # 确认时把"项目最新版本"置 confirmed 并设为当前剧本，避免把
                # current_script_id 回退到旧版本（实测 current 会从 570 掉回 569）。
                latest = (
                    await session.execute(
                        select(Script)
                        .where(Script.project_id == int(project.id))
                        .order_by(Script.version_no.desc(), Script.id.desc())
                        .limit(1)
                    )
                ).scalars().first() or script
                latest.status = "confirmed"
                project.current_script_id = int(latest.id)
                if project.status in ("draft", "scripting", "script_review"):
                    project.status = "asset_preparing"
            elif action == "reject":
                script.status = "draft"
                if project.status == "script_review":
                    project.status = "scripting"
            await session.commit()
            logger.info(
                "上传剧本关口决策完成 approval=%s action=%s user=%s project=%s",
                approval_id, action, user_id, project.id,
            )
    except Exception:  # noqa: BLE001 - 后台后处理失败不回收已受理决策
        logger.exception("上传剧本关口决策后处理失败 approval=%s", approval_id)


async def _finalize_gate_decision(approval_id: int, action: str, user_id: int) -> None:
    """非剧本类关口（storyboard/final_cut）审批决策后处理（D30/D28 修复）。

    剧本类审批单（script）由剧本子图 checkpoint resume 恢复；分镜与成片
    审批单不属于剧本图 interrupt，需要在此完成决策落库后的领域回写：

    - final_cut + approve：preview/master 时间线置 approved，项目
      final_review → completed；
    - final_cut + reject/edit：时间线打回 draft（重新调整后再次合成），
      项目 final_review → post_producing；
    - storyboard + approve：分镜锁定与状态推进已在锁定动作中完成，仅发布
      事件与埋点；
    - storyboard + reject/edit：分镜与镜头解锁回 pending_review 可编辑态，
      项目 storyboard_review → storyboarding。

    独立短会话执行（后台任务），失败仅记日志不影响决策受理结果。

    Args:
        approval_id: 审批单 ID。
        action: 决策动作 approve/reject/edit。
        user_id: 决策人用户 ID。
    """
    # 局部导入规避 services 间循环依赖
    from app.models.storyboard import Shot, Storyboard
    from app.repositories.post_repo import PostRepository

    try:
        async with async_session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
            if approval is None:
                return
            project = await session.get(Project, int(approval.project_id))
            if project is None:
                return
            approved = action == "approve"

            if approval.approval_type == "final_cut":
                repo = PostRepository(session)
                target_id = int(approval.target_id) if approval.target_id is not None else None
                for cut in ("preview", "master"):
                    tl = await repo.get_timeline(int(project.id), cut)
                    if tl is None:
                        continue
                    if (target_id is not None and int(tl.id) == target_id) or (
                        cut == "master" and tl.status == "pending_review"
                    ):
                        tl.status = "approved" if approved else "draft"
                if approved:
                    # 修复（测试报告 #29/#35）：项目若处于 failed（渲染/合成失败），
                    # 成片审批通过即为人工恢复信号——先自动恢复再链式推进到 completed，
                    # 避免 final_review 白名单分支永远不命中的状态脱节。
                    project = await project_service.recover_failed_project(
                        session, project, "成片终审通过，自动恢复失败项目", user_id=user_id,
                    )
                    if project.status in ("rendering", "post_producing", "final_review"):
                        await project_service.advance_chain(
                            project,
                            ["post_producing", "final_review", "completed"],
                            "成片终审通过",
                            session=session,
                            user_id=user_id,
                        )
                elif project.status == "final_review":
                    await transition(project, "post_producing", "成片终审驳回，打回后期", session=session)

            elif approval.approval_type == "storyboard":
                if not approved:
                    sb = await session.get(Storyboard, int(approval.target_id)) if approval.target_id else None
                    if sb is not None and sb.status == "locked":
                        sb.status = "pending_review"
                        sb.locked_at = None
                        shots = (
                            await session.execute(
                                select(Shot).where(Shot.storyboard_id == int(sb.id))
                            )
                        ).scalars().all()
                        for shot in shots:
                            if shot.status == "locked":
                                shot.status = "pending_review"
                                shot.locked_at = None
                    if project.status == "storyboard_review":
                        await transition(
                            project, "storyboarding", "分镜终审驳回，退回编辑", session=session
                        )
                # approve：锁定/状态推进已在「批准分镜并送审」动作中完成

            await session.commit()
            await event_bus.publish(
                int(project.id),
                "gate.finalized",
                {
                    "approval_id": approval_id,
                    "approval_type": approval.approval_type,
                    "action": action,
                    "project_status": project.status,
                },
            )
            # 关口埋点（分镜/成片）
            gate = (
                analytics_service.GATE_STORYBOARD
                if approval.approval_type == "storyboard"
                else analytics_service.GATE_MASTER
            )
            await analytics_service.track_gate(
                gate, approved, user_id=user_id, project_id=int(project.id)
            )
    except Exception:  # noqa: BLE001 后台后处理兜底
        logger.exception("关口决策后处理失败 approval_id=%s action=%s", approval_id, action)


async def reconcile_orphan_approvals() -> list[dict[str, Any]]:
    """服务启动补偿：修复跨进程重启遗留的孤儿审批单（D19 修复）。

    场景：剧本审批单在旧进程 approve（决策已落库）后服务重启，checkpoint
    resume 后台任务丢失，导致项目状态停在 script_review、冻结积分未结算。
    本补偿按「已决策 + 领域状态未跟进」判定孤儿并修复：

    - script + approved + 项目 status=script_review：
      项目推进 asset_preparing；该项目的未释放冻结（status=frozen）全额
      退还（旧进程结算数据已丢失，按失败兜底语义返还，不亏欠用户）；
    - final_cut + approved + 项目 status=final_review：
      preview/master 时间线置 approved 并推进 completed。

    Args:
        无（扫描全库已决策审批单）。

    Returns:
        修复清单 ``[{approval_id, approval_type, project_id, action}]``。

    Raises:
        无（内部兜底，失败仅记日志）。
    """
    from app.models.billing import CreditFreeze
    from app.repositories.post_repo import PostRepository
    from app.services.billing_service import refund

    fixed: list[dict[str, Any]] = []
    try:
        async with async_session_factory() as session:
            approvals = (
                await session.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.status == "approved",
                        ApprovalRequest.approval_type.in_(("script", "final_cut")),
                    )
                )
            ).scalars().all()
            for ap in approvals:
                project = await session.get(Project, int(ap.project_id))
                if project is None:
                    continue
                acted = False

                if ap.approval_type == "script" and project.status == "script_review":
                    await transition(
                        project, "asset_preparing", "孤儿审批单补偿：剧本已通过", session=session
                    )
                    # 结算数据随旧进程丢失：未释放冻结全额返还（用户不受损）
                    frozen = (
                        await session.execute(
                            select(CreditFreeze).where(
                                CreditFreeze.project_id == int(project.id),
                                CreditFreeze.status == "frozen",
                            )
                        )
                    ).scalars().all()
                    for fz in frozen:
                        try:
                            await refund(session, freeze_id=int(fz.id), reason="孤儿审批补偿：任务已通过，冻结返还")
                        except Exception:  # noqa: BLE001 单笔失败不阻断补偿
                            logger.warning("孤儿补偿退冻结失败 freeze_id=%s", fz.id)
                    acted = True

                elif ap.approval_type == "final_cut" and project.status == "final_review":
                    repo = PostRepository(session)
                    for cut in ("preview", "master"):
                        tl = await repo.get_timeline(int(project.id), cut)
                        if tl is not None and tl.status == "pending_review":
                            tl.status = "approved"
                    await transition(project, "completed", "孤儿审批补偿：成片已通过", session=session)
                    acted = True

                if acted:
                    fixed.append(
                        {
                            "approval_id": int(ap.id),
                            "approval_type": ap.approval_type,
                            "project_id": int(project.id),
                            "action": ap.decision,
                        }
                    )
            await session.commit()
        if fixed:
            logger.warning("孤儿审批单补偿完成 count=%s detail=%s", len(fixed), fixed)
    except Exception:  # noqa: BLE001 启动补偿兜底
        logger.exception("孤儿审批单补偿执行失败")
    return fixed


async def get_run_status(
    session: AsyncSession, *, project_id: int, user_id: int
) -> dict[str, Any]:
    """查询项目编排运行状态（进程内执行标记 + DB 真值）。

    Args:
        session: 请求级数据库会话。
        project_id: 项目 ID。
        user_id: 查询用户 ID（用于站内通知过滤等扩展，当前仅透传）。

    Returns:
        {project_id, running, waiting_approval, project_status,
         pending_approval, recent_runs}。

    Raises:
        BizError: 10002 项目不存在。
    """
    project = (
        await session.execute(
            select(Project).where(Project.id == project_id, Project.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")

    pending = await _has_pending_approval(project_id)
    recent = (
        await session.execute(
            select(AgentRun)
            .where(AgentRun.project_id == project_id)
            .order_by(AgentRun.id.desc())
            .limit(10)
        )
    ).scalars().all()

    running_info = _RUNNING.get(project_id)
    return {
        "project_id": project_id,
        "running": bool(running_info),
        "waiting_approval": pending is not None and not running_info,
        "project_status": project.status,
        "pending_approval": (
            {
                "approval_id": int(pending.id),
                "approval_type": pending.approval_type,
                "expires_at": pending.expires_at.isoformat(),
            }
            if pending is not None
            else None
        ),
        "recent_runs": [
            {
                "id": int(run.id),
                "node_name": run.node_name,
                "agent_key": run.agent_key,
                "status": run.status,
                "total_tokens": run.total_tokens,
                "cost_credits": run.cost_credits,
                "latency_ms": run.latency_ms,
            }
            for run in recent
        ],
    }

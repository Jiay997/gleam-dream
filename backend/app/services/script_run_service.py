"""剧本生成运行态服务（V4.0 US-F2-12 / AC-F2-12-1~3）。

职责：把「一次剧本生成任务」的生命周期落成可查询、可终止、可结算的
``script_runs`` 行，让前端在逐集生成过程中能：

1. 查看进度（已完成集数 / 计划集数 / 计划节点状态）；
2. 中途终止，**保留已完成集**，未执行部分按集解冻返还；
3. 终止后重新发起时使用新幂等键，并支持「只生成缺集」。

设计要点：

- **幂等**：``idempotency_key`` 唯一（部分索引），同一键重复创建返回原运行；
- **竞态**（AC-F2-12-2）：终止与「第 k 集写入完成」以**先持久化者为准**——
  结算依据读取数据库中已持久化的 ``completed_episodes``，且结算走
  ``billing_service.settle(freeze_id)`` 的行锁事务，重复结算第二次会因
  ``freeze.status != frozen`` 被拒（不重复扣费）；
- **协作式终止**：终止只落状态位 ``cancelled``；真正的停止由生成节点在
  **每集开始前**读取 :func:`is_cancel_requested` 自查，避免强杀协程导致
  半写数据。

计费口径：单集预估单价 :data:`CREDITS_PER_EPISODE`（与
``graphs/nodes/deterministic.CREDITS_PER_EPISODE`` 同源，均为 50 积分）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.models.script import ScriptRun
from app.services import billing_service

logger = logging.getLogger("gleamdream.script_run")

#: 单集剧本生成预估单价（积分）——与 deterministic.CREDITS_PER_EPISODE 对齐
CREDITS_PER_EPISODE: int = 50

#: 可终止的运行状态
_CANCELLABLE: frozenset[str] = frozenset({"generating"})


def _utcnow() -> datetime:
    """获取当前 UTC 时间。

    Returns:
        带时区的当前时间。
    """
    return datetime.now(timezone.utc)


def run_to_dict(run: ScriptRun) -> dict[str, Any]:
    """把运行态 ORM 行序列化为对外结构。

    Args:
        run: 运行态 ORM 实例。

    Returns:
        含进度百分比与结算明细的字典。
    """
    total = int(run.total_episodes)
    completed = int(run.completed_episodes)
    progress = int(round(completed / total * 100)) if total > 0 else 0
    return {
        "run_id": int(run.id),
        "project_id": int(run.project_id),
        "script_id": int(run.script_id) if run.script_id else None,
        "run_status": run.run_status,
        "total_episodes": total,
        "completed_episodes": completed,
        "progress_percent": min(100, max(0, progress)),
        "frozen_credits": int(run.frozen_credits),
        "settled_credits": int(run.settled_credits),
        "refunded_credits": int(run.refunded_credits),
        "plan": run.plan or [],
        "failure_type": run.failure_type,
        "failure_reason": run.failure_reason,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


async def get_active_run(
    session: AsyncSession, project_id: int
) -> ScriptRun | None:
    """取项目当前处于 ``generating`` 的运行态（无则 None）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。

    Returns:
        最新一条 generating 运行；无则 None。
    """
    stmt = (
        select(ScriptRun)
        .where(ScriptRun.project_id == project_id, ScriptRun.run_status == "generating")
        .order_by(ScriptRun.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_run_or_error(
    session: AsyncSession, project_id: int, run_id: int
) -> ScriptRun:
    """按 ID 取运行态并校验归属项目。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        run_id: 运行 ID。

    Returns:
        运行态 ORM 实例。

    Raises:
        BizError: 10002 运行记录不存在或不属于该项目。
    """
    run = (
        await session.execute(
            select(ScriptRun).where(
                ScriptRun.id == run_id, ScriptRun.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise BizError(ErrorCode.NOT_FOUND, "剧本生成任务不存在")
    return run


async def list_runs(
    session: AsyncSession, project_id: int, limit: int = 20
) -> list[dict[str, Any]]:
    """列出项目最近的剧本生成任务。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        limit: 返回条数上限。

    Returns:
        运行态字典列表（按 ID 倒序）。
    """
    stmt = (
        select(ScriptRun)
        .where(ScriptRun.project_id == project_id)
        .order_by(ScriptRun.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [run_to_dict(run) for run in rows]


async def start_run(
    session: AsyncSession,
    *,
    project_id: int,
    total_episodes: int,
    frozen_credits: int,
    user_id: int,
    idempotency_key: str | None = None,
    plan: list[dict[str, Any]] | None = None,
    freeze_id: int | None = None,
) -> ScriptRun:
    """创建（或幂等复用）一次生成运行态。

    幂等语义：同 ``idempotency_key`` 已存在时直接返回原运行（AC-F2-12-3
    要求终止后重新发起必须换新键，故新键必然产生新运行）。

    Args:
        session: 数据库会话（调用方提交）。
        project_id: 项目 ID。
        total_episodes: 计划生成集数（≥1）。
        frozen_credits: 本次冻结积分。
        user_id: 发起用户 ID。
        idempotency_key: 幂等键（可空）。
        plan: write_todos 计划节点列表。
        freeze_id: 关联计费冻结记录 ID（终止时按集结算依据）。

    Returns:
        运行态 ORM 实例。

    Raises:
        BizError: 10001 集数非法。
    """
    if total_episodes < 1:
        raise BizError(ErrorCode.PARAM_INVALID, "生成集数必须大于 0")
    if idempotency_key:
        existing = (
            await session.execute(
                select(ScriptRun).where(ScriptRun.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing

    run = ScriptRun(
        project_id=project_id,
        run_status="generating",
        idempotency_key=idempotency_key,
        freeze_id=freeze_id,
        total_episodes=total_episodes,
        completed_episodes=0,
        frozen_credits=int(frozen_credits),
        plan=plan or _default_plan(),
        created_by=user_id,
        started_at=_utcnow(),
    )
    session.add(run)
    await session.flush()
    return run


def _default_plan() -> list[dict[str, Any]]:
    """默认 write_todos 四节点计划（AC-F2-01-5 计划可见）。

    Returns:
        计划节点列表（大纲 / 人物 / 分集 / 对白）。
    """
    return [
        {"todo_id": "outline", "node_name": "大纲", "status": "pending"},
        {"todo_id": "characters", "node_name": "人物", "status": "pending"},
        {"todo_id": "episodes", "node_name": "分集", "status": "pending"},
        {"todo_id": "dialogues", "node_name": "对白", "status": "pending"},
    ]


async def mark_plan_node(
    project_id: int, todo_id: str, status: str
) -> None:
    """更新计划节点状态并发 ``todo_updated`` 事件（AC-F2-01-5）。

    使用独立短会话，避免影响图节点的长事务。

    Args:
        project_id: 项目 ID。
        todo_id: 计划节点标识（outline / characters / episodes / dialogues）。
        status: 节点状态 pending / running / done / failed。
    """
    from app.infra.event_bus import event_bus

    try:
        async with async_session_factory() as session:
            run = await get_active_run(session, project_id)
            if run is None:
                return
            plan = list(run.plan or _default_plan())
            elapsed_ms: int | None = None
            for node in plan:
                if node.get("todo_id") == todo_id:
                    node["status"] = status
                    if status == "done":
                        node["elapsed_ms"] = 0
                    elapsed_ms = node.get("elapsed_ms")
            run.plan = plan
            # JSONB 嵌套 dict 的原地修改无法被 SQLAlchemy 变更检测捕获
            # （浅拷贝后旧值与新值相等 → 不发 UPDATE，计划面板恒 0/5），
            # 这里显式标记属性已修改，确保状态真正落库。
            flag_modified(run, "plan")
            await session.commit()
        await event_bus.publish(
            project_id,
            "todo_updated",
            {"todo_id": todo_id, "node_name": todo_id, "status": status,
             "elapsed_ms": elapsed_ms or 0},
        )
    except Exception:  # noqa: BLE001
        logger.warning("计划节点更新失败 project_id=%s todo=%s", project_id, todo_id, exc_info=True)


async def record_progress(project_id: int, completed_episodes: int) -> None:
    """记录逐集生成进度（每完成一集调用一次）。

    使用独立短会话 + 单行 UPDATE 语义，保证「第 k 集写入完成」尽快持久化，
    使终止请求的结算依据尽可能接近真实完成集数（AC-F2-12-2）。

    Args:
        project_id: 项目 ID。
        completed_episodes: 已完成集数（单调递增，取最大值写入）。
    """
    try:
        async with async_session_factory() as session:
            run = await get_active_run(session, project_id)
            if run is None:
                return
            if completed_episodes > int(run.completed_episodes):
                run.completed_episodes = min(
                    int(run.total_episodes), int(completed_episodes)
                )
                await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("剧本生成进度落库失败 project_id=%s", project_id, exc_info=True)


async def is_cancel_requested(project_id: int) -> bool:
    """判断项目是否存在已终止的生成运行（生成节点每集前自查）。

    语义：最新一条运行若为 ``cancelled``，说明用户已终止，节点应尽快收尾。

    Args:
        project_id: 项目 ID。

    Returns:
        已终止返回 True。
    """
    try:
        async with async_session_factory() as session:
            latest = (
                await session.execute(
                    select(ScriptRun)
                    .where(ScriptRun.project_id == project_id)
                    .order_by(ScriptRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return latest is not None and latest.run_status == "cancelled"
    except Exception:  # noqa: BLE001
        logger.warning("终止位查询失败 project_id=%s", project_id, exc_info=True)
        return False


async def cancel_run(
    session: AsyncSession,
    project: Any,
    run_id: int,
    user_id: int,
    freeze_id: int | None = None,
) -> dict[str, Any]:
    """终止生成：保留已完成集，按实际完成集数结算并返还未执行部分。

    执行序列（单请求内完成，失败整体回滚）：

    1. 运行态校验（仅 ``generating`` 可终止）；
    2. 读取已持久化的 ``completed_episodes``（先持久化者为准）；
    3. 计费：``completed > 0`` → ``settle(freeze_id, completed × 单价)``
       （结算差额自动解冻返还）；``completed == 0`` → ``refund(freeze_id)``；
    4. 运行态置 ``cancelled`` 并记录已结算/已返还积分；
    5. 项目状态回收：``scripting`` → ``draft``（已完成集保留为 draft）；
    6. 发布 ``run.cancelled`` 事件。

    Args:
        session: 请求级数据库会话。
        project: 项目 ORM 实例（ACL 已由路由层校验）。
        run_id: 运行 ID。
        user_id: 操作人用户 ID（审计留痕）。
        freeze_id: 关联冻结记录 ID；None 时不做计费动作（兼容无冻结场景）。

    Returns:
        ``{run, retained_episodes, refunded_credits, settled_credits,
        project_status}``。

    Raises:
        BizError: 10002 运行不存在；40001 运行状态不允许终止。
    """
    from app.infra.event_bus import event_bus
    from app.models.iam import AuditLog
    from app.services.project_service import transition

    run = await get_run_or_error(session, int(project.id), run_id)
    if run.run_status not in _CANCELLABLE:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"当前运行状态 {run.run_status} 不允许终止",
            data={"run_status": run.run_status},
        )

    completed = int(run.completed_episodes)
    if freeze_id is None:
        freeze_id = int(run.freeze_id) if run.freeze_id else None
    settled = 0
    refunded = 0
    if freeze_id is not None:
        if completed > 0:
            actual = min(int(run.frozen_credits), completed * CREDITS_PER_EPISODE)
            detail = await billing_service.settle(
                session, freeze_id=int(freeze_id), actual_credits=max(1, actual)
            )
            settled = int(detail.get("actual_credits", 0))
            refunded = int(detail.get("released_credits", 0))
        else:
            detail = await billing_service.refund(
                session, freeze_id=int(freeze_id), reason="剧本生成终止（未完成任何集）"
            )
            refunded = int(detail.get("released_credits", 0))

    run.run_status = "cancelled"
    run.settled_credits = settled
    run.refunded_credits = refunded
    run.finished_at = _utcnow()

    session.add(
        AuditLog(
            actor_type="user",
            actor_user_id=user_id,
            project_id=int(project.id),
            action="script_generation_cancelled",
            target_type="script_run",
            target_id=int(run.id),
            detail={
                "completed_episodes": completed,
                "total_episodes": int(run.total_episodes),
                "settled_credits": settled,
                "refunded_credits": refunded,
            },
        )
    )

    project_status = project.status
    if project.status == "scripting":
        try:
            await transition(
                project, "draft", "用户终止剧本生成（已完成集保留）",
                session=session, user_id=user_id,
            )
            project_status = project.status
        except Exception:  # noqa: BLE001
            logger.warning("终止后项目状态回收失败 project_id=%s", project.id, exc_info=True)

    await event_bus.publish(
        int(project.id),
        "run.cancelled",
        {
            "run_id": int(run.id),
            "completed_episodes": completed,
            "refunded_credits": refunded,
            "settled_credits": settled,
        },
    )
    # 显式提交：无状态流转分支时 submit 侧不会隐式提交，须在此落库，
    # 否则重复终止请求会读到旧的 generating 状态并二次结算（30005）。
    await session.commit()
    return {
        "run": run_to_dict(run),
        "retained_episodes": completed,
        "settled_credits": settled,
        "refunded_credits": refunded,
        "project_status": project_status,
    }


async def finalize_run(
    project_id: int,
    *,
    status: str,
    script_id: int | None = None,
    completed_episodes: int | None = None,
    failure_type: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """收尾运行态（成功/失败），供图运行结束回调。

    Args:
        project_id: 项目 ID。
        status: 目标状态 succeeded / failed / cancelled。
        script_id: 产出的剧本版本 ID。
        completed_episodes: 已完成集数（None 表示不改动）。
        failure_type: 失败类型。
        failure_reason: 失败详情。
    """
    try:
        async with async_session_factory() as session:
            run = await get_active_run(session, project_id)
            if run is None:
                return
            if status == "cancelled":
                run.run_status = "cancelled"
            else:
                run.run_status = status
            if script_id is not None:
                run.script_id = int(script_id)
            if completed_episodes is not None:
                run.completed_episodes = max(
                    int(run.completed_episodes),
                    min(int(run.total_episodes), int(completed_episodes)),
                )
            run.failure_type = failure_type
            run.failure_reason = failure_reason
            run.finished_at = _utcnow()
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("运行态收尾失败 project_id=%s", project_id, exc_info=True)

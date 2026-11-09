"""埋点服务模块（验收报告 7.1 P0 · 埋点最小集）。

观测口径（对齐 PRD 第 9 章埋点清单最小集）：

- **注册**：``user_registered``（auth_service.register / wechat_login 建号）；
- **激活**：``project_activated``（用户创建首个项目，PRD 激活定义）；
- **四关口通过率**：``gate_passed`` / ``gate_rejected``（props.gate ∈
  script / asset / storyboard / master，对应 HITL-1~4）；
- **导出漏斗**：``export_started`` → ``export_completed`` / ``export_failed``；
- **分发**：``project_distributed``（导出后上传平台，漏斗末端）。

工程约定：
- **只追加**：事件行不更新不删除；
- **绝不影响主流程**：track 自建短会话，任何异常仅告警（与模型网关
  记账同策略）；埋点失败不回滚业务事务；
- **聚合读**：:func:`funnel_summary` 在请求级会话上做窗口聚合，
  供管理端漏斗查询。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import async_session_factory
from app.models.analytics import AnalyticsEvent

logger = logging.getLogger("gleamdream.analytics")

# 四关口（HITL-1~4）的 props.gate 取值
GATE_SCRIPT = "script"
GATE_ASSET = "asset"
GATE_STORYBOARD = "storyboard"
GATE_MASTER = "master"
#: 合法关口集合（服务端埋点与查询聚合共用）
GATE_NAMES: tuple[str, ...] = (GATE_SCRIPT, GATE_ASSET, GATE_STORYBOARD, GATE_MASTER)

# 服务端埋点事件名（客户端可上报其他业务事件，写入前校验命名格式）
EVENT_USER_REGISTERED = "user_registered"
EVENT_PROJECT_ACTIVATED = "project_activated"
EVENT_GATE_PASSED = "gate_passed"
EVENT_GATE_REJECTED = "gate_rejected"
EVENT_EXPORT_STARTED = "export_started"
EVENT_EXPORT_COMPLETED = "export_completed"
EVENT_EXPORT_FAILED = "export_failed"
EVENT_PROJECT_DISTRIBUTED = "project_distributed"

# 会话工厂类型别名（便于注入测试）
SessionFactory = async_sessionmaker[AsyncSession]


async def track(
    event_name: str,
    *,
    user_id: int | None = None,
    project_id: int | None = None,
    session_id: str | None = None,
    props: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    session_factory: SessionFactory = async_session_factory,
) -> None:
    """写入一条埋点事件（自建短会话，失败仅告警）。

    Args:
        event_name: 事件名（snake_case 3~64 字符）。
        user_id: 用户 ID（可空）。
        project_id: 项目 ID（可空）。
        session_id: 客户端漏斗会话 ID（可空）。
        props: 事件属性（可空，JSONB 落库）。
        occurred_at: 事件发生时间（缺省当前 UTC）。
        session_factory: 会话工厂（默认全局；测试可注入替代）。
    """
    try:
        async with session_factory() as session:
            session.add(
                AnalyticsEvent(
                    event_name=event_name,
                    user_id=user_id,
                    project_id=project_id,
                    session_id=session_id,
                    occurred_at=occurred_at or datetime.now(timezone.utc),
                    event_props=props,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 埋点失败绝不影响业务主流程
        logger.warning(
            "写入 analytics_events 失败 event=%s user=%s project=%s",
            event_name, user_id, project_id, exc_info=True,
        )


async def track_gate(
    gate: str,
    passed: bool,
    *,
    user_id: int | None = None,
    project_id: int | None = None,
    session_factory: SessionFactory = async_session_factory,
) -> None:
    """四关口决策埋点（gate_passed / gate_rejected）。

    Args:
        gate: 关口名（script/asset/storyboard/master）。
        passed: 是否通过（True → gate_passed，False → gate_rejected）。
        user_id: 决策人用户 ID。
        project_id: 项目 ID。
        session_factory: 会话工厂。
    """
    await track(
        EVENT_GATE_PASSED if passed else EVENT_GATE_REJECTED,
        user_id=user_id,
        project_id=project_id,
        props={"gate": gate},
        session_factory=session_factory,
    )


async def funnel_summary(session: AsyncSession, *, days: int = 30) -> dict[str, Any]:
    """聚合埋点漏斗摘要（管理端查询）。

    统计口径（窗口：最近 ``days`` 天，按 occurred_at）：
    - ``registered_users``：user_registered 事件去重用户数；
    - ``activated_users``：**新用户激活**——窗口内注册、且历史上产生过
      project_activated 事件的去重用户数（激活用户 ⊆ 注册队列，
      保证 activation_rate ≤ 1；窗口外注册的老用户激活不计入分子）；
    - ``gates``：四关口 passed/rejected 次数与通过率（按事件次数）；
    - ``export_funnel``：export_started/completed/failed 次数与完成率。

    Args:
        session: 请求级数据库会话（只读查询）。
        days: 统计窗口天数（默认 30）。

    Returns:
        可 JSON 序列化的漏斗摘要字典。
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async def _event_count(event_name: str) -> int:
        stmt = (
            select(func.count())
            .select_from(AnalyticsEvent)
            .where(AnalyticsEvent.event_name == event_name, AnalyticsEvent.occurred_at >= since)
        )
        return int((await session.execute(stmt)).scalar_one() or 0)

    # 窗口内注册的去重用户数
    registered = int(
        (
            await session.execute(
                select(func.count(func.distinct(AnalyticsEvent.user_id))).where(
                    AnalyticsEvent.event_name == EVENT_USER_REGISTERED,
                    AnalyticsEvent.occurred_at >= since,
                )
            )
        ).scalar_one()
        or 0
    )
    # 新用户激活：窗口内注册 ∩ 历史上有激活事件（子查询限定用户集合）
    activated_subq = (
        select(AnalyticsEvent.user_id)
        .where(
            AnalyticsEvent.event_name == EVENT_PROJECT_ACTIVATED,
            AnalyticsEvent.user_id.is_not(None),
        )
    ).scalar_subquery()
    activated = int(
        (
            await session.execute(
                select(func.count(func.distinct(AnalyticsEvent.user_id))).where(
                    AnalyticsEvent.event_name == EVENT_USER_REGISTERED,
                    AnalyticsEvent.occurred_at >= since,
                    AnalyticsEvent.user_id.in_(activated_subq),
                )
            )
        ).scalar_one()
        or 0
    )

    gate_expr = AnalyticsEvent.event_props["gate"].astext
    gate_rows = (
        await session.execute(
            select(gate_expr, AnalyticsEvent.event_name, func.count())
            .where(
                AnalyticsEvent.event_name.in_([EVENT_GATE_PASSED, EVENT_GATE_REJECTED]),
                AnalyticsEvent.occurred_at >= since,
            )
            .group_by(gate_expr, AnalyticsEvent.event_name)
        )
    ).all()
    gate_counts: dict[str, dict[str, int]] = {
        gate: {"passed": 0, "rejected": 0} for gate in GATE_NAMES
    }
    for gate, event_name, count in gate_rows:
        if gate in gate_counts:
            key = "passed" if event_name == EVENT_GATE_PASSED else "rejected"
            gate_counts[gate][key] = int(count)
    gates_out: dict[str, dict[str, Any]] = {}
    for gate, counts in gate_counts.items():
        total = counts["passed"] + counts["rejected"]
        gates_out[gate] = {
            **counts,
            "total": total,
            "pass_rate": round(counts["passed"] / total, 4) if total else None,
        }

    started = await _event_count(EVENT_EXPORT_STARTED)
    completed = await _event_count(EVENT_EXPORT_COMPLETED)
    failed = await _event_count(EVENT_EXPORT_FAILED)
    finished = completed + failed
    export_funnel = {
        "started": started,
        "completed": completed,
        "failed": failed,
        "completion_rate": round(completed / finished, 4) if finished else None,
    }

    return {
        "window_days": days,
        "registered_users": registered,
        "activated_users": activated,
        "activation_rate": round(activated / registered, 4) if registered else None,
        "gates": gates_out,
        "export_funnel": export_funnel,
    }

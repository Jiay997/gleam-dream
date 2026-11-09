"""埋点事件模型（观测域 L，表 analytics_events）。

落地《09_MVP验收总结报告》7.1 P0 事项「埋点最小集」：注册 / 激活 /
四关口通过率 / 导出漏斗。北极星指标 CCCR 与 M1–M13 的观测数据源。

设计约定：
- **只追加**：埋点行不更新不删除，分析口径由查询侧聚合；
- **弱引用**：user_id / project_id 不设物理外键（高写入日志表，
  与 ai_call_logs 同策略），关联完整性由业务侧保证；
- **服务端 + 客户端双通道**：服务端关键流（注册/激活/关口/导出）由
  :mod:`app.services.analytics_service` 自动埋点，前端行为可经
  ``POST /analytics/events`` 补充上报。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import IdMixin, TimestampMixin


class AnalyticsEvent(IdMixin, TimestampMixin, Base):
    """埋点事件（表 analytics_events，只追加）。

    Attributes:
        event_name: 事件名（snake_case，如 user_registered / gate_passed）。
        user_id: 用户 ID（弱引用，可空——匿名/系统事件）。
        project_id: 项目 ID（弱引用，可空）。
        session_id: 客户端漏斗会话 ID（可空，前端 funnel 拼接用）。
        occurred_at: 事件发生时间（UTC；服务端事件默认写入时刻）。
        event_props: 事件属性 JSONB（gate/export 相关维度，如
            {"gate": "script", "action": "approve"} / {"resolution": "1080p"}）。
    """

    __tablename__ = "analytics_events"
    __table_args__ = (
        CheckConstraint(
            text("char_length(event_name) BETWEEN 3 AND 64"),
            name="ck_analytics_events_event_name_len",
        ),
        Index("idx_analytics_events_name_occurred_at", "event_name", "occurred_at"),
        Index("idx_analytics_events_user_id_occurred_at", "user_id", "occurred_at"),
        Index("idx_analytics_events_project_id", "project_id"),
    )

    event_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="事件名")
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="用户 ID（弱引用）")
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="项目 ID（弱引用）")
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="漏斗会话 ID")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        comment="事件发生时间（UTC）",
    )
    event_props: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="事件属性 JSONB"
    )

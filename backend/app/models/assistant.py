"""AI 创作助手数据模型（PRD V6.4 第 6 章多智能体架构）。

本模块承载创作助手的 3 张表：
- ``assistant_conversations``：单项目单用户会话（含上下文快照与版本）；
- ``assistant_messages``：会话消息（用户 / 助手 / 系统），含 Agent 标识、
  动作渲染快照与自检结果；
- ``assistant_actions``：待人工确认的动作（HITL），参数只存服务端，
  客户端执行时仅提交 ``action_id``。

设计约定：
- 会话、消息、动作均按项目级联删除；
- 消息以 ``(conversation_id, client_message_id)`` 唯一约束实现幂等回放；
- 动作以 ``params_hash`` + ``status`` + ``expires_at`` 防止参数篡改与重放。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import IdMixin, SoftDeleteMixin, TimestampMixin

#: 消息角色枚举
_MESSAGE_ROLES = "'user', 'assistant', 'system'"
#: 消息状态枚举
_MESSAGE_STATUSES = "'ok', 'degraded', 'failed'"
#: 动作状态枚举
_ACTION_STATUSES = (
    "'pending', 'executing', 'accepted', 'succeeded', 'failed', 'cancelled', 'expired'"
)


class AssistantConversation(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """创作助手会话（表 assistant_conversations）。

    每个项目、每个用户仅保留一条存活会话（部分唯一索引），面板重开或刷新
    后通过 get-or-create 复用，实现跨刷新连续对话与待确认动作恢复。

    Attributes:
        project_id: 归属项目 ID（FK projects，级联删除）。
        user_id: 归属用户 ID（FK users，级联删除）。
        title: 会话标题（默认由首条消息摘要生成）。
        episode_no: 当前聚焦集号（≥1）。
        context_snapshot: 生成时的项目上下文快照（JSONB）。
        context_version: 上下文内容哈希前 12 位（用于前端判断新鲜度）。
        last_message_at: 最近消息时间（列表排序用）。
        deleted_at: 软删除时间（NULL 表示存活）。
    """

    __tablename__ = "assistant_conversations"
    __table_args__ = (
        CheckConstraint(text("episode_no >= 1"), name="ck_assistant_conversations_episode_no"),
        Index(
            "uq_assistant_conversations_project_user",
            "project_id",
            "user_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_assistant_conversations_user_updated", "user_id", "updated_at"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        comment="归属项目 ID",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="归属用户 ID",
    )
    title: Mapped[str] = mapped_column(
        String(100), nullable=False, server_default="新的对话", comment="会话标题"
    )
    episode_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), comment="当前聚焦集号"
    )
    context_snapshot: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="项目上下文快照"
    )
    context_version: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="上下文版本哈希"
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="最近消息时间",
    )


class AssistantMessage(IdMixin, TimestampMixin, Base):
    """助手消息（表 assistant_messages）。

    Attributes:
        conversation_id: 所属会话 ID（FK assistant_conversations，级联删除）。
        role: 角色 user/assistant/system。
        content: 消息正文（纯文本，保留换行）。
        agent_key: 应答 Agent 标识（10 个角色或 general）。
        intent: 意图标识（用于前端展示与统计）。
        actions: 动作渲染快照（JSONB 数组，与 assistant_actions 同步）。
        self_check: 自检结果（JSONB；含 passed/issues/rounds）。
        status: 消息状态 ok/degraded/failed。
        tokens_in: 输入 token 数。
        tokens_out: 输出 token 数。
        client_message_id: 客户端消息 ID（幂等键，助手消息为空）。
    """

    __tablename__ = "assistant_messages"
    __table_args__ = (
        CheckConstraint(f"role IN ({_MESSAGE_ROLES})", name="ck_assistant_messages_role"),
        CheckConstraint(f"status IN ({_MESSAGE_STATUSES})", name="ck_assistant_messages_status"),
        CheckConstraint(text("tokens_in >= 0"), name="ck_assistant_messages_tokens_in"),
        CheckConstraint(text("tokens_out >= 0"), name="ck_assistant_messages_tokens_out"),
        UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_assistant_messages_conversation_client",
        ),
        Index("idx_assistant_messages_conversation_id_id", "conversation_id", "id"),
    )

    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话 ID",
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="消息角色")
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="", comment="消息正文")
    agent_key: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="general", comment="应答 Agent 标识"
    )
    intent: Mapped[str | None] = mapped_column(String(48), nullable=True, comment="意图标识")
    actions: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="动作渲染快照"
    )
    self_check: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="自检结果"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="ok", comment="消息状态"
    )
    tokens_in: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="输入 token 数"
    )
    tokens_out: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="输出 token 数"
    )
    client_message_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="客户端消息 ID（幂等键）"
    )


class AssistantAction(IdMixin, TimestampMixin, Base):
    """助手动作提议（表 assistant_actions）。

    LLM 只能产出“动作提议”，真正的写操作必须经用户在面板确认后由
    服务端按本表参数执行（HITL）。参数不回流客户端，避免越权篡改。

    Attributes:
        message_id: 来源助手消息 ID（FK assistant_messages，级联删除）。
        conversation_id: 所属会话 ID（FK assistant_conversations，级联删除）。
        project_id: 归属项目 ID（FK projects，级联删除）。
        action_type: 动作类型（白名单常量）。
        params: 服务端动作参数（JSONB）。
        params_hash: 参数规范化哈希（执行时校验未被篡改）。
        status: 动作状态（pending/executing/accepted/succeeded/failed/cancelled/expired）。
        preview: 动作预览（标题/摘要/风险，供前端卡片展示）。
        result_ref: 执行结果引用（JSONB）。
        error_message: 失败原因（人类可读，不含密钥）。
        expires_at: 确认过期时间。
        executed_at: 实际执行时间。
    """

    __tablename__ = "assistant_actions"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_ACTION_STATUSES})", name="ck_assistant_actions_status"
        ),
        Index("idx_assistant_actions_project_id_status", "project_id", "status"),
        Index("idx_assistant_actions_message_id", "message_id"),
    )

    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("assistant_messages.id", ondelete="CASCADE"),
        nullable=False,
        comment="来源助手消息 ID",
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属会话 ID",
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        comment="归属项目 ID",
    )
    action_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="动作类型")
    params: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="动作参数"
    )
    params_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, comment="参数哈希")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", comment="动作状态"
    )
    preview: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="动作预览"
    )
    result_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="执行结果引用")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="确认过期时间"
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="执行时间"
    )

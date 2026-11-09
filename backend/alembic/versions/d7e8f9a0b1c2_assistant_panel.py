"""创建 AI 创作助手三张表（PRD V6.4 第 6 章多智能体架构）。

新增表：
- ``assistant_conversations``：单项目单用户会话（上下文快照 + 版本）；
- ``assistant_messages``：会话消息（含 Agent 标识、动作快照、自检结果）；
- ``assistant_actions``：待人工确认的动作（服务端参数 + 状态机）。

会话以 ``(project_id, user_id) WHERE deleted_at IS NULL`` 部分唯一索引保证
单活会话；消息以 ``(conversation_id, client_message_id)`` 唯一约束实现幂等。

Revision ID: d7e8f9a0b1c2
Revises: c3d4e5f6a7b8
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

#: 当前迁移版本号
revision: str = "d7e8f9a0b1c2"
#: 上一版本号
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建助手会话、消息、动作三张表及索引。

    Returns:
        无返回值。

    Raises:
        无（SQL 失败由 Alembic 事务回滚）。
    """
    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="归属项目 ID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="归属用户 ID"),
        sa.Column(
            "title", sa.String(100), nullable=False, server_default="新的对话", comment="会话标题"
        ),
        sa.Column("episode_no", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="当前聚焦集号"),
        sa.Column(
            "context_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="项目上下文快照",
        ),
        sa.Column("context_version", sa.String(16), nullable=True, comment="上下文版本哈希"),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="最近消息时间",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True, comment="软删除时间"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("episode_no >= 1", name="ck_assistant_conversations_episode_no"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_assistant_conversations_project_user",
        "assistant_conversations",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "idx_assistant_conversations_user_updated",
        "assistant_conversations",
        ["user_id", "updated_at"],
    )

    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False, comment="所属会话 ID"),
        sa.Column("role", sa.String(16), nullable=False, comment="消息角色"),
        sa.Column("content", sa.Text(), nullable=False, server_default="", comment="消息正文"),
        sa.Column(
            "agent_key", sa.String(32), nullable=False, server_default="general", comment="应答 Agent 标识"
        ),
        sa.Column("intent", sa.String(48), nullable=True, comment="意图标识"),
        sa.Column(
            "actions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="动作渲染快照",
        ),
        sa.Column("self_check", postgresql.JSONB(), nullable=True, comment="自检结果"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok", comment="消息状态"),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="输入 token"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="输出 token"),
        sa.Column("client_message_id", sa.String(64), nullable=True, comment="客户端消息 ID"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["assistant_conversations.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name="ck_assistant_messages_role"
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'degraded', 'failed')", name="ck_assistant_messages_status"
        ),
        sa.CheckConstraint("tokens_in >= 0", name="ck_assistant_messages_tokens_in"),
        sa.CheckConstraint("tokens_out >= 0", name="ck_assistant_messages_tokens_out"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "client_message_id",
            name="uq_assistant_messages_conversation_client",
        ),
    )
    op.create_index(
        "idx_assistant_messages_conversation_id_id",
        "assistant_messages",
        ["conversation_id", "id"],
    )

    op.create_table(
        "assistant_actions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False, comment="来源助手消息 ID"),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False, comment="所属会话 ID"),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="归属项目 ID"),
        sa.Column("action_type", sa.String(32), nullable=False, comment="动作类型"),
        sa.Column(
            "params",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="动作参数",
        ),
        sa.Column("params_hash", sa.CHAR(64), nullable=False, comment="参数哈希"),
        sa.Column(
            "status", sa.String(16), nullable=False, server_default="pending", comment="动作状态"
        ),
        sa.Column(
            "preview",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="动作预览",
        ),
        sa.Column("result_ref", postgresql.JSONB(), nullable=True, comment="执行结果引用"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="失败原因"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, comment="确认过期时间"),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True, comment="执行时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["assistant_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["assistant_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('pending', 'executing', 'accepted', 'succeeded', 'failed', 'cancelled', 'expired')",
            name="ck_assistant_actions_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_assistant_actions_project_id_status",
        "assistant_actions",
        ["project_id", "status"],
    )
    op.create_index("idx_assistant_actions_message_id", "assistant_actions", ["message_id"])


def downgrade() -> None:
    """逆序删除助手动作、消息、会话三张表。

    Returns:
        无返回值。

    Raises:
        无（SQL 失败由 Alembic 事务回滚）。
    """
    op.drop_index("idx_assistant_actions_message_id", table_name="assistant_actions")
    op.drop_index("idx_assistant_actions_project_id_status", table_name="assistant_actions")
    op.drop_table("assistant_actions")

    op.drop_index("idx_assistant_messages_conversation_id_id", table_name="assistant_messages")
    op.drop_table("assistant_messages")

    op.drop_index("idx_assistant_conversations_user_updated", table_name="assistant_conversations")
    op.drop_index(
        "uq_assistant_conversations_project_user", table_name="assistant_conversations"
    )
    op.drop_table("assistant_conversations")

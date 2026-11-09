"""为镜头增加剧本锚点列（剧情一致性治本项）。

背景（2026-10-29 剧情与剧本偏差归因排查）：分镜环节此前只落
``dialogue_refs``（对白行号），镜头与剧本原文之间缺少可追溯的语义锚点，
分镜自由发挥、生成 prompt 引入剧本外内容时无法定位与拦截。新增
``shots.script_anchor``（JSONB）保存：

- ``script_text``：本镜对应的剧本原文句子（可多句）；
- ``plot_function``：剧情功能标签（hook/development/escalation/twist/transition）；
- ``cause_from_prev``：与前一镜头的因果衔接说明。

变更内容：``shots`` 新增 ``script_anchor``（JSONB, nullable），历史数据
保持 NULL（渲染层按 dialogue_refs/action_desc 确定性兜底，不阻塞旧数据）。

Revision ID: a8b9c0d1e2f3
Revises: f2a3b4c5d6e7
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

#: 当前迁移版本号
revision: str = "a8b9c0d1e2f3"
#: 上一版本号
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增 shots.script_anchor 列。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.add_column(
        "shots",
        sa.Column(
            "script_anchor",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="剧本锚点（script_text 剧本原文句 + plot_function 剧情功能标签 + cause_from_prev 与前镜因果）",
        ),
    )


def downgrade() -> None:
    """移除 shots.script_anchor 列。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.drop_column("shots", "script_anchor")

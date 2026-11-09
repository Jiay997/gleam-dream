"""渲染任务补齐音画对齐列（F5.13 / P2-10）。

背景：交付说明 F5.13 声称渲染任务会写入 ``voice_clip_ids`` /
``voice_line_ids`` / ``line_duration_ms`` / ``duration_mismatch`` 四列，
实际这些数据只冗余在 ``render_tasks.input_params`` JSONB 中，表结构缺失，
导致外部按列查询/统计失败。本迁移补齐四列（均可空，历史数据不受影响）。

Revision ID: a1b2c3d4e5f6
Revises: d7e8f9a0b1c2
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

#: 当前迁移版本号
revision: str = "a1b2c3d4e5f6"
#: 上一版本号（AI 创作助手表）
down_revision: str | None = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为 render_tasks 增加音画对齐四列。

    Returns:
        无返回值。

    Raises:
        无（SQL 失败由 Alembic 事务回滚）。
    """
    op.add_column(
        "render_tasks",
        sa.Column(
            "voice_clip_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="绑定配音片段 ID 列表（F5.13）",
        ),
    )
    op.add_column(
        "render_tasks",
        sa.Column(
            "voice_line_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="绑定台词 ID 列表（F5.13）",
        ),
    )
    op.add_column(
        "render_tasks",
        sa.Column(
            "line_duration_ms",
            sa.Integer(),
            nullable=True,
            comment="台词总跨度毫秒（F5.13）",
        ),
    )
    op.add_column(
        "render_tasks",
        sa.Column(
            "duration_mismatch",
            sa.Boolean(),
            nullable=True,
            comment="镜头时长与台词跨度是否超容差（F5.13）",
        ),
    )


def downgrade() -> None:
    """回滚：删除音画对齐四列。

    Returns:
        无返回值。

    Raises:
        无（SQL 失败由 Alembic 事务回滚）。
    """
    op.drop_column("render_tasks", "duration_mismatch")
    op.drop_column("render_tasks", "line_duration_ms")
    op.drop_column("render_tasks", "voice_line_ids")
    op.drop_column("render_tasks", "voice_clip_ids")

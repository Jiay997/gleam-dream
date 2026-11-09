"""为镜头增加整幅要素画面构图列。

背景：分镜只落「要素绑定 + 长 prompt」，渲染层从资产图中择一充当画面，
导致同一张定妆照被多镜复用、场景与道具从不参与构图。新增
``shots.composition_spec`` 保存 CompositionSpec（画幅/景别/角色位置/道具
归属/字幕安全区），供本地合成器产出 9:16 整幅要素帧。

变更内容：``shots`` 新增 ``composition_spec``（JSONB, nullable），历史
数据保持 NULL（由服务层回落规则推导，不伪造历史构图）。

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-10-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

#: 当前迁移版本号
revision: str = "f2a3b4c5d6e7"
#: 上一版本号
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增 shots.composition_spec 列。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.add_column(
        "shots",
        sa.Column(
            "composition_spec",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="整幅要素画面构图（CompositionSpec，9:16 场景+角色+道具布局）",
        ),
    )


def downgrade() -> None:
    """移除 shots.composition_spec 列。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.drop_column("shots", "composition_spec")

"""为句级配音产物增加情绪标签持久化列。

背景：V6 配音在生成时会把剧本行的情绪标签传给 TTS 引擎（真实 Qwen3-TTS
通过语速/音调后处理体现情绪），但 ``voice_clips`` 表未落库该字段，配音详情
接口也无法返回，导致：

- 页面无法展示/复核每句配音实际使用的情绪；
- 「配音人设契合度与情绪表达准确性」缺少可验证证据；
- 单句重配传入的新情绪无法与历史记录对比。

变更内容：``voice_clips`` 新增 ``emotion``（String(32), nullable），
历史数据保持 NULL（不回填，避免伪造已生效情绪）。

Revision ID: e1f2a3b4c5d6
Revises: d9e1f2a3b4c5
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

#: 当前迁移版本号
revision: str = "e1f2a3b4c5d6"
#: 上一版本号
down_revision: str | None = "d9e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增 voice_clips.emotion 列。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.add_column(
        "voice_clips",
        sa.Column(
            "emotion",
            sa.String(length=32),
            nullable=True,
            comment="实际使用的情绪标签",
        ),
    )


def downgrade() -> None:
    """移除 voice_clips.emotion 列。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.drop_column("voice_clips", "emotion")

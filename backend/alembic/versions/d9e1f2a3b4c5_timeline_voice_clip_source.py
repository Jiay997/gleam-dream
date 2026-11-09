"""扩展时间线音频来源，支持 V6 配音片段（voice_clip）。

背景：V6 配音页将合成音频写入 ``voice_clips``。后期合成需要把它写入
``timeline_clips``（track_type=audio），但历史检查约束只允许
``shot_version/media_file/bgm/tts``，导致合成为 500、成片无配音。

变更内容：重建 ``ck_timeline_clips_source_type``，额外允许 ``voice_clip``。

Revision ID: d9e1f2a3b4c5
Revises: a1b2c3d4e5f6
Create Date: 2026-10-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

#: 当前迁移版本号
revision: str = "d9e1f2a3b4c5"
#: 上一版本号
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_VALUES = "'shot_version', 'media_file', 'bgm', 'tts'"
_NEW_VALUES = "'shot_version', 'media_file', 'bgm', 'tts', 'voice_clip'"


def upgrade() -> None:
    """重建来源检查约束，允许 voice_clip。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.drop_constraint("ck_timeline_clips_source_type", "timeline_clips", type_="check")
    op.create_check_constraint(
        "ck_timeline_clips_source_type",
        "timeline_clips",
        f"source_type IS NULL OR source_type IN ({_NEW_VALUES})",
    )


def downgrade() -> None:
    """恢复旧约束（先清理 voice_clip 片段以避免校验失败）。

    Returns:
        无。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: DDL 执行失败时由 Alembic 回滚。
    """
    op.execute("DELETE FROM timeline_clips WHERE source_type = 'voice_clip'")
    op.drop_constraint("ck_timeline_clips_source_type", "timeline_clips", type_="check")
    op.create_check_constraint(
        "ck_timeline_clips_source_type",
        "timeline_clips",
        f"source_type IS NULL OR source_type IN ({_OLD_VALUES})",
    )

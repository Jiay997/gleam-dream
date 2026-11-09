"""create analytics_events

Revision ID: c5d9e2f8a413
Revises: a7c1d2e94b05
Create Date: 2026-11-05 11:00:00.000000

背景（09_MVP验收总结报告 7.1 P0）：埋点最小集——注册 / 激活 / 四关口
通过率 / 导出漏斗。北极星指标 CCCR 与 M1–M13 的观测数据源。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c5d9e2f8a413'
down_revision: Union[str, None] = 'a7c1d2e94b05'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：创建 analytics_events 只追加埋点表。"""
    op.create_table('analytics_events',
    sa.Column('event_name', sa.String(length=64), nullable=False, comment='事件名'),
    sa.Column('user_id', sa.BigInteger(), nullable=True, comment='用户 ID（弱引用）'),
    sa.Column('project_id', sa.BigInteger(), nullable=True, comment='项目 ID（弱引用）'),
    sa.Column('session_id', sa.String(length=64), nullable=True, comment='漏斗会话 ID'),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='事件发生时间（UTC）'),
    sa.Column('event_props', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='事件属性 JSONB'),
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
    sa.CheckConstraint("char_length(event_name) BETWEEN 3 AND 64", name='ck_analytics_events_event_name_len'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_analytics_events'))
    )
    op.create_index('idx_analytics_events_name_occurred_at', 'analytics_events', ['event_name', 'occurred_at'], unique=False)
    op.create_index('idx_analytics_events_project_id', 'analytics_events', ['project_id'], unique=False)
    op.create_index('idx_analytics_events_user_id_occurred_at', 'analytics_events', ['user_id', 'occurred_at'], unique=False)


def downgrade() -> None:
    """降级：删除 analytics_events 表。"""
    op.drop_index('idx_analytics_events_user_id_occurred_at', table_name='analytics_events')
    op.drop_index('idx_analytics_events_project_id', table_name='analytics_events')
    op.drop_index('idx_analytics_events_name_occurred_at', table_name='analytics_events')
    op.drop_table('analytics_events')

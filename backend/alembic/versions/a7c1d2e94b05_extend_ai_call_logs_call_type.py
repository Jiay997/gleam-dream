"""extend ai_call_logs call_type with llm_text

Revision ID: a7c1d2e94b05
Revises: 4182cfc18eda
Create Date: 2026-11-05 10:00:00.000000

背景（09_MVP验收总结报告 · 网关已知限制）：
``ai_call_logs.call_type`` 原 CHECK 仅允许 9 种渲染任务类型，LLM 文本调用
（llm_text）被迫以 ``compose`` 承载，真实任务类型只能埋在 request_snapshot
里，导致模型成本观测（埋点最小集/模型调用 P95）无法按调用类型直接聚合。
本迁移将 ``llm_text`` 纳入 CHECK 约束，网关同步改为原样落库。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c1d2e94b05'
down_revision: Union[str, None] = '4182cfc18eda'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_CONSTRAINT = (
    "call_type IN ('text_to_image', 'image_to_image', 'text_to_video', "
    "'image_to_video', 'tts', 'bgm', 'subtitle', 'compose', 'upscale')"
)
_NEW_CONSTRAINT = (
    "call_type IN ('text_to_image', 'image_to_image', 'text_to_video', "
    "'image_to_video', 'tts', 'bgm', 'subtitle', 'compose', 'upscale', 'llm_text')"
)


def upgrade() -> None:
    """升级：CHECK 约束纳入 llm_text（幂等安全：先删后建）。"""
    op.drop_constraint('ck_ai_call_logs_call_type', 'ai_call_logs', type_='check')
    op.create_check_constraint('ck_ai_call_logs_call_type', 'ai_call_logs', _NEW_CONSTRAINT)


def downgrade() -> None:
    """降级：恢复 9 值约束（要求表中无 llm_text 行，存在则需先清理）。"""
    op.drop_constraint('ck_ai_call_logs_call_type', 'ai_call_logs', type_='check')
    op.create_check_constraint('ck_ai_call_logs_call_type', 'ai_call_logs', _OLD_CONSTRAINT)

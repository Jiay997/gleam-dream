"""add model config fields to model_registry

Revision ID: e9a2c7d4b310
Revises: d4e8b1f60c27
Create Date: 2026-10-21 20:00:00.000000

背景（模型配置功能）：
在 ``model_registry`` 上补充"可配置接入参数"，使模型名称 / 接口地址 / 密钥等
可在应用内维护，并由模型网关在每次调用时实时读取（改动即时生效，无需重启）：

1. ``protocol``    调用协议（openai_chat / dashscope_image / dashscope_video /
                   dashscope_tts / stub；为空时回退内置提供商实例）；
2. ``base_url``    接口地址（TTS 行存 WebSocket 地址）；
3. ``api_key``     访问密钥（仅后端使用，接口下发时掩码）；
4. ``api_model``   三方 API 模型名（如 qwen-image-3.0）；
5. ``extra_config`` 扩展参数 JSONB（如 TTS 音色 voice）。

均为可空列：历史行不回填也能继续按原 provider 工作，回填由
``scripts/seed_model_configs.py`` 幂等完成。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e9a2c7d4b310'
down_revision: Union[str, None] = 'd4e8b1f60c27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """升级：model_registry 增加 5 个可配置接入参数列。"""
    op.add_column(
        'model_registry',
        sa.Column('protocol', sa.String(length=32), nullable=True, comment='调用协议（openai_chat/dashscope_image/...）'),
    )
    op.add_column(
        'model_registry',
        sa.Column('base_url', sa.String(length=512), nullable=True, comment='接口地址（HTTP 基址或 TTS WebSocket 地址）'),
    )
    op.add_column(
        'model_registry',
        sa.Column('api_key', sa.String(length=1024), nullable=True, comment='访问密钥（仅后端使用，接口下发时掩码）'),
    )
    op.add_column(
        'model_registry',
        sa.Column('api_model', sa.String(length=128), nullable=True, comment='三方 API 模型名（如 qwen-image-3.0）'),
    )
    op.add_column(
        'model_registry',
        sa.Column('extra_config', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='扩展参数（音色等）'),
    )


def downgrade() -> None:
    """降级：移除 model_registry 的可配置接入参数列。"""
    op.drop_column('model_registry', 'extra_config')
    op.drop_column('model_registry', 'api_model')
    op.drop_column('model_registry', 'api_key')
    op.drop_column('model_registry', 'base_url')
    op.drop_column('model_registry', 'protocol')

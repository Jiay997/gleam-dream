"""切换语音模型到千问 Qwen3-TTS-Flash（HTTP 同步合成）。

变更内容：
1. 新增 ``qwen-tts`` 模型注册行（协议 ``qwen_tts``，模型
   ``qwen3-tts-flash``，基址 ``https://dashscope.aliyuncs.com``，默认音色
   Cherry）；API Key 由 ``backend/.env`` 的 ``QWEN_TTS_API_KEY`` 提供；
2. ``tts`` 任务路由 priority 1 切到 ``qwen-tts``，百炼 ``bailian-tts``
   降为 priority 2，``stub-tts`` 降为 priority 3 作为兜底。

语音合成请求形态（已实测中文成功）::

    POST /api/v1/services/aigc/multimodal-generation/generation
    {"model": "qwen3-tts-flash",
     "input": {"text": "...", "voice": "Cherry"},
     "parameters": {"language_type": "Chinese"}}
    -> output.audio.url（WAV）

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

#: 当前迁移版本号
revision: str = "c3d4e5f6a7b8"
#: 上一版本号
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """写入 qwen-tts 模型行并切换语音路由。

    Returns:
        无返回值。

    Raises:
        无（SQL 失败由 Alembic 事务回滚）。
    """
    op.execute(
        """
        INSERT INTO model_registry
            (model_key, model_name, provider, capability, quality_score, unit,
             unit_price_credits, max_concurrency, is_available, protocol, base_url,
             api_model, extra_config)
        VALUES
            ('qwen-tts', 'Qwen3-TTS-Flash', 'qwen-tts', 'tts', 9.20, 'per_second',
             2, 4, true, 'qwen_tts', 'https://dashscope.aliyuncs.com',
             'qwen3-tts-flash', '{"voice": "Cherry"}'::jsonb)
        ON CONFLICT (model_key) DO UPDATE SET
            provider = EXCLUDED.provider,
            capability = EXCLUDED.capability,
            quality_score = EXCLUDED.quality_score,
            unit = EXCLUDED.unit,
            unit_price_credits = EXCLUDED.unit_price_credits,
            is_available = true,
            protocol = EXCLUDED.protocol,
            base_url = EXCLUDED.base_url,
            api_model = EXCLUDED.api_model,
            extra_config = EXCLUDED.extra_config
        """
    )

    # 先腾出 priority 3，再把百炼降到 2，最后落位 qwen-tts 为 1
    op.execute(
        "UPDATE model_routes SET priority = 3 WHERE task_type = 'tts' "
        "AND model_key = 'stub-tts'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 2 WHERE task_type = 'tts' "
        "AND model_key = 'bailian-tts'"
    )
    op.execute(
        """
        INSERT INTO model_routes (task_type, priority, model_key, is_fallback, is_active)
        VALUES ('tts', 1, 'qwen-tts', false, true)
        ON CONFLICT (task_type, priority) DO UPDATE SET
            model_key = EXCLUDED.model_key,
            is_fallback = EXCLUDED.is_fallback,
            is_active = true
        """
    )


def downgrade() -> None:
    """回退语音路由到百炼并删除 qwen-tts 模型行。

    Returns:
        无返回值。

    Raises:
        无（SQL 失败由 Alembic 事务回滚）。
    """
    op.execute("DELETE FROM model_routes WHERE task_type = 'tts' AND model_key = 'qwen-tts'")
    op.execute(
        "UPDATE model_routes SET priority = 1 WHERE task_type = 'tts' "
        "AND model_key = 'bailian-tts'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 2 WHERE task_type = 'tts' "
        "AND model_key = 'stub-tts'"
    )
    op.execute("DELETE FROM model_registry WHERE model_key = 'qwen-tts'")

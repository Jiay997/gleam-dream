"""Agnes AI 图片/视频模型接入与媒体路由切换。

变更内容：
1. 修复被测试夹具残留的百炼媒体行协议（``stub`` → ``dashscope_*``）；
2. 新增 ``agnes-image`` / ``agnes-video`` 两行模型注册（协议
   ``agnes_image`` / ``agnes_video``，密钥与基址回退 ``.env``）；
3. 媒体任务路由优先级切到 Agnes：``text_to_image`` / ``image_to_image``
   首选 ``agnes-image``，``text_to_video`` / ``image_to_video`` 首选
   ``agnes-video``，百炼与 stub 顺延为降级候选；
4. 文本任务首选改为 ``glm-5.3-flash``（``deepseek-flash`` 降为备选）。

语音（tts）保持不变：当前 Agnes API Key 分组无可用 TTS 模型，继续由百炼
``dashscope_tts`` 承担。

Revision ID: b2c3d4e5f6a7
Revises: f1a2b3c4d5e6
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

#: 当前迁移版本号
revision: str = "b2c3d4e5f6a7"
#: 上一版本号
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """写入 Agnes 模型行并把媒体/文本路由切换到新模型。

    Returns:
        无返回值。

    Raises:
        无（SQL 失败由 Alembic 事务回滚）。
    """
    # 1) 恢复百炼媒体行的真实协议（历史测试中断曾把 protocol 写成 stub）
    op.execute(
        """
        UPDATE model_registry SET protocol = 'dashscope_image'
        WHERE model_key = 'bailian-image' AND (protocol IS NULL OR protocol = 'stub')
        """
    )
    op.execute(
        """
        UPDATE model_registry SET protocol = 'dashscope_video'
        WHERE model_key = 'bailian-video' AND (protocol IS NULL OR protocol = 'stub')
        """
    )
    op.execute(
        """
        UPDATE model_registry SET protocol = 'dashscope_tts'
        WHERE model_key = 'bailian-tts' AND (protocol IS NULL OR protocol = 'stub')
        """
    )

    # 2) 新增 Agnes 图片/视频模型行（幂等：已存在则更新接入参数）
    op.execute(
        """
        INSERT INTO model_registry
            (model_key, model_name, provider, capability, quality_score, unit,
             unit_price_credits, max_concurrency, is_available, protocol, base_url, api_model)
        VALUES
            ('agnes-image', 'Agnes Image 2.5 Flash', 'agnes', 'image', 9.00, 'per_image',
             15, 4, true, 'agnes_image', 'https://apihub.agnes-ai.com/v1', 'agnes-image-2.5-flash')
        ON CONFLICT (model_key) DO UPDATE SET
            provider = EXCLUDED.provider,
            capability = EXCLUDED.capability,
            quality_score = EXCLUDED.quality_score,
            unit = EXCLUDED.unit,
            unit_price_credits = EXCLUDED.unit_price_credits,
            is_available = true,
            protocol = EXCLUDED.protocol,
            base_url = EXCLUDED.base_url,
            api_model = EXCLUDED.api_model
        """
    )
    op.execute(
        """
        INSERT INTO model_registry
            (model_key, model_name, provider, capability, quality_score, unit,
             unit_price_credits, max_concurrency, is_available, protocol, base_url, api_model)
        VALUES
            ('agnes-video', 'Agnes Video 2.5 Flash', 'agnes', 'video', 8.80, 'per_second',
             40, 4, true, 'agnes_video', 'https://apihub.agnes-ai.com/v1', 'agnes-video-2.5-flash')
        ON CONFLICT (model_key) DO UPDATE SET
            provider = EXCLUDED.provider,
            capability = EXCLUDED.capability,
            quality_score = EXCLUDED.quality_score,
            unit = EXCLUDED.unit,
            unit_price_credits = EXCLUDED.unit_price_credits,
            is_available = true,
            protocol = EXCLUDED.protocol,
            base_url = EXCLUDED.base_url,
            api_model = EXCLUDED.api_model
        """
    )

    # 3) 媒体路由顺延：先腾出 priority 3/4，再把百炼降到 2，最后插入 Agnes 为 1
    op.execute(
        "UPDATE model_routes SET priority = 4 WHERE task_type IN "
        "('text_to_image', 'image_to_image') AND model_key = 'stub-image'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 3 WHERE task_type IN "
        "('text_to_image', 'image_to_image') AND model_key = 'stub-image-hd'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 2 WHERE task_type IN "
        "('text_to_image', 'image_to_image') AND model_key = 'bailian-image'"
    )
    op.execute(
        """
        INSERT INTO model_routes (task_type, priority, model_key, is_fallback, is_active)
        VALUES
            ('text_to_image', 1, 'agnes-image', false, true),
            ('image_to_image', 1, 'agnes-image', false, true)
        ON CONFLICT (task_type, priority) DO UPDATE SET
            model_key = EXCLUDED.model_key,
            is_fallback = EXCLUDED.is_fallback,
            is_active = true
        """
    )

    op.execute(
        "UPDATE model_routes SET priority = 4 WHERE task_type IN "
        "('text_to_video', 'image_to_video') AND model_key = 'stub-video'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 3 WHERE task_type IN "
        "('text_to_video', 'image_to_video') AND model_key = 'bailian-video'"
    )
    op.execute(
        """
        INSERT INTO model_routes (task_type, priority, model_key, is_fallback, is_active)
        VALUES
            ('text_to_video', 1, 'agnes-video', false, true),
            ('image_to_video', 1, 'agnes-video', false, true)
        ON CONFLICT (task_type, priority) DO UPDATE SET
            model_key = EXCLUDED.model_key,
            is_fallback = EXCLUDED.is_fallback,
            is_active = true
        """
    )

    # 4) 文本首选 GLM-5.3-flash：先腾挪 priority 再落位，避免唯一约束冲突
    op.execute(
        "UPDATE model_routes SET priority = 3 WHERE task_type = 'llm_text' "
        "AND model_key = 'glm-5.3-flash'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 2 WHERE task_type = 'llm_text' "
        "AND model_key = 'deepseek-flash'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 1 WHERE task_type = 'llm_text' "
        "AND model_key = 'glm-5.3-flash'"
    )


def downgrade() -> None:
    """回退到百炼媒体模型与 deepseek 优先的文本路由。

    Returns:
        无返回值。

    Raises:
        无（SQL 失败由 Alembic 事务回滚）。
    """
    op.execute("DELETE FROM model_routes WHERE model_key IN ('agnes-image', 'agnes-video')")
    op.execute(
        "UPDATE model_routes SET priority = 1 WHERE task_type IN "
        "('text_to_image', 'image_to_image') AND model_key = 'bailian-image'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 2 WHERE task_type IN "
        "('text_to_image', 'image_to_image') AND model_key = 'stub-image-hd'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 3 WHERE task_type IN "
        "('text_to_image', 'image_to_image') AND model_key = 'stub-image'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 1 WHERE task_type IN "
        "('text_to_video', 'image_to_video') AND model_key = 'bailian-video'"
    )
    op.execute(
        "UPDATE model_routes SET priority = 2 WHERE task_type IN "
        "('text_to_video', 'image_to_video') AND model_key = 'stub-video'"
    )
    op.execute("UPDATE model_routes SET priority = 3 WHERE task_type = 'llm_text' "
               "AND model_key = 'glm-5.3-flash'")
    op.execute("UPDATE model_routes SET priority = 2 WHERE task_type = 'llm_text' "
               "AND model_key = 'deepseek-flash'")
    op.execute("UPDATE model_routes SET priority = 1 WHERE task_type = 'llm_text' "
               "AND model_key = 'deepseek-flash'")
    op.execute("UPDATE model_routes SET priority = 2 WHERE task_type = 'llm_text' "
               "AND model_key = 'glm-5.3-flash'")
    op.execute("DELETE FROM model_registry WHERE model_key IN ('agnes-image', 'agnes-video')")

"""域 G · 生成执行（Render）模型。

包含 6 张表（对齐《02 数据库设计文档》§5.7 与 §13.5 V4.0 增量）：
- ``model_registry`` 模型注册表（能力/单价/质量分/并发）；
- ``model_routes`` 路由策略（任务类型 → 候选模型优先级）；
- ``render_batches`` 渲染批次（策略档/插队额度/80% 预警防重发，V4.0）；
- ``render_tasks`` 渲染任务（状态机 + 重试计数 + 冻结结算）；
- ``render_outputs`` 渲染产物（关联 media_files）；
- ``ai_call_logs`` 模型调用日志（token/时长/成本/trace_id，只追加）。

说明：``model_routes.task_type`` 除 9 种渲染任务类型外额外允许 ``llm_text``，
用于 Agent 文本生成的模型路由（种子数据要求，见 00 契约 Q2）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import IdMixin, TimestampMixin

# 渲染任务类型 9 值（对齐 00 契约 §6.7）
_RENDER_TASK_TYPE_VALUES = (
    "'text_to_image', 'image_to_image', 'text_to_video', 'image_to_video', "
    "'tts', 'bgm', 'subtitle', 'compose', 'upscale'"
)
# model_routes 额外支持 Agent 文本生成路由
_ROUTE_TASK_TYPE_VALUES = f"{_RENDER_TASK_TYPE_VALUES}, 'llm_text'"
# ai_call_logs 额外承载 LLM 文本调用（ADR-07 网关统一记账）
_AI_CALL_LOG_TYPE_VALUES = f"{_RENDER_TASK_TYPE_VALUES}, 'llm_text'"


class ModelRegistry(IdMixin, TimestampMixin, Base):
    """模型注册表：三方模型能力、单价、质量分、并发（全局主数据，表 model_registry）。

    Attributes:
        model_key: 模型键（唯一）。
        model_name: 模型名。
        provider: 提供方。
        capability: 能力 image/video/tts/llm/bgm/subtitle。
        task_types: 支持任务类型（JSONB 字符串数组）。
        quality_score: 质量分（0.00~10.00）。
        unit: 计费单位 per_image/per_second/per_1k_token/per_call。
        unit_price_credits: 参考单价（积分，≥0）。
        avg_latency_ms: 平均时延（毫秒）。
        max_concurrency: 最大并发（≥1）。
        is_available: 是否可用。
        region: 部署地域（如 cn）。
        version_tag: 模型版本标签。
        protocol: 调用协议（模型配置页维护；为空时回退内置提供商）。
        base_url: 接口地址（HTTP 基址；TTS 行存 WebSocket 地址）。
        api_key: 访问密钥（仅后端使用，接口下发时掩码）。
        api_model: 三方 API 模型名（如 qwen-image-3.0）。
        extra_config: 扩展参数 JSONB（如音色 voice）。
    """

    __tablename__ = "model_registry"
    __table_args__ = (
        CheckConstraint(
            text("capability IN ('image', 'video', 'tts', 'llm', 'bgm', 'subtitle')"),
            name="ck_model_registry_capability",
        ),
        CheckConstraint(
            text("unit IN ('per_image', 'per_second', 'per_1k_token', 'per_call')"),
            name="ck_model_registry_unit",
        ),
        CheckConstraint(
            text("quality_score IS NULL OR quality_score BETWEEN 0 AND 10"),
            name="ck_model_registry_quality_score",
        ),
        CheckConstraint(text("unit_price_credits >= 0"), name="ck_model_registry_unit_price"),
        CheckConstraint(text("max_concurrency >= 1"), name="ck_model_registry_max_concurrency"),
        UniqueConstraint("model_key", name="uq_model_registry_model_key"),
        Index("idx_model_registry_capability_is_available", "capability", "is_available"),
    )

    model_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="模型键")
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="模型名")
    provider: Mapped[str] = mapped_column(String(64), nullable=False, comment="提供方")
    capability: Mapped[str] = mapped_column(String(32), nullable=False, comment="能力（6 值）")
    task_types: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="支持任务类型数组")
    quality_score: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 2), nullable=True, comment="质量分 0.00~10.00"
    )
    unit: Mapped[str] = mapped_column(String(32), nullable=False, comment="计费单位（4 值）")
    unit_price_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="参考单价（积分）"
    )
    avg_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="平均时延（毫秒）")
    max_concurrency: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("4"), comment="最大并发"
    )
    is_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否可用"
    )
    region: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="部署地域")
    version_tag: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="模型版本标签")

    # ---- 可配置接入参数（模型配置页维护；网关每次调用实时读取，改动即时生效）----
    protocol: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="调用协议（openai_chat/dashscope_image/...）"
    )
    base_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="接口地址（HTTP 基址或 TTS WebSocket 地址）"
    )
    api_key: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="访问密钥（仅后端使用，接口下发时掩码）"
    )
    api_model: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="三方 API 模型名（如 qwen-image-3.0）"
    )
    extra_config: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="扩展参数（音色等）"
    )


class ModelRoute(IdMixin, TimestampMixin, Base):
    """路由策略：任务类型到候选模型的确定性优先级（表 model_routes）。

    ``model_key`` 与 model_registry 为逻辑关联（无物理外键）。

    Attributes:
        task_type: 任务类型（9 种渲染任务 + llm_text）。
        priority: 优先级（1~20，任务内唯一，1 最高）。
        model_key: 模型键。
        condition: 选用条件 JSONB（如 {"style":"anime"}）。
        is_fallback: 是否兜底候选。
        is_active: 是否启用。
    """

    __tablename__ = "model_routes"
    __table_args__ = (
        CheckConstraint(
            text(f"task_type IN ({_ROUTE_TASK_TYPE_VALUES})"),
            name="ck_model_routes_task_type",
        ),
        CheckConstraint(text("priority BETWEEN 1 AND 20"), name="ck_model_routes_priority"),
        UniqueConstraint("task_type", "priority", name="uq_model_routes_task_type_priority"),
        Index("idx_model_routes_task_type_is_active_priority", "task_type", "is_active", "priority"),
    )

    task_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="任务类型")
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="优先级 1~20")
    model_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="模型键")
    condition: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="选用条件")
    is_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否兜底候选"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否启用"
    )


class RenderTask(IdMixin, TimestampMixin, Base):
    """渲染任务：生成执行任务，含状态机、重试计数、预估/实际成本与幂等键（表 render_tasks）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        storyboard_id: 分镜板 ID（FK storyboards，SET NULL）。
        shot_id: 镜头 ID（FK shots，SET NULL）。
        task_type: 任务类型（9 值）。
        status: 状态（9 值，见 00 契约 §6.5）。
        priority: 优先级（1 最高 ~ 9 最低，默认 5）。
        model_key: 选用模型键（逻辑关联 model_registry）。
        retry_count: 已重试次数（≥0）。
        max_retries: 最大重试次数（默认 2，对齐 F5）。
        estimated_cost_credits: 预估成本（积分，≥0）。
        actual_cost_credits: 实际成本（积分，≥0）。
        frozen_credits: 已冻结积分（≥0）。
        idempotency_key: 幂等键（非空时唯一）。
        input_params: 入参 JSONB（prompt/尺寸/时长等）。
        input_hash: 入参哈希（SHA-256）。
        voice_clip_ids: 本镜绑定的配音片段 ID 列表（F5.13 音画对齐，P2-10 补齐）。
        voice_line_ids: 本镜绑定的台词 ID 列表（F5.13）。
        line_duration_ms: 台词总跨度毫秒（F5.13）。
        duration_mismatch: 镜头时长与台词跨度是否超容差（F5.13）。
        failure_type: 失败类型（6 值）。
        failure_reason: 失败详情。
        priority_source: 优先级来源 first3/queue_jump/batch_order（AC-F5-01-3，V4.0）。
        manual_pass: 是否被人工标记合格强制通过（AC-F5-09-1，V4.0）。
        manual_pass_reason: 放行理由（≥5 字，AC-F5-09-1，V4.0）。
        manual_pass_by: 放行操作人用户 ID（V4.0）。
        manual_pass_at: 放行时间（V4.0）。
        user_language_error: 失败原因的用户语言文案（AC-F5-02-4，V4.0）。
        celery_task_id: 队列任务 ID。
        agent_run_id: 触发运行 ID（弱引用 agent_runs）。
        queued_at: 入队时间。
        started_at: 开始时间。
        finished_at: 完成时间。
    """

    __tablename__ = "render_tasks"
    __table_args__ = (
        CheckConstraint(
            text(f"task_type IN ({_RENDER_TASK_TYPE_VALUES})"),
            name="ck_render_tasks_task_type",
        ),
        CheckConstraint(
            text(
                "status IN ('pending', 'queued', 'running', 'qc_review', 'succeeded', "
                "'failed', 'cancelled', 'retrying', 'manual_required', 'abandoned', "
                "'moderation_rejected')"
            ),
            name="ck_render_tasks_status",
        ),
        CheckConstraint(text("priority BETWEEN 1 AND 9"), name="ck_render_tasks_priority"),
        CheckConstraint(text("retry_count >= 0"), name="ck_render_tasks_retry_count_min"),
        CheckConstraint(
            text("retry_count <= max_retries + 3"),
            name="ck_render_tasks_retry_count",
        ),
        CheckConstraint(
            text("estimated_cost_credits >= 0"), name="ck_render_tasks_estimated_cost"
        ),
        CheckConstraint(
            text(
                "failure_type IS NULL OR failure_type IN ('timeout', 'rate_limited', "
                "'moderation_rejected', 'param_invalid', 'model_unavailable', 'unknown')"
            ),
            name="ck_render_tasks_failure_type",
        ),
        CheckConstraint(
            text(
                "priority_source IS NULL OR priority_source IN "
                "('first3', 'queue_jump', 'batch_order')"
            ),
            name="ck_render_tasks_priority_source",
        ),
        CheckConstraint(
            text(
                "manual_pass_reason IS NULL OR char_length(manual_pass_reason) <= 100"
            ),
            name="ck_render_tasks_manual_pass_reason_len",
        ),
        Index(
            "uq_render_tasks_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("idx_render_tasks_status_priority_queued_at", "status", "priority", "queued_at"),
        Index("idx_render_tasks_shot_id", "shot_id"),
        Index("idx_render_tasks_project_id_status", "project_id", "status"),
        Index("idx_render_tasks_celery_task_id", "celery_task_id"),
        Index("idx_render_tasks_created_at", "created_at"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    storyboard_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("storyboards.id", ondelete="SET NULL"), nullable=True, comment="分镜板 ID"
    )
    shot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shots.id", ondelete="SET NULL"), nullable=True, comment="镜头 ID"
    )
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="任务类型（9 值）")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending", comment="任务状态（9 值）"
    )
    priority: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("5"), comment="优先级 1~9"
    )
    model_key: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="选用模型键")
    retry_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="已重试次数"
    )
    max_retries: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("2"), comment="最大重试次数"
    )
    estimated_cost_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="预估成本（积分）"
    )
    actual_cost_credits: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="实际成本（积分）")
    frozen_credits: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="已冻结积分")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="幂等键")
    input_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="入参")
    input_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True, comment="入参哈希")
    # ---- P2-10：F5.13 音画对齐列（此前文档声称写入列，实际仅存在 input_params）----
    voice_clip_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="绑定配音片段 ID 列表（F5.13）"
    )
    voice_line_ids: Mapped[list | None] = mapped_column(
        JSONB, nullable=True, comment="绑定台词 ID 列表（F5.13）"
    )
    line_duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="台词总跨度毫秒（F5.13）"
    )
    duration_mismatch: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="镜头时长与台词跨度是否超容差（F5.13）"
    )
    failure_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="失败类型（6 值）")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败详情")
    priority_source: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="优先级来源（3 值，V4.0）"
    )
    manual_pass: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="人工强制合格（V4.0）"
    )
    manual_pass_reason: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="放行理由（≤100 字，V4.0）"
    )
    manual_pass_by: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="放行操作人用户 ID（V4.0）"
    )
    manual_pass_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="放行时间（V4.0）"
    )
    user_language_error: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="失败原因用户语言文案（V4.0）"
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="队列任务 ID")
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="触发运行 ID")
    queued_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="入队时间"
    )
    started_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[object | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )


class RenderBatch(IdMixin, TimestampMixin, Base):
    """渲染批次：一次批量提交的聚合态与护栏状态（V4.0，表 render_batches）。

    对齐 AC-F5-01-2（批次确认单）、AC-F5-01-3（每批次免费插队 3 次）、
    AC-F5-06-2（路由策略三档）、EX-F5-10（80% 预算预警防重发）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        batch_key: 批次业务键（唯一，等于提交时的 Idempotency-Key 哈希）。
        strategy_tier: 路由策略档 speed/balanced/quality。
        shot_count: 批次镜头数（≥0）。
        est_total_credits: 预估总积分（≥0）。
        est_total_seconds: 预计总耗时（秒，≥0）。
        jump_used: 已用免费插队次数（0~3）。
        warning_80_sent: 80% 预算预警是否已发（防重发）。
        route_summary: 路由摘要 JSONB（主力 + 备用模型）。
        status: 批次状态 submitting/running/completed/cancelled。
        created_by: 提交用户 ID（FK users，SET NULL）。
    """

    __tablename__ = "render_batches"
    __table_args__ = (
        CheckConstraint(
            text("strategy_tier IN ('speed', 'balanced', 'quality')"),
            name="ck_render_batches_strategy_tier",
        ),
        CheckConstraint(
            text("status IN ('submitting', 'running', 'completed', 'cancelled')"),
            name="ck_render_batches_status",
        ),
        CheckConstraint(text("shot_count >= 0"), name="ck_render_batches_shot_count"),
        CheckConstraint(text("est_total_credits >= 0"), name="ck_render_batches_est_credits"),
        CheckConstraint(text("est_total_seconds >= 0"), name="ck_render_batches_est_seconds"),
        CheckConstraint(text("jump_used BETWEEN 0 AND 3"), name="ck_render_batches_jump_used"),
        UniqueConstraint("batch_key", name="uq_render_batches_batch_key"),
        Index("idx_render_batches_project_id_created_at", "project_id", "created_at"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    batch_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="批次业务键")
    strategy_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="balanced", comment="路由策略档（3 值）"
    )
    shot_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="批次镜头数"
    )
    est_total_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="预估总积分"
    )
    est_total_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="预计总耗时（秒）"
    )
    jump_used: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="已用免费插队次数"
    )
    warning_80_sent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="80% 预警已发"
    )
    route_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="路由摘要")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="submitting", comment="批次状态（4 值）"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="提交用户 ID"
    )


class RenderOutput(IdMixin, TimestampMixin, Base):
    """渲染产物：渲染任务的产物，关联媒体文件（表 render_outputs）。

    Attributes:
        render_task_id: 渲染任务 ID（FK render_tasks，级联删除）。
        project_id: 项目 ID（FK projects，级联删除）。
        shot_id: 镜头 ID（FK shots，SET NULL）。
        media_file_id: 产物文件 ID（FK media_files，SET NULL）。
        output_index: 同任务内序号（≥0）。
        output_type: 产物类型 video/image/audio/subtitle。
        qc_report_id: 质检报告 ID（弱引用 qc_reports）。
        is_selected: 是否被选中采用。
    """

    __tablename__ = "render_outputs"
    __table_args__ = (
        CheckConstraint(text("output_index >= 0"), name="ck_render_outputs_output_index"),
        CheckConstraint(
            text("output_type IN ('video', 'image', 'audio', 'subtitle')"),
            name="ck_render_outputs_output_type",
        ),
        Index("idx_render_outputs_render_task_id", "render_task_id"),
        Index("idx_render_outputs_shot_id", "shot_id"),
        Index("idx_render_outputs_project_id_output_type", "project_id", "output_type"),
    )

    render_task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("render_tasks.id", ondelete="CASCADE"), nullable=False, comment="渲染任务 ID"
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    shot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shots.id", ondelete="SET NULL"), nullable=True, comment="镜头 ID"
    )
    media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="产物文件 ID"
    )
    output_index: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="产物序号"
    )
    output_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="产物类型（4 值）")
    qc_report_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="质检报告 ID")
    is_selected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否被选中"
    )


class AiCallLog(IdMixin, TimestampMixin, Base):
    """模型调用日志：每次模型调用的 token、时长、成本、链路留痕，只追加（表 ai_call_logs）。

    无物理外键（高写入日志）；业务上只写不改。

    Attributes:
        project_id: 项目 ID（弱引用）。
        render_task_id: 渲染任务 ID（弱引用）。
        agent_run_id: Agent 运行 ID（弱引用）。
        model_key: 模型键。
        provider: 提供方。
        call_type: 调用类型（同 render_tasks.task_type）。
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。
        total_tokens: 总 token 数。
        latency_ms: 时延（毫秒）。
        cost_credits: 成本（积分，≥0）。
        status: 调用状态 success/failed。
        error_code: 错误码。
        trace_id: 链路 ID（ULID）。
        request_snapshot: 请求快照 JSONB。
        response_snapshot: 响应快照 JSONB（可脱敏）。
    """

    __tablename__ = "ai_call_logs"
    __table_args__ = (
        CheckConstraint(
            text(f"call_type IN ({_AI_CALL_LOG_TYPE_VALUES})"),
            name="ck_ai_call_logs_call_type",
        ),
        CheckConstraint(text("cost_credits >= 0"), name="ck_ai_call_logs_cost"),
        CheckConstraint(text("status IN ('success', 'failed')"), name="ck_ai_call_logs_status"),
        Index("idx_ai_call_logs_project_id_created_at", "project_id", "created_at"),
        Index("idx_ai_call_logs_trace_id", "trace_id"),
        Index("idx_ai_call_logs_model_key_created_at", "model_key", "created_at"),
        Index("idx_ai_call_logs_render_task_id", "render_task_id"),
    )

    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="项目 ID")
    render_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="渲染任务 ID")
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="Agent 运行 ID")
    model_key: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="模型键")
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="提供方")
    call_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="调用类型")
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="输入 token 数")
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="输出 token 数")
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="总 token 数")
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="时延（毫秒）")
    cost_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="成本（积分）"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="调用状态")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="错误码")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="链路 ID")
    request_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="请求快照")
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="响应快照")

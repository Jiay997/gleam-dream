"""域 K · 协同与运行时（Collaboration & Runtime）模型。

包含 6 张表（对齐《02 数据库设计文档》§5.11）：
- ``approval_requests`` HITL 审批单（4 类关口）；
- ``notifications`` 站内通知（P1）；
- ``agent_runs`` Agent 运行记录（步数/耗时/token/成本）；
- ``style_presets`` 画风预设库；
- ``prompt_templates`` 提示词模板（可版本化，P1）；
- ``system_configs`` 系统配置中心（P1）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
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


class ApprovalRequest(IdMixin, TimestampMixin, Base):
    """HITL 审批单：剧本/资产/分镜/成片四类关口的人工审批（表 approval_requests）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        approval_type: 关口类型 script/asset/storyboard/final_cut。
        target_type: 目标类型 script/asset/shot/timeline。
        target_id: 目标 ID（多态）。
        status: 状态 pending/approved/rejected/edited/expired。
        requested_by_agent_run_id: 发起运行 ID（FK agent_runs，SET NULL）。
        requested_at: 发起时间。
        decided_by_user_id: 决策人用户 ID（FK users，SET NULL）。
        decided_at: 决策时间。
        decision: 决策 approve/reject/edit。
        feedback: 反馈（reject/edit 时写入）。
        feedback_scope: 结构化反馈作用域 JSONB（V4.0，F2.8 编剧 Agent 注入）。
        edited_payload: 人工编辑内容 JSONB。
        expires_at: 过期时刻（默认 +7 天）。
        checkpoint_id: 关联 LangGraph checkpoint ID。
    """

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            text("approval_type IN ('script', 'asset', 'storyboard', 'final_cut')"),
            name="ck_approval_requests_approval_type",
        ),
        CheckConstraint(
            text(
                "target_type IS NULL OR target_type IN ('script', 'asset', 'shot', 'timeline')"
            ),
            name="ck_approval_requests_target_type",
        ),
        CheckConstraint(
            text("status IN ('pending', 'approved', 'rejected', 'edited', 'expired')"),
            name="ck_approval_requests_status",
        ),
        CheckConstraint(
            text("decision IS NULL OR decision IN ('approve', 'reject', 'edit')"),
            name="ck_approval_requests_decision",
        ),
        Index("idx_approval_requests_project_id_status", "project_id", "status"),
        Index("idx_approval_requests_status_expires_at", "status", "expires_at"),
        Index("idx_approval_requests_target_type_target_id", "target_type", "target_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    approval_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="关口类型（4 值）")
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="目标类型（4 值）")
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="目标 ID（多态）")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending", comment="审批状态（5 值）"
    )
    requested_by_agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, comment="发起运行 ID"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="发起时间"
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="决策人用户 ID"
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="决策时间"
    )
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="决策（3 值）")
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True, comment="反馈")
    feedback_scope: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="结构化反馈作用域（V4.0）"
    )
    edited_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="人工编辑内容")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="过期时刻"
    )
    checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="checkpoint ID")


class Notification(IdMixin, TimestampMixin, Base):
    """站内通知：面向用户的通知消息（P1，表 notifications）。

    Attributes:
        user_id: 收件人用户 ID（FK users，级联删除）。
        project_id: 关联项目 ID（FK projects，级联删除）。
        type: 类型 render_done/render_failed/approval_pending/credit_low/system。
        title: 标题。
        content: 正文。
        payload: 跳转参数 JSONB。
        is_read: 是否已读。
        read_at: 已读时间。
    """

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint(
            text("type IN ('render_done', 'render_failed', 'approval_pending', 'credit_low', 'system')"),
            name="ck_notifications_type",
        ),
        Index("idx_notifications_user_id_is_read_created_at", "user_id", "is_read", "created_at"),
        Index("idx_notifications_project_id", "project_id"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="收件人用户 ID"
    )
    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, comment="关联项目 ID"
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False, comment="通知类型（5 值）")
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="标题")
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="正文")
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="跳转参数")
    is_read: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否已读"
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="已读时间"
    )


class AgentRun(IdMixin, TimestampMixin, Base):
    """Agent 运行记录：每个 LangGraph 节点运行的步数/耗时/token/成本（表 agent_runs）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        node_name: 节点名（snake_case）。
        agent_key: Agent 键（如 screenwriter/art_director）。
        parent_run_id: 父运行 ID（自引用 FK，SET NULL，子代理场景）。
        status: 状态 running/succeeded/failed/interrupted。
        steps: 步数（≥0）。
        total_tokens: 总 token 数（≥0）。
        cost_credits: 成本（积分，≥0）。
        latency_ms: 耗时（毫秒）。
        checkpoint_id: 关联 checkpoint ID。
        trace_id: 链路 ID（ULID）。
        input_summary: 入参摘要 JSONB。
        output_summary: 出参摘要 JSONB。
        error_reason: 失败原因。
        started_at: 开始时间。
        finished_at: 结束时间。
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(text("node_name IS NOT NULL"), name="ck_agent_runs_node_name"),
        CheckConstraint(
            text("status IN ('running', 'succeeded', 'failed', 'interrupted')"),
            name="ck_agent_runs_status",
        ),
        CheckConstraint(text("steps >= 0"), name="ck_agent_runs_steps"),
        CheckConstraint(text("total_tokens >= 0"), name="ck_agent_runs_total_tokens"),
        CheckConstraint(text("cost_credits >= 0"), name="ck_agent_runs_cost"),
        Index("idx_agent_runs_project_id_node_name", "project_id", "node_name"),
        Index("idx_agent_runs_trace_id", "trace_id"),
        Index("idx_agent_runs_parent_run_id", "parent_run_id"),
        Index("idx_agent_runs_status", "status"),
    )

    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, comment="项目 ID"
    )
    node_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="节点名")
    agent_key: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="Agent 键")
    parent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, comment="父运行 ID"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="running", comment="运行状态（4 值）"
    )
    steps: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="步数"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="总 token 数"
    )
    cost_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="成本（积分）"
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="耗时（毫秒）")
    checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="checkpoint ID")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="链路 ID")
    input_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="入参摘要")
    output_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="出参摘要")
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )


class StylePreset(IdMixin, TimestampMixin, Base):
    """画风预设库：可复用画风参数包（真人感/二次元/国风/3D）（全局主数据，表 style_presets）。

    Attributes:
        preset_key: 预设键 live_action/anime/chinese_style/three_d（唯一）。
        name: 显示名。
        description: 描述。
        cover_media_id: 封面示例 ID（FK media_files，SET NULL）。
        prompt_prefix: 正向 prompt 前缀。
        negative_prompt: 负向 prompt 前缀。
        params: 参数包 JSONB（模型/采样/风格权重）。
        is_active: 是否启用。
        sort_order: 排序。
    """

    __tablename__ = "style_presets"
    __table_args__ = (
        CheckConstraint(
            text("preset_key IN ('live_action', 'anime', 'chinese_style', 'three_d')"),
            name="ck_style_presets_preset_key",
        ),
        UniqueConstraint("preset_key", name="uq_style_presets_preset_key"),
        Index("idx_style_presets_is_active_sort_order", "is_active", "sort_order"),
    )

    preset_key: Mapped[str] = mapped_column(String(32), nullable=False, comment="预设键（4 值）")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="显示名")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="描述")
    cover_media_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="封面示例 ID"
    )
    prompt_prefix: Mapped[str | None] = mapped_column(Text, nullable=True, comment="正向 prompt 前缀")
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="负向 prompt 前缀")
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="参数包")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否启用"
    )
    sort_order: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="排序"
    )


class PromptTemplate(IdMixin, TimestampMixin, Base):
    """提示词模板：按 template_key + version_no 版本化（P1，表 prompt_templates）。

    Attributes:
        template_key: 模板键。
        agent_key: 适用 Agent 键（8 值）。
        version_no: 版本号（≥1，与 template_key 联合唯一）。
        content: 模板内容（含 {变量} 占位符）。
        variables: 变量定义 JSONB（变量名/类型/默认值）。
        is_active: 是否启用。
        created_by: 创建人用户 ID（FK users，SET NULL）。
    """

    __tablename__ = "prompt_templates"
    __table_args__ = (
        CheckConstraint(text("version_no >= 1"), name="ck_prompt_templates_version_no"),
        CheckConstraint(
            text(
                "agent_key IS NULL OR agent_key IN ('screenwriter', 'art_director', 'director', "
                "'producer', 'qc', 'compliance', 'post', 'distributor')"
            ),
            name="ck_prompt_templates_agent_key",
        ),
        UniqueConstraint(
            "template_key", "version_no", name="uq_prompt_templates_template_key_version_no"
        ),
        Index("idx_prompt_templates_template_key_is_active", "template_key", "is_active"),
        Index("idx_prompt_templates_agent_key", "agent_key"),
    )

    template_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="模板键")
    agent_key: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="适用 Agent 键")
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="模板内容")
    variables: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="变量定义")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否启用"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="创建人用户 ID"
    )


class SystemConfig(IdMixin, TimestampMixin, Base):
    """系统配置中心：系统级键值 + 类型配置（P1，表 system_configs）。

    Attributes:
        config_key: 配置键（唯一，点号分层如 billing.freeze_ttl_minutes）。
        config_value: 配置值 JSONB。
        value_type: 值类型 string/number/boolean/json。
        description: 说明。
        scope: 作用域 global/user/project。
        is_public: 是否前端可见。
        updated_by: 更新人用户 ID（FK users，SET NULL）。
    """

    __tablename__ = "system_configs"
    __table_args__ = (
        CheckConstraint(
            text("value_type IN ('string', 'number', 'boolean', 'json')"),
            name="ck_system_configs_value_type",
        ),
        CheckConstraint(
            text("scope IN ('global', 'user', 'project')"),
            name="ck_system_configs_scope",
        ),
        UniqueConstraint("config_key", name="uq_system_configs_config_key"),
        Index("idx_system_configs_scope", "scope"),
    )

    config_key: Mapped[str] = mapped_column(String(128), nullable=False, comment="配置键")
    config_value: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="配置值")
    value_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="值类型（4 值）")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="说明")
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="global", comment="作用域（3 值）"
    )
    is_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否前端可见"
    )
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="更新人用户 ID"
    )

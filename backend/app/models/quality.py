"""域 H · 质量与合规（QC & Compliance）模型。

包含 3 张表（对齐《02 数据库设计文档》§5.8）：
- ``qc_reports`` 质检报告（多态指向 shot/asset/script/timeline/output）；
- ``compliance_flags`` 合规标记（类型/等级/位置/建议）；
- ``moderation_records`` 三方审核记录（请求/响应快照，只追加）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import IdMixin, TimestampMixin


class QcReport(IdMixin, TimestampMixin, Base):
    """质检报告：对生成结果的一致性/质量自动判定，多态指向目标（表 qc_reports）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        target_type: 目标类型 shot/asset/script/timeline/output。
        target_id: 目标 ID（多态，无物理 FK）。
        target_version_id: 目标版本 ID（多态）。
        result: 结论 passed/failed/warning。
        consistency_score: 一致性分（0.000~1.000）。
        quality_defects: 质量缺陷清单 JSONB。
        detail: 详细报告 JSONB。
        round_no: 自检轮次（≥1）。
        model_key: 质检模型键。
        checked_by_agent_run_id: 质检运行 ID（弱引用 agent_runs）。
    """

    __tablename__ = "qc_reports"
    __table_args__ = (
        CheckConstraint(
            text("target_type IN ('shot', 'asset', 'script', 'timeline', 'output')"),
            name="ck_qc_reports_target_type",
        ),
        CheckConstraint(
            text("result IN ('passed', 'failed', 'warning')"),
            name="ck_qc_reports_result",
        ),
        CheckConstraint(
            text("consistency_score IS NULL OR consistency_score BETWEEN 0 AND 1"),
            name="ck_qc_reports_consistency_score",
        ),
        CheckConstraint(text("round_no >= 1"), name="ck_qc_reports_round_no"),
        Index("idx_qc_reports_target_type_target_id", "target_type", "target_id"),
        Index("idx_qc_reports_project_id_result", "project_id", "result"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="目标类型（5 值）")
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="目标 ID（多态）")
    target_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="目标版本 ID（多态）"
    )
    result: Mapped[str] = mapped_column(String(16), nullable=False, comment="质检结论（3 值）")
    consistency_score: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True, comment="一致性分 0.000~1.000"
    )
    quality_defects: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="质量缺陷清单")
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="详细报告")
    round_no: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="自检轮次"
    )
    model_key: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="质检模型键")
    checked_by_agent_run_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="质检运行 ID"
    )


class ComplianceFlag(IdMixin, TimestampMixin, Base):
    """合规标记：内容安全标记（类型/等级/位置/建议）（表 compliance_flags）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        target_type: 目标类型 script/shot/asset/timeline。
        target_id: 目标 ID（多态）。
        flag_type: 违规类型（8 值）。
        level: 等级 low/medium/high/block。
        position: 位置定位 JSONB（时间码/字符偏移）。
        description: 描述。
        suggestion: 修改建议。
        status: 处理状态 open/resolved/blocked。
        source: 来源 rule/model/manual。
    """

    __tablename__ = "compliance_flags"
    __table_args__ = (
        CheckConstraint(
            text("target_type IN ('script', 'shot', 'asset', 'timeline')"),
            name="ck_compliance_flags_target_type",
        ),
        CheckConstraint(
            text(
                "flag_type IN ('politics', 'porn', 'minor', 'violence', 'infringement', "
                "'sensitive_word', 'celebrity_face', 'real_person_likeness')"
            ),
            name="ck_compliance_flags_flag_type",
        ),
        CheckConstraint(
            text("level IN ('low', 'medium', 'high', 'block')"),
            name="ck_compliance_flags_level",
        ),
        CheckConstraint(
            text("status IN ('open', 'resolved', 'blocked')"),
            name="ck_compliance_flags_status",
        ),
        CheckConstraint(
            text("source IN ('rule', 'model', 'manual')"),
            name="ck_compliance_flags_source",
        ),
        Index("idx_compliance_flags_project_id_level", "project_id", "level"),
        Index("idx_compliance_flags_target_type_target_id", "target_type", "target_id"),
        Index("idx_compliance_flags_status", "status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="目标类型（4 值）")
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="目标 ID（多态）")
    flag_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="违规类型（8 值）")
    level: Mapped[str] = mapped_column(String(16), nullable=False, comment="等级（4 值）")
    position: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="位置定位")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="描述")
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True, comment="修改建议")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open", comment="处理状态（3 值）"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="model", comment="标记来源"
    )


class ModerationRecord(IdMixin, TimestampMixin, Base):
    """三方审核记录：三方内容审核的请求/响应快照，只追加（表 moderation_records）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        flag_id: 关联合规标记 ID（FK compliance_flags，SET NULL）。
        provider: 审核提供方。
        api_name: 调用接口名。
        verdict: 判定 pass/review/block。
        request_snapshot: 请求快照 JSONB。
        response_snapshot: 响应快照 JSONB。
        cost_credits: 成本（积分，≥0）。
        latency_ms: 时延（毫秒）。
        trace_id: 链路 ID（ULID）。
        target_type: 被审核对象类型 script/shot/asset/cover（``cover`` 为 V4.0 新增）。
        script_version_id: 关联剧本版本 ID（合规报告导出依据，V4.0）。
    """

    __tablename__ = "moderation_records"
    __table_args__ = (
        CheckConstraint(
            text("verdict IN ('pass', 'review', 'block')"),
            name="ck_moderation_records_verdict",
        ),
        CheckConstraint(
            text(
                "target_type IS NULL OR target_type IN "
                "('script', 'shot', 'asset', 'timeline', 'cover')"
            ),
            name="ck_moderation_records_target_type",
        ),
        CheckConstraint(text("cost_credits >= 0"), name="ck_moderation_records_cost"),
        Index("idx_moderation_records_project_id_created_at", "project_id", "created_at"),
        Index("idx_moderation_records_verdict", "verdict"),
        Index("idx_moderation_records_script_version_id", "script_version_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    flag_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("compliance_flags.id", ondelete="SET NULL"), nullable=True, comment="合规标记 ID"
    )
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="审核提供方")
    api_name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="调用接口名")
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, comment="审核判定（3 值）")
    request_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="请求快照")
    response_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="响应快照")
    cost_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="成本（积分）"
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="时延（毫秒）")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="链路 ID")
    target_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="被审核对象类型（含 cover，V4.0）"
    )
    script_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="关联剧本版本 ID（V4.0）"
    )

"""域 D · 剧本（Script）模型。

包含 9 张表（对齐《02 数据库设计文档》§5.4 与 §13.5 V4.0 增量）：
- ``documents`` 上传的剧本文档（含解析状态）；
- ``scripts`` 剧本版本（结构化根，按 version_no 版本化）；
- ``episodes`` 分集；
- ``scenes`` 场次；
- ``script_lines`` 台词/旁白/动作/舞台提示行；
- ``parse_confidence_items`` 解析置信度逐字段标注；
- ``script_runs`` 一次剧本生成任务的运行态（V4.0，支撑生成中终止按集结算）；
- ``script_template_refs`` 模板包引用与偏离记录（V4.0）；
- ``script_export_logs`` 剧本导出留痕（V4.0）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
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

# 情绪标签 8 值（对齐 00 契约 §6.7）
_EMOTION_VALUES = (
    "'calm', 'joy', 'anger', 'sadness', 'fear', 'surprise', 'tension', 'romance'"
)


class Document(IdMixin, TimestampMixin, Base):
    """上传剧本文档：用户上传的剧本文档与其解析状态（表 documents）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        uploader_user_id: 上传者用户 ID（FK users，SET NULL）。
        file_name: 原文件名。
        media_file_id: 文件存储 ID（FK media_files，SET NULL）。
        doc_type: 文档类型 txt/docx/pdf_text/pdf_scan。
        page_count: 页数（≥0）。
        ocr_used: 是否走 OCR。
        parse_status: 解析状态 pending/parsing/parsed/failed。
        parse_error: 解析错误信息。
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            text("doc_type IN ('txt', 'docx', 'pdf_text', 'pdf_scan')"),
            name="ck_documents_doc_type",
        ),
        CheckConstraint(text("page_count IS NULL OR page_count >= 0"), name="ck_documents_page_count"),
        CheckConstraint(
            text("parse_status IN ('pending', 'parsing', 'parsed', 'failed')"),
            name="ck_documents_parse_status",
        ),
        Index("idx_documents_project_id", "project_id"),
        Index("idx_documents_parse_status", "parse_status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    uploader_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="上传者用户 ID"
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="原文件名")
    media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="文件存储 ID"
    )
    doc_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="文档类型")
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="页数")
    ocr_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否走 OCR"
    )
    parse_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending", comment="解析状态"
    )
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True, comment="解析错误信息")


class Script(IdMixin, TimestampMixin, Base):
    """剧本版本：结构化剧本根，一条记录对应一个版本（表 scripts）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        version_no: 版本号（项目内唯一，≥1）。
        parent_script_id: 父版本剧本 ID（自引用 FK，SET NULL）。
        source_type: 来源 generated/uploaded。
        document_id: 来源文档 ID（FK documents，SET NULL）。
        status: 状态（8 值：draft/validating/compliance_review/pending_confirm/confirmed/locked/needs_manual_fix/rejected）。
        title: 剧名。
        logline: 一句话梗概。
        genre: 题材。
        total_episodes: 集数（≥0）。
        total_words: 总字数（≥0）。
        structure: 结构化剧本（集-场-行树 JSONB）。
        version_label: 版本命名备注（AC-F2-11-1，V4.0）。
        confidence_overall: 解析整体置信度（0.00~1.00，AC-F2-02-4，V4.0）。
        confirmed_at: 确认时间。
        locked_at: 锁定时间。
        created_by: 创建者用户 ID（FK users，SET NULL）。
    """

    __tablename__ = "scripts"
    __table_args__ = (
        CheckConstraint(text("version_no >= 1"), name="ck_scripts_version_no"),
        CheckConstraint(
            text(
                "status IN ('draft', 'validating', 'compliance_review', 'pending_confirm', "
                "'confirmed', 'locked', 'needs_manual_fix', 'rejected')"
            ),
            name="ck_scripts_status",
        ),
        CheckConstraint(
            text("source_type IN ('generated', 'uploaded')"),
            name="ck_scripts_source_type",
        ),
        CheckConstraint(text("total_episodes >= 0"), name="ck_scripts_total_episodes"),
        CheckConstraint(text("total_words >= 0"), name="ck_scripts_total_words"),
        CheckConstraint(
            text("confidence_overall IS NULL OR confidence_overall BETWEEN 0 AND 1"),
            name="ck_scripts_confidence_overall",
        ),
        UniqueConstraint("project_id", "version_no", name="uq_scripts_project_id_version_no"),
        Index("idx_scripts_project_id_status", "project_id", "status"),
        Index("idx_scripts_project_id_version_no", "project_id", "version_no"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    parent_script_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scripts.id", ondelete="SET NULL"), nullable=True, comment="父版本剧本 ID"
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="来源类型")
    document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True, comment="来源文档 ID"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="draft", comment="剧本状态（8 值）"
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="剧名")
    logline: Mapped[str | None] = mapped_column(Text, nullable=True, comment="一句话梗概")
    genre: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="题材")
    total_episodes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="集数"
    )
    total_words: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="总字数"
    )
    structure: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="结构化剧本树")
    version_label: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="版本命名备注（V4.0）"
    )
    confidence_overall: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 2), nullable=True, comment="解析整体置信度 0.00~1.00（V4.0）"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="确认时间"
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="锁定时间"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="创建者用户 ID"
    )


class Episode(IdMixin, TimestampMixin, Base):
    """分集：项目内叙事单元，目标时长 1~3 分钟（表 episodes）。

    Attributes:
        script_id: 剧本版本 ID（FK scripts，级联删除）。
        project_id: 项目 ID（FK projects，级联删除）。
        episode_no: 集号（剧本内唯一，≥1）。
        title: 集标题。
        synopsis: 集简介。
        target_duration_seconds: 目标时长（秒，计划值，60~180）。
        hook_text: 结尾钩子文案。
        hook_suggestion: 结尾钩子建议（≤50 字，AC-F2-05-2，V4.0）。
        est_duration_sec: 预估时长秒（60~180，AC-F2-05-2，V4.0）。
        order_index: 展示序（≥0）。
        status: 状态 draft/confirmed/locked。
    """

    __tablename__ = "episodes"
    __table_args__ = (
        CheckConstraint(text("episode_no >= 1"), name="ck_episodes_episode_no"),
        CheckConstraint(
            text("target_duration_seconds IS NULL OR target_duration_seconds BETWEEN 60 AND 180"),
            name="ck_episodes_target_duration",
        ),
        CheckConstraint(text("order_index >= 0"), name="ck_episodes_order_index"),
        CheckConstraint(
            text("est_duration_sec IS NULL OR est_duration_sec BETWEEN 60 AND 180"),
            name="ck_episodes_est_duration_sec",
        ),
        CheckConstraint(
            text("hook_suggestion IS NULL OR char_length(hook_suggestion) <= 50"),
            name="ck_episodes_hook_suggestion_len",
        ),
        CheckConstraint(
            text("status IN ('draft', 'confirmed', 'locked')"),
            name="ck_episodes_status",
        ),
        UniqueConstraint("script_id", "episode_no", name="uq_episodes_script_id_episode_no"),
        Index("idx_episodes_script_id_order_index", "script_id", "order_index"),
    )

    script_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False, comment="剧本版本 ID"
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    episode_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="集号")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="集标题")
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True, comment="集简介")
    target_duration_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="目标时长（秒，计划值）"
    )
    hook_text: Mapped[str | None] = mapped_column(Text, nullable=True, comment="结尾钩子")
    hook_suggestion: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="结尾钩子建议（≤50 字，V4.0）"
    )
    est_duration_sec: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="预估时长秒 60~180（V4.0）"
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="展示序")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="draft", comment="分集状态"
    )


class Scene(IdMixin, TimestampMixin, Base):
    """场次：集内时间/地点连续单元（表 scenes）。

    Attributes:
        episode_id: 集 ID（FK episodes，级联删除）。
        project_id: 项目 ID（FK projects，级联删除）。
        scene_no: 场号（集内唯一，≥1）。
        title: 场标题。
        location: 地点。
        time_of_day: 时段 day/night/dawn/dusk。
        int_ext: 内外景 int/ext。
        summary: 场摘要。
        order_index: 展示序（≥0）。
    """

    __tablename__ = "scenes"
    __table_args__ = (
        CheckConstraint(text("scene_no >= 1"), name="ck_scenes_scene_no"),
        CheckConstraint(
            text("time_of_day IS NULL OR time_of_day IN ('day', 'night', 'dawn', 'dusk')"),
            name="ck_scenes_time_of_day",
        ),
        CheckConstraint(
            text("int_ext IS NULL OR int_ext IN ('int', 'ext')"),
            name="ck_scenes_int_ext",
        ),
        CheckConstraint(text("order_index >= 0"), name="ck_scenes_order_index"),
        UniqueConstraint("episode_id", "scene_no", name="uq_scenes_episode_id_scene_no"),
        Index("idx_scenes_episode_id_order_index", "episode_id", "order_index"),
    )

    episode_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, comment="集 ID"
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    scene_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="场号")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="场标题")
    location: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="地点")
    time_of_day: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="时段")
    int_ext: Mapped[str | None] = mapped_column(String(8), nullable=True, comment="内外景")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="场摘要")
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="展示序")


class ScriptLine(IdMixin, TimestampMixin, Base):
    """剧本行：台词/旁白/动作/舞台提示最小单元（表 script_lines）。

    Attributes:
        scene_id: 场次 ID（FK scenes，级联删除）。
        project_id: 项目 ID（FK projects，级联删除）。
        line_no: 行号（场内唯一，≥1）。
        line_type: 行类型 dialogue/narration/action/stage_direction。
        character_asset_id: 角色资产 ID（FK assets，SET NULL）。
        speaker_name: 说话人（原文名）。
        content: 行内容。
        emotion_tag: 情绪标签（8 值）。
        order_index: 展示序（≥0）。
        line_key: 台词稳定键（V6.0 新增，跨剧本版本不变）。
        speaker_type: 说话人类型 dialogue/narration/voiceover（V6.0 新增）。
        est_ms: 预估时长（V6.0 新增）。
        actual_ms: 实测配音时长（V6.0 新增）。
        voice_status: 配音状态 pending/voiced/failed/dirty（V6.0 新增）。
        source: 台词来源 script/edit/split/import（V6.0 新增）。
        is_deleted: 软删除位，用于台词移除审计（V6.0 新增）。
    """

    __tablename__ = "script_lines"
    __table_args__ = (
        CheckConstraint(text("line_no >= 1"), name="ck_script_lines_line_no"),
        CheckConstraint(
            text("line_type IN ('dialogue', 'narration', 'action', 'stage_direction')"),
            name="ck_script_lines_line_type",
        ),
        CheckConstraint(
            text(f"emotion_tag IS NULL OR emotion_tag IN ({_EMOTION_VALUES})"),
            name="ck_script_lines_emotion_tag",
        ),
        CheckConstraint(text("order_index >= 0"), name="ck_script_lines_order_index"),
        UniqueConstraint("scene_id", "line_no", name="uq_script_lines_scene_id_line_no"),
        Index("idx_script_lines_scene_id_order_index", "scene_id", "order_index"),
        Index("idx_script_lines_character_asset_id", "character_asset_id"),
    )

    scene_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False, comment="场次 ID"
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    line_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="行号")
    line_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="行类型（4 值）")
    character_asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, comment="角色资产 ID"
    )
    speaker_name: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="说话人")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="行内容")
    emotion_tag: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="情绪标签")
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="展示序")
    # ---- V6.0 台词清单增量字段（F2.12）----
    line_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="台词稳定键（跨版本不变）"
    )
    speaker_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="dialogue", comment="说话人类型"
    )
    est_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="预估时长（ms）"
    )
    actual_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="实测配音时长（ms）")
    voice_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", comment="配音状态"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="script", comment="台词来源"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="软删除位"
    )


class ParseConfidenceItem(IdMixin, TimestampMixin, Base):
    """解析置信度标注：上传剧本解析后逐字段置信度，低置信度进人工校正（表 parse_confidence_items）。

    Attributes:
        script_id: 剧本版本 ID（FK scripts，级联删除）。
        document_id: 来源文档 ID（FK documents，级联删除）。
        target_type: 目标类型 episode/scene/line/character。
        target_ref: 目标引用键。
        field_name: 字段名（如 speaker/boundary）。
        confidence: 置信度（0.000~1.000）。
        extracted_value: 抽取值。
        corrected_value: 人工校正值。
        needs_manual: 是否需人工校正。
        reviewed_by: 校正人用户 ID（FK users，SET NULL）。
        reviewed_at: 校正时间。
    """

    __tablename__ = "parse_confidence_items"
    __table_args__ = (
        CheckConstraint(
            text("target_type IN ('episode', 'scene', 'line', 'character')"),
            name="ck_parse_confidence_items_target_type",
        ),
        CheckConstraint(
            text("confidence BETWEEN 0 AND 1"),
            name="ck_parse_confidence_items_confidence",
        ),
        Index("idx_parse_confidence_items_script_id_needs_manual", "script_id", "needs_manual"),
    )

    script_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False, comment="剧本版本 ID"
    )
    document_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, comment="来源文档 ID"
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="目标类型")
    target_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="目标引用键")
    field_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="字段名")
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, comment="置信度 0.000~1.000"
    )
    extracted_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="抽取值")
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="人工校正值")
    needs_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否需人工校正"
    )
    reviewed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="校正人用户 ID"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="校正时间"
    )


class ScriptRun(IdMixin, TimestampMixin, Base):
    """剧本生成运行态：一次生成任务的进度与结算依据（V4.0，表 script_runs）。

    支撑 AC-F2-12-1~3「生成中终止，已完成集保留，未执行部分解冻返还」：
    ``completed_episodes`` 为已完成集数，``frozen_credits`` 为本次冻结总额，
    ``settled_credits`` / ``refunded_credits`` 记录结算与返还。幂等键为
    ``project_id + script_run_id``（AC-F2-12-2）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        script_id: 目标剧本版本 ID（FK scripts，SET NULL）。
        run_status: 运行状态 generating/succeeded/failed/cancelled。
        idempotency_key: 幂等键（非空时唯一）。
        freeze_id: 关联计费冻结记录 ID（终止时按集结算依据）。
        total_episodes: 计划生成集数（≥1）。
        completed_episodes: 已完成集数（≥0，≤ total_episodes）。
        frozen_credits: 本次冻结积分（≥0）。
        settled_credits: 已结算积分（≥0）。
        refunded_credits: 已返还积分（≥0）。
        plan: write_todos 计划节点 JSONB。
        failure_type: 失败类型（超时/服务不可用等）。
        failure_reason: 失败详情。
        created_by: 发起用户 ID（FK users，SET NULL）。
        started_at: 开始时间。
        finished_at: 结束时间。
    """

    __tablename__ = "script_runs"
    __table_args__ = (
        CheckConstraint(
            text("run_status IN ('generating', 'succeeded', 'failed', 'cancelled')"),
            name="ck_script_runs_run_status",
        ),
        CheckConstraint(text("total_episodes >= 1"), name="ck_script_runs_total_episodes"),
        CheckConstraint(text("completed_episodes >= 0"), name="ck_script_runs_completed_min"),
        CheckConstraint(
            text("completed_episodes <= total_episodes"), name="ck_script_runs_completed_max"
        ),
        CheckConstraint(text("frozen_credits >= 0"), name="ck_script_runs_frozen_credits"),
        CheckConstraint(text("settled_credits >= 0"), name="ck_script_runs_settled_credits"),
        CheckConstraint(text("refunded_credits >= 0"), name="ck_script_runs_refunded_credits"),
        # 幂等键：非空唯一（AC-F2-12-2 / EX-F2-07）
        Index(
            "uq_script_runs_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("idx_script_runs_project_id_created_at", "project_id", "created_at"),
        Index("idx_script_runs_project_id_run_status", "project_id", "run_status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    script_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scripts.id", ondelete="SET NULL"), nullable=True, comment="目标剧本版本 ID"
    )
    run_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="generating", comment="运行状态（4 值）"
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="幂等键")
    freeze_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("credit_freezes.id", ondelete="SET NULL"),
        nullable=True,
        comment="关联计费冻结记录 ID",
    )
    total_episodes: Mapped[int] = mapped_column(Integer, nullable=False, comment="计划集数")
    completed_episodes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="已完成集数"
    )
    frozen_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="冻结积分"
    )
    settled_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="已结算积分"
    )
    refunded_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="已返还积分"
    )
    plan: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="write_todos 计划")
    failure_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="失败类型")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败详情")
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="发起用户 ID"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )


class ScriptTemplateRef(IdMixin, TimestampMixin, Base):
    """模板包引用与偏离记录（V4.0，表 script_template_refs）。

    模板包下线不影响历史项目参数（AC-F2-09-3）：项目记录引用时的
    参数快照与偏离字段，读取时优先用快照。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        template_id: 模板包 ID（如 ``urban_romance__anime``）。
        template_name: 模板包展示名快照。
        applied_params: 套用时的参数快照 JSONB。
        deviated_fields: 用户手改过的字段名数组（AC-F2-09-2）。
        applied_by: 套用用户 ID（FK users，SET NULL）。
    """

    __tablename__ = "script_template_refs"
    __table_args__ = (
        UniqueConstraint("project_id", "template_id", name="uq_script_template_refs_project_template"),
        Index("idx_script_template_refs_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    template_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="模板包 ID")
    template_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="模板名快照")
    applied_params: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="参数快照")
    deviated_fields: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="偏离字段名数组"
    )
    applied_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="套用用户 ID"
    )


class ScriptExportLog(IdMixin, TimestampMixin, Base):
    """剧本导出留痕（V4.0，表 script_export_logs）。

    Attributes:
        user_id: 导出用户 ID（FK users，级联删除）。
        project_id: 项目 ID（FK projects，级联删除）。
        version_id: 剧本版本 ID（FK scripts，SET NULL）。
        format: 导出格式 md/docx/txt。
        file_name: 产出文件名。
        size_bytes: 文件字节数（≥0）。
        low_confidence_count: 待人工校正字段数（写入文件首行提示）。
    """

    __tablename__ = "script_export_logs"
    __table_args__ = (
        CheckConstraint(text("format IN ('md', 'docx', 'txt')"), name="ck_script_export_logs_format"),
        CheckConstraint(text("size_bytes >= 0"), name="ck_script_export_logs_size_bytes"),
        Index("idx_script_export_logs_user_id_created_at", "user_id", "created_at"),
        Index("idx_script_export_logs_project_id", "project_id"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="导出用户 ID"
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scripts.id", ondelete="SET NULL"), nullable=True, comment="剧本版本 ID"
    )
    format: Mapped[str] = mapped_column(String(16), nullable=False, comment="导出格式（3 值）")
    file_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="产出文件名")
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="文件字节数"
    )
    low_confidence_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="待人工校正字段数"
    )

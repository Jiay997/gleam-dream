"""域 C · 项目与版本（Project）模型。

包含 4 张表（对齐《02 数据库设计文档》§5.3）：
- ``projects`` 项目主表（状态机 14 态、预算、画风、题材）；
- ``project_snapshots`` 版本快照（上限 30）；
- ``project_files`` 虚拟文件系统索引（基础设施表）；
- ``idempotency_keys`` 幂等键登记（基础设施表）。

说明：``projects.current_script_id`` / ``current_storyboard_id`` 为弱引用（无物理外键）；
``cover_media_id`` 因与 ``media_files.project_id`` 形成循环依赖，使用 use_alter 延迟建外键。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
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
from app.models.mixins import IdMixin, SoftDeleteMixin, TimestampMixin

# 项目状态 14 态（对齐 00 契约 §6.1 ProjectStatus）
_PROJECT_STATUS_VALUES = (
    "'draft', 'scripting', 'script_review', 'asset_preparing', 'asset_review', "
    "'storyboarding', 'storyboard_review', 'rendering', 'post_producing', "
    "'final_review', 'completed', 'archived', 'paused', 'failed'"
)


class Project(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """项目主表：一次短剧创作的全生命周期容器（表 projects）。

    Attributes:
        owner_user_id: 创建者用户 ID（FK users，RESTRICT）。
        team_id: 归属团队 ID（FK teams，SET NULL）。
        title: 项目标题。
        synopsis: 一句话简介/创意。
        genre: 题材（开放枚举，如 urban_romance/suspense）。
        style_preset_key: 画风预设键 live_action/anime/chinese_style/three_d。
        aspect_ratio: 画面比例 9:16/16:9/1:1。
        language: 主语言（BCP-47）。
        status: 项目状态（14 态状态机）。
        target_episode_count: 目标集数（1~30）。
        budget_credits: 项目预算上限（0 表示不限）。
        spent_credits: 已消耗积分。
        cover_media_id: 封面媒体文件 ID（FK media_files，SET NULL）。
        current_script_id: 当前剧本 ID（弱引用 scripts）。
        current_storyboard_id: 当前分镜板 ID（弱引用 storyboards）。
        config: 项目级配置 JSONB。
        lock_version: 乐观锁版本。
        archived_at: 归档时间。
        deleted_at: 软删除时间。
    """

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            text(f"status IN ({_PROJECT_STATUS_VALUES})"),
            name="ck_projects_status",
        ),
        CheckConstraint(
            text("aspect_ratio IN ('9:16', '16:9', '1:1')"),
            name="ck_projects_aspect_ratio",
        ),
        CheckConstraint(
            text("style_preset_key IN ('live_action', 'anime', 'chinese_style', 'three_d')"),
            name="ck_projects_style_preset_key",
        ),
        CheckConstraint(
            text("target_episode_count BETWEEN 1 AND 30"),
            name="ck_projects_target_episode_count",
        ),
        CheckConstraint(text("budget_credits >= 0"), name="ck_projects_budget_credits"),
        CheckConstraint(text("spent_credits >= 0"), name="ck_projects_spent_credits"),
        Index("idx_projects_owner_user_id_status", "owner_user_id", "status"),
        Index("idx_projects_team_id_status", "team_id", "status"),
        Index("idx_projects_status_updated_at", "status", "updated_at"),
        Index("idx_projects_created_at", "created_at"),
    )

    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, comment="创建者用户 ID"
    )
    team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="SET NULL"), nullable=True, comment="归属团队 ID"
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="项目标题")
    synopsis: Mapped[str | None] = mapped_column(Text, nullable=True, comment="一句话简介/创意")
    genre: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="题材")
    style_preset_key: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="live_action", comment="画风预设键"
    )
    aspect_ratio: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="9:16", comment="画面比例"
    )
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="zh-CN", comment="主语言"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="draft", comment="项目状态（14 态）"
    )
    target_episode_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("10"), comment="目标集数"
    )
    budget_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="项目预算上限（0 不限）"
    )
    spent_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="已消耗积分"
    )
    cover_media_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("media_files.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        comment="封面媒体文件 ID",
    )
    current_script_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前剧本 ID（弱引用 scripts）"
    )
    current_storyboard_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前分镜板 ID（弱引用 storyboards）"
    )
    config: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="项目级配置")
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="乐观锁版本"
    )
    # ---- V6.0 增量字段（F9 制作流程 / F11 参数中心 / 5A 完整性）----
    pipeline_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="voice_first", comment="制作流程 voice_first/classic"
    )
    config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="当前参数配置版本"
    )
    bible_incomplete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="Story Bible 是否不完整"
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="归档时间"
    )


class ProjectSnapshot(IdMixin, TimestampMixin, Base):
    """版本快照：项目状态快照（上限 30），支持回滚（表 project_snapshots）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        sequence_no: 快照序号（项目内唯一，≥1）。
        label: 快照名称。
        state: 项目状态快照 JSONB。
        checkpoint_id: 关联 LangGraph checkpoint ID。
        created_by: 触发用户 ID（FK users，SET NULL）。
        size_bytes: 快照体积。
    """

    __tablename__ = "project_snapshots"
    __table_args__ = (
        CheckConstraint(text("sequence_no >= 1"), name="ck_project_snapshots_sequence_no"),
        CheckConstraint(text("size_bytes >= 0"), name="ck_project_snapshots_size_bytes"),
        UniqueConstraint(
            "project_id", "sequence_no", name="uq_project_snapshots_project_id_sequence_no"
        ),
        Index("idx_project_snapshots_project_id_created_at", "project_id", "created_at"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="快照序号")
    label: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="快照名称")
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="项目状态快照")
    checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="checkpoint ID")
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="触发用户 ID"
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="快照体积（字节）"
    )


class ProjectFile(IdMixin, TimestampMixin, Base):
    """虚拟文件系统索引：DeepAgents 工作区产物索引（基础设施表 project_files）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        path: 工作区路径（以 /project/ 开头）。
        file_type: 文件类型 script/notes/json/media_ref 等。
        media_file_id: 媒体文件 ID（FK media_files，SET NULL）。
        content_hash: 内容 SHA-256。
        size_bytes: 体积（≥0）。
        agent_run_id: 产出运行 ID（弱引用 agent_runs）。
    """

    __tablename__ = "project_files"
    __table_args__ = (
        # file_type 为开放枚举（script/notes/json/media_ref…），不设 CHECK 以便扩展
        CheckConstraint(
            text("path LIKE '/project/%'"),
            name="ck_project_files_path_prefix",
        ),
        CheckConstraint(text("size_bytes >= 0"), name="ck_project_files_size_bytes"),
        UniqueConstraint("project_id", "path", name="uq_project_files_project_id_path"),
        Index("idx_project_files_project_id_file_type", "project_id", "file_type"),
        Index("idx_project_files_content_hash", "content_hash"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    path: Mapped[str] = mapped_column(String(512), nullable=False, comment="工作区路径")
    file_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="文件类型")
    media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="媒体文件 ID"
    )
    content_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True, comment="内容 SHA-256")
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="体积（字节）"
    )
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="产出运行 ID")


class IdempotencyKey(IdMixin, TimestampMixin, Base):
    """幂等键登记：节点级幂等 sha256(project_id + node + input_hash)（基础设施表）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        node_name: 节点名（snake_case）。
        scope: 作用域 agent_node/api_write/payment。
        key_hash: 幂等键哈希（唯一）。
        request_hash: 入参哈希。
        response_ref: 已产生结果引用 JSONB。
        status: 状态 processing/done/failed。
        first_seen_at: 首次登记时间。
        expires_at: 过期清理时间。
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        CheckConstraint(
            text("scope IN ('agent_node', 'api_write', 'payment')"),
            name="ck_idempotency_keys_scope",
        ),
        CheckConstraint(
            text("status IN ('processing', 'done', 'failed')"),
            name="ck_idempotency_keys_status",
        ),
        UniqueConstraint("key_hash", name="uq_idempotency_keys_key_hash"),
        Index("idx_idempotency_keys_project_id_node_name", "project_id", "node_name"),
        Index("idx_idempotency_keys_expires_at", "expires_at"),
    )

    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, comment="项目 ID"
    )
    node_name: Mapped[str] = mapped_column(String(64), nullable=False, comment="节点名")
    scope: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="agent_node", comment="作用域"
    )
    key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, comment="幂等键哈希")
    request_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True, comment="入参哈希")
    response_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="已产生结果引用")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="done", comment="登记状态"
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="首次登记时间"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="过期清理时间"
    )

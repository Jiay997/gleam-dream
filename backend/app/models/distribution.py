"""域 J · 导出与分发（Distribution）模型。

包含 4 张表（对齐《02 数据库设计文档》§5.10 与 §13.5 V4.0 增量）：
- ``export_jobs`` 导出任务（分辨率/水印/编码参数 + 转码缓存键/AI 标识落片）；
- ``export_presets`` 导出参数预设（上限 10，V4.0）；
- ``platform_accounts`` 三方平台授权（token 加密存储，P1）；
- ``distribution_records`` 分发记录与回链（P1，含状态机扩展）。
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


class ExportJob(IdMixin, TimestampMixin, Base):
    """导出任务：含分辨率/水印/编码参数（表 export_jobs）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        timeline_id: 时间线 ID（FK timelines，SET NULL）。
        resolution: 分辨率 720p/1080p/4k。
        has_watermark: 是否带水印。
        format: 格式 mp4/mov/zip。
        params: 编码参数 JSONB（码率/编码器/帧率）。
        status: 状态 pending/running/succeeded/failed。
        progress: 进度（0~100）。
        output_media_file_id: 产物文件 ID（FK media_files，SET NULL）。
        output_size_bytes: 产物大小（≥0）。
        idempotency_key: 幂等键（非空时唯一）。
        error_reason: 失败原因。
        cache_key: 转码缓存键（成片版本 + 参数 hash，AC-F7-06-2，V4.0）。
        preset_id: 套用的导出预设 ID（弱引用 export_presets，V4.0）。
        name_template: 命名模板（AC-F7-02-3，V4.0）。
        expires_at: 产物过期时间（90 天，AC-F7-06-1，V4.0）。
        ai_label_applied: AI 标识是否已落片（AC-F7-08-1，V4.0）。
        ai_label: AI 标识配置快照 JSONB（V4.0）。
        started_at: 开始时间。
        finished_at: 完成时间。
    """

    __tablename__ = "export_jobs"
    __table_args__ = (
        CheckConstraint(
            text("resolution IN ('720p', '1080p', '4k', '4K')"),
            name="ck_export_jobs_resolution",
        ),
        CheckConstraint(
            text("format IN ('mp4', 'mov', 'zip')"),
            name="ck_export_jobs_format",
        ),
        # 状态 CHECK 同时兼容 V3 四态与 V4.0 六态（02 文档 §13.5）
        CheckConstraint(
            text(
                "status IN ('pending', 'running', 'succeeded', 'failed', "
                "'export_queued', 'transcoding', 'packaging', 'export_ready', "
                "'export_failed', 'expired')"
            ),
            name="ck_export_jobs_status",
        ),
        CheckConstraint(text("progress BETWEEN 0 AND 100"), name="ck_export_jobs_progress"),
        CheckConstraint(
            text("output_size_bytes IS NULL OR output_size_bytes >= 0"),
            name="ck_export_jobs_output_size",
        ),
        Index(
            "uq_export_jobs_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # 转码缓存键：同一参数可被多次复用（复用会新建任务），故非唯一；
        # 与 02 文档 §13.5「唯一索引」的偏差见本次实现说明（唯一约束会阻断
        # 合法的「复用参数重新导出」AC-F7-06-2）。
        Index(
            "idx_export_jobs_cache_key",
            "cache_key",
            unique=False,
            postgresql_where=text("cache_key IS NOT NULL"),
        ),
        Index("idx_export_jobs_project_id_status", "project_id", "status"),
        Index("idx_export_jobs_status_created_at", "status", "created_at"),
        Index("idx_export_jobs_expires_at", "expires_at"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    timeline_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("timelines.id", ondelete="SET NULL"), nullable=True, comment="时间线 ID"
    )
    resolution: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="1080p", comment="分辨率（3 值）"
    )
    has_watermark: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否带水印"
    )
    format: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="mp4", comment="导出格式（3 值）"
    )
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="编码参数")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending", comment="导出状态（4 值）"
    )
    progress: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="进度 0~100"
    )
    output_media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="产物文件 ID"
    )
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="产物大小（字节）")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="幂等键")
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    cache_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="转码缓存键（V4.0）"
    )
    preset_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="导出预设 ID（V4.0）"
    )
    name_template: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="命名模板（V4.0）"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="产物过期时间（V4.0）"
    )
    ai_label_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="AI 标识已落片（V4.0）"
    )
    ai_label: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="AI 标识配置快照（V4.0）"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )


class PlatformAccount(IdMixin, TimestampMixin, Base):
    """三方平台授权：OAuth 授权，token 加密存储（P1，表 platform_accounts）。

    Attributes:
        user_id: 用户 ID（FK users，级联删除）。
        platform: 平台 douyin/kuaishou/wechat_channels/jianying。
        platform_user_id: 平台用户标识。
        access_token_encrypted: 加密 access token。
        refresh_token_encrypted: 加密 refresh token。
        token_expires_at: token 到期时间。
        scope: 授权范围。
        status: 状态 active/expired/revoked。
        authorized_at: 授权时间。
    """

    __tablename__ = "platform_accounts"
    __table_args__ = (
        CheckConstraint(
            text("platform IN ('douyin', 'kuaishou', 'wechat_channels', 'jianying')"),
            name="ck_platform_accounts_platform",
        ),
        CheckConstraint(
            text("status IN ('active', 'expired', 'revoked')"),
            name="ck_platform_accounts_status",
        ),
        UniqueConstraint(
            "user_id", "platform", "platform_user_id",
            name="uq_platform_accounts_user_id_platform_platform_user_id",
        ),
        Index("idx_platform_accounts_user_id", "user_id"),
        Index("idx_platform_accounts_status_token_expires_at", "status", "token_expires_at"),
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="用户 ID"
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="平台（4 值）")
    platform_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="平台用户标识")
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加密 access token")
    refresh_token_encrypted: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="加密 refresh token"
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="token 到期时间"
    )
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="授权范围")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="active", comment="授权状态（3 值）"
    )
    authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, server_default=func.now(), comment="授权时间"
    )


class DistributionRecord(IdMixin, TimestampMixin, Base):
    """分发记录与回链：发布到三方平台的记录（P1，表 distribution_records）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        export_job_id: 导出任务 ID（FK export_jobs，SET NULL）。
        platform_account_id: 平台账号 ID（FK platform_accounts，SET NULL）。
        platform: 平台（同 platform_accounts.platform）。
        external_video_id: 平台视频 ID。
        publish_url: 发布链接。
        title: 发布标题。
        status: 状态 draft/submitting/confirming/succeeded/failed/withdrawn（V4.0 扩展）。
        error_reason: 失败原因。
        fail_reason_user: 失败原因用户语言文案（AC-F7-05-2，V4.0）。
        metadata_snapshot: 发布时元数据快照 JSONB（AC-F7-05-2，V4.0）。
        retry_count: 已重试次数（≥0，V4.0）。
        distributed_at: 分发时间。
    """

    __tablename__ = "distribution_records"
    __table_args__ = (
        CheckConstraint(
            text("platform IN ('douyin', 'kuaishou', 'wechat_channels', 'jianying')"),
            name="ck_distribution_records_platform",
        ),
        CheckConstraint(
            text(
                "status IN ('draft', 'pending', 'submitting', 'uploading', 'confirming', "
                "'succeeded', 'failed', 'withdrawn')"
            ),
            name="ck_distribution_records_status",
        ),
        CheckConstraint(text("retry_count >= 0"), name="ck_distribution_records_retry_count"),
        Index("idx_distribution_records_project_id", "project_id"),
        Index("idx_distribution_records_status", "status"),
        Index("idx_distribution_records_project_id_status", "project_id", "status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    export_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("export_jobs.id", ondelete="SET NULL"), nullable=True, comment="导出任务 ID"
    )
    platform_account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("platform_accounts.id", ondelete="SET NULL"), nullable=True, comment="平台账号 ID"
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, comment="平台（4 值）")
    external_video_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="平台视频 ID")
    publish_url: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="发布链接")
    title: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="发布标题")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending", comment="分发状态（4 值）"
    )
    error_reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    fail_reason_user: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="失败原因用户语言文案（V4.0）"
    )
    metadata_snapshot: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="发布时元数据快照（V4.0）"
    )
    retry_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="已重试次数（V4.0）"
    )
    distributed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="分发时间"
    )


class ExportPreset(IdMixin, TimestampMixin, Base):
    """导出参数预设：用户保存的导出配置，上限 10 个（V4.0，表 export_presets）。

    对齐 AC-F7-01-3：套用预设时权益项按当下会员状态实时校验（不缓存权益
    结论），``params`` 仅存非权益性参数快照。

    Attributes:
        owner_user_id: 预设所有者用户 ID（FK users，级联删除）。
        name: 预设名（用户内唯一）。
        params: 参数快照 JSONB（resolution/aspect/watermark/format/name_template）。
        is_default: 是否默认预设。
    """

    __tablename__ = "export_presets"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_export_presets_owner_user_id_name"),
        Index("idx_export_presets_owner_user_id", "owner_user_id"),
    )

    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="所有者用户 ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="预设名")
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="参数快照")
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否默认预设"
    )

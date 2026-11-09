"""域 E · 资产（Asset）模型。

包含 7 张表（对齐《02 数据库设计文档》§5.5 与 §13.5 V4.0 增量）：
- ``assets`` 资产主表（character/scene/prop/voice）；
- ``asset_versions`` 资产版本（主形象/三视图/表情集等）；
- ``media_files`` 统一媒体文件表（OSS 引用、尺寸、时长、哈希）；
- ``face_embeddings`` 人脸向量（按任务要求使用 DOUBLE PRECISION[]，本机无 pgvector）；
- ``face_authorizations`` 真人照片授权留痕；
- ``tts_voices`` 音色库；
- ``voice_bindings`` 逐角色配音参数与版本历史（V4.0）。

说明：``assets.metadata`` 为 SQLAlchemy 保留属性名，Python 侧以 ``meta`` 映射到同名 DB 列。
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
    Float,
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
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import IdMixin, SoftDeleteMixin, TimestampMixin


class Asset(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """资产主表：可复用创作元素（角色/场景/道具/音色）（表 assets）。

    Attributes:
        project_id: 归属项目 ID（FK projects，SET NULL；NULL 表示跨项目复用）。
        owner_user_id: 资产所有者用户 ID（FK users，SET NULL）。
        asset_type: 资产类型 character/scene/prop/voice。
        name: 资产名称。
        description: 描述。
        status: 状态（9 值，见 00 契约 §6.3）。
        source: 来源 ai_generated/uploaded。
        current_version_id: 当前版本 ID（弱引用 asset_versions）。
        voice_id: 绑定音色 ID（FK tts_voices，SET NULL）。
        tags: 标签（JSONB 字符串数组）。
        labels: 可检索标签（JSONB 字符串数组，≤10 个，AC-F3-11-1，V4.0）。
        voice_status: 音色绑定状态 bound/delisted/pending（AC-F3-04-3，V4.0）。
        meta: 扩展元数据（DB 列名 metadata）。
        approved_at: 通过时间。
        locked_at: 锁定时间。
        deleted_at: 软删除时间。
    """

    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            text("asset_type IN ('character', 'scene', 'prop', 'voice')"),
            name="ck_assets_asset_type",
        ),
        CheckConstraint(
            text(
                "status IN ('planning', 'generating', 'qc_review', 'pending_review', "
                "'approved', 'locked', 'qc_failed', 'rejected', 'deprecated')"
            ),
            name="ck_assets_status",
        ),
        CheckConstraint(
            text("source IN ('ai_generated', 'uploaded')"),
            name="ck_assets_source",
        ),
        CheckConstraint(
            text("voice_status IS NULL OR voice_status IN ('bound', 'delisted', 'pending')"),
            name="ck_assets_voice_status",
        ),
        Index("idx_assets_project_id_asset_type_status", "project_id", "asset_type", "status"),
        Index("idx_assets_owner_user_id", "owner_user_id"),
        Index("idx_assets_status", "status"),
    )

    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, comment="归属项目 ID"
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="所有者用户 ID"
    )
    asset_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="资产类型（4 值）")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="资产名称")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="资产描述")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="planning", comment="资产状态（9 值）"
    )
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="ai_generated", comment="资产来源"
    )
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="当前版本 ID（弱引用 asset_versions）"
    )
    voice_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tts_voices.id", ondelete="SET NULL"), nullable=True, comment="绑定音色 ID"
    )
    tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="标签数组")
    labels: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="可检索标签数组（≤10 个，V4.0）"
    )
    voice_status: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="音色绑定状态 bound/delisted/pending（V4.0）"
    )
    meta: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True, comment="扩展元数据（年龄/戏份权重/外形等）"
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="通过时间"
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="锁定时间"
    )


class AssetVersion(IdMixin, TimestampMixin, Base):
    """资产版本：资产的具体产出（主形象/三视图/表情集/场景/道具参考）（表 asset_versions）。

    Attributes:
        asset_id: 资产 ID（FK assets，级联删除）。
        version_type: 版本类型（7 值）。
        version_no: 序号（≥1，与 asset_id+version_type 联合唯一）。
        media_file_id: 媒体文件 ID（FK media_files，SET NULL）。
        prompt: 生成正向 prompt。
        negative_prompt: 负向 prompt。
        seed: 随机种子。
        model_key: 生成模型键（逻辑关联 model_registry）。
        qc_report_id: 质检报告 ID（弱引用 qc_reports）。
        is_current: 是否当前版本。
        variant_tag: 场景时段/天气变体标签（AC-F3-08-1，V4.0）。
        revision_notes: 重抽反馈原文（AC-F3-03-3，V4.0）。
        cumulative_cost: 该资产累计消耗积分（AC-F3-03-2，V4.0）。
    """

    __tablename__ = "asset_versions"
    __table_args__ = (
        CheckConstraint(
            text(
                "version_type IN ('portrait', 'turnaround_front', 'turnaround_side', "
                "'turnaround_back', 'expression_set', 'scene_ref', 'prop_ref')"
            ),
            name="ck_asset_versions_version_type",
        ),
        CheckConstraint(text("version_no >= 1"), name="ck_asset_versions_version_no"),
        CheckConstraint(
            text("cumulative_cost IS NULL OR cumulative_cost >= 0"),
            name="ck_asset_versions_cumulative_cost",
        ),
        CheckConstraint(
            text("revision_notes IS NULL OR char_length(revision_notes) <= 200"),
            name="ck_asset_versions_revision_notes_len",
        ),
        UniqueConstraint(
            "asset_id", "version_type", "version_no",
            name="uq_asset_versions_asset_id_version_type_version_no",
        ),
        Index("idx_asset_versions_asset_id_version_type", "asset_id", "version_type"),
        Index("idx_asset_versions_is_current", "is_current"),
        Index("idx_asset_versions_asset_id_created_at", "asset_id", "created_at"),
    )

    asset_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, comment="资产 ID"
    )
    version_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="版本类型（7 值）")
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本序号")
    media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="媒体文件 ID"
    )
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="生成 prompt")
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="负向 prompt")
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="随机种子")
    model_key: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="生成模型键")
    qc_report_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="质检报告 ID")
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否当前版本"
    )
    variant_tag: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="变体标签（时段/天气，V4.0）"
    )
    revision_notes: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="重抽反馈原文（V4.0）"
    )
    cumulative_cost: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="资产累计消耗积分（V4.0）"
    )


class MediaFile(IdMixin, TimestampMixin, Base):
    """统一媒体文件表：全部分媒体文件的 OSS 引用与元数据（表 media_files）。

    Attributes:
        owner_user_id: 所有者用户 ID（FK users，SET NULL）。
        project_id: 项目 ID（FK projects，SET NULL）。
        storage_provider: 存储商 cos/oss。
        bucket: 存储桶。
        object_key: 对象键（唯一）。
        url: 访问 URL（可带签名）。
        media_type: 媒体类型 image/video/audio/subtitle/zip/document。
        mime_type: MIME 类型。
        file_size_bytes: 字节数（≥0）。
        width: 宽（像素）。
        height: 高（像素）。
        duration_ms: 时长（毫秒，音视频）。
        checksum_sha256: 内容哈希。
        meta: 扩展元数据（帧率/码率/转码状态等）。
    """

    __tablename__ = "media_files"
    __table_args__ = (
        CheckConstraint(
            text("storage_provider IN ('cos', 'oss')"),
            name="ck_media_files_storage_provider",
        ),
        CheckConstraint(
            text(
                "media_type IN ('image', 'video', 'audio', 'subtitle', 'zip', 'document')"
            ),
            name="ck_media_files_media_type",
        ),
        CheckConstraint(text("file_size_bytes >= 0"), name="ck_media_files_file_size"),
        CheckConstraint(text("width IS NULL OR width >= 0"), name="ck_media_files_width"),
        CheckConstraint(text("height IS NULL OR height >= 0"), name="ck_media_files_height"),
        CheckConstraint(text("duration_ms IS NULL OR duration_ms >= 0"), name="ck_media_files_duration"),
        UniqueConstraint("object_key", name="uq_media_files_object_key"),
        Index("idx_media_files_project_id_media_type", "project_id", "media_type"),
        Index("idx_media_files_checksum_sha256", "checksum_sha256"),
        Index("idx_media_files_created_at", "created_at"),
    )

    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="所有者用户 ID"
    )
    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, comment="项目 ID"
    )
    storage_provider: Mapped[str] = mapped_column(String(16), nullable=False, comment="存储商")
    bucket: Mapped[str] = mapped_column(String(128), nullable=False, comment="存储桶")
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, comment="对象键")
    url: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="访问 URL")
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="媒体类型（6 值）")
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="MIME 类型")
    file_size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="文件字节数"
    )
    width: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="宽度（像素）")
    height: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="高度（像素）")
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="时长（毫秒）")
    checksum_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True, comment="内容哈希")
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="扩展元数据")


class FaceEmbedding(IdMixin, TimestampMixin, Base):
    """人脸向量：角色人脸 embedding，用于一致性比对（表 face_embeddings）。

    按任务要求使用 DOUBLE PRECISION[]（本机 PostgreSQL 未启用 pgvector 扩展），
    相似度比对由应用层计算余弦距离。

    Attributes:
        asset_id: 资产 ID（FK assets，级联删除）。
        asset_version_id: 资产版本 ID（FK asset_versions，级联删除）。
        media_file_id: 来源图媒体文件 ID（FK media_files，SET NULL）。
        face_index: 人脸序号（≥0）。
        embedding: 人脸向量（DOUBLE PRECISION 数组）。
        model_key: 提取模型键。
        quality_score: 质量分（0.000~1.000）。
        bbox: 人脸框 JSONB（{x,y,w,h}）。
    """

    __tablename__ = "face_embeddings"
    __table_args__ = (
        CheckConstraint(text("face_index >= 0"), name="ck_face_embeddings_face_index"),
        CheckConstraint(
            text("quality_score IS NULL OR quality_score BETWEEN 0 AND 1"),
            name="ck_face_embeddings_quality_score",
        ),
        Index("idx_face_embeddings_asset_id", "asset_id"),
    )

    asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="CASCADE"), nullable=True, comment="资产 ID"
    )
    asset_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("asset_versions.id", ondelete="CASCADE"),
        nullable=True,
        comment="资产版本 ID",
    )
    media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="来源图 ID"
    )
    face_index: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="人脸序号"
    )
    embedding: Mapped[list[float]] = mapped_column(
        ARRAY(Float), nullable=False, comment="人脸向量（DOUBLE PRECISION[]）"
    )
    model_key: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="提取模型键")
    quality_score: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True, comment="质量分 0.000~1.000"
    )
    bbox: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="人脸框 {x,y,w,h}")


class FaceAuthorization(IdMixin, TimestampMixin, Base):
    """真人照片授权留痕：时间戳/IP/协议版本存证（表 face_authorizations）。

    Attributes:
        project_id: 项目 ID（FK projects，SET NULL）。
        user_id: 授权提交人用户 ID（FK users，RESTRICT）。
        asset_id: 关联资产 ID（FK assets，SET NULL）。
        authorization_type: 授权类型 self/authorized_other。
        subject_name: 被授权人姓名。
        id_card_hash: 证件号哈希（不存明文）。
        protocol_version: 协议版本。
        agreement_media_id: 协议/授权书存证 ID（FK media_files，SET NULL）。
        similarity_score: 相似度检测分（0.000~1.000）。
        ip: 提交 IP。
        user_agent: 客户端 UA。
        authorized_at: 授权时间。
        expires_at: 授权到期时间。
        status: 授权状态 active/revoked（AC-F3-12-2，V4.0）。
        revoked_at: 撤回时间（V4.0）。
        revoke_reason: 撤回原因（V4.0）。
    """

    __tablename__ = "face_authorizations"
    __table_args__ = (
        CheckConstraint(
            text("authorization_type IN ('self', 'authorized_other')"),
            name="ck_face_authorizations_authorization_type",
        ),
        CheckConstraint(
            text("status IN ('active', 'revoked')"),
            name="ck_face_authorizations_status",
        ),
        CheckConstraint(
            text("similarity_score IS NULL OR similarity_score BETWEEN 0 AND 1"),
            name="ck_face_authorizations_similarity",
        ),
        Index("idx_face_authorizations_user_id", "user_id"),
        Index("idx_face_authorizations_project_id", "project_id"),
        Index("idx_face_authorizations_expires_at", "expires_at"),
    )

    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, comment="项目 ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, comment="授权提交人 ID"
    )
    asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, comment="关联资产 ID"
    )
    authorization_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="授权类型")
    subject_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="被授权人姓名")
    id_card_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True, comment="证件号哈希")
    protocol_version: Mapped[str] = mapped_column(String(32), nullable=False, comment="协议版本")
    agreement_media_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="协议存证 ID"
    )
    similarity_score: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True, comment="相似度检测分"
    )
    ip: Mapped[str | None] = mapped_column(INET, nullable=True, comment="提交 IP")
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="客户端 UA")
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="授权时间"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="授权到期时间"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active", comment="授权状态 active/revoked（V4.0）"
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="撤回时间（V4.0）"
    )
    revoke_reason: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="撤回原因（V4.0）"
    )


class TtsVoice(IdMixin, TimestampMixin, Base):
    """音色库：可供角色绑定的 TTS 音色，含下架状态与授权到期（表 tts_voices）。

    Attributes:
        voice_key: 音色键（唯一）。
        name: 音色名。
        gender: 性别 male/female/neutral。
        language: 语言（BCP-47）。
        style_tags: 风格标签（JSONB 字符串数组）。
        provider: 提供方。
        sample_media_id: 试听样音 ID（FK media_files，SET NULL）。
        status: 状态 available/offline/deprecated。
        offline_at: 下架时间。
        license_expires_at: 授权到期时间。
    """

    __tablename__ = "tts_voices"
    __table_args__ = (
        CheckConstraint(
            text("gender IS NULL OR gender IN ('male', 'female', 'neutral')"),
            name="ck_tts_voices_gender",
        ),
        CheckConstraint(
            text("status IN ('available', 'offline', 'deprecated')"),
            name="ck_tts_voices_status",
        ),
        UniqueConstraint("voice_key", name="uq_tts_voices_voice_key"),
        Index("idx_tts_voices_status", "status"),
        Index("idx_tts_voices_language_status", "language", "status"),
    )

    voice_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="音色键")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="音色名")
    gender: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="性别")
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="zh-CN", comment="语言"
    )
    style_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="风格标签数组")
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="提供方")
    sample_media_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="试听样音 ID"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="available", comment="音色状态"
    )
    offline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="下架时间"
    )
    license_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="授权到期时间"
    )


class VoiceBinding(IdMixin, TimestampMixin, Base):
    """逐角色配音参数与版本历史（V4.0，表 voice_bindings）。

    支撑 US-F6-07「按角色单独试听并调整语速/语调后再全剧应用」：
    ``params`` 为当前生效参数，``history`` 保留最近 3 版供回退
    （F6.7 状态机），``confirmed_snapshot`` 记录 30s 样音确认时的参数
    快照以保证全剧 TTS 可追溯（AC-F6-01-3）。

    Attributes:
        asset_id: 角色资产 ID（FK assets，级联删除；角色唯一）。
        voice_id: 绑定音色 ID（FK tts_voices，SET NULL）。
        params: 参数 JSONB {rate, pitch_shift, emotion_override[]}。
        history: 历史参数 JSONB 数组（≤3 版）。
        preview_confirmed: 30s 样音是否已确认。
        confirmed_snapshot: 确认时的参数快照 JSONB。
        preview_media_id: 最近一次试听样音媒体文件 ID（FK media_files，SET NULL）。
        affected_line_count: 最近一次重试听受影响台词数（AC-F6-07-2）。
        qc_warning: 语速调慢导致的质检提示 JSONB（AC-F6-07-3）。
    """

    __tablename__ = "voice_bindings"
    __table_args__ = (
        CheckConstraint(text("asset_id > 0"), name="ck_voice_bindings_asset_id"),
        UniqueConstraint("asset_id", name="uq_voice_bindings_asset_id"),
        Index("idx_voice_bindings_voice_id", "voice_id"),
    )

    asset_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, comment="角色资产 ID"
    )
    voice_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tts_voices.id", ondelete="SET NULL"), nullable=True, comment="绑定音色 ID"
    )
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="配音参数")
    history: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="历史参数（≤3 版）")
    preview_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="样音是否已确认"
    )
    confirmed_snapshot: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="确认时参数快照"
    )
    preview_media_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("media_files.id", ondelete="SET NULL"),
        nullable=True,
        comment="试听样音媒体文件 ID",
    )
    affected_line_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="受影响台词数"
    )
    qc_warning: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="语速超时长质检提示"
    )

# ---------------------------------------------------------------------------
# P2-14 修复：media_files.file_size_bytes 自动回填
# 历史上多个写路径（TTS 配音、音色样音、导出等）创建 MediaFile 时把大小写死
# 为 0，导致列表/分发页展示的文件大小恒为 0。这里在 INSERT 前统一按落盘文件
# 回填，避免各调用点遗漏。
# ---------------------------------------------------------------------------
from sqlalchemy import event as _sa_event  # noqa: E402

from app.infra.storage import storage as _media_storage  # noqa: E402


@_sa_event.listens_for(MediaFile, "before_insert")
def _fill_media_file_size(_mapper, _connection, target: MediaFile) -> None:
    """在写入 media_files 前按对象键回填真实文件字节数。

    Args:
        _mapper: SQLAlchemy mapper（未使用）。
        _connection: 数据库连接（未使用）。
        target: 待插入的 MediaFile 实例。

    Returns:
        None。

    Raises:
        无：读取文件大小失败时保持原值 0，不阻断写库。
    """
    if int(target.file_size_bytes or 0) > 0 or not target.object_key:
        return
    try:
        size = _media_storage.size_of(str(target.object_key))
    except Exception:  # noqa: BLE001 回填失败不影响主流程
        return
    if size > 0:
        target.file_size_bytes = int(size)

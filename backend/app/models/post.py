"""域 I · 后期（Post）模型。

包含 6 张表（对齐《02 数据库设计文档》§5.9 与 §13.5 V4.0 增量）：
- ``timelines`` 时间线（母版/预览版，duration_ms 为成片实际时长唯一权威）；
- ``timeline_clips`` 时间线片段（视频/音频/字幕/BGM 轨元素）；
- ``subtitle_styles`` 字幕样式模板；
- ``subtitle_lines`` 逐条字幕（可编辑/合并/拆分，V4.0）；
- ``mix_profiles`` 混音画像（响度标准化与逐轨音量，V4.0）；
- ``bgm_tracks`` BGM 曲库与版权信息。

说明：``subtitle_lines.text`` 列名与 SQLAlchemy 的 ``text()`` 构造器同名，
本模块统一以别名 ``sa_text`` 引用该构造器，避免类体内名称遮蔽。
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
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import IdMixin, TimestampMixin


class Timeline(IdMixin, TimestampMixin, Base):
    """时间线：后期时间线容器，区分母版与预览版（表 timelines）。

    成片实际输出时长以 ``duration_ms`` 为唯一权威（对齐 00 契约 §6.7 时长口径）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        episode_id: 集 ID（FK episodes，SET NULL）。
        cut_type: 版本类型 master/preview。
        version_no: 版本号（≥1）。
        status: 状态 draft/composing/qc_review/pending_review/approved/failed。
        duration_ms: 总时长（毫秒，≥0）。
        fps: 帧率（24/25/30）。
        resolution: 分辨率 720p/1080p/4k。
        rendered_media_file_id: 合成产物媒体文件 ID（FK media_files，SET NULL）。
        edit_state: 编辑器状态 JSONB。
        change_summary: 成片版本对比摘要 JSONB（重配音角色/换曲/字幕条数，V4.0）。
        approved_at: 通过时间。
    """

    __tablename__ = "timelines"
    __table_args__ = (
        CheckConstraint(
            sa_text("cut_type IN ('master', 'preview')"),
            name="ck_timelines_cut_type",
        ),
        CheckConstraint(sa_text("version_no >= 1"), name="ck_timelines_version_no"),
        CheckConstraint(
            sa_text(
                "status IN ('draft', 'composing', 'qc_review', 'pending_review', "
                "'approved', 'failed')"
            ),
            name="ck_timelines_status",
        ),
        CheckConstraint(sa_text("duration_ms >= 0"), name="ck_timelines_duration_ms"),
        CheckConstraint(sa_text("fps IN (24, 25, 30)"), name="ck_timelines_fps"),
        CheckConstraint(
            sa_text("resolution IN ('720p', '1080p', '4k')"),
            name="ck_timelines_resolution",
        ),
        UniqueConstraint(
            "project_id", "episode_id", "cut_type", "version_no",
            name="uq_timelines_project_id_episode_id_cut_type_version_no",
        ),
        Index("idx_timelines_project_id_cut_type", "project_id", "cut_type"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    episode_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True, comment="集 ID"
    )
    cut_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="版本类型（2 值）")
    version_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("1"), comment="版本号"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="draft", comment="时间线状态（6 值）"
    )
    duration_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=sa_text("0"), comment="总时长（毫秒）"
    )
    fps: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=sa_text("30"), comment="帧率"
    )
    resolution: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="1080p", comment="分辨率（3 值）"
    )
    rendered_media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="合成产物 ID"
    )
    edit_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="编辑器状态")
    change_summary: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="成片版本对比摘要（V4.0）"
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="通过时间"
    )


class TimelineClip(IdMixin, TimestampMixin, Base):
    """时间线片段：视频/音频/字幕/BGM 轨元素（表 timeline_clips）。

    Attributes:
        timeline_id: 时间线 ID（FK timelines，级联删除）。
        track_type: 轨道类型 video/audio/subtitle/bgm。
        source_type: 源类型 shot_version/media_file/bgm/tts/voice_clip。
        source_id: 源 ID（多态）。
        order_index: 轨内序号（≥0）。
        start_ms: 时间线起点（毫秒，≥0）。
        end_ms: 时间线终点（毫秒，必须大于 start_ms）。
        in_point_ms: 源入点（毫秒，≥0）。
        out_point_ms: 源出点（毫秒，≥0）。
        transition: 转场参数 JSONB。
        volume_db: 音量（dB，对白基准 -6）。
        subtitle_style_id: 字幕样式 ID（FK subtitle_styles，SET NULL）。
        content: 字幕文本（字幕轨用）。
    """

    __tablename__ = "timeline_clips"
    __table_args__ = (
        CheckConstraint(
            sa_text("track_type IN ('video', 'audio', 'subtitle', 'bgm')"),
            name="ck_timeline_clips_track_type",
        ),
        CheckConstraint(
            sa_text(
                "source_type IS NULL OR source_type IN "
                "('shot_version', 'media_file', 'bgm', 'tts', 'voice_clip')"
            ),
            name="ck_timeline_clips_source_type",
        ),
        CheckConstraint(sa_text("order_index >= 0"), name="ck_timeline_clips_order_index"),
        CheckConstraint(sa_text("start_ms >= 0"), name="ck_timeline_clips_start_ms"),
        CheckConstraint(sa_text("end_ms > start_ms"), name="ck_timeline_clips_time_range"),
        CheckConstraint(sa_text("in_point_ms IS NULL OR in_point_ms >= 0"), name="ck_timeline_clips_in_point"),
        CheckConstraint(sa_text("out_point_ms IS NULL OR out_point_ms >= 0"), name="ck_timeline_clips_out_point"),
        Index("idx_timeline_clips_timeline_id_track_type_order_index", "timeline_id", "track_type", "order_index"),
    )

    timeline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("timelines.id", ondelete="CASCADE"), nullable=False, comment="时间线 ID"
    )
    track_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="轨道类型（4 值）")
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="源类型（4 值）")
    source_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="源 ID（多态）")
    order_index: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0"), comment="轨内序号"
    )
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="时间线起点（毫秒）")
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="时间线终点（毫秒）")
    in_point_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="源入点（毫秒）")
    out_point_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="源出点（毫秒）")
    transition: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="转场参数")
    volume_db: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True, comment="音量（dB）")
    subtitle_style_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("subtitle_styles.id", ondelete="SET NULL"), nullable=True, comment="字幕样式 ID"
    )
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="字幕文本")


class SubtitleStyle(IdMixin, TimestampMixin, Base):
    """字幕样式模板：字幕视觉样式模板（全局或项目级）（表 subtitle_styles）。

    Attributes:
        project_id: 归属项目 ID（FK projects，级联删除；NULL 为全局）。
        name: 样式名。
        font_family: 字体。
        font_size: 字号（≥1）。
        color: 颜色（十六进制）。
        stroke: 描边参数 JSONB。
        position: 位置 top/middle/bottom。
        safe_area: 安全区 JSONB（竖屏安全区）。
        is_default: 是否默认样式。
        is_custom: 是否用户自定义样式（P2 预留，V4.0）。
        owner_user_id: 自定义样式所有者用户 ID（FK users，SET NULL，V4.0）。
    """

    __tablename__ = "subtitle_styles"
    __table_args__ = (
        CheckConstraint(
            sa_text("position IS NULL OR position IN ('top', 'middle', 'bottom')"),
            name="ck_subtitle_styles_position",
        ),
        CheckConstraint(sa_text("font_size IS NULL OR font_size >= 1"), name="ck_subtitle_styles_font_size"),
        Index("idx_subtitle_styles_project_id", "project_id"),
        Index("idx_subtitle_styles_is_default", "is_default"),
    )

    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, comment="归属项目 ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="样式名")
    font_family: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="字体")
    font_size: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="字号")
    color: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="颜色（十六进制）")
    stroke: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="描边参数")
    position: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="位置（3 值）")
    safe_area: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="安全区")
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false"), comment="是否默认样式"
    )
    is_custom: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false"), comment="是否自定义样式（V4.0）"
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="所有者用户 ID（V4.0）"
    )


class SubtitleLine(IdMixin, TimestampMixin, Base):
    """逐条字幕：支持改文本/合并/拆分/出入点微调（V4.0，表 subtitle_lines）。

    对齐 AC-F6-02-2 与 AC-F6-08-1~3：每条含 ``start_ms / end_ms / text /
    speaker / style_ref``；``edited`` 标记人工编辑（逐条编辑溯源）；
    出入点调整导致的音画同步误差 > 100ms 由服务层拒绝保存。

    Attributes:
        timeline_id: 时间线 ID（FK timelines，级联删除）。
        project_id: 项目 ID（FK projects，级联删除）。
        line_index: 序号（时间线内唯一，≥1）。
        start_ms: 起始毫秒（≥0）。
        end_ms: 结束毫秒（> start_ms）。
        content: 字幕文本（单句 ≤20 字）。
        speaker: 说话人。
        style_ref: 样式模板 ID（弱引用 subtitle_styles）。
        edited: 是否被人工编辑（V4.0）。
        merged_from: 合并来源序号数组（合并操作留痕，V4.0）。
        out_of_safe_area: 是否超出竖屏安全区（AC-F6-02-3，V4.0）。
    """

    __tablename__ = "subtitle_lines"
    __table_args__ = (
        CheckConstraint(sa_text("line_index >= 1"), name="ck_subtitle_lines_line_index"),
        CheckConstraint(sa_text("start_ms >= 0"), name="ck_subtitle_lines_start_ms"),
        CheckConstraint(sa_text("end_ms > start_ms"), name="ck_subtitle_lines_time_range"),
        UniqueConstraint(
            "timeline_id", "line_index", name="uq_subtitle_lines_timeline_id_line_index"
        ),
        Index("idx_subtitle_lines_timeline_id_start_ms", "timeline_id", "start_ms"),
        Index("idx_subtitle_lines_project_id", "project_id"),
    )

    timeline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("timelines.id", ondelete="CASCADE"), nullable=False, comment="时间线 ID"
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    line_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="序号")
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="起始毫秒")
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="结束毫秒")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="字幕文本")
    speaker: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="说话人")
    style_ref: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="样式模板 ID")
    edited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false"), comment="是否人工编辑（V4.0）"
    )
    merged_from: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="合并来源序号数组")
    out_of_safe_area: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false"), comment="是否超出安全区"
    )


class MixProfile(IdMixin, TimestampMixin, Base):
    """混音画像：响度标准化与逐轨音量（V4.0，表 mix_profiles）。

    对齐 AC-F6-10-1~2：``loudness_target`` 默认 -14.0 LUFS，
    ``true_peak_db`` 默认 -1.0 dBTP；``track_volumes`` 记录对白/BGM/音效轨
    相对自动混音值的偏移（±6dB）与静音位；用户手动混音在标准化之后应用。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        timeline_id: 时间线 ID（FK timelines，级联删除；每时间线一条）。
        loudness_target: 响度目标 LUFS（默认 -14.0）。
        true_peak_db: 真峰值上限 dBTP（默认 -1.0）。
        normalize_enabled: 响度标准化开关（默认开）。
        track_volumes: 逐轨音量 JSONB {dialogue:0.0, bgm:0.0, sfx:0.0}。
        track_muted: 静音轨 JSONB 数组。
        measured_lufs: 实测响度（导出处回填）。
        clipped: 是否检测到超 0dBFS。
    """

    __tablename__ = "mix_profiles"
    __table_args__ = (
        CheckConstraint(
            sa_text("loudness_target BETWEEN -30.0 AND -6.0"),
            name="ck_mix_profiles_loudness_target",
        ),
        CheckConstraint(
            sa_text("true_peak_db BETWEEN -6.0 AND 0.0"),
            name="ck_mix_profiles_true_peak",
        ),
        UniqueConstraint("timeline_id", name="uq_mix_profiles_timeline_id"),
        Index("idx_mix_profiles_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    timeline_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("timelines.id", ondelete="CASCADE"), nullable=False, comment="时间线 ID"
    )
    loudness_target: Mapped[Decimal] = mapped_column(
        Numeric(4, 1), nullable=False, server_default=sa_text("-14.0"), comment="响度目标 LUFS"
    )
    true_peak_db: Mapped[Decimal] = mapped_column(
        Numeric(3, 1), nullable=False, server_default=sa_text("-1.0"), comment="真峰值上限 dBTP"
    )
    normalize_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true"), comment="响度标准化开关"
    )
    track_volumes: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="逐轨音量偏移")
    track_muted: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="静音轨数组")
    measured_lufs: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 1), nullable=True, comment="实测响度"
    )
    clipped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false"), comment="是否超 0dBFS"
    )


class BgmTrack(IdMixin, TimestampMixin, Base):
    """BGM 曲库：含版权到期字段（表 bgm_tracks）。

    Attributes:
        title: 曲名。
        artist: 艺术家。
        mood_tags: 情绪标签（JSONB 字符串数组）。
        bpm: 节拍（≥0）。
        duration_ms: 时长（毫秒，≥0）。
        media_file_id: 音频文件 ID（FK media_files，SET NULL）。
        license_type: 授权类型 self/third_party。
        license_scope: 授权范围 platform_only/distribution_ok（AC-F6-03-2，V4.0）。
        license_expires_at: 版权到期时间（即文档 13.5 的 ``expires_at``）。
        is_active: 是否上架。
    """

    __tablename__ = "bgm_tracks"
    __table_args__ = (
        CheckConstraint(sa_text("bpm IS NULL OR bpm >= 0"), name="ck_bgm_tracks_bpm"),
        CheckConstraint(sa_text("duration_ms >= 0"), name="ck_bgm_tracks_duration_ms"),
        CheckConstraint(
            sa_text("license_type IS NULL OR license_type IN ('self', 'third_party')"),
            name="ck_bgm_tracks_license_type",
        ),
        CheckConstraint(
            sa_text(
                "license_scope IS NULL OR license_scope IN "
                "('platform_only', 'distribution_ok')"
            ),
            name="ck_bgm_tracks_license_scope",
        ),
        Index("idx_bgm_tracks_mood_tags_gin", "mood_tags", postgresql_using="gin"),
        Index("idx_bgm_tracks_license_expires_at", "license_expires_at"),
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="曲名")
    artist: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="艺术家")
    mood_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="情绪标签数组")
    bpm: Mapped[int | None] = mapped_column(SmallInteger, nullable=True, comment="节拍")
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0"), comment="时长（毫秒）"
    )
    media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="音频文件 ID"
    )
    license_type: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="授权类型")
    license_scope: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="授权范围 platform_only/distribution_ok（V4.0）"
    )
    license_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="版权到期时间"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true"), comment="是否上架"
    )

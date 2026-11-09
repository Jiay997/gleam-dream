"""域 F · 分镜（Storyboard）模型。

包含 7 张表（对齐《02 数据库设计文档》§5.6 与 §13.5 V4.0 增量）：
- ``storyboards`` 分镜板（按集）；
- ``shots`` 镜头（景别/运镜/情绪/prompt/锚点）；
- ``shot_versions`` 镜头生成版本（保留 3 版）；
- ``storyboard_layouts`` 画布布局元数据（P1）；
- ``director_notes`` 导演策略（节奏模式/节奏曲线/机位规则，V4.0）；
- ``storyboard_ops`` 画布操作历史环形栈（V4.0，≤100 步）；
- ``storyboard_templates`` 用户自定义分镜模板（V4.0，≤20 个）。
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


class Storyboard(IdMixin, TimestampMixin, Base):
    """分镜板：某集的镜头有序集合容器（表 storyboards）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        episode_id: 集 ID（FK episodes，级联删除）。
        version_no: 版本号（≥1，与 episode_id 联合唯一）。
        status: 状态 draft/qc_review/pending_review/approved/locked。
        shot_count: 镜头数（≥0）。
        estimated_duration_ms: 预估总时长（毫秒，≥0）。
        locked_at: 锁定时间。
    """

    __tablename__ = "storyboards"
    __table_args__ = (
        CheckConstraint(text("version_no >= 1"), name="ck_storyboards_version_no"),
        CheckConstraint(
            text("status IN ('draft', 'qc_review', 'pending_review', 'approved', 'locked')"),
            name="ck_storyboards_status",
        ),
        CheckConstraint(text("shot_count >= 0"), name="ck_storyboards_shot_count"),
        CheckConstraint(
            text("estimated_duration_ms >= 0"), name="ck_storyboards_estimated_duration"
        ),
        UniqueConstraint("episode_id", "version_no", name="uq_storyboards_episode_id_version_no"),
        Index("idx_storyboards_project_id_episode_id", "project_id", "episode_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    episode_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, comment="集 ID"
    )
    version_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), comment="版本号"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="draft", comment="分镜板状态（5 值）"
    )
    shot_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="镜头数"
    )
    estimated_duration_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="预估总时长（毫秒）"
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="锁定时间"
    )


class Shot(IdMixin, TimestampMixin, Base):
    """镜头：分镜最小单位，含景别/运镜/情绪/prompt/锚点（表 shots）。

    Attributes:
        storyboard_id: 分镜板 ID（FK storyboards，级联删除）。
        project_id: 项目 ID（FK projects，级联删除）。
        episode_id: 集 ID（FK episodes，级联删除）。
        scene_id: 场次 ID（FK scenes，SET NULL）。
        shot_no: 镜号（分镜板内唯一，≥1）。
        order_index: 展示序（≥0）。
        shot_size: 景别（6 值）。
        camera_move: 运镜（9 值）。
        emotion_tag: 情绪标签（8 值）。
        duration_ms: 时长（毫秒，1000~15000）。
        dialogue: 对白。
        action_desc: 动作/画面描述。
        image_prompt: 关键帧 prompt。
        final_prompt: 最终组装 prompt（用户可见可改）。
        negative_prompt: 负向 prompt。
        asset_anchor_ids: 锚点资产版本 ID 数组（JSONB）。
        character_asset_ids: 出场角色资产 ID 数组（JSONB）。
        scene_asset_id: 场景资产 ID（FK assets，SET NULL）。
        prop_asset_ids: 道具资产 ID 数组（JSONB）。
        composition_spec: 整幅要素画面构图（CompositionSpec JSON，V6.13 新增）。
        first_frame_media_id: 首帧图 ID（FK media_files，SET NULL）。
        status: 镜头状态（11 值，见 00 契约 §6.4）。
        is_draft_preview: 是否草稿预览生成。
        locked: 镜头级锁定位（预留，Q16 默认整集解锁，V4.0）。
        upstream_changed: 上游变更后已生成产物标记（AC-F4-06-3，V4.0）。
        redraft_count: 静帧重出次数（≤3，AC-F4-03-3，V4.0）。
        locked_at: 锁定时间。
        duration_source: 时长来源 strategy/manual/audio（V6.0 新增，F4.13）。
        duration_manual: 用户是否手工锁定时长（V6.0 新增）。
        line_coverage_pct: 镜头台词覆盖率（V6.0 新增）。
        duration_drift_ms: 音频驱动时长与设定时长差值（V6.0 新增）。
        long_line: 是否命中超长句拆分提示（V6.0 新增）。
    """

    __tablename__ = "shots"
    __table_args__ = (
        CheckConstraint(text("shot_no >= 1"), name="ck_shots_shot_no"),
        CheckConstraint(text("order_index >= 0"), name="ck_shots_order_index"),
        CheckConstraint(
            text(
                "shot_size IN ('extreme_wide', 'wide', 'medium', 'close_up', "
                "'extreme_close_up', 'over_shoulder')"
            ),
            name="ck_shots_shot_size",
        ),
        CheckConstraint(
            text(
                "camera_move IN ('static', 'pan', 'tilt', 'dolly_in', 'dolly_out', "
                "'tracking', 'orbit', 'handheld', 'crane')"
            ),
            name="ck_shots_camera_move",
        ),
        CheckConstraint(
            text(
                "emotion_tag IS NULL OR emotion_tag IN ('calm', 'joy', 'anger', 'sadness', "
                "'fear', 'surprise', 'tension', 'romance')"
            ),
            name="ck_shots_emotion_tag",
        ),
        # 单镜头时长区间 1000~15000ms（对齐 00 契约 §6.8 与种子配置）
        CheckConstraint(
            text("duration_ms BETWEEN 1000 AND 15000"),
            name="ck_shots_duration_ms",
        ),
        CheckConstraint(
            text(
                "status IN ('draft', 'qc_review', 'pending_review', 'approved', 'locked', "
                "'render_queued', 'rendering', 'rendered', 'qc_failed', 'render_failed', "
                "'needs_manual')"
            ),
            name="ck_shots_status",
        ),
        CheckConstraint(
            text("redraft_count IS NULL OR redraft_count BETWEEN 0 AND 3"),
            name="ck_shots_redraft_count",
        ),
        UniqueConstraint("storyboard_id", "shot_no", name="uq_shots_storyboard_id_shot_no"),
        Index("idx_shots_storyboard_id_order_index", "storyboard_id", "order_index"),
        Index("idx_shots_status", "status"),
        Index("idx_shots_project_id_status", "project_id", "status"),
        Index("idx_shots_asset_anchor_ids_gin", "asset_anchor_ids", postgresql_using="gin"),
        Index("idx_shots_prop_asset_ids_gin", "prop_asset_ids", postgresql_using="gin"),
        Index("idx_shots_scene_asset_id", "scene_asset_id"),
    )

    storyboard_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storyboards.id", ondelete="CASCADE"), nullable=False, comment="分镜板 ID"
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    episode_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="CASCADE"), nullable=False, comment="集 ID"
    )
    scene_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True, comment="场次 ID"
    )
    shot_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="镜号")
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="展示序")
    shot_size: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="medium", comment="景别（6 值）"
    )
    camera_move: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="static", comment="运镜（9 值）"
    )
    emotion_tag: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="情绪标签")
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3000"), comment="时长（毫秒，1000~15000）"
    )
    dialogue: Mapped[str | None] = mapped_column(Text, nullable=True, comment="对白")
    action_desc: Mapped[str | None] = mapped_column(Text, nullable=True, comment="动作/画面描述")
    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="关键帧 prompt")
    final_prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="最终组装 prompt")
    negative_prompt: Mapped[str | None] = mapped_column(Text, nullable=True, comment="负向 prompt")
    asset_anchor_ids: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="锚点资产版本 ID 数组"
    )
    character_asset_ids: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="出场角色资产 ID 数组"
    )
    scene_asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, comment="场景资产 ID"
    )
    prop_asset_ids: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="道具资产 ID 数组"
    )
    composition_spec: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="整幅要素画面构图（CompositionSpec，9:16 场景+角色+道具布局）",
    )
    script_anchor: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="剧本锚点（script_text 剧本原文句 + plot_function 剧情功能标签 + cause_from_prev 与前镜因果）",
    )
    first_frame_media_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="首帧图 ID"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="draft", comment="镜头状态（11 值）"
    )
    is_draft_preview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否草稿预览生成"
    )
    locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="镜头级锁定位（预留，V4.0）"
    )
    upstream_changed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="上游已变更标记（V4.0）"
    )
    redraft_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="静帧重出次数 ≤3（V4.0）"
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="锁定时间"
    )
    # ---- V6.0 音频驱动时长增量字段（F4.13）----
    duration_source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="strategy", comment="时长来源 strategy/manual/audio"
    )
    duration_manual: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否手工锁定时长"
    )
    line_coverage_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("100"), comment="台词覆盖率"
    )
    duration_drift_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="音频时长漂移（ms）"
    )
    long_line: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="超长句提示"
    )


class ShotVersion(IdMixin, TimestampMixin, Base):
    """镜头生成版本：每个镜头多次生成的版本，保留 3 版（表 shot_versions）。

    Attributes:
        shot_id: 镜头 ID（FK shots，级联删除）。
        version_no: 版本号（镜头内唯一，≥1）。
        media_file_id: 产物文件 ID（FK media_files，SET NULL）。
        render_task_id: 来源渲染任务 ID（FK render_tasks，SET NULL）。
        qc_report_id: 质检报告 ID（弱引用 qc_reports）。
        prompt_snapshot: 生成时 prompt 快照。
        seed: 随机种子。
        is_current: 是否当前版本。
        is_locked: 是否锁定。
        label: 版本预设标签 color_good/action_right/expression_natural/other（AC-F5-03-3，V4.0）。
        note: 版本备注（≤100 字，AC-F5-03-3，V4.0）。
    """

    __tablename__ = "shot_versions"
    __table_args__ = (
        CheckConstraint(text("version_no >= 1"), name="ck_shot_versions_version_no"),
        CheckConstraint(
            text(
                "label IS NULL OR label IN ('color_good', 'action_right', "
                "'expression_natural', 'other')"
            ),
            name="ck_shot_versions_label",
        ),
        CheckConstraint(
            text("note IS NULL OR char_length(note) <= 100"),
            name="ck_shot_versions_note_len",
        ),
        UniqueConstraint("shot_id", "version_no", name="uq_shot_versions_shot_id_version_no"),
        Index("idx_shot_versions_shot_id_created_at", "shot_id", "created_at"),
        Index("idx_shot_versions_render_task_id", "render_task_id"),
    )

    shot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False, comment="镜头 ID"
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    media_file_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("media_files.id", ondelete="SET NULL"), nullable=True, comment="产物文件 ID"
    )
    render_task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("render_tasks.id", ondelete="SET NULL"), nullable=True, comment="来源任务 ID"
    )
    qc_report_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="质检报告 ID")
    prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True, comment="生成时 prompt")
    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="随机种子")
    is_current: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否当前版本"
    )
    is_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否锁定"
    )
    label: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="版本预设标签（4 值，V4.0）"
    )
    note: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="版本备注（≤100 字，V4.0）"
    )


class StoryboardLayout(IdMixin, TimestampMixin, Base):
    """画布布局元数据：分镜画布的拖拽布局与视口状态，按用户维度保存（P1，表 storyboard_layouts）。

    Attributes:
        storyboard_id: 分镜板 ID（FK storyboards，级联删除）。
        user_id: 用户 ID（FK users，级联删除）。
        layout: 布局 JSONB（镜头坐标/分组/连线）。
        viewport: 视口 JSONB（{x,y,zoom}）。
    """

    __tablename__ = "storyboard_layouts"
    __table_args__ = (
        UniqueConstraint(
            "storyboard_id", "user_id", name="uq_storyboard_layouts_storyboard_id_user_id"
        ),
        Index("idx_storyboard_layouts_storyboard_id", "storyboard_id"),
    )

    storyboard_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storyboards.id", ondelete="CASCADE"), nullable=False, comment="分镜板 ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="用户 ID"
    )
    layout: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="画布布局")
    viewport: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="视口状态")


class DirectorNote(IdMixin, TimestampMixin, Base):
    """导演策略：全剧/按集的分镜策略与节奏模式（V4.0，表 director_notes）。

    对齐 AC-F4-01-2 与 AC-F4-07-1：``pacing_mode`` 三选一（快节奏/均衡/
    慢节奏），拆解参数按模式取值；``rhythm_curve`` 为「集号 → 节奏值」数组；
    ``closeup_density`` 为高潮特写密度；``dialogue_camera_rule`` 为对话机位规则。

    作用域：``episode_id`` 为 NULL 表示全剧策略（一项目一条），非 NULL 表示
    该集单独覆盖（AC-F4-07-3 重拆保护）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        episode_id: 集 ID（FK episodes，级联删除；NULL 表示全剧策略）。
        pacing_mode: 节奏模式 fast/balanced/slow。
        rhythm_curve: 节奏曲线 JSONB（[{ep_no, rhythm}]）。
        closeup_density: 特写密度（0.00~1.00）。
        dialogue_camera_rule: 对话机位规则文本。
        strategy_notes: 其余策略说明（叙事节奏曲线/高潮密度等）。
        template_id: 套用的分镜模板 ID（V4.0）。
        created_by: 生成用户 ID（FK users，SET NULL）。
    """

    __tablename__ = "director_notes"
    __table_args__ = (
        CheckConstraint(
            text("pacing_mode IN ('fast', 'balanced', 'slow')"),
            name="ck_director_notes_pacing_mode",
        ),
        CheckConstraint(
            text("closeup_density IS NULL OR closeup_density BETWEEN 0 AND 1"),
            name="ck_director_notes_closeup_density",
        ),
        UniqueConstraint("project_id", "episode_id", name="uq_director_notes_project_id_episode_id"),
        Index("idx_director_notes_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    episode_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("episodes.id", ondelete="CASCADE"), nullable=True, comment="集 ID（NULL=全剧）"
    )
    pacing_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="balanced", comment="节奏模式（3 值）"
    )
    rhythm_curve: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="节奏曲线")
    closeup_density: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 2), nullable=True, comment="特写密度 0~1"
    )
    dialogue_camera_rule: Mapped[str | None] = mapped_column(Text, nullable=True, comment="对话机位规则")
    strategy_notes: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="其余策略说明")
    template_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="套用模板 ID")
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="生成用户 ID"
    )


class StoryboardOp(IdMixin, TimestampMixin, Base):
    """画布操作历史环形栈（V4.0，表 storyboard_ops）。

    对齐 AC-F4-09-1~2：按用户维度保留最近 100 步结构化操作（增/删/移/
    批量改），每步含 ``seq / op_type / target_shot_ids / before / after /
    at``；回退到某步时以 ``snapshot`` 恢复（被删镜头以原参数恢复）。

    Attributes:
        storyboard_id: 分镜板 ID（FK storyboards，级联删除）。
        user_id: 操作人用户 ID（FK users，级联删除）。
        op_history: 操作历史 JSONB 数组（环形 ≤100 步）。
        last_seq: 末步序号（≥0）。
        snapshot: 当前完整镜头快照 JSONB（回退恢复用）。
    """

    __tablename__ = "storyboard_ops"
    __table_args__ = (
        UniqueConstraint(
            "storyboard_id", "user_id", name="uq_storyboard_ops_storyboard_id_user_id"
        ),
        CheckConstraint(text("last_seq >= 0"), name="ck_storyboard_ops_last_seq"),
        Index("idx_storyboard_ops_storyboard_id", "storyboard_id"),
    )

    storyboard_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("storyboards.id", ondelete="CASCADE"), nullable=False, comment="分镜板 ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="操作人用户 ID"
    )
    op_history: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="操作历史数组（环形 ≤100）"
    )
    last_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="末步序号"
    )
    snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="当前镜头快照")


class StoryboardTemplate(IdMixin, TimestampMixin, Base):
    """分镜模板：机位规则条目集合，跨项目可用（V4.0，表 storyboard_templates）。

    对齐 AC-F4-10-1~2：``rules`` 为机位规则条目（≤10 条，每条 ≤50 字），
    以「条目」形式注入导演 Agent 策略上下文；用户自定义模板上限 20 个。

    Attributes:
        owner_user_id: 模板所有者用户 ID（FK users，级联删除）。
        name: 模板名。
        kind: 模板类型 dialogue/action/transition/custom。
        rules: 机位规则条目 JSONB 字符串数组（≤10 条）。
        description: 适用说明。
        is_builtin: 是否内置模板（内置不受用户上限与删除约束）。
    """

    __tablename__ = "storyboard_templates"
    __table_args__ = (
        CheckConstraint(
            text("kind IN ('dialogue', 'action', 'transition', 'custom')"),
            name="ck_storyboard_templates_kind",
        ),
        Index("idx_storyboard_templates_owner_user_id", "owner_user_id"),
    )

    owner_user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="所有者用户 ID"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="模板名")
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="custom", comment="模板类型（4 值）"
    )
    rules: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="机位规则条目（≤10）")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="适用说明")
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否内置模板"
    )

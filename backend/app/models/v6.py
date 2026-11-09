"""V6.0–V6.4 增量数据模型（对齐 PRD V6.4 第 5 章 F9/F10/F11、第 5A 章与 F3.14）。

本模块集中承载 V6 新增的 27 张表，避免把新增实体散落到 V4 各域文件：
- 剧本台词与连续性事实（F2.12 / F2.13 / 5A.2）：``continuity_facts``、
  ``story_bible_characters``、``story_bible_scenes``、``story_bible_props``、
  ``continuity_snapshots``、``continuity_checks``、``logic_waivers``；
- 结构化反馈（5A.4）：``human_feedback``、``negative_examples``；
- 配音轨（F9）：``voice_params``、``voice_tracks``、``voice_clips``、
  ``voice_drift_records``；
- 台词绑定（F4.13）：``shot_line_bindings``；
- 合成编排（F10）：``composition_graphs``、``cg_nodes``、``cg_edges``、
  ``cg_layouts``、``render_plans``、``composition_switch_logs``；
- 参数中心（F11）：``param_defs``、``param_configs``、``config_versions``、
  ``config_audits``；
- 照片合成角色（F3.14）：``photo_uploads``、``identity_profiles``、
  ``character_compositions``。

命名与约束遵循《02 数据库设计文档》§4.1：索引 ``idx_<表>_<字段>``、
唯一 ``uq_<表>_<字段>``、检查 ``ck_<表>_<字段>``。
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


# =====================================================================
# 一、连续性事实与 Story Bible（F2.12 / F2.13 / 5A.2）
# =====================================================================


class ContinuityFact(IdMixin, TimestampMixin, Base):
    """场次级连续性事实：由剧本层产出的“世界状态”声明（表 continuity_facts）。

    对齐 F2.13.2 输出契约：每场一条记录，含在场角色、服装、时间、道具与
    台词锚点；缺失字段以 ``unknown`` 填充并列入待补全清单（AC-F2-17-1）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        script_id: 来源剧本版本 ID（FK scripts，SET NULL）。
        scene_id: 对应场次 ID（FK scenes，SET NULL，场次重建时保留事实）。
        ep_no: 集号（≥1）。
        scene_no: 场号（≥1）。
        facts: 事实主体 JSONB（characters/outfits/time/props/emotion_arc/line_ids）。
        missing_fields: 缺失字段路径清单（JSONB 数组）。
        completeness_pct: 字段完整度百分比（0~100）。
        version_no: 场次事实版本号（改写后 +1）。
        source: 产出方式 ``agent`` / ``inferred`` / ``edit``。
    """

    __tablename__ = "continuity_facts"
    __table_args__ = (
        CheckConstraint(text("ep_no >= 1"), name="ck_continuity_facts_ep_no"),
        CheckConstraint(text("scene_no >= 1"), name="ck_continuity_facts_scene_no"),
        CheckConstraint(text("version_no >= 1"), name="ck_continuity_facts_version_no"),
        CheckConstraint(
            text("completeness_pct BETWEEN 0 AND 100"),
            name="ck_continuity_facts_completeness",
        ),
        CheckConstraint(
            text("source IN ('agent', 'inferred', 'edit')"),
            name="ck_continuity_facts_source",
        ),
        UniqueConstraint(
            "project_id", "ep_no", "scene_no", "version_no",
            name="uq_continuity_facts_scene_version",
        ),
        Index("idx_continuity_facts_project_id_ep_no", "project_id", "ep_no"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    script_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scripts.id", ondelete="SET NULL"), nullable=True, comment="来源剧本版本 ID"
    )
    scene_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("scenes.id", ondelete="SET NULL"), nullable=True, comment="场次 ID"
    )
    ep_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="集号")
    scene_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="场号")
    facts: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="连续性事实主体"
    )
    missing_fields: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="缺失字段路径"
    )
    completeness_pct: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("0"), comment="完整度百分比"
    )
    version_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), comment="事实版本号"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="agent", comment="产出方式"
    )


class StoryBibleCharacter(IdMixin, TimestampMixin, Base):
    """Story Bible 角色档案：角色的权威外观与状态定义（表 story_bible_characters）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        char_key: 角色稳定键（项目内唯一，来自剧本角色名或资产键）。
        name: 角色显示名。
        char_asset_id: 关联角色资产 ID（FK assets，SET NULL）。
        base_look: 基础造型描述。
        body_desc: 体态/年龄等描述。
        voice_id: 绑定音色 ID（FK tts_voices，SET NULL）。
        outfits: 服装变体数组 ``[{outfit_id, desc, scenes[]}]``。
        current_state: 当前状态 ``alive/dead/offstage``。
        extra: 扩展字段（JSONB）。
    """

    __tablename__ = "story_bible_characters"
    __table_args__ = (
        CheckConstraint(
            text("current_state IN ('alive', 'dead', 'offstage', 'unknown')"),
            name="ck_story_bible_characters_state",
        ),
        UniqueConstraint("project_id", "char_key", name="uq_story_bible_characters_key"),
        Index("idx_story_bible_characters_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    char_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="角色稳定键")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="角色显示名")
    char_asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, comment="角色资产 ID"
    )
    base_look: Mapped[str | None] = mapped_column(Text, nullable=True, comment="基础造型描述")
    body_desc: Mapped[str | None] = mapped_column(Text, nullable=True, comment="体态/年龄描述")
    voice_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tts_voices.id", ondelete="SET NULL"), nullable=True, comment="音色 ID"
    )
    outfits: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="服装变体"
    )
    current_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="alive", comment="角色状态"
    )
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="扩展字段")


class StoryBibleScene(IdMixin, TimestampMixin, Base):
    """Story Bible 场景档案：场景空间与时间锚点（表 story_bible_scenes）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        ep_no: 集号。
        scene_no: 场号。
        location: 地点。
        layout_ref: 空间布局引用（资产/图片键）。
        key_points: 关键点位数组 ``[{name, pos_desc}]``。
        time_of_day: 时段标签。
        elapsed_minutes: 剧情内时间推进（分钟）。
        weather: 天气。
        lighting_base: 光线基线描述。
        is_flashback: 是否闪回场。
    """

    __tablename__ = "story_bible_scenes"
    __table_args__ = (
        CheckConstraint(text("ep_no >= 1"), name="ck_story_bible_scenes_ep_no"),
        CheckConstraint(text("scene_no >= 1"), name="ck_story_bible_scenes_scene_no"),
        CheckConstraint(
            text("elapsed_minutes IS NULL OR elapsed_minutes >= 0"),
            name="ck_story_bible_scenes_elapsed",
        ),
        UniqueConstraint(
            "project_id", "ep_no", "scene_no", name="uq_story_bible_scenes_position"
        ),
        Index("idx_story_bible_scenes_project_id_ep_no", "project_id", "ep_no"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    ep_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="集号")
    scene_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="场号")
    location: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="地点")
    layout_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="空间布局引用")
    key_points: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="关键点位"
    )
    time_of_day: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="时段")
    elapsed_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="剧情内分钟")
    weather: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="天气")
    lighting_base: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="光线基线")
    is_flashback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否闪回"
    )


class StoryBibleProp(IdMixin, TimestampMixin, Base):
    """Story Bible 道具档案：道具状态机与持有人轨迹（表 story_bible_props）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        prop_key: 道具稳定键（项目内唯一）。
        name: 道具名。
        prop_asset_id: 关联道具资产 ID（FK assets，SET NULL）。
        state_machine: 状态链数组 ``[{state, at_scene}]``。
        holder_by_scene: 持有人映射 ``{scene_key: char_key}``。
        ref_images: 参考图键数组。
        current_state: 当前状态。
    """

    __tablename__ = "story_bible_props"
    __table_args__ = (
        UniqueConstraint("project_id", "prop_key", name="uq_story_bible_props_key"),
        Index("idx_story_bible_props_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    prop_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="道具稳定键")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="道具名")
    prop_asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, comment="道具资产 ID"
    )
    state_machine: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="状态链"
    )
    holder_by_scene: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="持有人映射"
    )
    ref_images: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="参考图"
    )
    current_state: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="当前状态")


class ContinuitySnapshot(IdMixin, TimestampMixin, Base):
    """镜头连续性快照：镜头生成前应满足的世界状态（表 continuity_snapshots）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        shot_id: 镜头 ID（FK shots，级联删除）。
        expected_state: 期望状态 JSONB（在场/服装/时段/道具/台词）。
        source_version: 来源版本串（剧本版本 + 资产版本）。
        snapshot_hash: 快照内容哈希（回放与去重）。
    """

    __tablename__ = "continuity_snapshots"
    __table_args__ = (
        UniqueConstraint("shot_id", "snapshot_hash", name="uq_continuity_snapshots_shot_hash"),
        Index("idx_continuity_snapshots_project_id_shot_id", "project_id", "shot_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    shot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False, comment="镜头 ID"
    )
    expected_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="期望世界状态"
    )
    source_version: Mapped[str] = mapped_column(String(128), nullable=False, comment="来源版本串")
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="快照哈希")


class ContinuityCheck(IdMixin, TimestampMixin, Base):
    """连续性校验结果：L1/L2/L3 逐维得分与裁决（表 continuity_checks）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        shot_id: 被检镜头 ID（FK shots，级联删除）。
        adjacent_shot_id: 相邻镜头 ID（L2 用，FK shots，SET NULL）。
        level: 校验层级 L1/L2/L3。
        dimension: 维度 face/outfit/scene/prop/timing_light/plot_logic。
        score: 得分（0~1，规则类可为 1/0）。
        threshold: 判定阈值。
        verdict: 裁决 pass/warn/fail/degraded/unavailable。
        evidence_ref: 证据引用 JSONB。
        reviewer: 复核方式 auto/human_confirm/human_override。
        config_version: 使用的参数配置版本。
    """

    __tablename__ = "continuity_checks"
    __table_args__ = (
        CheckConstraint(text("level IN ('L1', 'L2', 'L3')"), name="ck_continuity_checks_level"),
        CheckConstraint(
            text(
                "dimension IN ('face', 'outfit', 'scene', 'prop', 'timing_light', 'plot_logic')"
            ),
            name="ck_continuity_checks_dimension",
        ),
        CheckConstraint(
            text("verdict IN ('pass', 'warn', 'fail', 'degraded', 'unavailable')"),
            name="ck_continuity_checks_verdict",
        ),
        CheckConstraint(
            text("reviewer IN ('auto', 'human_confirm', 'human_override')"),
            name="ck_continuity_checks_reviewer",
        ),
        CheckConstraint(
            text("score IS NULL OR score BETWEEN 0 AND 1"),
            name="ck_continuity_checks_score",
        ),
        Index(
            "idx_continuity_checks_project_shot_dimension",
            "project_id", "shot_id", "dimension",
        ),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    shot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False, comment="镜头 ID"
    )
    adjacent_shot_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("shots.id", ondelete="SET NULL"), nullable=True, comment="相邻镜头 ID"
    )
    level: Mapped[str] = mapped_column(String(8), nullable=False, comment="层级")
    dimension: Mapped[str] = mapped_column(String(32), nullable=False, comment="维度")
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True, comment="得分")
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True, comment="阈值")
    verdict: Mapped[str] = mapped_column(String(16), nullable=False, comment="裁决")
    evidence_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="证据引用")
    reviewer: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="auto", comment="复核方式"
    )
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="配置版本")


class LogicWaiver(IdMixin, TimestampMixin, Base):
    """逻辑规则豁免：用户“忽略并留痕”的项目级例外（表 logic_waivers）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        rule_code: 规则编号 LR-1~LR-6。
        target_ref: 定位引用（如 ep:1/scene:3/line:12）。
        reason: 豁免理由（≥10 字）。
        actor_user_id: 操作者用户 ID（FK users，SET NULL）。
        extra: 扩展信息。
    """

    __tablename__ = "logic_waivers"
    __table_args__ = (
        CheckConstraint(
            text("rule_code IN ('LR-1', 'LR-2', 'LR-3', 'LR-4', 'LR-5', 'LR-6')"),
            name="ck_logic_waivers_rule_code",
        ),
        UniqueConstraint(
            "project_id", "rule_code", "target_ref", name="uq_logic_waivers_rule_target"
        ),
        Index("idx_logic_waivers_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    rule_code: Mapped[str] = mapped_column(String(8), nullable=False, comment="规则编号")
    target_ref: Mapped[str] = mapped_column(String(128), nullable=False, comment="定位引用")
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="豁免理由")
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="操作者"
    )
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="扩展信息")


# =====================================================================
# 二、结构化反馈（5A.4）
# =====================================================================


class HumanFeedback(IdMixin, TimestampMixin, Base):
    """结构化反馈：维度化 reject 表单与确定性路由结果（表 human_feedback）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        target_type: 反馈对象 shot/line/episode/final_cut。
        target_id: 对象 ID。
        dimensions: 维度数组（≥1）。
        scope: 作用域 JSONB（ep_no/shot_ids/line_ids）。
        expectation: 期望枚举。
        expectation_text: 期望补充文本。
        keep_items: 保留项数组。
        text: 补充说明（≤200 字）。
        source_gate: 来源关口 gate1/gateV/gate2/gate3/gate4。
        routed_strategy: 确定性路由命中的修正策略。
        strategy_round: 同维度第几次 reject。
        actor_user_id: 提交人（FK users，SET NULL）。
        adopted: 是否已采纳执行。
    """

    __tablename__ = "human_feedback"
    __table_args__ = (
        CheckConstraint(
            text("target_type IN ('shot', 'line', 'episode', 'final_cut')"),
            name="ck_human_feedback_target_type",
        ),
        CheckConstraint(
            text("source_gate IN ('gate1', 'gateV', 'gate2', 'gate3', 'gate4')"),
            name="ck_human_feedback_source_gate",
        ),
        Index("idx_human_feedback_project_target", "project_id", "target_type", "target_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="对象类型")
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="对象 ID")
    dimensions: Mapped[list] = mapped_column(JSONB, nullable=False, comment="维度数组")
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="作用域")
    expectation: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="期望枚举")
    expectation_text: Mapped[str | None] = mapped_column(Text, nullable=True, comment="期望文本")
    keep_items: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="保留项"
    )
    # 属性名使用 note_text，物理列名保持 PRD 约定的 ``text``，避免遮蔽 sqlalchemy.text
    note_text: Mapped[str | None] = mapped_column("text", Text, nullable=True, comment="补充说明")
    source_gate: Mapped[str] = mapped_column(String(16), nullable=False, comment="来源关口")
    routed_strategy: Mapped[str] = mapped_column(String(64), nullable=False, comment="路由策略")
    strategy_round: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"), comment="策略轮次"
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="提交人"
    )
    adopted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否已采纳"
    )


class NegativeExample(IdMixin, TimestampMixin, Base):
    """反例库：问题→修正→结果的“错-改-对”三元组（表 negative_examples）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        dimension: 维度。
        character_asset_id: 相关角色资产 ID（FK assets，SET NULL）。
        scene_ref: 场次引用。
        issue_desc: 问题描述。
        evidence_ref: 证据引用 JSONB。
        fix_action: 修正动作。
        passed: 修正后是否通过。
        source: 来源 reject/misjudge/manual_fix。
    """

    __tablename__ = "negative_examples"
    __table_args__ = (
        CheckConstraint(
            text("source IN ('reject', 'misjudge', 'manual_fix')"),
            name="ck_negative_examples_source",
        ),
        Index("idx_negative_examples_project_dimension", "project_id", "dimension"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    dimension: Mapped[str] = mapped_column(String(32), nullable=False, comment="维度")
    character_asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, comment="角色资产 ID"
    )
    scene_ref: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="场次引用")
    issue_desc: Mapped[str] = mapped_column(Text, nullable=False, comment="问题描述")
    evidence_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="证据引用")
    fix_action: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="修正动作")
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="修正后是否通过"
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False, comment="来源")


# =====================================================================
# 三、配音轨（F9）与台词绑定（F4.13）
# =====================================================================


class VoiceParam(IdMixin, TimestampMixin, Base):
    """角色配音参数快照：样音确认后锁定的音色与参数（表 voice_params）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        character_asset_id: 角色资产 ID（FK assets，级联删除）。
        voice_id: 音色 ID（FK tts_voices，SET NULL）。
        rate: 语速倍率（0.5~2.0）。
        pitch: 音调偏移（-6~+6）。
        emotion_default: 默认情绪。
        params_hash: 参数哈希（缓存键组成部分）。
        status: active/superseded。
        sample_audio_key: 样音对象键。
        sample_duration_ms: 样音时长。
        confirmed_by: 确认人（FK users，SET NULL）。
        confirmed_at: 确认时间。
    """

    __tablename__ = "voice_params"
    __table_args__ = (
        CheckConstraint(text("rate BETWEEN 0.5 AND 2.0"), name="ck_voice_params_rate"),
        CheckConstraint(text("pitch BETWEEN -6 AND 6"), name="ck_voice_params_pitch"),
        CheckConstraint(
            text("status IN ('active', 'superseded')"), name="ck_voice_params_status"
        ),
        UniqueConstraint(
            "project_id", "character_asset_id", "params_hash",
            name="uq_voice_params_character_hash",
        ),
        Index("idx_voice_params_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    character_asset_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, comment="角色资产 ID"
    )
    voice_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tts_voices.id", ondelete="SET NULL"), nullable=True, comment="音色 ID"
    )
    rate: Mapped[Decimal] = mapped_column(
        Numeric(4, 2), nullable=False, server_default=text("1.00"), comment="语速倍率"
    )
    pitch: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="音调偏移"
    )
    emotion_default: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="默认情绪")
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="参数哈希")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active", comment="状态"
    )
    sample_audio_key: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="样音对象键")
    sample_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="样音时长")
    confirmed_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="确认人"
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="确认时间"
    )


class VoiceTrack(IdMixin, TimestampMixin, Base):
    """集级配音轨：一集一条，承载状态、时长与关口 V（表 voice_tracks）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        ep_no: 集号（项目内唯一）。
        status: not_started/previewing_done/voice_ready/voice_locked/voice_drift。
        total_ms: 集总时长（ms）。
        speech_ms: 台词音频总时长（ms）。
        silence_ms: 静默总时长（ms）。
        gap_ms: 句间气口（ms）。
        line_count: 台词总数。
        voiced_count: 已完成句数。
        failed_count: 失败句数。
        gate_status: pending/approved/rejected。
        report: 时长报告 JSONB。
        align_degraded: 是否发生对齐降级。
        approved_by: 关口 V 审批人（FK users，SET NULL）。
        approved_at: 审批时间。
        locked_at: 锁定时间。
    """

    __tablename__ = "voice_tracks"
    __table_args__ = (
        CheckConstraint(text("ep_no >= 1"), name="ck_voice_tracks_ep_no"),
        CheckConstraint(
            text(
                "status IN ('not_started', 'previewing_done', 'voice_ready', "
                "'voice_locked', 'voice_drift')"
            ),
            name="ck_voice_tracks_status",
        ),
        CheckConstraint(
            text("gate_status IN ('pending', 'approved', 'rejected')"),
            name="ck_voice_tracks_gate_status",
        ),
        UniqueConstraint("project_id", "ep_no", name="uq_voice_tracks_project_ep"),
        Index("idx_voice_tracks_project_id_status", "project_id", "status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    ep_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="集号")
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="not_started", comment="集级状态"
    )
    total_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="集总时长"
    )
    speech_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="台词音频总时长"
    )
    silence_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="静默总时长"
    )
    gap_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("120"), comment="句间气口"
    )
    line_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="台词总数"
    )
    voiced_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="已完成句数"
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="失败句数"
    )
    gate_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", comment="关口 V 状态"
    )
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="时长报告")
    align_degraded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="对齐降级标记"
    )
    approved_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="审批人"
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="审批时间"
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="锁定时间"
    )


class VoiceClip(IdMixin, TimestampMixin, Base):
    """句级配音产物：逐句 TTS + 词级对齐结果（表 voice_clips）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        track_id: 配音轨 ID（FK voice_tracks，级联删除）。
        line_id: 台词 ID（FK script_lines，级联删除）。
        line_key: 台词稳定键（冗余，便于跨版本复用）。
        text_hash: 文本哈希（缓存键）。
        params_hash: 参数哈希（缓存键）。
        voice_id: 实际使用的音色 ID。
        character_asset_id: 说话角色资产 ID。
        source: ai/human。
        status: pending/voicing/voiced/failed/aligning_failed/dirty_voice。
        audio_key: 音频对象键。
        audio_url: 可播放地址。
        duration_ms: 实测时长。
        words: 词级时间戳数组 ``[{w,start_ms,end_ms}]``。
        alignment_source: whisperx/tts_timestamp/human/none。
        align_degraded: 是否降级。
        retry_count: 重试次数。
        error_message: 失败原因。
        billed: 是否计费。
    """

    __tablename__ = "voice_clips"
    __table_args__ = (
        CheckConstraint(
            text(
                "status IN ('pending', 'voicing', 'voiced', 'failed', "
                "'aligning_failed', 'dirty_voice')"
            ),
            name="ck_voice_clips_status",
        ),
        CheckConstraint(text("source IN ('ai', 'human')"), name="ck_voice_clips_source"),
        CheckConstraint(
            text("alignment_source IN ('whisperx', 'tts_timestamp', 'human', 'none')"),
            name="ck_voice_clips_alignment_source",
        ),
        UniqueConstraint("track_id", "line_id", name="uq_voice_clips_track_line"),
        Index("idx_voice_clips_cache", "text_hash", "params_hash"),
        Index("idx_voice_clips_project_id_status", "project_id", "status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    track_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("voice_tracks.id", ondelete="CASCADE"), nullable=False, comment="配音轨 ID"
    )
    line_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("script_lines.id", ondelete="CASCADE"), nullable=False, comment="台词 ID"
    )
    line_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="台词稳定键")
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="文本哈希")
    params_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="参数哈希")
    voice_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("tts_voices.id", ondelete="SET NULL"), nullable=True, comment="音色 ID"
    )
    character_asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, comment="角色资产 ID"
    )
    source: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="ai", comment="音频来源"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", comment="句级状态"
    )
    audio_key: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="音频对象键")
    audio_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="音频地址")
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="实测时长"
    )
    words: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="词级时间戳"
    )
    alignment_source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="none", comment="对齐来源"
    )
    align_degraded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否降级"
    )
    retry_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="重试次数"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    billed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否计费"
    )
    emotion: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="实际使用的情绪标签（P1 修复：情绪持久化）"
    )


class VoiceDriftRecord(IdMixin, TimestampMixin, Base):
    """配音时长漂移记录：重配后时长变化的分级判定（表 voice_drift_records）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        ep_no: 集号。
        line_id: 台词 ID（FK script_lines，SET NULL）。
        old_ms: 旧时长。
        new_ms: 新时长。
        delta_ms: 差值（毫秒）。
        delta_pct: 变化百分比。
        level: absorb/recalc/regenerate（4.6.5 分级）。
        affected_shot_ids: 受影响镜头 ID 数组。
        status: pending/confirmed/dismissed。
        source: rerecord/rate_change/text_change。
        config_version: 判定所用配置版本。
    """

    __tablename__ = "voice_drift_records"
    __table_args__ = (
        CheckConstraint(
            text("level IN ('absorb', 'recalc', 'regenerate')"),
            name="ck_voice_drift_records_level",
        ),
        CheckConstraint(
            text("status IN ('pending', 'confirmed', 'dismissed')"),
            name="ck_voice_drift_records_status",
        ),
        Index("idx_voice_drift_records_project_ep", "project_id", "ep_no"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    ep_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="集号")
    line_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("script_lines.id", ondelete="SET NULL"), nullable=True, comment="台词 ID"
    )
    old_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="旧时长")
    new_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="新时长")
    delta_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="差值")
    delta_pct: Mapped[Decimal] = mapped_column(
        Numeric(6, 2), nullable=False, server_default=text("0"), comment="变化百分比"
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, comment="分级")
    affected_shot_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="受影响镜头"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", comment="处理状态"
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="rerecord", comment="变更来源"
    )
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="配置版本")


class ShotLineBinding(IdMixin, TimestampMixin, Base):
    """镜头台词绑定：镜头 ↔ 台词的多对多引用（表 shot_line_bindings）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        shot_id: 镜头 ID（FK shots，级联删除）。
        line_id: 台词 ID（FK script_lines，级联删除）。
        offset_ms: 台词在镜头内的起始偏移。
        order_index: 镜头内顺序（≥0）。
        created_by: 绑定人（FK users，SET NULL）。
    """

    __tablename__ = "shot_line_bindings"
    __table_args__ = (
        CheckConstraint(text("offset_ms >= 0"), name="ck_shot_line_bindings_offset"),
        CheckConstraint(text("order_index >= 0"), name="ck_shot_line_bindings_order"),
        UniqueConstraint("shot_id", "line_id", name="uq_shot_line_bindings_shot_line"),
        Index("idx_shot_line_bindings_line_id", "line_id"),
        Index("idx_shot_line_bindings_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    shot_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("shots.id", ondelete="CASCADE"), nullable=False, comment="镜头 ID"
    )
    line_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("script_lines.id", ondelete="CASCADE"), nullable=False, comment="台词 ID"
    )
    offset_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="镜头内偏移"
    )
    order_index: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="镜头内顺序"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="绑定人"
    )


# =====================================================================
# 四、合成编排（F10）
# =====================================================================


class CompositionGraph(IdMixin, TimestampMixin, Base):
    """编排图：一集一张图，按版本不可变保存（表 composition_graphs）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        ep_no: 集号。
        version: 版本号（项目 + 集内唯一）。
        status: draft/active/archived。
        view_pref: 视图偏好 track/node（不影响语义）。
        graph_hash: 规范化图哈希。
        source: auto/manual/template。
        created_by: 创建人（FK users，SET NULL）。
        locked_by_render: 渲染中锁定编排。
        activated_at: 设为 active 的时间。
    """

    __tablename__ = "composition_graphs"
    __table_args__ = (
        CheckConstraint(text("version >= 1"), name="ck_composition_graphs_version"),
        CheckConstraint(
            text("status IN ('draft', 'active', 'archived')"),
            name="ck_composition_graphs_status",
        ),
        CheckConstraint(text("view_pref IN ('track', 'node')"), name="ck_composition_graphs_view_pref"),
        CheckConstraint(
            text("source IN ('auto', 'manual', 'template')"),
            name="ck_composition_graphs_source",
        ),
        UniqueConstraint("project_id", "ep_no", "version", name="uq_composition_graphs_version"),
        Index("idx_composition_graphs_project_ep_status", "project_id", "ep_no", "status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    ep_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="集号")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"), comment="版本号")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active", comment="图状态"
    )
    view_pref: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="track", comment="视图偏好"
    )
    graph_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="规范化图哈希")
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="auto", comment="来源"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="创建人"
    )
    locked_by_render: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="渲染中锁定"
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="激活时间"
    )


class CgNode(IdMixin, TimestampMixin, Base):
    """编排节点：片段/分组/字卡/音频叠加等（表 cg_nodes）。

    Attributes:
        graph_id: 编排图 ID（FK composition_graphs，级联删除）。
        node_key: 图内稳定键（唯一）。
        node_type: clip/group/title/audio_overlay/compose/branch/start/end。
        ref_id: 引用对象 ID（镜头 ID / 媒体 ID 等）。
        ref_kind: 引用类型 shot/media/line/text。
        params: 节点参数 JSONB（in_ms/out_ms/文本等）。
        label: 展示标签。
        parent_id: 分组父节点 ID（FK cg_nodes，SET NULL）。
        ui_pos: 节点坐标（仅布局）。
        order_hint: 轨道排序提示。
        is_stale: 引用上游已变更。
        status: normal/stale/disabled。
    """

    __tablename__ = "cg_nodes"
    __table_args__ = (
        CheckConstraint(
            text(
                "node_type IN ('clip', 'group', 'title', 'audio_overlay', "
                "'compose', 'branch', 'start', 'end')"
            ),
            name="ck_cg_nodes_type",
        ),
        CheckConstraint(
            text("status IN ('normal', 'stale', 'disabled')"), name="ck_cg_nodes_status"
        ),
        UniqueConstraint("graph_id", "node_key", name="uq_cg_nodes_graph_key"),
        Index("idx_cg_nodes_graph_id_type", "graph_id", "node_type"),
    )

    graph_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("composition_graphs.id", ondelete="CASCADE"), nullable=False, comment="图 ID"
    )
    node_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="节点键")
    node_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="节点类型")
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="引用对象 ID")
    ref_kind: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="引用类型")
    params: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="节点参数"
    )
    label: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="展示标签")
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("cg_nodes.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        comment="分组父节点",
    )
    ui_pos: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="节点坐标")
    order_hint: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="轨道排序"
    )
    is_stale: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="引用失效"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="normal", comment="节点状态"
    )


class CgEdge(IdMixin, TimestampMixin, Base):
    """编排边：定义节点顺序与转场（表 cg_edges）。

    Attributes:
        graph_id: 编排图 ID（FK composition_graphs，级联删除）。
        from_node_id: 起点节点 ID（FK cg_nodes，级联删除）。
        to_node_id: 终点节点 ID（FK cg_nodes，级联删除）。
        transition: cut/dissolve/fade/wipe/slide。
        duration_ms: 转场时长。
        order_hint: 轨道排序。
        is_main: 是否主路径。
    """

    __tablename__ = "cg_edges"
    __table_args__ = (
        CheckConstraint(
            text("transition IN ('cut', 'dissolve', 'fade', 'wipe', 'slide')"),
            name="ck_cg_edges_transition",
        ),
        CheckConstraint(text("duration_ms >= 0"), name="ck_cg_edges_duration"),
        UniqueConstraint(
            "graph_id", "from_node_id", "to_node_id", name="uq_cg_edges_graph_from_to"
        ),
        Index("idx_cg_edges_graph_id_order", "graph_id", "order_hint"),
    )

    graph_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("composition_graphs.id", ondelete="CASCADE"), nullable=False, comment="图 ID"
    )
    from_node_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cg_nodes.id", ondelete="CASCADE"), nullable=False, comment="起点节点"
    )
    to_node_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("cg_nodes.id", ondelete="CASCADE"), nullable=False, comment="终点节点"
    )
    transition: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="cut", comment="转场类型"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="转场时长"
    )
    order_hint: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="排序提示"
    )
    is_main: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="主路径"
    )


class CgLayout(IdMixin, TimestampMixin, Base):
    """编排布局：仅存视图布局，不参与成片语义（表 cg_layouts）。

    Attributes:
        graph_id: 编排图 ID（FK composition_graphs，级联删除）。
        view_type: track/node。
        payload: 布局 JSONB（轨道高度、缩放、坐标）。
    """

    __tablename__ = "cg_layouts"
    __table_args__ = (
        CheckConstraint(text("view_type IN ('track', 'node')"), name="ck_cg_layouts_view_type"),
        UniqueConstraint("graph_id", "view_type", name="uq_cg_layouts_graph_view"),
    )

    graph_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("composition_graphs.id", ondelete="CASCADE"), nullable=False, comment="图 ID"
    )
    view_type: Mapped[str] = mapped_column(String(8), nullable=False, comment="视图类型")
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="布局数据"
    )


class RenderPlan(IdMixin, TimestampMixin, Base):
    """渲染计划：编译器 C1–C4 的产物与幂等键（表 render_plans）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        graph_id: 来源图 ID（FK composition_graphs，级联删除）。
        graph_hash: 图哈希。
        plan_hash: 计划哈希（唯一索引，幂等键）。
        plan_json: 计划 JSONB。
        output_ref: 产物引用。
        status: compiled/reused/rendering/rendered/failed。
        reused_count: 复用次数。
        cost_credits: 估算成本。
        duration_ms: 估算或实测时长。
        is_preview: 是否试渲。
    """

    __tablename__ = "render_plans"
    __table_args__ = (
        CheckConstraint(
            text("status IN ('compiled', 'reused', 'rendering', 'rendered', 'failed')"),
            name="ck_render_plans_status",
        ),
        UniqueConstraint("plan_hash", name="uq_render_plans_plan_hash"),
        Index("idx_render_plans_project_id_status", "project_id", "status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    graph_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("composition_graphs.id", ondelete="CASCADE"), nullable=False, comment="图 ID"
    )
    graph_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="图哈希")
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="计划哈希")
    plan_json: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="渲染计划")
    output_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="产物引用")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="compiled", comment="状态"
    )
    reused_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="复用次数"
    )
    cost_credits: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="估算成本"
    )
    duration_ms: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="时长"
    )
    is_preview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否试渲"
    )


class CompositionSwitchLog(IdMixin, TimestampMixin, Base):
    """视图切换审计：轨道 ↔ 节点切换是否无损（表 composition_switch_logs）。

    Attributes:
        graph_id: 图 ID（FK composition_graphs，级联删除）。
        from_view: 来源视图。
        to_view: 目标视图。
        lossless: 是否无损。
        discarded_nodes: 被折叠节点 JSONB。
        actor_user_id: 操作者（FK users，SET NULL）。
    """

    __tablename__ = "composition_switch_logs"
    __table_args__ = (
        CheckConstraint(
            text("from_view IN ('track', 'node') AND to_view IN ('track', 'node')"),
            name="ck_composition_switch_logs_views",
        ),
        Index("idx_composition_switch_logs_graph_id", "graph_id"),
    )

    graph_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("composition_graphs.id", ondelete="CASCADE"), nullable=False, comment="图 ID"
    )
    from_view: Mapped[str] = mapped_column(String(8), nullable=False, comment="来源视图")
    to_view: Mapped[str] = mapped_column(String(8), nullable=False, comment="目标视图")
    lossless: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否无损"
    )
    discarded_nodes: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="折叠节点"
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="操作者"
    )


# =====================================================================
# 五、参数中心（F11）
# =====================================================================


class ParamDef(IdMixin, TimestampMixin, Base):
    """参数定义：40 项可调参数的元数据与文案（表 param_defs）。

    Attributes:
        param_key: 参数键（唯一）。
        param_group: A–G 组键。
        label: 参数名。
        value_type: int/float/bool/enum。
        default_value: 平台默认值 JSONB。
        hard_min/hard_max: 硬边界 JSONB（数值或枚举白名单）。
        suggested_min/suggested_max: 建议范围 JSONB。
        enum_options: 枚举选项 JSONB。
        unit: 单位。
        effect_scope: immediate/next_check/next_generate/next_voice。
        is_dangerous: 是否危险参数。
        is_locked: 是否合规锁定（只读）。
        is_advanced: 是否进阶参数（默认折叠）。
        is_common: 是否首屏常用 8 项。
        description: 作用说明（≤40 字）。
        high_effect: 调高影响（≤40 字）。
        low_effect: 调低影响（≤40 字）。
        display_order: 展示顺序。
    """

    __tablename__ = "param_defs"
    __table_args__ = (
        CheckConstraint(
            text("value_type IN ('int', 'float', 'bool', 'enum')"),
            name="ck_param_defs_value_type",
        ),
        CheckConstraint(
            text(
                "effect_scope IN ('immediate', 'next_check', 'next_generate', "
                "'next_voice', 'next_storyboard', 'next_compose')"
            ),
            name="ck_param_defs_effect_scope",
        ),
        UniqueConstraint("param_key", name="uq_param_defs_param_key"),
        Index("idx_param_defs_group_order", "param_group", "display_order"),
    )

    param_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="参数键")
    param_group: Mapped[str] = mapped_column(String(8), nullable=False, comment="参数分组")
    label: Mapped[str] = mapped_column(String(128), nullable=False, comment="参数名")
    value_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="值类型")
    default_value: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="默认值")
    hard_min: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="硬下界")
    hard_max: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="硬上界")
    suggested_min: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="建议下界")
    suggested_max: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="建议上界")
    enum_options: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="枚举选项"
    )
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="单位")
    effect_scope: Mapped[str] = mapped_column(String(24), nullable=False, comment="生效方式")
    is_dangerous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="危险参数"
    )
    is_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="合规锁定"
    )
    is_advanced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="进阶参数"
    )
    is_common: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="常用参数"
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False, comment="作用说明")
    high_effect: Mapped[str] = mapped_column(String(255), nullable=False, comment="调高影响")
    low_effect: Mapped[str] = mapped_column(String(255), nullable=False, comment="调低影响")
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="展示顺序"
    )


class ParamConfig(IdMixin, TimestampMixin, Base):
    """参数当前值：项目级作用域的覆盖值（表 param_configs）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        scope: project/episode/user_pref。
        scope_id: 作用域标识（集号或用户 ID，project 为 "0"）。
        param_key: 参数键。
        value: 覆盖值 JSONB。
        updated_by: 更新人（FK users，SET NULL）。
        config_version: 所属配置版本。
    """

    __tablename__ = "param_configs"
    __table_args__ = (
        CheckConstraint(
            text("scope IN ('project', 'episode', 'user_pref')"), name="ck_param_configs_scope"
        ),
        UniqueConstraint(
            "project_id", "scope", "scope_id", "param_key",
            name="uq_param_configs_project_scope_key",
        ),
        Index("idx_param_configs_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False, comment="作用域")
    scope_id: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="0", comment="作用域标识"
    )
    param_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="参数键")
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="覆盖值")
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="更新人"
    )
    config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="配置版本"
    )


class ConfigVersion(IdMixin, TimestampMixin, Base):
    """配置版本：不可变快照，支持回滚（表 config_versions）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        version_no: 版本号（项目内唯一）。
        snapshot: 全量参数快照 JSONB。
        changes: 变更摘要 JSONB。
        source: save/rollback/restore_default。
        rollback_from: 回滚来源版本号。
        actor_user_id: 操作者（FK users，SET NULL）。
        config_hash: 快照哈希（相同值不产生新版本）。
    """

    __tablename__ = "config_versions"
    __table_args__ = (
        CheckConstraint(text("version_no >= 1"), name="ck_config_versions_version_no"),
        CheckConstraint(
            text("source IN ('save', 'rollback', 'restore_default')"),
            name="ck_config_versions_source",
        ),
        UniqueConstraint("project_id", "version_no", name="uq_config_versions_project_version"),
        Index("idx_config_versions_project_id", "project_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="参数快照")
    changes: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="变更摘要"
    )
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="save", comment="来源"
    )
    rollback_from: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="回滚来源")
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="操作者"
    )
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="快照哈希")


class ConfigAudit(IdMixin, TimestampMixin, Base):
    """参数审计：谁在何时改了什么、为什么（表 config_audits）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        config_version: 配置版本号。
        param_key: 参数键。
        old_value: 旧值 JSONB。
        new_value: 新值 JSONB。
        is_dangerous: 是否危险参数。
        reason: 危险操作理由。
        actor_user_id: 操作者（FK users，SET NULL）。
        action: update/rollback/restore_default。
    """

    __tablename__ = "config_audits"
    __table_args__ = (
        CheckConstraint(
            text("action IN ('update', 'rollback', 'restore_default')"),
            name="ck_config_audits_action",
        ),
        Index("idx_config_audits_project_id_version", "project_id", "config_version"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="配置版本")
    param_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="参数键")
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="旧值")
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="新值")
    is_dangerous: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="危险参数"
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="操作理由")
    actor_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="操作者"
    )
    action: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="update", comment="动作"
    )


# =====================================================================
# 六、照片合成角色（F3.14）
# =====================================================================


class PhotoUpload(IdMixin, TimestampMixin, Base):
    """照片上传与四道闸结果（表 photo_uploads）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        batch_key: 批次键（同一次六步流程的照片集）。
        uploader_user_id: 上传人（FK users，SET NULL）。
        file_name: 原文件名。
        object_key: 存储对象键。
        mime: MIME 类型。
        size_bytes: 文件大小（≤20MB）。
        width/height: 分辨率。
        sha256: 内容指纹（去重闸）。
        status: uploading/uploaded/analyzing/usable/degraded/rejected/consumed/removed/invalid。
        quality_score: 质量分（0~1）。
        analysis: 模型分析明细 JSONB。
        reject_code: 拒绝码（F314_*）。
        reject_message: 用户可读提示。
        gate: 拒绝所在闸 static/content/compliance/dedup。
        face_count: 人脸数量。
        pose_yaw/pose_pitch: 偏转角度。
        blur_score: 清晰度分。
        lighting_score: 光照分。
        is_public_figure: 公众人物命中。
        minor_confidence: 年龄估计未成年置信度。
        is_sensitive: 敏感内容命中。
        is_duplicate: 重复照片。
        is_main: 是否主图。
        batch_blocked: 批次级联拦截。
        declaration: self/authorized_other。
        authorization_confirmed: 授权声明勾选。
        exif_stripped: 是否已剥离 EXIF。
    """

    __tablename__ = "photo_uploads"
    __table_args__ = (
        CheckConstraint(
            text(
                "status IN ('uploading', 'uploaded', 'analyzing', 'usable', 'degraded', "
                "'rejected', 'consumed', 'removed', 'invalid')"
            ),
            name="ck_photo_uploads_status",
        ),
        CheckConstraint(
            text("gate IS NULL OR gate IN ('static', 'content', 'compliance', 'dedup')"),
            name="ck_photo_uploads_gate",
        ),
        CheckConstraint(
            text("declaration IN ('self', 'authorized_other')"),
            name="ck_photo_uploads_declaration",
        ),
        CheckConstraint(text("size_bytes > 0"), name="ck_photo_uploads_size"),
        CheckConstraint(
            text("quality_score IS NULL OR quality_score BETWEEN 0 AND 1"),
            name="ck_photo_uploads_quality",
        ),
        Index("idx_photo_uploads_project_batch", "project_id", "batch_key"),
        Index("idx_photo_uploads_sha256", "sha256"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    batch_key: Mapped[str] = mapped_column(String(32), nullable=False, comment="批次键")
    uploader_user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="上传人"
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False, comment="原文件名")
    object_key: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="对象键")
    mime: Mapped[str] = mapped_column(String(64), nullable=False, comment="MIME 类型")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="文件大小")
    width: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="宽")
    height: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="高")
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="内容指纹")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="uploaded", comment="状态"
    )
    quality_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True, comment="质量分")
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="分析明细")
    reject_code: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="拒绝码")
    reject_message: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="拒绝提示")
    gate: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="拒绝闸")
    face_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1"), comment="人脸数量"
    )
    pose_yaw: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True, comment="左右偏转")
    pose_pitch: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True, comment="俯仰角")
    blur_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True, comment="清晰度")
    lighting_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True, comment="光照")
    is_public_figure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="公众人物"
    )
    minor_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True, comment="未成年置信度"
    )
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="敏感内容"
    )
    is_duplicate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="重复照片"
    )
    is_main: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否主图"
    )
    batch_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="批次级联拦截"
    )
    declaration: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="self", comment="用途声明"
    )
    authorization_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="授权确认"
    )
    exif_stripped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否剥离 EXIF"
    )


class IdentityProfile(IdMixin, TimestampMixin, Base):
    """身份档：照片集与主图的身份聚类结果（表 identity_profiles）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        batch_key: 批次键。
        name: 身份档名称。
        main_photo_id: 主图 ID（FK photo_uploads，SET NULL）。
        photo_ids: 照片 ID 数组。
        fingerprint: 身份指纹。
        cluster_info: 聚类明细 JSONB。
        status: draft/ready/consumed。
        created_by: 创建人（FK users，SET NULL）。
    """

    __tablename__ = "identity_profiles"
    __table_args__ = (
        CheckConstraint(
            text("status IN ('draft', 'ready', 'consumed')"),
            name="ck_identity_profiles_status",
        ),
        Index("idx_identity_profiles_project_batch", "project_id", "batch_key"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    batch_key: Mapped[str] = mapped_column(String(32), nullable=False, comment="批次键")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="身份档名称")
    main_photo_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("photo_uploads.id", ondelete="SET NULL"), nullable=True, comment="主图 ID"
    )
    photo_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="照片 ID 数组"
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, comment="身份指纹")
    cluster_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="聚类明细")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="draft", comment="状态"
    )
    created_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, comment="创建人"
    )


class CharacterComposition(IdMixin, TimestampMixin, Base):
    """照片合成角色任务：M1 身份保持的合成与自检（表 character_compositions）。

    Attributes:
        project_id: 项目 ID（FK projects，级联删除）。
        profile_id: 身份档 ID（FK identity_profiles，级联删除）。
        batch_key: 批次键。
        style_preset_id: 画风预设 ID（FK style_presets，SET NULL）。
        stylization: 风格化强度（0~100）。
        mode: 合成模式（MVP 恒 M1）。
        status: pending/composing/preview_ready/confirmed/failed/rejected。
        asset_id: 合成产出的角色资产 ID（FK assets，SET NULL）。
        preview_media_key: 预览媒体键。
        face_score: 与原图的人脸相似度。
        over_similarity: 过度相似告警。
        self_check: 自检明细 JSONB。
        freeze_credits: 冻结积分。
        cost_credits: 实际消耗。
        error_code: 错误码。
        error_message: 错误信息。
        rejected_codes: 批次拒绝码数组。
        config_version: 配置版本。
        composed_at: 合成完成时间。
    """

    __tablename__ = "character_compositions"
    __table_args__ = (
        CheckConstraint(
            text(
                "status IN ('pending', 'composing', 'preview_ready', 'confirmed', "
                "'failed', 'rejected')"
            ),
            name="ck_character_compositions_status",
        ),
        CheckConstraint(text("mode = 'M1'"), name="ck_character_compositions_mode"),
        CheckConstraint(
            text("stylization BETWEEN 0 AND 100"), name="ck_character_compositions_stylization"
        ),
        CheckConstraint(
            text("face_score IS NULL OR face_score BETWEEN 0 AND 1"),
            name="ck_character_compositions_face_score",
        ),
        Index("idx_character_compositions_project_status", "project_id", "status"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, comment="项目 ID"
    )
    profile_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("identity_profiles.id", ondelete="CASCADE"), nullable=False, comment="身份档 ID"
    )
    batch_key: Mapped[str] = mapped_column(String(32), nullable=False, comment="批次键")
    style_preset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("style_presets.id", ondelete="SET NULL"), nullable=True, comment="画风预设"
    )
    stylization: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("50"), comment="风格化强度"
    )
    mode: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="M1", comment="合成模式"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending", comment="状态"
    )
    asset_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, comment="角色资产 ID"
    )
    preview_media_key: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="预览媒体")
    face_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True, comment="人脸相似度")
    over_similarity: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="过度相似"
    )
    self_check: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="自检明细")
    freeze_credits: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="冻结积分"
    )
    cost_credits: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="实际消耗"
    )
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="错误码")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="错误信息")
    rejected_codes: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="批次拒绝码"
    )
    config_version: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="配置版本")
    composed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="合成完成时间"
    )


__all__ = [
    "ContinuityFact",
    "StoryBibleCharacter",
    "StoryBibleScene",
    "StoryBibleProp",
    "ContinuitySnapshot",
    "ContinuityCheck",
    "LogicWaiver",
    "HumanFeedback",
    "NegativeExample",
    "VoiceParam",
    "VoiceTrack",
    "VoiceClip",
    "VoiceDriftRecord",
    "ShotLineBinding",
    "CompositionGraph",
    "CgNode",
    "CgEdge",
    "CgLayout",
    "RenderPlan",
    "CompositionSwitchLog",
    "ParamDef",
    "ParamConfig",
    "ConfigVersion",
    "ConfigAudit",
    "PhotoUpload",
    "IdentityProfile",
    "CharacterComposition",
]

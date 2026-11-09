"""V6.0–V6.4 增量：27 张新表与既有表字段扩展。

Revision ID: f1a2b3c4d5e6
Revises: e9a2c7d4b310
Create Date: 2026-10-22

覆盖范围（对齐 PRD V6.4）：
- F9 配音轨：voice_params / voice_tracks / voice_clips / voice_drift_records；
- F4.13 台词绑定：shot_line_bindings；
- F2.12 / F2.13 / 5A：continuity_facts / story_bible_* / continuity_snapshots /
  continuity_checks / logic_waivers / human_feedback / negative_examples；
- F10 合成编排：composition_graphs / cg_nodes / cg_edges / cg_layouts /
  render_plans / composition_switch_logs；
- F11 参数中心：param_defs / param_configs / config_versions / config_audits；
- F3.14 照片合成角色：photo_uploads / identity_profiles / character_compositions；
- 既有表扩展：script_lines / shots / projects。

所有新表默认值与 ORM 模型保持一致，JSONB 默认使用 ``'[]'::jsonb`` / ``'{}'::jsonb``。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e9a2c7d4b310"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 V6 增量表，并为既有表补充 V6 字段。"""
    # ------------------------------------------------------------------
    # 1. 既有表增量字段
    # ------------------------------------------------------------------
    op.add_column(
        "script_lines",
        sa.Column("line_key", sa.String(64), nullable=True, comment="台词稳定键（跨版本不变）"),
    )
    op.add_column(
        "script_lines",
        sa.Column(
            "speaker_type", sa.String(16), nullable=False, server_default="dialogue", comment="说话人类型"
        ),
    )
    op.add_column(
        "script_lines",
        sa.Column("est_ms", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="预估时长"),
    )
    op.add_column(
        "script_lines", sa.Column("actual_ms", sa.Integer(), nullable=True, comment="实测配音时长")
    )
    op.add_column(
        "script_lines",
        sa.Column(
            "voice_status", sa.String(16), nullable=False, server_default="pending", comment="配音状态"
        ),
    )
    op.add_column(
        "script_lines",
        sa.Column("source", sa.String(16), nullable=False, server_default="script", comment="台词来源"),
    )
    op.add_column(
        "script_lines",
        sa.Column(
            "is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="软删除位"
        ),
    )
    op.create_index("idx_script_lines_line_key", "script_lines", ["line_key"])

    op.add_column(
        "shots",
        sa.Column(
            "duration_source", sa.String(16), nullable=False, server_default="strategy", comment="时长来源"
        ),
    )
    op.add_column(
        "shots",
        sa.Column(
            "duration_manual", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="手工锁定时长"
        ),
    )
    op.add_column(
        "shots",
        sa.Column(
            "line_coverage_pct", sa.Numeric(5, 2), nullable=False, server_default=sa.text("100"), comment="台词覆盖率"
        ),
    )
    op.add_column(
        "shots",
        sa.Column(
            "duration_drift_ms", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="音频时长漂移"
        ),
    )
    op.add_column(
        "shots",
        sa.Column(
            "long_line", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="超长句提示"
        ),
    )

    op.add_column(
        "projects",
        sa.Column(
            "pipeline_mode", sa.String(16), nullable=False, server_default="voice_first", comment="制作流程"
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "config_version", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="当前配置版本"
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "bible_incomplete", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="Bible 不完整"
        ),
    )

    # ------------------------------------------------------------------
    # 2. 连续性事实与 Story Bible
    # ------------------------------------------------------------------
    op.create_table(
        "continuity_facts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("script_id", sa.BigInteger(), nullable=True, comment="来源剧本版本 ID"),
        sa.Column("scene_id", sa.BigInteger(), nullable=True, comment="场次 ID"),
        sa.Column("ep_no", sa.Integer(), nullable=False, comment="集号"),
        sa.Column("scene_no", sa.Integer(), nullable=False, comment="场号"),
        sa.Column(
            "facts", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"), comment="事实主体"
        ),
        sa.Column(
            "missing_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="缺失字段",
        ),
        sa.Column(
            "completeness_pct", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0"), comment="完整度"
        ),
        sa.Column("version_no", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="版本号"),
        sa.Column("source", sa.String(16), nullable=False, server_default="agent", comment="产出方式"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["script_id"], ["scripts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scene_id"], ["scenes.id"], ondelete="SET NULL"),
        sa.CheckConstraint("ep_no >= 1", name="ck_continuity_facts_ep_no"),
        sa.CheckConstraint("scene_no >= 1", name="ck_continuity_facts_scene_no"),
        sa.CheckConstraint("version_no >= 1", name="ck_continuity_facts_version_no"),
        sa.CheckConstraint("completeness_pct BETWEEN 0 AND 100", name="ck_continuity_facts_completeness"),
        sa.CheckConstraint("source IN ('agent', 'inferred', 'edit')", name="ck_continuity_facts_source"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "ep_no", "scene_no", "version_no", name="uq_continuity_facts_scene_version"
        ),
    )
    op.create_index("idx_continuity_facts_project_id_ep_no", "continuity_facts", ["project_id", "ep_no"])

    op.create_table(
        "story_bible_characters",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("char_key", sa.String(64), nullable=False, comment="角色稳定键"),
        sa.Column("name", sa.String(128), nullable=False, comment="角色名"),
        sa.Column("char_asset_id", sa.BigInteger(), nullable=True, comment="角色资产 ID"),
        sa.Column("base_look", sa.Text(), nullable=True, comment="基础造型"),
        sa.Column("body_desc", sa.Text(), nullable=True, comment="体态描述"),
        sa.Column("voice_id", sa.BigInteger(), nullable=True, comment="音色 ID"),
        sa.Column(
            "outfits", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb"), comment="服装"
        ),
        sa.Column(
            "current_state", sa.String(16), nullable=False, server_default="alive", comment="角色状态"
        ),
        sa.Column("extra", postgresql.JSONB(), nullable=True, comment="扩展字段"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["char_asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["voice_id"], ["tts_voices.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "current_state IN ('alive', 'dead', 'offstage', 'unknown')",
            name="ck_story_bible_characters_state",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "char_key", name="uq_story_bible_characters_key"),
    )
    op.create_index("idx_story_bible_characters_project_id", "story_bible_characters", ["project_id"])

    op.create_table(
        "story_bible_scenes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("ep_no", sa.Integer(), nullable=False, comment="集号"),
        sa.Column("scene_no", sa.Integer(), nullable=False, comment="场号"),
        sa.Column("location", sa.String(255), nullable=True, comment="地点"),
        sa.Column("layout_ref", sa.String(255), nullable=True, comment="空间布局引用"),
        sa.Column(
            "key_points",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="关键点位",
        ),
        sa.Column("time_of_day", sa.String(16), nullable=True, comment="时段"),
        sa.Column("elapsed_minutes", sa.Integer(), nullable=True, comment="剧情内分钟"),
        sa.Column("weather", sa.String(64), nullable=True, comment="天气"),
        sa.Column("lighting_base", sa.String(255), nullable=True, comment="光线基线"),
        sa.Column(
            "is_flashback", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="是否闪回"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.CheckConstraint("ep_no >= 1", name="ck_story_bible_scenes_ep_no"),
        sa.CheckConstraint("scene_no >= 1", name="ck_story_bible_scenes_scene_no"),
        sa.CheckConstraint(
            "elapsed_minutes IS NULL OR elapsed_minutes >= 0", name="ck_story_bible_scenes_elapsed"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "ep_no", "scene_no", name="uq_story_bible_scenes_position"),
    )
    op.create_index(
        "idx_story_bible_scenes_project_id_ep_no", "story_bible_scenes", ["project_id", "ep_no"]
    )

    op.create_table(
        "story_bible_props",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("prop_key", sa.String(64), nullable=False, comment="道具稳定键"),
        sa.Column("name", sa.String(128), nullable=False, comment="道具名"),
        sa.Column("prop_asset_id", sa.BigInteger(), nullable=True, comment="道具资产 ID"),
        sa.Column(
            "state_machine",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="状态链",
        ),
        sa.Column(
            "holder_by_scene",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="持有人映射",
        ),
        sa.Column(
            "ref_images",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="参考图",
        ),
        sa.Column("current_state", sa.String(64), nullable=True, comment="当前状态"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["prop_asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "prop_key", name="uq_story_bible_props_key"),
    )
    op.create_index("idx_story_bible_props_project_id", "story_bible_props", ["project_id"])

    op.create_table(
        "continuity_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("shot_id", sa.BigInteger(), nullable=False, comment="镜头 ID"),
        sa.Column(
            "expected_state",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="期望状态",
        ),
        sa.Column("source_version", sa.String(128), nullable=False, comment="来源版本串"),
        sa.Column("snapshot_hash", sa.String(64), nullable=False, comment="快照哈希"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shot_id", "snapshot_hash", name="uq_continuity_snapshots_shot_hash"),
    )
    op.create_index(
        "idx_continuity_snapshots_project_id_shot_id", "continuity_snapshots", ["project_id", "shot_id"]
    )

    op.create_table(
        "continuity_checks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("shot_id", sa.BigInteger(), nullable=False, comment="镜头 ID"),
        sa.Column("adjacent_shot_id", sa.BigInteger(), nullable=True, comment="相邻镜头 ID"),
        sa.Column("level", sa.String(8), nullable=False, comment="层级"),
        sa.Column("dimension", sa.String(32), nullable=False, comment="维度"),
        sa.Column("score", sa.Numeric(5, 4), nullable=True, comment="得分"),
        sa.Column("threshold", sa.Numeric(5, 4), nullable=True, comment="阈值"),
        sa.Column("verdict", sa.String(16), nullable=False, comment="裁决"),
        sa.Column("evidence_ref", postgresql.JSONB(), nullable=True, comment="证据"),
        sa.Column("reviewer", sa.String(16), nullable=False, server_default="auto", comment="复核方式"),
        sa.Column("config_version", sa.Integer(), nullable=True, comment="配置版本"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["adjacent_shot_id"], ["shots.id"], ondelete="SET NULL"),
        sa.CheckConstraint("level IN ('L1', 'L2', 'L3')", name="ck_continuity_checks_level"),
        sa.CheckConstraint(
            "dimension IN ('face', 'outfit', 'scene', 'prop', 'timing_light', 'plot_logic')",
            name="ck_continuity_checks_dimension",
        ),
        sa.CheckConstraint(
            "verdict IN ('pass', 'warn', 'fail', 'degraded', 'unavailable')",
            name="ck_continuity_checks_verdict",
        ),
        sa.CheckConstraint(
            "reviewer IN ('auto', 'human_confirm', 'human_override')", name="ck_continuity_checks_reviewer"
        ),
        sa.CheckConstraint("score IS NULL OR score BETWEEN 0 AND 1", name="ck_continuity_checks_score"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_continuity_checks_project_shot_dimension",
        "continuity_checks",
        ["project_id", "shot_id", "dimension"],
    )

    op.create_table(
        "logic_waivers",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("rule_code", sa.String(8), nullable=False, comment="规则编号"),
        sa.Column("target_ref", sa.String(128), nullable=False, comment="定位引用"),
        sa.Column("reason", sa.Text(), nullable=False, comment="豁免理由"),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True, comment="操作者"),
        sa.Column("extra", postgresql.JSONB(), nullable=True, comment="扩展信息"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "rule_code IN ('LR-1', 'LR-2', 'LR-3', 'LR-4', 'LR-5', 'LR-6')",
            name="ck_logic_waivers_rule_code",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "rule_code", "target_ref", name="uq_logic_waivers_rule_target"),
    )
    op.create_index("idx_logic_waivers_project_id", "logic_waivers", ["project_id"])

    # ------------------------------------------------------------------
    # 3. 结构化反馈
    # ------------------------------------------------------------------
    op.create_table(
        "human_feedback",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("target_type", sa.String(16), nullable=False, comment="对象类型"),
        sa.Column("target_id", sa.BigInteger(), nullable=False, comment="对象 ID"),
        sa.Column("dimensions", postgresql.JSONB(), nullable=False, comment="维度数组"),
        sa.Column("scope", postgresql.JSONB(), nullable=False, comment="作用域"),
        sa.Column("expectation", sa.String(32), nullable=True, comment="期望枚举"),
        sa.Column("expectation_text", sa.Text(), nullable=True, comment="期望文本"),
        sa.Column(
            "keep_items",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="保留项",
        ),
        sa.Column("text", sa.Text(), nullable=True, comment="补充说明"),
        sa.Column("source_gate", sa.String(16), nullable=False, comment="来源关口"),
        sa.Column("routed_strategy", sa.String(64), nullable=False, comment="路由策略"),
        sa.Column("strategy_round", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="策略轮次"),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True, comment="提交人"),
        sa.Column(
            "adopted", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="是否采纳"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "target_type IN ('shot', 'line', 'episode', 'final_cut')", name="ck_human_feedback_target_type"
        ),
        sa.CheckConstraint(
            "source_gate IN ('gate1', 'gateV', 'gate2', 'gate3', 'gate4')",
            name="ck_human_feedback_source_gate",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_human_feedback_project_target", "human_feedback", ["project_id", "target_type", "target_id"]
    )

    op.create_table(
        "negative_examples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("dimension", sa.String(32), nullable=False, comment="维度"),
        sa.Column("character_asset_id", sa.BigInteger(), nullable=True, comment="角色资产 ID"),
        sa.Column("scene_ref", sa.String(128), nullable=True, comment="场次引用"),
        sa.Column("issue_desc", sa.Text(), nullable=False, comment="问题描述"),
        sa.Column("evidence_ref", postgresql.JSONB(), nullable=True, comment="证据"),
        sa.Column("fix_action", sa.String(128), nullable=True, comment="修正动作"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="是否通过"),
        sa.Column("source", sa.String(16), nullable=False, comment="来源"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["character_asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.CheckConstraint("source IN ('reject', 'misjudge', 'manual_fix')", name="ck_negative_examples_source"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_negative_examples_project_dimension", "negative_examples", ["project_id", "dimension"]
    )

    # ------------------------------------------------------------------
    # 4. 配音轨与台词绑定
    # ------------------------------------------------------------------
    op.create_table(
        "voice_params",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("character_asset_id", sa.BigInteger(), nullable=False, comment="角色资产 ID"),
        sa.Column("voice_id", sa.BigInteger(), nullable=True, comment="音色 ID"),
        sa.Column("rate", sa.Numeric(4, 2), nullable=False, server_default=sa.text("1.00"), comment="语速"),
        sa.Column("pitch", sa.SmallInteger(), nullable=False, server_default=sa.text("0"), comment="音调"),
        sa.Column("emotion_default", sa.String(32), nullable=True, comment="默认情绪"),
        sa.Column("params_hash", sa.String(64), nullable=False, comment="参数哈希"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active", comment="状态"),
        sa.Column("sample_audio_key", sa.String(255), nullable=True, comment="样音键"),
        sa.Column("sample_duration_ms", sa.Integer(), nullable=True, comment="样音时长"),
        sa.Column("confirmed_by", sa.BigInteger(), nullable=True, comment="确认人"),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True, comment="确认时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["character_asset_id"], ["assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["voice_id"], ["tts_voices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("rate BETWEEN 0.5 AND 2.0", name="ck_voice_params_rate"),
        sa.CheckConstraint("pitch BETWEEN -6 AND 6", name="ck_voice_params_pitch"),
        sa.CheckConstraint("status IN ('active', 'superseded')", name="ck_voice_params_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "character_asset_id", "params_hash", name="uq_voice_params_character_hash"
        ),
    )
    op.create_index("idx_voice_params_project_id", "voice_params", ["project_id"])

    op.create_table(
        "voice_tracks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("ep_no", sa.Integer(), nullable=False, comment="集号"),
        sa.Column("status", sa.String(24), nullable=False, server_default="not_started", comment="状态"),
        sa.Column("total_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0"), comment="总时长"),
        sa.Column("speech_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0"), comment="语音时长"),
        sa.Column("silence_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0"), comment="静默时长"),
        sa.Column("gap_ms", sa.Integer(), nullable=False, server_default=sa.text("120"), comment="句间气口"),
        sa.Column("line_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="台词数"),
        sa.Column("voiced_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="完成数"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="失败数"),
        sa.Column("gate_status", sa.String(16), nullable=False, server_default="pending", comment="关口 V"),
        sa.Column("report", postgresql.JSONB(), nullable=True, comment="时长报告"),
        sa.Column(
            "align_degraded", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="对齐降级"
        ),
        sa.Column("approved_by", sa.BigInteger(), nullable=True, comment="审批人"),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True, comment="审批时间"),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True, comment="锁定时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("ep_no >= 1", name="ck_voice_tracks_ep_no"),
        sa.CheckConstraint(
            "status IN ('not_started', 'previewing_done', 'voice_ready', 'voice_locked', 'voice_drift')",
            name="ck_voice_tracks_status",
        ),
        sa.CheckConstraint(
            "gate_status IN ('pending', 'approved', 'rejected')", name="ck_voice_tracks_gate_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "ep_no", name="uq_voice_tracks_project_ep"),
    )
    op.create_index("idx_voice_tracks_project_id_status", "voice_tracks", ["project_id", "status"])

    op.create_table(
        "voice_clips",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("track_id", sa.BigInteger(), nullable=False, comment="配音轨 ID"),
        sa.Column("line_id", sa.BigInteger(), nullable=False, comment="台词 ID"),
        sa.Column("line_key", sa.String(64), nullable=False, comment="台词稳定键"),
        sa.Column("text_hash", sa.String(64), nullable=False, comment="文本哈希"),
        sa.Column("params_hash", sa.String(64), nullable=False, comment="参数哈希"),
        sa.Column("voice_id", sa.BigInteger(), nullable=True, comment="音色 ID"),
        sa.Column("character_asset_id", sa.BigInteger(), nullable=True, comment="角色资产 ID"),
        sa.Column("source", sa.String(8), nullable=False, server_default="ai", comment="来源"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending", comment="状态"),
        sa.Column("audio_key", sa.String(255), nullable=True, comment="音频键"),
        sa.Column("audio_url", sa.String(512), nullable=True, comment="音频地址"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="时长"),
        sa.Column(
            "words", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb"), comment="词级时间戳"
        ),
        sa.Column(
            "alignment_source", sa.String(20), nullable=False, server_default="none", comment="对齐来源"
        ),
        sa.Column(
            "align_degraded", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="是否降级"
        ),
        sa.Column("retry_count", sa.SmallInteger(), nullable=False, server_default=sa.text("0"), comment="重试次数"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="失败原因"),
        sa.Column("billed", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="是否计费"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["track_id"], ["voice_tracks.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["line_id"], ["script_lines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["voice_id"], ["tts_voices.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["character_asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('pending', 'voicing', 'voiced', 'failed', 'aligning_failed', 'dirty_voice')",
            name="ck_voice_clips_status",
        ),
        sa.CheckConstraint("source IN ('ai', 'human')", name="ck_voice_clips_source"),
        sa.CheckConstraint(
            "alignment_source IN ('whisperx', 'tts_timestamp', 'human', 'none')",
            name="ck_voice_clips_alignment_source",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("track_id", "line_id", name="uq_voice_clips_track_line"),
    )
    op.create_index("idx_voice_clips_cache", "voice_clips", ["text_hash", "params_hash"])
    op.create_index("idx_voice_clips_project_id_status", "voice_clips", ["project_id", "status"])

    op.create_table(
        "voice_drift_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("ep_no", sa.Integer(), nullable=False, comment="集号"),
        sa.Column("line_id", sa.BigInteger(), nullable=True, comment="台词 ID"),
        sa.Column("old_ms", sa.Integer(), nullable=False, comment="旧时长"),
        sa.Column("new_ms", sa.Integer(), nullable=False, comment="新时长"),
        sa.Column("delta_ms", sa.Integer(), nullable=False, comment="差值"),
        sa.Column("delta_pct", sa.Numeric(6, 2), nullable=False, server_default=sa.text("0"), comment="变化百分比"),
        sa.Column("level", sa.String(16), nullable=False, comment="分级"),
        sa.Column(
            "affected_shot_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="受影响镜头",
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", comment="状态"),
        sa.Column("source", sa.String(16), nullable=False, server_default="rerecord", comment="来源"),
        sa.Column("config_version", sa.Integer(), nullable=True, comment="配置版本"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["line_id"], ["script_lines.id"], ondelete="SET NULL"),
        sa.CheckConstraint("level IN ('absorb', 'recalc', 'regenerate')", name="ck_voice_drift_records_level"),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'dismissed')", name="ck_voice_drift_records_status"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_voice_drift_records_project_ep", "voice_drift_records", ["project_id", "ep_no"])

    op.create_table(
        "shot_line_bindings",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("shot_id", sa.BigInteger(), nullable=False, comment="镜头 ID"),
        sa.Column("line_id", sa.BigInteger(), nullable=False, comment="台词 ID"),
        sa.Column("offset_ms", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="镜头内偏移"),
        sa.Column("order_index", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="顺序"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="绑定人"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shot_id"], ["shots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["line_id"], ["script_lines.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("offset_ms >= 0", name="ck_shot_line_bindings_offset"),
        sa.CheckConstraint("order_index >= 0", name="ck_shot_line_bindings_order"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shot_id", "line_id", name="uq_shot_line_bindings_shot_line"),
    )
    op.create_index("idx_shot_line_bindings_line_id", "shot_line_bindings", ["line_id"])
    op.create_index("idx_shot_line_bindings_project_id", "shot_line_bindings", ["project_id"])

    # ------------------------------------------------------------------
    # 5. 合成编排（F10）
    # ------------------------------------------------------------------
    op.create_table(
        "composition_graphs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("ep_no", sa.Integer(), nullable=False, comment="集号"),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1"), comment="版本"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active", comment="状态"),
        sa.Column("view_pref", sa.String(8), nullable=False, server_default="track", comment="视图偏好"),
        sa.Column("graph_hash", sa.String(64), nullable=True, comment="图哈希"),
        sa.Column("source", sa.String(16), nullable=False, server_default="auto", comment="来源"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人"),
        sa.Column(
            "locked_by_render", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="渲染锁定"
        ),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True, comment="激活时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("version >= 1", name="ck_composition_graphs_version"),
        sa.CheckConstraint("status IN ('draft', 'active', 'archived')", name="ck_composition_graphs_status"),
        sa.CheckConstraint("view_pref IN ('track', 'node')", name="ck_composition_graphs_view_pref"),
        sa.CheckConstraint("source IN ('auto', 'manual', 'template')", name="ck_composition_graphs_source"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "ep_no", "version", name="uq_composition_graphs_version"),
    )
    op.create_index(
        "idx_composition_graphs_project_ep_status", "composition_graphs", ["project_id", "ep_no", "status"]
    )

    op.create_table(
        "cg_nodes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("graph_id", sa.BigInteger(), nullable=False, comment="图 ID"),
        sa.Column("node_key", sa.String(64), nullable=False, comment="节点键"),
        sa.Column("node_type", sa.String(20), nullable=False, comment="节点类型"),
        sa.Column("ref_id", sa.String(64), nullable=True, comment="引用对象 ID"),
        sa.Column("ref_kind", sa.String(16), nullable=True, comment="引用类型"),
        sa.Column(
            "params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"), comment="节点参数"
        ),
        sa.Column("label", sa.String(255), nullable=True, comment="标签"),
        sa.Column("parent_id", sa.BigInteger(), nullable=True, comment="父节点"),
        sa.Column("ui_pos", postgresql.JSONB(), nullable=True, comment="坐标"),
        sa.Column("order_hint", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="排序"),
        sa.Column(
            "is_stale", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="引用失效"
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="normal", comment="状态"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["graph_id"], ["composition_graphs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["cg_nodes.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "node_type IN ('clip', 'group', 'title', 'audio_overlay', 'compose', 'branch', 'start', 'end')",
            name="ck_cg_nodes_type",
        ),
        sa.CheckConstraint("status IN ('normal', 'stale', 'disabled')", name="ck_cg_nodes_status"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("graph_id", "node_key", name="uq_cg_nodes_graph_key"),
    )
    op.create_index("idx_cg_nodes_graph_id_type", "cg_nodes", ["graph_id", "node_type"])

    op.create_table(
        "cg_edges",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("graph_id", sa.BigInteger(), nullable=False, comment="图 ID"),
        sa.Column("from_node_id", sa.BigInteger(), nullable=False, comment="起点"),
        sa.Column("to_node_id", sa.BigInteger(), nullable=False, comment="终点"),
        sa.Column("transition", sa.String(16), nullable=False, server_default="cut", comment="转场"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="转场时长"),
        sa.Column("order_hint", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="排序"),
        sa.Column("is_main", sa.Boolean(), nullable=False, server_default=sa.text("true"), comment="主路径"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["graph_id"], ["composition_graphs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_node_id"], ["cg_nodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_node_id"], ["cg_nodes.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "transition IN ('cut', 'dissolve', 'fade', 'wipe', 'slide')", name="ck_cg_edges_transition"
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_cg_edges_duration"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("graph_id", "from_node_id", "to_node_id", name="uq_cg_edges_graph_from_to"),
    )
    op.create_index("idx_cg_edges_graph_id_order", "cg_edges", ["graph_id", "order_hint"])

    op.create_table(
        "cg_layouts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("graph_id", sa.BigInteger(), nullable=False, comment="图 ID"),
        sa.Column("view_type", sa.String(8), nullable=False, comment="视图类型"),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"), comment="布局数据"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["graph_id"], ["composition_graphs.id"], ondelete="CASCADE"),
        sa.CheckConstraint("view_type IN ('track', 'node')", name="ck_cg_layouts_view_type"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("graph_id", "view_type", name="uq_cg_layouts_graph_view"),
    )

    op.create_table(
        "render_plans",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("graph_id", sa.BigInteger(), nullable=False, comment="图 ID"),
        sa.Column("graph_hash", sa.String(64), nullable=False, comment="图哈希"),
        sa.Column("plan_hash", sa.String(64), nullable=False, comment="计划哈希"),
        sa.Column("plan_json", postgresql.JSONB(), nullable=False, comment="计划"),
        sa.Column("output_ref", sa.String(255), nullable=True, comment="产物引用"),
        sa.Column("status", sa.String(16), nullable=False, server_default="compiled", comment="状态"),
        sa.Column("reused_count", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="复用次数"),
        sa.Column("cost_credits", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="成本"),
        sa.Column("duration_ms", sa.BigInteger(), nullable=False, server_default=sa.text("0"), comment="时长"),
        sa.Column("is_preview", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="试渲"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["graph_id"], ["composition_graphs.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('compiled', 'reused', 'rendering', 'rendered', 'failed')", name="ck_render_plans_status"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_hash", name="uq_render_plans_plan_hash"),
    )
    op.create_index("idx_render_plans_project_id_status", "render_plans", ["project_id", "status"])

    op.create_table(
        "composition_switch_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("graph_id", sa.BigInteger(), nullable=False, comment="图 ID"),
        sa.Column("from_view", sa.String(8), nullable=False, comment="来源视图"),
        sa.Column("to_view", sa.String(8), nullable=False, comment="目标视图"),
        sa.Column("lossless", sa.Boolean(), nullable=False, server_default=sa.text("true"), comment="是否无损"),
        sa.Column(
            "discarded_nodes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="折叠节点",
        ),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True, comment="操作者"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["graph_id"], ["composition_graphs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "from_view IN ('track', 'node') AND to_view IN ('track', 'node')",
            name="ck_composition_switch_logs_views",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_composition_switch_logs_graph_id", "composition_switch_logs", ["graph_id"])

    # ------------------------------------------------------------------
    # 6. 参数中心（F11）
    # ------------------------------------------------------------------
    op.create_table(
        "param_defs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("param_key", sa.String(64), nullable=False, comment="参数键"),
        sa.Column("param_group", sa.String(8), nullable=False, comment="分组"),
        sa.Column("label", sa.String(128), nullable=False, comment="参数名"),
        sa.Column("value_type", sa.String(16), nullable=False, comment="值类型"),
        sa.Column("default_value", postgresql.JSONB(), nullable=False, comment="默认值"),
        sa.Column("hard_min", postgresql.JSONB(), nullable=True, comment="硬下界"),
        sa.Column("hard_max", postgresql.JSONB(), nullable=True, comment="硬上界"),
        sa.Column("suggested_min", postgresql.JSONB(), nullable=True, comment="建议下界"),
        sa.Column("suggested_max", postgresql.JSONB(), nullable=True, comment="建议上界"),
        sa.Column(
            "enum_options",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="枚举选项",
        ),
        sa.Column("unit", sa.String(32), nullable=True, comment="单位"),
        sa.Column("effect_scope", sa.String(24), nullable=False, comment="生效方式"),
        sa.Column(
            "is_dangerous", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="危险参数"
        ),
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="合规锁定"),
        sa.Column("is_advanced", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="进阶参数"),
        sa.Column("is_common", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="常用参数"),
        sa.Column("description", sa.String(255), nullable=False, comment="作用说明"),
        sa.Column("high_effect", sa.String(255), nullable=False, comment="调高影响"),
        sa.Column("low_effect", sa.String(255), nullable=False, comment="调低影响"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="排序"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("value_type IN ('int', 'float', 'bool', 'enum')", name="ck_param_defs_value_type"),
        sa.CheckConstraint(
            "effect_scope IN ('immediate', 'next_check', 'next_generate', 'next_voice', "
            "'next_storyboard', 'next_compose')",
            name="ck_param_defs_effect_scope",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("param_key", name="uq_param_defs_param_key"),
    )
    op.create_index("idx_param_defs_group_order", "param_defs", ["param_group", "display_order"])

    op.create_table(
        "param_configs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("scope", sa.String(16), nullable=False, comment="作用域"),
        sa.Column("scope_id", sa.String(32), nullable=False, server_default="0", comment="作用域标识"),
        sa.Column("param_key", sa.String(64), nullable=False, comment="参数键"),
        sa.Column("value", postgresql.JSONB(), nullable=False, comment="覆盖值"),
        sa.Column("updated_by", sa.BigInteger(), nullable=True, comment="更新人"),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="版本"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "scope IN ('project', 'episode', 'user_pref')", name="ck_param_configs_scope"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id", "scope", "scope_id", "param_key", name="uq_param_configs_project_scope_key"
        ),
    )
    op.create_index("idx_param_configs_project_id", "param_configs", ["project_id"])

    op.create_table(
        "config_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("version_no", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False, comment="快照"),
        sa.Column(
            "changes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="变更摘要",
        ),
        sa.Column("source", sa.String(24), nullable=False, server_default="save", comment="来源"),
        sa.Column("rollback_from", sa.Integer(), nullable=True, comment="回滚来源"),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True, comment="操作者"),
        sa.Column("config_hash", sa.String(64), nullable=False, comment="快照哈希"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("version_no >= 1", name="ck_config_versions_version_no"),
        sa.CheckConstraint(
            "source IN ('save', 'rollback', 'restore_default')", name="ck_config_versions_source"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "version_no", name="uq_config_versions_project_version"),
    )
    op.create_index("idx_config_versions_project_id", "config_versions", ["project_id"])

    op.create_table(
        "config_audits",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("config_version", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column("param_key", sa.String(64), nullable=False, comment="参数键"),
        sa.Column("old_value", postgresql.JSONB(), nullable=True, comment="旧值"),
        sa.Column("new_value", postgresql.JSONB(), nullable=True, comment="新值"),
        sa.Column(
            "is_dangerous", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="危险参数"
        ),
        sa.Column("reason", sa.Text(), nullable=True, comment="操作理由"),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True, comment="操作者"),
        sa.Column("action", sa.String(24), nullable=False, server_default="update", comment="动作"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("action IN ('update', 'rollback', 'restore_default')", name="ck_config_audits_action"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_config_audits_project_id_version", "config_audits", ["project_id", "config_version"])

    # ------------------------------------------------------------------
    # 7. 照片合成角色（F3.14）
    # ------------------------------------------------------------------
    op.create_table(
        "photo_uploads",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("batch_key", sa.String(32), nullable=False, comment="批次键"),
        sa.Column("uploader_user_id", sa.BigInteger(), nullable=True, comment="上传人"),
        sa.Column("file_name", sa.String(255), nullable=False, comment="文件名"),
        sa.Column("object_key", sa.String(255), nullable=True, comment="对象键"),
        sa.Column("mime", sa.String(64), nullable=False, comment="MIME"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False, comment="文件大小"),
        sa.Column("width", sa.Integer(), nullable=True, comment="宽"),
        sa.Column("height", sa.Integer(), nullable=True, comment="高"),
        sa.Column("sha256", sa.String(64), nullable=True, comment="内容指纹"),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploaded", comment="状态"),
        sa.Column("quality_score", sa.Numeric(4, 3), nullable=True, comment="质量分"),
        sa.Column("analysis", postgresql.JSONB(), nullable=True, comment="分析明细"),
        sa.Column("reject_code", sa.String(32), nullable=True, comment="拒绝码"),
        sa.Column("reject_message", sa.String(255), nullable=True, comment="拒绝提示"),
        sa.Column("gate", sa.String(16), nullable=True, comment="拒绝闸"),
        sa.Column("face_count", sa.SmallInteger(), nullable=False, server_default=sa.text("1"), comment="人脸数"),
        sa.Column("pose_yaw", sa.Numeric(5, 2), nullable=True, comment="左右偏转"),
        sa.Column("pose_pitch", sa.Numeric(5, 2), nullable=True, comment="俯仰角"),
        sa.Column("blur_score", sa.Numeric(4, 3), nullable=True, comment="清晰度"),
        sa.Column("lighting_score", sa.Numeric(4, 3), nullable=True, comment="光照"),
        sa.Column(
            "is_public_figure", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="公众人物"
        ),
        sa.Column("minor_confidence", sa.Numeric(4, 3), nullable=True, comment="未成年置信度"),
        sa.Column(
            "is_sensitive", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="敏感内容"
        ),
        sa.Column(
            "is_duplicate", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="重复照片"
        ),
        sa.Column("is_main", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="主图"),
        sa.Column(
            "batch_blocked", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="批次拦截"
        ),
        sa.Column("declaration", sa.String(24), nullable=False, server_default="self", comment="用途声明"),
        sa.Column(
            "authorization_confirmed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="授权确认",
        ),
        sa.Column(
            "exif_stripped", sa.Boolean(), nullable=False, server_default=sa.text("true"), comment="剥离 EXIF"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploader_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('uploading', 'uploaded', 'analyzing', 'usable', 'degraded', "
            "'rejected', 'consumed', 'removed', 'invalid')",
            name="ck_photo_uploads_status",
        ),
        sa.CheckConstraint(
            "gate IS NULL OR gate IN ('static', 'content', 'compliance', 'dedup')",
            name="ck_photo_uploads_gate",
        ),
        sa.CheckConstraint("declaration IN ('self', 'authorized_other')", name="ck_photo_uploads_declaration"),
        sa.CheckConstraint("size_bytes > 0", name="ck_photo_uploads_size"),
        sa.CheckConstraint(
            "quality_score IS NULL OR quality_score BETWEEN 0 AND 1", name="ck_photo_uploads_quality"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_photo_uploads_project_batch", "photo_uploads", ["project_id", "batch_key"])
    op.create_index("idx_photo_uploads_sha256", "photo_uploads", ["sha256"])

    op.create_table(
        "identity_profiles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("batch_key", sa.String(32), nullable=False, comment="批次键"),
        sa.Column("name", sa.String(128), nullable=False, comment="身份档名称"),
        sa.Column("main_photo_id", sa.BigInteger(), nullable=True, comment="主图 ID"),
        sa.Column(
            "photo_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb"), comment="照片 IDs"
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False, comment="身份指纹"),
        sa.Column("cluster_info", postgresql.JSONB(), nullable=True, comment="聚类明细"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft", comment="状态"),
        sa.Column("created_by", sa.BigInteger(), nullable=True, comment="创建人"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["main_photo_id"], ["photo_uploads.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("status IN ('draft', 'ready', 'consumed')", name="ck_identity_profiles_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_identity_profiles_project_batch", "identity_profiles", ["project_id", "batch_key"])

    op.create_table(
        "character_compositions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False, comment="项目 ID"),
        sa.Column("profile_id", sa.BigInteger(), nullable=False, comment="身份档 ID"),
        sa.Column("batch_key", sa.String(32), nullable=False, comment="批次键"),
        sa.Column("style_preset_id", sa.BigInteger(), nullable=True, comment="画风预设"),
        sa.Column(
            "stylization", sa.SmallInteger(), nullable=False, server_default=sa.text("50"), comment="风格化强度"
        ),
        sa.Column("mode", sa.String(8), nullable=False, server_default="M1", comment="模式"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending", comment="状态"),
        sa.Column("asset_id", sa.BigInteger(), nullable=True, comment="角色资产 ID"),
        sa.Column("preview_media_key", sa.String(255), nullable=True, comment="预览媒体"),
        sa.Column("face_score", sa.Numeric(4, 3), nullable=True, comment="人脸相似度"),
        sa.Column(
            "over_similarity", sa.Boolean(), nullable=False, server_default=sa.text("false"), comment="过度相似"
        ),
        sa.Column("self_check", postgresql.JSONB(), nullable=True, comment="自检明细"),
        sa.Column("freeze_credits", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="冻结积分"),
        sa.Column("cost_credits", sa.Integer(), nullable=False, server_default=sa.text("0"), comment="消耗积分"),
        sa.Column("error_code", sa.String(32), nullable=True, comment="错误码"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="错误信息"),
        sa.Column(
            "rejected_codes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="拒绝码",
        ),
        sa.Column("config_version", sa.Integer(), nullable=True, comment="配置版本"),
        sa.Column("composed_at", sa.DateTime(timezone=True), nullable=True, comment="合成完成时间"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["profile_id"], ["identity_profiles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["style_preset_id"], ["style_presets.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('pending', 'composing', 'preview_ready', 'confirmed', 'failed', 'rejected')",
            name="ck_character_compositions_status",
        ),
        sa.CheckConstraint("mode = 'M1'", name="ck_character_compositions_mode"),
        sa.CheckConstraint("stylization BETWEEN 0 AND 100", name="ck_character_compositions_stylization"),
        sa.CheckConstraint(
            "face_score IS NULL OR face_score BETWEEN 0 AND 1", name="ck_character_compositions_face_score"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_character_compositions_project_status", "character_compositions", ["project_id", "status"]
    )


def downgrade() -> None:
    """逆序删除 V6 增量表并移除既有表字段。"""
    for table in (
        "character_compositions",
        "identity_profiles",
        "photo_uploads",
        "config_audits",
        "config_versions",
        "param_configs",
        "param_defs",
        "composition_switch_logs",
        "render_plans",
        "cg_layouts",
        "cg_edges",
        "cg_nodes",
        "composition_graphs",
        "shot_line_bindings",
        "voice_drift_records",
        "voice_clips",
        "voice_tracks",
        "voice_params",
        "negative_examples",
        "human_feedback",
        "logic_waivers",
        "continuity_checks",
        "continuity_snapshots",
        "story_bible_props",
        "story_bible_scenes",
        "story_bible_characters",
        "continuity_facts",
    ):
        op.drop_table(table)

    op.drop_column("projects", "bible_incomplete")
    op.drop_column("projects", "config_version")
    op.drop_column("projects", "pipeline_mode")

    op.drop_column("shots", "long_line")
    op.drop_column("shots", "duration_drift_ms")
    op.drop_column("shots", "line_coverage_pct")
    op.drop_column("shots", "duration_manual")
    op.drop_column("shots", "duration_source")

    op.drop_index("idx_script_lines_line_key", table_name="script_lines")
    op.drop_column("script_lines", "is_deleted")
    op.drop_column("script_lines", "source")
    op.drop_column("script_lines", "voice_status")
    op.drop_column("script_lines", "actual_ms")
    op.drop_column("script_lines", "est_ms")
    op.drop_column("script_lines", "speaker_type")
    op.drop_column("script_lines", "line_key")

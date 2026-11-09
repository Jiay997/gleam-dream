"""v4 data increment

Revision ID: d4e8b1f60c27
Revises: c5d9e2f8a413
Create Date: 2026-10-20 20:19:02.559553

背景（《10 开发进度评估报告》V4 增量 0% 落地 → 本次补齐）：

对齐《02 数据库设计文档》§13.5 V4.0 数据集增量与《01 PRD V4.0》第 8 章 DR-9~12：

1. **新增表（11 张）**——V4 官方 5 张（``script_runs`` / ``script_template_refs`` /
   ``storyboard_templates`` / ``export_presets`` / ``script_export_logs``）+
   6 张字段宿主表（``director_notes`` / ``storyboard_ops`` / ``voice_bindings`` /
   ``subtitle_lines`` / ``mix_profiles`` / ``render_batches``）；
2. **新增字段（35 个，按表）**——剧本版本命名/置信度、分集钩子建议与预估时长、
   资产标签/音色状态/变体/累计消耗、授权状态、镜头锁定位/上游变更/重抽次数、
   镜头版本标签备注、渲染任务人工放行与失败文案/优先级来源、字幕样式自定义位、
   BGM 授权范围、时间线变更摘要、导出缓存键/预设/命名模板/过期/AI 标识、
   分发状态扩展字段、审核对象类型与剧本版本关联、HITL 反馈作用域；
3. **状态 CHECK 扩展（4 处）**——``render_tasks.status`` 增 ``abandoned`` /
   ``moderation_rejected``；``export_jobs.status`` 增 V4 六态；
   ``distribution_records.status`` 增 ``draft`` / ``submitting`` / ``confirming`` /
   ``withdrawn``；``export_jobs.resolution`` 兼容 ``4K`` 大小写。
4. **索引补充**——``script_runs(project_id, created_at desc)``、
   ``export_jobs(cache_key)`` 唯一（命中复用）、
   ``distribution_records(project_id, status)``、``asset_versions(asset_id, created_at)``。

说明：LangGraph checkpointer 的 ``checkpoints`` / ``checkpoint_blobs`` /
``checkpoint_writes`` / ``checkpoint_migrations`` 四张表由运行时按需创建，
不属于业务模型，本迁移**不**触碰。
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd4e8b1f60c27'
down_revision: Union[str, None] = 'c5d9e2f8a413'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---- 既有表新增的 CHECK 约束（表, 约束名, 表达式）----
_ADDED_CHECKS: list[tuple[str, str, str]] = [
    ("scripts", "ck_scripts_confidence_overall",
     "confidence_overall IS NULL OR confidence_overall BETWEEN 0 AND 1"),
    ("episodes", "ck_episodes_est_duration_sec",
     "est_duration_sec IS NULL OR est_duration_sec BETWEEN 60 AND 180"),
    ("episodes", "ck_episodes_hook_suggestion_len",
     "hook_suggestion IS NULL OR char_length(hook_suggestion) <= 50"),
    ("assets", "ck_assets_voice_status",
     "voice_status IS NULL OR voice_status IN ('bound', 'delisted', 'pending')"),
    ("asset_versions", "ck_asset_versions_cumulative_cost",
     "cumulative_cost IS NULL OR cumulative_cost >= 0"),
    ("asset_versions", "ck_asset_versions_revision_notes_len",
     "revision_notes IS NULL OR char_length(revision_notes) <= 200"),
    ("face_authorizations", "ck_face_authorizations_status",
     "status IN ('active', 'revoked')"),
    ("shots", "ck_shots_redraft_count",
     "redraft_count IS NULL OR redraft_count BETWEEN 0 AND 3"),
    ("shot_versions", "ck_shot_versions_label",
     "label IS NULL OR label IN ('color_good', 'action_right', "
     "'expression_natural', 'other')"),
    ("shot_versions", "ck_shot_versions_note_len",
     "note IS NULL OR char_length(note) <= 100"),
    ("render_tasks", "ck_render_tasks_priority_source",
     "priority_source IS NULL OR priority_source IN "
     "('first3', 'queue_jump', 'batch_order')"),
    ("render_tasks", "ck_render_tasks_manual_pass_reason_len",
     "manual_pass_reason IS NULL OR char_length(manual_pass_reason) <= 100"),
    ("distribution_records", "ck_distribution_records_retry_count",
     "retry_count >= 0"),
    ("bgm_tracks", "ck_bgm_tracks_license_scope",
     "license_scope IS NULL OR license_scope IN "
     "('platform_only', 'distribution_ok')"),
    ("moderation_records", "ck_moderation_records_target_type",
     "target_type IS NULL OR target_type IN "
     "('script', 'shot', 'asset', 'timeline', 'cover')"),
]

# ---- 既有 CHECK 的取值扩展（表, 约束名, 旧表达式, 新表达式）----
_EXTENDED_CHECKS: list[tuple[str, str, str, str]] = [
    (
        "render_tasks",
        "ck_render_tasks_status",
        "status IN ('pending', 'queued', 'running', 'qc_review', 'succeeded', "
        "'failed', 'cancelled', 'retrying', 'manual_required')",
        "status IN ('pending', 'queued', 'running', 'qc_review', 'succeeded', "
        "'failed', 'cancelled', 'retrying', 'manual_required', 'abandoned', "
        "'moderation_rejected')",
    ),
    (
        "export_jobs",
        "ck_export_jobs_status",
        "status IN ('pending', 'running', 'succeeded', 'failed')",
        "status IN ('pending', 'running', 'succeeded', 'failed', 'export_queued', "
        "'transcoding', 'packaging', 'export_ready', 'export_failed', 'expired')",
    ),
    (
        "export_jobs",
        "ck_export_jobs_resolution",
        "resolution IN ('720p', '1080p', '4k')",
        "resolution IN ('720p', '1080p', '4k', '4K')",
    ),
    (
        "distribution_records",
        "ck_distribution_records_status",
        "status IN ('pending', 'uploading', 'succeeded', 'failed')",
        "status IN ('draft', 'pending', 'submitting', 'uploading', 'confirming', "
        "'succeeded', 'failed', 'withdrawn')",
    ),
]


def upgrade() -> None:
    """升级：建 11 张新表 → 加 35 个字段 → 扩 4 组状态 CHECK → 补索引。"""

    # ---------- 1. 新增表 ----------
    op.create_table(
        'export_presets',
        sa.Column('owner_user_id', sa.BigInteger(), nullable=False, comment='所有者用户 ID'),
        sa.Column('name', sa.String(length=128), nullable=False, comment='预设名'),
        sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='参数快照'),
        sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='是否默认预设'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_export_presets_owner_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_export_presets')),
        sa.UniqueConstraint('owner_user_id', 'name', name='uq_export_presets_owner_user_id_name'),
    )
    op.create_index('idx_export_presets_owner_user_id', 'export_presets', ['owner_user_id'], unique=False)

    op.create_table(
        'storyboard_templates',
        sa.Column('owner_user_id', sa.BigInteger(), nullable=False, comment='所有者用户 ID'),
        sa.Column('name', sa.String(length=128), nullable=False, comment='模板名'),
        sa.Column('kind', sa.String(length=32), server_default='custom', nullable=False, comment='模板类型（4 值）'),
        sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='机位规则条目（≤10）'),
        sa.Column('description', sa.Text(), nullable=True, comment='适用说明'),
        sa.Column('is_builtin', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='是否内置模板'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint("kind IN ('dialogue', 'action', 'transition', 'custom')", name='ck_storyboard_templates_kind'),
        sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_storyboard_templates_owner_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_storyboard_templates')),
    )
    op.create_index('idx_storyboard_templates_owner_user_id', 'storyboard_templates', ['owner_user_id'], unique=False)

    op.create_table(
        'render_batches',
        sa.Column('project_id', sa.BigInteger(), nullable=False, comment='项目 ID'),
        sa.Column('batch_key', sa.String(length=64), nullable=False, comment='批次业务键'),
        sa.Column('strategy_tier', sa.String(length=16), server_default='balanced', nullable=False, comment='路由策略档（3 值）'),
        sa.Column('shot_count', sa.Integer(), server_default=sa.text('0'), nullable=False, comment='批次镜头数'),
        sa.Column('est_total_credits', sa.BigInteger(), server_default=sa.text('0'), nullable=False, comment='预估总积分'),
        sa.Column('est_total_seconds', sa.Integer(), server_default=sa.text('0'), nullable=False, comment='预计总耗时（秒）'),
        sa.Column('jump_used', sa.SmallInteger(), server_default=sa.text('0'), nullable=False, comment='已用免费插队次数'),
        sa.Column('warning_80_sent', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='80% 预警已发'),
        sa.Column('route_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='路由摘要'),
        sa.Column('status', sa.String(length=16), server_default='submitting', nullable=False, comment='批次状态（4 值）'),
        sa.Column('created_by', sa.BigInteger(), nullable=True, comment='提交用户 ID'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint("status IN ('submitting', 'running', 'completed', 'cancelled')", name='ck_render_batches_status'),
        sa.CheckConstraint("strategy_tier IN ('speed', 'balanced', 'quality')", name='ck_render_batches_strategy_tier'),
        sa.CheckConstraint('est_total_credits >= 0', name='ck_render_batches_est_credits'),
        sa.CheckConstraint('est_total_seconds >= 0', name='ck_render_batches_est_seconds'),
        sa.CheckConstraint('jump_used BETWEEN 0 AND 3', name='ck_render_batches_jump_used'),
        sa.CheckConstraint('shot_count >= 0', name='ck_render_batches_shot_count'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_render_batches_created_by_users'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_render_batches_project_id_projects'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_render_batches')),
        sa.UniqueConstraint('batch_key', name='uq_render_batches_batch_key'),
    )
    op.create_index('idx_render_batches_project_id_created_at', 'render_batches', ['project_id', 'created_at'], unique=False)

    op.create_table(
        'script_template_refs',
        sa.Column('project_id', sa.BigInteger(), nullable=False, comment='项目 ID'),
        sa.Column('template_id', sa.String(length=64), nullable=False, comment='模板包 ID'),
        sa.Column('template_name', sa.String(length=128), nullable=True, comment='模板名快照'),
        sa.Column('applied_params', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='参数快照'),
        sa.Column('deviated_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='偏离字段名数组'),
        sa.Column('applied_by', sa.BigInteger(), nullable=True, comment='套用用户 ID'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.ForeignKeyConstraint(['applied_by'], ['users.id'], name=op.f('fk_script_template_refs_applied_by_users'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_script_template_refs_project_id_projects'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_script_template_refs')),
        sa.UniqueConstraint('project_id', 'template_id', name='uq_script_template_refs_project_template'),
    )
    op.create_index('idx_script_template_refs_project_id', 'script_template_refs', ['project_id'], unique=False)

    op.create_table(
        'script_export_logs',
        sa.Column('user_id', sa.BigInteger(), nullable=False, comment='导出用户 ID'),
        sa.Column('project_id', sa.BigInteger(), nullable=False, comment='项目 ID'),
        sa.Column('version_id', sa.BigInteger(), nullable=True, comment='剧本版本 ID'),
        sa.Column('format', sa.String(length=16), nullable=False, comment='导出格式（3 值）'),
        sa.Column('file_name', sa.String(length=255), nullable=False, comment='产出文件名'),
        sa.Column('size_bytes', sa.BigInteger(), server_default=sa.text('0'), nullable=False, comment='文件字节数'),
        sa.Column('low_confidence_count', sa.Integer(), server_default=sa.text('0'), nullable=False, comment='待人工校正字段数'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint("format IN ('md', 'docx', 'txt')", name='ck_script_export_logs_format'),
        sa.CheckConstraint('size_bytes >= 0', name='ck_script_export_logs_size_bytes'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_script_export_logs_project_id_projects'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_script_export_logs_user_id_users'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['version_id'], ['scripts.id'], name=op.f('fk_script_export_logs_version_id_scripts'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_script_export_logs')),
    )
    op.create_index('idx_script_export_logs_project_id', 'script_export_logs', ['project_id'], unique=False)
    op.create_index('idx_script_export_logs_user_id_created_at', 'script_export_logs', ['user_id', 'created_at'], unique=False)

    op.create_table(
        'script_runs',
        sa.Column('project_id', sa.BigInteger(), nullable=False, comment='项目 ID'),
        sa.Column('script_id', sa.BigInteger(), nullable=True, comment='目标剧本版本 ID'),
        sa.Column('run_status', sa.String(length=16), server_default='generating', nullable=False, comment='运行状态（4 值）'),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True, comment='幂等键'),
        sa.Column('freeze_id', sa.BigInteger(), nullable=True, comment='关联计费冻结记录 ID'),
        sa.Column('total_episodes', sa.Integer(), nullable=False, comment='计划集数'),
        sa.Column('completed_episodes', sa.Integer(), server_default=sa.text('0'), nullable=False, comment='已完成集数'),
        sa.Column('frozen_credits', sa.BigInteger(), server_default=sa.text('0'), nullable=False, comment='冻结积分'),
        sa.Column('settled_credits', sa.BigInteger(), server_default=sa.text('0'), nullable=False, comment='已结算积分'),
        sa.Column('refunded_credits', sa.BigInteger(), server_default=sa.text('0'), nullable=False, comment='已返还积分'),
        sa.Column('plan', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='write_todos 计划'),
        sa.Column('failure_type', sa.String(length=32), nullable=True, comment='失败类型'),
        sa.Column('failure_reason', sa.Text(), nullable=True, comment='失败详情'),
        sa.Column('created_by', sa.BigInteger(), nullable=True, comment='发起用户 ID'),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True, comment='开始时间'),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True, comment='结束时间'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint("run_status IN ('generating', 'succeeded', 'failed', 'cancelled')", name='ck_script_runs_run_status'),
        sa.CheckConstraint('completed_episodes <= total_episodes', name='ck_script_runs_completed_max'),
        sa.CheckConstraint('completed_episodes >= 0', name='ck_script_runs_completed_min'),
        sa.CheckConstraint('frozen_credits >= 0', name='ck_script_runs_frozen_credits'),
        sa.CheckConstraint('refunded_credits >= 0', name='ck_script_runs_refunded_credits'),
        sa.CheckConstraint('settled_credits >= 0', name='ck_script_runs_settled_credits'),
        sa.CheckConstraint('total_episodes >= 1', name='ck_script_runs_total_episodes'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_script_runs_created_by_users'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['freeze_id'], ['credit_freezes.id'], name=op.f('fk_script_runs_freeze_id_credit_freezes'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_script_runs_project_id_projects'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['script_id'], ['scripts.id'], name=op.f('fk_script_runs_script_id_scripts'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_script_runs')),
    )
    op.create_index('idx_script_runs_project_id_created_at', 'script_runs', ['project_id', 'created_at'], unique=False)
    op.create_index('idx_script_runs_project_id_run_status', 'script_runs', ['project_id', 'run_status'], unique=False)
    op.create_index('uq_script_runs_idempotency_key', 'script_runs', ['idempotency_key'], unique=True, postgresql_where=sa.text('idempotency_key IS NOT NULL'))

    op.create_table(
        'voice_bindings',
        sa.Column('asset_id', sa.BigInteger(), nullable=False, comment='角色资产 ID'),
        sa.Column('voice_id', sa.BigInteger(), nullable=True, comment='绑定音色 ID'),
        sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='配音参数'),
        sa.Column('history', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='历史参数（≤3 版）'),
        sa.Column('preview_confirmed', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='样音是否已确认'),
        sa.Column('confirmed_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='确认时参数快照'),
        sa.Column('preview_media_id', sa.BigInteger(), nullable=True, comment='试听样音媒体文件 ID'),
        sa.Column('affected_line_count', sa.Integer(), server_default=sa.text('0'), nullable=False, comment='受影响台词数'),
        sa.Column('qc_warning', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='语速超时长质检提示'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint('asset_id > 0', name='ck_voice_bindings_asset_id'),
        sa.ForeignKeyConstraint(['asset_id'], ['assets.id'], name=op.f('fk_voice_bindings_asset_id_assets'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['preview_media_id'], ['media_files.id'], name=op.f('fk_voice_bindings_preview_media_id_media_files'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['voice_id'], ['tts_voices.id'], name=op.f('fk_voice_bindings_voice_id_tts_voices'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_voice_bindings')),
        sa.UniqueConstraint('asset_id', name='uq_voice_bindings_asset_id'),
    )
    op.create_index('idx_voice_bindings_voice_id', 'voice_bindings', ['voice_id'], unique=False)

    op.create_table(
        'director_notes',
        sa.Column('project_id', sa.BigInteger(), nullable=False, comment='项目 ID'),
        sa.Column('episode_id', sa.BigInteger(), nullable=True, comment='集 ID（NULL=全剧）'),
        sa.Column('pacing_mode', sa.String(length=16), server_default='balanced', nullable=False, comment='节奏模式（3 值）'),
        sa.Column('rhythm_curve', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='节奏曲线'),
        sa.Column('closeup_density', sa.Numeric(precision=3, scale=2), nullable=True, comment='特写密度 0~1'),
        sa.Column('dialogue_camera_rule', sa.Text(), nullable=True, comment='对话机位规则'),
        sa.Column('strategy_notes', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='其余策略说明'),
        sa.Column('template_id', sa.String(length=64), nullable=True, comment='套用模板 ID'),
        sa.Column('created_by', sa.BigInteger(), nullable=True, comment='生成用户 ID'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint("pacing_mode IN ('fast', 'balanced', 'slow')", name='ck_director_notes_pacing_mode'),
        sa.CheckConstraint('closeup_density IS NULL OR closeup_density BETWEEN 0 AND 1', name='ck_director_notes_closeup_density'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_director_notes_created_by_users'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['episode_id'], ['episodes.id'], name=op.f('fk_director_notes_episode_id_episodes'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_director_notes_project_id_projects'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_director_notes')),
        sa.UniqueConstraint('project_id', 'episode_id', name='uq_director_notes_project_id_episode_id'),
    )
    op.create_index('idx_director_notes_project_id', 'director_notes', ['project_id'], unique=False)

    op.create_table(
        'mix_profiles',
        sa.Column('project_id', sa.BigInteger(), nullable=False, comment='项目 ID'),
        sa.Column('timeline_id', sa.BigInteger(), nullable=False, comment='时间线 ID'),
        sa.Column('loudness_target', sa.Numeric(precision=4, scale=1), server_default=sa.text('-14.0'), nullable=False, comment='响度目标 LUFS'),
        sa.Column('true_peak_db', sa.Numeric(precision=3, scale=1), server_default=sa.text('-1.0'), nullable=False, comment='真峰值上限 dBTP'),
        sa.Column('normalize_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False, comment='响度标准化开关'),
        sa.Column('track_volumes', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='逐轨音量偏移'),
        sa.Column('track_muted', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='静音轨数组'),
        sa.Column('measured_lufs', sa.Numeric(precision=4, scale=1), nullable=True, comment='实测响度'),
        sa.Column('clipped', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='是否超 0dBFS'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint('loudness_target BETWEEN -30.0 AND -6.0', name='ck_mix_profiles_loudness_target'),
        sa.CheckConstraint('true_peak_db BETWEEN -6.0 AND 0.0', name='ck_mix_profiles_true_peak'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_mix_profiles_project_id_projects'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['timeline_id'], ['timelines.id'], name=op.f('fk_mix_profiles_timeline_id_timelines'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_mix_profiles')),
        sa.UniqueConstraint('timeline_id', name='uq_mix_profiles_timeline_id'),
    )
    op.create_index('idx_mix_profiles_project_id', 'mix_profiles', ['project_id'], unique=False)

    op.create_table(
        'storyboard_ops',
        sa.Column('storyboard_id', sa.BigInteger(), nullable=False, comment='分镜板 ID'),
        sa.Column('user_id', sa.BigInteger(), nullable=False, comment='操作人用户 ID'),
        sa.Column('op_history', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='操作历史数组（环形 ≤100）'),
        sa.Column('last_seq', sa.Integer(), server_default=sa.text('0'), nullable=False, comment='末步序号'),
        sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='当前镜头快照'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint('last_seq >= 0', name='ck_storyboard_ops_last_seq'),
        sa.ForeignKeyConstraint(['storyboard_id'], ['storyboards.id'], name=op.f('fk_storyboard_ops_storyboard_id_storyboards'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_storyboard_ops_user_id_users'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_storyboard_ops')),
        sa.UniqueConstraint('storyboard_id', 'user_id', name='uq_storyboard_ops_storyboard_id_user_id'),
    )
    op.create_index('idx_storyboard_ops_storyboard_id', 'storyboard_ops', ['storyboard_id'], unique=False)

    op.create_table(
        'subtitle_lines',
        sa.Column('timeline_id', sa.BigInteger(), nullable=False, comment='时间线 ID'),
        sa.Column('project_id', sa.BigInteger(), nullable=False, comment='项目 ID'),
        sa.Column('line_index', sa.Integer(), nullable=False, comment='序号'),
        sa.Column('start_ms', sa.Integer(), nullable=False, comment='起始毫秒'),
        sa.Column('end_ms', sa.Integer(), nullable=False, comment='结束毫秒'),
        sa.Column('content', sa.Text(), nullable=False, comment='字幕文本'),
        sa.Column('speaker', sa.String(length=64), nullable=True, comment='说话人'),
        sa.Column('style_ref', sa.BigInteger(), nullable=True, comment='样式模板 ID'),
        sa.Column('edited', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='是否人工编辑（V4.0）'),
        sa.Column('merged_from', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='合并来源序号数组'),
        sa.Column('out_of_safe_area', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='是否超出安全区'),
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False, comment='主键 ID'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='创建时间（UTC）'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='末次更新时间（UTC）'),
        sa.CheckConstraint('end_ms > start_ms', name='ck_subtitle_lines_time_range'),
        sa.CheckConstraint('line_index >= 1', name='ck_subtitle_lines_line_index'),
        sa.CheckConstraint('start_ms >= 0', name='ck_subtitle_lines_start_ms'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_subtitle_lines_project_id_projects'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['timeline_id'], ['timelines.id'], name=op.f('fk_subtitle_lines_timeline_id_timelines'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_subtitle_lines')),
        sa.UniqueConstraint('timeline_id', 'line_index', name='uq_subtitle_lines_timeline_id_line_index'),
    )
    op.create_index('idx_subtitle_lines_project_id', 'subtitle_lines', ['project_id'], unique=False)
    op.create_index('idx_subtitle_lines_timeline_id_start_ms', 'subtitle_lines', ['timeline_id', 'start_ms'], unique=False)

    # ---------- 2. 既有表新增字段 ----------
    op.add_column('approval_requests', sa.Column('feedback_scope', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='结构化反馈作用域（V4.0）'))

    op.add_column('asset_versions', sa.Column('variant_tag', sa.String(length=20), nullable=True, comment='变体标签（时段/天气，V4.0）'))
    op.add_column('asset_versions', sa.Column('revision_notes', sa.String(length=200), nullable=True, comment='重抽反馈原文（V4.0）'))
    op.add_column('asset_versions', sa.Column('cumulative_cost', sa.Integer(), nullable=True, comment='资产累计消耗积分（V4.0）'))
    op.create_index('idx_asset_versions_asset_id_created_at', 'asset_versions', ['asset_id', 'created_at'], unique=False)

    op.add_column('assets', sa.Column('labels', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='可检索标签数组（≤10 个，V4.0）'))
    op.add_column('assets', sa.Column('voice_status', sa.String(length=16), nullable=True, comment='音色绑定状态 bound/delisted/pending（V4.0）'))

    op.add_column('bgm_tracks', sa.Column('license_scope', sa.String(length=32), nullable=True, comment='授权范围 platform_only/distribution_ok（V4.0）'))

    op.add_column('distribution_records', sa.Column('fail_reason_user', sa.String(length=200), nullable=True, comment='失败原因用户语言文案（V4.0）'))
    op.add_column('distribution_records', sa.Column('metadata_snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='发布时元数据快照（V4.0）'))
    op.add_column('distribution_records', sa.Column('retry_count', sa.SmallInteger(), server_default=sa.text('0'), nullable=False, comment='已重试次数（V4.0）'))
    op.create_index('idx_distribution_records_project_id_status', 'distribution_records', ['project_id', 'status'], unique=False)

    op.add_column('episodes', sa.Column('hook_suggestion', sa.String(length=50), nullable=True, comment='结尾钩子建议（≤50 字，V4.0）'))
    op.add_column('episodes', sa.Column('est_duration_sec', sa.Integer(), nullable=True, comment='预估时长秒 60~180（V4.0）'))

    op.add_column('export_jobs', sa.Column('cache_key', sa.String(length=64), nullable=True, comment='转码缓存键（V4.0）'))
    op.add_column('export_jobs', sa.Column('preset_id', sa.BigInteger(), nullable=True, comment='导出预设 ID（V4.0）'))
    op.add_column('export_jobs', sa.Column('name_template', sa.String(length=100), nullable=True, comment='命名模板（V4.0）'))
    op.add_column('export_jobs', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True, comment='产物过期时间（V4.0）'))
    op.add_column('export_jobs', sa.Column('ai_label_applied', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='AI 标识已落片（V4.0）'))
    op.add_column('export_jobs', sa.Column('ai_label', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='AI 标识配置快照（V4.0）'))
    op.create_index('idx_export_jobs_expires_at', 'export_jobs', ['expires_at'], unique=False)
    op.create_index('idx_export_jobs_cache_key', 'export_jobs', ['cache_key'], unique=False, postgresql_where=sa.text('cache_key IS NOT NULL'))

    op.add_column('face_authorizations', sa.Column('status', sa.String(length=16), server_default='active', nullable=False, comment='授权状态 active/revoked（V4.0）'))
    op.add_column('face_authorizations', sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True, comment='撤回时间（V4.0）'))
    op.add_column('face_authorizations', sa.Column('revoke_reason', sa.String(length=200), nullable=True, comment='撤回原因（V4.0）'))

    op.add_column('moderation_records', sa.Column('target_type', sa.String(length=32), nullable=True, comment='被审核对象类型（含 cover，V4.0）'))
    op.add_column('moderation_records', sa.Column('script_version_id', sa.BigInteger(), nullable=True, comment='关联剧本版本 ID（V4.0）'))
    op.create_index('idx_moderation_records_script_version_id', 'moderation_records', ['script_version_id'], unique=False)

    op.add_column('render_tasks', sa.Column('priority_source', sa.String(length=16), nullable=True, comment='优先级来源（3 值，V4.0）'))
    op.add_column('render_tasks', sa.Column('manual_pass', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='人工强制合格（V4.0）'))
    op.add_column('render_tasks', sa.Column('manual_pass_reason', sa.String(length=100), nullable=True, comment='放行理由（≤100 字，V4.0）'))
    op.add_column('render_tasks', sa.Column('manual_pass_by', sa.BigInteger(), nullable=True, comment='放行操作人用户 ID（V4.0）'))
    op.add_column('render_tasks', sa.Column('manual_pass_at', sa.DateTime(timezone=True), nullable=True, comment='放行时间（V4.0）'))
    op.add_column('render_tasks', sa.Column('user_language_error', sa.String(length=200), nullable=True, comment='失败原因用户语言文案（V4.0）'))

    op.add_column('scripts', sa.Column('version_label', sa.String(length=50), nullable=True, comment='版本命名备注（V4.0）'))
    op.add_column('scripts', sa.Column('confidence_overall', sa.Numeric(precision=3, scale=2), nullable=True, comment='解析整体置信度 0.00~1.00（V4.0）'))

    op.add_column('shot_versions', sa.Column('label', sa.String(length=32), nullable=True, comment='版本预设标签（4 值，V4.0）'))
    op.add_column('shot_versions', sa.Column('note', sa.String(length=100), nullable=True, comment='版本备注（≤100 字，V4.0）'))

    op.add_column('shots', sa.Column('locked', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='镜头级锁定位（预留，V4.0）'))
    op.add_column('shots', sa.Column('upstream_changed', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='上游已变更标记（V4.0）'))
    op.add_column('shots', sa.Column('redraft_count', sa.Integer(), server_default=sa.text('0'), nullable=False, comment='静帧重出次数 ≤3（V4.0）'))

    op.add_column('subtitle_styles', sa.Column('is_custom', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='是否自定义样式（V4.0）'))
    op.add_column('subtitle_styles', sa.Column('owner_user_id', sa.BigInteger(), nullable=True, comment='所有者用户 ID（V4.0）'))
    op.create_foreign_key(op.f('fk_subtitle_styles_owner_user_id_users'), 'subtitle_styles', 'users', ['owner_user_id'], ['id'], ondelete='SET NULL')

    op.add_column('timelines', sa.Column('change_summary', postgresql.JSONB(astext_type=sa.Text()), nullable=True, comment='成片版本对比摘要（V4.0）'))

    # ``users.preferences`` 列注释对齐模型定义（历史漂移，顺手收敛）
    op.alter_column(
        'users', 'preferences',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment='用户偏好设置（新手引导 P02，JSONB 浅合并更新）',
        existing_nullable=True,
    )

    # ---------- 3. 状态 CHECK 取值扩展 ----------
    for table, name, old_expr, new_expr in _EXTENDED_CHECKS:
        op.drop_constraint(name, table, type_='check')
        op.create_check_constraint(name, table, new_expr)

    # ---------- 4. 既有表新增 CHECK ----------
    for table, name, expr in _ADDED_CHECKS:
        op.create_check_constraint(name, table, expr)


def downgrade() -> None:
    """降级：回滚 CHECK/字段/表（数据列丢失不可恢复）。"""

    # ---- 反向：既有表新增 CHECK ----
    for table, name, _expr in reversed(_ADDED_CHECKS):
        op.drop_constraint(name, table, type_='check')

    # ---- 反向：状态 CHECK 还原 ----
    for table, name, old_expr, _new_expr in reversed(_EXTENDED_CHECKS):
        op.drop_constraint(name, table, type_='check')
        op.create_check_constraint(name, table, old_expr)

    # ---- 反向：删除新增字段 ----
    op.alter_column(
        'users', 'preferences',
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        comment=None,
        existing_comment='用户偏好设置（新手引导 P02，JSONB 浅合并更新）',
        existing_nullable=True,
    )
    op.drop_column('timelines', 'change_summary')

    op.drop_constraint(op.f('fk_subtitle_styles_owner_user_id_users'), 'subtitle_styles', type_='foreignkey')
    op.drop_column('subtitle_styles', 'owner_user_id')
    op.drop_column('subtitle_styles', 'is_custom')

    op.drop_column('shots', 'redraft_count')
    op.drop_column('shots', 'upstream_changed')
    op.drop_column('shots', 'locked')

    op.drop_column('shot_versions', 'note')
    op.drop_column('shot_versions', 'label')

    op.drop_column('scripts', 'confidence_overall')
    op.drop_column('scripts', 'version_label')

    op.drop_column('render_tasks', 'user_language_error')
    op.drop_column('render_tasks', 'manual_pass_at')
    op.drop_column('render_tasks', 'manual_pass_by')
    op.drop_column('render_tasks', 'manual_pass_reason')
    op.drop_column('render_tasks', 'manual_pass')
    op.drop_column('render_tasks', 'priority_source')

    op.drop_index('idx_moderation_records_script_version_id', table_name='moderation_records')
    op.drop_column('moderation_records', 'script_version_id')
    op.drop_column('moderation_records', 'target_type')

    op.drop_column('face_authorizations', 'revoke_reason')
    op.drop_column('face_authorizations', 'revoked_at')
    op.drop_column('face_authorizations', 'status')

    op.drop_index('idx_export_jobs_cache_key', table_name='export_jobs', postgresql_where=sa.text('cache_key IS NOT NULL'))
    op.drop_index('idx_export_jobs_expires_at', table_name='export_jobs')
    op.drop_column('export_jobs', 'ai_label')
    op.drop_column('export_jobs', 'ai_label_applied')
    op.drop_column('export_jobs', 'expires_at')
    op.drop_column('export_jobs', 'name_template')
    op.drop_column('export_jobs', 'preset_id')
    op.drop_column('export_jobs', 'cache_key')

    op.drop_column('episodes', 'est_duration_sec')
    op.drop_column('episodes', 'hook_suggestion')

    op.drop_index('idx_distribution_records_project_id_status', table_name='distribution_records')
    op.drop_column('distribution_records', 'retry_count')
    op.drop_column('distribution_records', 'metadata_snapshot')
    op.drop_column('distribution_records', 'fail_reason_user')

    op.drop_column('bgm_tracks', 'license_scope')

    op.drop_column('assets', 'voice_status')
    op.drop_column('assets', 'labels')

    op.drop_index('idx_asset_versions_asset_id_created_at', table_name='asset_versions')
    op.drop_column('asset_versions', 'cumulative_cost')
    op.drop_column('asset_versions', 'revision_notes')
    op.drop_column('asset_versions', 'variant_tag')

    op.drop_column('approval_requests', 'feedback_scope')

    # ---- 反向：删除新增表（依赖序） ----
    op.drop_index('idx_subtitle_lines_timeline_id_start_ms', table_name='subtitle_lines')
    op.drop_index('idx_subtitle_lines_project_id', table_name='subtitle_lines')
    op.drop_table('subtitle_lines')

    op.drop_index('idx_storyboard_ops_storyboard_id', table_name='storyboard_ops')
    op.drop_table('storyboard_ops')

    op.drop_index('idx_mix_profiles_project_id', table_name='mix_profiles')
    op.drop_table('mix_profiles')

    op.drop_index('idx_director_notes_project_id', table_name='director_notes')
    op.drop_table('director_notes')

    op.drop_index('idx_voice_bindings_voice_id', table_name='voice_bindings')
    op.drop_table('voice_bindings')

    op.drop_index('uq_script_runs_idempotency_key', table_name='script_runs', postgresql_where=sa.text('idempotency_key IS NOT NULL'))
    op.drop_index('idx_script_runs_project_id_run_status', table_name='script_runs')
    op.drop_index('idx_script_runs_project_id_created_at', table_name='script_runs')
    op.drop_table('script_runs')

    op.drop_index('idx_script_export_logs_user_id_created_at', table_name='script_export_logs')
    op.drop_index('idx_script_export_logs_project_id', table_name='script_export_logs')
    op.drop_table('script_export_logs')

    op.drop_index('idx_script_template_refs_project_id', table_name='script_template_refs')
    op.drop_table('script_template_refs')

    op.drop_index('idx_render_batches_project_id_created_at', table_name='render_batches')
    op.drop_table('render_batches')

    op.drop_index('idx_storyboard_templates_owner_user_id', table_name='storyboard_templates')
    op.drop_table('storyboard_templates')

    op.drop_index('idx_export_presets_owner_user_id', table_name='export_presets')
    op.drop_table('export_presets')

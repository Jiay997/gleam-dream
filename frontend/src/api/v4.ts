/**
 * 文件级注释：V4.0 增量 API 客户端（对齐 backend `06_API接口文档` §22.1 与
 * `app/api/*` 实现）。
 *
 * 端点分组：
 * - 剧本域：版本列表 / 版本恢复 / 版本命名 / 导出 / 续写 / 扩写 / 生成任务与终止 /
 *   模板包列表与套用；
 * - 资产域：标签更新 / 重抽成本预估 / 批量重抽 / 场景变体 / 真人授权列表与撤回；
 * - 分镜域：导演策略（节奏模式）/ 批量改镜头 / 复制镜头 / 静帧重出 /
 *   操作历史与回退 / 分镜模板；
 * - 生成域：仪表盘 / 批量重试 / 延迟重试 / 人工标记合格 / 放弃 / 按集锁定 /
 *   插队 / 镜头版本标签；
 * - 后期域：逐角色调参 / 角色试听 / 逐条字幕 / 字幕批量样式 / BGM 替换 / 混音；
 * - 导出域：导出历史 / 参数复用 / 平台规格自检 / 导出预设 / 分发重试与撤回。
 *
 * 约定：http 拦截器已解包统一信封并统一 toast 错误，本模块只负责类型与路径。
 */
import { http } from './http'

/* ============================== 剧本域 ============================== */

/** 剧本版本摘要 */
export interface ScriptVersionSummary {
  /** 版本 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 版本号 */
  version_no: number
  /** 父版本 ID */
  parent_script_id: number | null
  /** 来源类型 */
  source_type: string
  /** 状态 */
  status: string
  /** 剧名 */
  title: string | null
  /** 梗概 */
  logline: string | null
  /** 集数 */
  total_episodes: number
  /** 字数 */
  total_words: number
  /** 版本命名备注 */
  version_label: string | null
  /** 解析整体置信度 */
  confidence_overall: number | null
  /** 是否当前版本 */
  is_current: boolean
  /** 创建时间 */
  created_at: string | null
}

/** 版本列表响应 */
export interface ScriptVersionList {
  /** 版本条目 */
  items: ScriptVersionSummary[]
  /** 总数 */
  total: number
  /** 版本上限 */
  version_limit: number
}

/** 剧本生成运行态摘要 */
export interface ScriptRunSummary {
  /** 运行 ID */
  run_id: number
  /** 运行状态 generating/succeeded/failed/cancelled */
  run_status: string
  /** 计划集数 */
  total_episodes: number
  /** 已完成集数 */
  completed_episodes: number
  /** 进度百分比 */
  progress_percent: number
  /** 冻结积分 */
  frozen_credits: number
  /** 已结算积分 */
  settled_credits: number
  /** 已返还积分 */
  refunded_credits: number
  /** 计划节点 */
  plan: Array<{ todo_id: string; node_name: string; status: string; elapsed_ms?: number }>
  /** 失败类型 */
  failure_type: string | null
  /** 失败详情 */
  failure_reason: string | null
  /** 开始时间 */
  started_at: string | null
  /** 结束时间 */
  finished_at: string | null
  /** 创建时间 */
  created_at: string | null
}

/** 剧本模板包 */
export interface ScriptTemplatePack {
  /** 模板 ID */
  template_id: string
  /** 模板名 */
  name: string
  /** 题材 */
  genre: string
  /** 画风 */
  art_style: string
  /** 默认单集字数 */
  word_target: number
  /** 默认集数 */
  episode_target: number
  /** 节奏倾向 */
  pacing: string
  /** 结局走向 */
  ending: string
  /** 适用说明 */
  description: string
  /** 是否上线 */
  is_active: boolean
}

/**
 * 查询剧本版本列表。
 * @param projectId 项目 ID
 * @returns 版本列表
 */
export function listScriptVersions(projectId: string | number): Promise<ScriptVersionList> {
  return http.get<ScriptVersionList>(`/projects/${projectId}/script/versions`)
}

/**
 * 把历史版本设为当前（生成新版本）。
 * @param projectId 项目 ID
 * @param versionId 历史版本 ID
 * @param label 新版本备注
 * @returns 新版本摘要与来源版本号
 */
export function restoreScriptVersion(
  projectId: string | number,
  versionId: string | number,
  label?: string,
): Promise<{ script: ScriptVersionSummary; restored_from: number }> {
  return http.post(`/projects/${projectId}/script/versions/${versionId}/restore`, { label: label ?? null })
}

/**
 * 写版本命名备注。
 * @param projectId 项目 ID
 * @param versionId 版本 ID
 * @param label 备注（≤50 字）
 * @returns 更新后的版本摘要
 */
export function labelScriptVersion(
  projectId: string | number,
  versionId: string | number,
  label: string,
): Promise<{ script: ScriptVersionSummary }> {
  return http.patch(`/projects/${projectId}/script/versions/${versionId}`, { label })
}

/**
 * 导出剧本为 md/docx/txt 并触发浏览器下载。
 * @param projectId 项目 ID
 * @param format 导出格式
 * @param versionId 目标版本 ID（可空=当前版本）
 * @returns 无
 */
export async function exportScript(
  projectId: string | number,
  format: 'md' | 'docx' | 'txt',
  versionId?: string | number,
): Promise<void> {
  const blob = await http.download(`/projects/${projectId}/script/export`, {
    params: { format, version_id: versionId ?? undefined },
  })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `剧本-v${versionId ?? 'current'}.${format}`
  anchor.click()
  URL.revokeObjectURL(url)
}

/**
 * 续写 N 集（原集零改动）。D31 修复：后端改为 202 后台受理，
 * 完成后发布 script.continued 事件；前端轮询版本列表感知结果。
 * @param projectId 项目 ID
 * @param count 续写集数
 * @param outline 续写要点
 * @returns 受理回执（当前版本号/集数基线，用于轮询比对）
 */
export function continueScript(
  projectId: string | number,
  count: number,
  outline: string,
): Promise<{ status: string; current_version: number; current_episodes: number; message: string }> {
  return http.post(`/projects/${projectId}/script/continue`, { count, outline })
}

/**
 * 单场扩写。
 * @param projectId 项目 ID
 * @param episodeNo 集号
 * @param sceneNo 场号
 * @param instruction 扩写指令（≤200 字）
 * @returns 变更行数与时长变化
 */
export function expandScene(
  projectId: string | number,
  episodeNo: number,
  sceneNo: number,
  instruction: string,
): Promise<{
  script: ScriptVersionSummary
  changed_lines: number
  duration_change_seconds: number
  duration_hint: string | null
}> {
  return http.post(`/projects/${projectId}/script/expand`, {
    episode_no: episodeNo,
    scene_no: sceneNo,
    instruction,
  })
}

/**
 * 查询剧本生成任务列表（进度页）。
 * @param projectId 项目 ID
 * @returns 运行态列表
 */
export function listScriptRuns(projectId: string | number): Promise<{ items: ScriptRunSummary[]; total: number }> {
  return http.get(`/projects/${projectId}/script/runs`)
}

/**
 * 终止剧本生成（保留已完成集，按集结算返还）。
 * @param projectId 项目 ID
 * @param runId 运行 ID
 * @returns 终止结果
 */
export function cancelScriptRun(
  projectId: string | number,
  runId: number,
): Promise<{
  run: ScriptRunSummary
  retained_episodes: number
  settled_credits: number
  refunded_credits: number
  project_status: string
}> {
  return http.post(`/projects/${projectId}/script/runs/${runId}/cancel`)
}

/**
 * 查询剧本组合模板包。
 * @returns 模板包列表
 */
export function listScriptPacks(): Promise<{
  items: ScriptTemplatePack[]
  total: number
  mutable_fields: string[]
}> {
  return http.get('/templates/script-packs')
}

/**
 * 套用剧本模板包（参数回填项目）。
 * @param projectId 项目 ID
 * @param templateId 模板 ID
 * @returns 套用结果（含偏离字段）
 */
export function applyScriptPack(
  projectId: string | number,
  templateId: string,
): Promise<{
  template: ScriptTemplatePack
  project_params: Record<string, unknown>
  deviated_fields: string[]
  ref_id: number
}> {
  return http.post(`/projects/${projectId}/apply-template`, {
    template_id: templateId,
    deviated_fields: [],
  })
}

/* ============================== 资产域 ============================== */

/** 重抽成本预估 */
export interface RegenEstimate {
  /** 资产 ID */
  asset_id: number
  /** 单张单价 */
  per_version_credits: number
  /** 累计消耗 */
  accumulated_credits: number
  /** 已重抽次数 */
  regen_count?: number
  /** 已重抽次数（后端字段名） */
  regenerate_count: number
  /** 成本基线 */
  cost_baseline: number
  /** 是否建议换策略 */
  suggest_change_strategy: boolean
  /** 文案提示 */
  hint: string
}

/** 批量重抽阻塞项 */
export interface BatchRegenBlocked {
  /** 资产 ID */
  asset_id: number
  /** 资产名 */
  asset_name?: string
  /** 原因 */
  reason: string
  /** 细分标记 */
  error_detail?: string
}

/**
 * 查询重抽成本预估（本次预估 + 累计消耗 + 已重抽次数）。
 * @param assetId 资产 ID
 * @returns 成本预估
 */
export function getRegenEstimate(assetId: string | number): Promise<RegenEstimate> {
  return http.get(`/assets/${assetId}/regen-estimate`)
}

/**
 * 批量重抽（confirm=false 仅返回预估）。
 * @param assetIds 资产 ID 列表
 * @param feedback 统一反馈（5~100 字）
 * @param confirm 是否确认执行
 * @param versionType 版本类型
 * @returns 预估或受理结果
 */
export function batchRegenerate(
  assetIds: Array<string | number>,
  feedback: string,
  confirm: boolean,
  versionType = 'portrait',
): Promise<{
  confirmed: boolean
  total: number
  per_asset_credits: number
  estimated_credits: number
  formula?: string
  accepted: number[]
  blocked: BatchRegenBlocked[]
  message?: string
}> {
  return http.post('/assets/batch-regenerate', {
    asset_ids: assetIds.map((id) => Number(id)),
    version_type: versionType,
    feedback,
    confirm,
  })
}

/**
 * 更新资产标签。
 * @param assetId 资产 ID
 * @param labels 标签数组（≤10 个，单个 ≤12 字）
 * @returns 更新后的资产
 */
export function updateAssetLabels(assetId: string | number, labels: string[]): Promise<Record<string, unknown>> {
  return http.patch(`/assets/${assetId}/labels`, { labels })
}

/**
 * 生成场景时段/天气变体。
 * @param assetId 场景资产 ID
 * @param variants 变体标签（同类型最多 2 项）
 * @returns 受理回执
 */
export function generateSceneVariants(
  assetId: string | number,
  variants: string[],
): Promise<{ asset_id: number; accepted_variants: string[]; estimated_credits: number }> {
  return http.post(`/assets/${assetId}/variants`, { variants })
}

/** 场景变体覆盖情况 */
export interface SceneVariantCoverage {
  /** 资产 ID */
  asset_id: number
  /** 已生成变体 */
  variants: Array<{ variant_tag: string; versions: unknown[]; is_current: boolean }>
  /** 未生成变体 */
  missing: string[]
  /** 可用标签 */
  available_tags: { tod: string[]; weather: string[] }
}

/**
 * 查询场景变体覆盖与缺失。
 * @param assetId 场景资产 ID
 * @returns 变体覆盖情况
 */
export function listSceneVariants(assetId: string | number): Promise<SceneVariantCoverage> {
  return http.get(`/assets/${assetId}/variants`)
}

/** 真人授权记录 */
export interface FaceAuthorizationRecord {
  /** 授权 ID */
  id: number
  /** 资产 ID */
  asset_id: number | null
  /** 资产名 */
  asset_name: string | null
  /** 项目 ID */
  project_id: number | null
  /** 授权类型 */
  authorization_type: string
  /** 协议版本 */
  protocol_version: string
  /** 状态 active/revoked */
  status: string
  /** 撤回时间 */
  revoked_at: string | null
  /** 撤回原因 */
  revoke_reason: string | null
  /** IP */
  ip: string | null
  /** 授权时间 */
  authorized_at: string | null
}

/**
 * 查询我的真人授权记录。
 * @param params 筛选（项目 / 状态 / 分页）
 * @returns 分页列表
 */
export function listFaceAuthorizations(params?: {
  projectId?: string | number
  status?: string
  page?: number
  pageSize?: number
}): Promise<{ items: FaceAuthorizationRecord[]; page: number; page_size: number; total: number }> {
  return http.get('/face-authorizations', {
    params: {
      project_id: params?.projectId,
      status: params?.status,
      page: params?.page ?? 1,
      page_size: params?.pageSize ?? 20,
    },
  })
}

/**
 * 撤回真人授权（须输入角色名二次确认）。
 * @param authorizationId 授权记录 ID
 * @param confirmName 角色名（须与资产名一致）
 * @param reason 撤回原因
 * @returns 撤回结果
 */
export function revokeFaceAuthorization(
  authorizationId: number,
  confirmName: string,
  reason?: string,
): Promise<{ authorization: FaceAuthorizationRecord; shots_blocked: boolean }> {
  return http.post(`/face-authorizations/${authorizationId}/revoke`, {
    confirm_name: confirmName,
    reason: reason ?? null,
  })
}

/* ============================== 分镜域 ============================== */

/** 节奏模式档位 */
export type PacingMode = 'fast' | 'balanced' | 'slow'

/** 导演策略 */
export interface DirectorNote {
  /** 策略 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 集 ID（null=全剧） */
  episode_id: number | null
  /** 节奏模式 */
  pacing_mode: PacingMode
  /** 特写密度 */
  closeup_density: number | null
  /** 对话机位规则 */
  dialogue_camera_rule: string | null
  /** 其余策略说明 */
  strategy_notes: Record<string, unknown> | null
  /** 套用模板 ID */
  template_id: string | null
}

/** 导演策略集合 */
export interface DirectorNotesPayload {
  /** 全剧策略 */
  project_note: DirectorNote | null
  /** 按集策略 */
  episode_notes: DirectorNote[]
  /** 题材推荐节奏 */
  recommended_pacing: PacingMode
  /** 可用模式 */
  available_modes: Array<{
    value: PacingMode
    label: string
    hint: string
    shot_duration_min: number
    shot_duration_max: number
    closeup_density: number
  }>
}

/**
 * 查询导演策略（全剧 + 按集）。
 * @param projectId 项目 ID
 * @returns 策略集合
 */
export function getDirectorNotes(projectId: string | number): Promise<DirectorNotesPayload> {
  return http.get(`/projects/${projectId}/director-notes`)
}

/**
 * 写入节奏模式等导演策略。
 * @param projectId 项目 ID
 * @param payload 策略（pacing_mode 必填）
 * @returns 写入结果
 */
export function putDirectorNote(
  projectId: string | number,
  payload: {
    pacing_mode: PacingMode
    episode_no?: number | null
    closeup_density?: number | null
    dialogue_camera_rule?: string | null
    template_id?: string | null
  },
): Promise<{
  note: DirectorNote
  pacing_profile: { label: string; hint: string }
  protected_manual_episode: boolean
  message: string
}> {
  return http.put(`/projects/${projectId}/director-notes`, payload)
}

/**
 * 批量修改镜头参数（confirm=false 仅返回摘要）。
 * @param shotIds 镜头 ID 列表
 * @param changes 待改字段
 * @param confirm 是否确认执行
 * @returns 摘要或执行结果
 */
export function batchPatchShots(
  shotIds: Array<string | number>,
  changes: {
    camera_move?: string
    duration_ms?: number
    emotion_tag?: string
    shot_size?: string
  },
  confirm: boolean,
): Promise<{
  confirmed: boolean
  affected: number
  changes: Array<{ field: string; from: unknown; to: unknown }>
  qc_flags?: Array<{ shot_id: number; shot_no: number; issue: string; message: string }>
  op_seq?: number
  message?: string
}> {
  return http.patch('/shots/batch', { shot_ids: shotIds.map((id) => Number(id)), ...changes, confirm })
}

/**
 * 复制镜头。
 * @param shotId 源镜头 ID
 * @param mode 粘贴位置 after/tail
 * @param anchorShotId after 模式下的锚点镜头
 * @returns 新镜头
 */
export function copyShot(
  shotId: string | number,
  mode: 'after' | 'tail' = 'tail',
  anchorShotId?: number,
): Promise<{ new_shot: Record<string, unknown>; order_index: number; op_seq: number }> {
  return http.post(`/shots/${shotId}/copy`, { mode, anchor_shot_id: anchorShotId ?? null })
}

/**
 * 重出镜头首帧（默认本地合成整幅要素画面）。
 * @param shotId 镜头 ID
 * @param feedback 反馈（≤50 字）
 * @param mode compose=本地合成（0 成本）；generate=文生图（≤3 次）
 * @returns 重出结果
 */
export function redrawShotFrame(
  shotId: string | number,
  feedback?: string,
  mode: 'compose' | 'generate' = 'compose',
): Promise<{
  shot_id: number
  mode: 'compose' | 'generate'
  redraft_count: number
  remaining: number
  media_url: string | null
  degraded: string[]
  elements: {
    setting: { name?: string | null; present: boolean } | null
    subjects: string[]
    props: string[]
  } | null
  suggest_change_prompt?: boolean
}> {
  return http.patch(`/shots/${shotId}/frame/redraw`, {
    feedback: feedback ?? null,
    mode,
  })
}

/**
 * 批量合成整板整幅要素首帧。
 * @param storyboardId 分镜板 ID
 * @param shotIds 指定镜头（空=整板）
 * @returns 批量结果
 */
export function composeStoryboardFrames(
  storyboardId: string | number,
  shotIds?: number[],
): Promise<{
  total: number
  succeeded: number
  failed: number
  results: Array<{
    shot_id: number
    shot_no: number
    status: 'ok' | 'failed'
    media_url: string | null
    degraded: string[]
    error: string | null
  }>
}> {
  return http.post(`/storyboards/${storyboardId}/frames/compose`, {
    shot_ids: shotIds ?? null,
  })
}

/**
 * 获取整板画面 QC 门禁结果。
 * @param storyboardId 分镜板 ID
 * @returns 门禁结果
 */
export function getStoryboardFramesQc(storyboardId: string | number): Promise<{
  passed: boolean
  checks: Record<string, { ratio?: number; value?: number; threshold: number; shots?: number[] }>
  issues: Array<{ code: string; shot_no: number | null; message: string; severity: string }>
  items: Array<{ shot_id: number; shot_no: number; frame_media_id: number | null; degraded: string[] }>
}> {
  return http.get(`/storyboards/${storyboardId}/frames/qc`)
}

/** 画布操作步骤 */
export interface StoryboardOpStep {
  /** 步骤序号 */
  seq: number
  /** 操作类型 */
  op_type: string
  /** 受影响镜头 */
  target_shot_ids: number[]
  /** 时间 */
  at: string
}

/**
 * 查询画布操作历史。
 * @param storyboardId 分镜板 ID
 * @returns 操作历史
 */
export function getStoryboardOps(storyboardId: string | number): Promise<{
  storyboard_id: number
  last_seq: number
  items: StoryboardOpStep[]
}> {
  return http.get('/storyboard/ops-history', { params: { storyboard_id: storyboardId } })
}

/**
 * 回退到指定操作步骤。
 * @param storyboardId 分镜板 ID
 * @param seq 目标步骤
 * @param confirm 是否确认
 * @returns 回退结果
 */
export function rollbackStoryboard(
  storyboardId: string | number,
  seq: number,
  confirm: boolean,
): Promise<{
  confirmed: boolean
  target_seq: number
  restored_shots?: number
  truncated_steps: number
  message?: string
}> {
  return http.post('/storyboard/rollback', { storyboard_id: Number(storyboardId), seq, confirm })
}

/** 分镜模板 */
export interface StoryboardTemplateInfo {
  /** 模板 ID */
  template_id: string
  /** 名称 */
  name: string
  /** 类型 */
  kind: string
  /** 机位规则 */
  rules: string[]
  /** 说明 */
  description: string | null
  /** 是否内置 */
  is_builtin: boolean
}

/**
 * 查询内置 + 自定义分镜模板。
 * @returns 模板集合
 */
export function listStoryboardTemplates(): Promise<{
  builtin: StoryboardTemplateInfo[]
  custom: StoryboardTemplateInfo[]
  custom_limit: number
}> {
  return http.get('/storyboard-templates')
}

/**
 * 保存自定义分镜模板（规则 ≤10 条，每条 ≤50 字）。
 * @param payload 模板内容
 * @returns 保存结果
 */
export function saveStoryboardTemplate(payload: {
  name: string
  kind?: string
  rules: string[]
  description?: string | null
}): Promise<{ template: StoryboardTemplateInfo; total: number }> {
  return http.post('/storyboard-templates', payload)
}

/* ============================== 生成域 ============================== */

/** 生成仪表盘 */
export interface RenderDashboard {
  /** 六态计数 */
  counts: Record<string, number>
  /** 任务总数 */
  total: number
  /** 总进度百分比 */
  progress_percent: number
  /** 预计剩余秒数 */
  eta_seconds: number | null
  /** 速率是否波动大 */
  eta_volatile: boolean
  /** ETA 提示 */
  eta_hint: string
  /** 实时消耗积分 */
  consumed_credits: number
  /** 冻结积分 */
  frozen_credits: number
  /** 可用积分 */
  available_credits: number
  /** 每分钟完成速率 */
  rate_per_minute: number
  /** 估算窗口（分钟） */
  window_minutes: number
}

/**
 * 查询生成总览仪表盘。
 * @param projectId 项目 ID
 * @returns 仪表盘数据
 */
export function getRenderDashboard(projectId: string | number): Promise<RenderDashboard> {
  return http.get(`/projects/${projectId}/render-dashboard`)
}

/**
 * 批量重试失败镜头（delay_minutes>0 为延迟重试）。
 * @param taskIds 任务 ID 列表
 * @param delayMinutes 延迟分钟数
 * @returns 受理结果
 */
export function batchRetryRender(
  taskIds: number[],
  delayMinutes = 0,
): Promise<{
  accepted: number[]
  skipped: BatchRegenBlocked[] | Array<{ task_id: number; reason: string; error_detail?: string }>
  delayed_task_ids: number[]
  delay_minutes: number
  message?: string
}> {
  return http.post('/render-tasks/batch-retry', { task_ids: taskIds, delay_minutes: delayMinutes })
}

/**
 * 人工标记合格（强制通过质检）。
 * @param taskId 渲染任务 ID
 * @param reason 放行理由（≥5 字）
 * @returns 通过结果
 */
export function manualPassTask(
  taskId: number,
  reason: string,
): Promise<{
  task_id: number
  status: string
  manual_pass: boolean
  manual_pass_reason: string
  settled_credits: number
  shot_status: string | null
}> {
  return http.post(`/render-tasks/${taskId}/manual-pass`, { reason, acknowledged: true })
}

/**
 * 放弃失败/待人工任务并全额返还。
 * @param taskId 渲染任务 ID
 * @returns 放弃结果
 */
export function abandonRenderTask(
  taskId: number,
): Promise<{ task_id: number; status: string; refunded_credits: number; shot_status: string | null }> {
  return http.post(`/render-tasks/${taskId}/abandon`)
}

/**
 * 按集一键锁定全部镜头版本。
 * @param episodeId 集 ID
 * @param selections 逐镜头版本选择（可空=自动最优版）
 * @returns 锁定结果
 */
export function lockEpisodeVersions(
  episodeId: number,
  selections: Record<string, number> = {},
): Promise<{
  episode_id: number
  locked: Array<{ shot_id: number; version_no: number }>
  skipped?: Array<{ shot_id: number; shot_no: number; reason: string }>
  exceptions: unknown[]
}> {
  return http.post(`/episodes/${episodeId}/lock-versions`, { selections })
}

/**
 * 单镜头插队（每批次免费 3 次）。
 * @param batchKey 批次键
 * @param taskId 渲染任务 ID
 * @returns 插队结果
 */
export function jumpRenderQueue(
  batchKey: string,
  taskId: number,
): Promise<{ task_id: number; priority: number; priority_source: string; jump_used: number; free_quota: number }> {
  return http.post(`/render-queues/${batchKey}/jump`, { task_id: taskId })
}

/**
 * 给镜头版本打标签/备注。
 * @param versionId 版本 ID
 * @param label 预设标签
 * @param note 备注（≤100 字）
 * @returns 更新结果
 */
export function labelShotVersion(
  versionId: number,
  label: string | null,
  note: string | null,
): Promise<{ version_id: number; label: string | null; note: string | null }> {
  return http.patch(`/shot-versions/${versionId}`, { label, note })
}

/** 用户语言失败详情 */
export interface RenderFailureDetail {
  /** 用户语言文案 */
  user_message: string
  /** 失败类型 */
  failure_type: string
  /** 原始错误码 */
  error_code: string | null
  /** 原始失败详情 */
  raw_reason: string | null
  /** 是否可重试 */
  retryable: boolean
}

/* ============================== 后期域 ============================== */

/** 逐角色配音参数 */
export interface VoiceParams {
  /** 语速 0.80~1.50 */
  rate: number
  /** 语调偏移 ±3 */
  pitch_shift: number
  /** 情绪按场覆盖 */
  emotion_override: Array<Record<string, unknown>>
}

/** 配音绑定 */
export interface VoiceBindingInfo {
  /** 绑定 ID */
  id: number
  /** 角色资产 ID */
  asset_id: number
  /** 音色 ID */
  voice_id: number | null
  /** 当前参数 */
  params: VoiceParams | Record<string, never>
  /** 历史版本数 */
  history_count: number
  /** 样音是否已确认 */
  preview_confirmed: boolean
  /** 确认快照 */
  confirmed_snapshot: Record<string, unknown> | null
  /** 受影响台词数 */
  affected_line_count: number
  /** 语速超时长质检提示 */
  qc_warning: Record<string, unknown> | null
}

/**
 * 逐角色调整语速/语调。
 * @param characterId 角色资产 ID
 * @param params 参数
 * @returns 绑定与质检提示
 */
export function patchVoiceParams(
  characterId: string | number,
  params: VoiceParams,
): Promise<{ binding: VoiceBindingInfo; qc_warning: Record<string, unknown> | null; affected_line_count: number }> {
  return http.patch(`/voices/${characterId}/params`, params)
}

/**
 * 重新试听该角色（仅该角色台词重合成）。
 * @param characterId 角色资产 ID
 * @returns 试听结果
 */
export function previewVoice(
  characterId: string | number,
): Promise<{ binding: VoiceBindingInfo; preview_media_url: string | null; affected_line_count: number }> {
  return http.post(`/voices/${characterId}/preview`)
}

/** 逐条字幕 */
export interface SubtitleLineItem {
  /** 字幕行 ID */
  id: number
  /** 时间线 ID */
  timeline_id: number
  /** 序号 */
  line_index: number
  /** 起始毫秒 */
  start_ms: number
  /** 结束毫秒 */
  end_ms: number
  /** 文本 */
  text: string
  /** 说话人 */
  speaker: string | null
  /** 样式引用 */
  style_ref: number | null
  /** 是否人工编辑 */
  edited: boolean
  /** 是否超出安全区 */
  out_of_safe_area: boolean
}

/**
 * 查询逐条字幕。
 * @param projectId 项目 ID
 * @param cutType 版本类型
 * @returns 字幕行列表
 */
export function listSubtitleLines(
  projectId: string | number,
  cutType = 'preview',
): Promise<{ timeline_id: number; items: SubtitleLineItem[]; total: number; max_chars: number }> {
  return http.get(`/projects/${projectId}/post/subtitle-lines`, { params: { cut_type: cutType } })
}

/**
 * 逐条编辑字幕（改文本 / 出入点微调）。
 * @param lineId 字幕行 ID
 * @param payload 待改字段
 * @returns 更新结果
 */
export function patchSubtitleLine(
  lineId: number,
  payload: { text?: string; start_ms?: number; end_ms?: number },
): Promise<{ line: SubtitleLineItem; sync_error_ms: number }> {
  return http.patch(`/subtitles/${lineId}`, payload)
}

/**
 * 批量修改字幕样式。
 * @param payload 样式与范围
 * @returns 应用结果
 */
export function batchStyleSubtitles(payload: {
  style_id: number
  scope: 'all' | 'episode' | 'speaker'
  speaker?: string | null
}): Promise<{
  timeline_id: number
  style_id: number
  scope: string
  applied: number
  out_of_safe_area: Array<Record<string, unknown>>
}> {
  return http.post('/subtitles/batch-style', payload)
}

/**
 * BGM 手动替换（含授权范围与临期提示）。
 * @param timelineId 时间线 ID
 * @param bgmTrackId 目标曲目 ID
 * @param volumeDb 音量（可空）
 * @returns 替换结果
 */
export function replaceBgm(
  timelineId: number,
  bgmTrackId: number,
  volumeDb?: number,
): Promise<{
  timeline_id: number
  bgm: Record<string, unknown>
  license_scope: string | null
  license_warning: string | null
  distribution_blocked: boolean
}> {
  return http.post(`/timeline/${timelineId}/bgm/replace`, {
    bgm_track_id: bgmTrackId,
    volume_db: volumeDb ?? null,
  })
}

/** 混音画像 */
export interface MixProfileInfo {
  /** 画像 ID */
  id: number
  /** 时间线 ID */
  timeline_id: number
  /** 响度目标 LUFS */
  loudness_target: number
  /** 真峰值 dBTP */
  true_peak_db: number
  /** 是否启用标准化 */
  normalize_enabled: boolean
  /** 逐轨音量偏移 */
  track_volumes: Record<string, number>
  /** 静音轨 */
  track_muted: string[]
  /** 实测响度 */
  measured_lufs: number | null
  /** 是否超 0dBFS */
  clipped: boolean
  /** 可调范围 */
  volume_range_db: number
}

/**
 * 查询混音画像。
 * @param projectId 项目 ID
 * @param cutType 版本类型
 * @returns 混音画像
 */
export function getMixProfile(projectId: string | number, cutType = 'preview'): Promise<{ profile: MixProfileInfo }> {
  return http.get(`/projects/${projectId}/post/mix-profile`, { params: { cut_type: cutType } })
}

/**
 * 更新混音画像（逐轨音量 / 响度目标）。
 * @param projectId 项目 ID
 * @param payload 待改字段
 * @returns 更新结果
 */
export function updateMixProfile(
  projectId: string | number,
  payload: {
    track_volumes?: Record<string, number>
    track_muted?: string[]
    loudness_target?: number
    normalize_enabled?: boolean
  },
): Promise<{ profile: MixProfileInfo; clipped_tracks: string[] }> {
  return http.put(`/projects/${projectId}/post/mix-profile`, payload)
}

/* ============================== 导出域 ============================== */

/** AI 标识配置 */
export interface AiLabelConfig {
  /** 显式标识 */
  visible: {
    enabled: boolean
    text: string
    position: string
    style: Record<string, unknown>
    closable: boolean
  }
  /** 隐式标识 */
  implicit: { metadata: Record<string, unknown> }
}

/** 导出历史项 */
export interface ExportHistoryItem {
  /** 任务 ID */
  id: number
  /** 分辨率 */
  resolution: string
  /** 是否带水印 */
  has_watermark: boolean
  /** 状态 */
  status: string
  /** 进度 */
  progress: number
  /** 集号 */
  episode_no: number | null
  /** 参数摘要 */
  parameter_summary: string
  /** 文件是否可下载 */
  file_available: boolean
  /** 转码缓存键 */
  cache_key: string | null
  /** AI 标识是否落片 */
  ai_label_applied: boolean
  /** AI 标识配置 */
  ai_label: AiLabelConfig | null
  /** 过期时间 */
  expires_at: string | null
  /** 创建时间 */
  created_at: string | null
  /** 产物 */
  output: { download_url?: string } | null
}

/**
 * 查询导出历史。
 * @param projectId 项目 ID
 * @returns 历史列表
 */
export function listExportHistory(projectId: string | number): Promise<{
  items: ExportHistoryItem[]
  total: number
  retention_days: number
}> {
  return http.get(`/projects/${projectId}/export-jobs`, { params: { history: 1 } })
}

/**
 * 复用历史参数重新导出。
 * @param jobId 历史任务 ID
 * @param payload 覆写参数
 * @returns 新任务与缓存命中信息
 */
export function reuseExportJob(
  jobId: number,
  payload: { resolution?: string; name_template?: string } = {},
): Promise<{
  export_job: ExportHistoryItem
  reused_from: number
  cache_hit: boolean
  latest_timeline_changed: boolean
  message: string
}> {
  return http.post(`/export-jobs/${jobId}/reuse`, payload)
}

/**
 * 目标平台规格自检。
 * @param projectId 项目 ID
 * @param payload 平台与分辨率
 * @returns 自检结论
 */
export function prespecCheck(
  projectId: string | number,
  payload: { platform: string; resolution?: string; episode_no?: number | null },
): Promise<{
  platform: string
  platform_label: string
  rules: Record<string, unknown>
  checks: Array<{ item: string; ok: boolean; blocking: boolean; detail: string }>
  blocked: boolean
  blocked_items: string[]
  recommendations: string[]
  degraded: boolean
  degraded_hint: string | null
}> {
  return http.post('/export-jobs/prespec-check', payload, { params: { project_id: projectId } })
}

/** 导出预设 */
export interface ExportPresetInfo {
  /** 预设 ID */
  id: number
  /** 名称 */
  name: string
  /** 参数 */
  params: Record<string, unknown>
  /** 是否默认 */
  is_default: boolean
  /** 创建时间 */
  created_at: string | null
}

/**
 * 列出导出预设。
 * @returns 预设列表
 */
export function listExportPresets(): Promise<{ items: ExportPresetInfo[]; total: number; limit: number }> {
  return http.get('/export-presets')
}

/**
 * 保存导出预设（同名覆盖）。
 * @param payload 预设内容
 * @returns 保存结果
 */
export function saveExportPreset(payload: {
  name: string
  resolution?: string
  aspect?: string
  watermark?: boolean
  name_template?: string | null
  is_default?: boolean
}): Promise<{ preset: ExportPresetInfo; total: number }> {
  return http.post('/export-presets', payload)
}

/**
 * 删除导出预设。
 * @param presetId 预设 ID
 * @returns 删除结果
 */
export function deleteExportPreset(presetId: number): Promise<{ preset_id: number; deleted: boolean }> {
  return http.delete(`/export-presets/${presetId}`)
}

/** 分发记录（V4 扩展字段） */
export interface DistributionRecordInfo {
  /** 记录 ID */
  id: number
  /** 平台 */
  platform: string
  /** 平台视频 ID */
  external_video_id: string | null
  /** 发布链接 */
  publish_url: string | null
  /** 标题 */
  title: string | null
  /** 状态 */
  status: string
  /** 状态中文 */
  status_label: string
  /** 用户语言失败原因 */
  fail_reason_user: string | null
  /** 元数据快照 */
  metadata_snapshot: Record<string, unknown> | null
  /** 重试次数 */
  retry_count: number
  /** 是否可重试 */
  retryable: boolean
  /** 分发时间 */
  distributed_at: string | null
}

/**
 * 分发重试。
 * @param recordId 分发记录 ID
 * @returns 重试结果
 */
export function retryDistribution(
  recordId: number,
): Promise<{ record: DistributionRecordInfo; need_reauth: boolean; retry_count: number; message: string }> {
  return http.post(`/distributions/${recordId}/retry`)
}

/**
 * 分发撤回（依平台能力）。
 * @param recordId 分发记录 ID
 * @returns 撤回结果
 */
export function withdrawDistribution(
  recordId: number,
): Promise<{ record: DistributionRecordInfo; platform_capable: boolean; message: string }> {
  return http.post(`/distributions/${recordId}/withdraw`)
}

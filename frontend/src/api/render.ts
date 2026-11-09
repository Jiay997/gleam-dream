/**
 * 文件级注释：渲染 / 生成任务域 API（与 backend/app/api/render.py 签名对齐，Task 15）。
 * 端点清单：
 * - GET  /projects/{project_id}/render/estimate?shot_ids=   渲染成本估算（空=全部锁定镜头）；
 * - POST /projects/{project_id}/render-tasks/batch          批量提交渲染（202 受理，批次幂等回放 200）；
 * - GET  /projects/{project_id}/render-tasks                任务分页列表（含镜头摘要 / 成本 / failure_type）；
 * - GET  /render-tasks/{task_id}                            任务详情（outputs / qc_reports / 冻结结算）；
 * - POST /render-tasks/{task_id}/cancel                     取消（仅 pending/queued；全额退款）；
 * - POST /render-tasks/{task_id}/retry                      重试（failed/manual_required/cancelled → queued，202）；
 * - POST /shots/{shot_id}/regenerate                        单镜重新生成（202，旧任务保留）。
 * SSE 事件流 GET /projects/{project_id}/render-tasks/events 由视图层 useSSE 拼接
 * （EventSource 不支持请求头鉴权，token 走 access_token 查询参数）。
 * 说明：后端列表接口不返回 model_key / started_at / finished_at（仅详情接口携带），
 * 列表表格以 task_type 展示类型，模型与耗时在详情抽屉呈现。
 */
import { http } from './http'

/** 任务类型 → 中文标签映射（render_tasks.task_type 9 值） */
export const RENDER_TASK_TYPE_LABELS: Record<string, string> = {
  text_to_image: '文生图',
  image_to_image: '图生图',
  text_to_video: '文生视频',
  image_to_video: '图生视频',
  tts: '配音合成',
  bgm: 'BGM 生成',
  subtitle: '字幕生成',
  compose: '成片合成',
  upscale: '画质增强',
}

/** 失败类型 → 中文标签映射（render_tasks.failure_type 6 值） */
export const FAILURE_TYPE_LABELS: Record<string, string> = {
  timeout: '超时',
  rate_limited: '限流',
  moderation_rejected: '审核拦截',
  param_invalid: '参数错误',
  model_unavailable: '模型不可用',
  unknown: '未知错误',
}

/** 任务状态筛选选项（render_tasks.status 9 值；空串 = 全部） */
export const RENDER_STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '待处理' },
  { value: 'queued', label: '排队中' },
  { value: 'running', label: '生成中' },
  { value: 'qc_review', label: '质检审核中' },
  { value: 'retrying', label: '重试中' },
  { value: 'manual_required', label: '需人工介入' },
  { value: 'succeeded', label: '已成功' },
  { value: 'failed', label: '已失败' },
  { value: 'cancelled', label: '已取消' },
]

/** 镜头摘要（列表 / 详情内嵌 shot 字段，后端 _shot_summary 形状） */
export interface RenderShotSummary {
  /** 镜头 ID */
  id: number
  /** 镜号 */
  shot_no: number
  /** 轨内顺序 */
  order_index: number
  /** 时长（毫秒） */
  duration_ms: number
  /** 镜头状态 */
  status: string
  /** 最终提示词 */
  final_prompt: string | null
}

/** 渲染任务列表项（后端 list_tasks 输出形状） */
export interface RenderTaskListItem {
  /** 任务 ID */
  id: number
  /** 关联镜头 ID */
  shot_id: number | null
  /** 任务类型（9 值） */
  task_type: string
  /** 任务状态（9 值） */
  status: string
  /** 优先级 1~9 */
  priority: number
  /** 已重试次数 */
  retry_count: number
  /** 预估成本（积分） */
  estimated_cost_credits: number
  /** 实际成本（积分，未结算为 null） */
  actual_cost_credits: number | null
  /** 失败类型（6 值，可空） */
  failure_type: string | null
  /** 失败详情 */
  failure_reason: string | null
  /** 创建时间 */
  created_at: string | null
  /** 内嵌镜头摘要（可空） */
  shot: RenderShotSummary | null
}

/** 任务分页响应 */
export interface RenderTaskPage {
  /** 当前页数据 */
  items: RenderTaskListItem[]
  /** 页码 */
  page: number
  /** 页大小 */
  page_size: number
  /** 总条数 */
  total: number
}

/** 任务列表查询参数 */
export interface ListRenderTasksParams {
  /** 状态筛选（缺省全部） */
  status?: string
  /** 页码（≥1） */
  page?: number
  /** 页大小（1~100） */
  page_size?: number
  /** 排序（-created_at / created_at） */
  sort?: string
}

/** 任务详情内嵌 QC 报告（任务创建时间之后的轮次） */
export interface RenderTaskQcReport {
  /** 报告 ID */
  id: number
  /** 结果（passed/warning/failed） */
  result: string
  /** 一致性得分 0~1（可空） */
  consistency_score: number | null
  /** 轮次 */
  round_no: number
  /** 明细 JSONB */
  detail: Record<string, unknown> | null
  /** 创建时间 */
  created_at: string | null
}

/** 任务详情内嵌渲染产物 */
export interface RenderTaskOutput {
  /** 产物 ID */
  id: number
  /** 产物类型（video/image/audio/subtitle） */
  output_type: string
  /** 同任务内序号 */
  output_index: number
  /** 媒体文件 ID（可空） */
  media_file_id: number | null
  /** 关联 QC 报告 ID（可空） */
  qc_report_id: number | null
  /** 是否被选中采用 */
  is_selected: boolean
  /** 产物可播放地址（后端媒体 URL，可空） */
  media_url: string | null
  /** 产物 MIME 类型（video/mp4、application/json 等） */
  mime_type: string | null
  /** 产物时长（毫秒，可空） */
  duration_ms: number | null
}

/** 任务详情内嵌冻结 / 结算信息 */
export interface RenderFreezeInfo {
  /** 冻结记录 ID */
  freeze_id: number
  /** 冻结状态（frozen/settled/released 等） */
  status: string
  /** 冻结积分 */
  frozen_credits: number
  /** 已结算积分（可空） */
  settled_credits: number | null
  /** 已释放积分（released 时等于冻结额） */
  released_credits: number | null
  /** 冻结过期时刻 */
  expires_at: string | null
}

/** 渲染任务详情（后端 get_task_detail 输出形状） */
export interface RenderTaskDetail {
  /** 任务 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 关联镜头 ID */
  shot_id: number | null
  /** 任务类型 */
  task_type: string
  /** 任务状态 */
  status: string
  /** 优先级 */
  priority: number
  /** 选用模型键（可空，仅详情携带） */
  model_key: string | null
  /** 已重试次数 */
  retry_count: number
  /** 最大重试次数 */
  max_retries: number
  /** 预估成本（积分） */
  estimated_cost_credits: number
  /** 实际成本（积分，可空） */
  actual_cost_credits: number | null
  /** 已冻结积分 */
  frozen_credits: number
  /** 入参哈希 */
  input_hash: string | null
  /** 失败类型 */
  failure_type: string | null
  /** 失败详情 */
  failure_reason: string | null
  /** 是否降级占位产物（P0-3：true 时实际成本为 0） */
  degraded?: boolean
  /** 绑定配音片段 ID 列表（P2-10） */
  voice_clip_ids?: number[]
  /** 绑定台词 ID 列表（P2-10） */
  voice_line_ids?: number[]
  /** 台词总跨度毫秒（P2-10） */
  line_duration_ms?: number | null
  /** 镜头时长与台词跨度是否超容差（P2-10） */
  duration_mismatch?: boolean | null
  /** 入队时间 */
  queued_at: string | null
  /** 开始时间 */
  started_at: string | null
  /** 结束时间 */
  finished_at: string | null
  /** 创建时间 */
  created_at: string | null
  /** 内嵌镜头摘要 */
  shot: RenderShotSummary | null
  /** 渲染产物列表 */
  outputs: RenderTaskOutput[]
  /** QC 报告列表 */
  qc_reports: RenderTaskQcReport[]
  /** 冻结 / 结算信息（可空） */
  freeze: RenderFreezeInfo | null
}

/** 单镜头成本估算项 */
export interface RenderEstimateItem {
  /** 镜头 ID */
  shot_id: number
  /** 预估成本（积分） */
  estimated_credits: number
}

/** 渲染成本估算响应 */
export interface RenderEstimateData {
  /** 逐镜头估算列表 */
  per_shot: RenderEstimateItem[]
  /** 合计预估成本（积分） */
  total_credits: number
  /** 参与估算镜头数 */
  shot_count: number
  /** 当前可用积分（balance - frozen） */
  available_credits: number
  /** 项目算力预算（0 = 不限额，P2-12） */
  budget_credits: number
  /** 项目已结算消耗（P2-12） */
  spent_credits: number
  /** 项目在途冻结（P2-12） */
  frozen_credits: number
  /** 项目剩余预算（-1 = 不限额，P2-12） */
  remaining_budget: number
}

/** 批量提交结果中的单任务摘要 */
export interface BatchRenderTaskRef {
  /** 任务 ID */
  id: number
  /** 镜头 ID */
  shot_id: number | null
  /** 任务状态（新建恒为 queued） */
  status: string
}

/** 批量提交渲染响应 */
export interface BatchRenderData {
  /** 任务摘要列表 */
  tasks: BatchRenderTaskRef[]
  /** 本次冻结积分总额 */
  frozen_credits: number
  /** 预估积分总额 */
  estimated_credits: number
  /** 跳过镜头提示 */
  warnings: string[]
  /** 是否幂等回放（同 Idempotency-Key 重复提交） */
  idempotent?: boolean
}

/**
 * 渲染成本估算（shot_ids 为空 = 全部锁定镜头）。
 * @param projectId 项目 ID
 * @param shotIds 镜头 ID 列表（可空）
 * @returns 估算结果（逐镜 / 合计 / 可用余额）
 * @throws ApiError 60001 无可用锁定镜头时由拦截器统一提示
 */
export function estimateRender(projectId: string, shotIds?: number[]): Promise<RenderEstimateData> {
  const shot_ids = shotIds && shotIds.length > 0 ? shotIds.join(',') : undefined
  return http.get<RenderEstimateData>(`/projects/${projectId}/render/estimate`, {
    params: shot_ids ? { shot_ids } : undefined,
  })
}

/**
 * 批量提交渲染任务（202 受理；写请求自动携带幂等键头）。
 * Q-016：批量提交在后端同步完成首帧合成，耗时可达数十秒，全局 15s 超时
 * 会导致客户端报错但任务实际已创建；此处将本请求超时放宽到 120s。
 * @param projectId 项目 ID
 * @param shotIds 镜头 ID 列表（空 / 缺省 = 全部锁定镜头）
 * @returns 提交结果（任务摘要 / 冻结总额 / warnings）
 * @throws ApiError 60001 无可用镜头 / 30001 积分不足 / 60006 队列满
 */
export function batchSubmitRender(projectId: string, shotIds?: number[]): Promise<BatchRenderData> {
  return http.post<BatchRenderData>(
    `/projects/${projectId}/render-tasks/batch`,
    {
      shot_ids: shotIds && shotIds.length > 0 ? shotIds : null,
    },
    { timeout: 120000 },
  )
}

/**
 * 分页查询项目渲染任务列表。
 * @param projectId 项目 ID
 * @param params 状态 / 分页 / 排序参数
 * @returns 分页任务列表
 */
export function listRenderTasks(projectId: string, params?: ListRenderTasksParams): Promise<RenderTaskPage> {
  return http.get<RenderTaskPage>(`/projects/${projectId}/render-tasks`, { params })
}

/**
 * 查询渲染任务详情（outputs / qc_reports / 冻结结算）。
 * @param taskId 任务 ID
 * @returns 任务详情
 * @throws ApiError 60002 任务不存在 / 20003 无权限
 */
export function getRenderTaskDetail(taskId: number | string): Promise<RenderTaskDetail> {
  return http.get<RenderTaskDetail>(`/render-tasks/${taskId}`)
}

/**
 * 取消渲染任务（仅 pending/queued；取消后全额退款）。
 * @param taskId 任务 ID
 * @returns 取消结果（含退款金额）
 * @throws ApiError 40001 running 等状态不可取消
 */
export function cancelRenderTask(taskId: number | string): Promise<{ task_id: number; status: string; refunded_credits: number; shot_status: string | null }> {
  return http.post(`/render-tasks/${taskId}/cancel`)
}

/**
 * 重试渲染任务（failed/manual_required/cancelled → queued，202 受理）。
 * @param taskId 任务 ID
 * @returns 重试结果（含新 retry_count / 是否复用冻结）
 * @throws ApiError 40001 状态不允许 / 60004 重试超限 / 30001 积分不足
 */
export function retryRenderTask(taskId: number | string): Promise<{ task_id: number; status: string; retry_count: number; reused_freeze: boolean }> {
  return http.post(`/render-tasks/${taskId}/retry`)
}

/**
 * 单镜重新生成（等价 batch 单镜；新任务旧任务保留，202 受理）。
 * @param shotId 镜头 ID
 * @returns 提交结果
 * @throws ApiError 40001 镜头状态不允许 / 30001 积分不足
 */
export function regenerateShot(shotId: number | string): Promise<BatchRenderData> {
  return http.post(`/shots/${shotId}/regenerate`)
}

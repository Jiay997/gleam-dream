<!--
  文件级注释：P15 · 生成任务中心（路由 /projects/:projectId/render-tasks，Task 15）。
  v8 夜窗工作台 · 对照 stitch_prd/p5/code.html（BATCH CONTROL NODE 原型）1:1 布局对齐
  （Redesign-Preserve：结构对齐原型，主题沿用既有 gd-* 令牌体系，不引入新色值/字体/依赖）：
  - §1 批次控制头部（gd-night-window-hero 深色卡）：统一刊头（4px 光条 + 28px 衬线标题）；
    引擎说明行（主力任务类型聚合 + 质检/返还口径）；右侧护栏组（成本护栏 used/limit +
    可用余额 +「批量编排」主按钮 +「全批次挂起」预留禁用位）；
  - §2 状态仪表行（lg:grid-cols-12）：左 4 列全局进度卡（REALTIME 60FPS 徽标 + SVG 环形
    进度 = 已成功/全部 + 已渲染/待渲染 + 完成度细条 + 台账条[已结算/冻结中/预算上限] +
    已消耗金渐变/冻结中紫渐变分段预算条与图例）；右 8 列六态仪表卡（排队/生成/质检/已
    成功/重试/待人工，计数与预估积分绑定真实状态聚合，点击卡=切换状态筛选，激活描边轮廓）；
  - §3 渲染流水队列卡：工具栏（标题 + TASKS 徽标 + 搜索框[镜号/提示词/失败原因本地过滤]
    + 六态筛选 chips 迁入工具栏）；7 列栅格表（镜头编号&缩略图 | 提示词锚点/任务内容 |
    任务类型/编号 | 成本与结算 | 质检(QC) | 状态 | 实时操作），行状态色带（生成中紫洗 /
    失败红洗 / 待人工紫洗+左描边）+ 生成中 live 扫光进度；
  - §4 人工异常裁决台卡（v-if 首个失败/需人工任务）：FAILED 徽标 + 三栏（基准真源=
    final_prompt 锚点块 + 镜头元数据 / 本轮生成结果=图像产物直出或占位 + 质检诊断简报 /
    裁决操作与审计留痕=审计提示 + 失败原因 + 重试[主]/详情/全额返还标注）；
  - §5 成片版本选择与锁定卡（v-if 首个已成功任务）：版本 A/B/C 三卡并排（产物
    is_selected 锁定态紫描边 + 当前首选徽标，其余备选分支），右上产物统计徽标；
  - §6 底部批次收口行：批次就绪状态 + P6 流转状态行 + 导出简报（预留禁用位）+
    「进入 P6 后期合成」主按钮（全量达标才可用，替代原独立 readyForPost 横幅）。
  既有功能全保留：三态加载/错误重试/空态引导/状态筛选（chips + 仪表卡双入口）/搜索/
  详情抽屉（final_prompt 折叠 / outputs / qc_reports 分数条 / 冻结结算状态 / 失败详情）/
  取消与重试二次确认弹窗 / CostEstimateDialog 批量编排 / SSE + 10s 轮询兜底 + 600ms 防抖。
  数据：GET /projects/{id}/render/estimate、POST render-tasks/batch、
  GET render-tasks?status=&page=、GET /render-tasks/{id}、POST cancel|retry。
  SSE /projects/{id}/render-tasks/events（render.task.updated / render.batch.completed）
  + 10s 轮询兜底（useSSE 降级语义）；SSE 事件 600ms 防抖合并刷新。
  空态：项目零任务时引导回分镜画布锁定镜头。
  说明：原型假数据（ETA 04m15s、均速 18s/镜、Seed、人脸基准图、判决 textarea、
  同步循环播放等）无对应后端数据源，一律以「—」/占位/预留禁用位呈现，不硬编码。
-->
<script setup lang="ts">
/**
 * RenderTaskCenterView 生成任务中心（暗场夜窗工作台，布局对照 stitch_prd/p5）。
 * props：无。emits：无。
 * 依赖 render API（estimate/batch/list/detail/cancel/retry）、useSSE、useToast、
 * CostEstimateDialog（detail 插槽渲染逐镜成本）、useBillingStore（余额 / 冻结，预算条数据源）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  batchSubmitRender,
  cancelRenderTask,
  estimateRender,
  FAILURE_TYPE_LABELS,
  getRenderTaskDetail,
  listRenderTasks,
  RENDER_STATUS_OPTIONS,
  RENDER_TASK_TYPE_LABELS,
  retryRenderTask,
  type BatchRenderData,
  type RenderEstimateData,
  type RenderTaskDetail,
  type RenderTaskListItem,
} from '@/api/render'
import AppButton from '@/components/common/AppButton.vue'
import { getProject } from '@/api/projects'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import CostEstimateDialog from '@/components/common/CostEstimateDialog.vue'
import CreditAmountText from '@/components/common/CreditAmountText.vue'
import DrawerPanel from '@/components/common/DrawerPanel.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/composables/useToast'
import { useAuthStore } from '@/stores/auth'
import { useBillingStore } from '@/stores/billing'
import { formatRelativeTime } from '@/utils/datetime'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const authStore = useAuthStore()
const billingStore = useBillingStore()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ==================== 列表数据 ==================== */

/** 全量任务列表（用于统计聚合，最多拉 10 页 × 100 条） */
const allTasks = ref<RenderTaskListItem[]>([])
/** 列表总条数（后端 total） */
const total = ref(0)
/** 加载中 */
const loading = ref(true)
/** 加载失败文案 */
const loadError = ref('')
/** 状态筛选（空串 = 全部，本地过滤） */
const statusFilter = ref('')
/** 队列搜索关键字（镜号 / 任务 ID / 提示词 / 失败信息本地过滤） */
const searchQuery = ref('')

/**
 * 搜索匹配（镜号 / 任务 ID / 任务类型 / 提示词 / 失败类型与原因任一命中）。
 * @param task 任务行
 * @param keyword 小写关键字
 * @returns 是否命中
 */
function matchSearch(task: RenderTaskListItem, keyword: string): boolean {
  const haystack = [
    shotLabel(task),
    `#${task.id}`,
    RENDER_TASK_TYPE_LABELS[task.task_type] ?? task.task_type,
    task.shot?.final_prompt ?? '',
    task.failure_type ? (FAILURE_TYPE_LABELS[task.failure_type] ?? task.failure_type) : '',
    task.failure_reason ?? '',
  ]
    .join(' ')
    .toLowerCase()
  return haystack.includes(keyword)
}

/** 筛选后的任务（状态 + 搜索双重本地过滤，避免重复请求） */
const filteredTasks = computed(() => {
  const byStatus = statusFilter.value
    ? allTasks.value.filter((task) => task.status === statusFilter.value)
    : allTasks.value
  const keyword = searchQuery.value.trim().toLowerCase()
  return keyword ? byStatus.filter((task) => matchSearch(task, keyword)) : byStatus
})

/** 失败 / 需人工介入行的置顶权重（F5-01：失败行置顶，其余保持原序） */
function failureRank(task: RenderTaskListItem): number {
  if (task.status === 'failed') return 0
  if (task.status === 'manual_required') return 1
  return 2
}

/** 玻璃任务行（筛选 + 失败置顶；稳定排序不打乱同权重原序） */
const sortedTasks = computed(() =>
  filteredTasks.value
    .map((task, index) => ({ task, index }))
    .sort((a, b) => failureRank(a.task) - failureRank(b.task) || a.index - b.index)
    .map((entry) => entry.task),
)

/** 任务总数 */
const totalCount = computed(() => allTasks.value.length)
/** 已成功任务数 */
const succeededCount = computed(() => allTasks.value.filter((t) => t.status === 'succeeded').length)
/** 待渲染任务数（未成功，含排队/生成/质检/失败等） */
const pendingCount = computed(() => totalCount.value - succeededCount.value)

/** 全部任务终结且无失败/待人工（cancelled 视为放弃不阻塞）→ 引导进入后期合成 */
const readyForPost = computed(
  () => succeededCount.value > 0 && allTasks.value.every((t) => t.status === 'succeeded' || t.status === 'cancelled'),
)

/** 主列表区就绪（非加载 / 无错误 / 有任务）——§4~§6 区块的渲染前提 */
const queueReady = computed(() => !loading.value && !loadError.value && totalCount.value > 0)

/* ==================== 仪表盘聚合（§1 头部护栏 / §2 状态仪表行） ==================== */

/** 六态筛选 chips 计数（key 为状态值，'' = 全部；同时是仪表卡计数数据源） */
const filterCounts = computed<Record<string, number>>(() => {
  const counts: Record<string, number> = { '': allTasks.value.length }
  for (const task of allTasks.value) {
    counts[task.status] = (counts[task.status] ?? 0) + 1
  }
  return counts
})

/** 各状态预估积分合计（仪表卡副徽标数据源） */
const estimatedByStatus = computed<Record<string, number>>(() => {
  const sums: Record<string, number> = {}
  for (const task of allTasks.value) {
    sums[task.status] = (sums[task.status] ?? 0) + task.estimated_cost_credits
  }
  return sums
})

/** 排队 + 待处理的预估积分合计（排队中卡副徽标） */
const queuedEstimatedCredits = computed(
  () => (estimatedByStatus.value.queued ?? 0) + (estimatedByStatus.value.pending ?? 0),
)

/** 生成中预估积分合计（生成中卡副徽标） */
const runningEstimatedCredits = computed(() => estimatedByStatus.value.running ?? 0)

/** 全局完成度（已成功 / 全部任务，%） */
const completionPct = computed(() => (totalCount.value > 0 ? Math.round((succeededCount.value / totalCount.value) * 100) : 0))

/** 环形进度 SVG 周长（r = 40） */
const GAUGE_CIRCUMFERENCE = 251.2

/** 环形进度描边偏移（完成度 → dashoffset） */
const gaugeOffset = computed(() => GAUGE_CIRCUMFERENCE * (1 - Math.min(1, completionPct.value / 100)))

/** 主力任务类型标签（出现最多的 task_type；无任务为 null） */
const primaryTaskTypeLabel = computed<string | null>(() => {
  const counts = new Map<string, number>()
  for (const task of allTasks.value) {
    counts.set(task.task_type, (counts.get(task.task_type) ?? 0) + 1)
  }
  const top = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]
  return top ? (RENDER_TASK_TYPE_LABELS[top[0]] ?? top[0]) : null
})

/* ==================== 预算统计（§2 全局进度卡台账数据源） ==================== */

/** 已结算任务子集（参与成本偏差计算） */
const settledTasks = computed(() => allTasks.value.filter((t) => t.actual_cost_credits !== null))

/** 已结算任务的预估合计 */
const settledEstimated = computed(() => settledTasks.value.reduce((sum, t) => sum + t.estimated_cost_credits, 0))
/** 已消耗（已结算任务的实际合计） */
const consumedCredits = computed(() => settledTasks.value.reduce((sum, t) => sum + (t.actual_cost_credits ?? 0), 0))
/** 全部任务预估合计（未结算任务仍在途冻结） */
const totalEstimated = computed(() => allTasks.value.reduce((sum, t) => sum + t.estimated_cost_credits, 0))

/** 预算上限（P2-12：优先项目算力预算；项目未设预算时回退账户口径；全 0 展示 —） */
const projectBudget = ref<number | null>(null)
/** 项目已结算消耗（P2-12：成本护栏以项目口径展示，非用户余额） */
const projectSpent = ref<number | null>(null)
/** 项目在途冻结（估算接口返回；未加载时为 0） */
const projectFrozen = ref<number | null>(null)

const budgetCredits = computed(
  () => projectBudget.value ?? billingStore.balance + billingStore.frozenAmount + consumedCredits.value,
)

/** 已消耗占预算比例（%） */
const consumedPct = computed(() => (budgetCredits.value > 0 ? Math.min(100, (consumedCredits.value / budgetCredits.value) * 100) : 0))
/** 冻结中占预算比例（%） */
const frozenPct = computed(() => (budgetCredits.value > 0 ? Math.min(100 - consumedPct.value, (totalEstimated.value / budgetCredits.value) * 100) : 0))

/** 已消耗 + 在途冻结合计（成本护栏口径；P2-12：优先项目已结算 + 项目在途冻结） */
const costGuardUsed = computed(() =>
  projectSpent.value != null
    ? projectSpent.value + (projectFrozen.value ?? 0)
    : consumedCredits.value + billingStore.frozenAmount,
)

/** 已成功任务均耗（积分，一位小数；无结算数据为 null） */
const avgSettledCredits = computed<number | null>(() =>
  settledTasks.value.length > 0 ? Math.round((consumedCredits.value / settledTasks.value.length) * 10) / 10 : null,
)

/** 成本偏差（|actual-estimated|/estimated，来自列表聚合；无结算数据为 null） */
const costDeviation = computed<number | null>(() => {
  if (settledEstimated.value <= 0) return null
  return Math.abs(consumedCredits.value - settledEstimated.value) / settledEstimated.value
})

/** 偏差胶囊文案 */
const deviationLabel = computed(() =>
  costDeviation.value === null ? '' : `偏差 ${(costDeviation.value * 100).toFixed(1)}%`,
)

/** 偏差胶囊样式（洗底 token 档：≤15% 薄荷 / 15-30% 琥珀 / >30% 玫红） */
const deviationClass = computed(() => {
  const value = costDeviation.value
  if (value === null) return ''
  if (value <= 0.15) return 'bg-success-bg text-success-bright'
  if (value <= 0.3) return 'bg-warning-bg text-warning-bright'
  return 'bg-danger-bg text-danger-bright'
})

/**
 * 仪表卡点击 → 切换对应状态筛选（再次点击同态取消回全部）。
 * @param status 状态值
 * @returns 无
 */
function toggleStatusFilter(status: string): void {
  statusFilter.value = statusFilter.value === status ? '' : status
}

/**
 * 六态仪表卡样式：语义底色档 + 筛选激活轮廓（.gd-card-dark 由 index.css 后置定义，
 * 静态覆盖需 `!` 前缀；激活态用 outline 避免与语义描边档互相覆盖）。
 * @param status 状态值
 * @param tint 语义底色/描边档（可为空串）
 * @returns 卡片类
 */
function gaugeCardClass(status: string, tint: string): string {
  const active = statusFilter.value === status
  return [tint, active ? 'outline outline-1 outline-primary-300 !shadow-gd-bloom' : 'hover:!border-white/25']
    .filter(Boolean)
    .join(' ')
}

/* ==================== 玻璃任务行单元格辅助 ==================== */

/**
 * 镜号展示文案（无镜头摘要时回退任务 ID）。
 * @param task 任务行
 * @returns 镜号文案
 */
function shotLabel(task: RenderTaskListItem): string {
  if (task.shot) return `S${task.shot.shot_no}`
  return task.shot_id ? `#${task.shot_id}` : '—'
}

/**
 * 镜头时长 timecode（mm:ss，对齐原型缩略图角标风格；无镜头摘要为 —）。
 * @param task 任务行
 * @returns 时长文案
 */
function shotDurationLabel(task: RenderTaskListItem): string {
  const ms = task.shot?.duration_ms
  if (!ms) return '—'
  const totalSeconds = Math.round(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/** 重试色阶样式（0 次中性 / 未超限黄 / 达到上限红；暗场用 bright 档） */
function retryClass(task: RenderTaskListItem): string {
  const max = (task as { max_retries?: number }).max_retries ?? 2
  if (task.retry_count <= 0) return 'text-player-hint'
  if (task.retry_count < max) return 'text-warning-bright'
  return 'text-danger-bright'
}

/**
 * 任务最大重试次数展示值（后端列表不含 max_retries 时回退默认 2）。
 * @param task 任务行
 * @returns 最大重试次数
 */
function maxRetries(task: RenderTaskListItem): number {
  return (task as { max_retries?: number }).max_retries ?? 2
}

/**
 * 产物展示地址：优先后端附带的 media_url，否则回退媒体文件 ID 占位。
 * @param output 产物对象
 * @returns 展示文案
 */
function outputUrl(output: RenderTaskDetail['outputs'][number]): string {
  const url = (output as { media_url?: string | null }).media_url
  return url ?? `媒体文件 #${output.media_file_id ?? '—'}`
}

/**
 * 判断产物是否为可在页面内直接播放的视频。
 * @param output 产物对象
 * @returns 产物类型为 video 且 MIME/扩展名表明是 MP4 时为 true
 */
function isPlayableVideo(output: RenderTaskDetail['outputs'][number]): boolean {
  const url = output.media_url ?? ''
  return output.output_type === 'video'
    && (output.mime_type === 'video/mp4' || url.toLowerCase().endsWith('.mp4'))
}

/** 是否展示展开失败卡（failed / manual_required 行置顶展开） */
function isFailedRow(task: RenderTaskListItem): boolean {
  return task.status === 'failed' || task.status === 'manual_required'
}

/** 行内质检格状态（列表聚合无逐镜 QC 分数，按任务状态推导五态） */
type QcCellState = 'pass' | 'reviewing' | 'failed' | 'borderline' | 'pending'

/**
 * 行内质检格状态推导。
 * @param task 任务行
 * @returns pass=已通过 / reviewing=评估中 / failed=未通过 / borderline=踩线待裁决 / pending=未开始
 */
function qcCellState(task: RenderTaskListItem): QcCellState {
  if (task.status === 'succeeded') return 'pass'
  if (task.status === 'failed') return 'failed'
  if (task.status === 'manual_required') return 'borderline'
  if (task.status === 'qc_review' || task.status === 'retrying') return 'reviewing'
  return 'pending'
}

/**
 * 行底色带（对齐原型行状态色带；tinted 行不再叠加 hover 提亮）。
 * @param status 任务状态
 * @returns 行容器类
 */
function statusRowClass(status: string): string {
  switch (status) {
    case 'running':
      return 'bg-primary-50 hover:bg-primary-100'
    case 'failed':
      return 'bg-danger-bg'
    case 'manual_required':
      // ! 前缀：抵消 ul divide-white/5 对行 border-color 的覆盖（divide 选择器特异性更高）
      return 'bg-primary-100 border-l-2 !border-l-primary-400'
    default:
      return 'hover:bg-white/[0.03]'
  }
}

/* ==================== 人工异常裁决台（§4，v-if 首个失败/需人工任务） ==================== */

/** 裁决台目标任务（失败置顶序首个 failed / manual_required） */
const adjudicationTask = computed(() => sortedTasks.value.find((task) => isFailedRow(task)) ?? null)

/** 裁决台任务详情（产物 / QC 报告数据源） */
const adjudicationDetail = ref<RenderTaskDetail | null>(null)

/** 裁决台最新一轮一致性得分（无报告为 null） */
const adjudicationScore = computed<number | null>(() => {
  const reports = (adjudicationDetail.value?.qc_reports ?? []).filter((report) => report.consistency_score !== null)
  return reports.length > 0 ? (reports[reports.length - 1].consistency_score as number) : null
})

/** 裁决台候选帧（首个 image 产物直链 + 非图像产物占位说明） */
const adjudicationFrame = computed<{ url: string; note: string }>(() => {
  const outputs = adjudicationDetail.value?.outputs ?? []
  for (const output of outputs) {
    const url = (output as { media_url?: string | null }).media_url
    if (url && output.output_type === 'image') return { url, note: '' }
  }
  const first = outputs[0]
  return { url: '', note: first ? '该产物为视频 / 音频，请在详情抽屉查看' : '产物待汇编，暂无可比对帧' }
})

// 裁决目标变化时拉取详情（竞态守卫：仅在目标未变时回写）
watch(adjudicationTask, (task) => {
  adjudicationDetail.value = null
  if (!task) return
  void loadDetail(task).then((data) => {
    if (adjudicationTask.value?.id === task.id) adjudicationDetail.value = data
  })
})

/* ==================== 成片版本选择与锁定（§5，v-if 首个已成功任务） ==================== */

/** 渲染产物类型 → 中文标签 */
const OUTPUT_TYPE_LABELS: Record<string, string> = {
  video: '视频',
  image: '图像',
  audio: '音频',
  subtitle: '字幕',
}

/** 版本选择器目标任务（首个已成功任务） */
const versionTask = computed(() => allTasks.value.find((task) => task.status === 'succeeded') ?? null)

/** 版本选择器任务详情（outputs 多产物数据源） */
const versionDetail = ref<RenderTaskDetail | null>(null)

/** 已采用产物数（版本选择器头部徽标） */
const selectedOutputCount = computed(() => (versionDetail.value?.outputs ?? []).filter((output) => output.is_selected).length)

/** 版本卡槽位形状（版本 A/B/C） */
interface VersionSlot {
  /** 槽位角标 */
  label: string
  /** 产物数据（未生成 / 未加载为 null） */
  output: RenderTaskDetail['outputs'][number] | null
  /** 图像产物直链（仅 image 类型可直出预览） */
  imageUrl: string
  /** 非图像产物占位说明 */
  note: string
}

/** 版本卡槽位（最多 3 个产物 A/B/C；产物不足补占位卡） */
const versionSlots = computed<VersionSlot[]>(() => {
  const outputs = versionDetail.value?.outputs ?? []
  return (['A', 'B', 'C'] as const).map((label, index) => {
    const output = outputs[index] ?? null
    const mediaUrl = output ? ((output as { media_url?: string | null }).media_url ?? '') : ''
    return {
      label,
      output,
      imageUrl: output?.output_type === 'image' && mediaUrl ? mediaUrl : '',
      note: output && output.output_type !== 'image' ? '非图像产物 · 详情抽屉查看' : '',
    }
  })
})

// 版本目标变化时拉取详情（竞态守卫：仅在目标未变时回写）
watch(versionTask, (task) => {
  versionDetail.value = null
  if (!task) return
  void loadDetail(task).then((data) => {
    if (versionTask.value?.id === task.id) versionDetail.value = data
  })
})

/* ==================== 批量生成 ==================== */

/** 估算弹窗显示状态 */
const estimateDialogOpen = ref(false)
/** 估算请求中 */
const estimating = ref(false)
/** 提交批量生成请求中 */
const submitting = ref(false)
/** 估算数据（逐镜 + 合计 + 可用余额） */
const estimateData = ref<RenderEstimateData | null>(null)

/**
 * 打开批量生成弹窗：先估算全部锁定镜头成本并同步余额。
 * @returns 无
 */
async function openEstimate(): Promise<void> {
  estimating.value = true
  try {
    const [data] = await Promise.all([estimateRender(projectId.value), billingStore.refresh()])
    estimateData.value = data
    // P2-12：估算结果带项目预算快照，同步刷新护栏口径
    if (data.budget_credits > 0) projectBudget.value = data.budget_credits
    projectSpent.value = data.spent_credits
    projectFrozen.value = data.frozen_credits
    estimateDialogOpen.value = true
  } catch {
    // 拦截器已 toast（60001 无锁定镜头等）
  } finally {
    estimating.value = false
  }
}

/**
 * 确认批量提交（POST batch，空 shot_ids = 全部锁定镜头；202 后 SSE 刷新）。
 * @returns 无
 */
async function confirmBatch(): Promise<void> {
  submitting.value = true
  try {
    const result: BatchRenderData = await batchSubmitRender(projectId.value)
    estimateDialogOpen.value = false
    if (result.idempotent) {
      toast.info('该批次已在处理中（幂等命中），未重复提交')
    } else {
      toast.success(`已受理 ${result.tasks.length} 个渲染任务，冻结 ${result.frozen_credits} 积分`)
    }
    if (result.warnings.length > 0) {
      toast.warning(result.warnings.slice(0, 3).join('；'))
    }
    await Promise.all([fetchTasks(), billingStore.refresh()])
  } catch {
    // 拦截器已 toast（30001 积分不足 / 60001 无可用镜头）
  } finally {
    submitting.value = false
  }
}

/* ==================== 行内操作：取消 / 重试（二次确认） ==================== */

/** 操作进行中的任务 ID（按钮 loading） */
const busyTaskId = ref<number | null>(null)
/** 取消确认弹窗目标任务 */
const cancelTarget = ref<RenderTaskListItem | null>(null)
/** 取消确认弹窗显示状态 */
const cancelDialogOpen = ref(false)
/** 重试确认弹窗目标任务（F5-01-s3） */
const retryTarget = ref<RenderTaskListItem | null>(null)
/** 重试确认弹窗显示状态 */
const retryDialogOpen = ref(false)
/** 重试确认弹窗正文（冻结预估 + 剩余重试次数） */
const retryDialogContent = computed(() => {
  const task = retryTarget.value
  if (!task) return ''
  const max = maxRetries(task)
  return `任务 #${task.id} 重试将重新入队并按预估冻结 ${task.estimated_cost_credits} 积分；本任务剩余重试次数 ${Math.max(0, max - task.retry_count)}/${max}，超限后需修改参数再提交。`
})

/**
 * 打开取消确认弹窗。
 * @param task 任务行
 * @returns 无
 */
function openCancelDialog(task: RenderTaskListItem): void {
  cancelTarget.value = task
  cancelDialogOpen.value = true
}

/**
 * 打开重试二次确认弹窗（提示冻结预估与剩余重试次数）。
 * @param task 任务行
 * @returns 无
 */
function openRetryDialog(task: RenderTaskListItem): void {
  retryTarget.value = task
  retryDialogOpen.value = true
}

/**
 * 提交取消（仅 pending/queued 可取消；全额退款）。
 * @returns 无
 */
async function confirmCancel(): Promise<void> {
  const task = cancelTarget.value
  if (!task) return
  busyTaskId.value = task.id
  try {
    const result = await cancelRenderTask(task.id)
    toast.success(`任务已取消${result.refunded_credits > 0 ? `，退款 ${result.refunded_credits} 积分` : ''}`)
    cancelDialogOpen.value = false
    cancelTarget.value = null
    await Promise.all([fetchTasks(), billingStore.refresh()])
  } catch {
    // 拦截器已 toast（40001 执行中不可取消等）
  } finally {
    busyTaskId.value = null
  }
}

/**
 * 重试任务（failed/manual_required → queued，202 受理）。
 * @param task 任务行
 * @returns 无
 */
async function retryTask(task: RenderTaskListItem): Promise<void> {
  busyTaskId.value = task.id
  try {
    const result = await retryRenderTask(task.id)
    toast.success(`任务已重新入队（第 ${result.retry_count} 次重试）`)
    await Promise.all([fetchTasks(), billingStore.refresh()])
  } catch {
    // 拦截器已 toast（60004 超限 / 30001 积分不足）
  } finally {
    busyTaskId.value = null
  }
}

/**
 * 重试二次确认回调（确认后执行重试并关闭弹窗）。
 * @returns 无
 */
async function confirmRetry(): Promise<void> {
  const task = retryTarget.value
  if (!task) return
  await retryTask(task)
  retryDialogOpen.value = false
  retryTarget.value = null
}

/* ==================== 详情抽屉 ==================== */

/** 详情抽屉显示状态 */
const drawerOpen = ref(false)
/** 详情加载中 */
const detailLoading = ref(false)
/** 当前详情数据 */
const detail = ref<RenderTaskDetail | null>(null)
/** prompt 折叠块展开状态 */
const promptExpanded = ref(false)

/**
 * 拉取任务详情（抽屉 / 裁决台 / 版本卡共用；失败返回 null，拦截器已 toast）。
 * @param task 任务行
 * @returns 详情数据（失败为 null）
 */
async function loadDetail(task: RenderTaskListItem): Promise<RenderTaskDetail | null> {
  try {
    return await getRenderTaskDetail(task.id)
  } catch {
    // 拦截器已 toast（60002 任务不存在）
    return null
  }
}

/**
 * 打开详情抽屉并加载任务详情。
 * @param task 任务行
 * @returns 无
 */
async function openDetail(task: RenderTaskListItem): Promise<void> {
  drawerOpen.value = true
  detailLoading.value = true
  detail.value = null
  promptExpanded.value = false
  detail.value = await loadDetail(task)
  if (detail.value === null) drawerOpen.value = false
  detailLoading.value = false
}

/**
 * 计算任务耗时文案（started/finished 或 queued/finished，秒级一位小数）。
 * @param data 任务详情
 * @returns 耗时文案
 */
function durationLabel(data: RenderTaskDetail): string {
  const start = data.started_at ?? data.queued_at
  if (!start || !data.finished_at) return '—'
  const ms = new Date(data.finished_at).getTime() - new Date(start).getTime()
  if (Number.isNaN(ms) || ms < 0) return '—'
  return ms < 60_000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}

/** 冻结状态 → 中文标签 */
const FREEZE_STATUS_LABELS: Record<string, string> = {
  frozen: '已冻结',
  settled: '已结算',
  released: '已释放',
  expired: '已过期',
}

/* ==================== SSE 实时刷新 + 轮询兜底 ==================== */

/** SSE 事件流地址（EventSource 不支持请求头鉴权，token 走查询参数） */
const sseUrl = computed<string>(() =>
  authStore.token ? `/api/v1/projects/${projectId.value}/render-tasks/events?access_token=${authStore.token}` : '',
)

/** 防抖刷新定时器 */
let refreshTimer: ReturnType<typeof setTimeout> | null = null

/**
 * SSE 事件防抖合并刷新（高频 task.updated 事件 600ms 合并为一次列表请求）。
 * @returns 无
 */
function scheduleRefresh(): void {
  if (refreshTimer) return
  refreshTimer = setTimeout(() => {
    refreshTimer = null
    void fetchTasks()
  }, 600)
}

/**
 * SSE 事件分派：任务状态更新防抖刷新；批次完成弹出汇总 toast。
 * @param event 消息事件
 * @returns 无
 */
function onSseMessage(event: MessageEvent<string>): void {
  if (event.type === 'render.task.updated') {
    scheduleRefresh()
    return
  }
  if (event.type === 'render.batch.completed') {
    try {
      const data = JSON.parse(event.data) as { succeeded?: number; failed?: number; cost?: number }
      toast.success(`批次完成：成功 ${data.succeeded ?? 0} / 失败 ${data.failed ?? 0} · 消耗 ${data.cost ?? 0} 积分`)
    } catch {
      toast.success('渲染批次已完成')
    }
    void fetchTasks()
  }
}

// SSE 断线自动降级 10s 轮询兜底
useSSE(sseUrl, {
  onMessage: onSseMessage,
  onPoll: fetchTasks,
  eventTypes: ['render.task.updated', 'render.batch.completed'],
  pollIntervalMs: 10_000,
})

// 挂载加载（?start=1 为分镜审批通过直达：加载后自动打开批量生成确认弹窗）+ 卸载清理防抖定时器
onMounted(async () => {
  // P2-12：加载项目预算口径（预算条/成本护栏显示项目预算而非用户余额）
  try {
    const project = await getProject(projectId.value)
    const budget = project.budget_credits ?? 0
    projectBudget.value = budget > 0 ? budget : null
    projectSpent.value = project.spent_credits ?? 0
  } catch {
    // 预算读取失败不影响任务列表，回退账户口径展示
  }
  await Promise.all([fetchTasks(), billingStore.refresh()])
  // 修复（E2E 报告 P-05）：分镜审批（HITL-3）通过后直达本页时自动打开
  // 批量编排弹窗。此前带 totalCount > 0 守卫——首次审批通过时任务列表
  // 必然为空，弹窗被跳过，用户落在空页无引导；现在无论是否有历史任务
  // 都尝试打开（无可渲染镜头时后端 60001 由拦截器统一 toast 提示）。
  if (route.query.start === '1') {
    await openEstimate()
  }
})
onUnmounted(() => {
  if (refreshTimer) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
})

/**
 * 拉取全量任务列表（page_size=100 循环翻页，最多 10 页）。
 * @returns 无
 */
async function fetchTasks(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const first = await listRenderTasks(projectId.value, { page: 1, page_size: 100 })
    const collected: RenderTaskListItem[] = [...first.items]
    const pages = Math.min(10, Math.ceil(first.total / 100))
    for (let page = 2; page <= pages; page += 1) {
      const next = await listRenderTasks(projectId.value, { page, page_size: 100 })
      collected.push(...next.items)
      if (next.items.length < 100) break
    }
    allTasks.value = collected
    total.value = first.total
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '渲染任务列表加载失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="gd-stage-page flex h-full flex-col overflow-hidden">
    <!-- ============ §1 批次控制头部（stitch_prd/p5 §1 · BATCH CONTROL NODE） ============ -->
    <header class="shrink-0 px-6 pb-4 pt-5">
      <section class="gd-night-window gd-night-window-hero flex flex-col gap-4 p-5 xl:flex-row xl:items-center xl:justify-between">
        <!-- 左列：徽标行 / 大标题 / 引擎说明行 -->
        <div class="flex min-w-0 flex-col gap-1.5">
          <div class="flex min-w-0 items-center gap-3">
            <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
            <h1 class="gd-masthead truncate text-display-lg">P5 批量视频渲染与任务仪表盘</h1>
          </div>
          <p class="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-player-hint">
            <span class="inline-flex items-center gap-1.5 text-primary-300">
              <span class="gd-dot gd-dot--violet animate-pulse" aria-hidden="true" />
              主力任务类型: {{ primaryTaskTypeLabel ?? '—' }}
            </span>
            <span class="text-white/20" aria-hidden="true">|</span>
            <span>渲染队列实时驱动 · 失败全额返还 · 质检自动修正最多 3 轮</span>
          </p>
        </div>
        <!-- 右列：护栏徽标组（成本护栏 / 可用余额 / 主操作 / 预留位） -->
        <div class="flex flex-wrap items-center gap-2">
          <div class="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 py-2">
            <svg class="h-4 w-4 shrink-0 text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
              <path d="M12 3l7 3v5.2c0 4.3-2.9 7.9-7 9.8-4.1-1.9-7-5.5-7-9.8V6l7-3z" stroke-linejoin="round" />
            </svg>
            <span class="text-caption text-player-hint">成本护栏</span>
            <span class="gd-num text-caption font-semibold text-player-text">{{ costGuardUsed }}<template v-if="budgetCredits > 0"> / {{ budgetCredits }}</template> 积分</span>
            <span v-if="deviationLabel" class="rounded-full px-2 py-0.5 text-micro font-medium" :class="deviationClass">{{ deviationLabel }}</span>
          </div>
          <div class="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 py-2">
            <span class="text-caption text-player-hint">可用余额</span>
            <span class="gd-num text-caption font-semibold text-gold">{{ billingStore.balance }}</span>
          </div>
          <AppButton variant="primary" size="md" class="gd-btn-bright" :loading="estimating" @click="openEstimate">批量编排</AppButton>
          <AppButton variant="ghost" size="md" disabled class="!text-danger-bright" title="预留操作位：批次级挂起接口暂未开放">全批次挂起</AppButton>
        </div>
      </section>
    </header>

    <!-- ============ §2~§6 滚动区 ============ -->
    <div class="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 pb-6">
      <!-- ======== §2 状态仪表行（左全局进度卡 + 右六态仪表卡） ======== -->
      <section class="grid grid-cols-1 gap-4 lg:grid-cols-12" aria-label="状态仪表行">
        <!-- 全局进度卡 -->
        <div class="gd-card-dark flex flex-col p-5 lg:col-span-4">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-2">
              <svg class="h-4 w-4 text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <circle cx="12" cy="12" r="8.5" />
                <path d="M12 7.5V12l3 2" stroke-linecap="round" />
              </svg>
              <span class="text-kicker uppercase tracking-[0.14em] text-player-hint">管线总览 · 全局吞吐率</span>
            </div>
            <span class="gd-num rounded-full bg-primary-100 px-2 py-0.5 text-micro uppercase tracking-wider text-primary-300">Realtime 60fps</span>
          </div>
          <div class="my-4 flex items-center justify-between gap-4">
            <!-- 环形进度（完成度 = 已成功 / 全部） -->
            <div class="relative flex h-32 w-32 shrink-0 items-center justify-center">
              <svg class="h-full w-full -rotate-90" viewBox="0 0 100 100" aria-hidden="true">
                <circle class="text-white/10" cx="50" cy="50" r="40" fill="transparent" stroke="currentColor" stroke-width="8" />
                <circle
                  class="text-primary-400 transition-[stroke-dashoffset] duration-gd-slow ease-gd-out"
                  cx="50" cy="50" r="40" fill="transparent" stroke="currentColor" stroke-width="8"
                  stroke-linecap="round" :stroke-dasharray="GAUGE_CIRCUMFERENCE" :stroke-dashoffset="gaugeOffset"
                />
              </svg>
              <div class="absolute inset-0 flex flex-col items-center justify-center">
                <span class="font-num text-[28px] font-semibold leading-tight tabular-nums text-player-text">{{ completionPct }}%</span>
                <span class="text-micro uppercase tracking-[0.14em] text-player-hint">Batch Sync</span>
              </div>
            </div>
            <div class="flex min-w-0 flex-1 flex-col gap-1">
              <span class="text-caption text-player-hint">已渲染 / 待渲染</span>
              <span class="gd-num text-title text-player-text">{{ succeededCount }} / {{ pendingCount }}</span>
              <span class="truncate text-caption text-player-hint" title="后端暂未提供速率采样，暂不支持 ETA 估算">近 10 分钟平均速率: <span class="gd-num text-primary-300">—</span></span>
              <div class="gd-progress-dark mt-1 h-1.5 w-full overflow-hidden rounded-full" role="progressbar" :aria-valuenow="completionPct" aria-valuemin="0" aria-valuemax="100" aria-label="批次完成度">
                <div class="h-full bg-grad-primary transition-[width] duration-gd-slow ease-gd-out" :style="{ width: `${completionPct}%` }" />
              </div>
            </div>
          </div>
          <!-- 台账条：预算分段条 + 已结算 / 冻结中 / 预算上限 -->
          <div class="mt-auto border-t border-white/5 pt-3">
            <div class="gd-progress-dark h-1.5 w-full overflow-hidden rounded-full" role="progressbar" :aria-valuenow="Math.round(consumedPct)" aria-valuemin="0" aria-valuemax="100" aria-label="预算消耗进度">
              <div class="flex h-full">
                <div class="h-full bg-grad-gold transition-[width] duration-gd-slow ease-gd-out" :style="{ width: `${consumedPct}%` }" />
                <div class="h-full bg-grad-primary transition-[width] duration-gd-slow ease-gd-out" :style="{ width: `${frozenPct}%` }" />
              </div>
            </div>
            <div class="mt-1.5 flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-micro text-player-hint">
              <span class="inline-flex items-center gap-1.5">
                <span class="h-1.5 w-1.5 rounded-full bg-gold" aria-hidden="true" />已结算 <span class="gd-num font-semibold text-player-text">{{ consumedCredits }}</span>
                <span v-if="deviationLabel" class="rounded-full px-1.5 py-0.5 text-micro font-medium" :class="deviationClass">{{ deviationLabel }}</span>
              </span>
              <span class="inline-flex items-center gap-1.5"><span class="h-1.5 w-1.5 rounded-full bg-primary-400" aria-hidden="true" />冻结中 <span class="gd-num font-semibold text-primary-300">{{ billingStore.frozenAmount }}</span></span>
              <span>预算上限 <span class="gd-num font-semibold text-player-text">{{ budgetCredits > 0 ? budgetCredits : '—' }}</span></span>
            </div>
          </div>
        </div>

        <!-- 六态仪表卡（点击 = 切换状态筛选） -->
        <div class="grid grid-cols-2 content-start gap-3 md:grid-cols-3 lg:col-span-8" role="group" aria-label="六态仪表卡（点击筛选）">
          <!-- 排队中 -->
          <button
            type="button"
            class="gd-card-dark flex cursor-pointer flex-col justify-between p-4 text-left"
            :class="gaugeCardClass('queued', '')"
            :aria-pressed="statusFilter === 'queued'"
            @click="toggleStatusFilter('queued')"
          >
            <span class="flex items-center justify-between">
              <span class="text-caption text-player-hint">排队中 (Queued)</span>
              <svg class="h-4 w-4 text-player-hint" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <circle cx="12" cy="12" r="8.5" />
                <path d="M12 7.5V12l3 2" stroke-linecap="round" />
              </svg>
            </span>
            <span class="mt-3 flex items-baseline justify-between gap-2">
              <span class="font-num text-[26px] font-semibold leading-none tabular-nums text-player-text">{{ filterCounts['queued'] ?? 0 }}</span>
              <span class="rounded-full bg-white/5 px-2 py-0.5 text-micro text-player-hint">预估 {{ queuedEstimatedCredits }} 积分</span>
            </span>
            <span class="mt-1.5 block text-caption text-player-hint">Worker 队列待派发</span>
          </button>
          <!-- 生成中 -->
          <button
            type="button"
            class="gd-card-dark flex cursor-pointer flex-col justify-between p-4 text-left"
            :class="gaugeCardClass('running', '!bg-primary-50 !border-primary-200')"
            :aria-pressed="statusFilter === 'running'"
            @click="toggleStatusFilter('running')"
          >
            <span class="flex items-center justify-between">
              <span class="text-caption text-primary-300">生成中 (Rendering)</span>
              <span class="gd-dot gd-dot--violet animate-pulse" aria-hidden="true" />
            </span>
            <span class="mt-3 flex items-baseline justify-between gap-2">
              <span class="font-num text-[26px] font-semibold leading-none tabular-nums text-primary-300">{{ filterCounts['running'] ?? 0 }}</span>
              <span class="gd-num text-micro text-primary-300">预估 {{ runningEstimatedCredits }} 积分</span>
            </span>
            <span class="mt-1.5 block text-caption text-player-hint">渲染节点执行中 · SSE 实时回传</span>
          </button>
          <!-- 质检中 -->
          <button
            type="button"
            class="gd-card-dark flex cursor-pointer flex-col justify-between p-4 text-left"
            :class="gaugeCardClass('qc_review', '')"
            :aria-pressed="statusFilter === 'qc_review'"
            @click="toggleStatusFilter('qc_review')"
          >
            <span class="flex items-center justify-between">
              <span class="text-caption text-player-hint">质检中 (QC Check)</span>
              <svg class="h-4 w-4 animate-spin text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <path d="M20 12a8 8 0 11-2.34-5.66M20 4v4h-4" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <span class="mt-3 flex items-baseline justify-between gap-2">
              <span class="font-num text-[26px] font-semibold leading-none tabular-nums text-player-text">{{ filterCounts['qc_review'] ?? 0 }}</span>
              <span class="rounded bg-primary-100 px-1.5 py-0.5 text-micro text-primary-300">人脸向量比对</span>
            </span>
            <span class="mt-1.5 block text-caption text-player-hint">512-dim 骨骼与一致性</span>
          </button>
          <!-- 已成功 -->
          <button
            type="button"
            class="gd-card-dark flex cursor-pointer flex-col justify-between p-4 text-left"
            :class="gaugeCardClass('succeeded', '')"
            :aria-pressed="statusFilter === 'succeeded'"
            @click="toggleStatusFilter('succeeded')"
          >
            <span class="flex items-center justify-between">
              <span class="text-caption text-player-hint">已成功 (Settled)</span>
              <svg class="h-4 w-4 text-success-bright" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <span class="mt-3 flex items-baseline justify-between gap-2">
              <span class="font-num text-[26px] font-semibold leading-none tabular-nums text-success-bright">{{ filterCounts['succeeded'] ?? 0 }}</span>
              <span class="gd-num text-micro text-player-hint">均耗 {{ avgSettledCredits ?? '—' }} 积分</span>
            </span>
            <span class="mt-1.5 block text-caption text-player-hint">资产就绪已入库</span>
          </button>
          <!-- 重试中 -->
          <button
            type="button"
            class="gd-card-dark flex cursor-pointer flex-col justify-between p-4 text-left"
            :class="gaugeCardClass('retrying', '!bg-danger-bg !border-danger-border')"
            :aria-pressed="statusFilter === 'retrying'"
            @click="toggleStatusFilter('retrying')"
          >
            <span class="flex items-center justify-between">
              <span class="text-caption text-player-hint">重试中 (Retrying)</span>
              <svg class="h-4 w-4 text-danger-bright" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <path d="M20 12a8 8 0 11-2.34-5.66M20 4v4h-4" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <span class="mt-3 flex items-baseline justify-between gap-2">
              <span class="font-num text-[26px] font-semibold leading-none tabular-nums text-danger-bright">{{ filterCounts['retrying'] ?? 0 }}</span>
              <span class="rounded bg-danger-bg px-1.5 py-0.5 text-micro text-danger-bright">自动回退</span>
            </span>
            <span class="mt-1.5 block text-caption text-player-hint">超时 / 限流捕获 · 不重复计费</span>
          </button>
          <!-- 待人工 -->
          <button
            type="button"
            class="gd-card-dark flex cursor-pointer flex-col justify-between p-4 text-left"
            :class="gaugeCardClass('manual_required', '!bg-primary-100 !border-primary-200 !shadow-gd-bloom')"
            :aria-pressed="statusFilter === 'manual_required'"
            @click="toggleStatusFilter('manual_required')"
          >
            <span class="flex items-center justify-between">
              <span class="text-caption font-medium text-primary-300">待人工 (Manual)</span>
              <svg class="h-4 w-4 animate-bounce text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <path d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <span class="mt-3 flex items-baseline justify-between gap-2">
              <span class="font-num text-[26px] font-semibold leading-none tabular-nums text-primary-300">{{ filterCounts['manual_required'] ?? 0 }}</span>
              <span class="rounded-full bg-grad-primary px-2 py-0.5 text-micro font-medium text-white">需导演裁决</span>
            </span>
            <span class="mt-1.5 block text-caption text-player-hint">质检踩线或风格偏离</span>
          </button>
        </div>
      </section>

      <!-- ======== §3 渲染流水队列卡 ======== -->
      <!-- shrink-0：滚动区为固定高度 flex 容器，队列卡带 overflow-hidden 时会被压缩到 0 最小高度，需禁止收缩 -->
      <section class="gd-card-dark shrink-0 overflow-hidden" aria-label="渲染流水队列">
        <!-- 工具栏：标题 + 任务徽标 | 搜索 + 状态筛选 chips -->
        <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-b border-white/5 px-5 py-4">
          <div class="flex items-center gap-2.5">
            <svg class="h-4 w-4 text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
              <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
              <path d="M8 5v14M16 5v14" />
            </svg>
            <h2 class="text-title font-semibold text-player-text">渲染流水队列</h2>
            <span class="gd-num rounded bg-primary-100 px-2 py-0.5 text-micro text-primary-300">{{ totalCount }} TASKS</span>
            <span v-if="statusFilter || searchQuery" class="text-micro text-player-hint">筛出 {{ sortedTasks.length }} 项</span>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <div class="relative">
              <svg class="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-player-hint" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="M20.5 20.5L16.7 16.7" stroke-linecap="round" />
              </svg>
              <input
                v-model="searchQuery"
                type="search"
                aria-label="搜索渲染任务"
                placeholder="按镜头、提示词或错误搜索..."
                class="h-8 w-44 rounded-lg border border-white/10 bg-white/5 pl-8 pr-3 text-caption text-player-text placeholder:text-player-hint focus:border-primary-200 focus:outline-none sm:w-60"
              />
            </div>
            <div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="状态筛选">
              <button
                v-for="opt in RENDER_STATUS_OPTIONS"
                :key="opt.value"
                type="button"
                class="inline-flex h-7 items-center gap-1.5 rounded-full border px-3 text-caption transition-colors duration-gd-fast"
                :class="
                  statusFilter === opt.value
                    ? 'border-transparent bg-[image:var(--gd-grad-primary)] font-medium text-white shadow-gd-bloom'
                    : opt.value === 'failed' || opt.value === 'manual_required'
                      ? 'border-danger-border bg-danger-bg text-danger-bright hover:border-danger-strong'
                      : 'border-white/10 bg-white/5 text-player-hint hover:border-white/25 hover:text-player-text'
                "
                :aria-pressed="statusFilter === opt.value"
                @click="statusFilter = opt.value"
              >
                {{ opt.value === '' ? '全部' : opt.label }}
                <span class="gd-num">{{ filterCounts[opt.value] ?? 0 }}</span>
              </button>
            </div>
          </div>
        </div>

        <!-- 队列体：三态 + 表 -->
        <div class="px-5 py-4">
          <!-- 骨架屏 -->
          <div v-if="loading">
            <LoadingSkeleton variant="table" :rows="8" />
          </div>

          <!-- 错误态 -->
          <div v-else-if="loadError">
            <ErrorRetryBlock message="渲染任务列表加载失败" :description="loadError" @retry="fetchTasks" />
          </div>

          <!-- 空队列：项目还没有任何渲染任务（F5-01-s2 暗场虚线玻璃空态） -->
          <div v-else-if="totalCount === 0" class="rounded-2xl border border-dashed border-white/15 px-6 py-16">
            <div class="flex flex-col items-center gap-4 text-center">
              <div class="flex h-16 w-16 items-center justify-center rounded-2xl border border-white/10 bg-white/5 text-player-hint" aria-hidden="true">
                <svg class="h-7 w-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <rect x="3" y="5" width="18" height="14" rx="3" />
                  <path d="M10 9.5v5l4.5-2.5z" stroke-linejoin="round" />
                </svg>
              </div>
              <h3 class="font-serif text-title text-player-text">暂无生成任务</h3>
              <p class="max-w-[46ch] text-caption leading-relaxed text-player-hint">
                锁定分镜后，可在此批量提交渲染。冻结积分按预估预扣，失败全额返还。
              </p>
              <AppButton variant="primary" size="md" class="gd-btn-bright" @click="router.push(`/projects/${projectId}/storyboard`)">去分镜画布生成</AppButton>
            </div>
          </div>

          <!-- 小空态：筛选 / 搜索组合无结果 -->
          <div v-else-if="sortedTasks.length === 0" class="py-16 text-center">
            <p class="font-serif text-title text-player-text">该条件下暂无任务</p>
            <p class="mt-2 text-caption text-player-hint">可切换状态筛选或清空搜索关键字</p>
          </div>

          <!-- 任务表（原型 7 列栅格；窄屏塌缩为纵向堆叠） -->
          <template v-else>
            <div
              class="mb-1 hidden items-center gap-x-4 px-3 py-2 text-caption text-player-hint md:grid md:grid-cols-[minmax(190px,1.35fr)_minmax(170px,1.25fr)_minmax(130px,0.95fr)_minmax(150px,1.05fr)_minmax(130px,0.95fr)_104px_136px] md:items-center"
              aria-hidden="true"
            >
              <span>镜头编号 &amp; 缩略图</span>
              <span>提示词锚点 / 任务内容</span>
              <span>任务类型 / 编号</span>
              <span>成本与结算</span>
              <span>质检 (QC)</span>
              <span>状态</span>
              <span class="text-right">实时操作</span>
            </div>
            <ul class="divide-y divide-white/5" role="list">
              <li v-for="row in sortedTasks" :key="row.id" class="transition-colors" :class="statusRowClass(row.status)">
                <div class="flex flex-col gap-2 px-3 py-3.5 md:grid md:grid-cols-[minmax(190px,1.35fr)_minmax(170px,1.25fr)_minmax(130px,0.95fr)_minmax(150px,1.05fr)_minmax(130px,0.95fr)_104px_136px] md:items-center md:gap-x-4">
                  <!-- 1 镜头编号 & 缩略图（列表无缩略图数据源，用影片占位框） -->
                  <div class="flex items-center gap-3">
                    <div class="flex h-10 w-16 shrink-0 items-center justify-center rounded-lg border border-white/10 bg-surface-inset text-player-hint" aria-hidden="true">
                      <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                        <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
                        <path d="M10 9.5v5l4.5-2.5z" stroke-linejoin="round" />
                      </svg>
                    </div>
                    <div class="min-w-0">
                      <p class="gd-num truncate text-sm font-semibold text-player-text">{{ shotLabel(row) }} 镜</p>
                      <p class="gd-timecode">时长 {{ shotDurationLabel(row) }}</p>
                    </div>
                  </div>
                  <!-- 2 提示词锚点 / 任务内容 -->
                  <div class="min-w-0">
                    <p class="truncate text-sm font-medium text-player-text">{{ row.shot?.final_prompt || `${RENDER_TASK_TYPE_LABELS[row.task_type] ?? row.task_type}（提示词待同步）` }}</p>
                    <p class="mt-0.5 truncate text-caption text-player-hint">{{ RENDER_TASK_TYPE_LABELS[row.task_type] ?? row.task_type }} · 时长 {{ shotDurationLabel(row) }}</p>
                  </div>
                  <!-- 3 任务类型 / 编号 -->
                  <div class="min-w-0">
                    <p class="truncate text-sm text-player-text">{{ RENDER_TASK_TYPE_LABELS[row.task_type] ?? row.task_type }}</p>
                    <p class="gd-timecode">任务 #{{ row.id }}</p>
                  </div>
                  <!-- 4 成本与结算 -->
                  <div class="min-w-0">
                    <CreditAmountText :amount="row.actual_cost_credits ?? row.estimated_cost_credits" :colored="false" class="text-xs font-medium text-player-text" />
                    <div v-if="row.status === 'running'" class="gd-progress gd-progress--sm gd-progress--live mt-1 max-w-[120px]" aria-hidden="true">
                      <div class="gd-progress__bar" />
                    </div>
                    <p v-if="row.actual_cost_credits !== null" class="mt-0.5 text-caption text-player-hint">实际结算 · 预估 {{ row.estimated_cost_credits }}</p>
                    <p v-else-if="row.status === 'failed'" class="mt-0.5 text-caption text-success-bright">失败已全额返还</p>
                    <p v-else class="mt-0.5 text-caption text-player-hint">
                      冻结中<template v-if="row.retry_count > 0"> · 重试 <span class="gd-num" :class="retryClass(row)">{{ row.retry_count }}/{{ maxRetries(row) }}</span></template>
                    </p>
                  </div>
                  <!-- 5 质检 (QC) -->
                  <div class="min-w-0 text-xs leading-snug">
                    <template v-if="qcCellState(row) === 'pass'">
                      <p class="flex items-center gap-1.5">
                        <span class="gd-num font-semibold text-success-bright">PASS</span>
                        <span class="rounded bg-success-bg px-1 py-0.5 text-micro text-success-strong">质检通过</span>
                      </p>
                      <p class="mt-0.5 text-caption text-player-hint">资产就绪已入库</p>
                    </template>
                    <template v-else-if="qcCellState(row) === 'failed'">
                      <p class="flex items-center gap-1.5">
                        <span class="gd-num font-semibold text-danger-bright">FAILED</span>
                        <span class="rounded bg-danger-bg px-1 py-0.5 text-micro text-danger-bright">前次注销</span>
                      </p>
                      <p class="mt-0.5 truncate text-caption text-player-hint">{{ row.failure_type ? (FAILURE_TYPE_LABELS[row.failure_type] ?? row.failure_type) : '失败已全额返还' }}</p>
                    </template>
                    <template v-else-if="qcCellState(row) === 'borderline'">
                      <p class="flex items-center gap-1.5">
                        <span class="rounded bg-warning-bg px-1.5 py-0.5 text-micro text-warning-bright">踩线待裁决</span>
                      </p>
                      <p class="mt-0.5 text-caption text-player-hint">需人工判决 · 见裁决台</p>
                    </template>
                    <template v-else-if="qcCellState(row) === 'reviewing'">
                      <p class="text-caption italic text-player-hint">待帧流汇编后评估...</p>
                    </template>
                    <template v-else>
                      <p class="text-caption text-player-hint">—</p>
                    </template>
                  </div>
                  <!-- 6 状态 -->
                  <div>
                    <StatusTag :status="row.status" size="sm" variant="dark" />
                  </div>
                  <!-- 7 实时操作 -->
                  <div class="flex items-center gap-1.5 md:justify-end" @click.stop>
                    <template v-if="isFailedRow(row)">
                      <AppButton variant="primary" size="sm" class="gd-btn-bright" :loading="busyTaskId === row.id" @click="openRetryDialog(row)">重试</AppButton>
                      <AppButton variant="secondary" size="sm" @click="openDetail(row)">详情</AppButton>
                    </template>
                    <template v-else>
                      <AppButton variant="secondary" size="sm" @click="openDetail(row)">详情</AppButton>
                      <AppButton
                        v-if="row.status === 'queued' || row.status === 'pending'"
                        variant="ghost"
                        size="sm"
                        class="!text-player-hint hover:!bg-white/10"
                        :loading="busyTaskId === row.id"
                        @click="openCancelDialog(row)"
                      >
                        取消
                      </AppButton>
                    </template>
                  </div>
                </div>

                <!-- 失败展开卡（红色玻璃：失败类型 + 原因 + 一键重试 + 全额返还标注） -->
                <div v-if="isFailedRow(row)" class="mx-3 mb-4 rounded-lg border border-danger-border bg-surface-inset px-4 py-3.5">
                  <div class="flex flex-wrap items-center gap-2">
                    <svg class="h-4 w-4 shrink-0 text-danger-bright" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                      <path d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                    <span class="text-sm font-semibold text-danger-bright">生成失败 · 建议重试</span>
                    <span v-if="row.failure_type" class="rounded-full bg-danger-bg px-2 py-0.5 text-caption font-medium text-danger-bright">
                      {{ FAILURE_TYPE_LABELS[row.failure_type] ?? row.failure_type }}
                    </span>
                  </div>
                  <p class="mt-1.5 break-all text-xs leading-relaxed text-player-hint">
                    {{ row.failure_reason || '画面生成超时：该镜头未超过时长上限，常见于复杂运镜。已自动返还全部冻结积分，重试不重复计费。' }}
                  </p>
                  <div class="mt-3 flex flex-wrap items-center gap-2">
                    <AppButton variant="primary" size="sm" class="gd-btn-bright" :loading="busyTaskId === row.id" @click="openRetryDialog(row)">一键重试</AppButton>
                    <AppButton variant="ghost" size="sm" class="!text-player-hint hover:!bg-white/10" @click="openDetail(row)">查看详情</AppButton>
                    <span class="ml-auto inline-flex items-center gap-1.5 text-caption text-success-bright">
                      <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                        <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
                      </svg>
                      失败全额返还 · 不重复计费
                    </span>
                  </div>
                </div>
              </li>
            </ul>
          </template>
        </div>
      </section>

      <!-- ======== §4 人工异常裁决台（首个失败 / 需人工任务） ======== -->
      <section v-if="queueReady && adjudicationTask" class="gd-card-dark p-5" aria-label="人工异常裁决台">
        <!-- 头部：裁决标题 + FAILED 徽标 + 冻结状态 -->
        <div class="flex flex-col gap-3 pb-4 md:flex-row md:items-center md:justify-between">
          <div class="flex min-w-0 items-center gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary-100 text-primary-300 shadow-gd-bloom" aria-hidden="true">
              <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="8.5" />
                <path d="M8.5 12.5l2.5 2.5 4.5-5" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </div>
            <div class="min-w-0">
              <h2 class="flex flex-wrap items-center gap-2 text-title font-semibold text-player-text">
                <span>人工异常裁决台 · {{ shotLabel(adjudicationTask) }} 镜</span>
                <span class="gd-num rounded-full bg-danger-bg px-2 py-0.5 text-micro text-danger-bright">
                  <template v-if="adjudicationTask.status === 'manual_required'">Face Consistency Check FAILED<template v-if="adjudicationScore !== null"> ({{ adjudicationScore.toFixed(3) }})</template></template>
                  <template v-else>QC Check FAILED · {{ adjudicationTask.failure_type ? (FAILURE_TYPE_LABELS[adjudicationTask.failure_type] ?? adjudicationTask.failure_type) : '生成失败' }}</template>
                </span>
              </h2>
              <p class="mt-0.5 text-caption text-player-hint">遵循 PRD SF-F5-11 规范：失败任务已自动返还冻结积分；重试重新入队并按预估冻结，日志永久存证。</p>
            </div>
          </div>
          <div class="flex shrink-0 items-center gap-2">
            <span class="gd-num text-caption text-player-hint">待决状态: 冻结 {{ adjudicationTask.estimated_cost_credits }} 积分</span>
            <span class="gd-dot gd-dot--violet" aria-hidden="true" />
          </div>
        </div>
        <!-- 三栏：基准真源 / 本轮生成结果 / 裁决操作与审计 -->
        <div class="grid grid-cols-1 gap-4 lg:grid-cols-12">
          <!-- 基准真源（无基准面容图数据源：以 final_prompt 锚点块承载） -->
          <div class="rounded-xl border border-white/10 bg-white/[0.03] p-4 lg:col-span-4">
            <div class="mb-2 flex items-center justify-between gap-2">
              <span class="text-kicker uppercase tracking-[0.14em] text-primary-300">基准真源 (Ground Truth)</span>
              <span class="gd-timecode">Shot {{ adjudicationTask.shot_id ?? '—' }}</span>
            </div>
            <div class="relative flex aspect-[16/10] flex-col overflow-hidden rounded-lg border border-white/10 bg-surface-inset p-3">
              <p class="text-micro uppercase tracking-[0.14em] text-player-hint">Final Prompt Anchor</p>
              <p class="mt-2 flex-1 overflow-hidden break-all font-mono text-caption leading-relaxed text-player-hint">{{ adjudicationTask.shot?.final_prompt || '该镜头暂无最终提示词锚点' }}</p>
              <span class="gd-num self-start rounded bg-black/50 px-1.5 py-0.5 text-micro text-primary-300">Prompt Vector: Locked</span>
            </div>
            <dl class="mt-3 space-y-1 text-caption text-player-hint">
              <div class="flex justify-between gap-2"><dt>镜头时长:</dt><dd class="gd-num text-player-text">{{ shotDurationLabel(adjudicationTask) }}</dd></div>
              <div class="flex justify-between gap-2"><dt>提交时间:</dt><dd class="gd-num text-player-text">{{ formatRelativeTime(adjudicationTask.created_at ?? '') }}</dd></div>
            </dl>
          </div>
          <!-- 本轮生成结果（图像产物直出或占位） -->
          <div class="rounded-xl border border-white/10 bg-white/[0.03] p-4 lg:col-span-5">
            <div class="mb-2 flex items-center justify-between gap-2">
              <span class="text-kicker uppercase tracking-[0.14em] text-primary-300">本轮生成结果 (Candidate Frame)</span>
              <span class="gd-num text-micro" :class="adjudicationScore === null ? 'text-player-hint' : 'text-danger-bright'">得分 {{ adjudicationScore === null ? '—' : adjudicationScore.toFixed(3) }}</span>
            </div>
            <div class="relative flex aspect-[16/10] items-center justify-center overflow-hidden rounded-lg border border-white/10 bg-surface-inset">
              <img v-if="adjudicationFrame.url" :src="adjudicationFrame.url" alt="失败任务候选帧预览" class="h-full w-full object-cover" />
              <svg v-else class="h-6 w-6 text-player-hint" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
                <circle cx="9" cy="10" r="1.5" />
                <path d="M20.5 15.5L15 10l-8.5 8.5" />
              </svg>
              <span class="gd-num absolute bottom-2 right-2 rounded bg-black/50 px-1.5 py-0.5 text-micro text-player-text">任务 #{{ adjudicationTask.id }}</span>
            </div>
            <p class="mt-3 flex items-start justify-between gap-3 text-caption">
              <span class="shrink-0 text-player-hint">质检系统诊断简报:</span>
              <span class="text-right leading-relaxed text-primary-300">{{ adjudicationTask.failure_reason || '画面生成未达标，已自动返还冻结积分，可重试重新入队。' }}</span>
            </p>
          </div>
          <!-- 裁决操作与审计留痕（原型判决意见 textarea 无提交端点：以失败原因只读块承载） -->
          <div class="flex flex-col rounded-xl border border-white/10 bg-white/[0.03] p-4 lg:col-span-3">
            <p class="text-caption font-semibold text-player-text">裁决操作与审计留痕</p>
            <div class="mt-2 rounded-lg bg-white/5 p-2.5 text-caption leading-relaxed text-player-hint">
              <span class="font-semibold text-danger-bright">⚠ 审计机制提示：</span>失败任务已自动返还全部冻结积分，重试不重复计费；重试将按预估重新冻结并入队。
            </div>
            <div class="mt-2.5">
              <p class="text-micro text-player-hint">失败原因 (failure_reason)</p>
              <p class="mt-1 break-all rounded-lg border border-white/10 bg-surface-inset p-2.5 font-mono text-caption leading-relaxed text-player-hint">
                {{ adjudicationTask.failure_reason || (FAILURE_TYPE_LABELS[adjudicationTask.failure_type ?? ''] ?? '—') }}
              </p>
            </div>
            <div class="mt-3 flex flex-col gap-2">
              <AppButton variant="primary" size="md" class="gd-btn-bright" :loading="busyTaskId === adjudicationTask.id" @click="openRetryDialog(adjudicationTask)">重新入队（冻结 {{ adjudicationTask.estimated_cost_credits }} 积分）</AppButton>
              <AppButton variant="secondary" size="sm" @click="openDetail(adjudicationTask)">查看质检报告与详情</AppButton>
              <span class="inline-flex items-center justify-center gap-1.5 text-caption text-success-bright">
                <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                失败全额返还 · 不重复计费
              </span>
            </div>
          </div>
        </div>
      </section>

      <!-- ======== §5 成片版本选择与锁定（首个已成功任务的产物分支） ======== -->
      <section v-if="queueReady && versionTask" class="gd-card-dark flex flex-col gap-4 p-5" aria-label="成片版本选择与锁定">
        <div class="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
          <div class="flex min-w-0 items-center gap-3">
            <div class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary-100 text-primary-300" aria-hidden="true">
              <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <path d="M12 3.5l8.5 4.5-8.5 4.5L3.5 8 12 3.5z" stroke-linejoin="round" />
                <path d="M3.5 13l8.5 4.5L20.5 13" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </div>
            <div class="min-w-0">
              <h2 class="truncate text-title font-semibold text-player-text">成片版本选择与锁定器 · 当前查看: {{ shotLabel(versionTask) }} 镜</h2>
              <p class="mt-0.5 text-caption text-player-hint">同任务并行产物分支，已采用版本进入下游后期管线</p>
            </div>
          </div>
          <span class="inline-flex shrink-0 items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-caption text-player-hint">
            <svg class="h-3.5 w-3.5 text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
              <circle cx="12" cy="12" r="8.5" />
              <path d="M10 9l5 3-5 3V9z" stroke-linejoin="round" />
            </svg>
            共 {{ versionDetail?.outputs.length ?? 0 }} 个产物 · 已采用 {{ selectedOutputCount }}
          </span>
        </div>
        <!-- 三版本卡并排（A/B/C 槽位；已采用 = 紫描边 + 徽标） -->
        <div class="grid grid-cols-1 gap-4 md:grid-cols-3">
          <article
            v-for="(version, index) in versionSlots"
            :key="version.label"
            class="relative flex flex-col rounded-xl border p-4 transition-colors duration-gd-fast"
            :class="version.output?.is_selected ? 'border-primary-200 bg-primary-50 shadow-gd-bloom' : 'border-white/10 bg-white/[0.03]'"
          >
            <span v-if="version.output?.is_selected" class="gd-num absolute right-3 top-3 rounded-full bg-grad-primary px-2 py-0.5 text-micro font-semibold text-white shadow-gd-bloom">当前首选 ✓</span>
            <div class="min-w-0">
              <h3 class="truncate text-sm font-semibold text-player-text">版本 {{ version.label }} · {{ version.output ? (OUTPUT_TYPE_LABELS[version.output.output_type] ?? version.output.output_type) : '待生成' }}</h3>
              <p class="gd-timecode mt-0.5">任务 #{{ versionTask.id }} · 产物 {{ index + 1 }}</p>
              <div class="relative mt-3 flex aspect-video items-center justify-center overflow-hidden rounded-lg border border-white/10 bg-surface-inset">
                <img v-if="version.imageUrl" :src="version.imageUrl" :alt="`版本 ${version.label} 产物预览`" class="h-full w-full object-cover" />
                <svg v-else class="h-6 w-6 text-player-hint" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                  <rect x="3.5" y="5" width="17" height="14" rx="2.5" />
                  <circle cx="9" cy="10" r="1.5" />
                  <path d="M20.5 15.5L15 10l-8.5 8.5" />
                </svg>
                <span v-if="version.note" class="absolute bottom-1.5 left-2 rounded bg-black/50 px-1.5 py-0.5 text-micro text-player-hint">{{ version.note }}</span>
                <span v-else-if="version.output" class="gd-num absolute bottom-1.5 left-2 rounded bg-black/50 px-1.5 py-0.5 text-micro" :class="version.output.is_selected ? 'text-success-bright' : 'text-player-hint'">
                  {{ version.output.is_selected ? 'QC 已采用' : '备选分支' }}
                </span>
              </div>
            </div>
            <div class="mt-4 flex items-center justify-between gap-2">
              <span class="text-caption" :class="version.output?.is_selected ? 'text-primary-300' : 'text-player-hint'">{{ version.output?.is_selected ? '已锁定为默认输出' : '备选分支' }}</span>
              <AppButton v-if="version.output?.is_selected" variant="primary" size="sm" class="gd-btn-bright" disabled>已锁定</AppButton>
              <AppButton v-else-if="version.output" variant="secondary" size="sm" @click="openDetail(versionTask)">查看详情</AppButton>
            </div>
          </article>
        </div>
      </section>

      <!-- ======== §6 底部批次收口行 ======== -->
      <section v-if="queueReady" class="gd-card-dark flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between" aria-label="批次收口">
        <div class="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div class="flex items-center gap-2">
            <span class="gd-dot gd-dot--violet animate-pulse" aria-hidden="true" />
            <span class="text-caption font-medium text-player-text">批次就绪状态:</span>
            <span class="gd-num text-caption font-semibold text-primary-300">{{ succeededCount }} / {{ totalCount }} 镜已就绪 ({{ completionPct }}%)</span>
          </div>
          <span class="hidden items-center gap-1.5 text-caption md:inline-flex" :class="readyForPost ? 'text-success-bright' : 'text-player-hint'">
            <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            {{ readyForPost ? '全量达标 · 可流转 P6 后期合成' : '全量达标后自动触发 P6 后期合成流转' }}
          </span>
        </div>
        <div class="flex items-center justify-end gap-2">
          <AppButton variant="ghost" size="md" disabled class="!text-player-hint" title="预留操作位：全批质检简报导出暂未开放">导出全批质检简报 (PDF/JSON)</AppButton>
          <AppButton
            variant="primary"
            size="md"
            class="gd-btn-bright"
            :disabled="!readyForPost"
            :title="readyForPost ? '' : '仍有未完成镜头，全量达标后可进入 P6 合成'"
            @click="router.push(`/projects/${projectId}/post`)"
          >
            锁定当前已完成镜头并进入 P6 合成 →
          </AppButton>
        </div>
      </section>
    </div>

    <!-- ============ 详情抽屉 ============ -->
    <DrawerPanel v-model="drawerOpen" title="任务详情" :width="480">
      <div v-if="detailLoading" class="space-y-3">
        <LoadingSkeleton variant="text" :rows="6" />
      </div>
      <div v-else-if="detail" class="space-y-5">
        <!-- 基本信息 -->
        <section class="gd-card-flat p-4">
          <div class="flex items-center justify-between">
            <span class="gd-num text-sm font-semibold text-neutral-900">任务 #{{ detail.id }}</span>
            <StatusTag :status="detail.status" size="sm" />
          </div>
          <dl class="mt-3 space-y-2 text-xs">
            <div class="flex justify-between">
              <dt class="text-neutral-400">类型 / 模型</dt>
              <dd class="text-neutral-700">
                {{ RENDER_TASK_TYPE_LABELS[detail.task_type] ?? detail.task_type }}
                <span v-if="detail.model_key" class="gd-num ml-1 text-neutral-400">{{ detail.model_key }}</span>
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-neutral-400">镜头</dt>
              <dd class="gd-num text-neutral-700">
                <template v-if="detail.shot">S{{ detail.shot.shot_no }} · {{ (detail.shot.duration_ms / 1000).toFixed(1) }}s · {{ detail.shot.status }}</template>
                <template v-else>—</template>
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-neutral-400">重试次数</dt>
              <dd class="gd-num" :class="retryClass({ ...detail, shot: detail.shot } as RenderTaskListItem)">{{ detail.retry_count }}/{{ detail.max_retries }}</dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-neutral-400">耗时</dt>
              <dd class="gd-num text-neutral-700">{{ durationLabel(detail) }}</dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-neutral-400">创建时间</dt>
              <dd class="gd-num text-neutral-700">{{ formatRelativeTime(detail.created_at) }}</dd>
            </div>
          </dl>
        </section>

        <!-- 成本与冻结结算 -->
        <section class="gd-card-flat p-4">
          <h4 class="text-xs font-semibold text-neutral-500">成本与结算</h4>
          <dl class="mt-3 space-y-2 text-xs">
            <div class="flex justify-between">
              <dt class="text-neutral-400">预估 → 实际</dt>
              <dd class="flex items-center gap-1.5">
                <CreditAmountText :amount="detail.estimated_cost_credits" :colored="false" />
                <span class="text-neutral-400">→</span>
                <CreditAmountText v-if="detail.actual_cost_credits !== null" :amount="detail.actual_cost_credits" />
                <span v-else class="text-neutral-400">待结算</span>
              </dd>
            </div>
            <template v-if="detail.freeze">
              <div class="flex justify-between">
                <dt class="text-neutral-400">冻结状态</dt>
                <dd>
                  <span
                    class="rounded-full px-2 py-0.5 text-caption font-medium"
                    :class="detail.freeze.status === 'settled' ? 'bg-success-bg text-success' : detail.freeze.status === 'frozen' ? 'bg-primary-50 text-primary-600' : 'bg-surface-sunken text-neutral-500'"
                  >
                    {{ FREEZE_STATUS_LABELS[detail.freeze.status] ?? detail.freeze.status }}
                  </span>
                </dd>
              </div>
              <div class="flex justify-between">
                <dt class="text-neutral-400">冻结积分</dt>
                <dd class="gd-num text-neutral-700">{{ detail.freeze.frozen_credits }}</dd>
              </div>
              <div v-if="detail.freeze.settled_credits !== null" class="flex justify-between">
                <dt class="text-neutral-400">已结算积分</dt>
                <dd class="gd-num text-neutral-700">{{ detail.freeze.settled_credits }}</dd>
              </div>
            </template>
          </dl>
        </section>

        <!-- 提示词（折叠代码块） -->
        <section v-if="detail.shot?.final_prompt" class="gd-card-flat p-4">
          <button type="button" class="flex w-full items-center justify-between text-left text-xs font-semibold text-neutral-500" @click="promptExpanded = !promptExpanded">
            输入提示词（final_prompt）
            <span class="text-neutral-400">{{ promptExpanded ? '收起 ▲' : '展开 ▼' }}</span>
          </button>
          <pre
            v-if="promptExpanded"
            class="mt-2.5 max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-md border border-hairline bg-surface-sunken px-3 py-2.5 font-mono text-caption leading-relaxed text-neutral-800"
          >{{ detail.shot.final_prompt }}</pre>
        </section>

        <!-- 渲染产物 -->
        <section class="gd-card-flat p-4">
          <h4 class="text-xs font-semibold text-neutral-500">渲染产物（{{ detail.outputs.length }}）</h4>
          <ul v-if="detail.outputs.length > 0" class="mt-3 space-y-2">
            <li v-for="output in detail.outputs" :key="output.id" class="rounded-md bg-surface-canvas px-3 py-2 text-xs">
              <div class="flex items-center gap-2">
                <span class="rounded bg-surface-inset px-1.5 py-0.5 text-caption text-neutral-600">{{ output.output_type }}</span>
                <a
                  v-if="isPlayableVideo(output)"
                  :href="outputUrl(output)"
                  target="_blank"
                  rel="noopener"
                  class="gd-num truncate text-primary hover:underline"
                >{{ outputUrl(output) }}</a>
                <span v-else class="gd-num truncate text-neutral-600">{{ outputUrl(output) }}</span>
                <span v-if="output.is_selected" class="ml-auto rounded-full bg-success-bg px-1.5 py-0.5 text-caption text-success">已采用</span>
              </div>
              <!-- 生成视频可播放修复：MP4 产物直接内嵌播放器，旧 JSON 占位不渲染播放器 -->
              <video
                v-if="isPlayableVideo(output)"
                :src="outputUrl(output)"
                controls
                preload="metadata"
                class="mt-2 w-full max-w-sm rounded-md border border-hairline bg-black"
              />
              <p v-else-if="output.output_type === 'video'" class="mt-1 text-caption text-gd-pending">
                历史占位产物（非视频文件），请重新生成该镜头
              </p>
            </li>
          </ul>
          <p v-else class="mt-2 text-xs text-neutral-400">任务完成前暂无产物</p>
        </section>

        <!-- QC 报告分数条（质检报告卡：多轮分数 + 通过/告警/未通过） -->
        <section v-if="detail.qc_reports.length > 0" class="gd-card-flat p-4">
          <h4 class="text-xs font-semibold text-neutral-500">质检报告（{{ detail.qc_reports.length }} 轮）</h4>
          <ul class="mt-3 space-y-3">
            <li v-for="report in detail.qc_reports" :key="report.id" class="text-xs">
              <div class="flex items-center justify-between">
                <span class="text-neutral-600">第 {{ report.round_no }} 轮 · {{ report.result === 'passed' ? '通过' : report.result === 'warning' ? '告警' : '未通过' }}</span>
                <span v-if="report.consistency_score !== null" class="gd-num font-medium" :class="report.consistency_score >= 0.8 ? 'text-success' : report.consistency_score >= 0.6 ? 'text-gd-pending' : 'text-danger'">
                  {{ (report.consistency_score * 100).toFixed(0) }} 分
                </span>
              </div>
              <div v-if="report.consistency_score !== null" class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-[var(--gd-progress-track)]">
                <div
                  class="h-full rounded-full transition-[width] duration-gd-slow ease-gd-out"
                  :class="report.consistency_score >= 0.8 ? 'bg-success' : report.consistency_score >= 0.6 ? 'bg-warning' : 'bg-danger'"
                  :style="{ width: `${Math.min(100, report.consistency_score * 100)}%` }"
                />
              </div>
            </li>
          </ul>
        </section>

        <!-- 错误详情 -->
        <!-- P0-3：降级占位产物提示（媒体提供商不可用时产出占位素材且不计费） -->
        <section v-if="detail.degraded" class="rounded-lg border border-hairline bg-warning-bg p-4">
          <p class="text-sm font-semibold text-warning-bright">降级产物 · 未计费</p>
          <p class="mt-1 text-xs leading-relaxed text-neutral-600">
            媒体提供商当前不可用，本次输出为占位素材，实际扣费 0 积分。建议在提供商恢复后重新生成。
          </p>
        </section>
        <section v-if="detail.failure_type || detail.failure_reason" class="rounded-lg border border-danger-border bg-danger-bg p-4">
          <h4 class="text-xs font-semibold text-danger">错误详情</h4>
          <p class="mt-2 text-xs text-neutral-700">
            <span class="rounded bg-surface-raised px-1.5 py-0.5 text-caption font-medium text-danger">{{ FAILURE_TYPE_LABELS[detail.failure_type ?? ''] ?? detail.failure_type }}</span>
          </p>
          <p class="mt-1.5 break-all text-xs leading-relaxed text-neutral-600">{{ detail.failure_reason || '—' }}</p>
        </section>
      </div>
    </DrawerPanel>

    <!-- ============ 批量生成估算弹窗 ============ -->
    <CostEstimateDialog
      v-model="estimateDialogOpen"
      :estimated-credits="estimateData?.total_credits ?? 0"
      :title="`批量生成 ${estimateData?.shot_count ?? 0} 个锁定镜头`"
      description="逐镜按时长 × 模型单价估算，确认后冻结积分并排队执行"
      :loading="submitting"
      @confirm="confirmBatch"
    >
      <template #detail>
        <!-- 逐镜成本明细 -->
        <div v-if="estimateData && estimateData.per_shot.length > 0" class="max-h-44 overflow-y-auto rounded-lg border border-hairline-soft">
          <ul class="divide-y divide-hairline-soft">
            <li v-for="item in estimateData.per_shot" :key="item.shot_id" class="flex items-center justify-between px-3 py-1.5 text-xs">
              <span class="gd-num text-neutral-500">镜头 #{{ item.shot_id }}</span>
              <CreditAmountText :amount="item.estimated_credits" :colored="false" class="font-medium" />
            </li>
          </ul>
        </div>
        <p class="mt-2 text-caption text-neutral-400">
          后端可用余额 {{ estimateData?.available_credits ?? 0 }} 积分 · 实际以结算为准
        </p>
        <!-- Q-016：批量提交需在后端同步合成首帧，耗时可达数十秒；提交期间明确提示，
             弹窗确认按钮在 loading 中不可重复点击，失败也不自动重试（幂等键防重放） -->
        <p v-if="submitting" class="mt-2 text-caption font-medium text-info" role="status">
          正在合成首帧，请勿重复提交或关闭页面（最长等待 120 秒）…
        </p>
      </template>
    </CostEstimateDialog>

    <!-- ============ 取消确认弹窗 ============ -->
    <ConfirmDialog
      v-model="cancelDialogOpen"
      title="取消该渲染任务？"
      :content="`任务 #${cancelTarget?.id ?? ''} 取消后将全额退款，镜头回到「已锁定」状态。`"
      confirm-label="确认取消任务"
      :loading="busyTaskId === cancelTarget?.id"
      @confirm="confirmCancel"
      @cancel="cancelTarget = null"
    />

    <!-- ============ 重试二次确认弹窗（F5-01-s3：冻结预估 + 剩余重试次数） ============ -->
    <ConfirmDialog
      v-model="retryDialogOpen"
      title="确认重试此任务？"
      :content="retryDialogContent"
      confirm-label="确认重试"
      :loading="busyTaskId === retryTarget?.id"
      @confirm="confirmRetry"
      @cancel="retryTarget = null"
    />
  </div>
</template>

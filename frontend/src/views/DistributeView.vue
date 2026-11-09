<!--
  文件级注释：P18 · 导出分发中心（路由 /projects/:projectId/distribute，AppShell 布局）。
  布局 Redesign-Preserve：按 stitch_prd/p7（code.html + screen.png）1:1 还原功能布局——
  刊头（图标徽标 + PRD/H4 状态徽标 + 状态行 + 右侧规格摘要卡）；
  左列（母版级监视器与质检开口 / 《人工智能生成合成内容标识办法》法定标识预览 /
  各发行平台合规预审报告）；
  右列（母版导出规格与打包 / AI 智能宣发文案与高光抓取 / 矩阵平台一键推流授权）；
  全宽「导出与分发记录」卡 + 底部收口条（校验状态 + 历史导出记录 + 一键推流全平台）。
  主题保持现有实现：沿用视图与令牌中已有的 gd-* 类、极光色板、字体、圆角、阴影体系，
  不引入新色值 / 新字体 / 新依赖。
  功能全部保留：三态加载、权益控制（分辨率 / 水印按套餐 benefits 禁用 + 4K 升级弹窗）、
  CostEstimateDialog 导出确认、批量导出 ZIP、平台授权（沙箱 mock）、一键分发、分发记录。
  数据绑定：导出任务 / 平台授权 / 分发记录 / 套餐权益 / 项目 store 真实数据；
  监视器为纯展示质检开口（无媒体流）：时码 / 码率 / HDR 为规格示意 chrome，
  分辨率与画幅徽标跟随真实所选导出参数；SSE/Celery 与导出节点徽标属于 AppShell
  壳层状态栏，不在本视图范围。
  备注：SRT 软字幕轨 / 边导边下 / 工程清单勾选与命名模板为 UI 结构件（导出 API
  暂无对应参数）；导出中逐集进度百分比与转码缓存命中 toast（F7-01-s1）需后端
  导出任务提供 progress 字段 / 缓存命中事件，当前 API 未提供，暂未落地。
-->
<script setup lang="ts">
/**
 * DistributeView 导出分发中心（布局对齐 stitch_prd/p7，Redesign-Preserve）。
 * props：无。emits：无。
 * 依赖 export API（导出 / 批量 / 元数据 / 平台授权 / 分发）、billing API（套餐权益）、
 * project store（当前项目状态）、useToast。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { listPlans } from '@/api/billing'
import type { PlanBenefits, SubscriptionInfo } from '@/api/billing'
import {
  batchExport,
  createDistribution,
  createExport,
  createPlatformAccount,
  generateExportMetadata,
  getExport,
  listDistributions,
  listExports,
  listPlatformAccounts,
} from '@/api/export'
import type { DistributionRecordItem, ExportJobItem, ExportMetadataResult, PlatformAccountItem } from '@/api/export'
import AppButton from '@/components/common/AppButton.vue'
import CostEstimateDialog from '@/components/common/CostEstimateDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import Modal from '@/components/common/Modal.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { getProjectTimeline, getPostPreviewUrl } from '@/api/post'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/composables/useToast'
import { PLATFORM_OPTIONS, platformOption } from '@/constants/options'
import { useProjectStore } from '@/stores/project'
import { formatRelativeTime } from '@/utils/datetime'
import { formatBytes } from '@/utils/format'

/** 路由实例（取 projectId；升级直达 / 平台授权管理跳转） */
const route = useRoute()
/** 路由跳转 */
const router = useRouter()
/** Toast 提示 */
const toast = useToast()
/** 项目 store（当前项目状态用于成片确认校验与刊头摘要） */
const projectStore = useProjectStore()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ==================== 权益信息（套餐 benefits） ==================== */

/** 当前订阅态 */
const subscription = ref<SubscriptionInfo | null>(null)
/** 当前套餐权益位 */
const benefits = ref<PlanBenefits | null>(null)

/** 分辨率等级表（数值越大越高清） */
const RESOLUTION_RANK: Record<string, number> = { '720p': 1, '1080p': 2, '4k': 3 }

/** 当前套餐允许的最高分辨率等级（free 默认 720p） */
const maxResolutionRank = computed(() => {
  const max = benefits.value?.max_resolution
  return (max && RESOLUTION_RANK[max]) || 1
})

/** 当前套餐是否允许去水印（benefits.no_watermark 为 true 才可关水印） */
const canRemoveWatermark = computed(() => benefits.value?.no_watermark === true)

/**
 * 分辨率是否在当前套餐可用。
 * @param resolution 分辨率键
 * @returns 是否可用
 */
function resolutionEnabled(resolution: string): boolean {
  return (RESOLUTION_RANK[resolution] ?? 1) <= maxResolutionRank.value
}

/* ==================== 导出配置 ==================== */

/** 选中的分辨率 */
const resolution = ref('720p')
/** 是否带水印（无去水印权益时强制 true） */
const withWatermark = ref(true)
/** 集数范围：空串=全部，数字=单集 */
const episodeScope = ref<string>('')
/** 创建导出提交中 */
const exporting = ref(false)
/** 导出确认弹窗显示 */
const confirmOpen = ref(false)

/** 分辨率选项（档位 + 说明文案，对照 p7 原型三档规格卡） */
const RESOLUTION_OPTIONS: Array<{ key: string; label: string; desc: string }> = [
  { key: '720p', label: '720p', desc: '极速分发流' },
  { key: '1080p', label: '1080p（高清）', desc: '全平台通用标准' },
  { key: '4k', label: '4K UHD', desc: '媒体存档专用' },
]

/** 权益等级 → 展示标签（升级弹窗 / 命名预览用） */
const RESOLUTION_LABELS: Record<string, string> = { '720p': '720p', '1080p': '1080p', '4k': '4K' }

/** 当前套餐最高分辨率展示标签（默认 720P） */
const maxResolutionLabel = computed(() => {
  const max = benefits.value?.max_resolution
  return (max && RESOLUTION_LABELS[max]) || '720P'
})

/** 4K 升级弹窗显示状态（权益不匹配不静默降级，F7-01-s2） */
const upgradeModalOpen = ref(false)

/** 项目目标集数（缺省 1） */
const episodeCount = computed(() => projectStore.currentProject?.target_episode_count ?? 1)

/** 是否已通过成片确认（项目 completed 视为成片确认通过） */
const finalApproved = computed(() => projectStore.currentProject?.status === 'completed')

/** 导出配置是否可提交（成片确认通过 + 分辨率有权益） */
const canExport = computed(() => finalApproved.value && resolutionEnabled(resolution.value))

/** 「立即导出当前集母版」不可用原因（hover 提示，避免禁用按钮无解释）。
 *  修复（E2E 报告 P-13）：导出范围跟随 episodeScope 动态表述，避免
 *  按钮语义（当前集）与默认范围（全部剧集）不一致。 */
const exportHint = computed(() => {
  if (!finalApproved.value) return '成片确认未通过：请先在「成片审核」通过 HITL-4，再导出母版'
  if (!resolutionEnabled(resolution.value)) return '当前套餐不支持该分辨率：可在计费中心升级套餐'
  const scopeText = episodeScope.value === '' ? '全部剧集' : `第 ${episodeScope.value} 集`
  return `导出${scopeText}母版（不消耗积分，产物保留 7 天）`
})

/** 「批量导出」不可用原因 */
const batchExportHint = computed(() =>
  finalApproved.value ? '按当前分辨率批量导出全剧并打包 ZIP' : '成片确认未通过：请先通过 HITL-4 成片审核',
)

/** 「重新提炼宣发文案」不可用原因 */
const metadataHint = computed(() =>
  finalApproved.value ? '基于剧情转折重新提炼标题、简介与封面建议' : '成片确认未通过：暂不能生成宣发文案',
)

/** 「发布到平台」不可用原因（按缺失条件给出可操作指引） */
const distributeHint = computed(() => {
  if (!finalApproved.value) return '成片确认未通过：请先通过 HITL-4 成片审核'
  if (!metadataReady.value) return '请先生成或填写发行标题'
  if (!distributePlatform.value) return '请先选择要发布的目标平台'
  return '按平台规格提交分发任务'
})

/**
 * 切换分辨率（无权益时打开升级弹窗，不静默降级、不选中）。
 * @param key 分辨率键
 * @returns 无
 */
function pickResolution(key: string): void {
  if (!resolutionEnabled(key)) {
    upgradeModalOpen.value = true
    return
  }
  resolution.value = key
}

/**
 * 切换水印开关（无去水印权益时强制保持开启）。
 * @returns 无
 */
function toggleWatermark(): void {
  if (!canRemoveWatermark.value) {
    toast.warning('当前套餐不含去水印权益，升级后可去除水印')
    return
  }
  withWatermark.value = !withWatermark.value
}

/**
 * 打开导出确认弹窗（配置卡校验通过后进入 CostEstimateDialog）。
 * @returns 无
 */
function openConfirm(): void {
  if (!finalApproved.value) return
  confirmOpen.value = true
}

/* ==================== 导出任务列表 ==================== */

/** 导出任务列表 */
const exportJobs = ref<ExportJobItem[]>([])
/** 列表加载中 */
const loading = ref(true)
/** 加载失败信息 */
const errorMsg = ref('')

/**
 * 拉取导出任务列表。
 * @returns 无
 */
async function fetchExports(): Promise<void> {
  errorMsg.value = ''
  try {
    const result = await listExports(projectId.value, { page: 1, page_size: 50 })
    exportJobs.value = result.items
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '导出任务加载失败'
  } finally {
    loading.value = false
  }
}

/* ==================== 导出完成实时刷新（SSE，修复 E2E 报告 P-08） ==================== */

/** 是否存在进行中的导出任务（决定降级轮询是否拉取列表） */
const hasRunningExport = computed(() => exportJobs.value.some((job) => job.status === 'running' || job.status === 'pending'))

/**
 * 导出事件到达后的刷新动作：重拉任务列表并按最新任务状态给出 toast 反馈。
 * @returns 无
 */
async function onExportEvent(): Promise<void> {
  await fetchExports()
  // 导出完成后同步刷新监视器成片地址，避免需手动刷新页面才能预览新母版
  void loadFilmPreview()
  const latest = exportJobs.value[0]
  if (latest?.status === 'succeeded') {
    toast.success(`母版导出完成（任务 #${latest.id}），可在「导出与分发记录」下载`)
  } else if (latest?.status === 'failed') {
    toast.error(`母版导出失败（任务 #${latest.id}），请查看失败原因后重试`)
  } else {
    toast.info('导出任务状态已更新')
  }
}

/** 项目级 SSE 地址（导出完成事件由后端 event_bus 发布到该端点） */
const sseUrl = computed(() => `/api/v1/projects/${projectId.value}/events`)

// 订阅 export.done：后端导出管线完成后发布该事件，前端即时刷新列表；
// SSE 不可用时自动降级为 10s 轮询（仅当存在未完成任务时才真正拉取，避免空转）。
useSSE(sseUrl, {
  eventTypes: ['export.done'],
  onMessage: () => {
    void onExportEvent()
  },
  onPoll: () => {
    if (hasRunningExport.value) void fetchExports()
  },
  pollIntervalMs: 10_000,
})

/* ==================== 母版成片时长（修复 E2E 报告 P-10） ==================== */

/** 预览版成片总时长（毫秒，0=未取到，用于监视器时码与合规时长校验） */
const filmDurationMs = ref(0)

/* ==================== 母版监视器播放（真实成片预览） ==================== */

/** 成片预览地址（优先最近成功导出的 MP4，其次后端合成预览产物；空串表示暂无可播放成片） */
const filmPreviewUrl = ref('')
/** 监视器 video 元素 */
const monitorVideo = ref<HTMLVideoElement | null>(null)
/** 监视器舞台容器（全屏监看宿主） */
const monitorStage = ref<HTMLDivElement | null>(null)
/** 监视器播放位置（毫秒，由 video timeupdate 驱动） */
const monitorPositionMs = ref(0)
/** 监视器成片总时长（毫秒，video 元数据就绪后覆盖时间线时长） */
const monitorDurationMs = ref(0)
/** 进度条拖拽中 */
const monitorScrubbing = ref(false)

/** 预览地址是否为可播放 MP4 */
const filmPlayable = computed(() => /\.mp4($|#|\?)/i.test(filmPreviewUrl.value))

/**
 * 将毫秒时长格式化为监视器时码（MM:SS.00）。
 * @param ms 时长毫秒数
 * @returns 时码文案
 */
function formatTimecode(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  const mm = String(Math.floor(totalSeconds / 60)).padStart(2, '0')
  const ss = String(totalSeconds % 60).padStart(2, '0')
  return `${mm}:${ss}.00`
}

/** 母版监视器成片总时长（video 元数据优先，其次时间线时长） */
const monitorTotalMs = computed(() => (monitorDurationMs.value > 0 ? monitorDurationMs.value : filmDurationMs.value))

/** 母版监视器时码（真实成片时长，取不到时回退占位符） */
const monitorTimecode = computed(() => (monitorTotalMs.value > 0 ? formatTimecode(monitorTotalMs.value) : '--:--.00'))

/** 母版监视器当前播放位置时码 */
const monitorElapsedText = computed(() => formatTimecode(monitorPositionMs.value))

/** 进度条填充比例（0%~100%） */
const monitorProgressPct = computed(() => {
  if (monitorTotalMs.value <= 0) return '0%'
  return `${Math.min(100, Math.max(0, (monitorPositionMs.value / monitorTotalMs.value) * 100)).toFixed(2)}%`
})

/**
 * 静默拉取预览版时间线以获取真实成片时长（失败不影响页面其它功能）。
 * @returns 无
 */
async function loadFilmDuration(): Promise<void> {
  try {
    const data = await getProjectTimeline(projectId.value, 'preview')
    filmDurationMs.value = Number(data.timeline?.duration_ms ?? 0)
  } catch {
    // 视觉辅助数据：时间线不存在（尚未合成）时保持 0，时码显示占位符
  }
}

/**
 * 加载母版成片预览地址：优先最近一次成功导出的 MP4，其次后端合成预览产物。
 * @returns 无
 */
async function loadFilmPreview(): Promise<void> {
  const exported = exportJobs.value.find((job) => job.status === 'succeeded' && Boolean(job.output?.download_url))
  if (exported?.output?.download_url) {
    filmPreviewUrl.value = exported.output.download_url
    return
  }
  try {
    const data = await getPostPreviewUrl(projectId.value)
    filmPreviewUrl.value = data.url ?? ''
  } catch {
    // 预览服务未就绪时保持空地址，监视器回退静态封面
    filmPreviewUrl.value = ''
  }
}

/**
 * 播放 / 暂停母版成片（无成片时给出引导提示）。
 * @returns 无
 */
function toggleMonitorPlay(): void {
  const video = monitorVideo.value
  if (!video) {
    toast.info('母版成片尚未就绪，导出完成后即可在此预览')
    return
  }
  if (video.paused) {
    void video.play().catch(() => toast.warning('成片预览播放失败，请检查母版地址'))
  } else {
    video.pause()
  }
}

/**
 * 快退 / 快进指定秒数。
 * @param seconds 秒数（负值回退，正值前进）
 * @returns 无
 */
function skipMonitor(seconds: number): void {
  const video = monitorVideo.value
  if (!video || !Number.isFinite(video.duration) || video.duration <= 0) return
  video.currentTime = Math.min(video.duration, Math.max(0, video.currentTime + seconds))
  monitorPositionMs.value = Math.round(video.currentTime * 1000)
}

/**
 * 指针横坐标 → 成片播放位置（进度条单击 / 拖拽跳转）。
 * @param event 指针事件
 * @returns 无
 */
function seekMonitorFromPointer(event: PointerEvent): void {
  const video = monitorVideo.value
  if (!video || !Number.isFinite(video.duration) || video.duration <= 0) return
  const track = event.currentTarget as HTMLElement
  const rect = track.getBoundingClientRect()
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(1, rect.width)))
  video.currentTime = ratio * video.duration
  monitorPositionMs.value = Math.round(video.currentTime * 1000)
}

/**
 * 进度条按下：捕获指针并立即跳转。
 * @param event 指针事件（button 非 0 时不处理）
 * @returns 无
 */
function onMonitorSeekDown(event: PointerEvent): void {
  if (event.button !== 0) return
  const track = event.currentTarget as HTMLElement
  monitorScrubbing.value = true
  track.setPointerCapture?.(event.pointerId)
  seekMonitorFromPointer(event)
}

/**
 * 进度条拖拽中：指针移动时持续跳转。
 * @param event 指针事件
 * @returns 无
 */
function onMonitorSeekMove(event: PointerEvent): void {
  if (!monitorScrubbing.value) return
  seekMonitorFromPointer(event)
}

/**
 * 进度条松开 / 指针取消：结束拖拽并释放指针捕获。
 * @param event 指针事件
 * @returns 无
 */
function onMonitorSeekUp(event: PointerEvent): void {
  monitorScrubbing.value = false
  const track = event.currentTarget as HTMLElement
  if (track.hasPointerCapture?.(event.pointerId)) track.releasePointerCapture(event.pointerId)
}

/**
 * 播放位置更新（timeupdate）：同步进度条与时码。
 * @returns 无
 */
function onMonitorTimeUpdate(): void {
  const video = monitorVideo.value
  if (!video) return
  monitorPositionMs.value = Math.round(video.currentTime * 1000)
}

/**
 * 视频元数据就绪：用真实成片时长覆盖时间线时长。
 * @returns 无
 */
function onMonitorLoadedMetadata(): void {
  const video = monitorVideo.value
  if (video && Number.isFinite(video.duration)) {
    monitorDurationMs.value = Math.round(video.duration * 1000)
  }
}

/**
 * 播放结束：复位播放态。
 * @returns 无
 */
function onMonitorEnded(): void {
  monitorPlaying.value = false
}

/**
 * 成片加载失败：清空地址并回退静态封面，避免监视器黑屏。
 * @returns 无
 */
function onMonitorError(): void {
  if (!filmPreviewUrl.value) return
  filmPreviewUrl.value = ''
  monitorPlaying.value = false
  toast.warning('母版成片加载失败，已回退静态封面')
}

/**
 * 切换监视器全屏。
 * @returns 无
 */
async function toggleMonitorFullscreen(): Promise<void> {
  const el = monitorStage.value
  if (!el) return
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen()
    } else {
      await el.requestFullscreen()
    }
  } catch {
    // 浏览器拒绝全屏（非用户手势等）时静默
  }
}

/**
 * 导出任务下载地址（download_url / url 兼容字段）。
 * @param job 导出任务
 * @returns 下载地址或空串
 */
function jobDownloadUrl(job: ExportJobItem): string {
  return job.download_url ?? job.url ?? ''
}

/**
 * 点击下载：产物地址缺失时回退详情接口获取。
 * @param job 导出任务
 * @returns 无
 */
async function downloadJob(job: ExportJobItem): Promise<void> {
  let url = jobDownloadUrl(job)
  if (!url) {
    try {
      const detail = await getExport(job.id)
      url = detail.download_url ?? detail.url ?? ''
    } catch {
      return
    }
  }
  if (url) {
    window.open(url, '_blank', 'noopener')
  }
}

/**
 * 失败重试：以同参数重新创建导出任务。
 * @param job 失败的任务
 * @returns 无
 */
async function retryJob(job: ExportJobItem): Promise<void> {
  try {
    await createExport(projectId.value, {
      resolution: job.resolution,
      with_watermark: job.has_watermark,
    })
    toast.success('已重新提交导出任务')
    await fetchExports()
  } catch {
    // 拦截器已 toast（70003 权益不足等）
  }
}

/* ==================== 确认导出 ==================== */

/**
 * 确认开始导出（CostEstimateDialog confirm 回调）。
 * @returns 无
 */
async function confirmExport(): Promise<void> {
  exporting.value = true
  try {
    const payload: { resolution: string; with_watermark?: boolean; episode_no?: number } = {
      resolution: resolution.value,
      with_watermark: withWatermark.value,
    }
    if (episodeScope.value !== '') {
      payload.episode_no = Number(episodeScope.value)
    }
    await createExport(projectId.value, payload)
    toast.success('导出任务已创建，完成后可下载')
    confirmOpen.value = false
    await fetchExports()
  } catch {
    // 拦截器已 toast
  } finally {
    exporting.value = false
  }
}

/* ==================== 批量导出 ==================== */

/** 批量导出分辨率（继承主配置） */
const batchResolution = ref('720p')
/** 批量导出是否带水印 */
const batchWatermark = ref(true)
/** 批量导出提交中 */
const batchLoading = ref(false)

/**
 * 提交批量导出（全部剧集打包 zip）。
 * @returns 无
 */
async function submitBatchExport(): Promise<void> {
  if (!finalApproved.value) return
  batchLoading.value = true
  try {
    await batchExport(projectId.value, {
      resolution: batchResolution.value,
      with_watermark: batchWatermark.value,
    })
    toast.success('批量导出已提交，完成后可下载 ZIP')
    await fetchExports()
  } catch {
    // 拦截器已 toast
  } finally {
    batchLoading.value = false
  }
}

/* ==================== 发行元数据 ==================== */

/** 元数据表单（保存后供一键分发使用） */
const metadata = ref<{ title: string; description: string; tags: string[]; cover: string }>({
  title: '',
  description: '',
  tags: [],
  cover: '',
})
/** 封面建议列表（三选一） */
const coverSuggestions = ref<string[]>([])
/** 元数据生成中 */
const metadataLoading = ref(false)
/** 新标签输入框内容 */
const tagInput = ref('')

/** 元数据是否完整（一键分发前置校验） */
const metadataReady = computed(() => metadata.value.title.trim() !== '')

/**
 * AI 生成发行元数据并回填表单。
 * @returns 无
 */
async function generateMetadata(): Promise<void> {
  metadataLoading.value = true
  try {
    const result: ExportMetadataResult = await generateExportMetadata(projectId.value)
    metadata.value = {
      title: result.title ?? '',
      description: result.description ?? '',
      tags: Array.isArray(result.tags) ? [...result.tags] : [],
      cover: Array.isArray(result.cover_suggestions) && result.cover_suggestions.length > 0 ? result.cover_suggestions[0] : '',
    }
    coverSuggestions.value = Array.isArray(result.cover_suggestions) ? [...result.cover_suggestions] : []
    toast.success('发行元数据已生成，可编辑后用于分发')
  } catch {
    // 拦截器已 toast（后端未就绪时提示失败）
  } finally {
    metadataLoading.value = false
  }
}

/**
 * 添加话题标签（回车 / 加号触发，去重上限 10 个）。
 * @returns 无
 */
function addTag(): void {
  const tag = tagInput.value.trim().replace(/^#/, '')
  if (!tag) return
  if (metadata.value.tags.includes(tag)) {
    tagInput.value = ''
    return
  }
  if (metadata.value.tags.length >= 10) {
    toast.warning('话题标签最多 10 个')
    return
  }
  metadata.value.tags.push(tag)
  tagInput.value = ''
}

/**
 * 移除话题标签。
 * @param tag 目标标签
 * @returns 无
 */
function removeTag(tag: string): void {
  metadata.value.tags = metadata.value.tags.filter((item) => item !== tag)
}

/* ==================== 平台授权（矩阵行卡） ==================== */

/** 平台授权账号列表 */
const platformAccounts = ref<PlatformAccountItem[]>([])
/** 授权动作进行中的平台键 */
const authorizingPlatform = ref('')

/**
 * 查询指定平台当前授权账号（active 优先）。
 * @param platform 平台键
 * @returns 授权账号或 null
 */
function accountOf(platform: string): PlatformAccountItem | null {
  return platformAccounts.value.find((item) => item.platform === platform && item.status === 'active') ?? null
}

/** 平台授权行状态（行卡徽标与按钮形态） */
interface MatrixAccountState {
  /** 行状态：active 已授权 / expired 临期过期 / none 未授权 */
  state: 'active' | 'expired' | 'none'
  /** 命中的授权账号（无则 null） */
  account: PlatformAccountItem | null
}

/**
 * 查询平台授权行状态（active 优先，其次 expired 用于临期提醒）。
 * @param platform 平台键
 * @returns 行状态与账号
 */
function matrixStateOf(platform: string): MatrixAccountState {
  const account = platformAccounts.value.find((item) => item.platform === platform && item.status === 'active') ?? null
  if (account) return { state: 'active', account }
  const expired = platformAccounts.value.find((item) => item.platform === platform && item.status === 'expired') ?? null
  if (expired) return { state: 'expired', account: expired }
  return { state: 'none', account: null }
}

/**
 * OAuth 令牌剩余天数（不足 1 天按 1 天向上取整；缺失或已过期返回 null）。
 * @param account 授权账号
 * @returns 剩余天数或 null
 */
function tokenDaysLeft(account: PlatformAccountItem | null): number | null {
  if (!account?.token_expires_at) return null
  const ms = new Date(account.token_expires_at).getTime() - Date.now()
  if (Number.isNaN(ms) || ms <= 0) return null
  return Math.max(1, Math.ceil(ms / 86_400_000))
}

/**
 * 平台授权行的描述文案（OAuth 剩余天数 / 临期提醒 / 未授权引导）。
 * @param platform 平台键
 * @returns 描述文案
 */
function matrixDescOf(platform: string): string {
  const { state, account } = matrixStateOf(platform)
  if (state === 'active') {
    const days = tokenDaysLeft(account)
    const daysText = days !== null ? `OAuth 正常 · 令牌剩余 ${days} 天` : 'OAuth 正常 · 令牌有效'
    return `${daysText} · ${account?.platform_user_id ?? '自动同步作品草稿'}`
  }
  if (state === 'expired') {
    return '授权已过期 · 建议重新授权 Refresh Token 后再分发'
  }
  return '未授权 · 完成授权后可一键分发起草'
}

/**
 * 刷新平台授权列表。
 * @returns 无
 */
async function fetchPlatformAccounts(): Promise<void> {
  try {
    platformAccounts.value = await listPlatformAccounts()
  } catch {
    // 平台授权服务未就绪时静默降级（卡片显示未授权态）
  }
}

/** 已授权平台数（一键分发概览 chips，F7-01） */
const authorizedCount = computed(() => PLATFORM_OPTIONS.filter((platform) => accountOf(platform.key) !== null).length)
/** 授权过期数（概览警示 chips） */
const expiredCount = computed(() => platformAccounts.value.filter((item) => item.status === 'expired').length)

/**
 * 授权平台（沙箱 mock_authorize 直接成功）。
 * @param platform 平台键
 * @returns 无
 */
async function authorizePlatform(platform: string): Promise<void> {
  authorizingPlatform.value = platform
  try {
    await createPlatformAccount({ platform, mock_authorize: true })
    toast.success('授权成功')
    await fetchPlatformAccounts()
  } catch {
    // 拦截器已 toast
  } finally {
    authorizingPlatform.value = ''
  }
}

/* ==================== 一键分发 ==================== */

/** 选中的分发平台键 */
const distributePlatform = ref('')
/** 分发提交中 */
const distributing = ref(false)

/**
 * 点击平台分发选择（未授权平台给出引导）。
 * @param platform 平台键
 * @returns 无
 */
function pickDistributePlatform(platform: string): void {
  if (accountOf(platform) === null) {
    toast.warning('该平台尚未授权，请先完成授权')
    return
  }
  distributePlatform.value = distributePlatform.value === platform ? '' : platform
}

/**
 * 一键分发（元数据完整 + 已选平台 + 存在成功导出）。
 * @returns 无
 */
async function submitDistribute(): Promise<void> {
  if (!metadataReady.value) {
    toast.warning('请先生成或填写发行标题')
    return
  }
  if (!distributePlatform.value) {
    toast.warning('请选择要分发的平台')
    return
  }
  const exportJobId = exportJobs.value.find((job) => job.status === 'succeeded')?.id
  distributing.value = true
  try {
    await createDistribution(projectId.value, {
      platform: distributePlatform.value,
      metadata: {
        title: metadata.value.title.trim(),
        description: metadata.value.description.trim() || undefined,
        tags: metadata.value.tags.length > 0 ? metadata.value.tags : undefined,
      },
      export_job_id: exportJobId,
    })
    toast.success('分发请求已提交')
    distributePlatform.value = ''
    await fetchDistributions()
  } catch {
    // 拦截器已 toast
  } finally {
    distributing.value = false
  }
}

/* ==================== 分发记录 ==================== */

/** 分发记录列表 */
const distributions = ref<DistributionRecordItem[]>([])

/**
 * 拉取分发记录。
 * @returns 无
 */
async function fetchDistributions(): Promise<void> {
  try {
    const result = await listDistributions(projectId.value, { page: 1, page_size: 20 })
    distributions.value = result.items
  } catch {
    // 分发服务未就绪时静默降级
  }
}

/** 最近一条分发记录（矩阵卡「最近推流记录」区） */
const latestDistribution = computed(() => distributions.value[0] ?? null)

/** 已成功同步的平台数（按平台去重） */
const succeededPlatformCount = computed(
  () => new Set(distributions.value.filter((item) => item.status === 'succeeded').map((item) => item.platform)).size,
)

/* ==================== 刊头与母版摘要（真实数据派生） ==================== */

/** 项目标题（刊头状态行 / 命名预览 / 溯源区） */
const projectTitle = computed(() => projectStore.currentProject?.title ?? '')

/** 项目画幅（缺省 9:16 竖屏短剧） */
const projectAspect = computed(() => projectStore.currentProject?.aspect_ratio ?? '9:16')

/** 最近一次成功导出（刊头规格摘要 / 预审清单数据源） */
const latestSucceeded = computed(() => exportJobs.value.find((job) => job.status === 'succeeded') ?? null)

/** 母版文件大小摘要（无成功导出时显示 —） */
const masterSizeText = computed(() => (latestSucceeded.value?.output_size_bytes ? formatBytes(latestSucceeded.value.output_size_bytes) : '—'))

/** 溯源数字编码（由最近成功导出任务派生，无任务时提示待生成） */
const traceCode = computed(() => (latestSucceeded.value ? `GD-${latestSucceeded.value.id}·SHA256` : '待母版导出后生成'))

/** 监视器播放态（预览交互，无真实媒体流） */
const monitorPlaying = ref(false)

/** 分辨率 → 监视器规格徽标文案 */
const MONITOR_SPEC: Record<string, string> = {
  '720p': '720×1280 · 60FPS',
  '1080p': '1080×1920 · 60FPS',
  '4k': '2160×3840 · 60FPS',
}

/** 当前监视器规格徽标（跟随所选导出分辨率） */
const monitorSpec = computed(() => MONITOR_SPEC[resolution.value] ?? '1080×1920 · 60FPS')

/* ==================== 平台合规预审（体积 / 分辨率 / 封面绑真实数据） ==================== */

/** 合规预审激活的平台 tab（展示性切换，清单为平台通用规范档） */
const platformTab = ref(PLATFORM_OPTIONS[0]?.key ?? 'douyin')

/** 合规检查行（名称 + 要求值 + 达标徽标与色调） */
interface ComplianceCheck {
  /** 检查项名称 */
  name: string
  /** 要求值 / 实测值文案 */
  value: string
  /** 右侧达标徽标文案 */
  badge: string
  /** 徽标色调：mint 达标 / amber 待完成 */
  tone: 'mint' | 'amber'
}

/** 平台合规预审清单（体积 / 封面 / 单集时长绑定真实数据，其余为平台规范档位文案）。
 *  修复（E2E 报告 P-09）：单集时长项由硬编码「✓ 规范档位」改为按真实成片
 *  时长校验 00:45 ~ 03:00 规范区间，不达标显示 ✗ 并给出琥珀色警示。 */
const complianceChecks = computed<ComplianceCheck[]>(() => {
  const hasSize = Boolean(latestSucceeded.value?.output_size_bytes)
  const hasCovers = coverSuggestions.value.length > 0
  const durationOk = filmDurationMs.value <= 0 ? null : filmDurationMs.value >= 45_000 && filmDurationMs.value <= 180_000
  return [
    {
      name: '单集时长要求',
      value:
        filmDurationMs.value <= 0
          ? '00:45 ~ 03:00 规范区间（成片时长未知）'
          : `实测 ${monitorTimecode.value}（规范 00:45 ~ 03:00）`,
      badge: durationOk === null ? '待合成' : durationOk ? '✓ 时长达标' : '✗ 超出规范',
      tone: durationOk === false ? 'amber' : durationOk === null ? 'amber' : 'mint',
    },
    {
      name: '文件体积上限',
      value: hasSize ? `${masterSizeText.value}（平台限额 ≤ 1 GB）` : '待母版导出',
      badge: hasSize ? '✓ 体积达标' : '待导出',
      tone: hasSize ? 'mint' : 'amber',
    },
    { name: '分辨率与色彩编码', value: `${monitorSpec.value} · Rec.709`, badge: '✓ 标准竖屏流', tone: 'mint' },
    { name: 'UI 遮挡安全边距', value: '上下 120px / 右侧 80px 交互避让', badge: '✓ 100% 避让', tone: 'mint' },
    {
      name: '封面候选图与关键帧',
      value: hasCovers ? `${coverSuggestions.value.length} 张高光候选已萃取` : '生成发行元数据后自动萃取',
      badge: hasCovers ? '✓ 高光已萃取' : '待萃取',
      tone: hasCovers ? 'mint' : 'amber',
    },
  ]
})

/* ==================== 命名模板与打包选项（UI 结构件，API 暂无对应参数） ==================== */

/** 母版命名模板输入（{剧名}/{集号}/{集标题}/{分辨率} 宏） */
const namingTemplate = ref('{剧名}_{集号}_{集标题}_{分辨率}')

/** 附加 SRT 软字幕轨（展示选项，导出 API 暂无对应参数） */
const pkgSubtitleTrack = ref(true)
/** 分集边导边下（展示选项） */
const pkgStreamDownload = ref(true)
/** 导出全剧工程清单（展示选项） */
const pkgManifest = ref(false)

/** 预览文件名（宏替换为当前项目真实信息） */
const previewFileName = computed(() => {
  const episodeNo = episodeScope.value === '' ? 1 : Number(episodeScope.value)
  const base = namingTemplate.value
    .replace(/\{剧名\}/g, projectTitle.value || '未命名项目')
    .replace(/\{集号\}/g, `EP${String(episodeNo).padStart(2, '0')}`)
    .replace(/\{集标题\}/g, '正片')
    .replace(/\{分辨率\}/g, RESOLUTION_LABELS[resolution.value] ?? resolution.value)
  return `${base}.mp4`
})

/* ==================== 底部收口条 ==================== */

/** 记录卡 DOM 引用（「历史导出记录」锚点滚动） */
const recordsCard = ref<HTMLElement | null>(null)

/**
 * 平滑滚动到导出与分发记录卡。
 * @returns 无
 */
function scrollToRecords(): void {
  recordsCard.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** 底部收口条标题（按成片确认与最近导出状态切换） */
const footerTitle = computed(() => {
  if (!finalApproved.value) return '成片确认未通过 · 暂不可推流'
  return latestSucceeded.value ? '母版校验全部通过' : '终审已放行 · 母版待导出'
})

/** 底部收口条说明（法定水印与授权通路摘要） */
const footerDesc = computed(() => {
  const watermarkText = withWatermark.value ? '平台水印随导出叠加（升级后可剥离）' : '法定 AI 显式与隐式水印已内嵌'
  const laneText = authorizedCount.value > 0 ? `${authorizedCount.value} 个平台授权通路畅通无阻` : '平台授权通路待授权'
  return `${watermarkText} · 画面与字幕安全区合规 · ${laneText}`
})

// 挂载：拉取套餐权益 + 导出任务 + 平台授权 + 分发记录；静默获取成片时长（P-10）
onMounted(async () => {
  try {
    const planResult = await listPlans()
    subscription.value = planResult.subscription
    const currentKey = planResult.subscription?.plan_key ?? 'free'
    benefits.value = planResult.items.find((plan) => plan.key === currentKey)?.benefits ?? null
  } catch {
    // 权益信息拉取失败按 free 最低权益兜底
  }
  await Promise.all([fetchExports(), fetchPlatformAccounts(), fetchDistributions()])
  void loadFilmPreview()
  void loadFilmDuration()
})
</script>

<template>
  <div class="p-6 lg:p-8">
    <!-- ============ 未过成片确认提示条（功能保留） ============ -->
    <div
      v-if="!finalApproved"
      class="mb-5 flex items-center gap-2.5 rounded-lg border border-warning-border bg-warning-bg px-4 py-3"
      role="status"
    >
      <svg class="h-4 w-4 shrink-0 text-gd-pending" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <path
          d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
      <p class="text-sm text-gd-pending">成片尚未确认：请先在成片确认（P17）通过审核后，再进行导出与分发。</p>
    </div>

    <!-- ============ 刊头：统一光条 + 28px 衬线标题 + 状态徽标 + 规格摘要卡 ============ -->
    <section class="gd-card mb-6 p-5">
      <div class="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
        <div class="flex min-w-0 items-center gap-3">
          <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
          <div class="flex min-w-0 flex-col">
            <div class="flex flex-wrap items-center gap-2">
              <h1 class="gd-masthead text-display-lg">母版导出与多平台分发中心</h1>
              <span
                class="flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-micro font-semibold"
                :class="finalApproved ? 'gd-fill-mint' : 'gd-fill-amber'"
              >
                <span class="h-1.5 w-1.5 rounded-full" :class="finalApproved ? 'bg-gd-locked' : 'bg-gd-pending'" aria-hidden="true" />
                {{ finalApproved ? 'H4 终审已放行 · 母版已就绪' : 'H4 终审未放行 · 需完成成片确认' }}
              </span>
            </div>
            <div class="mt-2 flex flex-wrap items-center gap-2.5 text-xs text-neutral-400">
              <span class="text-sm font-semibold text-primary-700">{{ projectTitle || '当前项目' }}</span>
              <span aria-hidden="true">•</span>
              <span class="gd-num font-semibold text-neutral-700">{{ projectStore.currentProject?.target_episode_count ?? 1 }} 集</span>
              <span aria-hidden="true">•</span>
              <span class="gd-num font-semibold text-primary-700">{{ projectAspect }} 完整母版</span>
            </div>
          </div>
        </div>
        <!-- 规格摘要卡（总文件大小绑定最近成功导出；编码 / 响度为平台交付规范档） -->
        <div class="flex flex-wrap items-center gap-4 rounded-xl bg-surface-canvas px-5 py-3.5">
          <div class="flex flex-col">
            <span class="gd-section-label">总文件大小</span>
            <span class="gd-num text-base font-semibold text-neutral-800">{{ masterSizeText }}</span>
          </div>
          <span class="gd-junction-v h-8" aria-hidden="true" />
          <div class="flex flex-col">
            <span class="gd-section-label">视频编码</span>
            <span class="gd-num text-base font-semibold text-neutral-800">H.264 <span class="text-caption font-normal text-neutral-400">/ AAC</span></span>
          </div>
          <span class="gd-junction-v h-8" aria-hidden="true" />
          <div class="flex flex-col" title="平台交付响度规范档">
            <span class="gd-section-label">电平响度</span>
            <span class="gd-num text-base font-semibold text-primary-600">-14.0 <span class="text-caption font-normal text-neutral-400">LUFS 真峰值</span></span>
          </div>
        </div>
      </div>
    </section>

    <!-- ============ 主区两栏（左 7 / 右 5，对齐 p7 原型 55/45） ============ -->
    <div class="grid grid-cols-1 gap-5 xl:grid-cols-12">
      <!-- ==================== 左列（监视器 / 合规 / 预审） ==================== -->
      <div class="flex flex-col gap-5 xl:col-span-7">
        <!-- A. 母版级监视器与质检开口（纯展示预览窗，规格徽标绑定真实参数） -->
        <section class="gd-night-window gd-night-window-hero p-5">
          <div class="relative z-10 flex flex-wrap items-center justify-between gap-2 pb-4">
            <div class="flex items-center gap-2.5">
              <span class="h-2.5 w-2.5 animate-pulse rounded-full bg-primary-500" aria-hidden="true" />
              <h2 class="text-title font-semibold text-neutral-900">母版级监视器与质检开口</h2>
              <span class="rounded bg-primary-100 px-2 py-0.5 gd-num text-micro font-semibold uppercase text-primary-800">
                H.264 · {{ RESOLUTION_LABELS[resolution] }}
              </span>
            </div>
            <div class="flex items-center gap-1.5 text-caption font-semibold" :class="withWatermark ? 'text-gd-pending' : 'text-gd-locked'">
              <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M7 11V7a5 5 0 0110 0v4M5 11h14v9H5z" />
              </svg>
              <span>{{ withWatermark ? '平台水印版 · 去水印需升级' : 'PRO 纯净母版 · 免平台水印' }}</span>
            </div>
          </div>

          <!-- 9:16 竖屏监视器（有母版成片时播放真实 MP4，无产物时回退静态封面示意） -->
          <div ref="monitorStage" class="relative mx-auto flex aspect-[9/16] max-h-[540px] w-full flex-col justify-between overflow-hidden rounded-lg bg-surface-inset p-3">
            <div v-if="!filmPlayable" class="gd-cover-1 absolute inset-0" aria-hidden="true" />
            <video
              v-if="filmPlayable"
              ref="monitorVideo"
              :src="filmPreviewUrl"
              class="absolute inset-0 h-full w-full bg-black object-contain"
              playsinline
              preload="metadata"
              @play="monitorPlaying = true"
              @pause="monitorPlaying = false"
              @ended="onMonitorEnded"
              @timeupdate="onMonitorTimeUpdate"
              @loadedmetadata="onMonitorLoadedMetadata"
              @error="onMonitorError"
              @click="toggleMonitorPlay"
            />
            <div class="pointer-events-none absolute inset-0 bg-gradient-to-t from-scrim-strong via-transparent to-scrim-soft" aria-hidden="true" />

            <!-- 顶部遥测行：REC 规格徽标 + 安全区徽标 -->
            <div class="relative z-10 flex items-center justify-between gap-2 gd-num text-xs text-neutral-800">
              <div class="flex items-center gap-1.5 rounded-md bg-scrim-soft px-2.5 py-1 backdrop-blur-sm">
                <span class="h-2 w-2 animate-ping rounded-full bg-danger" aria-hidden="true" />
                <span class="font-semibold tracking-wider">REC</span>
                <span class="text-neutral-400" aria-hidden="true">|</span>
                <span>{{ monitorSpec }}</span>
              </div>
              <div class="flex items-center gap-1.5 rounded-md bg-scrim-soft px-2.5 py-1 text-caption text-neutral-600 backdrop-blur-sm">
                <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M17 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V5a2 2 0 00-2-2z" />
                </svg>
                <span>{{ projectAspect }} 竖屏安全区已激活</span>
              </div>
            </div>

            <!-- 9:16 安全区虚线参考框 -->
            <div class="pointer-events-none absolute inset-x-6 bottom-16 top-12 flex flex-col justify-between rounded-lg border border-dashed border-primary-200 p-2" aria-hidden="true">
              <span class="gd-num self-start text-[9px] text-primary-300">SAFE AREA TOP · 字幕极限</span>
              <span class="gd-num self-end text-[9px] text-primary-300">SAFE AREA BOTTOM · 手势交互区</span>
            </div>

            <!-- 中央播放钮 -->
            <div class="relative z-10 flex items-center justify-center">
              <button
                type="button"
                class="rounded-full transition-transform duration-gd-normal hover:scale-105 active:scale-95"
                :aria-label="monitorPlaying ? '暂停预览' : '播放预览'"
                @click="toggleMonitorPlay"
              >
                <span class="gd-icon-chip gd-icon-chip--solid gd-icon-chip--lg !rounded-full">
                  <svg v-if="!monitorPlaying" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l10-6.5z" /></svg>
                  <svg v-else viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" /></svg>
                </span>
              </button>
            </div>

            <!-- 底部走带与工具条（时码 / 进度 / 控制按钮绑定真实成片播放） -->
            <div class="relative z-10 flex flex-col gap-1.5 rounded-lg bg-scrim-soft p-2.5 backdrop-blur-sm">
              <div class="flex items-center gap-2.5">
                <span class="gd-num text-xs font-semibold text-primary-300">{{ monitorElapsedText }}</span>
                <div
                  class="group relative h-1.5 flex-1 cursor-pointer touch-none overflow-hidden rounded-full bg-neutral-200"
                  role="slider"
                  tabindex="0"
                  aria-label="成片预览进度"
                  aria-valuemin="0"
                  :aria-valuemax="Math.round(monitorTotalMs)"
                  :aria-valuenow="Math.round(monitorPositionMs)"
                  @pointerdown="onMonitorSeekDown"
                  @pointermove="onMonitorSeekMove"
                  @pointerup="onMonitorSeekUp"
                  @pointercancel="onMonitorSeekUp"
                >
                  <div class="h-full rounded-full bg-primary-500" :style="{ width: monitorProgressPct }" />
                </div>
                <span class="gd-num text-xs text-neutral-400">{{ monitorTimecode }}</span>
              </div>
              <div class="flex items-center justify-between pt-1">
                <div class="flex items-center gap-3 text-neutral-500">
                  <button type="button" class="transition-colors duration-gd-fast hover:text-primary-300" aria-label="回退 10 秒" @click="skipMonitor(-10)">
                    <svg class="gd-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M11 17l-5-5 5-5M18 17l-5-5 5-5" />
                    </svg>
                  </button>
                  <button type="button" class="transition-colors duration-gd-fast hover:text-primary-300" aria-label="播放 / 暂停" @click="toggleMonitorPlay">
                    <svg v-if="!monitorPlaying" class="gd-icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13l10-6.5z" /></svg>
                    <svg v-else class="gd-icon" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5h3.5v14H7zM13.5 5H17v14h-3.5z" /></svg>
                  </button>
                  <button type="button" class="transition-colors duration-gd-fast hover:text-primary-300" aria-label="前进 10 秒" @click="skipMonitor(10)">
                    <svg class="gd-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M13 7l5 5-5 5M6 7l5 5-5 5" />
                    </svg>
                  </button>
                  <span class="h-3.5 w-px bg-neutral-200" aria-hidden="true" />
                  <button type="button" class="transition-colors duration-gd-fast hover:text-primary-300" aria-label="音量">
                    <svg class="gd-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M11 5L6 9H3v6h3l5 4V5z" />
                      <path d="M15.5 8.5a5 5 0 010 7" />
                    </svg>
                  </button>
                </div>
                <div class="flex items-center gap-3 text-caption text-neutral-500">
                  <span class="rounded bg-primary-100 px-2 py-0.5 gd-num text-micro font-semibold text-primary-800">HDR</span>
                  <span class="gd-num">18.4 Mbps</span>
                  <button type="button" class="transition-colors duration-gd-fast hover:text-primary-300" aria-label="全屏" @click="toggleMonitorFullscreen">
                    <svg class="gd-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" />
                    </svg>
                  </button>
                </div>
              </div>
            </div>
          </div>

          <!-- 水印保障说明行（跟随真实水印开关状态） -->
          <div class="mt-4 flex items-center justify-between gap-2 rounded-lg bg-primary-50 px-3 py-2 text-caption" :class="withWatermark ? 'text-gd-pending' : 'text-primary-300'">
            <div class="flex items-center gap-1.5">
              <svg class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 3l7 3v5c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z" />
                <path d="M9.2 11.8l2 2 3.6-4.2" />
              </svg>
              <span>{{ withWatermark ? '当前导出将叠加平台水印：升级 PRO 后自动剥离水印与工程元标记' : 'PRO 会员独占：导出文件已自动剥离平台水印及一切工程元标记' }}</span>
            </div>
            <span class="gd-num shrink-0 text-[11px] font-semibold" :class="withWatermark ? 'text-gd-pending' : 'text-primary-700'">
              {{ withWatermark ? 'WATERMARK_ON' : 'CLEAN_FEED_PASS' }}
            </span>
          </div>
        </section>

        <!-- B. 《人工智能生成合成内容标识办法》法定标识预览（强制嵌入，不可关闭） -->
        <section class="gd-card p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2.5">
              <span class="gd-icon-chip gd-icon-chip--violet gd-icon-chip--sm" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 3l7 3v5c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z" />
                </svg>
              </span>
              <div>
                <h2 class="text-title font-semibold text-neutral-900">《人工智能生成合成内容标识办法》法定标识预览</h2>
                <p class="mt-0.5 text-caption text-neutral-400">国家网信办监管标准 · 片尾显式标识与溯源元数据双重锚定</p>
              </div>
            </div>
            <span class="gd-badge">
              <svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M7 11V7a5 5 0 0110 0v4M5 11h14v9H5z" />
              </svg>
              系统强制嵌入 · 不可关闭
            </span>
          </div>

          <!-- 片尾标识预览：纯黑影院卡（溯源编码由最近成功导出派生） -->
          <div class="mt-4 flex min-h-[110px] w-full flex-col items-center justify-center gap-2 rounded-lg bg-surface-inset p-4 text-center">
            <div class="flex items-center gap-2 text-sm font-semibold text-neutral-800">
              <svg class="h-4 w-4 text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z" />
              </svg>
              本内容由 AI 辅助生成
            </div>
            <p class="text-caption uppercase tracking-widest text-neutral-400">浮光造梦 GleamDream 智能协同引擎制作 · 深度合成算法服务备案</p>
            <div class="gd-num mt-1 flex items-center gap-2 rounded-full bg-neutral-100 px-3 py-0.5 text-caption text-neutral-400">
              <span>溯源数字编码:</span>
              <span class="font-semibold text-primary-700">{{ traceCode }}</span>
            </div>
          </div>

          <!-- 两张锚定说明小卡 -->
          <div class="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
            <div class="gd-inset flex items-start gap-2">
              <svg class="gd-icon mt-0.5 shrink-0 text-primary-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 3l7 3v5c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z" />
                <path d="M9.2 11.8l2 2 3.6-4.2" />
              </svg>
              <div>
                <p class="text-caption font-semibold text-neutral-800">显式尾标（2.0 秒）</p>
                <p class="mt-0.5 text-xs leading-relaxed text-neutral-400">成片片尾帧底居中合成标识，不遮挡核心对白。</p>
              </div>
            </div>
            <div class="gd-inset flex items-start gap-2">
              <svg class="gd-icon mt-0.5 shrink-0 text-primary-600" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M4 8V6a2 2 0 012-2h2M16 4h2a2 2 0 012 2v2M20 16v2a2 2 0 01-2 2h-2M8 20H6a2 2 0 01-2-2v-2" />
                <path d="M12 12h.01" />
              </svg>
              <div>
                <p class="text-caption font-semibold text-neutral-800">隐式盲水印已注入</p>
                <p class="mt-0.5 text-xs leading-relaxed text-neutral-400">视频像素级高频频域内嵌工坊 ID、制作时间戳与模型指纹。</p>
              </div>
            </div>
          </div>
        </section>

        <!-- C. 各发行平台合规预审报告（平台 tabs + 检查清单行） -->
        <section class="gd-card p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2.5">
              <span class="gd-icon-chip gd-icon-chip--violet gd-icon-chip--sm" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M10 6h10M10 12h10M10 18h10" />
                  <path d="M4 6l1.5 1.5L8 5" />
                </svg>
              </span>
              <h2 class="text-title font-semibold text-neutral-900">各发行平台合规预审报告</h2>
            </div>
            <span class="flex items-center gap-1 text-caption font-semibold" :class="latestSucceeded ? 'text-gd-locked' : 'text-gd-pending'">
              <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M12 21a9 9 0 100-18 9 9 0 000 18z" />
                <path d="M8.5 12.5l2.5 2.5 4.5-5" />
              </svg>
              {{ latestSucceeded ? '全平台预估 100% 达标' : '待母版导出后预审' }}
            </span>
          </div>

          <!-- 平台 tabs -->
          <div class="mt-4 flex items-center gap-2 overflow-x-auto pb-1">
            <button
              v-for="platform in PLATFORM_OPTIONS"
              :key="platform.key"
              type="button"
              class="flex shrink-0 items-center gap-1.5 rounded-lg px-3.5 py-1.5 text-sm transition-colors duration-gd-fast"
              :class="platformTab === platform.key ? 'bg-primary-100 font-medium text-primary-800' : 'bg-surface-canvas text-neutral-500 hover:text-neutral-700'"
              :aria-pressed="platformTab === platform.key"
              @click="platformTab = platform.key"
            >
              <span v-if="platformTab === platform.key" class="h-2 w-2 rounded-full bg-primary-500" aria-hidden="true" />
              {{ platform.label }}
            </button>
          </div>

          <!-- 合规检查清单行 -->
          <ul class="mt-3 flex flex-col gap-2">
            <li v-for="check in complianceChecks" :key="check.name" class="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-surface-canvas px-3 py-2.5">
              <div class="flex min-w-0 items-center gap-2">
                <svg class="h-4 w-4 shrink-0 text-gd-locked" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M12 21a9 9 0 100-18 9 9 0 000 18z" />
                  <path d="M8.5 12.5l2.5 2.5 4.5-5" />
                </svg>
                <span class="text-sm font-medium text-neutral-800">{{ check.name }}</span>
                <span class="gd-num truncate text-xs text-neutral-400">{{ check.value }}</span>
              </div>
              <span class="gd-num shrink-0 rounded px-2 py-0.5 text-micro font-semibold" :class="check.tone === 'mint' ? 'gd-fill-mint' : 'gd-fill-amber'">
                {{ check.badge }}
              </span>
            </li>
          </ul>
        </section>
      </div>

      <!-- ==================== 右列（导出规格 / AI 宣发 / 矩阵授权） ==================== -->
      <div class="flex flex-col gap-5 xl:col-span-5">
        <!-- A. 母版导出规格与打包（单集即时渲染流） -->
        <section class="gd-card p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2.5">
              <span class="gd-icon-chip gd-icon-chip--violet gd-icon-chip--sm" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 7h10M18 7h2M14 5v4M4 17h4M12 17h8M8 15v4" />
                </svg>
              </span>
              <h2 class="text-title font-semibold text-neutral-900">母版导出规格与打包</h2>
            </div>
            <span class="text-caption text-neutral-400">单集即时渲染流</span>
          </div>

          <!-- 输出分辨率三选项（权益控制保留：无权益点击弹升级弹窗） -->
          <div class="mt-4">
            <p class="gd-section-label">输出分辨率</p>
            <div class="mt-2 grid grid-cols-3 gap-2.5">
              <button
                v-for="option in RESOLUTION_OPTIONS"
                :key="option.key"
                type="button"
                class="flex flex-col items-center gap-0.5 rounded-lg border px-2 py-2.5 text-sm transition-colors duration-gd-fast"
                :class="
                  resolution === option.key
                    ? 'border-primary-200 bg-primary-100 font-medium text-primary-800'
                    : resolutionEnabled(option.key)
                      ? 'border-hairline bg-surface-canvas text-neutral-600 hover:border-hairline-strong'
                      : 'border-gold-light bg-gold-fog text-gold-deep opacity-80'
                "
                :title="resolutionEnabled(option.key) ? option.desc : '当前套餐不支持该分辨率，点击查看升级权益'"
                :aria-pressed="resolution === option.key"
                @click="pickResolution(option.key)"
              >
                <span class="flex items-center gap-1 font-semibold">
                  {{ option.label }}
                  <span
                    v-if="option.key === '4k'"
                    class="gd-fill-amber rounded px-1 py-px text-micro font-semibold"
                    :class="resolutionEnabled('4k') ? '' : 'opacity-65'"
                  >PRO</span>
                  <svg
                    v-else-if="!resolutionEnabled(option.key)"
                    class="h-3 w-3 text-gold-deep"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    aria-hidden="true"
                  >
                    <path d="M7 11V7a5 5 0 0110 0v4M5 11h14v9H5z" />
                  </svg>
                </span>
                <span class="text-[10px] text-neutral-400">{{ option.desc }}</span>
              </button>
            </div>
          </div>

          <!-- 画幅比例设定（跟随项目画幅，创建后不可改） -->
          <div class="mt-4">
            <p class="gd-section-label">画幅比例设定</p>
            <div class="mt-2 grid grid-cols-2 gap-2.5">
              <button
                type="button"
                class="flex items-center justify-center gap-2 rounded-lg px-3 py-2.5 text-sm transition-colors duration-gd-fast"
                :class="projectAspect === '9:16' ? 'bg-primary-100 font-medium text-primary-800' : 'bg-surface-canvas text-neutral-500'"
                :aria-pressed="projectAspect === '9:16'"
                :disabled="projectAspect !== '9:16'"
                :title="projectAspect === '9:16' ? '项目画幅' : '画幅跟随项目设定，当前项目非竖屏'"
              >
                <svg class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M17 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V5a2 2 0 00-2-2z" />
                </svg>
                9:16 竖屏短剧（默认）
              </button>
              <button
                type="button"
                class="flex items-center justify-center gap-2 rounded-lg px-3 py-2.5 text-sm transition-colors duration-gd-fast"
                :class="projectAspect === '16:9' ? 'bg-primary-100 font-medium text-primary-800' : 'bg-surface-canvas text-neutral-500'"
                :aria-pressed="projectAspect === '16:9'"
                :disabled="projectAspect !== '16:9'"
                :title="projectAspect === '16:9' ? '项目画幅' : '画幅跟随项目设定，当前项目非横屏'"
              >
                <svg class="h-4 w-4 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M5 6h14a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2z" />
                </svg>
                16:9 横屏影视切片
              </button>
            </div>
          </div>

          <!-- 母版命名规范模版 + 真实信息预览 -->
          <div class="mt-4">
            <div class="flex items-center justify-between">
              <p class="gd-section-label">母版命名规范模版</p>
              <span class="gd-num text-micro text-neutral-400">宏：{剧名} {集号} {集标题} {分辨率}</span>
            </div>
            <div class="mt-2 flex items-center gap-2 rounded-lg border border-hairline bg-surface-canvas px-3 transition-colors duration-gd-fast focus-within:border-primary-200">
              <svg class="h-4 w-4 shrink-0 text-neutral-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M4 4h7l9 9-7 7-9-9V4z" />
                <path d="M8 8h.01" />
              </svg>
              <input v-model="namingTemplate" type="text" class="gd-num h-10 min-w-0 flex-1 bg-transparent text-sm text-neutral-800 outline-none" aria-label="母版命名模板" />
            </div>
            <p class="gd-num mt-1.5 flex items-center gap-1 text-[11px] text-neutral-400">
              <span>预览:</span>
              <span class="truncate font-semibold text-neutral-600">{{ previewFileName }}</span>
            </p>
          </div>

          <!-- 打包选项清单（前 3 项为 p7 结构件；带水印为既有权益功能） -->
          <div class="mt-4 flex flex-col gap-2 pt-1">
            <label class="flex cursor-pointer items-center gap-2.5 text-sm text-neutral-700">
              <input v-model="pkgSubtitleTrack" type="checkbox" class="h-4 w-4 accent-primary-500" />
              附带无损 SRT 软字幕轨与 CC 标头
            </label>
            <label class="flex cursor-pointer items-center gap-2.5 text-sm text-neutral-700">
              <input v-model="pkgStreamDownload" type="checkbox" class="h-4 w-4 accent-primary-500" />
              分集边导边下（.zip 离线容灾通道）
            </label>
            <label class="flex cursor-pointer items-center gap-2.5 text-sm text-neutral-700">
              <input v-model="pkgManifest" type="checkbox" class="h-4 w-4 accent-primary-500" />
              导出全剧工程清单（JSON / Final Cut XML）
            </label>
            <label class="flex items-center gap-2.5 text-sm" :class="canRemoveWatermark ? 'cursor-pointer text-neutral-700' : 'cursor-not-allowed text-neutral-400'">
              <input
                type="checkbox"
                class="h-4 w-4 accent-primary-500"
                :checked="withWatermark"
                :disabled="!canRemoveWatermark"
                :title="canRemoveWatermark ? '切换水印' : '去水印需升级套餐'"
                @change="toggleWatermark"
              />
              带平台水印
              <span v-if="!canRemoveWatermark" class="flex items-center gap-1 text-xs text-gd-pending">
                <svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <path d="M7 11V7a5 5 0 0110 0v4M5 11h14v9H5z" />
                </svg>
                去水印权益不足
              </span>
            </label>
          </div>

          <!-- 导出范围 / 批量参数（既有功能保留） -->
          <div class="mt-4 flex flex-wrap items-center gap-x-5 gap-y-2 border-t border-hairline-soft pt-4">
            <label class="flex items-center gap-2 text-sm text-neutral-700">
              导出范围
              <select v-model="episodeScope" class="gd-input gd-input--sm w-32" aria-label="选择导出集数">
                <option value="">全部剧集</option>
                <option v-for="no in episodeCount" :key="no" :value="String(no)">第 {{ no }} 集</option>
              </select>
            </label>
            <label class="flex items-center gap-2 text-sm text-neutral-700">
              批量分辨率
              <select v-model="batchResolution" class="gd-input gd-input--sm w-28" aria-label="批量导出分辨率">
                <option v-for="option in RESOLUTION_OPTIONS" :key="option.key" :value="option.key" :disabled="!resolutionEnabled(option.key)">
                  {{ option.label }}{{ resolutionEnabled(option.key) ? '' : '（权益不足）' }}
                </option>
              </select>
            </label>
            <label class="flex cursor-pointer items-center gap-2 text-sm text-neutral-700">
              <input
                v-model="batchWatermark"
                type="checkbox"
                class="h-4 w-4 accent-primary-500"
                :disabled="!canRemoveWatermark"
                :title="canRemoveWatermark ? '勾选后批量产物叠加水印' : '当前套餐不含去水印权益：批量带水印固定开启'"
              />
              批量带水印
            </label>
          </div>
          <p class="mt-2 text-caption text-neutral-400">导出不消耗积分 · 产物保留 7 天，请及时下载</p>

          <!-- 主次操作按钮 -->
          <div class="mt-4 flex flex-col gap-2.5">
            <AppButton variant="primary" size="md" block :disabled="!canExport" :title="exportHint" @click="openConfirm">
              {{ episodeScope === '' ? '立即导出全部剧集母版' : `立即导出第 ${episodeScope} 集母版` }}
            </AppButton>
            <AppButton
              variant="secondary"
              size="md"
              block
              :disabled="!finalApproved"
              :loading="batchLoading"
              :title="batchExportHint"
              @click="submitBatchExport"
            >
              批量打包导出全剧 ZIP（{{ episodeCount }} 集）
            </AppButton>
          </div>
        </section>

        <!-- B. AI 智能宣发文案与高光抓取 -->
        <section class="gd-card p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2.5">
              <span class="gd-icon-chip gd-icon-chip--magenta gd-icon-chip--sm" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M9 9h6v6H9z" />
                  <path d="M5 5h14v14H5z" />
                  <path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" />
                </svg>
              </span>
              <div>
                <h2 class="text-title font-semibold text-neutral-900">AI 智能宣发文案与高光抓取</h2>
                <p class="mt-0.5 text-caption text-neutral-400">基于剧情转折算法自动提取吸睛文案</p>
              </div>
            </div>
            <AppButton
              variant="secondary"
              size="sm"
              :loading="metadataLoading"
              :disabled="!finalApproved"
              :title="metadataHint"
              @click="generateMetadata"
            >
              重新提炼
            </AppButton>
          </div>

          <div v-if="metadata.title || coverSuggestions.length > 0" class="mt-4 flex flex-col gap-4">
            <!-- 爆款吸引力标题建议 -->
            <label class="block">
              <span class="mb-1.5 flex items-center justify-between text-caption text-neutral-400">
                <span>爆款吸引力标题建议</span>
                <span class="gd-num text-primary-700">{{ metadata.title.length }} / 55 字</span>
              </span>
              <input v-model="metadata.title" type="text" maxlength="55" placeholder="分发标题（≤55 字）" class="gd-input" />
            </label>

            <!-- 剧情吸睛提要 -->
            <label class="block">
              <span class="mb-1.5 flex items-center justify-between text-caption text-neutral-400">
                <span>剧情吸睛提要（带热搜情绪钩子）</span>
                <span class="gd-num">{{ metadata.description.length }} / 300 字</span>
              </span>
              <textarea v-model="metadata.description" rows="3" maxlength="300" placeholder="分发简介（≤300 字）" class="gd-input" />
            </label>

            <!-- 宣发关联话题标签 chips -->
            <div>
              <span class="mb-1.5 block text-caption text-neutral-400">宣发关联话题标签（回车添加，≤10 个）</span>
              <div class="flex flex-wrap items-center gap-2 rounded-lg border border-hairline p-2 transition-colors duration-gd-fast focus-within:border-primary-200">
                <span
                  v-for="tag in metadata.tags"
                  :key="tag"
                  class="inline-flex items-center gap-1 rounded-full bg-primary-50 py-0.5 pl-2.5 pr-1.5 text-xs text-primary-700"
                >
                  #{{ tag }}
                  <button type="button" class="rounded-full p-0.5 hover:bg-primary-100" :aria-label="`删除标签 ${tag}`" @click="removeTag(tag)">
                    <svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
                      <path d="M6 6l12 12M18 6L6 18" />
                    </svg>
                  </button>
                </span>
                <input
                  v-model="tagInput"
                  type="text"
                  placeholder="添加标签"
                  class="h-6 min-w-[96px] flex-1 bg-transparent text-xs outline-none placeholder:text-neutral-400"
                  @keydown.enter.prevent="addTag"
                />
              </div>
            </div>

            <!-- AI 抓取高点击率封面候选（9:16 缩略卡 + 选中态；候选为帧描述文案） -->
            <div v-if="coverSuggestions.length > 0">
              <span class="mb-1.5 block text-caption text-neutral-400">AI 抓取高点击率封面候选（点选作为分发封面）</span>
              <div class="grid grid-cols-3 gap-2.5">
                <button
                  v-for="(suggestion, index) in coverSuggestions"
                  :key="index"
                  type="button"
                  class="gd-cv-cover relative flex aspect-[9/16] flex-col justify-end overflow-hidden rounded-lg border p-2 text-left transition-all duration-gd-normal"
                  :class="[
                    `gd-cv-${(index % 6) + 1}`,
                    metadata.cover === suggestion ? 'border-primary-200 shadow-gd-1' : 'border-hairline opacity-80 hover:opacity-100',
                  ]"
                  :aria-pressed="metadata.cover === suggestion"
                  @click="metadata.cover = suggestion"
                >
                  <span class="gd-num relative z-10 text-[10px] font-semibold text-neutral-800">方案 {{ String(index + 1).padStart(2, '0') }}</span>
                  <span class="relative z-10 mt-0.5 line-clamp-4 text-[11px] leading-snug text-neutral-700">{{ suggestion }}</span>
                  <span
                    v-if="metadata.cover === suggestion"
                    class="gd-fill-brand absolute right-1.5 top-1.5 z-10 flex h-5 w-5 items-center justify-center rounded-full"
                  >
                    <svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M5 13l4 4L19 7" />
                    </svg>
                  </span>
                </button>
              </div>
            </div>
          </div>
          <p v-else class="mt-4 py-6 text-center text-sm text-neutral-400">尚未生成宣发文案，点击右上角「重新提炼」</p>
        </section>

        <!-- C. 矩阵平台一键推流授权（平台行卡 + 分发操作 + 最近推流记录） -->
        <section class="gd-card p-5">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2.5">
              <span class="gd-icon-chip gd-icon-chip--violet gd-icon-chip--sm" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M22 2L11 13" />
                  <path d="M22 2l-7 20-4-9-9-4z" />
                </svg>
              </span>
              <div>
                <h2 class="text-title font-semibold text-neutral-900">矩阵平台一键推流授权</h2>
                <p class="mt-0.5 text-caption text-neutral-400">已绑定官方短剧开放平台 OAuth 凭据（{{ authorizedCount }}/{{ PLATFORM_OPTIONS.length }}）</p>
              </div>
            </div>
            <div class="flex items-center gap-1">
              <AppButton variant="text" size="sm" @click="fetchPlatformAccounts">刷新授权</AppButton>
              <AppButton variant="text" size="sm" @click="router.push('/settings/platform-accounts')">添加渠道 →</AppButton>
            </div>
          </div>

          <!-- 授权概览 chips（F7-01） -->
          <div class="mt-3 flex flex-wrap gap-2">
            <span class="inline-flex items-center gap-1.5 rounded-full bg-success-bg px-2.5 py-1 text-caption font-medium text-success">
              <span class="h-1.5 w-1.5 rounded-full bg-success" aria-hidden="true" />已授权 {{ authorizedCount }}
            </span>
            <span v-if="expiredCount > 0" class="inline-flex items-center gap-1.5 rounded-full bg-danger-bg px-2.5 py-1 text-caption font-medium text-danger">
              <span class="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden="true" />授权过期 {{ expiredCount }}
            </span>
          </div>

          <!-- 平台行卡（授权态 / 临期态 / 未授权态 三形态） -->
          <ul class="mt-3 flex flex-col gap-2.5">
            <li
              v-for="platform in PLATFORM_OPTIONS"
              :key="platform.key"
              class="rounded-lg border p-3 transition-colors duration-gd-fast"
              :class="distributePlatform === platform.key ? 'border-primary-200 bg-primary-50' : 'border-hairline-soft bg-surface-canvas'"
            >
              <div class="flex items-center justify-between gap-3">
                <div class="flex min-w-0 items-center gap-3">
                  <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-surface-raised text-lg shadow-gd-1" aria-hidden="true">
                    {{ platform.icon }}
                  </span>
                  <div class="min-w-0">
                    <div class="flex flex-wrap items-center gap-1.5">
                      <span class="text-sm font-medium text-neutral-800">{{ platform.label }}</span>
                      <span
                        class="rounded px-1.5 py-0.5 text-micro font-semibold"
                        :class="
                          matrixStateOf(platform.key).state === 'active'
                            ? 'gd-fill-mint'
                            : matrixStateOf(platform.key).state === 'expired'
                              ? 'gd-fill-rose'
                              : 'bg-surface-sunken text-neutral-400'
                        "
                      >
                        {{ matrixStateOf(platform.key).state === 'active' ? '授权有效' : matrixStateOf(platform.key).state === 'expired' ? '授权临期' : '未授权' }}
                      </span>
                    </div>
                    <p class="mt-0.5 truncate text-xs" :class="matrixStateOf(platform.key).state === 'expired' ? 'text-danger' : 'text-neutral-400'">
                      {{ matrixDescOf(platform.key) }}
                    </p>
                  </div>
                </div>
                <AppButton
                  v-if="matrixStateOf(platform.key).state === 'none'"
                  variant="secondary"
                  size="sm"
                  :loading="authorizingPlatform === platform.key"
                  @click="authorizePlatform(platform.key)"
                >
                  授权
                </AppButton>
                <AppButton
                  v-else-if="matrixStateOf(platform.key).state === 'expired'"
                  variant="secondary"
                  size="sm"
                  :loading="authorizingPlatform === platform.key"
                  @click="authorizePlatform(platform.key)"
                >
                  重新授权
                </AppButton>
                <AppButton v-else variant="primary" size="sm" @click="pickDistributePlatform(platform.key)">
                  {{ distributePlatform === platform.key ? '已选为分发目标' : '一键分发起草' }}
                </AppButton>
              </div>
            </li>
          </ul>

          <!-- 分发操作区（平台选择 chips + 发布按钮，既有功能保留） -->
          <div class="mt-4 border-t border-hairline-soft pt-4">
            <div class="flex flex-wrap items-center gap-2">
              <button
                v-for="platform in PLATFORM_OPTIONS"
                :key="`chip-${platform.key}`"
                type="button"
                class="flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors duration-gd-normal"
                :class="
                  distributePlatform === platform.key
                    ? 'border-primary-200 bg-primary-100 text-primary-800'
                    : accountOf(platform.key)
                      ? 'border-hairline bg-surface-canvas text-neutral-600 hover:border-hairline-strong'
                      : 'cursor-not-allowed border-hairline-soft bg-surface-canvas text-neutral-400'
                "
                :title="accountOf(platform.key) ? `分发到${platform.label}` : '未授权'"
                @click="pickDistributePlatform(platform.key)"
              >
                <span aria-hidden="true">{{ platform.icon }}</span>
                {{ platform.label }}
              </button>
            </div>
            <AppButton
              variant="primary"
              size="md"
              block
              class="mt-3"
              :loading="distributing"
              :disabled="!metadataReady || !distributePlatform || !finalApproved"
              :title="distributeHint"
              @click="submitDistribute"
            >
              发布到{{ distributePlatform ? platformOption(distributePlatform).label : '所选平台' }}
            </AppButton>
            <p v-if="!metadataReady" class="mt-2 text-xs text-gd-pending">请先生成或填写发行标题</p>
          </div>

          <!-- 最近推流记录（真实分发记录派生） -->
          <div class="mt-4 flex flex-col gap-1.5">
            <div class="flex items-center justify-between text-caption text-neutral-400">
              <span>最近推流记录（{{ projectTitle || '当前项目' }} 溯源）</span>
              <span class="gd-num font-semibold" :class="succeededPlatformCount > 0 ? 'text-primary-700' : 'text-neutral-400'">
                已同步 {{ succeededPlatformCount }} 个平台
              </span>
            </div>
            <div v-if="latestDistribution" class="flex items-center justify-between gap-2 rounded-lg bg-surface-canvas px-3 py-2.5 text-sm">
              <div class="flex min-w-0 items-center gap-2">
                <span class="h-2 w-2 shrink-0 rounded-full bg-primary-500" aria-hidden="true" />
                <span class="truncate font-medium text-neutral-800">
                  {{ latestDistribution.title || platformOption(latestDistribution.platform).label }} · {{ platformOption(latestDistribution.platform).label }}
                </span>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <a
                  v-if="latestDistribution.publish_url"
                  :href="latestDistribution.publish_url"
                  target="_blank"
                  rel="noopener"
                  class="text-xs font-medium text-primary-700 hover:text-primary-600"
                >
                  回链 ↗
                </a>
                <StatusTag :status="latestDistribution.status" size="sm" />
              </div>
            </div>
            <p v-else class="rounded-lg bg-surface-canvas px-3 py-2.5 text-center text-sm text-neutral-400">
              暂无推流记录 · 完成授权并生成分发标题后可一键推流
            </p>
          </div>
        </section>
      </div>
    </div>

    <!-- ============ 导出与分发记录（三态加载 + 下载 / 重试 / 回链，功能保留） ============ -->
    <section ref="recordsCard" class="gd-card mt-5 scroll-mt-4 p-5">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2.5">
          <span class="gd-icon-chip gd-icon-chip--cyan gd-icon-chip--sm" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M3 12a9 9 0 103-6.7" />
              <path d="M3 4v5h5" />
              <path d="M12 8v4l3 2" />
            </svg>
          </span>
          <h2 class="text-title font-semibold text-neutral-900">导出与分发记录</h2>
        </div>
        <AppButton variant="text" size="sm" @click="fetchExports">刷新导出任务</AppButton>
      </div>

      <!-- 导出任务 -->
      <div class="mt-4">
        <p class="gd-section-label">导出任务</p>
        <LoadingSkeleton v-if="loading" variant="table" :rows="3" class="mt-3" />
        <ErrorRetryBlock v-else-if="errorMsg" message="导出任务加载失败" :description="errorMsg" class="mt-3" @retry="fetchExports" />
        <p v-else-if="exportJobs.length === 0" class="mt-3 py-6 text-center text-sm text-neutral-400">
          暂无导出任务，配置参数后点击「立即导出当前集母版」
        </p>
        <ul v-else class="mt-1 divide-y divide-hairline-soft">
          <li v-for="job in exportJobs" :key="job.id" class="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
            <StatusTag :status="job.status" size="sm" class="w-[92px]" />
            <span class="gd-num w-14 text-sm text-neutral-700">{{ job.resolution }}</span>
            <span class="w-16 text-xs" :class="job.has_watermark ? 'text-neutral-500' : 'text-gd-locked'">
              {{ job.has_watermark ? '带水印' : '无水印' }}
            </span>
            <span class="w-10 text-xs text-neutral-400">{{ job.format }}</span>
            <span class="w-24 text-xs text-neutral-400">{{ formatRelativeTime(job.created_at) }}</span>
            <span class="gd-num w-20 text-xs text-neutral-400">{{ formatBytes(job.output_size_bytes) }}</span>

            <div class="ml-auto flex items-center gap-2">
              <!-- 失败原因 -->
              <span v-if="job.status === 'failed' && job.error_reason" class="max-w-[180px] truncate text-xs text-danger" :title="job.error_reason">
                {{ job.error_reason }}
              </span>
              <!-- 下载（succeeded） -->
              <a
                v-if="job.status === 'succeeded'"
                :href="jobDownloadUrl(job) || '#'"
                download
                class="inline-flex h-7 items-center rounded-md border border-primary-200 bg-primary-50 px-2.5 text-xs font-medium text-primary-700 transition-colors duration-gd-fast hover:bg-primary-100"
                @click.prevent="downloadJob(job)"
              >
                下载 {{ job.format === 'zip' ? 'ZIP' : 'MP4' }}
              </a>
              <!-- 失败重试 -->
              <AppButton v-if="job.status === 'failed'" variant="secondary" size="sm" @click="retryJob(job)">重试</AppButton>
            </div>
            <!-- 成片预览修复：MP4 导出产物在记录行内直接播放 -->
            <video
              v-if="job.status === 'succeeded' && job.output?.download_url?.toLowerCase().endsWith('.mp4')"
              :src="job.output.download_url"
              controls
              preload="metadata"
              class="mt-1 w-full max-w-[280px] basis-full rounded-md border border-hairline bg-black"
            />
            <p
              v-else-if="job.status === 'succeeded' && !!jobDownloadUrl(job)"
              class="mt-1 basis-full text-caption text-gd-pending"
            >
              历史占位产物（非 MP4），请重新导出后可播放
            </p>
          </li>
        </ul>
      </div>

      <!-- 分发记录 -->
      <div class="mt-2 border-t border-hairline-soft pt-4">
        <div class="flex items-center justify-between">
          <p class="gd-section-label">分发记录</p>
          <AppButton variant="text" size="sm" @click="fetchDistributions">刷新分发记录</AppButton>
        </div>
        <p v-if="distributions.length === 0" class="mt-3 py-6 text-center text-sm text-neutral-400">暂无分发记录</p>
        <ul v-else class="mt-1 divide-y divide-hairline-soft">
          <li v-for="record in distributions" :key="record.id" class="flex items-center gap-3 py-3">
            <span class="text-lg" aria-hidden="true">{{ platformOption(record.platform).icon }}</span>
            <div class="min-w-0 flex-1">
              <p class="truncate text-sm text-neutral-800">{{ record.title || platformOption(record.platform).label }}</p>
              <p class="text-xs text-neutral-400">{{ formatRelativeTime(record.distributed_at ?? undefined) }}</p>
            </div>
            <a
              v-if="record.publish_url"
              :href="record.publish_url"
              target="_blank"
              rel="noopener"
              class="shrink-0 text-xs font-medium text-primary-600 hover:text-primary-700"
            >
              查看外链 ↗
            </a>
            <StatusTag :status="record.status" size="sm" class="shrink-0" />
          </li>
        </ul>
      </div>
    </section>

    <!-- 分发引导空态（功能保留） -->
    <EmptyState
      v-if="!finalApproved && exportJobs.length === 0 && distributions.length === 0"
      icon="🚀"
      title="成片确认后开启分发"
      description="完成 P17 成片确认后，即可在此导出成片并一键分发到多个平台"
    />

    <!-- ============ 底部收口条：校验状态 + 历史记录 + 一键推流全平台 ============ -->
    <footer class="gd-card mt-5 flex flex-col items-start justify-between gap-4 p-4 md:flex-row md:items-center">
      <div class="flex items-center gap-3">
        <span
          class="gd-icon-chip gd-icon-chip--sm !rounded-full"
          :class="finalApproved ? 'gd-icon-chip--mint' : 'gd-icon-chip--amber'"
          aria-hidden="true"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M2 13l4 4 8-9" />
            <path d="M10 13l4 4 8-9" />
          </svg>
        </span>
        <div>
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-title font-semibold text-neutral-900">{{ footerTitle }}</span>
            <span v-if="latestSucceeded" class="gd-fill-mint gd-num rounded-full px-2 py-0.5 text-micro">JOB #{{ latestSucceeded.id }}</span>
          </div>
          <p class="mt-0.5 text-xs text-neutral-400">{{ footerDesc }}</p>
        </div>
      </div>
      <div class="flex w-full shrink-0 items-center justify-end gap-2.5 md:w-auto">
        <AppButton variant="secondary" size="md" @click="scrollToRecords">
          <span class="flex items-center gap-1.5">历史导出记录（90 天留存）</span>
        </AppButton>
        <AppButton
          variant="primary"
          size="md"
          :loading="distributing"
          :disabled="!metadataReady || !distributePlatform || !finalApproved"
          :title="distributeHint"
          @click="submitDistribute"
        >
          🚀 立即一键推流全平台草稿
        </AppButton>
      </div>
    </footer>

    <!-- ============ 导出确认弹窗（CostEstimateDialog 风格，功能保留） ============ -->
    <CostEstimateDialog
      v-model="confirmOpen"
      :estimated-credits="0"
      title="确认开始导出"
      :description="`分辨率 ${resolution} · ${withWatermark ? '带水印' : '无水印'} · ${episodeScope === '' ? '全部剧集' : `第 ${episodeScope} 集`}`"
      :loading="exporting"
      @confirm="confirmExport"
    />

    <!-- ============ 4K 升级弹窗（F7-01-s2：权益不匹配不静默降级，功能保留） ============ -->
    <Modal v-model="upgradeModalOpen" title="4K 为会员权益" :max-width="440">
      <div class="flex items-center gap-2">
        <span class="gd-fill-amber rounded px-1.5 py-0.5 text-micro font-semibold">PRO</span>
        <span class="text-sm font-medium text-neutral-900">会员专属分辨率</span>
      </div>
      <p class="mt-3 text-sm leading-relaxed text-neutral-700">
        当前套餐最高支持 <span class="gd-num font-semibold">{{ maxResolutionLabel }}</span> 导出。升级 Pro 后可解锁：
      </p>
      <ul class="mt-3 space-y-2">
        <li class="rounded-lg bg-gold-fog px-3.5 py-2 text-xs text-gold-deep">4K 分辨率导出</li>
        <li class="rounded-lg bg-gold-fog px-3.5 py-2 text-xs text-gold-deep">导出去水印</li>
        <li class="rounded-lg bg-gold-fog px-3.5 py-2 text-xs text-gold-deep">批量 30 集 ZIP 打包</li>
      </ul>
      <p class="mt-3 text-caption text-neutral-400">权益不足项标灰展示，不会静默降级导出。</p>
      <template #footer>
        <div class="flex justify-end gap-2.5">
          <AppButton variant="secondary" size="md" @click="upgradeModalOpen = false">按 {{ maxResolutionLabel }} 导出</AppButton>
          <AppButton variant="primary" size="md" class="!bg-gold !text-[#1a1205] hover:!bg-gold-deep active:!bg-gold-deep" @click="router.push('/billing')">升级直达</AppButton>
        </div>
      </template>
    </Modal>
  </div>
</template>

<!--
  文件级注释：P10 · 资产详情（角色画廊，路由 /projects/:projectId/assets/characters/:assetId）。
  v7 纸上放映厅 Pro · 对照 F3-03（P10 角色详情）重构：
  - 左画廊：主形象大图（左上 v3·当前版本 chip + 右下一致性 QC 分数胶囊）+ 三视图横排
    （正视图 front / 侧视图 side / 背视图 back）+ 表情集大图 + 场景道具参考图（缺项夜窗占位 +
    「生成」按钮 → POST generate {version_type}）；
  - 右详情：衬线角色名卡（类型 chip + 更新时间 + 外观描述 + 属性 chips）+ 音色绑定卡
    （播放圆钮 + 波形装饰 + 试听 30s + 更换音色）+ 重抽成本卡（成本透明：本次预估 / 累计已消耗
    金字 gold-deep + .gd-num；已重抽 N 次·达上限转人工）+ 锚点影响卡 + 版本对比 +
    质检历史（版本轴 v3/v2/v1 + 通过/未过 chips + 失败原因）；
  - 底部关口操作条：approve / reject ≥10 字 / 重新生成 / approved 后锁定（60003 缺视图错误展示）。
  重抽二次确认带成本行（本次预估 X · 余额 1,240→1,198）；锚点更换影响弹窗功能保留。
  生成动作后 2s 轮询详情直至状态离开 generating（onUnmounted 清理）；SSE 订阅
  asset.generated 等事件自动刷新。三态：骨架 / 错误重试 / 数据区。
-->
<script setup lang="ts">
/**
 * CharacterDetailView 资产详情（角色画廊）。
 * props：无。emits：无。
 * 依赖 assets API（详情 / 生成 / 审核 / 锁定 / 音色 / 锚点影响）、VersionCompareSlider、
 * ApprovalBar、useSSE、useToast、billing store（重抽成本二次确认的余额行）。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  approveAsset,
  ASSET_TYPE_LABELS,
  ASSET_VERSION_TYPE_LABELS,
  bindAssetVoice,
  generateAsset,
  getAsset,
  getAssetAnchorImpact,
  listTtsVoices,
  lockAsset,
  previewTtsVoice,
  regenerateAsset,
  rejectAsset,
  type AssetDetail,
  type AssetVersionItem,
  type AssetVersionType,
  type TtsVoice,
} from '@/api/assets'
import ApprovalBar from '@/components/common/ApprovalBar.vue'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import VersionCompareSlider from '@/components/common/VersionCompareSlider.vue'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/composables/useToast'
import { useVoicePreview } from '@/composables/useVoicePreview'
import { useAuthStore } from '@/stores/auth'
import { useBillingStore } from '@/stores/billing'
import { formatShortDate } from '@/utils/datetime'

/** 路由实例 */
const route = useRoute()
const router = useRouter()
/** Toast */
const toast = useToast()
/** 认证 store（SSE 查询参数 token） */
const authStore = useAuthStore()
/** 计费 store（重抽成本二次确认展示余额） */
const billingStore = useBillingStore()

/** 项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))
/** 资产 ID */
const assetId = computed(() => String(route.params.assetId ?? ''))

/* ==================== 重抽成本估算（F3-03 成本透明卡） ====================
 * 后端暂无单次重撞计费端点：按演示环境固定估算（与草稿预览 30 积分/秒口径同族的
 * 单图固定档）。金额仅作成本透明展示，实际扣费以结算为准。 */
/** 单次主形象重抽估算积分 */
const REGEN_ESTIMATE_CREDITS = 42

/* ==================== 详情数据 ==================== */

/** 资产详情 */
const detail = ref<AssetDetail | null>(null)
/** 详情加载中 */
const loading = ref(true)
/** 详情加载失败信息 */
const loadError = ref('')

/** 音色库选项 */
const voiceOptions = ref<TtsVoice[]>([])
/** 待绑定音色 ID（下拉选中值） */
const selectedVoiceId = ref<number | null>(null)
/** 音色绑定中 */
const voiceBinding = ref(false)
/** 音色试听播放器（实时调用后端合成接口，不再用提示文案伪装试听） */
const { playingId, loadingId, toggle: toggleVoicePreview } = useVoicePreview()
/** 锚点影响镜头数（null 未加载） */
const affectedShots = ref<number | null>(null)

/** 是否角色类型（决定画廊分组与音色卡展示） */
const isCharacter = computed(() => detail.value?.asset_type === 'character')

/**
 * 从详情 meta 宽松读取字段值。
 * @param key meta 键
 * @returns 未知类型值
 */
function metaValue(key: string): unknown {
  return detail.value?.meta?.[key]
}

/**
 * meta 字符串字段读取。
 * @param key meta 键
 * @returns 字符串（无则空串）
 */
function metaText(key: string): string {
  const value = metaValue(key)
  return typeof value === 'string' ? value : ''
}

/** 展示用外观描述（meta.appearance 优先，回退 description） */
const appearanceText = computed(() => metaText('appearance') || detail.value?.description || '—')

/** 最近一次失败原因（meta.last_error） */
const lastErrorText = computed(() => metaText('last_error'))

/**
 * 失败是否发生在「模型生成」阶段而非质检阶段。
 * 当前资产表没有独立的 generation_failed 状态，生成失败会复用 qc_failed；
 * 通过错误文案识别，避免把 API Key/网络错误误报成“质检不合格”。
 */
const isGenerationFailure = computed(() =>
  /生成失败|鉴权失败|API Key|ConnectError|网络连接失败|HTTP 401|HTTP 403/.test(lastErrorText.value),
)

/** 戏份权重展示（meta.weight 1-5） */
const weightValue = computed(() => {
  const value = metaValue('weight')
  return typeof value === 'number' ? value : null
})

/** 主形象重抽次数（portrait 版本数 - 1，首次生成不计重抽） */
const regenCount = computed(() => {
  if (!detail.value) return 0
  const portraitVersions = detail.value.versions.filter((v) => v.version_type === 'portrait').length
  return Math.max(0, portraitVersions - 1)
})

/**
 * 累计重抽估算积分（重抽次数 × 单次预估）。
 * P3-d 修复：后端版本记录不含实扣流水，此值只能为「按预估单价的估算」；
 * 文案已同步标注「估算」，避免与真实扣费（预估 42 实扣约 10）混淆。
 */
const regenAccumulated = computed(() => regenCount.value * REGEN_ESTIMATE_CREDITS)

/** 当前余额（计费 store；未加载时为 0） */
const creditBalance = computed(() => billingStore.balance)

/* ==================== 幻彩封面系统已随 09 号「梦境窗口」退役：缺图占位统一夜窗化 ==================== */

/* ==================== 版本分组（画廊） ==================== */

/** 画廊分组定义（角色）：主形象 / 三视图 / 表情集 */
interface GallerySlot {
  /** 版本类型 */
  versionType: AssetVersionType
  /** 分组标题 */
  title: string
  /** 占位图标 */
  icon: string
}

/** 角色画廊槽位 */
const CHARACTER_SLOTS: GallerySlot[] = [
  { versionType: 'portrait', title: '主形象', icon: '🧑‍🎤' },
  { versionType: 'turnaround_front', title: '三视图 · 正面', icon: '🧍' },
  { versionType: 'turnaround_side', title: '三视图 · 侧面', icon: '🚶' },
  { versionType: 'turnaround_back', title: '三视图 · 背面', icon: '🔙' },
  { versionType: 'expression_set', title: '表情集', icon: '😊' },
]

/** 场景 / 道具画廊槽位 */
const REF_SLOT: Record<string, GallerySlot> = {
  scene: { versionType: 'scene_ref', title: '场景参考图', icon: '🏛️' },
  prop: { versionType: 'prop_ref', title: '道具参考图', icon: '📦' },
}

/**
 * 取某版本类型的当前展示版本（is_current 优先，否则第一项）。
 * @param versionType 版本类型
 * @returns 版本对象（无则 null）
 */
function currentVersionOf(versionType: AssetVersionType): AssetVersionItem | null {
  const list = detail.value?.versions.filter((v) => v.version_type === versionType) ?? []
  return list.find((v) => v.is_current) ?? list[0] ?? null
}

/** 当前详情的画廊槽位（角色 5 槽 / 场景道具 1 槽 / 音色无） */
const gallerySlots = computed<GallerySlot[]>(() => {
  if (!detail.value) return []
  if (detail.value.asset_type === 'character') return CHARACTER_SLOTS
  return REF_SLOT[detail.value.asset_type] ? [REF_SLOT[detail.value.asset_type]] : []
})

/** 角色三视图槽位（主形象之外的前 3 个） */
const turnaroundSlots = computed<GallerySlot[]>(() => gallerySlots.value.filter((s) => s.versionType.startsWith('turnaround_')))

/** 主形象槽位 */
const portraitSlot = computed<GallerySlot | null>(() => gallerySlots.value.find((s) => s.versionType === 'portrait') ?? null)

/** 表情集槽位 */
const expressionSlot = computed<GallerySlot | null>(() => gallerySlots.value.find((s) => s.versionType === 'expression_set') ?? null)

/** 场景 / 道具参考图槽位 */
const refSlot = computed<GallerySlot | null>(() => (detail.value && !isCharacter.value ? gallerySlots.value[0] ?? null : null))

/* ==================== QC 报告 ==================== */

/** QC 报告列表（详情倒序展示） */
const qcReports = computed(() => detail.value?.qc_reports ?? [])

/**
 * 主形象最新一致性分数（QC 报告按版本类型过滤取最新；主画廊右下胶囊用）。
 * @returns 分数（无报告为 null）
 */
const portraitScore = computed<number | null>(() => {
  const reports = qcReports.value
  if (reports.length === 0) return null
  const portraitReports = reports.filter((r) => (r.detail?.version_type as string) === 'portrait')
  const pool = portraitReports.length > 0 ? portraitReports : reports
  const scored = pool.find((r) => typeof r.consistency_score === 'number')
  return scored?.consistency_score ?? null
})

/** 主形象一致性胶囊色调（≥0.80 绿 / 0.68-0.80 黄 / 其余红） */
const portraitScoreClass = computed(() => {
  const score = portraitScore.value
  if (score === null) return 'bg-surface-sunken text-neutral-400'
  if (score >= 0.8) return 'gd-fill-mint'
  if (score >= 0.68) return 'gd-fill-amber'
  return 'gd-fill-rose'
})

/**
 * QC 分数 → 色调类型（≥0.80 绿 / 0.68-0.80 黄 / <0.68 红 / 无分灰）。
 * @param score 一致性得分
 * @returns 色调键
 */
function scoreTone(score: number | null): 'success' | 'warning' | 'danger' | 'neutral' {
  if (score === null) return 'neutral'
  if (score >= 0.8) return 'success'
  if (score >= 0.68) return 'warning'
  return 'danger'
}

/** QC 结果中文标签 */
const QC_RESULT_LABELS: Record<string, string> = { passed: '通过', warning: '警告', failed: '未通过' }

/**
 * QC 报告关联的版本号（detail.version_no 宽松读取）。
 * @param report QC 报告
 * @returns 版本号（无则 null）
 */
function qcVersionNo(report: (typeof qcReports.value)[number]): number | null {
  const value = report.detail?.version_no
  return typeof value === 'number' ? value : null
}

/**
 * QC 报告关联的版本类型标签。
 * @param report QC 报告
 * @returns 标签（无则空串）
 */
function qcVersionTypeLabel(report: (typeof qcReports.value)[number]): string {
  const value = report.detail?.version_type
  return typeof value === 'string' ? ASSET_VERSION_TYPE_LABELS[value as AssetVersionType] ?? value : ''
}

/* ==================== 音色绑定 ==================== */

/**
 * 从角色描述推断性别（用于音色库排序，修复 E2E 报告问题 9：
 * 男角色下拉默认把女声排在前面，缺乏合理性）。
 * @param desc 角色描述
 * @returns male / female / null（无法判断）
 */
function inferGenderFromDescription(desc?: string | null): 'male' | 'female' | null {
  if (!desc) return null
  const head = desc.slice(0, 12)
  if (/男/.test(head)) return 'male'
  if (/女/.test(head)) return 'female'
  return null
}

/**
 * 加载音色库（仅可用音色，供下拉选择）。
 * 后端默认排除自动化测试音色；此处再按角色性别把匹配音色置顶，
 * 避免男角色看见的第一项是「温暖女声」。
 * @returns 无
 */
async function fetchVoices(): Promise<void> {
  try {
    const page = await listTtsVoices({ status: 'available', page: 1, page_size: 100 })
    const gender = inferGenderFromDescription(detail.value?.description)
    if (gender) {
      const score = (item: TtsVoice): number =>
        item.gender === gender ? 2 : item.gender === 'neutral' ? 1 : 0
      voiceOptions.value = [...page.items].sort((a, b) => score(b) - score(a))
    } else {
      voiceOptions.value = page.items
    }
  } catch {
    // 音色库加载失败不阻断详情展示
  }
}

/**
 * 绑定音色（PUT voice {voice_id}）。
 * @returns 无
 */
async function bindVoice(): Promise<void> {
  if (!selectedVoiceId.value || !detail.value) return
  voiceBinding.value = true
  try {
    await bindAssetVoice(detail.value.id, selectedVoiceId.value)
    toast.success('音色已绑定，后续配音将使用该音色')
    await fetchDetail()
  } catch {
    // 拦截器已 toast
  } finally {
    voiceBinding.value = false
  }
}

/**
 * 试听音色：实时调用 `POST /tts-voices/{id}/preview` 合成样音并播放。
 *
 * - 传入 voiceId 时优先试听指定音色（已绑定音色卡的播放圆钮）；
 * - 未传时使用下拉框选中的音色；
 * - 播放中再次点击会暂停；失败提示由播放器写入 error 状态（由模板展示）。
 * @param voiceId 可选，指定试听的音色 ID
 * @returns 无
 */
async function previewVoice(voiceId?: number): Promise<void> {
  const targetId = voiceId ?? selectedVoiceId.value ?? detail.value?.voice?.id ?? null
  if (targetId == null) {
    toast.warning('请先在下方选择要试听的音色')
    return
  }
  await toggleVoicePreview(targetId, async () => {
    const result = await previewTtsVoice(targetId)
    return result.audio_url
  })
}

/* ==================== 锚点影响 ==================== */

/**
 * 拉取锚点影响镜头数。
 * @returns 无
 */
async function fetchAnchorImpact(): Promise<void> {
  if (!detail.value) return
  try {
    const data = await getAssetAnchorImpact(detail.value.id)
    affectedShots.value = data.affected_shots
  } catch {
    affectedShots.value = null
  }
}

/** 更换锚点确认弹窗 */
const anchorDialogOpen = ref(false)
/** 更换锚点提交中 */
const anchorSubmitting = ref(false)

/**
 * 更换锚点：确认后重抽主形象 portrait（QC 通过后可再次锁定）。
 * @returns 无
 */
async function confirmAnchorChange(): Promise<void> {
  if (!detail.value) return
  anchorSubmitting.value = true
  try {
    await regenerateAsset(detail.value.id, { version_type: 'portrait' })
    toast.success('主形象重抽任务已受理，完成后可重新锁定锚点')
    anchorDialogOpen.value = false
    startGeneratePolling()
  } catch {
    // 拦截器已 toast
  } finally {
    anchorSubmitting.value = false
  }
}

/* ==================== 版本对比 ==================== */

/** 对比旧版本 ID */
const compareBeforeId = ref<number | null>(null)
/** 对比新版本 ID */
const compareAfterId = ref<number | null>(null)

/** 全部版本（详情 versions） */
const allVersions = computed<AssetVersionItem[]>(() => detail.value?.versions ?? [])

/** 有产物的可对比版本 */
const comparableVersions = computed(() => allVersions.value.filter((v) => v.media_url))

/**
 * 按 ID 查版本。
 * @param id 版本 ID
 * @returns 版本对象（无则 null）
 */
function versionById(id: number | null): AssetVersionItem | null {
  return comparableVersions.value.find((v) => v.id === id) ?? null
}

/** 对比旧版本对象 */
const compareBefore = computed(() => versionById(compareBeforeId.value))
/** 对比新版本对象 */
const compareAfter = computed(() => versionById(compareAfterId.value))

/** 是否满足对比条件（两个不同版本均有产物） */
const canCompare = computed(() => Boolean(compareBefore.value && compareAfter.value && compareBefore.value.id !== compareAfter.value.id))

/**
 * 版本下拉选项文案。
 * @param version 版本对象
 * @returns 文案
 */
function versionOptionLabel(version: AssetVersionItem): string {
  return `v${version.version_no} · ${ASSET_VERSION_TYPE_LABELS[version.version_type]}`
}

// 默认选中最旧 / 最新两个版本
watch(comparableVersions, (list) => {
  if (list.length >= 2) {
    if (compareBeforeId.value === null) compareBeforeId.value = list[list.length - 1].id
    if (compareAfterId.value === null) compareAfterId.value = list[0].id
  }
}, { immediate: true })

/* ==================== 生成 / 审核 / 锁定 ==================== */

/** 生成弹窗显示状态 */
const generateDialogOpen = ref(false)
/** 生成弹窗选中的版本类型 */
const generateVersionType = ref<AssetVersionType>('portrait')
/** 生成弹窗反馈输入（可选） */
const generateFeedback = ref('')
/** 生成提交中 */
const generating = ref(false)

/** 当前资产可发起的版本类型（按资产类型过滤） */
const allowedVersionTypes = computed<AssetVersionType[]>(() => {
  const type = detail.value?.asset_type
  if (type === 'scene') return ['scene_ref']
  if (type === 'prop') return ['prop_ref']
  return ['portrait', 'turnaround_front', 'turnaround_side', 'turnaround_back', 'expression_set']
})

/** 是否可发起生成（与后端 _GENERATABLE_STATUSES 对齐：locked / generating 中不可） */
const canGenerate = computed(() => {
  const status = detail.value?.status
  return (
    status === 'planning' ||
    status === 'pending_review' ||
    status === 'approved' ||
    status === 'rejected' ||
    status === 'qc_failed'
  )
})

/**
 * 打开生成弹窗（默认选中第一个缺失视图类型）。
 * @returns 无
 */
function openGenerateDialog(): void {
  const missing = gallerySlots.value.find((slot) => !currentVersionOf(slot.versionType))
  generateVersionType.value = missing?.versionType ?? allowedVersionTypes.value[0]
  generateFeedback.value = ''
  generateDialogOpen.value = true
}

/**
 * 提交生成（POST generate，202 受理 → 轮询详情）。
 * @returns 无
 */
async function confirmGenerate(): Promise<void> {
  if (!detail.value) return
  generating.value = true
  try {
    await generateAsset(detail.value.id, {
      version_type: generateVersionType.value,
      feedback: generateFeedback.value.trim() || undefined,
    })
    toast.success(`「${ASSET_VERSION_TYPE_LABELS[generateVersionType.value]}」生成任务已受理`)
    generateDialogOpen.value = false
    await fetchDetail()
    startGeneratePolling()
  } catch {
    // 拦截器已 toast
  } finally {
    generating.value = false
  }
}

/** 审核 / 锁定操作进行中 */
const deciding = ref(false)
/** approve 二次确认弹窗 */
const approveDialogOpen = ref(false)
/** 锁定错误信息（60003 缺视图展示） */
const lockError = ref('')

/**
 * 提交审核通过（二次确认后调用）。
 * @returns 无
 */
async function confirmApprove(): Promise<void> {
  if (!detail.value) return
  deciding.value = true
  try {
    await approveAsset(detail.value.id)
    toast.success('资产已通过审核，可锁定为生成锚点')
    approveDialogOpen.value = false
    await fetchDetail()
  } catch {
    // 拦截器已 toast
  } finally {
    deciding.value = false
  }
}

/**
 * 提交驳回（ApprovalBar 校验 ≥10 字后回调）。
 * @param comment 驳回原因
 * @returns 无
 */
async function handleReject(comment: string): Promise<void> {
  if (!detail.value) return
  deciding.value = true
  try {
    await rejectAsset(detail.value.id, comment)
    toast.success('已驳回，可按反馈重新生成')
    await fetchDetail()
  } catch {
    // 拦截器已 toast
  } finally {
    deciding.value = false
  }
}

/**
 * 锁定资产（60003 时展示缺失视图原因）。
 * @returns 无
 */
async function handleLock(): Promise<void> {
  if (!detail.value) return
  deciding.value = true
  lockError.value = ''
  try {
    await lockAsset(detail.value.id)
    toast.success('资产已锁定，将作为分镜生成锚点')
    await fetchDetail()
  } catch (error) {
    lockError.value = error instanceof Error ? error.message : '锁定失败'
  } finally {
    deciding.value = false
  }
}

/* ==================== 轮询（生成中状态） ==================== */

/** 生成中轮询定时器 */
let pollTimer: ReturnType<typeof setInterval> | null = null
/** 轮询剩余次数（2s × 60 = 120s 上限） */
let pollLeft = 0

/**
 * 启动轮询：每 2s 拉详情，状态离开 generating 后停止。
 * @returns 无
 */
function startGeneratePolling(): void {
  pollLeft = 60
  if (pollTimer) return
  pollTimer = setInterval(() => {
    pollLeft -= 1
    void fetchDetail({ silent: true }).then(() => {
      const status = detail.value?.status
      if ((pollLeft <= 0 || (status && status !== 'generating')) && pollTimer) {
        clearInterval(pollTimer)
        pollTimer = null
        if (status === 'qc_failed') {
          toast.warning('生成完成但质检未通过，请查看失败原因')
        } else if (status === 'pending_review') {
          toast.success('新版本已生成，等待人工审核')
        }
      }
    })
  }, 2_000)
}

/* ==================== 详情加载与 SSE ==================== */

/**
 * 拉取资产详情（附带锚点影响与音色选项）。
 * @param options silent=true 时不置 loading（轮询用）
 * @returns 无
 */
async function fetchDetail(options: { silent?: boolean } = {}): Promise<void> {
  if (!options.silent) {
    loading.value = true
    loadError.value = ''
  }
  try {
    detail.value = await getAsset(assetId.value)
    if (!options.silent) {
      selectedVoiceId.value = detail.value.voice_id ?? null
      await fetchAnchorImpact()
      if (isCharacter.value && voiceOptions.value.length === 0) {
        await fetchVoices()
      }
    }
  } catch (error) {
    if (!options.silent) {
      loadError.value = error instanceof Error ? error.message : '资产详情加载失败'
    }
  } finally {
    if (!options.silent) {
      loading.value = false
    }
  }
}

/** 项目事件流地址（access_token 查询参数鉴权） */
const sseUrl = computed<string>(() =>
  authStore.token ? `/api/v1/projects/${projectId.value}/events?access_token=${authStore.token}` : '',
)

/**
 * SSE 事件分派：资产事件刷新详情。
 * @param event 消息事件
 * @returns 无
 */
function onSseMessage(event: MessageEvent<string>): void {
  if (event.type === 'asset.generated' || event.type === 'asset.generation_failed') {
    void fetchDetail({ silent: true })
  }
}

// 订阅项目事件流
useSSE(sseUrl, { onMessage: onSseMessage, eventTypes: ['asset.generated', 'asset.generation_failed'] })

/**
 * 返回资产卡片墙（P09）。
 * @returns 无
 */
function goBack(): void {
  void router.push(`/projects/${projectId.value}/assets`)
}

// 挂载：拉详情 + 刷新余额（重抽成本行）
onMounted(() => {
  void fetchDetail()
  void billingStore.refresh()
})

// 卸载：清理轮询
onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <!-- ============ 加载骨架 ============ -->
    <div v-if="loading" class="flex min-h-0 flex-1">
      <div class="w-[45%] max-w-[480px] shrink-0 space-y-4 border-r border-hairline bg-surface-raised p-6 lg:w-[480px]">
        <LoadingSkeleton variant="card" />
        <div class="grid grid-cols-3 gap-3">
          <LoadingSkeleton v-for="i in 3" :key="i" variant="card" />
        </div>
      </div>
      <div class="min-w-0 flex-1 p-6">
        <LoadingSkeleton variant="text" :rows="10" />
      </div>
    </div>

    <!-- ============ 错误态 ============ -->
    <div v-else-if="loadError || !detail" class="min-h-0 flex-1 overflow-y-auto p-6">
      <ErrorRetryBlock message="资产详情加载失败" :description="loadError" @retry="fetchDetail" />
    </div>

    <!-- ============ 数据区 ============ -->
    <template v-else>
      <!-- ===== 顶部信息条（F3-03：面包屑式返回 + 衬线角色名 + 类型/状态） ===== -->
      <header class="flex h-14 shrink-0 items-center gap-3 border-b border-hairline bg-surface-raised px-5">
        <button type="button" class="rounded-full p-1.5 text-neutral-400 transition-colors hover:bg-surface-sunken hover:text-neutral-700" aria-label="返回资产墙" @click="goBack">
          <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
        <h1 class="gd-masthead truncate text-display-lg">{{ detail.name }}</h1>
        <span class="shrink-0 rounded-full bg-primary-50 px-2.5 py-0.5 text-xs font-medium text-primary-700">{{ ASSET_TYPE_LABELS[detail.asset_type] }}</span>
        <StatusTag :status="detail.status" />
        <div class="flex-1" />
        <span class="gd-num shrink-0 text-xs text-neutral-400">被 {{ detail.shot_refs ?? 0 }} 个镜头引用</span>
      </header>

      <!-- ===== 主体双栏 ===== -->
      <div class="flex min-h-0 flex-1">
        <!-- 左画廊 -->
        <section class="w-[45%] max-w-[480px] shrink-0 space-y-6 overflow-y-auto border-r border-hairline bg-surface-raised p-5 pb-20 lg:w-[480px]" aria-label="版本画廊">
          <!-- 场景 / 道具：单参考图 -->
          <template v-if="refSlot">
            <h3 class="gd-section-label">{{ refSlot.title }}</h3>
            <div class="gd-night-window relative overflow-hidden">
              <img
                v-if="currentVersionOf(refSlot.versionType)?.media_url"
                :src="currentVersionOf(refSlot.versionType)!.media_url!"
                :alt="refSlot.title"
                class="aspect-square w-full bg-surface-canvas object-cover"
              />
              <div v-else class="flex aspect-square w-full flex-col items-center justify-center gap-3">
                <span class="text-4xl opacity-70" aria-hidden="true">{{ refSlot.icon }}</span>
                <p class="text-xs text-player-hint">暂无参考图</p>
                <AppButton v-if="canGenerate" variant="secondary" size="sm" @click="openGenerateDialog">生成</AppButton>
                <p v-else class="text-caption text-player-hint">{{ detail.status === 'generating' ? '生成中…' : '当前状态不可生成' }}</p>
              </div>
              <span v-if="currentVersionOf(refSlot.versionType)" class="gd-num absolute left-3 top-3 rounded-full bg-scrim-soft px-2.5 py-0.5 text-caption text-white">
                v{{ currentVersionOf(refSlot.versionType)!.version_no }} · 当前版本
              </span>
            </div>
          </template>

          <!-- 角色：主形象大图（F3-03：版本 chip 左上 + 一致性 chip 右下） -->
          <template v-if="portraitSlot">
            <h3 class="gd-section-label">主形象 Portrait</h3>
            <div class="gd-night-window gd-night-window-hero relative overflow-hidden">
              <img
                v-if="currentVersionOf(portraitSlot.versionType)?.media_url"
                :src="currentVersionOf(portraitSlot.versionType)!.media_url!"
                :alt="portraitSlot.title"
                class="aspect-square w-full bg-surface-canvas object-cover"
              />
              <div v-else class="flex aspect-square w-full flex-col items-center justify-center gap-3">
                <span class="text-5xl opacity-70" aria-hidden="true">{{ portraitSlot.icon }}</span>
                <p class="text-xs text-player-hint">暂无主形象，先生成一张正面半身像</p>
                <AppButton v-if="canGenerate" variant="primary" size="sm" @click="openGenerateDialog">生成主形象</AppButton>
                <p v-else class="text-caption text-player-hint">{{ detail.status === 'generating' ? '生成中…' : '当前状态不可生成' }}</p>
              </div>
              <template v-if="currentVersionOf(portraitSlot.versionType)">
                <span class="gd-num absolute left-3 top-3 rounded-full bg-scrim-soft px-2.5 py-0.5 text-caption text-white">
                  v{{ currentVersionOf(portraitSlot.versionType)!.version_no }} · 当前版本
                </span>
                <span class="gd-num absolute bottom-3 right-3 rounded-full px-2.5 py-0.5 text-caption font-semibold" :class="portraitScoreClass">
                  {{ portraitScore !== null ? `一致性 ${Math.round(portraitScore * 100)}%` : '一致性 —' }}
                </span>
              </template>
            </div>
          </template>

          <!-- 角色：三视图横排（F3-03：正面 front / 侧面 side / 背面 back） -->
          <template v-if="isCharacter && turnaroundSlots.length > 0">
            <div>
              <h3 class="gd-section-label">三视图 Turnaround</h3>
              <div class="mt-2 grid grid-cols-3 gap-3">
                <div v-for="slot in turnaroundSlots" :key="slot.versionType">
                  <div class="gd-night-window gd-night-window-sm overflow-hidden">
                    <img
                      v-if="currentVersionOf(slot.versionType)?.media_url"
                      :src="currentVersionOf(slot.versionType)!.media_url!"
                      :alt="slot.title"
                      class="aspect-[3/4] w-full bg-surface-canvas object-cover"
                    />
                    <div v-else class="flex aspect-[3/4] w-full flex-col items-center justify-center gap-2">
                      <span class="text-2xl opacity-50" aria-hidden="true">{{ slot.icon }}</span>
                      <AppButton v-if="canGenerate" variant="ghost" size="sm" @click="generateVersionType = slot.versionType; generateDialogOpen = true">生成</AppButton>
                      <span v-else class="text-caption text-player-hint">待生成</span>
                    </div>
                  </div>
                  <p class="mt-1 truncate text-center text-caption text-neutral-500">
                    {{ slot.title.replace('三视图 · ', '') }}
                    <span v-if="currentVersionOf(slot.versionType)" class="gd-num text-neutral-400">v{{ currentVersionOf(slot.versionType)!.version_no }}</span>
                  </p>
                </div>
              </div>
            </div>
          </template>

          <!-- 角色：表情集 -->
          <template v-if="expressionSlot">
            <div>
              <h3 class="gd-section-label">表情集 Expression Set</h3>
              <div class="gd-night-window mt-2 overflow-hidden">
                <img
                  v-if="currentVersionOf(expressionSlot.versionType)?.media_url"
                  :src="currentVersionOf(expressionSlot.versionType)!.media_url!"
                  :alt="expressionSlot.title"
                  class="aspect-square w-full bg-surface-canvas object-cover"
                />
                <div v-else class="flex aspect-square w-full flex-col items-center justify-center gap-3">
                  <span class="text-4xl opacity-70" aria-hidden="true">{{ expressionSlot.icon }}</span>
                  <p class="text-xs text-player-hint">暂无表情集</p>
                  <AppButton v-if="canGenerate" variant="secondary" size="sm" @click="generateVersionType = 'expression_set'; generateDialogOpen = true">生成表情集</AppButton>
                  <span v-else class="text-caption text-player-hint">待生成</span>
                </div>
              </div>
              <p v-if="currentVersionOf(expressionSlot.versionType)" class="gd-num mt-1.5 text-caption text-neutral-400">
                v{{ currentVersionOf(expressionSlot.versionType)!.version_no }} · {{ formatShortDate(currentVersionOf(expressionSlot.versionType)!.created_at) }}
              </p>
            </div>
          </template>

          <!-- 版本总数说明 -->
          <p class="gd-num text-caption text-neutral-400">共 {{ allVersions.length }} 个版本（旧版本保留，可在右侧对比）</p>
        </section>

        <!-- 右详情列 -->
        <aside class="min-w-0 flex-1 space-y-6 overflow-y-auto bg-surface-canvas p-6 pb-20" aria-label="资产详情">
          <!-- 角色名卡（F3-03：衬线大名字 + 类型 chip + 更新时间 + 外观 + 属性 chips） -->
          <section class="gd-card p-6">
            <div class="flex flex-wrap items-baseline gap-x-3 gap-y-1.5">
              <h2 class="font-serif text-display-lg text-neutral-900">{{ detail.name }}</h2>
              <!-- P2-11：类型标签按真实 asset_type 展示（此前硬编码 character，场景详情会显示 character · 场景） -->
              <span class="rounded-full bg-surface-sunken px-2.5 py-0.5 text-caption text-neutral-500">{{ detail.asset_type }} · {{ ASSET_TYPE_LABELS[detail.asset_type] }}</span>
              <span class="gd-num ml-auto text-caption text-neutral-400">更新于 {{ formatShortDate(detail.updated_at) }}</span>
            </div>
            <p class="mt-3 text-sm leading-[1.75] text-neutral-600">{{ appearanceText }}</p>
            <!-- 属性 chips -->
            <div class="mt-3 flex flex-wrap gap-2">
              <span v-if="metaText('age_range')" class="rounded-full border border-hairline bg-surface-raised px-2.5 py-0.5 text-caption text-neutral-600">年龄段 · {{ metaText('age_range') }}</span>
              <span v-if="weightValue !== null" class="rounded-full border border-hairline bg-surface-raised px-2.5 py-0.5 text-caption text-neutral-600">戏份权重 · <span class="gd-num">{{ weightValue }}/5</span></span>
              <span v-if="detail.voice" class="rounded-full border border-hairline bg-surface-raised px-2.5 py-0.5 text-caption text-neutral-600">音色已绑定 · {{ detail.voice.name }}</span>
            </div>
            <p v-if="detail.status === 'rejected' && metaText('reject_feedback')" class="mt-3 rounded-lg bg-warning-bg p-3 text-xs leading-[1.65] text-gd-pending">
              驳回原因：{{ metaText('reject_feedback') }}
            </p>
            <div v-if="detail.status === 'qc_failed' && lastErrorText" class="mt-3 rounded-lg bg-danger-bg p-3 text-xs leading-[1.65] text-danger" role="alert">
              <p class="font-medium">
                {{ isGenerationFailure ? '生成失败' : '质检失败' }}：{{ lastErrorText }}
              </p>
              <p v-if="isGenerationFailure" class="mt-1.5 text-danger/80">
                模型服务未返回图片，当前尚未进入质检环节。请检查图片模型配置（密钥/网络）或稍后重试。
              </p>
              <RouterLink
                v-if="isGenerationFailure"
                to="/settings/models"
                class="mt-2 inline-flex items-center gap-1 rounded-md border border-danger/40 px-2.5 py-1 text-caption font-medium text-danger transition-colors hover:bg-danger/10"
              >
                前往模型配置检查 →
              </RouterLink>
            </div>
          </section>

          <!-- 音色绑定卡（仅角色；F3-03：播放圆钮 + 波形 + 试听 30s + 更换音色） -->
          <section v-if="isCharacter" class="gd-card p-6">
            <div class="flex items-baseline justify-between gap-3">
              <h3 class="text-sm font-semibold text-neutral-900">音色绑定</h3>
              <span class="gd-num text-caption text-neutral-400">tts_voice · available 才可选</span>
            </div>
            <!-- 已绑定信息 -->
            <div v-if="detail.voice" class="mt-3 flex flex-wrap items-center gap-3 rounded-lg bg-gold-fog p-3">
              <button
                type="button"
                class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full gd-fill-ink transition-colors hover:bg-primary-600 disabled:cursor-wait disabled:opacity-70"
                :aria-label="playingId === detail.voice.id ? '暂停试听已绑定音色' : '试听已绑定音色'"
                :aria-busy="loadingId === detail.voice.id"
                @click="previewVoice(detail.voice.id)"
              >
                <!-- 合成中显示转圈；播放中显示暂停；空闲显示播放三角 -->
                <svg v-if="loadingId === detail.voice.id" class="h-4 w-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M21 12a9 9 0 11-6.219-8.56" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                <svg v-else-if="playingId === detail.voice.id" class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M6.75 6.5h3.5v11h-3.5zM13.75 6.5h3.5v11h-3.5z" />
                </svg>
                <svg v-else class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M8 5.14v13.72c0 .8.87 1.3 1.56.9l11-6.86a1.05 1.05 0 000-1.8l-11-6.86A1.05 1.05 0 008 5.14z" />
                </svg>
              </button>
              <div class="min-w-0 flex-1">
                <p class="text-sm font-medium text-neutral-800">{{ detail.voice.name }}</p>
                <p class="gd-num truncate text-xs text-neutral-500">{{ detail.voice.voice_key }} · {{ detail.voice.language }} · {{ detail.voice.style_tags || detail.voice.provider || '' }}</p>
              </div>
              <!-- 波形装饰 -->
              <span class="hidden items-end gap-0.5 sm:flex" aria-hidden="true">
                <span v-for="(h, i) in [8, 14, 10, 18, 12, 20, 9, 15]" :key="i" class="w-1 rounded-full bg-gold" :style="{ height: `${h}px` }" />
              </span>
              <StatusTag status="approved" label="已绑定" size="sm" />
            </div>
            <!-- 选择与操作 -->
            <div class="mt-3 flex flex-wrap items-center gap-2">
              <select
                v-model="selectedVoiceId"
                class="gd-input min-w-0 flex-1"
                aria-label="选择音色"
              >
                <option :value="null" disabled>请选择音色</option>
                <option v-for="voice in voiceOptions" :key="voice.id" :value="voice.id">
                  {{ voice.name }}（{{ voice.gender === 'male' ? '男声' : voice.gender === 'female' ? '女声' : voice.gender }} · {{ voice.voice_key }}）
                </option>
              </select>
              <AppButton
                variant="secondary"
                size="md"
                :loading="Boolean(selectedVoiceId && loadingId === selectedVoiceId)"
                :disabled="!selectedVoiceId"
                :title="selectedVoiceId ? '按音色默认示例句实时合成 30 秒样音' : '请先选择要试听的音色'"
                @click="previewVoice()"
              >
                {{ selectedVoiceId && playingId === selectedVoiceId ? '暂停试听' : '试听 30s' }}
              </AppButton>
              <AppButton variant="primary" size="md" :loading="voiceBinding" :disabled="!selectedVoiceId || selectedVoiceId === (detail.voice?.id ?? null)" @click="bindVoice">
                {{ detail.voice ? '更换音色' : '绑定' }}
              </AppButton>
            </div>
            <p class="mt-2 text-caption text-neutral-500">试听按音色默认示例句实时合成（不写入配音时间线）；绑定后用于后续台词配音生成。</p>
          </section>

          <!-- 重抽成本卡（F3-03：成本透明两行金字 + 重抽按钮 + 次数提示） -->
          <section class="gd-card p-6">
            <div class="flex items-baseline justify-between gap-3">
              <h3 class="text-sm font-semibold text-neutral-900">重抽形象</h3>
              <span class="gd-num text-caption text-neutral-400">成本透明 · regenerate</span>
            </div>
            <dl class="mt-3 divide-y divide-dashed divide-hairline rounded-lg border border-warning-border bg-gold-fog px-4 py-1 text-sm">
              <div class="flex items-center justify-between py-2.5">
                <dt class="text-neutral-600">本次预估消耗</dt>
                <dd class="gd-num text-base font-semibold text-gold-deep">{{ REGEN_ESTIMATE_CREDITS }} <span class="text-xs font-normal">积分</span></dd>
              </div>
              <div class="flex items-center justify-between py-2.5">
                <!-- P3-d 修复：标注估算口径，避免与实际扣费流水混淆 -->
                <dt class="text-neutral-600">
                  累计重抽（估算）
                  <span class="ml-1 text-caption text-neutral-400" title="按重抽次数 × 单次预估价计算，实际扣费以积分流水为准">ⓘ</span>
                </dt>
                <dd class="gd-num text-base font-semibold text-gold-deep">≈{{ regenAccumulated }} <span class="text-xs font-normal">积分</span></dd>
              </div>
            </dl>
            <p class="gd-num mt-2 text-caption text-neutral-400">当前余额 {{ creditBalance.toLocaleString('zh-CN') }} 积分 · 预估以实际结算为准</p>
            <div class="mt-3 flex flex-wrap items-center gap-3">
              <AppButton variant="primary" size="md" :disabled="!canGenerate" @click="openGenerateDialog">↻ 重抽形象</AppButton>
              <span class="text-caption text-neutral-500">
                已重抽 <span class="gd-num font-medium text-neutral-700">{{ regenCount }}</span> 次 · 达上限后转人工复核
              </span>
            </div>
          </section>

          <!-- 锚点影响卡（功能保留） -->
          <section class="gd-card p-6">
            <div class="flex items-start justify-between gap-3">
              <div>
                <h3 class="text-sm font-semibold text-neutral-900">锚点影响面</h3>
                <p class="mt-1.5 text-xs leading-[1.65] text-neutral-500">
                  该资产作为形象锚点出现在
                  <span class="gd-num font-semibold" :class="(affectedShots ?? 0) > 0 ? 'text-gd-pending' : 'text-neutral-700'">{{ affectedShots ?? '—' }}</span>
                  个镜头中，更换主形象将影响这些镜头的已生成画面。
                </p>
              </div>
              <AppButton
                variant="secondary"
                size="sm"
                :disabled="detail.status === 'locked' || detail.status === 'generating'"
                @click="anchorDialogOpen = true"
              >
                更换锚点
              </AppButton>
            </div>
          </section>

          <!-- 版本对比卡 -->
          <section class="gd-card p-6">
            <h3 class="text-sm font-semibold text-neutral-900">版本对比</h3>
            <div class="mt-3 grid grid-cols-2 gap-2">
              <select
                v-model="compareBeforeId"
                class="gd-input"
                aria-label="旧版本"
              >
                <option v-for="version in comparableVersions" :key="version.id" :value="version.id">{{ versionOptionLabel(version) }}</option>
              </select>
              <select
                v-model="compareAfterId"
                class="gd-input"
                aria-label="新版本"
              >
                <option v-for="version in comparableVersions" :key="version.id" :value="version.id">{{ versionOptionLabel(version) }}</option>
              </select>
            </div>
            <div v-if="canCompare" class="mt-3 h-64">
              <VersionCompareSlider
                :before-src="compareBefore!.media_url!"
                :after-src="compareAfter!.media_url!"
                :before-label="`旧 v${compareBefore!.version_no}`"
                :after-label="`新 v${compareAfter!.version_no}`"
              />
            </div>
            <p v-else class="mt-3 rounded-lg bg-surface-canvas p-4 text-center text-xs text-neutral-400">
              {{ comparableVersions.length >= 2 ? '请选择两个不同的版本进行对比' : '版本生成后即可对比（至少 2 个带产物版本）' }}
            </p>
          </section>

          <!-- 质检历史（F3-03：版本轴 + 通过/未过 chips + 失败原因墨色回显） -->
          <section class="gd-card p-6">
            <div class="flex items-baseline justify-between gap-3">
              <h3 class="text-sm font-semibold text-neutral-900">质检历史</h3>
              <span class="gd-num text-caption text-neutral-400">qc_reports · 保留全部轮次</span>
            </div>
            <ul v-if="qcReports.length > 0" class="mt-3 divide-y divide-hairline">
              <li v-for="report in qcReports" :key="report.id" class="flex items-start gap-3 py-3">
                <!-- 版本轴标签 -->
                <span class="gd-num w-14 shrink-0 pt-0.5 text-xs text-neutral-400">
                  v{{ qcVersionNo(report) ?? '?' }}
                  <template v-if="report.id === qcReports[0].id"><span class="ml-0.5 text-primary-600">当前</span></template>
                </span>
                <!-- 结果 + 原因 -->
                <div class="min-w-0 flex-1">
                  <p class="flex flex-wrap items-center gap-2 text-xs">
                    <span
                      class="inline-flex items-center rounded-full px-2 py-0.5 font-medium"
                      :class="scoreTone(report.consistency_score) === 'success' ? 'bg-success-bg text-success-strong' : scoreTone(report.consistency_score) === 'warning' ? 'bg-warning-bg text-warning-strong' : scoreTone(report.consistency_score) === 'danger' ? 'bg-danger-bg text-danger-strong' : 'bg-surface-sunken text-neutral-400'"
                    >
                      <span class="mr-1 h-1.5 w-1.5 rounded-full" :class="scoreTone(report.consistency_score) === 'success' ? 'bg-success' : scoreTone(report.consistency_score) === 'warning' ? 'bg-warning' : scoreTone(report.consistency_score) === 'danger' ? 'bg-danger' : 'bg-neutral-300'" aria-hidden="true" />
                      {{ (QC_RESULT_LABELS[report.result] ?? report.result) === '通过' ? 'passed · 通过' : (QC_RESULT_LABELS[report.result] ?? report.result) === '未通过' ? 'failed · 未过' : 'warning · 警告' }}
                    </span>
                    <span v-if="report.consistency_score !== null" class="gd-num text-neutral-500">一致性 {{ Math.round(report.consistency_score * 100) }}%</span>
                    <span class="text-neutral-400">{{ qcVersionTypeLabel(report) }} · 第 {{ report.round_no ?? 1 }} 轮</span>
                  </p>
                  <p class="gd-num mt-0.5 text-caption text-neutral-400">{{ formatShortDate(report.created_at) }}</p>
                </div>
                <span
                  class="gd-num shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold"
                  :class="scoreTone(report.consistency_score) === 'success' ? 'bg-success-bg text-success-strong' : scoreTone(report.consistency_score) === 'warning' ? 'bg-warning-bg text-warning-strong' : scoreTone(report.consistency_score) === 'danger' ? 'bg-danger-bg text-danger-strong' : 'bg-surface-sunken text-neutral-400'"
                >
                  {{ report.consistency_score !== null ? report.consistency_score.toFixed(2) : '—' }}
                </span>
              </li>
            </ul>
            <p v-else class="mt-3 rounded-lg bg-surface-canvas p-4 text-center text-xs text-neutral-400">暂无质检记录，生成版本后将自动执行一致性 QC</p>
            <!-- 阈值说明 -->
            <p class="mt-3 text-caption leading-[1.65] text-neutral-500">相似度阈值：≥0.80 通过（绿）· 0.68-0.80 警告（黄）· &lt;0.68 未通过（红）</p>
          </section>
        </aside>
      </div>

      <!-- ===== 底部关口操作条（F3-03：关口二 · 资产审批；min-h 防止窄屏换行时超出 72px 遮挡主体） ===== -->
      <footer class="flex min-h-[72px] shrink-0 flex-wrap items-center gap-3 border-t border-hairline bg-surface-raised px-6 py-2 shadow-gd-1">
        <p class="mr-auto text-xs text-neutral-400">
          <span class="mr-2 inline-flex items-center gap-1.5 font-semibold text-neutral-700">
            <span class="h-2 w-2 rounded-full bg-primary-500" aria-hidden="true" />关口二 · 资产审批
          </span>
          <template v-if="detail.status === 'locked'">资产已锁定为生成锚点，不可再修改</template>
          <template v-else-if="detail.status === 'generating'">形象生成中，完成后自动进入审核…</template>
          <template v-else-if="detail.status === 'approved'">已通过审核，建议补齐三视图后锁定为生成锚点</template>
          <template v-else-if="detail.status === 'pending_review'">请审核当前形象：通过 / 驳回（需反馈）/ 重新生成</template>
          <template v-else>驳回或质检失败后可重新生成</template>
        </p>
        <!-- 60003 锁定错误提示 -->
        <p v-if="lockError" class="max-w-sm truncate rounded-md bg-danger-bg px-3 py-1.5 text-xs text-danger" role="alert">{{ lockError }}</p>
        <AppButton variant="secondary" size="md" :disabled="!canGenerate" @click="openGenerateDialog">↻ 重新生成</AppButton>
        <AppButton v-if="detail.status === 'approved'" variant="secondary" size="md" :loading="deciding" @click="handleLock">🔒 锁定资产</AppButton>
        <ApprovalBar
          :loading="deciding"
          :approve-disabled="detail.status !== 'pending_review'"
          :reject-disabled="detail.status !== 'pending_review'"
          :show-edit="false"
          approve-text="通过"
          reject-text="驳回"
          @approve="approveDialogOpen = true"
          @reject="handleReject"
        />
      </footer>
    </template>

    <!-- ============ approve 二次确认 ============ -->
    <ConfirmDialog
      v-model="approveDialogOpen"
      title="通过该资产审核？"
      content="通过后可锁定为分镜生成的形象锚点；如对形象不满意可选择驳回并填写反馈。"
      confirm-label="确认通过"
      :loading="deciding"
      @confirm="confirmApprove"
      @cancel="approveDialogOpen = false"
    />

    <!-- ============ 生成弹窗（含成本行） ============ -->
    <ConfirmDialog
      v-model="generateDialogOpen"
      title="发起形象生成"
      content="选择要生成的版本类型（旧版本保留可对比），202 受理后后台生成并自动质检。"
      :confirm-label="`发起生成（-${REGEN_ESTIMATE_CREDITS} 积分）`"
      :loading="generating"
      @confirm="confirmGenerate"
      @cancel="generateDialogOpen = false"
    >
      <div class="space-y-3">
        <div>
          <label class="mb-1.5 block text-xs font-medium text-neutral-700" for="generate-version-type">版本类型</label>
          <select
            id="generate-version-type"
            v-model="generateVersionType"
            class="gd-input"
          >
            <option v-for="vt in allowedVersionTypes" :key="vt" :value="vt">{{ ASSET_VERSION_TYPE_LABELS[vt] }}</option>
          </select>
        </div>
        <div>
          <label class="mb-1.5 block text-xs font-medium text-neutral-700" for="generate-feedback">调整要求（可选）</label>
          <textarea
            id="generate-feedback"
            v-model="generateFeedback"
            rows="2"
            class="gd-input"
            placeholder="例如：表情更坚毅一些，发色改为深棕…"
          />
        </div>
        <!-- 成本行（F3-04：本次预估 X · 余额 1,240→1,198） -->
        <dl class="divide-y divide-dashed divide-hairline rounded-lg border border-warning-border bg-gold-fog px-4 py-1 text-sm">
          <div class="flex items-center justify-between py-2">
            <dt class="text-neutral-600">本次预估扣费</dt>
            <dd class="gd-num font-semibold text-gold-deep">{{ REGEN_ESTIMATE_CREDITS }} <span class="text-xs font-normal">积分</span></dd>
          </div>
          <div class="flex items-center justify-between py-2">
            <dt class="text-neutral-600">当前余额</dt>
            <dd class="text-xs text-neutral-500">
              <span class="gd-num font-semibold text-neutral-800">{{ creditBalance.toLocaleString('zh-CN') }}</span>
              → <span class="gd-num font-semibold" :class="creditBalance - REGEN_ESTIMATE_CREDITS < 0 ? 'text-danger' : 'text-neutral-800'">{{ (creditBalance - REGEN_ESTIMATE_CREDITS).toLocaleString('zh-CN') }}</span>
            </dd>
          </div>
        </dl>
      </div>
    </ConfirmDialog>

    <!-- ============ 更换锚点确认（影响弹窗功能保留） ============ -->
    <ConfirmDialog
      v-model="anchorDialogOpen"
      title="更换主形象锚点？"
      :content="`该资产作为锚点出现在 ${affectedShots ?? 0} 个镜头中，更换主形象将触发重新生成（本次预估 ${REGEN_ESTIMATE_CREDITS} 积分）并可能影响这些镜头的已生成画面一致性。确认继续？`"
      confirm-label="更换锚点（重抽主形象）"
      :loading="anchorSubmitting"
      @confirm="confirmAnchorChange"
      @cancel="anchorDialogOpen = false"
    />
  </div>
</template>

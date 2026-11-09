<!--
  文件级注释：P09 · 资产卡片墙（HITL-2，路由 /projects/:projectId/assets，AppShell 布局）。
  v8 · 按 stitch_prd/p3（P3 资产管理与一致性质检墙）Redesign-Preserve 对齐「功能布局」，
  主题保持 aurora 令牌体系（gd-* / surface-* / 语义五态），不引入新色值 / 字体 / 依赖：
  - 刊头：左缘品牌竖条 + STAGE 03 / HITL-GATE 02 徽标 + 脉冲审批状态 + 衬线大标题
    「资产管理与一致性质检墙 · ASSET MATRIX」+ lede（| 分隔 + HITL 关口二品牌句）+
    右侧工具区（搜索 / 批量审核 / 导出资产清单 / 智能规划主按钮）；
  - 四张质检统计卡（全剧达标率 / 三实体锚点锁定 / 质检未过红警示卡 / 专属 TTS 音色绑定）：
    数据全部来自 refreshGateStats 三类型轻快照（真实 status / voice_id 聚合），警示卡红 ring；
  - 筛选行：类型 tab 药丸组（全部资产 / 角色 / 场景 / 道具 / 音色，带真实计数）+
    右侧三段一致性图例（≥0.90 / 0.80-0.89 / <0.80，纯结构件）+ 快捷状态 chips（qc_failed 红点）+
    全量状态下拉（8 态筛选保留）；
  - 资产卡网格（1→2→3 列）：卡头（名 + 类型 tag + 版本/质检结论列）→ 夜窗（gd-cv 渐变 +
    ASSET_ID / 来源徽标 + 底部状态·更新时间行）→ 版本基准槽位组（角色=正交三视图，
    场景/道具=参考图）→ 特征标签 chips → 音色绑定条 → 状态化页脚操作（通过/驳回/重抽/
    带反馈重抽/锁定徽标）；qc_failed 卡红 ring + 顶部红警示条（meta.last_error 真实数据）；
  - 底部 HITL 关口审批条（sticky 于滚动容器）：图标块 + 「前置阻塞关口」徽章 +
    达标摘要（待人工核检红字）+ 三按钮（批量审核 / 真人授权留痕 / 批准全部并锁定进入分镜）。
  功能逻辑（三态加载 / SSE / 轮询 / 弹窗 / 批量 / 搜索 / 导出 / 空态 / aria / 响应式）全部保留，
  仅布局结构与交互骨架按原型 p3 重排。
-->
<script setup lang="ts">
/**
 * AssetWallView 资产卡片墙（HITL-2）。
 * props：无。emits：无。
 * 依赖 assets API（列表 / 审核 / 生成 / 规划 / 音色库）、useSSE、useToast。
 * 布局按 stitch_prd/p3 对齐：刊头 + 统计卡 + 筛选行 + 3 列资产矩阵 + 底部关口审批条。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  approveAsset,
  ASSET_TYPE_LABELS,
  ASSET_VERSION_TYPE_LABELS,
  generateAsset,
  listAssets,
  listTtsVoices,
  planAssets,
  previewTtsVoice,
  rejectAsset,
  regenerateAsset,
  lockAsset,
  VOICE_GENDER_LABELS,
  voiceIdTag,
  voiceStyleLabel,
  voiceStyleTagList,
  type AssetItem,
  type AssetType,
  type AssetVersionType,
  type TtsVoice,
} from '@/api/assets'
import AppButton from '@/components/common/AppButton.vue'
import PhotoCharacterPanel from '@/components/asset/PhotoCharacterPanel.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/composables/useToast'
import { formatAudioTime, useVoicePreview } from '@/composables/useVoicePreview'
import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { coverTone } from '@/utils/coverTone'
import { formatRelativeTime } from '@/utils/datetime'

/** 路由实例（卡片点击跳详情 / 项目上下文） */
const route = useRoute()
const router = useRouter()
/** Toast 提示 */
const toast = useToast()
/** 认证 store（SSE 查询参数 token） */
const authStore = useAuthStore()
/** 项目 store（读取当前项目状态，判断是否已有剧本可供规划） */
const projectStore = useProjectStore()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ==================== 类型 Tab 与筛选 ==================== */

/** 资产 Tab 定义（音色为音色库只读；图标用于空态） */
const ASSET_TABS: Array<{ key: AssetType; label: string; icon: string }> = [
  { key: 'character', label: '角色', icon: '🎭' },
  { key: 'scene', label: '场景', icon: '🏛️' },
  { key: 'prop', label: '道具', icon: '📦' },
  { key: 'voice', label: '音色', icon: '🎙️' },
]

/** 资产 Tab 值（空串 = 全部资产，对齐原型 p3「全部资产 (N)」首 tab） */
type AssetTab = '' | AssetType

/** 当前选中 Tab（默认「全部资产」） */
const activeTab = ref<AssetTab>('')

/** 合法 Tab 值集合（用于路由 query 恢复与 URL 同步校验） */
const TAB_VALUES = new Set<AssetTab>(['', ...ASSET_TABS.map((tab) => tab.key)])

// 从 ?tab=voice 等 query 恢复分类：音色详情页返回时可回到「音色」Tab，
// 刷新页面也保持当前分类（信息层级不因返回而丢失）。
const queryTab = route.query.tab
if (typeof queryTab === 'string' && TAB_VALUES.has(queryTab as AssetTab)) {
  activeTab.value = queryTab as AssetTab
}

/**
 * 将当前分类同步到 URL query（只改 tab，保留其它 query；不触发重新加载）。
 * @param value 当前分类
 * @returns 无
 */
function syncTabQuery(value: AssetTab): void {
  if (typeof route.query.tab === 'string' && route.query.tab === value) return
  const nextQuery = { ...route.query }
  if (value) nextQuery.tab = value
  else delete nextQuery.tab
  void router.replace({ query: nextQuery })
}

/** 照片合成角色内嵌面板展开状态（仅角色 Tab 下显示，默认收起） */
const photoPanelOpen = ref(false)
/** 状态筛选（空串=全部） */
const statusFilter = ref('')

/** 状态筛选选项（资产状态机 8 态） */
const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部状态' },
  { value: 'planning', label: '规划中' },
  { value: 'generating', label: '生成中' },
  { value: 'pending_review', label: '待确认' },
  { value: 'approved', label: '已通过' },
  { value: 'locked', label: '已锁定' },
  { value: 'qc_failed', label: '质检失败' },
  { value: 'rejected', label: '已驳回' },
  { value: 'deprecated', label: '已废弃' },
]

/** 快捷状态筛选 chips（与 select 共用同一 statusFilter；qc_failed 带红点对齐原型「踩线待审」） */
const QUICK_STATUS_CHIPS: Array<{ value: string; label: string; danger?: boolean }> = [
  { value: '', label: '全部' },
  { value: 'generating', label: '生成中' },
  { value: 'pending_review', label: '待确认' },
  { value: 'qc_failed', label: '质检未过', danger: true },
  { value: 'approved', label: '已通过' },
  { value: 'locked', label: '已锁定' },
]

/** 封面状态胶囊文案（英文状态机 + 中文，夜窗底部行共用） */
const COVER_STATUS_LABELS: Record<string, string> = {
  planning: 'planning · 规划中',
  generating: 'generating · 生成中',
  pending_review: 'pending_review · 待确认',
  approved: 'approved · 已通过',
  locked: 'approved · 已锁定',
  qc_failed: 'qc_failed · 质检未过',
  rejected: 'rejected · 已驳回',
  deprecated: 'deprecated · 已废弃',
}

/** 封面状态胶囊圆点色（业务五态语义色） */
const COVER_STATUS_DOT: Record<string, string> = {
  planning: 'bg-info',
  generating: 'bg-info',
  pending_review: 'bg-warning',
  approved: 'bg-success',
  locked: 'bg-success',
  qc_failed: 'bg-danger',
  rejected: 'bg-danger',
  deprecated: 'bg-neutral-300',
}

/** 卡头右上质检结论（列表接口无实时相似度分，以状态机结论表达该信息位） */
const QC_CONCLUSION: Record<string, { label: string; tone: string }> = {
  locked: { label: '锚点已锁定', tone: 'text-success' },
  approved: { label: 'QC 通过', tone: 'text-success' },
  pending_review: { label: '待人工确认', tone: 'text-gd-pending' },
  qc_failed: { label: '质检未过', tone: 'text-danger' },
  rejected: { label: '已驳回', tone: 'text-gd-blocked' },
  planning: { label: '规划中', tone: 'text-info' },
  generating: { label: '生成中', tone: 'text-info' },
}

/** 版本基准槽位（角色=正交三视图三槽；场景/道具=参考图单槽；结构对齐原型 3-View 组） */
const REF_SLOTS: Partial<Record<AssetType, Array<{ type: AssetVersionType; label: string }>>> = {
  character: [
    { type: 'turnaround_front', label: '正视 Front' },
    { type: 'turnaround_side', label: '侧面 Profile' },
    { type: 'turnaround_back', label: '背视 Back' },
  ],
  scene: [{ type: 'scene_ref', label: '场景参考图 Ref' }],
  prop: [{ type: 'prop_ref', label: '道具参考图 Ref' }],
}

/** 版本基准组标题（对齐原型「正交三视图基准 (3-View Ground Truth)」标签行） */
const REF_TITLES: Partial<Record<AssetType, string>> = {
  character: '正交三视图基准 · 3-VIEW',
  scene: '场景参考基准 · REFERENCE',
  prop: '特写与贴图基准 · REFERENCE',
}

/**
 * 资产的质检结论（文案 + 语义色 class）。
 * @param asset 资产对象
 * @returns 结论文案与色调
 */
function qcConclusion(asset: AssetItem): { label: string; tone: string } {
  return QC_CONCLUSION[asset.status] ?? { label: '待质检', tone: 'text-neutral-400' }
}

/**
 * ASSET_ID 徽标文案（CH_001 / SC_002 / PR_003；真身 ID 补零展示）。
 * @param asset 资产对象
 * @returns 徽标文案
 */
function assetIdTag(asset: AssetItem): string {
  const prefix =
    asset.asset_type === 'character' ? 'CH' : asset.asset_type === 'scene' ? 'SC' : asset.asset_type === 'prop' ? 'PR' : 'VO'
  return `${prefix}_${String(asset.id).padStart(3, '0')}`
}

/**
 * 来源徽标文案（夜窗右上第二徽标，对齐原型 LoRA / 系统原创位）。
 * @param asset 资产对象
 * @returns 来源文案
 */
function sourceLabel(asset: AssetItem): string {
  if (asset.source === 'ai_generated') return 'AI 生成'
  if (asset.source === 'manual') return '人工建档'
  return '系统资产'
}

/**
 * 卡头一句话描述（description 优先，回退 meta.appearance）。
 * @param asset 资产对象
 * @returns 描述文案（可空串）
 */
function displayDesc(asset: AssetItem): string {
  return asset.description || metaText(asset, 'appearance')
}

/**
 * 当前资产的版本基准槽位（音色无基准槽）。
 * @param asset 资产对象
 * @returns 槽位数组
 */
function refSlots(asset: AssetItem): Array<{ type: AssetVersionType; label: string }> {
  return REF_SLOTS[asset.asset_type] ?? []
}

/**
 * 从资产 meta 宽松读取字符串字段。
 * @param asset 资产对象
 * @param key meta 键名
 * @returns 字符串值（无则空串）
 */
function metaText(asset: AssetItem, key: string): string {
  const value = asset.meta?.[key]
  return typeof value === 'string' ? value : ''
}

/**
 * 资产失败类型文案：区分「模型生成失败」与「画面质检未过」。
 * 生成失败也会复用 qc_failed 状态，直接写“质检未过”会误导用户。
 * @param asset 资产
 * @returns 失败类型文案
 */
function failureLabel(asset: AssetItem): string {
  const error = metaText(asset, 'last_error')
  return /生成失败|鉴权失败|API Key|ConnectError|网络连接失败|HTTP 401|HTTP 403/.test(error)
    ? '模型生成失败'
    : '相似度质检未过'
}

/**
 * 版本号展示文案（列表接口无版本聚合，以当前版本 ID 有无近似表达）。
 * @param asset 资产对象
 * @returns 版本文案
 */
function versionLabel(asset: AssetItem): string {
  return asset.current_version_id ? `当前版本 #${asset.current_version_id}` : '暂无成品版本'
}

/**
 * 封面类名：统一走 utils/coverTone（确定性哈希 + 装饰池不含薄荷，
 * 保证「QC 通过」这一薄荷色结论不会和一整片绿封面混在一起）。
 * @param asset 资产对象
 * @returns 封面类名
 */
function coverClass(asset: AssetItem): string {
  return coverTone(asset.id)
}

/** 当前 Tab 类型图标（空态用；「全部资产」用场记板） */
const activeTabIcon = computed(() => {
  if (activeTab.value === '') return '🎬'
  return ASSET_TABS.find((t) => t.key === activeTab.value)?.icon ?? '🧱'
})

/* ==================== 列表数据 ==================== */

/** 资产列表（当前 Tab） */
const assets = ref<AssetItem[]>([])
/** 列表总条数（当前 Tab / 筛选组合） */
const total = ref(0)
/** 列表加载中 */
const loading = ref(true)
/** 列表加载失败信息 */
const loadError = ref('')
/** 项目资产总数（含全部类型，判断「未规划」空态） */
const projectAssetTotal = ref<number | null>(null)

/** 音色库列表（voice Tab） */
const voices = ref<TtsVoice[]>([])
/** 音色库加载中 */
const voicesLoading = ref(false)

/** 项目是否尚未规划任何资产 */
const isUnplanned = computed(() => projectAssetTotal.value === 0)

/**
 * 「智能规划资产」是否被前置条件阻塞：
 * 项目仍处于 draft（尚未生成 / 导入剧本）且没有任何资产时，规划请求必然失败。
 */
const planBlocked = computed(
  () => projectStore.currentProject?.status === 'draft' && projectAssetTotal.value === 0,
)

/** 「智能规划资产」按钮的 hover 说明（含不可用原因） */
const planHint = computed(() =>
  planBlocked.value
    ? '当前项目还没有剧本：请先在剧本页生成或导入剧本，再回来规划资产'
    : '从当前剧本提取角色 / 场景 / 道具，AI 将逐个生成视觉锚点',
)

/** 关键词搜索（对齐原型刊头搜索框；在当前 Tab 结果内匹配 ID / 名称 / 描述 / 标签） */
const searchQuery = ref('')

/**
 * 当前 Tab + 状态 + 关键词过滤后的资产（卡片网格与导出共用此数据源）。
 */
const filteredAssets = computed<AssetItem[]>(() => {
  const keyword = searchQuery.value.trim().toLowerCase()
  if (!keyword) return assets.value
  return assets.value.filter((asset) => {
    const haystack = [
      String(asset.id),
      asset.name,
      asset.description ?? '',
      metaText(asset, 'appearance'),
      ...(asset.tags ?? []),
    ]
      .join(' ')
      .toLowerCase()
    return haystack.includes(keyword)
  })
})

/* ==================== 音色分类筛选（声线 / 风格） ==================== */

/** 音色性别筛选（空串=全部声线） */
const voiceGenderFilter = ref('')
/** 音色风格筛选（空串=全部风格；选项来自当前音色库真实标签） */
const voiceStyleFilter = ref('')

/** 声线筛选选项（计数在模板中按真实音色库统计） */
const VOICE_GENDER_TABS: Array<{ value: string; label: string }> = [
  { value: '', label: '全部声线' },
  { value: 'female', label: '女声' },
  { value: 'male', label: '男声' },
  { value: 'neutral', label: '中性' },
]

/** 音色性别计数（用于筛选 chips 与统计卡） */
const voiceGenderCounts = computed(() => {
  const counts: Record<string, number> = { female: 0, male: 0, neutral: 0 }
  voices.value.forEach((voice) => {
    counts[voice.gender] = (counts[voice.gender] ?? 0) + 1
  })
  return counts
})

/** 音色风格选项（去重 + 字典序，避免每次渲染重排） */
const voiceStyleOptions = computed<string[]>(() => {
  const tags = new Set<string>()
  voices.value.forEach((voice) => {
    voiceStyleTagList(voice.style_tags).forEach((tag) => tags.add(tag))
  })
  return Array.from(tags).sort((a, b) => a.localeCompare(b))
})

/**
 * 音色过滤结果（关键词匹配 ID / 短编号 / 名称 / 键名 / 风格中英文 + 声线 + 风格）。
 * 与资产网格分离：音色没有资产状态机，筛选维度为声线与风格。
 */
const filteredVoices = computed<TtsVoice[]>(() => {
  const keyword = searchQuery.value.trim().toLowerCase()
  return voices.value.filter((voice) => {
    if (voiceGenderFilter.value && voice.gender !== voiceGenderFilter.value) return false
    const tags = voiceStyleTagList(voice.style_tags)
    if (voiceStyleFilter.value && !tags.includes(voiceStyleFilter.value)) return false
    if (!keyword) return true
    const haystack = [
      String(voice.id),
      voiceIdTag(voice.id),
      voice.name,
      voice.voice_key,
      VOICE_GENDER_LABELS[voice.gender] ?? voice.gender,
      voice.language,
      ...tags,
      ...tags.map(voiceStyleLabel),
    ]
      .join(' ')
      .toLowerCase()
    return haystack.includes(keyword)
  })
})

/* ==================== 音色选中态 + 试听播放 ==================== */

/** 当前选中的音色 ID（点击卡片 / 试听时更新，视觉上给出明确选中反馈） */
const selectedVoiceId = ref<number | null>(null)

/** 选中态持久化键（按项目隔离，返回列表后仍可辨认上次查看的音色） */
const selectedVoiceStorageKey = computed(() => `gd.assets.selectedVoice.${projectId.value}`)

/**
 * 恢复上次选中的音色（sessionStorage 不可用时静默降级）。
 * @returns 无
 */
function restoreSelectedVoice(): void {
  try {
    const raw = sessionStorage.getItem(selectedVoiceStorageKey.value)
    const parsed = Number(raw)
    selectedVoiceId.value = Number.isFinite(parsed) && parsed > 0 ? parsed : null
  } catch {
    selectedVoiceId.value = null
  }
}

/**
 * 记忆音色选中态（点击卡片 / 试听时调用）。
 * @param voiceId 音色 ID
 * @returns 无
 */
function rememberSelectedVoice(voiceId: number): void {
  selectedVoiceId.value = voiceId
  try {
    sessionStorage.setItem(selectedVoiceStorageKey.value, String(voiceId))
  } catch {
    // 隐私模式 / 存储禁用时忽略
  }
}

/** 音色试听播放器（一次仅播放一条，切换自动停上一条） */
const { playingId, audioOwnerId, loadingId, positionMs, durationMs, progress, error: voicePlaybackError, toggle } = useVoicePreview()

/** 列表内实时合成结果缓存（避免暂停后再次点击重复请求 TTS） */
const voicePreviewUrls = ref<Record<number, string>>({})
/** 列表内试听音频时长缓存（毫秒） */
const voicePreviewDurations = ref<Record<number, number>>({})

/**
 * 音色卡试听 / 暂停（懒合成：首次点击调用 preview 接口，之后复用缓存）。
 * @param voice 音色对象
 * @returns 无
 */
async function toggleVoicePreview(voice: TtsVoice): Promise<void> {
  if (voice.status !== 'available') {
    toast.warning('该音色不可用，无法试听')
    return
  }
  if (loadingId.value === voice.id) return
  rememberSelectedVoice(voice.id)

  const cachedUrl = voicePreviewUrls.value[voice.id]
  if (cachedUrl) {
    await toggle(voice.id, cachedUrl, voicePreviewDurations.value[voice.id] ?? 0)
    return
  }

  await toggle(voice.id, async () => {
    const result = await previewTtsVoice(voice.id)
    voicePreviewUrls.value = { ...voicePreviewUrls.value, [voice.id]: result.audio_url }
    voicePreviewDurations.value = { ...voicePreviewDurations.value, [voice.id]: result.duration_ms }
    return result.audio_url
  })
}

/**
 * 当前音频是否属于指定音色（暂停后进度仍归属该卡）。
 * @param voiceId 音色 ID
 * @returns 是否归属
 */
function ownsAudio(voiceId: number): boolean {
  return audioOwnerId.value === voiceId
}

/**
 * 指定音色卡的播放进度（非当前音频返回 0）。
 * @param voiceId 音色 ID
 * @returns 进度 0–1
 */
function voiceProgress(voiceId: number): number {
  return ownsAudio(voiceId) ? progress.value : 0
}

/**
 * 指定音色卡的播放位置（毫秒）。
 * @param voiceId 音色 ID
 * @returns 毫秒数
 */
function voicePosition(voiceId: number): number {
  return ownsAudio(voiceId) ? positionMs.value : 0
}

/**
 * 指定音色卡的音频总时长（毫秒）。
 * @param voiceId 音色 ID
 * @returns 毫秒数
 */
function voiceDuration(voiceId: number): number {
  return ownsAudio(voiceId) ? durationMs.value : 0
}

/**
 * 音色卡波形柱是否被进度点亮。
 * @param voiceId 音色 ID
 * @param index 柱索引
 * @param count 柱总数
 * @returns 是否点亮
 */
function isVoiceBarActive(voiceId: number, index: number, count: number): boolean {
  const value = voiceProgress(voiceId)
  if (value <= 0) return false
  return (index + 1) / count <= value
}

/* ==================== 数据加载 ==================== */

/**
 * 拉取当前 Tab 资产列表（同时探测项目资产总数以渲染规划空态）。
 * Tab 为空串时按「全部资产」查询（不传 asset_type）。
 * @returns 无
 */
async function fetchAssets(): Promise<void> {
  if (activeTab.value === 'voice') {
    await fetchVoices()
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    // 并行：全量 total（判断空态）+ 当前 Tab 分页列表
    const [allPage, page] = await Promise.all([
      listAssets(projectId.value, { page: 1, page_size: 1 }),
      listAssets(projectId.value, {
        asset_type: activeTab.value === '' ? undefined : (activeTab.value as AssetType),
        status: statusFilter.value || undefined,
        page: 1,
        page_size: 60,
      }),
    ])
    projectAssetTotal.value = allPage.total
    assets.value = page.items
    total.value = page.total
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '资产列表加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 拉取音色库（音色 Tab：可点击进详情 + 卡片内试听）。
 * @returns 无
 */
async function fetchVoices(): Promise<void> {
  voicesLoading.value = true
  loadError.value = ''
  // 音色 Tab 与资产 Tab 共用同一个内容区，切过来时清掉上一 Tab 的资产，
  // 否则 assets 这个「非当前 Tab 的数据」会继续参与空态 / 统计判断
  assets.value = []
  projectAssetTotal.value = projectAssetTotal.value ?? 0
  try {
    const page = await listTtsVoices({ status: 'available', page: 1, page_size: 60 })
    voices.value = page.items
    total.value = page.total
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '音色库加载失败'
  } finally {
    voicesLoading.value = false
  }
}

// Tab / 筛选变化重新加载（分类同时同步到 URL query，便于详情页返回时还原）
watch([activeTab, statusFilter], () => {
  selectedIds.value = new Set()
  batchMode.value = false
  syncTabQuery(activeTab.value)
  void fetchAssets()
})

/* ==================== 智能规划 + 轮询兜底 ==================== */

/** 规划请求进行中 */
const planning = ref(false)
/** 规划后兜底轮询定时器 */
let planPollTimer: ReturnType<typeof setInterval> | null = null
/** 轮询剩余次数 */
let planPollLeft = 0

/**
 * 发起智能规划资产（202 后台 LLM 提取；SSE assets.planned / 轮询双通道刷新）。
 * @returns 无
 */
async function startPlan(): Promise<void> {
  if (planBlocked.value) {
    toast.warning('当前项目还没有剧本：请先在剧本页生成或导入剧本，再回来规划资产')
    return
  }
  planning.value = true
  try {
    const receipt = await planAssets(projectId.value)
    // P1-4：并发/重复点击时后端返回 in_progress，提示等待而非再次受理
    if (receipt.status === 'in_progress') {
      toast.info(receipt.message || '资产规划任务正在执行，请稍候')
      return
    }
    toast.success('资产规划任务已受理，AI 正在从剧本提取角色 / 场景 / 道具…')
    // 启动兜底轮询：每 4s 刷新一次，最多 20 次（80s），规划完成即停
    planPollLeft = 20
    if (!planPollTimer) {
      planPollTimer = setInterval(() => {
        planPollLeft -= 1
        void fetchAssets().then(() => {
          const stillPlanning = assets.value.some((a) => a.status === 'planning')
          if ((planPollLeft <= 0 || (!stillPlanning && assets.value.length > 0)) && planPollTimer) {
            clearInterval(planPollTimer)
            planPollTimer = null
          }
        })
      }, 4_000)
    }
  } catch {
    // 拦截器已 toast（10002 项目尚无剧本等）
  } finally {
    planning.value = false
  }
}

/* ==================== 关口二统计（对齐原型四张质检统计卡） ==================== */

/** 三实体类型（统计口径不含音色库） */
type GateEntityType = Exclude<AssetType, 'voice'>

/** 关口二全量快照：由三类型轻请求（page_size=100）聚合，仅状态与 voice_id 计数，不引入新接口 */
interface GateSnapshot {
  /** 各类型资产总数 */
  typeTotals: Record<GateEntityType, number>
  /** 各类型已通过（未锁定）数——与原 approvedCount 同口径，控制「全部锁定」可用态 */
  approvedTotals: Record<GateEntityType, number>
  /** 已锁定总数 */
  lockedTotal: number
  /** 质检未过总数（红警示卡） */
  qcFailedTotal: number
  /** 待人工确认总数（跨三类型） */
  pendingReviewTotal: number
  /** 已绑定音色的角色数（统计卡四） */
  voiceBoundTotal: number
  /** 首个质检未过资产名（警示卡副文案） */
  firstQcFailedName: string
}

/** 关口二统计数据（初始为零值，refreshGateStats 填充） */
const gate = ref<GateSnapshot>({
  typeTotals: { character: 0, scene: 0, prop: 0 },
  approvedTotals: { character: 0, scene: 0, prop: 0 },
  lockedTotal: 0,
  qcFailedTotal: 0,
  pendingReviewTotal: 0,
  voiceBoundTotal: 0,
  firstQcFailedName: '',
})

/** 已通过（未锁定）资产数——保持与旧 approvedCount 完全同口径（仅 status=approved） */
const approvedCount = computed(
  () => gate.value.approvedTotals.character + gate.value.approvedTotals.scene + gate.value.approvedTotals.prop,
)

/** 三实体资产总数（统计卡分母 / tab 计数） */
const entityTotal = computed(
  () => gate.value.typeTotals.character + gate.value.typeTotals.scene + gate.value.typeTotals.prop,
)

/** 已达标资产数（已通过 + 已锁定） */
const passedTotal = computed(() => approvedCount.value + gate.value.lockedTotal)

/** 达标率文案（统计卡一大数字；总数为 0 时显示占位符） */
const passRateLabel = computed(() => {
  if (entityTotal.value === 0) return '—'
  const percent = (passedTotal.value / entityTotal.value) * 100
  return Number.isInteger(percent) ? `${percent}%` : `${percent.toFixed(1)}%`
})

/** 刊头审批状态（脉冲点 + 文案，随真实进度切换） */
const gateStatus = computed<{ label: string; live: boolean }>(() => {
  if (gate.value.qcFailedTotal > 0) return { label: '质检异常待处理', live: true }
  if (gate.value.pendingReviewTotal > 0) return { label: '质检审批进行中', live: true }
  if (entityTotal.value > 0 && passedTotal.value >= entityTotal.value) return { label: '全剧资产已达标', live: false }
  return { label: '等待资产生成', live: true }
})

/** 是否处于音色分类（音色库与视觉资产共用路由，但信息层级与工具区不同） */
const isVoiceTab = computed(() => activeTab.value === 'voice')

/** 刊头标题（音色分类切换到「音色库与试听台」，突出唯一 ID 与试听） */
const headerTitle = computed(() => (isVoiceTab.value ? '音色库与试听台' : '资产设计与一致性质检墙'))

/** 刊头状态：音色分类展示可用音色数与 ID 引用提示，其余分类沿用关口二状态 */
const headerStatus = computed<{ label: string; live: boolean }>(() =>
  isVoiceTab.value
    ? { label: `音色库 ${voices.value.length} 条可用 · 按唯一 ID 引用`, live: false }
    : gateStatus.value,
)

/** 类型 Tab 药丸组（含真实计数；音色计数仅在进入音色 Tab 后可知） */
const TYPE_TABS = computed<Array<{ value: AssetTab; label: string; count: number | null }>>(() => [
  { value: '', label: '全部资产', count: entityTotal.value },
  { value: 'character', label: '角色', count: gate.value.typeTotals.character },
  { value: 'scene', label: '场景', count: gate.value.typeTotals.scene },
  { value: 'prop', label: '道具', count: gate.value.typeTotals.prop },
  { value: 'voice', label: '音色', count: activeTab.value === 'voice' ? total.value : null },
])

/**
 * 刷新关口二统计（三类型轻请求聚合状态计数；超过 100 条/类型时锁定/质检计数按前 100 条近似）。
 * 替代旧 refreshApprovedCount 的 3 次 total 探测，请求数不变、信息量更足。
 * @returns 无
 */
async function refreshGateStats(): Promise<void> {
  try {
    const [characters, scenes, props] = await Promise.all([
      listAssets(projectId.value, { asset_type: 'character', page: 1, page_size: 100 }),
      listAssets(projectId.value, { asset_type: 'scene', page: 1, page_size: 100 }),
      listAssets(projectId.value, { asset_type: 'prop', page: 1, page_size: 100 }),
    ])
    const all = [...characters.items, ...scenes.items, ...props.items]
    gate.value = {
      typeTotals: { character: characters.total, scene: scenes.total, prop: props.total },
      approvedTotals: {
        character: characters.items.filter((a) => a.status === 'approved').length,
        scene: scenes.items.filter((a) => a.status === 'approved').length,
        prop: props.items.filter((a) => a.status === 'approved').length,
      },
      lockedTotal: all.filter((a) => a.status === 'locked').length,
      qcFailedTotal: all.filter((a) => a.status === 'qc_failed').length,
      pendingReviewTotal: all.filter((a) => a.status === 'pending_review').length,
      voiceBoundTotal: characters.items.filter((a) => a.voice_id !== null).length,
      firstQcFailedName: all.find((a) => a.status === 'qc_failed')?.name ?? '',
    }
  } catch {
    // P3-a 修复：统计请求失败时保留上一次快照，不再归零——
    // 归零会让类型筛选 tab 显示「(0)」而主列表实际有资产（计数与所见矛盾）。
    // 首次加载即失败时保持零值，主列表仍可正常浏览。
  }
}

/* ==================== 批量模式 ==================== */

/** 是否批量模式（展示选中框） */
const batchMode = ref(false)
/** 已选资产 ID 集合（仅 pending_review 可选） */
const selectedIds = ref<Set<number>>(new Set())
/** 批量通过进行中 */
const batchApproving = ref(false)
/** 批量通过确认弹窗 */
const batchApproveDialogOpen = ref(false)

/** 可勾选资产（当前筛选结果中待审核项；与网格所见一致，批量全选不越界） */
const reviewableAssets = computed(() => filteredAssets.value.filter((a) => a.status === 'pending_review'))

/** 已选数量 */
const selectedCount = computed(() => selectedIds.value.size)

/**
 * 切换资产选中态。
 * @param asset 资产对象
 * @returns 无
 */
function toggleSelected(asset: AssetItem): void {
  const next = new Set(selectedIds.value)
  if (next.has(asset.id)) {
    next.delete(asset.id)
  } else {
    next.add(asset.id)
  }
  selectedIds.value = next
}

/**
 * 全选 / 清空当前 Tab 可审核资产。
 * @returns 无
 */
function toggleSelectAll(): void {
  if (selectedIds.value.size >= reviewableAssets.value.length) {
    selectedIds.value = new Set()
  } else {
    selectedIds.value = new Set(reviewableAssets.value.map((a) => a.id))
  }
}

/**
 * 退出批量模式并清空选择。
 * @returns 无
 */
function exitBatchMode(): void {
  batchMode.value = false
  selectedIds.value = new Set()
}

/**
 * 提交批量通过：逐个 approve，失败项汇总提示。
 * @returns 无
 */
async function confirmBatchApprove(): Promise<void> {
  batchApproving.value = true
  let ok = 0
  const failed: string[] = []
  for (const id of selectedIds.value) {
    try {
      await approveAsset(id)
      ok += 1
    } catch (error) {
      // P2-8：批量通过失败不再静默——记录具体原因，随汇总 toast 一并暴露
      const reason = error instanceof Error ? error.message : '未知错误'
      failed.push(`#${id}（${reason}）`)
    }
  }
  batchApproving.value = false
  batchApproveDialogOpen.value = false
  selectedIds.value = new Set()
  if (failed.length === 0) {
    toast.success(`已批量通过 ${ok} 个资产`)
  } else {
    toast.warning(`通过 ${ok} 个，失败 ${failed.length} 个（${failed.join('、')}）`)
  }
  await fetchAssets()
  await refreshGateStats()
}

/* ==================== 导出资产清单（对齐原型刊头「导出资产清单」） ==================== */

/**
 * 将当前筛选结果导出为 CSV（纯前端 Blob 下载；含 BOM 便于 Excel 识别 UTF-8）。
 * @returns 无
 */
function exportAssetsCsv(): void {
  if (filteredAssets.value.length === 0) {
    toast.warning('当前列表为空，无可导出的资产')
    return
  }
  const header = ['资产ID', '类型', '名称', '状态', '当前版本', '音色绑定', '更新时间', '描述']
  const rows = filteredAssets.value.map((asset) => [
    asset.id,
    ASSET_TYPE_LABELS[asset.asset_type],
    asset.name,
    COVER_STATUS_LABELS[asset.status] ?? asset.status,
    asset.current_version_id ?? '',
    asset.voice_id ? `#${asset.voice_id}` : '',
    asset.updated_at ?? '',
    (asset.description ?? '').replace(/\s+/g, ' '),
  ])
  const csv =
    '\ufeff' +
    [header, ...rows].map((row) => row.map((cell) => `"${String(cell).replace(/"/g, '""')}"`).join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `asset-list-p${projectId.value || 'all'}-${Date.now()}.csv`
  anchor.click()
  URL.revokeObjectURL(url)
  toast.success(`已导出 ${rows.length} 条资产清单`)
}

/* ==================== 全部锁定（关口二） ==================== */

/** 全部锁定进行中 */
const locking = ref(false)
/** 全部锁定确认弹窗 */
const lockDialogOpen = ref(false)
/** 锁定结果面板（错误汇总） */
const lockResult = ref<{ ok: number; failures: Array<{ name: string; reason: string; missingView: boolean }> } | null>(null)

/**
 * 执行全部锁定：对三类已通过资产逐个 lock，错误汇总展示（60003 标注缺视图）。
 * @returns 无
 */
async function confirmLockAll(): Promise<void> {
  locking.value = true
  lockResult.value = null
  let ok = 0
  const failures: Array<{ name: string; reason: string; missingView: boolean }> = []
  try {
    const pages = await Promise.all([
      listAssets(projectId.value, { asset_type: 'character', status: 'approved', page: 1, page_size: 100 }),
      listAssets(projectId.value, { asset_type: 'scene', status: 'approved', page: 1, page_size: 100 }),
      listAssets(projectId.value, { asset_type: 'prop', status: 'approved', page: 1, page_size: 100 }),
    ])
    const approved = pages.flatMap((p) => p.items)
    for (const asset of approved) {
      try {
        await lockAsset(asset.id)
        ok += 1
      } catch (error) {
        const code = (error as { code?: number }).code ?? 0
        failures.push({
          name: asset.name,
          reason: error instanceof Error ? error.message : '锁定失败',
          missingView: code === 60003,
        })
      }
    }
    lockResult.value = { ok, failures }
    if (failures.length === 0) {
      toast.success(`已锁定 ${ok} 个资产，可进入分镜生成`)
    } else {
      toast.warning(`锁定 ${ok} 个成功，${failures.length} 个失败，详见结果面板`)
    }
    lockDialogOpen.value = false
    await fetchAssets()
    await refreshGateStats()
  } finally {
    locking.value = false
  }
}

/* ==================== 批量生成（关口二，修复 E2E 报告 P-01） ==================== */

/** 待生成资产（planning：已有规划清单但尚未产出首版图像） */
const generatableAssets = computed(() => filteredAssets.value.filter((a) => a.status === 'planning'))
/** 批量生成进行中 */
const batchGenerating = ref(false)
/** 批量生成确认弹窗 */
const batchGenerateDialogOpen = ref(false)

/**
 * 资产首版默认版本类型（角色主形象 / 场景参考图 / 道具参考图）。
 * @param asset 资产对象
 * @returns 版本类型
 */
function firstVersionTypeOf(asset: AssetItem): AssetVersionType {
  if (asset.asset_type === 'scene') return 'scene_ref'
  if (asset.asset_type === 'prop') return 'prop_ref'
  return 'portrait'
}

/**
 * 提交批量生成：对全部 planning 资产逐个受理首版生成（202 后台任务），
 * 失败项汇总提示；成功后启动生成轮询直至全部进入待审核。
 * @returns 无
 */
async function confirmBatchGenerate(): Promise<void> {
  batchGenerating.value = true
  let ok = 0
  const failed: string[] = []
  for (const asset of generatableAssets.value) {
    try {
      await generateAsset(asset.id, { version_type: firstVersionTypeOf(asset) })
      ok += 1
    } catch (error) {
      // 单个受理失败不中断批次（40001 生成中 / 已锁定等），汇总展示
      failed.push(`「${asset.name}」（${error instanceof Error ? error.message : '未知错误'}）`)
    }
  }
  batchGenerating.value = false
  batchGenerateDialogOpen.value = false
  if (failed.length === 0) {
    toast.success(`已受理 ${ok} 个资产生成任务，完成后将进入「待确认」`)
  } else {
    toast.warning(`受理 ${ok} 个，失败 ${failed.length} 个：${failed.join('、')}`)
  }
  if (ok > 0) startPlanPolling()
  await fetchAssets()
  await refreshGateStats()
}

/* ==================== 单卡操作（通过 / 驳回 / 重抽） ==================== */

/** 单卡操作进行中的资产 ID（按钮 loading） */
const cardBusyId = ref<number | null>(null)

/** 驳回弹窗目标资产 */
const rejectTarget = ref<AssetItem | null>(null)
/** 驳回弹窗显示状态 */
const rejectDialogOpen = ref(false)
/** 驳回原因输入 */
const rejectFeedback = ref('')
/** 驳回提交中 */
const rejecting = ref(false)

/** 重抽弹窗目标资产 */
const regenerateTarget = ref<AssetItem | null>(null)
/** 重抽弹窗显示状态 */
const regenerateDialogOpen = ref(false)
/** 重抽反馈输入（可选） */
const regenerateFeedback = ref('')
/** 重抽提交中 */
const regenerating = ref(false)

/**
 * 当前重抽目标的默认版本类型（角色重抽主形象，场景/道具重抽参考图）。
 * @returns 版本类型
 */
const regenerateVersionType = computed<AssetVersionType>(() => {
  const type = regenerateTarget.value?.asset_type
  if (type === 'scene') return 'scene_ref'
  if (type === 'prop') return 'prop_ref'
  return 'portrait'
})

/**
 * 通过单卡审核。
 * @param asset 资产对象
 * @returns 无
 */
async function approveCard(asset: AssetItem): Promise<void> {
  cardBusyId.value = asset.id
  try {
    await approveAsset(asset.id)
    toast.success(`「${asset.name}」已通过审核`)
    await fetchAssets()
    await refreshGateStats()
  } catch {
    // 拦截器已 toast
  } finally {
    cardBusyId.value = null
  }
}

/**
 * 打开驳回弹窗（ConfirmDialog 内强校验 ≥10 字）。
 * @param asset 资产对象
 * @returns 无
 */
function openRejectDialog(asset: AssetItem): void {
  rejectTarget.value = asset
  rejectFeedback.value = ''
  rejectDialogOpen.value = true
}

/**
 * 提交驳回（反馈不足 10 字时阻断并提示，弹窗保持打开）。
 * @returns 无
 */
async function confirmReject(): Promise<void> {
  const target = rejectTarget.value
  if (!target) return
  if (rejectFeedback.value.trim().length < 10) {
    toast.warning('驳回原因需至少 10 个字，便于 AI 按反馈重新生成')
    return
  }
  rejecting.value = true
  try {
    await rejectAsset(target.id, rejectFeedback.value.trim())
    toast.success(`「${target.name}」已驳回`)
    rejectDialogOpen.value = false
    rejectTarget.value = null
    await fetchAssets()
  } catch {
    // 拦截器已 toast（10001 / 40001）
  } finally {
    rejecting.value = false
  }
}

/**
 * 打开重抽弹窗（反馈可选）。
 * @param asset 资产对象
 * @returns 无
 */
function openRegenerateDialog(asset: AssetItem): void {
  regenerateTarget.value = asset
  regenerateFeedback.value = ''
  regenerateDialogOpen.value = true
}

/**
 * 提交重抽（regenerate 202 受理 → 轮询刷新卡片状态）。
 * @returns 无
 */
async function confirmRegenerate(): Promise<void> {
  const target = regenerateTarget.value
  if (!target) return
  regenerating.value = true
  try {
    await regenerateAsset(target.id, {
      version_type: regenerateVersionType.value,
      feedback: regenerateFeedback.value.trim() || undefined,
    })
    toast.success(`「${target.name}」重新生成任务已受理`)
    regenerateDialogOpen.value = false
    regenerateTarget.value = null
    startPlanPolling()
  } catch {
    // 拦截器已 toast（40001 生成中 / 已锁定等）
  } finally {
    regenerating.value = false
  }
}

/**
 * qc_failed / rejected 卡片快捷重新生成（无反馈，直接受理）。
 * @param asset 资产对象
 * @returns 无
 */
async function quickRegenerate(asset: AssetItem): Promise<void> {
  cardBusyId.value = asset.id
  try {
    const versionType: AssetVersionType =
      asset.asset_type === 'scene' ? 'scene_ref' : asset.asset_type === 'prop' ? 'prop_ref' : 'portrait'
    await regenerateAsset(asset.id, { version_type: versionType })
    toast.success(`「${asset.name}」重新生成任务已受理`)
    startPlanPolling()
  } catch {
    // 拦截器已 toast
  } finally {
    cardBusyId.value = null
  }
}

/* ==================== 生成中轮询兜底 ==================== */

/** 生成中状态兜底轮询定时器 */
let generatePollTimer: ReturnType<typeof setInterval> | null = null
/** 轮询剩余次数 */
let generatePollLeft = 0

/**
 * 启动生成中轮询：每 3s 刷新列表，列表无 generating/planning 资产或超时后停止。
 * @returns 无
 */
function startPlanPolling(): void {
  generatePollLeft = 30
  // 修复（E2E 报告建议项）：规划轮询与生成轮询条件重叠（planning 同时满足
  // 两者），并行会产生双倍请求；启动生成轮询时先停掉规划轮询。
  if (planPollTimer) {
    clearInterval(planPollTimer)
    planPollTimer = null
  }
  if (generatePollTimer) return
  // 修复（E2E 报告 P-04）：记录本轮是否有生成中资产，轮询结束时给出
  // 显著的「新版本已打回待审核」汇总提示（此前状态静默变化，toast 短暂易漏）。
  const hadGenerating = assets.value.some((a) => a.status === 'generating')
  generatePollTimer = setInterval(() => {
    generatePollLeft -= 1
    void fetchAssets().then(() => {
      const busy = assets.value.some((a) => a.status === 'generating' || a.status === 'planning')
      if ((generatePollLeft <= 0 || !busy) && generatePollTimer) {
        clearInterval(generatePollTimer)
        generatePollTimer = null
        if (hadGenerating) {
          const pending = assets.value.filter((a) => a.status === 'pending_review').length
          if (pending > 0) {
            toast.info(`资产生成完毕：${pending} 个资产的新版本已进入「待确认」，请重新审核通过后再锁定`)
          }
        }
      }
    })
  }, 3_000)
}

/* ==================== SSE 实时刷新 ==================== */

/** 项目事件流地址（携带 access_token 查询参数，EventSource 不支持请求头鉴权） */
const sseUrl = computed<string>(() =>
  authStore.token ? `/api/v1/projects/${projectId.value}/events?access_token=${authStore.token}` : '',
)

/**
 * SSE 事件分派：资产相关事件触发列表刷新。
 * @param event 消息事件（命名事件 / 默认事件）
 * @returns 无
 */
function onSseMessage(event: MessageEvent<string>): void {
  const type = event.type
  if (
    type === 'asset.generated' ||
    type === 'assets.planned' ||
    type === 'asset.generation_failed' ||
    type === 'asset_plan.started' ||
    type === 'asset_plan.failed' ||
    type === 'approval.created'
  ) {
    void fetchAssets()
    if (type === 'assets.planned') {
      toast.success('资产规划完成，清单已生成')
    }
  }
}

// 订阅项目事件流（SSE 失败自动降级 30s 轮询兜底）
useSSE(sseUrl, { onMessage: onSseMessage, eventTypes: ['asset.generated', 'assets.planned', 'asset_plan.started', 'asset_plan.failed', 'asset.generation_failed', 'approval.created'] })

/* ==================== 导航 ==================== */

/**
 * 点击卡片跳转资产详情（P10；角色/场景/道具共用同一路由，类型徽章区分）。
 * @param asset 资产对象
 * @returns 无
 */
function goDetail(asset: AssetItem): void {
  void router.push(`/projects/${projectId.value}/assets/characters/${asset.id}`)
}

/**
 * 点击音色卡进入音色详情与试听台（先记录选中态，返回列表时仍高亮）。
 * @param voice 音色对象
 * @returns 无
 */
function goVoiceDetail(voice: TtsVoice): void {
  rememberSelectedVoice(voice.id)
  void router.push(`/projects/${projectId.value}/assets/voices/${voice.id}`)
}

/**
 * 音色卡键盘可达：Enter / Space 等价于点击进入详情。
 * @param event 键盘事件
 * @param voice 音色对象
 * @returns 无
 */
function handleVoiceCardKeydown(event: KeyboardEvent, voice: TtsVoice): void {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    goVoiceDetail(voice)
  }
}

/**
 * 跳转真人授权向导（P11；关口审批条第二按钮位，对齐原型「人脸向量比对」入口位）。
 * @returns 无
 */
function goAuthorization(): void {
  void router.push(`/projects/${projectId.value}/assets/authorizations/new`)
}

/**
 * 全部锁定成功后跳转分镜画布（HITL-2 完成 → 分镜拆解）。
 * @returns 无
 */
function goStoryboard(): void {
  void router.push(`/projects/${projectId.value}/storyboard`)
}

/**
 * 跳转剧本编辑页（资产规划前置条件未满足时的引导）。
 * @returns 无
 */
function goScript(): void {
  void router.push(`/projects/${projectId.value}/script`)
}

// 挂载：恢复音色选中态 + 加载列表 + 关口二统计
onMounted(() => {
  restoreSelectedVoice()
  void fetchAssets()
  void refreshGateStats()
})

// 卸载：清理轮询定时器
onUnmounted(() => {
  if (planPollTimer) {
    clearInterval(planPollTimer)
    planPollTimer = null
  }
  if (generatePollTimer) {
    clearInterval(generatePollTimer)
    generatePollTimer = null
  }
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col overflow-y-auto">
    <!-- ============ 刊头（统一刊头：左缘光条 + 28px 衬线标题 + 关口状态 | 右侧工具区） ============ -->
    <header class="shrink-0 px-6 pt-6 lg:px-8">
      <div class="relative pl-4">
        <!-- 左缘品牌光条（与全站非工作台页面同规格：4px × 28px） -->
        <span class="absolute left-0 top-0 h-7 w-1 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />

        <div class="flex flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
          <!-- 左：统一标题 + 分类状态（音色分类强调唯一 ID 与试听） -->
          <div class="flex min-w-0 flex-wrap items-center gap-3">
            <h1 class="gd-masthead truncate text-display-lg">{{ headerTitle }}</h1>
            <span class="inline-flex shrink-0 items-center gap-1.5 text-caption text-neutral-500">
              <span
                class="h-2 w-2 rounded-full"
                :class="headerStatus.live ? 'animate-pulse bg-info' : isVoiceTab ? 'bg-primary-400' : 'bg-success'"
                aria-hidden="true"
              />
              {{ headerStatus.label }}
            </span>
          </div>

          <!-- 右：工具区（音色分类：搜索 + 刷新；资产分类：搜索 / 批量审核 / 导出 / 主按钮） -->
          <div class="flex shrink-0 flex-wrap items-center gap-3">
            <div class="gd-input-group w-64 max-w-full">
              <span class="gd-input-group__icon" aria-hidden="true">
                <svg class="gd-icon gd-icon--sm" viewBox="0 0 24 24">
                  <circle cx="11" cy="11" r="7" />
                  <path d="m20 20-3.5-3.5" />
                </svg>
              </span>
              <input
                v-model="searchQuery"
                type="search"
                class="gd-input"
                :placeholder="isVoiceTab ? '搜索音色 ID、名称、风格…' : '搜索资产ID、名称、标签…'"
                :aria-label="isVoiceTab ? '搜索音色' : '搜索资产'"
              />
            </div>
            <AppButton
              v-if="!isVoiceTab"
              variant="secondary"
              size="md"
              :disabled="generatableAssets.length === 0 || batchGenerating"
              :loading="batchGenerating"
              :title="generatableAssets.length === 0
                ? '没有待生成（规划中）的资产'
                : `为 ${generatableAssets.length} 个规划中资产生成首版图像（每张约 10 积分）`"
              @click="batchGenerateDialogOpen = true"
            >
              批量生成 ({{ generatableAssets.length }})
            </AppButton>
            <AppButton
              v-if="!isVoiceTab"
              variant="secondary"
              size="md"
              :disabled="reviewableAssets.length === 0 && !batchMode"
              @click="batchMode ? exitBatchMode() : (batchMode = true)"
            >
              批量审核 (多选 {{ selectedCount }})
            </AppButton>
            <AppButton v-if="!isVoiceTab" variant="secondary" size="md" @click="exportAssetsCsv">导出资产清单</AppButton>
            <AppButton
              v-if="!isVoiceTab"
              variant="primary"
              size="md"
              :loading="planning"
              :disabled="planBlocked"
              :title="planHint"
              @click="startPlan"
            >
              智能规划资产
            </AppButton>
            <AppButton v-else variant="secondary" size="md" :loading="voicesLoading" @click="fetchVoices">
              刷新音色库
            </AppButton>
          </div>
        </div>
      </div>
    </header>

    <!-- ============ 四张质检统计卡（资产分类；数据来自关口二快照，警示卡红 ring） ============ -->
    <section v-if="!isVoiceTab" class="shrink-0 px-6 pt-5 lg:px-8" aria-label="质检统计">
      <div class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <!-- 统计 1：全剧达标率 -->
        <div class="gd-card flex items-center justify-between gap-3 p-4">
          <div class="flex min-w-0 flex-col gap-1">
            <span class="gd-section-label">全剧资产达标率</span>
            <div class="flex items-baseline gap-2">
              <span class="gd-num text-2xl">{{ passRateLabel }}</span>
              <span class="rounded-full bg-success-bg px-2 py-0.5 text-caption font-medium text-success-bright">
                已通过 + 已锁定
              </span>
            </div>
            <span class="truncate text-caption text-neutral-500">角色 / 场景 / 道具一致性锚点占比</span>
          </div>
          <span class="gd-icon-chip gd-icon-chip--violet" aria-hidden="true">
            <svg class="gd-icon" viewBox="0 0 24 24">
              <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z" />
              <path d="M4 20c1.8-3.2 4.6-5 8-5s6.2 1.8 8 5" />
            </svg>
          </span>
        </div>

        <!-- 统计 2：三实体锚点锁定 -->
        <div class="gd-card flex items-center justify-between gap-3 p-4">
          <div class="flex min-w-0 flex-col gap-1">
            <span class="gd-section-label">三实体锚点锁定</span>
            <div class="flex items-baseline gap-2">
              <span class="gd-num text-2xl">{{ gate.lockedTotal }} / {{ entityTotal }}</span>
              <span class="rounded-full bg-info-bg px-2 py-0.5 text-caption font-medium text-info-bright">分镜锚点</span>
            </div>
            <span class="truncate text-caption text-neutral-500">锁定后作为分镜生成形象基准</span>
          </div>
          <span class="gd-icon-chip gd-icon-chip--cyan" aria-hidden="true">
            <svg class="gd-icon" viewBox="0 0 24 24">
              <path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z" />
              <path d="m12 12 8-4.5M12 12v9M12 12 4 7.5" />
            </svg>
          </span>
        </div>

        <!-- 统计 3：质检未过（红警示卡） -->
        <div class="gd-card flex items-center justify-between gap-3 p-4 ring-1 ring-danger">
          <div class="flex min-w-0 flex-col gap-1">
            <span class="gd-section-label">质检未过资产</span>
            <div class="flex items-baseline gap-2">
              <span class="gd-num text-2xl text-danger">{{ gate.qcFailedTotal }} 项</span>
              <span class="rounded-full bg-danger-bg px-2 py-0.5 text-caption font-medium text-danger-bright">
                需重抽复核
              </span>
            </div>
            <span class="truncate text-caption text-neutral-500">
              {{ gate.firstQcFailedName ? `「${gate.firstQcFailedName}」需人工核验` : '暂无质检异常资产' }}
            </span>
          </div>
          <span class="gd-icon-chip gd-icon-chip--rose" aria-hidden="true">
            <svg class="gd-icon" viewBox="0 0 24 24">
              <path d="M12 4 22 20H2L12 4Z" />
              <path d="M12 10v4M12 17h.01" />
            </svg>
          </span>
        </div>

        <!-- 统计 4：专属 TTS 音色绑定 -->
        <div class="gd-card flex items-center justify-between gap-3 p-4">
          <div class="flex min-w-0 flex-col gap-1">
            <span class="gd-section-label">专属 TTS 音色绑定</span>
            <div class="flex items-baseline gap-2">
              <span class="gd-num text-2xl">{{ gate.voiceBoundTotal }} / {{ gate.typeTotals.character }}</span>
              <span class="rounded-full bg-primary-50 px-2 py-0.5 text-caption font-medium text-primary-300">
                角色音色
              </span>
            </div>
            <span class="truncate text-caption text-neutral-500">在角色详情页绑定与试听音色</span>
          </div>
          <span class="gd-icon-chip gd-icon-chip--magenta" aria-hidden="true">
            <svg class="gd-icon" viewBox="0 0 24 24">
              <path d="M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Z" />
              <path d="M19 11a7 7 0 0 1-14 0M12 18v3" />
            </svg>
          </span>
        </div>
      </div>
    </section>

    <!-- ============ 音色统计卡（音色分类专用；与资产质检卡信息层级分离） ============ -->
    <section v-else class="shrink-0 px-6 pt-5 lg:px-8" aria-label="音色统计">
      <div class="grid grid-cols-1 gap-4 md:grid-cols-3">
        <!-- 统计 1：可用音色 -->
        <div class="gd-card flex items-center justify-between gap-3 p-4">
          <div class="flex min-w-0 flex-col gap-1">
            <span class="gd-section-label">可用音色库</span>
            <div class="flex items-baseline gap-2">
              <span class="gd-num text-2xl">{{ voices.length }}</span>
              <span class="rounded-full bg-primary-50 px-2 py-0.5 text-caption font-medium text-primary-300">系统声线</span>
            </div>
            <span class="truncate text-caption text-neutral-500">每个音色均有唯一 ID，供配音模块引用</span>
          </div>
          <span class="gd-icon-chip gd-icon-chip--violet" aria-hidden="true">
            <svg class="gd-icon" viewBox="0 0 24 24">
              <path d="M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Z" />
              <path d="M19 11a7 7 0 0 1-14 0M12 18v3" />
            </svg>
          </span>
        </div>

        <!-- 统计 2：声线分布 -->
        <div class="gd-card flex items-center justify-between gap-3 p-4">
          <div class="flex min-w-0 flex-col gap-1">
            <span class="gd-section-label">声线分布</span>
            <div class="flex items-baseline gap-2">
              <span class="gd-num text-2xl">
                {{ voiceGenderCounts.female }} / {{ voiceGenderCounts.male }} / {{ voiceGenderCounts.neutral }}
              </span>
              <span class="rounded-full bg-info-bg px-2 py-0.5 text-caption font-medium text-info-bright">女 / 男 / 中性</span>
            </div>
            <span class="truncate text-caption text-neutral-500">可按声线与风格筛选后试听</span>
          </div>
          <span class="gd-icon-chip gd-icon-chip--cyan" aria-hidden="true">
            <svg class="gd-icon" viewBox="0 0 24 24">
              <path d="M4 12v3M8 8v9M12 5v14M16 8v9M20 12v3" />
            </svg>
          </span>
        </div>

        <!-- 统计 3：已绑定角色 -->
        <div class="gd-card flex items-center justify-between gap-3 p-4">
          <div class="flex min-w-0 flex-col gap-1">
            <span class="gd-section-label">已绑定音色的角色</span>
            <div class="flex items-baseline gap-2">
              <span class="gd-num text-2xl">{{ gate.voiceBoundTotal }} / {{ gate.typeTotals.character }}</span>
              <span class="rounded-full bg-primary-50 px-2 py-0.5 text-caption font-medium text-primary-300">绑定关系</span>
            </div>
            <span class="truncate text-caption text-neutral-500">在音色详情可查看绑定角色并跳转</span>
          </div>
          <span class="gd-icon-chip gd-icon-chip--magenta" aria-hidden="true">
            <svg class="gd-icon" viewBox="0 0 24 24">
              <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z" />
              <path d="M4 20c1.8-3.2 4.6-5 8-5s6.2 1.8 8 5" />
            </svg>
          </span>
        </div>
      </div>
    </section>

    <!-- ============ 筛选行（tab 药丸组 + 图例 + 状态 chips + 全量下拉） ============ -->
    <section class="shrink-0 px-6 pb-2 pt-5 lg:px-8" aria-label="资产筛选">
      <div class="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <!-- 类型 tab 药丸组（带真实计数，默认「全部资产」） -->
        <div
          class="flex items-center gap-1.5 overflow-x-auto rounded-md border border-hairline bg-surface-inset p-1"
          role="group"
          aria-label="资产类型筛选"
        >
          <button
            v-for="tab in TYPE_TABS"
            :key="tab.value"
            type="button"
            class="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-sm px-3.5 text-sm font-medium transition-[background-color,color,box-shadow] duration-gd-fast ease-gd-standard"
            :class="
              activeTab === tab.value
                ? 'bg-surface-raised text-neutral-800 shadow-gd-1'
                : 'text-neutral-500 hover:text-neutral-700'
            "
            :aria-pressed="activeTab === tab.value"
            @click="activeTab = tab.value"
          >
            {{ tab.label }}
            <span v-if="tab.count !== null" class="gd-num text-xs opacity-70">({{ tab.count }})</span>
          </button>
        </div>

        <!-- 一致性图例（纯结构件：三段分值语义，与语义色一一对应；音色分类不适用） -->
        <div v-if="!isVoiceTab" class="gd-num flex items-center gap-4 text-caption text-neutral-400">
          <span class="inline-flex items-center gap-1.5">
            <span class="h-2.5 w-2.5 rounded-sm bg-success" aria-hidden="true" />≥ 0.90 一致
          </span>
          <span class="inline-flex items-center gap-1.5">
            <span class="h-2.5 w-2.5 rounded-sm bg-warning" aria-hidden="true" />0.80 - 0.89 预警
          </span>
          <span class="inline-flex items-center gap-1.5">
            <span class="h-2.5 w-2.5 rounded-sm bg-danger" aria-hidden="true" />&lt; 0.80 不合格
          </span>
        </div>
      </div>

      <!-- 第二行：音色分类 → 声线 + 风格筛选；资产分类 → 快捷状态 chips + 计数 + 全量状态筛选 -->
      <div v-if="isVoiceTab" class="mt-3 flex flex-col gap-3">
        <div class="flex flex-wrap items-center gap-2" role="group" aria-label="音色声线筛选">
          <span class="gd-section-label mr-1">声线</span>
          <button
            v-for="item in VOICE_GENDER_TABS"
            :key="item.value"
            type="button"
            class="inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1 text-xs transition-[background-color,border-color,color] duration-gd-fast ease-gd-standard"
            :class="
              voiceGenderFilter === item.value
                ? 'border-neutral-900 gd-fill-ink'
                : 'border-hairline bg-surface-raised text-neutral-500 hover:border-hairline-strong'
            "
            :aria-pressed="voiceGenderFilter === item.value"
            @click="voiceGenderFilter = item.value"
          >
            {{ item.label }}
            <span v-if="item.value" class="gd-num text-micro opacity-70">{{ voiceGenderCounts[item.value] ?? 0 }}</span>
          </button>
          <div class="flex-1" />
          <span class="gd-num text-xs text-neutral-400">共 {{ filteredVoices.length }} / {{ voices.length }} 个音色</span>
        </div>

        <div v-if="voiceStyleOptions.length > 0" class="flex flex-wrap items-center gap-2" role="group" aria-label="音色风格筛选">
          <span class="gd-section-label mr-1">风格</span>
          <button
            type="button"
            class="inline-flex items-center rounded-full border px-3.5 py-1 text-xs transition-[background-color,border-color,color] duration-gd-fast ease-gd-standard"
            :class="
              voiceStyleFilter === ''
                ? 'border-neutral-900 gd-fill-ink'
                : 'border-hairline bg-surface-raised text-neutral-500 hover:border-hairline-strong'
            "
            :aria-pressed="voiceStyleFilter === ''"
            @click="voiceStyleFilter = ''"
          >
            全部风格
          </button>
          <button
            v-for="tag in voiceStyleOptions"
            :key="tag"
            type="button"
            class="inline-flex items-center rounded-full border px-3.5 py-1 text-xs transition-[background-color,border-color,color] duration-gd-fast ease-gd-standard"
            :class="
              voiceStyleFilter === tag
                ? 'border-neutral-900 gd-fill-ink'
                : 'border-hairline bg-surface-raised text-neutral-500 hover:border-hairline-strong'
            "
            :aria-pressed="voiceStyleFilter === tag"
            @click="voiceStyleFilter = tag"
          >
            {{ voiceStyleLabel(tag) }}
          </button>
        </div>
      </div>

      <div v-else class="mt-3 flex flex-wrap items-center gap-2" role="group" aria-label="快捷状态筛选">
        <button
          v-for="chip in QUICK_STATUS_CHIPS"
          :key="chip.value"
          type="button"
          class="inline-flex items-center gap-1.5 rounded-full border px-3.5 py-1 text-xs transition-[background-color,border-color,color] duration-gd-fast ease-gd-standard"
          :class="
            statusFilter === chip.value
              ? 'border-neutral-900 gd-fill-ink'
              : 'border-hairline bg-surface-raised text-neutral-500 hover:border-hairline-strong'
          "
          :aria-pressed="statusFilter === chip.value"
          @click="statusFilter = chip.value"
        >
          <span v-if="chip.danger" class="h-1.5 w-1.5 rounded-full bg-danger" aria-hidden="true" />
          {{ chip.label }}
        </button>

        <div class="flex-1" />

        <!-- 批量操作条（批量模式：全选 / 已选 N / 批量通过 / 清除） -->
        <div v-if="batchMode" class="flex items-center gap-2.5 rounded-full bg-primary-50 py-1.5 pl-4 pr-1.5">
          <label class="flex cursor-pointer items-center gap-1.5 text-xs text-neutral-600">
            <input
              type="checkbox"
              class="h-3.5 w-3.5 accent-primary-500"
              :checked="reviewableAssets.length > 0 && selectedIds.size >= reviewableAssets.length"
              :disabled="reviewableAssets.length === 0"
              @change="toggleSelectAll"
            />
            全选待审核
          </label>
          <span class="gd-num text-xs font-medium text-primary-700">已选 {{ selectedCount }} 项</span>
          <AppButton
            variant="primary"
            size="sm"
            :disabled="selectedCount === 0"
            :loading="batchApproving"
            @click="batchApproveDialogOpen = true"
          >
            批量通过
          </AppButton>
          <button
            type="button"
            class="rounded-full px-2.5 py-1 text-xs text-neutral-500 transition-colors hover:bg-surface-sunken"
            @click="selectedIds = new Set()"
          >
            清除选择
          </button>
        </div>
        <span v-else class="gd-num text-xs text-neutral-400">共 {{ total }} 项</span>

        <select v-model="statusFilter" class="gd-input gd-field-sm w-32" aria-label="状态筛选">
          <option v-for="opt in STATUS_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
        </select>
      </div>
    </section>

    <!-- ============ 照片合成角色（角色设计模块内嵌能力，原独立入口已取消） ============ -->
    <section v-if="activeTab === 'character'" class="shrink-0 px-6 lg:px-8" aria-label="照片合成角色">
      <div class="rounded-lg border border-hairline bg-surface-canvas">
        <button
          type="button"
          class="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-inset"
          :aria-expanded="photoPanelOpen"
          @click="photoPanelOpen = !photoPanelOpen"
        >
          <span class="flex min-w-0 items-center gap-2.5">
            <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-primary-50 text-sm" aria-hidden="true">
              📷
            </span>
            <span class="min-w-0">
              <span class="block text-sm font-semibold text-neutral-900">照片合成角色</span>
              <span class="block text-caption text-neutral-500">
                上传真人照片 → 四道闸合规 → 身份档聚类 → M1 风格化合成 → 落为待审核角色资产
              </span>
            </span>
          </span>
          <svg
            class="h-4 w-4 shrink-0 text-neutral-400 transition-transform"
            :class="photoPanelOpen ? 'rotate-180' : ''"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            aria-hidden="true"
          >
            <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <div v-if="photoPanelOpen" class="max-h-[560px] overflow-y-auto border-t border-hairline p-4">
          <PhotoCharacterPanel :project-id="projectId" />
        </div>
      </div>
    </section>

    <!-- ============ 内容区 ============ -->
    <div class="shrink-0 px-6 pb-6 pt-3 lg:px-8">
      <!-- 骨架屏 -->
      <div v-if="loading || voicesLoading" class="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
        <LoadingSkeleton v-for="i in 6" :key="i" variant="card" />
      </div>

      <!-- 错误态 -->
      <div v-else-if="loadError">
        <ErrorRetryBlock message="资产列表加载失败" :description="loadError" @retry="fetchAssets" />
      </div>

      <!-- 大空态：项目尚未规划任何资产 -->
      <div v-else-if="isUnplanned && activeTab !== 'voice'">
        <EmptyState
          icon="🪄"
          title="还没有任何数字资产"
          description="从剧本一键提取角色 / 场景 / 道具清单，AI 将逐个生成主形象、三视图与表情集，审核通过后锁定为分镜生成锚点。"
        >
          <template #action>
            <div class="flex flex-wrap items-center justify-center gap-2.5">
              <AppButton v-if="planBlocked" variant="primary" size="lg" @click="goScript">去剧本页生成剧本 →</AppButton>
              <AppButton
                variant="primary"
                size="lg"
                :loading="planning"
                :disabled="planBlocked"
                :title="planHint"
                @click="startPlan"
              >
                ✨ 智能规划资产
              </AppButton>
            </div>
          </template>
        </EmptyState>
      </div>

      <!-- 小空态：当前筛选 / 搜索组合无结果。
           判空必须按 Tab 分开看：音色 Tab 读 filteredVoices，其余 Tab 读 filteredAssets（含搜索过滤）。 -->
      <div v-else-if="isVoiceTab ? filteredVoices.length === 0 : filteredAssets.length === 0">
        <EmptyState
          :icon="activeTabIcon"
          :title="
            isVoiceTab
              ? voices.length === 0
                ? '音色库为空'
                : '未找到匹配的音色'
              : searchQuery.trim()
                ? '未找到匹配的资产'
                : statusFilter
                  ? '该筛选组合下暂无资产'
                  : `暂无${activeTab === '' ? '' : ASSET_TYPE_LABELS[activeTab]}资产`
          "
          :description="
            isVoiceTab
              ? voices.length === 0
                ? '系统音色库暂未配置可用音色'
                : '试试更短的关键词，或切换声线 / 风格筛选后重试'
              : searchQuery.trim()
                ? '试试更短的关键词，或清空搜索后重试'
                : '可调整状态筛选，或点击右上角「智能规划资产」重新提取'
          "
        />
      </div>

      <!-- ============ 音色库卡片（可点击进详情 + 卡片内试听 + 选中态） ============ -->
      <div v-else-if="isVoiceTab" class="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
        <!-- 音色试听出错提示（合成失败 / 音频加载失败） -->
        <p
          v-if="voicePlaybackError"
          class="rounded-lg bg-danger-bg px-3 py-2 text-caption text-danger-bright sm:col-span-2 xl:col-span-3"
          role="alert"
        >
          {{ voicePlaybackError }}
        </p>

        <article
          v-for="voice in filteredVoices"
          :key="voice.id"
          class="gd-card gd-card-interactive group relative flex cursor-pointer flex-col overflow-hidden p-0 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-300"
          :class="[
            selectedVoiceId === voice.id ? 'border-primary-400 ring-2 ring-primary-200' : '',
            playingId === voice.id ? 'shadow-gd-2' : '',
          ]"
          role="link"
          tabindex="0"
          :aria-label="`音色 ${voice.name}，唯一 ID ${voice.id}，点击查看详情与试听`"
          :aria-current="selectedVoiceId === voice.id ? 'true' : undefined"
          @click="goVoiceDetail(voice)"
          @keydown="handleVoiceCardKeydown($event, voice)"
        >
          <!-- 选中态：左缘品牌光条 + 顶部细光（双重反馈，不只依赖描边） -->
          <span
            v-if="selectedVoiceId === voice.id"
            class="absolute left-0 top-5 h-10 w-1 rounded-full bg-grad-primary shadow-gd-bloom"
            aria-hidden="true"
          />
          <span
            v-if="selectedVoiceId === voice.id"
            class="pointer-events-none absolute inset-x-6 top-0 h-px bg-grad-primary opacity-70"
            aria-hidden="true"
          />

          <!-- 卡头：头像 + 名称 / 键名 + 短编号徽标 -->
          <div class="flex items-start gap-3 p-5 pb-3">
            <span
              class="relative flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-primary-100 to-primary-50 text-xl"
              aria-hidden="true"
            >
              🎙️
              <span
                v-if="playingId === voice.id"
                class="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 animate-pulse rounded-full bg-info ring-2 ring-surface-raised"
              />
            </span>
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2">
                <h3 class="truncate font-serif text-base font-semibold text-neutral-900">{{ voice.name }}</h3>
                <StatusTag
                  :status="voice.status === 'available' ? 'approved' : 'deprecated'"
                  :label="voice.status === 'available' ? '可用' : '不可用'"
                  size="sm"
                />
              </div>
              <p class="gd-num mt-0.5 truncate text-caption text-neutral-400">{{ voice.voice_key }}</p>
            </div>
            <span
              class="shrink-0 rounded-full border border-primary-200 bg-primary-50 px-2 py-0.5 font-mono text-micro text-primary-700"
            >
              {{ voiceIdTag(voice.id) }}
            </span>
          </div>

          <!-- 唯一 ID + 声线信息（ID 为配音模块引用主键） -->
          <div class="mx-5 flex items-center justify-between gap-3 rounded-lg bg-surface-canvas px-3 py-2">
            <span class="flex min-w-0 items-center gap-1.5 text-caption text-neutral-500">
              <svg
                class="h-3.5 w-3.5 shrink-0"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="1.8"
                aria-hidden="true"
              >
                <path d="M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Z" />
                <path d="M19 11a7 7 0 0 1-14 0M12 18v3" stroke-linecap="round" />
              </svg>
              <span class="truncate">
                {{ VOICE_GENDER_LABELS[voice.gender] ?? voice.gender }} · {{ voice.language }}
              </span>
            </span>
            <span class="gd-num shrink-0 text-caption text-neutral-500">
              ID <span class="text-primary-300">#{{ voice.id }}</span>
            </span>
          </div>

          <!-- 风格标签（真实标签 + 中文展示） -->
          <div class="mt-3 flex min-h-[26px] flex-wrap items-center gap-1.5 px-5">
            <span v-for="tag in voiceStyleTagList(voice.style_tags)" :key="tag" class="gd-tag">
              {{ voiceStyleLabel(tag) }}
            </span>
            <span v-if="voiceStyleTagList(voice.style_tags).length === 0" class="text-caption text-neutral-400">
              暂无风格标签
            </span>
          </div>

          <!-- 试听波形 + 播放进度反馈（暂停后进度保留） -->
          <div class="mt-3 px-5">
            <div class="flex h-10 items-end gap-[3px]" aria-hidden="true">
              <span
                v-for="index in 24"
                :key="index"
                class="voice-mini-wave min-w-0 flex-1 rounded-full transition-colors duration-gd-fast"
                :class="[
                  isVoiceBarActive(voice.id, index - 1, 24)
                    ? 'bg-primary-400'
                    : playingId === voice.id
                      ? 'bg-primary-100'
                      : 'bg-neutral-200',
                  playingId === voice.id ? 'is-live' : '',
                ]"
                :style="{ height: `${22 + ((index * 29) % 62)}%`, animationDelay: `-${(index % 7) * 0.12}s` }"
              />
            </div>
            <div class="mt-2 h-1 w-full overflow-hidden rounded-full bg-surface-sunken">
              <div
                class="h-full rounded-full bg-grad-primary transition-[width] duration-gd-fast ease-gd-standard"
                :style="{ width: `${voiceProgress(voice.id) * 100}%` }"
              />
            </div>
            <div class="mt-1 flex items-center justify-between text-micro text-neutral-400">
              <span class="gd-num">{{ formatAudioTime(voicePosition(voice.id)) }}</span>
              <span v-if="playingId === voice.id" class="inline-flex items-center gap-1 text-primary-300">
                <span class="h-1 w-1 rounded-full bg-primary-400" aria-hidden="true" />试听中
              </span>
              <span v-else class="gd-num">{{ formatAudioTime(voiceDuration(voice.id)) }}</span>
            </div>
          </div>

          <!-- 页脚：试听 / 详情（阻止冒泡，避免误跳详情） -->
          <div class="mt-auto flex items-center gap-2 border-t border-hairline px-5 py-3.5" @click.stop>
            <AppButton
              variant="secondary"
              size="sm"
              :loading="loadingId === voice.id"
              :disabled="voice.status !== 'available'"
              :aria-label="`试听音色 ${voice.name}`"
              @click="toggleVoicePreview(voice)"
            >
              <span class="inline-flex items-center gap-1.5">
                <svg v-if="playingId === voice.id" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <rect x="7" y="5" width="4" height="14" rx="1" />
                  <rect x="13" y="5" width="4" height="14" rx="1" />
                </svg>
                <svg v-else class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M8 5.14v13.72c0 .8.87 1.3 1.56.9l11-6.86a1.05 1.05 0 000-1.8l-11-6.86A1.05 1.05 0 008 5.14z" />
                </svg>
                {{ playingId === voice.id ? '暂停试听' : '试听' }}
              </span>
            </AppButton>
            <span class="min-w-0 flex-1 truncate text-caption text-neutral-400">
              {{ selectedVoiceId === voice.id ? '已选中 · 点击卡片查看详情' : '点击卡片查看详情' }}
            </span>
            <AppButton variant="text" size="sm" @click="goVoiceDetail(voice)">详情 →</AppButton>
          </div>
        </article>
      </div>

      <!-- ============ 资产卡网格（stitch_prd/p3：卡头 → 夜窗 → 基准组 → 标签 → 音色条 → 页脚） ============ -->
      <div v-else class="grid grid-cols-1 gap-5 sm:grid-cols-2 xl:grid-cols-3">
        <article
          v-for="asset in filteredAssets"
          :key="asset.id"
          class="gd-card gd-card-interactive group relative flex cursor-pointer flex-col p-5"
          :class="[
            asset.status === 'qc_failed' ? 'ring-1 ring-danger' : '',
            batchMode && selectedIds.has(asset.id) ? 'border-primary-400 ring-2 ring-primary-200' : '',
          ]"
          :aria-label="`资产 ${asset.name}`"
          @click="batchMode && asset.status === 'pending_review' ? toggleSelected(asset) : goDetail(asset)"
        >
          <!-- qc_failed 顶部红警示条（对齐原型异常卡 floating 红条；last_error 为真实数据） -->
          <div
            v-if="asset.status === 'qc_failed'"
            class="mb-3 flex items-start gap-2 rounded-lg bg-danger-bg px-3 py-2 text-caption text-danger-bright"
            role="alert"
          >
            <span class="mt-0.5 shrink-0" aria-hidden="true">⛔</span>
            <span class="min-w-0 flex-1">
              {{ failureLabel(asset) }} · {{ metaText(asset, 'last_error') || '画面需人工复核后重抽' }}
            </span>
          </div>

          <!-- 卡头：名称 + 类型 tag ｜ 版本 / 质检结论列 -->
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <div class="flex items-center gap-2">
                <h3 class="min-w-0 truncate font-serif text-base font-semibold text-neutral-900">{{ asset.name }}</h3>
                <span class="shrink-0 rounded-full bg-surface-sunken px-2 py-0.5 text-caption text-neutral-500">
                  {{ ASSET_TYPE_LABELS[asset.asset_type] }}
                </span>
              </div>
              <p v-if="displayDesc(asset)" class="mt-0.5 truncate text-caption text-neutral-500">
                {{ displayDesc(asset) }}
              </p>
            </div>
            <div class="flex shrink-0 flex-col items-end gap-0.5">
              <span class="gd-num text-sm" :class="qcConclusion(asset).tone">
                v#{{ asset.current_version_id ?? '—' }}
              </span>
              <span class="text-caption font-medium" :class="qcConclusion(asset).tone">
                {{ qcConclusion(asset).label }}
              </span>
            </div>
          </div>

          <!-- 夜窗：暗底渐变封面（.gd-night-window 外框 + .gd-cv-N 渐变族） -->
          <div class="gd-night-window gd-night-window-sm mt-3 p-2">
            <div
              class="relative h-64 w-full overflow-hidden rounded-lg"
              :class="
                asset.status === 'generating' || asset.status === 'planning'
                  ? 'bg-surface-sunken'
                  : ['gd-cv-cover', coverClass(asset)]
              "
            >
              <!-- 选中圆框（批量模式） -->
              <span
                v-if="batchMode"
                class="absolute right-2.5 top-2.5 z-10 flex h-6 w-6 items-center justify-center rounded-full border-2 bg-[rgba(4,7,15,0.66)] shadow-gd-1 backdrop-blur-[6px]"
                :class="selectedIds.has(asset.id) ? 'border-primary-500' : 'border-hairline-strong'"
                @click.stop="toggleSelected(asset)"
              >
                <input
                  type="checkbox"
                  class="h-3.5 w-3.5 accent-primary-500"
                  :checked="selectedIds.has(asset.id)"
                  :disabled="asset.status !== 'pending_review'"
                  @click.stop
                  @change="toggleSelected(asset)"
                />
              </span>

              <!-- 生成中骨架：进度环 + 状态胶囊（SSE / 轮询刷新期间展示） -->
              <div
                v-if="asset.status === 'generating' || asset.status === 'planning'"
                class="flex h-full w-full flex-col items-center justify-center gap-3"
              >
                <svg class="h-12 w-12 animate-spin text-primary-500" viewBox="0 0 36 36" fill="none" aria-hidden="true">
                  <circle cx="18" cy="18" r="15" class="stroke-neutral-200" stroke-width="4" />
                  <path d="M18 3 a15 15 0 0 1 14.7 12" stroke="currentColor" stroke-width="4" stroke-linecap="round" />
                </svg>
                <span
                  class="inline-flex items-center gap-1.5 rounded-full border border-info-border bg-info-bg px-2.5 py-0.5 text-caption font-medium text-info"
                >
                  <span class="h-1.5 w-1.5 rounded-full bg-info" aria-hidden="true" />
                  {{ asset.status === 'planning' ? 'planning · 规划中' : 'generating · 生成中' }}
                </span>
              </div>

              <template v-else>
                <!-- 左上徽标组：ASSET_ID + 来源 / 锚点（对齐原型 ASSET_ID + LoRA 徽标位） -->
                <div class="absolute left-2.5 top-2.5 z-10 flex flex-wrap items-center gap-1.5">
                  <span class="rounded bg-scrim-strong px-2 py-0.5 font-mono text-micro text-primary-200 backdrop-blur-[6px]">
                    ASSET_ID: {{ assetIdTag(asset) }}
                  </span>
                  <span
                    class="rounded px-2 py-0.5 text-micro backdrop-blur-[6px]"
                    :class="asset.status === 'locked' ? 'gd-fill-mint' : 'bg-scrim-soft text-neutral-200'"
                  >
                    {{ asset.status === 'locked' ? '分镜锚点' : sourceLabel(asset) }}
                  </span>
                </div>

                <!-- 底部信息行：状态点 + 状态码 ｜ 更新时间（对齐原型 scrubber 行） -->
                <div class="absolute inset-x-3 bottom-2.5 z-10 flex items-center justify-between gap-2">
                  <span class="inline-flex min-w-0 items-center gap-1.5">
                    <span
                      class="h-1.5 w-1.5 shrink-0 rounded-full"
                      :class="COVER_STATUS_DOT[asset.status] ?? 'bg-neutral-300'"
                      aria-hidden="true"
                    />
                    <span class="truncate font-mono text-micro text-neutral-200">
                      {{ COVER_STATUS_LABELS[asset.status] ?? asset.status }}
                    </span>
                  </span>
                  <span class="gd-timecode shrink-0">{{ formatRelativeTime(asset.updated_at) }}</span>
                </div>
              </template>
            </div>
          </div>

          <!-- 图下 meta：版本元信息行 -->
          <p class="gd-num mt-3 truncate text-caption text-neutral-400">
            v{{ asset.current_version_id ?? '—' }} · {{ versionLabel(asset) }}
            <template v-if="metaText(asset, 'regenerate_count')"> · 已重抽 {{ metaText(asset, 'regenerate_count') }} 次</template>
          </p>

          <div class="mt-3 flex flex-1 flex-col gap-3.5">
            <!-- 版本基准槽位组（角色=正交三视图；场景/道具=参考图；点击进详情） -->
            <div v-if="refSlots(asset).length > 0" class="space-y-1.5">
              <div class="flex items-center justify-between gap-2">
                <span class="gd-section-label">{{ REF_TITLES[asset.asset_type] ?? '版本基准' }}</span>
                <span
                  class="gd-num text-caption"
                  :class="asset.current_version_id ? 'text-success' : 'text-neutral-400'"
                >
                  {{ asset.current_version_id ? '基准就绪' : '待齐套' }}
                </span>
              </div>
              <div class="grid gap-2" :class="refSlots(asset).length > 1 ? 'grid-cols-3' : 'grid-cols-1'">
                <button
                  v-for="slot in refSlots(asset)"
                  :key="slot.type"
                  type="button"
                  class="group/ref relative h-20 overflow-hidden rounded-lg text-left transition-transform duration-gd-fast ease-gd-standard hover:-translate-y-0.5"
                  :aria-label="`${slot.label} · 查看资产详情`"
                  @click.stop="goDetail(asset)"
                >
                  <span class="absolute inset-0" :class="['gd-cv-cover', coverClass(asset)]" aria-hidden="true" />
                  <span class="absolute inset-x-0 bottom-0 flex items-center justify-between gap-2 bg-scrim-soft px-1.5 py-0.5">
                    <span class="text-micro text-neutral-200">{{ slot.label }}</span>
                    <span class="font-mono text-micro text-neutral-300">{{ ASSET_VERSION_TYPE_LABELS[slot.type] }}</span>
                  </span>
                </button>
              </div>
            </div>

            <!-- 特征标签 chips（真实 tags 数据；无标签时整组隐藏，不造假数据） -->
            <div v-if="asset.tags && asset.tags.length > 0" class="space-y-1.5">
              <span class="gd-section-label">特征标签 · TAGS</span>
              <div class="flex flex-wrap gap-1.5">
                <span v-for="tag in asset.tags" :key="tag" class="gd-tag">{{ tag }}</span>
              </div>
            </div>

            <!-- 音色绑定条（角色卡专属；voice_id 为真实绑定关系） -->
            <button
              v-if="asset.asset_type === 'character'"
              type="button"
              class="flex w-full items-center justify-between gap-2 rounded-lg bg-surface-sunken px-3 py-2.5 text-left transition-colors duration-gd-fast ease-gd-standard hover:bg-state-hover"
              @click.stop="goDetail(asset)"
            >
              <span class="flex min-w-0 items-center gap-2.5">
                <span
                  class="gd-icon-chip gd-icon-chip--sm"
                  :class="asset.voice_id ? 'gd-icon-chip--violet' : ''"
                  aria-hidden="true"
                >
                  <svg class="gd-icon gd-icon--sm" viewBox="0 0 24 24">
                    <path d="M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Z" />
                    <path d="M19 11a7 7 0 0 1-14 0M12 18v3" />
                  </svg>
                </span>
                <span class="flex min-w-0 flex-col">
                  <span class="truncate text-xs font-medium text-neutral-800">
                    {{ asset.voice_id ? `已绑定音色 #${asset.voice_id}` : '未绑定专属音色' }}
                  </span>
                  <span class="gd-timecode truncate">
                    {{ asset.voice_id ? '前往角色详情试听 / 更换音色' : '绑定后供台词配音 · 前往角色详情' }}
                  </span>
                </span>
              </span>
              <span class="gd-badge shrink-0" :class="asset.voice_id ? 'gd-badge--mint' : ''">
                {{ asset.voice_id ? '已绑定' : '未绑定' }}
              </span>
            </button>

            <!-- 驳回反馈（rejected，保留原功能） -->
            <p
              v-if="asset.status === 'rejected' && metaText(asset, 'reject_feedback')"
              class="line-clamp-2 rounded-md bg-warning-bg px-2 py-1.5 text-caption leading-[1.65] text-gd-pending"
            >
              驳回原因：{{ metaText(asset, 'reject_feedback') }}
            </p>
          </div>

          <!-- 页脚操作（border-t；按状态分派，全部沿用既有处理器） -->
          <div class="mt-4 flex items-center justify-between gap-2 border-t border-hairline pt-3" @click.stop>
            <template v-if="asset.status === 'pending_review'">
              <div class="flex items-center gap-1.5">
                <AppButton variant="primary" size="sm" :loading="cardBusyId === asset.id" @click="approveCard(asset)">
                  ✓ 通过
                </AppButton>
                <AppButton variant="danger" size="sm" :disabled="cardBusyId === asset.id" @click="openRejectDialog(asset)">
                  ✕ 不通过
                </AppButton>
              </div>
              <AppButton variant="ghost" size="sm" :disabled="cardBusyId === asset.id" @click="openRegenerateDialog(asset)">
                ↻ 重抽
              </AppButton>
            </template>

            <template v-else-if="asset.status === 'qc_failed'">
              <!-- Q-022：失败原因已在卡顶红警示条展示（last_error），
                   页脚补快捷重试（无反馈直接受理）+ 带反馈重抽两条路径 -->
              <AppButton
                variant="danger"
                size="sm"
                :loading="cardBusyId === asset.id"
                @click="quickRegenerate(asset)"
              >
                ↻ 直接重试
              </AppButton>
              <AppButton
                variant="secondary"
                size="sm"
                :disabled="cardBusyId === asset.id"
                @click="openRegenerateDialog(asset)"
              >
                带反馈重抽
              </AppButton>
              <AppButton variant="subtle" size="sm" @click="goDetail(asset)">复核报告</AppButton>
            </template>

            <template v-else-if="asset.status === 'rejected'">
              <AppButton variant="secondary" size="sm" :loading="cardBusyId === asset.id" @click="quickRegenerate(asset)">
                ↻ 重新生成
              </AppButton>
              <AppButton variant="ghost" size="sm" @click="goDetail(asset)">查看详情</AppButton>
            </template>

            <template v-else-if="asset.status === 'approved'">
              <AppButton
                variant="ghost"
                size="sm"
                :loading="cardBusyId === asset.id"
                @click="openRegenerateDialog(asset)"
              >
                ↻ 重抽形象
              </AppButton>
              <AppButton variant="text" @click="goDetail(asset)">查看详情 →</AppButton>
            </template>

            <template v-else-if="asset.status === 'locked'">
              <span class="gd-badge gd-badge--mint">
                <span class="gd-badge__dot" aria-hidden="true" />已锁定 · 分镜生成锚点
              </span>
              <AppButton variant="text" @click="goDetail(asset)">查看详情 →</AppButton>
            </template>

            <template v-else>
              <span class="gd-timecode">{{ versionLabel(asset) }}</span>
              <AppButton variant="text" @click="goDetail(asset)">查看详情 →</AppButton>
            </template>
          </div>
        </article>
      </div>

      <!-- ============ 底部 HITL 关口审批条（sticky 于滚动容器，对齐原型底部审批 aside） ============ -->
      <section
        v-if="activeTab !== 'voice' && !loading && !loadError && assets.length > 0"
        class="sticky bottom-0 z-raised mt-6"
        aria-label="关口二：资产确认"
      >
        <!-- P2-12：窄视口限制审批条高度并允许自身滚动，避免遮挡资产卡片按钮（需滚动才能点击） -->
        <div class="gd-card flex max-h-[42vh] flex-col gap-3 overflow-y-auto px-4 py-3 shadow-gd-3 xl:max-h-none xl:flex-row xl:items-center xl:justify-between xl:px-5 xl:py-3.5">
          <!-- 左：图标块 + 标题 + 徽章 + 达标摘要 -->
          <div class="flex min-w-0 items-center gap-3">
            <span class="gd-icon-chip gd-icon-chip--violet shrink-0" aria-hidden="true">
              <svg class="gd-icon" viewBox="0 0 24 24">
                <path d="M12 3l7 3v5c0 4.4-2.9 7.6-7 9-4.1-1.4-7-4.6-7-9V6l7-3Z" />
                <path d="m9.5 12 2 2 3.5-3.5" />
              </svg>
            </span>
            <div class="min-w-0">
              <div class="flex flex-wrap items-center gap-2">
                <span class="text-sm font-semibold text-neutral-900">HITL 关口二 · 视觉资产锚点锁定审批</span>
                <span class="gd-badge gd-badge--violet">前置阻塞关口</span>
              </div>
              <p class="gd-num mt-0.5 truncate text-xs text-neutral-500">
                全剧资产 {{ entityTotal }} 项 · 已达标
                <span class="font-semibold text-success">{{ passedTotal }}/{{ entityTotal }}</span>
                <template v-if="gate.pendingReviewTotal > 0">
                  · 待人工核检
                  <span class="font-semibold text-danger">{{ gate.pendingReviewTotal }} 项</span>
                </template>
              </p>
            </div>
          </div>

          <!-- 右：决策按钮组 -->
          <div class="flex shrink-0 flex-wrap items-center gap-3">
            <AppButton variant="secondary" size="md" @click="batchMode ? exitBatchMode() : (batchMode = true)">
              批量审核 (已选 {{ selectedCount }})
            </AppButton>
            <AppButton variant="secondary" size="md" @click="goAuthorization">真人授权留痕</AppButton>
            <AppButton
              variant="primary"
              size="md"
              :disabled="entityTotal === 0 || (approvedCount === 0 && gate.lockedTotal === 0)"
              @click="approvedCount === 0 ? goStoryboard() : (lockDialogOpen = true)"
            >
              <svg class="gd-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M7 11V8a5 5 0 0 1 10 0v3" />
                <path d="M5 11h14v9H5z" />
              </svg>
              批准全部并锁定 · 进入分镜
            </AppButton>
          </div>
        </div>
      </section>

      <!-- 锁定结果汇总面板（错误列表） -->
      <section v-if="lockResult" class="gd-card mt-5 p-6" aria-live="polite">
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold text-neutral-900">
            全部锁定结果：<span class="gd-num text-success">成功 {{ lockResult.ok }}</span>
            <span v-if="lockResult.failures.length > 0" class="gd-num text-danger"> · 失败 {{ lockResult.failures.length }}</span>
          </h3>
          <div class="flex items-center gap-2">
            <AppButton
              v-if="lockResult.failures.length === 0 && lockResult.ok > 0"
              variant="primary"
              size="sm"
              @click="goStoryboard"
            >
              去分镜拆解 →
            </AppButton>
            <AppButton variant="ghost" size="sm" @click="lockResult = null">关闭</AppButton>
          </div>
        </div>
        <ul v-if="lockResult.failures.length > 0" class="mt-3 space-y-2">
          <li v-for="(fail, index) in lockResult.failures" :key="index" class="flex items-start gap-2 rounded-lg bg-danger-bg px-3 py-2 text-xs">
            <span
              class="mt-0.5 shrink-0 rounded-full px-1.5 py-0.5 text-caption font-semibold"
              :class="fail.missingView ? 'gd-fill-rose' : 'bg-surface-raised text-danger'"
            >
              {{ fail.missingView ? '缺视图' : '错误' }}
            </span>
            <span class="font-medium text-neutral-800">{{ fail.name }}</span>
            <span class="text-neutral-500">{{ fail.reason }}</span>
          </li>
        </ul>
      </section>
    </div>

    <!-- ============ 批量生成确认 ============ -->
    <ConfirmDialog
      v-model="batchGenerateDialogOpen"
      title="批量生成全部待生成资产？"
      :content="`将为 ${generatableAssets.length} 个规划中资产逐个发起首版图像生成（角色主形象 / 场景与道具参考图），完成后进入「待确认」。生成按张计费（约 10 积分/张），失败项不扣费。`"
      confirm-label="开始批量生成"
      :loading="batchGenerating"
      @confirm="confirmBatchGenerate"
      @cancel="batchGenerateDialogOpen = false"
    />

    <!-- ============ 批量通过确认 ============ -->
    <ConfirmDialog
      v-model="batchApproveDialogOpen"
      title="批量通过选中资产？"
      :content="`将逐个通过 ${selectedCount} 个待审核资产，通过后可锁定为分镜生成锚点。`"
      confirm-label="批量通过"
      :loading="batchApproving"
      @confirm="confirmBatchApprove"
      @cancel="batchApproveDialogOpen = false"
    />

    <!-- ============ 全部锁定确认 ============ -->
    <ConfirmDialog
      v-model="lockDialogOpen"
      title="锁定全部已通过资产？"
      content="锁定后资产将作为分镜生成的形象锚点；角色资产需主形象与三视图齐全且质检通过，缺失项将返回错误汇总。"
      confirm-label="开始锁定"
      :loading="locking"
      @confirm="confirmLockAll"
      @cancel="lockDialogOpen = false"
    />

    <!-- ============ 驳回弹窗（≥10 字强校验） ============ -->
    <ConfirmDialog
      v-model="rejectDialogOpen"
      :title="`驳回「${rejectTarget?.name ?? ''}」`"
      content="请填写至少 10 个字的驳回原因，AI 将按反馈重新生成形象。"
      confirm-label="提交驳回"
      :loading="rejecting"
      @confirm="confirmReject"
      @cancel="rejectTarget = null"
    >
      <textarea
        v-model="rejectFeedback"
        rows="3"
        class="gd-input"
        :class="rejectFeedback.length > 0 && rejectFeedback.trim().length < 10 ? 'gd-field-invalid' : ''"
        placeholder="例如：角色年龄感不符，形象偏年轻，请调整为 30 岁左右并更换深色西装…"
      />
      <p class="mt-1.5 text-xs" :class="rejectFeedback.trim().length >= 10 ? 'text-success' : 'text-neutral-400'">
        {{ rejectFeedback.trim().length }}/10 字（至少 10 字）
      </p>
    </ConfirmDialog>

    <!-- ============ 重抽弹窗（反馈可选；qc_failed 卡「带反馈重抽」同用此弹窗） ============ -->
    <ConfirmDialog
      v-model="regenerateDialogOpen"
      :title="`重抽「${regenerateTarget?.name ?? ''}」`"
      :content="`将重新生成${ASSET_VERSION_TYPE_LABELS[regenerateVersionType]}（旧版本保留可对比），可填写调整要求引导生成方向。`"
      confirm-label="发起重抽"
      :loading="regenerating"
      @confirm="confirmRegenerate"
      @cancel="regenerateTarget = null"
    >
      <textarea
        v-model="regenerateFeedback"
        rows="2"
        class="gd-input"
        placeholder="（可选）例如：光线更暖一些，背景改为黄昏色调…"
      />
    </ConfirmDialog>
  </div>
</template>

<style scoped>
/* 音色卡试听波形：播放时以柱底为轴轻微起伏，暂停即静止（进度点亮仍保留） */
.voice-mini-wave.is-live {
  animation: voice-card-wave 900ms var(--gd-ease-in-out) infinite alternate;
  transform-origin: bottom;
}

@keyframes voice-card-wave {
  from {
    transform: scaleY(0.45);
  }
  to {
    transform: scaleY(1);
  }
}

@media (prefers-reduced-motion: reduce) {
  .voice-mini-wave.is-live {
    animation: none;
  }
}
</style>

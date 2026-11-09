<!--
  文件级注释：P12 · 智能分镜（路由 /projects/:projectId/storyboard）。
  v10 版面梳理（移除画布矩阵模块 + 双栏占比对调，业务与数据流不变）：
  - 刊头：集名 + N 镜/时码徽标 + 状态 + 一行上下文（项目 · 卡点钩子）；右侧仅保留集选择
    与「连播 3min 节奏草稿」主 CTA；
  - 主区双栏对调（xl: 360px + minmax(0,1fr)）：左「镜头序列」窄栏（卡头：标题/计数/待确认；
    工具栏：节奏模型 chips + 全宽生成按钮；正文：按场分组列表，竖幅缩略图 + 两行文案 + ↑↓ 排序）；
    右「镜头监看」夜窗放大为主视觉（240px / 2xl 280px 9:16 监看 + 并存解算信息：景别/运镜/时码 +
    锚点 chips + 传输条 + 优化运镜词）；窄屏按 序列 → 监看 堆叠；
  - 移除 Konva 画布矩阵（网格/连线/拖拽/缩放/平移/自动布局/保存布局/视口裁剪）及其专属状态，
    列表成为唯一序列视图；上下移动仍走 reorderShots；
  - 底部审批卡（文档流）：H3 + 统计行（镜数/预估成片/预估消耗）+ 景别跳跃确认 +
    导演策略重拆 / 批准送审（锁定后进入 F5）；
  - decomposing 进度条、HITL-3 待审条、锁定只读条保留；60005 缺锚点清单可直接跳首个缺锚点镜头抽屉；
  - P13 镜头抽屉；SSE 不可用降级 30s 轮询。
  主题纪律：仅沿用视图与令牌中已有的 gd-* 类 / 颜色 / 字体 / 圆角 / 阴影体系，
  不引入新色值 / 新字体 / 新依赖。
-->
<script setup lang="ts">
/**
 * StoryboardCanvasView 分镜画布。
 * props：无。emits：无。
 * 依赖 project store、script/storyboard/assets API、useSSE（分镜生成事件）、useToast。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { getProject } from '@/api/projects'
import { getProjectCurrentScript, type EpisodeNode } from '@/api/script'
import {
  generateStoryboard,
  getStoryboardDetail,
  listStoryboards,
  lockStoryboard,
  reorderShots,
  type ShotItem,
  type StoryboardDetail,
  type StoryboardSummary,
} from '@/api/storyboard'
import { listAssets } from '@/api/assets'
import { ApiError } from '@/api/http'
import { redrawShotFrame } from '@/api/v4'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import Modal from '@/components/common/Modal.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import ShotDetailDrawer from '@/components/storyboard/ShotDetailDrawer.vue'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/composables/useToast'
import { CANVAS as CANVAS_TOKENS } from '@/constants/canvasTokens'
import { CAMERA_MOVE_OPTIONS, EMOTION_TAG_OPTIONS, SHOT_SIZE_OPTIONS, emotionColors, optionLabel } from '@/constants/storyboard'
import { useProjectStore } from '@/stores/project'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const toast = useToast()
const authStore = useAuthStore()
const projectStore = useProjectStore()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ==================== 剧本分集 ==================== */

/** 剧本分集视图（集号 + 标题） */
interface EpisodeOption {
  /** 剧集 ID */
  id: string | number
  /** 集号 */
  no: number
  /** 集标题 */
  title: string
}

/** 分集列表 */
const episodes = ref<EpisodeOption[]>([])
/** 分集加载中 */
const episodesLoading = ref(true)
/** 剧本加载失败：'no-script' 项目尚无剧本 / 其他为错误文案 */
const episodesError = ref('')

/**
 * 拉取剧本树提取分集列表（生成分镜的集号来源）。
 * @returns 无
 */
async function loadEpisodes(): Promise<void> {
  episodesLoading.value = true
  episodesError.value = ''
  try {
    const tree = await getProjectCurrentScript(projectId.value)
    const list = Array.isArray(tree.episodes) ? tree.episodes : []
    episodes.value = list.map((ep: EpisodeNode, index: number) => ({
      id: ep.id,
      no: Number(ep.episode_no ?? ep.index ?? index + 1),
      title: ep.title || `第 ${Number(ep.episode_no ?? ep.index ?? index + 1)} 集`,
    }))
  } catch (error) {
    episodes.value = []
    episodesError.value = error instanceof ApiError && error.code === 10002 ? 'no-script' : (error instanceof Error ? error.message : '剧本加载失败')
  } finally {
    episodesLoading.value = false
  }
}

/* ==================== 分镜板列表 / 详情 ==================== */

/** 项目分镜板列表（按集升序） */
const storyboards = ref<StoryboardSummary[]>([])
/** 列表加载中 */
const listLoading = ref(false)
/** 分镜列表加载失败（2026-11 QA 修复：此前失败被静默吞掉，页面只剩「0 镜」无提示） */
const listError = ref('')
/** 当前选中集号 */
const selectedEpisodeNo = ref(0)
/** 当前分镜板详情（含 shots） */
const currentStoryboard = ref<StoryboardDetail | null>(null)
/** 详情加载中 */
const detailLoading = ref(false)
/** 详情加载失败文案 */
const detailError = ref('')

/** 集号 → 分镜板摘要映射 */
const storyboardMap = computed(() => {
  const map = new Map<number, StoryboardSummary>()
  for (const sb of storyboards.value) {
    if (typeof sb.episode_no === 'number') map.set(sb.episode_no, sb)
  }
  return map
})

/** 当前集的分镜板摘要 */
const currentSummary = computed(() => storyboardMap.value.get(selectedEpisodeNo.value) ?? null)

/** 当前分镜板是否锁定 */
const isLocked = computed(() => currentStoryboard.value?.status === 'locked')

/** 镜头列表（按 order_index 升序） */
const shots = computed<ShotItem[]>(() => {
  const list = currentStoryboard.value?.shots ?? []
  return [...list].sort((a, b) => a.order_index - b.order_index)
})

/** 镜头总时长（毫秒） */
const totalDurationMs = computed(() => shots.value.reduce((sum, shot) => sum + shot.duration_ms, 0))

/** 本集预估渲染成本（预算感知粗估：总秒数 × 30 积分/秒的默认视频单价口径）。
 * 修复（E2E 报告 P-06）：真实计价按批量编排时所选视频模型的单价
 * （unit_price_credits，通常高于默认 30/秒），此处粗估必然偏低——文案
 * 已明确标注「粗估」与差异原因，避免与批量编排弹层的准确预估相矛盾。 */
const estimatedCredits = computed(() => Math.ceil((currentSummary.value?.estimated_duration_ms ?? 0) / 1000) * 30)

/* ==================== 列表模式按场分组（F4-01 场次组头） ==================== */

/** 场分组视图 */
interface SceneGroup {
  /** 分组键（场景资产 ID，空串=未绑定场景锚点） */
  key: string
  /** 组头文案（第 N 场 · 场景名） */
  name: string
  /** 组内镜头 */
  shots: ShotItem[]
  /** 组内总时长（毫秒） */
  durationMs: number
}

/** 资产名称映射（场景 → 列表模式组头；角色 → 夜窗锚点 chips；加载失败回退「场景 #ID」） */
const assetNameMap = ref<Map<string, string>>(new Map())

/**
 * 拉取项目场景与角色资产名称（列表模式组头 + 夜窗锚点 chips 的视觉辅助数据）。
 * @returns 无
 */
async function loadAssetNames(): Promise<void> {
  try {
    const [scenePage, characterPage] = await Promise.all([
      listAssets(projectId.value, { asset_type: 'scene', page_size: 100 }),
      listAssets(projectId.value, { asset_type: 'character', page_size: 100 }),
    ])
    const map = new Map<string, string>()
    for (const asset of [...scenePage.items, ...characterPage.items]) map.set(String(asset.id), asset.name)
    assetNameMap.value = map
  } catch {
    // 视觉辅助数据：失败回退 ID 展示
  }
}

/**
 * 按场景锚点对镜头做「连续段分组」（保持 order 顺序，同场景不相邻时各自成组）。
 * @returns 场分组列表
 */
const sceneGroups = computed<SceneGroup[]>(() => {
  const groups: SceneGroup[] = []
  const indexByKey = new Map<string, number>()
  for (const shot of shots.value) {
    const key = shot.scene_asset_id != null ? String(shot.scene_asset_id) : ''
    let index = indexByKey.get(key)
    if (index === undefined) {
      const sceneName = key ? assetNameMap.value.get(key) ?? `场景 #${key}` : '未绑定场景锚点'
      index = groups.length
      indexByKey.set(key, index)
      groups.push({ key, name: `第 ${groups.length + 1} 场 · ${sceneName}`, shots: [], durationMs: 0 })
    }
    const group = groups[index]
    group.shots.push(shot)
    group.durationMs += shot.duration_ms
  }
  return groups
})

/**
 * 拉取项目分镜板列表。
 * @returns 无
 */
async function loadStoryboards(): Promise<void> {
  listLoading.value = true
  listError.value = ''
  try {
    storyboards.value = await listStoryboards(projectId.value)
  } catch (error) {
    storyboards.value = []
    // 2026-11 QA 修复：不再静默吞掉错误（否则页面显示「0 镜」且无任何原因提示）
    listError.value = error instanceof Error ? error.message : '分镜列表加载失败'
  } finally {
    listLoading.value = false
  }
}

/**
 * 列表/详情加载失败后的手动重试。
 * @returns 无
 */
async function retryListLoad(): Promise<void> {
  await loadStoryboards()
  if (currentSummary.value) await loadCurrentDetail()
}

/**
 * 加载当前选中集的分镜板详情并初始化画布。
 * @returns 无
 */
async function loadCurrentDetail(): Promise<void> {
  const summary = currentSummary.value
  if (!summary) {
    currentStoryboard.value = null
    return
  }
  detailLoading.value = true
  detailError.value = ''
  try {
    currentStoryboard.value = await getStoryboardDetail(summary.id)
  } catch (error) {
    currentStoryboard.value = null
    detailError.value = error instanceof Error ? error.message : '分镜加载失败'
  } finally {
    detailLoading.value = false
  }
}

/**
 * 切换集号并加载对应分镜板。
 * @param no 集号
 * @returns 无
 */
function selectEpisode(no: number): void {
  if (no === selectedEpisodeNo.value) return
  pausePreview()
  playheadMs.value = 0
  selectedEpisodeNo.value = no
  activeShotId.value = null
  drawerOpen.value = false
  void loadCurrentDetail()
}

/* ==================== 卡片交互 / 重排 ==================== */

/** 当前选中镜头 ID（列表选中态 + 打开抽屉） */
const activeShotId = ref<string | number | null>(null)
/** 镜头抽屉显示状态 */
const drawerOpen = ref(false)
/** 单镜重出进行中（防重复触发 + 按钮 loading 态） */
const redrawing = ref(false)

/** 当前选中镜头对象 */
const activeShot = computed(() => shots.value.find((shot) => String(shot.id) === String(activeShotId.value)) ?? null)

/**
 * 点击镜头行：选中并打开 P13 抽屉。
 * @param shotId 镜头 ID
 * @returns 无
 */
function onCardSelect(shotId: number | string): void {
  activeShotId.value = shotId
  drawerOpen.value = true
}

/**
 * 将 reorder 返回的 order 重算结果应用到本地 shots（含总时长）。
 * @param orderList [{shot_id, order_index}]
 * @returns 无
 */
function applyReorderResult(orderList: Array<{ shot_id: number | string; order_index: number }>): void {
  const storyboard = currentStoryboard.value
  if (!storyboard) return
  const orderMap = new Map(orderList.map((item) => [String(item.shot_id), item.order_index]))
  storyboard.shots = storyboard.shots.map((shot) => ({
    ...shot,
    order_index: orderMap.get(String(shot.id)) ?? shot.order_index,
  }))
}

/* ==================== 生成分镜 ==================== */

/** 生成进度模式（SSE storyboard_generate.started → generated/failed） */
const generating = ref(false)

/** 上一次分镜生成失败记录（后端持久化在 project.config，刷新后仍可见） */
const lastFailure = ref<{ episode_no?: number; message: string; at?: string } | null>(null)
/** 用户已关闭的失败横幅指纹（按 集号+时间 记忆，避免重复打扰） */
const dismissedFailureKey = ref('')

/**
 * 读取后端持久化的「上一次分镜生成失败」记录（E2E 报告问题 1/4）。
 * SSE 断线或页面刷新后，`storyboard_generate.failed` 事件无法抵达前端，
 * 页面此前与「从未生成」完全无法区分；此处从项目详情读回失败原因。
 * @returns 无
 */
async function loadLastFailure(): Promise<void> {
  try {
    const project = await getProject(projectId.value)
    const raw = (project.config ?? {})['_last_storyboard_failure']
    if (raw && typeof raw === 'object' && typeof (raw as { message?: unknown }).message === 'string') {
      lastFailure.value = raw as { episode_no?: number; message: string; at?: string }
    } else {
      lastFailure.value = null
    }
  } catch {
    lastFailure.value = null
  }
}

/** 失败横幅指纹 */
const failureKey = computed(() => `${lastFailure.value?.episode_no ?? ''}-${lastFailure.value?.at ?? ''}`)

/**
 * 是否展示持久化的失败横幅：仅当失败集与当前所选集一致、且该集仍无分镜板、
 * 用户尚未关闭时展示（重新生成成功后后端会清除该标记）。
 */
const showFailureBanner = computed(
  () =>
    lastFailure.value !== null &&
    !episodeHasStoryboard.value &&
    dismissedFailureKey.value !== failureKey.value &&
    (!lastFailure.value.episode_no ||
      Number(lastFailure.value.episode_no) === Number(selectedEpisodeNo.value)),
)

/**
 * 关闭失败横幅（仅本地隐藏，不清后端记录）。
 * @returns 无
 */
function dismissFailureBanner(): void {
  dismissedFailureKey.value = failureKey.value
}

/**
 * 当前集是否已生成分镜。
 * @returns 是否已生成
 */
const episodeHasStoryboard = computed(() => currentSummary.value !== null)

/**
 * 发起分镜生成（POST generate）：202 受理进入生成模式；
 * 幂等命中（该集已有分镜板）直接刷新详情。
 * @returns 无
 */
async function handleGenerate(): Promise<void> {
  if (generating.value) return
  if (!selectedEpisodeNo.value) {
    toast.warning('请先选择要生成分镜的集')
    return
  }
  try {
    // P1-7：镜头数/节奏/单镜时长作为显式控制参数下发，不再固定默认值
    const result = await generateStoryboard(projectId.value, selectedEpisodeNo.value, {
      pacing_mode: pacingModeOf(pacingModel.value),
      max_shot_duration_ms: PACING_MAX_SHOT_MS[pacingModel.value],
      ...(targetShotCount.value ? { target_shot_count: targetShotCount.value } : {}),
    })
    if (result.idempotent) {
      toast.info(result.message || '该集已存在分镜板')
      await loadStoryboards()
      await loadCurrentDetail()
      return
    }
    generating.value = true
    // 修复（E2E 报告问题 3）：后端回执携带资产就绪度，未全部锁定时明确告知，
    // 而不是默默受理后产出缺少形象基准的镜头。
    const readiness = result.asset_readiness
    if (readiness && !readiness.ready) {
      toast.warning(
        `分镜生成已受理，但仍有 ${readiness.unlocked} / ${readiness.total} 项资产未锁定，生成镜头可能缺少形象基准`,
      )
    } else {
      toast.info(result.message || '分镜生成任务已受理，正在后台拆镜')
    }
  } catch {
    // 拦截器已 toast（10002 无剧本 / 集号不存在等）
  }
}

/**
 * 生成完成：刷新列表与详情。
 * @returns 无
 */
async function finishGeneration(): Promise<void> {
  generating.value = false
  await loadStoryboards()
  await loadCurrentDetail()
  // 成功后后端已清除失败标记，这里同步清空本地横幅
  lastFailure.value = null
}

/* ==================== 锁定分镜 ==================== */

/** 锁定确认弹窗 */
const lockConfirmOpen = ref(false)
/** 锁定请求中 */
const locking = ref(false)
/** 缺锚点镜头清单弹窗（60005） */
const missingAnchorsOpen = ref(false)
/** 缺锚点镜头清单 */
const missingShots = ref<Array<{ shot_id: number | string; shot_no: number; order_index: number }>>([])

/**
 * 确认锁定分镜（POST lock）：成功产生 HITL-3 审批单；
 * 60005 弹出缺锚点镜头清单引导回画布绑定。
 * @returns 无
 */
async function handleLock(): Promise<void> {
  const storyboard = currentStoryboard.value
  if (!storyboard || locking.value) return
  locking.value = true
  try {
    const result = await lockStoryboard(storyboard.id)
    lockConfirmOpen.value = false
    toast.success(result.message || '分镜已锁定，审批单已创建（HITL-3），请前往审批台确认')
    await loadStoryboards()
    await loadCurrentDetail()
  } catch (error) {
    lockConfirmOpen.value = false
    if (error instanceof ApiError && error.code === 60005) {
      const data = error.data as { missing_shots?: typeof missingShots.value } | undefined
      missingShots.value = data?.missing_shots ?? []
      missingAnchorsOpen.value = true
    }
    // 其余错误拦截器已 toast
  } finally {
    locking.value = false
  }
}

/**
 * 缺锚点清单 → 定位首个缺失镜头并打开抽屉补绑。
 * @returns 无
 */
function goFirstMissingShot(): void {
  const first = missingShots.value[0]
  missingAnchorsOpen.value = false
  if (!first) return
  activeShotId.value = first.shot_id
  drawerOpen.value = true
}

/* ==================== 抽屉联动 ==================== */

/**
 * 抽屉保存 / 绑定锚点后回写镜头数据。
 * @param updated 最新镜头字段
 * @returns 无
 */
function onShotUpdated(updated: ShotItem): void {
  const storyboard = currentStoryboard.value
  if (!storyboard) return
  storyboard.shots = storyboard.shots.map((shot) => (String(shot.id) === String(updated.id) ? { ...shot, ...updated } : shot))
}

/* ==================== 夜窗监图（对齐 stitch_prd/p4 草稿监看夜窗） ==================== */

/**
 * 镜头级时间码格式化（mm:ss.S，保留一位小数）。
 * @param ms 毫秒
 * @returns 镜头级时间码文案
 */
function timecode(ms: number): string {
  const totalTenths = Math.max(0, Math.round(ms / 100))
  const minutes = Math.floor(totalTenths / 600)
  const seconds = Math.floor((totalTenths % 600) / 10)
  const tenths = totalTenths % 10
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${tenths}`
}

/**
 * 集级总时长格式化（mm:ss）。
 * @param ms 毫秒
 * @returns 集级时长文案
 */
function episodeTimecode(ms: number): string {
  const totalSeconds = Math.floor(Math.max(0, ms) / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/**
 * 判断媒体地址是否可作为图片渲染（过滤空值与后端 .json 占位资源）。
 * @param src 媒体地址（可为空）
 * @returns 可渲染返回 true
 */
function isRenderableMedia(src?: string | null): boolean {
  if (!src) return false
  return !/\.json(\?|#|$)/i.test(src)
}

/**
 * 图片加载失败兜底：隐藏破损的 img，回退到同容器内的占位背景。
 * @param event 图片 error 事件
 * @returns 无
 */
function hideBrokenImage(event: Event): void {
  const img = event.target as HTMLImageElement | null
  if (img) img.style.display = 'none'
}

/** 镜头时间线条目 */
interface ShotTimelineEntry {
  /** 起始时间码（毫秒，全片轴） */
  startMs: number
  /** 结束时间码（毫秒） */
  endMs: number
}

/** 镜头 ID → 片轴时间线映射（刊头卡点钩子 / 夜窗 scrubber / 张力曲线爆点共用） */
const timelineMap = computed<Map<string, ShotTimelineEntry>>(() => {
  const map = new Map<string, ShotTimelineEntry>()
  let acc = 0
  for (const shot of shots.value) {
    const startMs = acc
    acc += shot.duration_ms
    map.set(String(shot.id), { startMs, endMs: acc })
  }
  return map
})

/** 全片总时长时间码（无镜头时 00:00） */
const totalTimecode = computed(() => episodeTimecode(totalDurationMs.value))

/** 当前集选项（刊头标题 / 副行） */
const currentEpisode = computed<EpisodeOption | null>(() => episodes.value.find((ep) => ep.no === selectedEpisodeNo.value) ?? null)

/** 夜窗焦点镜头：选中镜头优先，未选中时取第一个镜头（对应原型 SHOT_XX_FOCUS） */
const focusShot = computed<ShotItem | null>(() => activeShot.value ?? shots.value[0] ?? null)

/** 焦点镜头片轴时间线 */
const focusTimeline = computed<ShotTimelineEntry | null>(() => (focusShot.value ? timelineMap.value.get(String(focusShot.value.id)) ?? null : null))

/** 焦点镜头在当前序列中的下标（-1 = 无焦点；夜窗传输控制按钮可用态用） */
const focusIndex = computed(() => shots.value.findIndex((shot) => String(shot.id) === String(focusShot.value?.id)))

/** 焦点镜头情绪视觉色（9:16 预览缺图时的渐变占位，取值与画布占位同源） */
const focusEmotion = computed(() => emotionColors(focusShot.value?.emotion_tag))

/** 焦点镜头绑定角色资产名列表（夜窗锚点 chips） */
const focusCharacterNames = computed<string[]>(() => {
  const shot = focusShot.value
  if (!shot) return []
  return (shot.character_asset_ids ?? [])
    .map((id) => assetNameMap.value.get(String(id)))
    .filter((name): name is string => Boolean(name))
})

/** 焦点镜头场景锚点名（未绑定回退 null → 模板渲染引导 chip） */
const focusSceneName = computed<string | null>(() => {
  const shot = focusShot.value
  if (!shot?.scene_asset_id) return null
  return assetNameMap.value.get(String(shot.scene_asset_id)) ?? `场景 #${shot.scene_asset_id}`
})

/** 焦点镜头是否有任何已绑定锚点（夜窗 chips 空态判断） */
const focusHasAnchors = computed(() => Boolean(focusCharacterNames.value.length || focusSceneName.value || focusShot.value?.asset_anchor_ids?.length))

/** 焦点镜头锚点资产版本数（夜窗「锚点 ×N」chip） */
const focusAnchorCount = computed(() => focusShot.value?.asset_anchor_ids?.length ?? 0)

/* ==================== 分镜内联预览（草稿预览页下线后的播放能力） ==================== */

/** 分镜预览是否播放中 */
const previewing = ref(false)
/** 预览播放头（全片毫秒；播放推进时驱动镜头自动切换） */
const playheadMs = ref(0)
/** 预览播放定时器 */
let previewTimer: ReturnType<typeof setInterval> | null = null
/** 视频元素引用（镜头媒体为视频时使用） */
const previewVideoRef = ref<HTMLVideoElement | null>(null)
/** 视频无法加载 / 播放失败（回退关键帧与渐变占位） */
const videoBroken = ref(false)
/** 播放步进（毫秒；1.0x 固定） */
const PREVIEW_TICK_MS = 100

/** 当前镜头媒体是否为可播放视频（mp4/webm/ogg/mov） */
const focusVideoUrl = computed<string>(() => {
  const url = focusShot.value?.media_url
  if (!url) return ''
  return /\.(mp4|webm|ogg|mov|m4v)(\?|#|$)/i.test(url) ? url : ''
})

/** 是否以视频形态渲染监看画面（视频源存在且未损坏） */
const showPreviewVideo = computed(() => Boolean(focusVideoUrl.value) && !videoBroken.value)

/** 当前镜头时长（毫秒，下限 200 避免除零） */
const focusSpanMs = computed(() => {
  const entry = focusTimeline.value
  if (!entry) return 0
  return Math.max(200, entry.endMs - entry.startMs)
})

/** 当前镜头内已播毫秒（播放头相对镜头起点） */
const focusPlayedMs = computed(() => {
  const entry = focusTimeline.value
  if (!entry) return 0
  return Math.min(focusSpanMs.value, Math.max(0, playheadMs.value - entry.startMs))
})

/** 播放头在当前镜头内的百分比（0~100，镜头段内播放头位置） */
const playPercent = computed(() => (focusSpanMs.value > 0 ? Math.min(100, (focusPlayedMs.value / focusSpanMs.value) * 100) : 0))

/**
 * 按全片毫秒定位镜头（播放跨镜切换用）。
 * @param ms 全片毫秒
 * @returns 命中的镜头；超出尾部返回最后一镜
 */
function shotAtPlayhead(ms: number): ShotItem | null {
  for (const shot of shots.value) {
    const entry = timelineMap.value.get(String(shot.id))
    if (entry && ms >= entry.startMs && ms < entry.endMs) return shot
  }
  return shots.value[shots.value.length - 1] ?? null
}

/**
 * 同步视频播放状态（无视频元素时静默跳过）。
 * @param shouldPlay true 播放 / false 暂停
 * @returns 无
 */
async function syncVideoPlayback(shouldPlay: boolean): Promise<void> {
  const video = previewVideoRef.value
  if (!video) return
  try {
    if (shouldPlay) {
      await video.play()
    } else {
      video.pause()
    }
  } catch {
    // 自动播放被拦截 / 编码不支持：回退关键帧预览并提示，不阻塞静帧播放引擎
    videoBroken.value = true
    toast.warning('该镜头视频暂时无法播放，已切换为关键帧预览')
  }
}

/**
 * 播放 tick：推进播放头、跨镜自动切焦、循环或播完停止。
 * @returns 无
 */
function tickPreview(): void {
  const total = totalDurationMs.value
  if (total <= 0 || shots.value.length === 0) {
    pausePreview()
    return
  }
  playheadMs.value += PREVIEW_TICK_MS
  const hit = shotAtPlayhead(playheadMs.value)
  if (hit && String(hit.id) !== String(focusShot.value?.id)) activeShotId.value = hit.id
  if (playheadMs.value >= total) {
    if (loopArmed.value) {
      playheadMs.value = 0
      activeShotId.value = shots.value[0]?.id ?? null
    } else {
      playheadMs.value = total
      pausePreview()
    }
  }
}

/**
 * 开始 / 继续播放分镜预览（点击监看画面或传输条播放键）。
 * @returns 无
 */
async function startPreview(): Promise<void> {
  if (previewing.value) return
  if (shots.value.length === 0) {
    toast.warning('本集暂无镜头，无法预览播放')
    return
  }
  if (totalDurationMs.value <= 0) {
    toast.warning('镜头时长数据异常，暂不能播放预览')
    return
  }
  const entry = focusTimeline.value
  if (!entry) {
    toast.warning('未定位到可播放的镜头，请先选择镜头')
    return
  }
  // 播放头不在当前镜头区间时，回到镜头起点
  if (playheadMs.value < entry.startMs || playheadMs.value >= entry.endMs) playheadMs.value = entry.startMs
  previewing.value = true
  previewTimer = setInterval(tickPreview, PREVIEW_TICK_MS)
  await syncVideoPlayback(true)
}

/**
 * 暂停播放并清理定时器。
 * @returns 无
 */
function pausePreview(): void {
  previewing.value = false
  if (previewTimer) {
    clearInterval(previewTimer)
    previewTimer = null
  }
  void syncVideoPlayback(false)
}

/**
 * 播放 / 暂停切换（按钮与画面点击共用）。
 * @returns 无
 */
function togglePreview(): void {
  if (previewing.value) pausePreview()
  else void startPreview()
}

/**
 * 视频加载 / 播放异常兜底：回退关键帧占位并提示。
 * @returns 无
 */
function onPreviewVideoError(): void {
  videoBroken.value = true
  toast.warning('镜头视频加载失败，已回退关键帧占位预览')
}

// 切换镜头时重置视频损坏态；播放中切镜后继续播放新媒体
watch(
  () => focusShot.value?.id,
  () => {
    videoBroken.value = false
    if (previewing.value) void nextTick(() => syncVideoPlayback(true))
  },
)

onBeforeUnmount(pausePreview)

/** 结尾钩子镜头（顺序最后一位，与 reorder 的 hook 语义一致） */
const hookShot = computed<ShotItem | null>(() => (shots.value.length > 0 ? shots.value[shots.value.length - 1] : null))

/** 结尾钩子起始时间码（刊头「卡点钩子」与张力曲线爆点标注） */
const hookStartMs = computed(() => (hookShot.value ? timelineMap.value.get(String(hookShot.value.id))?.startMs ?? 0 : 0))

/** 景别档位跳变镜头 ID 集（与前一镜档位差 ≥2；对应原型「景别跳跃待核对」黄警示） */
const jumpWarningIds = computed<Set<string>>(() => {
  const result = new Set<string>()
  let prevIndex: number | null = null
  for (const shot of shots.value) {
    const index = SHOT_SIZE_OPTIONS.findIndex((item) => item.key === shot.shot_size)
    if (index >= 0) {
      if (prevIndex !== null && Math.abs(index - prevIndex) >= 2) result.add(String(shot.id))
      prevIndex = index
    }
  }
  return result
})

/** 首个景别跳变镜头（审批卡确认 checkbox 文案用） */
const firstJumpShot = computed<ShotItem | null>(() => shots.value.find((shot) => jumpWarningIds.value.has(String(shot.id))) ?? null)

/** 景别跳变确认勾选（HITL 结构件：仅本地确认态，不落库） */
const jumpAck = ref(true)

/** 待确认镜头数（pending_review 计数：卡片序列徽标 / 审批卡质检徽标） */
const pendingShotCount = computed(() => shots.value.filter((shot) => shot.status === 'pending_review').length)

/** 单镜平均预估积分（审批卡统计行） */
const creditsPerShot = computed(() => (shots.value.length > 0 ? Math.max(1, Math.round(estimatedCredits.value / shots.value.length)) : 0))

/**
 * 编导节奏模型（P1-7：现在真正参与生成参数，映射到后端 pacing_mode）。
 * fast → fast / balanced → balanced / suspense → slow。
 */
const pacingModel = ref<'fast' | 'balanced' | 'suspense'>('fast')

/** 节奏模型三档选项（对齐原型 segmented） */
const PACING_MODELS = [
  { key: 'fast', label: '快节奏爽剧 (2.4s/镜)' },
  { key: 'balanced', label: '均衡叙事 (3.2s/镜)' },
  { key: 'suspense', label: '悬疑压抑 (4.0s/镜)' },
] as const

/**
 * 节奏档位 → 后端 pacing_mode 映射。
 * @param model 前端节奏档位
 * @returns 后端枚举值
 */
function pacingModeOf(model: 'fast' | 'balanced' | 'suspense'): 'fast' | 'balanced' | 'slow' {
  return model === 'suspense' ? 'slow' : model
}

/** 节奏档位 → 单镜时长上限（毫秒），用于把节奏选择落到实际镜头长度 */
const PACING_MAX_SHOT_MS: Record<'fast' | 'balanced' | 'suspense', number> = {
  fast: 4000,
  balanced: 5000,
  suspense: 6000,
}

/** 目标镜头数（空表示交给导演自动决定 6~12 镜） */
const targetShotCount = ref<number | null>(null)

/** AB 循环标记（结构件视觉态：草稿播放引擎未接入） */
const loopArmed = ref(false)

/**
 * 夜窗传输控制：切换到相邻镜头（仅更新选中，不弹抽屉）。
 * @param delta -1 上一镜 / 1 下一镜
 * @returns 无
 */
function stepFocus(delta: -1 | 1): void {
  const target = shots.value[focusIndex.value + delta]
  if (!target) return
  activeShotId.value = target.id
  // 播放中切镜：播放头同步到目标镜头起点，避免下一 tick 又被拉回
  if (previewing.value) {
    const entry = timelineMap.value.get(String(target.id))
    if (entry) playheadMs.value = entry.startMs
  }
}

/**
 * 打开焦点镜头抽屉（「优化运镜词」与本集审批共用入口：
 * 关键帧 prompt 与运镜词均在 P13 抽屉编辑；重出画面走 redrawFocusShot）。
 * @returns 无
 */
function openFocusDrawer(): void {
  if (!focusShot.value) return
  activeShotId.value = focusShot.value.id
  drawerOpen.value = true
}

/* ==================== 单镜重出（首帧整幅要素画面，0 积分） ==================== */

/** 单镜重出是否可用：有焦点镜头、分镜未锁定、且没有正在进行的重出 */
const canRedrawShot = computed(() => Boolean(focusShot.value) && !isLocked.value && !redrawing.value)

/** 单镜重出按钮的提示文案（按不可用原因给出可操作指引） */
const redrawHint = computed<string>(() => {
  if (!focusShot.value) return '请先在镜头序列中选择一个镜头'
  if (isLocked.value) return '分镜已锁定：请先在镜头抽屉驳回审批（反馈 ≥10 字）解锁后重出'
  if (redrawing.value) return '正在重出当前镜首帧，请稍候…'
  return '按当前锚点与构图重出本镜整幅要素画面（本地合成，0 积分）'
})

/**
 * 重出当前焦点镜头的首帧画面。
 *
 * 交互约定：
 * - 未选择镜头 / 分镜已锁定 / 已有重出在跑时直接返回并提示，避免重复触发；
 * - 成功后先用返回值就地更新该镜 media_url（避免整页闪烁），再拉取详情兜底同步；
 * - 失败时保留旧画面，错误提示由请求拦截器统一给出。
 * @returns 无
 */
async function redrawFocusShot(): Promise<void> {
  if (redrawing.value) return
  const shot = focusShot.value
  if (!shot) {
    toast.warning('请先在镜头序列中选择一个镜头')
    return
  }
  if (isLocked.value) {
    toast.warning('分镜已锁定：请先在镜头抽屉驳回审批解锁后再重出画面')
    return
  }
  redrawing.value = true
  pausePreview()
  try {
    const result = await redrawShotFrame(shot.id, undefined, 'compose')
    // 就地更新，让监看立刻切到新首帧（后端 _shot_media_url 首帧优先）
    const target = currentStoryboard.value?.shots?.find((item) => String(item.id) === String(shot.id))
    if (target && result.media_url) {
      target.media_url = result.media_url
    }
    // 新图可能与此前损坏的视频源无关，重置损坏标记让监看重新加载
    videoBroken.value = false
    const degraded = result.degraded?.length ? `（降级：${result.degraded.join('、')}）` : ''
    toast.success(`镜 ${shot.shot_no} 整幅画面已重出${degraded}`)
    await loadCurrentDetail()
  } catch {
    // 请求拦截器已统一 toast 错误信息；这里只负责保留旧画面
  } finally {
    redrawing.value = false
  }
}

/**
 * 本集审批决策回调（抽屉内 approve / reject 后执行）：
 * - approve：刷新分镜状态并跳转生成任务中心发起批量渲染；
 * - reject：分镜已解锁，刷新画布回到可编辑态。
 * @param action 决策动作
 * @returns 无
 */
async function onStoryboardDecided(action: 'approve' | 'reject'): Promise<void> {
  if (action === 'approve') {
    drawerOpen.value = false
    await router.push({ path: `/projects/${projectId.value}/render-tasks`, query: { start: '1' } })
    return
  }
  await loadStoryboards()
  await loadCurrentDetail()
}

/**
 * 抽屉内点击镜头明细：切换当前镜头并保持抽屉打开。
 * @param shotId 目标镜头 ID
 * @returns 无
 */
function onDrawerSelectShot(shotId: number | string): void {
  activeShotId.value = shotId
  drawerOpen.value = true
}

/**
 * 列表模式内上移 / 下移镜头（调同一 reorder API）。
 * @param index 当前索引
 * @param direction -1 上移 / 1 下移
 * @returns 无
 */
async function moveShot(index: number, direction: -1 | 1): Promise<void> {
  const list = shots.value
  const target = index + direction
  if (isLocked.value || target < 0 || target >= list.length) return
  const moving = list[index]
  const anchor = direction === -1 ? { insert_before_shot_id: list[target].id } : { insert_after_shot_id: list[target].id }
  const storyboardId = currentStoryboard.value?.id
  if (!storyboardId) return
  try {
    const result = await reorderShots(storyboardId, moving.id, anchor)
    applyReorderResult(result.shots)
    if (result.hook_position_changed) toast.info('镜头顺序已更新：结尾钩子镜头发生位移')
    else toast.success('镜头顺序已更新')
  } catch {
    // 拦截器已 toast
  }
}

/* ==================== SSE 订阅 / 轮询兜底 ==================== */

/**
 * 全量刷新：列表 + 当前详情（SSE 降级轮询与部分事件共用）。
 * @returns 无
 */
async function refreshAll(): Promise<void> {
  await loadStoryboards()
  if (currentSummary.value) await loadCurrentDetail()
}

// 项目级事件流：分镜生成进度 / 生成完成 / 失败 / 任务更新 / 审批决策联动
// （D23/D24 修复：EventSource 无法带 header，token 经 ?access_token= 传递）
const sseUrl = computed(() => {
  const token = authStore.token
  // 首屏 token 尚未恢复时不建立连接，避免带空 access_token 触发 401
  return token ? `/api/v1/projects/${projectId.value}/events?access_token=${token}` : ''
})
useSSE(sseUrl, {
  eventTypes: ['storyboard_generate.started', 'storyboard.generated', 'storyboard_generate.failed', 'render.task.updated', 'approval.decided'],
  onMessage: (event) => {
    let data: Record<string, unknown> = {}
    try {
      data = JSON.parse(event.data) as Record<string, unknown>
    } catch {
      data = {}
    }
    if (event.type === 'storyboard_generate.started') {
      generating.value = true
      return
    }
    if (event.type === 'storyboard.generated') {
      const storyboardId = data.storyboard_id !== undefined ? String(data.storyboard_id) : ''
      if (!storyboardId || String(currentSummary.value?.id ?? '') === storyboardId || !episodeHasStoryboard.value) {
        void finishGeneration()
      } else {
        generating.value = false
        void loadStoryboards()
      }
      toast.success('分镜生成完成')
      return
    }
    if (event.type === 'storyboard_generate.failed') {
      generating.value = false
      // 修复（E2E 报告问题 1）：事件可能晚于页面刷新，这里同时读取后端
      // 持久化的失败原因，保证横幅与 toast 都带具体错误信息。
      const message = String(data.message ?? '分镜生成失败，请重试')
      toast.error(message)
      void loadLastFailure()
      return
    }
    if (event.type === 'render.task.updated' && !generating.value) {
      void refreshAll()
      return
    }
    if (event.type === 'approval.decided') {
      void refreshAll()
    }
  },
  onPoll: () => {
    // D24 修复：SSE 不可用降级轮询时，生成中也要检测分镜板是否已产出，
    // 否则画布停留在「0 镜」空态直到手动刷新
    if (generating.value) {
      void pollGenerationResult()
      return
    }
    void refreshAll()
  },
})

/**
 * 生成中轮询兜底：检测当前集是否已产出分镜板，出现即结束生成态并刷新详情；
 * 配合后端失败自动回滚，项目状态离开 storyboarding 且仍无分镜板时判定失败。
 * @returns 无
 */
async function pollGenerationResult(): Promise<void> {
  if (!selectedEpisodeNo.value) return
  try {
    const boards = await listStoryboards(projectId.value)
    const hit = boards.find((b) => Number(b.episode_no) === Number(selectedEpisodeNo.value))
    if (hit) {
      generating.value = false
      await loadStoryboards()
      await loadCurrentDetail()
      toast.success('分镜生成完成')
      return
    }
    // P4-b 修复：SSE 不可用（降级轮询）时 storyboard_generate.failed 事件无法
    // 送达前端，这里以项目状态兜底判定失败——后端失败已自动把状态回滚出
    // storyboarding，若此时该集仍无分镜板，说明本轮生成已终止。
    const project = await getProject(projectId.value)
    if (project.status !== 'storyboarding') {
      generating.value = false
      // 修复（E2E 报告问题 1/4）：同步读取后端持久化的失败原因，让兜底
      // 提示也能给出具体错误，而不是笼统的「请重试」。
      await loadLastFailure()
      toast.error(lastFailure.value?.message ?? '分镜生成失败，请重试')
    }
  } catch {
    // 轮询失败静默（下个周期重试）
  }
}

/* ==================== 深链兼容（/storyboard/shots/:shotId → 抽屉） ==================== */

/**
 * 处理 ?shot= 深链：在全部分镜板中定位镜头并打开抽屉。
 * @returns 无
 */
async function resolveQueryShot(): Promise<void> {
  const shotId = route.query.shot
  if (!shotId || typeof shotId !== 'string') return
  for (const sb of storyboards.value) {
    try {
      const detail = await getStoryboardDetail(sb.id)
      const found = (detail.shots ?? []).find((shot) => String(shot.id) === shotId)
      if (found) {
        selectedEpisodeNo.value = sb.episode_no ?? selectedEpisodeNo.value
        currentStoryboard.value = detail
        activeShotId.value = found.id
        drawerOpen.value = true
        return
      }
    } catch {
      // 单个分镜板加载失败继续查找下一个
    }
  }
  toast.warning('未找到指定镜头，可能已被删除')
}

/* ==================== 生命周期 ==================== */

/** 首屏数据装配中（P4-e 修复：装配期间展示骨架屏，替代暗色空画布的「黑屏」观感） */
const booting = ref(true)

onMounted(async () => {
  try {
    await Promise.all([loadEpisodes(), loadStoryboards(), loadAssetNames(), loadLastFailure()])
    // 默认选中：第一个有分镜板的集，否则第一集
    const firstWithStoryboard = storyboards.value.find((sb) => typeof sb.episode_no === 'number')
    selectedEpisodeNo.value =
      firstWithStoryboard && typeof firstWithStoryboard.episode_no === 'number'
        ? firstWithStoryboard.episode_no
        : (episodes.value[0]?.no ?? 0)
    if (currentSummary.value) await loadCurrentDetail()

    // 深链：?shot= 直接定位镜头抽屉
    if (route.query.shot) await resolveQueryShot()
  } finally {
    // 骨架屏退出（至少保留一帧，避免闪烁）
    booting.value = false
  }
})
// 分集加载完成后若尚未选中集号则补默认值
watch(episodes, (list) => {
  if (selectedEpisodeNo.value === 0 && list.length > 0) {
    const firstWithStoryboard = storyboards.value.find((sb) => typeof sb.episode_no === 'number')
    selectedEpisodeNo.value =
      firstWithStoryboard && typeof firstWithStoryboard.episode_no === 'number' ? firstWithStoryboard.episode_no : list[0].no
  }
})
</script>

<template>
  <div class="flex h-full flex-col overflow-y-auto">
    <!-- ============ ① 刊头（单行：集名 / 状态 / 项目与卡点；右侧集选择 + 连播主 CTA） ============ -->
    <header class="shrink-0 px-5 pt-4">
      <div class="mx-auto flex w-full max-w-[1400px] flex-wrap items-end justify-between gap-3">
        <div class="flex min-w-0 items-center gap-3">
          <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2">
              <h1 class="gd-masthead truncate text-display-lg">
                第 {{ selectedEpisodeNo }} 集{{ currentEpisode?.title ? ` · ${currentEpisode.title}` : '' }}
              </h1>
              <span v-if="currentStoryboard" class="gd-badge">
                <span class="gd-num">{{ shots.length }} 镜 · {{ totalTimecode }}</span>
              </span>
              <StatusTag v-if="currentStoryboard" :status="currentStoryboard.status" size="sm" />
            </div>
            <p class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-caption text-ink-mute">
              <span class="max-w-[260px] truncate">{{ projectStore.currentProject?.title ?? '—' }}</span>
              <span aria-hidden="true">·</span>
              <span v-if="hookShot" class="gd-num text-primary-700">卡点钩子 {{ timecode(hookStartMs) }}（第 {{ hookShot.shot_no }} 镜）</span>
              <span v-else>卡点钩子待分镜生成后标注</span>
            </p>
          </div>
        </div>

        <!-- 集选择 + 连播（工具行已下沉到画布卡，刊头只保留上下文与主 CTA） -->
        <div class="flex flex-wrap items-center gap-2">
          <select
            :value="selectedEpisodeNo"
            class="gd-input gd-field-sm max-w-[200px]"
            aria-label="选择分集"
            @change="selectEpisode(Number(($event.target as HTMLSelectElement).value))"
          >
            <option v-for="ep in episodes" :key="ep.id" :value="ep.no">
              第 {{ ep.no }} 集{{ storyboardMap.get(ep.no) ? ` · ${storyboardMap.get(ep.no)!.shot_count} 镜` : ' · 未生成' }}
            </option>
            <option v-if="episodes.length === 0" :value="0">暂无剧本分集</option>
          </select>
          <!-- P4-a 修复：空态不再禁用（禁用点击零反馈），由 startPreview 守卫给出明确提示 -->
          <AppButton variant="primary" size="sm" @click="togglePreview">
            <svg v-if="!previewing" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M8 5.14v13.72c0 .8.87 1.3 1.56.9l11-6.86a1.05 1.05 0 000-1.8l-11-6.86A1.05 1.05 0 008 5.14z" />
            </svg>
            <svg v-else viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M7 5h4v14H7zM13 5h4v14h-4z" />
            </svg>
            {{ previewing ? '暂停预览' : '播放本集预览' }}
          </AppButton>
        </div>
      </div>
    </header>

    <!-- ============ decomposing 生成进度条（F4-02：拆解中 chip + 流动进度） ============ -->
    <div v-if="generating" class="mx-auto mt-3 w-full max-w-[1400px] flex shrink-0 items-center gap-3 rounded-xl border border-hairline bg-surface-raised px-4 py-2.5 text-xs text-info" role="status">
      <span class="inline-flex items-center gap-1.5 rounded-full bg-info-bg px-2.5 py-0.5 font-medium text-info">
        <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-info" aria-hidden="true" />
        decomposing · 第 {{ selectedEpisodeNo }} 集拆解中
      </span>
      <div class="relative h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-inset">
        <div class="absolute inset-y-0 w-1/3 animate-pulse rounded-full bg-primary-500" style="animation-duration: 1.6s" aria-hidden="true" />
      </div>
      <span class="shrink-0 text-neutral-400">AI 导演正在按剧本拆镜，完成后画布将自动刷新；你可以离开此页稍后回来</span>
    </div>

    <!-- ============ 分镜列表加载失败（2026-11 QA 修复：失败不再静默为「0 镜」） ============ -->
    <div
      v-if="listError"
      class="mx-auto mt-3 flex w-full max-w-[1400px] flex-wrap items-center gap-3 rounded-xl border border-danger/40 bg-danger-bg px-4 py-2.5 text-xs text-danger"
      role="alert"
    >
      <span class="font-medium">分镜列表加载失败：{{ listError }}</span>
      <AppButton variant="secondary" size="sm" @click="retryListLoad">重试</AppButton>
    </div>

    <!-- ============ 上次生成失败横幅（后端持久化，刷新后仍可见） ============ -->
    <div
      v-if="showFailureBanner"
      class="mx-auto mt-3 flex w-full max-w-[1400px] flex-wrap items-center gap-3 rounded-xl border border-danger/40 bg-danger-bg px-4 py-2.5 text-xs text-danger"
      role="alert"
    >
      <span class="font-medium">上次分镜生成失败{{ lastFailure?.episode_no ? `（第 ${lastFailure.episode_no} 集）` : '' }}</span>
      <span class="min-w-0 flex-1 truncate" :title="lastFailure?.message">{{ lastFailure?.message }}</span>
      <AppButton variant="primary" size="sm" @click="handleGenerate">重新生成</AppButton>
      <AppButton variant="ghost" size="sm" @click="dismissFailureBanner">知道了</AppButton>
    </div>

    <!-- ============ HITL-3 待审批提示条（分镜锁定 + 项目处于关口三等待态） ============ -->
    <div
      v-if="isLocked && projectStore.currentProject?.status === 'storyboard_review'"
      class="mx-auto mt-3 w-full max-w-[1400px] flex shrink-0 items-center justify-center gap-2 rounded-xl border border-hairline bg-surface-raised px-4 py-2 text-xs text-warning-bright"
      role="status"
    >
      <span>分镜已锁定，等待你的确认（HITL-3）——通过后将进入视频生成，驳回则解锁回画布</span>
      <AppButton variant="ghost" size="sm" @click="openFocusDrawer">打开镜头抽屉审批 →</AppButton>
    </div>

    <!-- ============ 锁定只读黄条（锁定且非待审批态：只读 + 解锁引导） ============ -->
    <div
      v-else-if="isLocked"
      class="mx-auto mt-3 w-full max-w-[1400px] flex shrink-0 items-center justify-center gap-2 rounded-xl border border-hairline bg-surface-raised px-4 py-2 text-xs text-warning-bright"
      role="status"
    >
      <span>🔒 分镜已锁定，镜头卡片与参数均只读；如需修改，请在镜头抽屉中驳回审批（反馈 ≥10 字）解锁回画布</span>
      <AppButton variant="ghost" size="sm" @click="openFocusDrawer">打开镜头抽屉审批 →</AppButton>
    </div>

    <!-- ============ ② 主体工作区：左镜头序列（窄栏）+ 右镜头监看（主区） ============ -->
    <!-- P4-e 修复：首屏装配期间展示骨架屏（原为暗色空画布，观感为 3.5s+ 黑屏） -->
    <section v-if="booting" class="mx-auto w-full max-w-[1400px] px-4 pt-4" aria-busy="true" aria-label="分镜加载中">
      <div class="grid grid-cols-1 gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
        <div class="gd-night-window gd-night-window-sm order-2 self-start p-4">
          <LoadingSkeleton variant="table" :rows="6" />
        </div>
        <div class="order-1 space-y-3">
          <LoadingSkeleton variant="card" :rows="1" />
          <LoadingSkeleton variant="table" :rows="9" />
        </div>
      </div>
    </section>
    <section v-else class="mx-auto w-full max-w-[1400px] px-4 pt-4">
      <div class="grid grid-cols-1 gap-4 xl:grid-cols-[360px_minmax(0,1fr)]">
        <!-- 右栏：镜头监看夜窗（9:16 小监看 + 实时导演解算 + 传输） -->
        <section class="gd-night-window gd-night-window-sm order-2 self-start p-4" aria-label="草稿实时监看">
          <div class="relative z-10 flex flex-col gap-3">
            <!-- 头部缎带 -->
            <div class="flex flex-wrap items-center justify-between gap-2">
              <span class="gd-num shrink-0 rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-micro text-primary-300">
                DRAFT_STAGE · SH-{{ String(focusShot?.shot_no ?? 0).padStart(2, '0') }}
              </span>
              <button
                type="button"
                class="gd-num inline-flex items-center gap-1 rounded-md bg-white/10 px-2 py-1 text-micro text-white transition-colors hover:bg-white/20 disabled:cursor-not-allowed disabled:opacity-40"
                :disabled="!canRedrawShot"
                :aria-busy="redrawing"
                :title="redrawHint"
                @click="redrawFocusShot"
              >
                <svg
                  class="h-3.5 w-3.5"
                  :class="{ 'animate-spin': redrawing }"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.8"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                  aria-hidden="true"
                >
                  <path d="M20 11a8 8 0 10.6 4M20 5v6h-6" />
                </svg>
                {{ redrawing ? '重出中…' : '单镜重出' }}
              </button>
            </div>

            <!-- 监看 + 解算：宽屏左右并排（监看为主视觉，按视口高度收敛），窄屏纵向堆叠 -->
            <div class="flex flex-col gap-5 xl:flex-row xl:items-center xl:justify-center xl:gap-7">
            <!-- 9:16 竖屏监看（大版面：高度随视口收敛，宽高比锁定） -->
            <figure
              class="group relative mx-auto flex aspect-[9/16] h-[min(60vh,520px)] w-auto shrink-0 cursor-pointer flex-col justify-between overflow-hidden rounded-xl border border-white/10 bg-player-bg shadow-gd-bloom xl:mx-0"
              role="button"
              tabindex="0"
              :aria-label="previewing ? '暂停分镜预览' : '播放分镜预览'"
              @click="togglePreview"
              @keydown.enter.prevent="togglePreview"
              @keydown.space.prevent="togglePreview"
            >
              <!-- 镜头媒体：视频优先，其次关键帧，最后情绪渐变占位 -->
              <video
                v-if="showPreviewVideo"
                ref="previewVideoRef"
                :src="focusVideoUrl"
                class="absolute inset-0 h-full w-full object-cover"
                playsinline
                muted
                loop
                :aria-label="`SH-${String(focusShot?.shot_no ?? 0).padStart(2, '0')} 分镜视频`"
                @error="onPreviewVideoError"
              />
              <img
                v-else-if="isRenderableMedia(focusShot?.media_url)"
                :src="focusShot!.media_url!"
                :alt="`SH-${String(focusShot?.shot_no ?? 0).padStart(2, '0')} 关键帧`"
                class="absolute inset-0 h-full w-full object-cover"
                @error="hideBrokenImage"
              />
              <div v-else class="absolute inset-0" :style="{ background: `linear-gradient(160deg, ${focusEmotion.from} 0%, ${focusEmotion.to} 100%)` }" aria-hidden="true" />

              <!-- 未播放时的中央播放提示（点击画面即播放） -->
              <span
                v-if="!previewing && focusShot"
                class="pointer-events-none absolute inset-0 z-10 flex items-center justify-center"
                aria-hidden="true"
              >
                <span class="flex h-12 w-12 items-center justify-center rounded-full border border-white/25 bg-black/45 text-white shadow-gd-bloom backdrop-blur-md transition-transform duration-300 group-hover:scale-110">
                  <svg class="ml-0.5 h-5 w-5" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5.14v13.72c0 .8.87 1.3 1.56.9l11-6.86a1.05 1.05 0 000-1.8l-11-6.86A1.05 1.05 0 008 5.14z" /></svg>
                </span>
              </span>
              <div class="absolute inset-0 bg-gradient-to-t from-scrim-strong via-scrim-faint to-scrim-soft" aria-hidden="true" />
              <div class="gd-num relative flex items-center justify-between px-2.5 pt-2 text-[10px] text-white/80">
                <span class="flex items-center gap-1">
                  <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-danger" aria-hidden="true" />
                  <span>LIVE</span>
                </span>
                <span class="rounded bg-black/60 px-1.5 py-0.5 font-semibold">9:16</span>
              </div>
              <div class="relative p-2.5">
                <div class="rounded-lg border border-white/10 bg-black/70 px-2 py-1.5 text-center shadow-gd-1 backdrop-blur-md">
                  <p class="text-[11px] font-semibold leading-tight tracking-wide text-white">
                    {{ focusShot?.dialogue || focusShot?.action_desc || '（本镜暂无台词）' }}
                  </p>
                  <span class="gd-num mt-0.5 block text-[10px] text-white/60">
                    {{ optionLabel(EMOTION_TAG_OPTIONS, focusShot?.emotion_tag) || '—' }} · {{ ((focusShot?.duration_ms ?? 0) / 1000).toFixed(1) }}s
                  </span>
                  <span v-if="focusShot && !focusVideoUrl" class="mt-0.5 block text-[10px] text-white/45">
                    分镜阶段仅关键帧预览，暂无镜头视频
                  </span>
                </div>
              </div>
            </figure>

            <!-- 镜头解算信息（顺序：标题 → 参数 → 锚点 → 时码与传输） -->
            <div class="flex min-w-0 flex-1 flex-col gap-3 xl:max-w-[520px]">
              <h2 class="min-w-0 font-serif text-title leading-snug text-white">
                {{ focusShot?.action_desc || focusShot?.dialogue || '等待镜头聚焦' }}
              </h2>
              <div class="gd-num flex flex-wrap items-center gap-1.5 text-micro text-neutral-400">
                <span class="rounded bg-white/10 px-2 py-0.5">景别 {{ optionLabel(SHOT_SIZE_OPTIONS, focusShot?.shot_size) }}</span>
                <span class="rounded bg-white/10 px-2 py-0.5">运镜 {{ optionLabel(CAMERA_MOVE_OPTIONS, focusShot?.camera_move) }}</span>
                <span class="rounded bg-white/10 px-2 py-0.5">{{ ((focusShot?.duration_ms ?? 0) / 1000).toFixed(1) }}s</span>
                <span class="rounded bg-white/10 px-2 py-0.5">
                  {{ focusTimeline ? timecode(focusTimeline.startMs) : '00:00.0' }}–{{ focusTimeline ? timecode(focusTimeline.endMs) : '00:00.0' }}
                </span>
              </div>

              <!-- 已绑定角色资产与道具锚点 -->
              <div class="flex flex-col gap-1.5">
                <span class="text-kicker uppercase tracking-wide text-neutral-400">已绑定锚点</span>
                <div class="flex flex-wrap gap-1.5">
                  <span
                    v-for="name in focusCharacterNames"
                    :key="name"
                    class="inline-flex items-center rounded-full border border-white/15 bg-white/10 px-2 py-0.5 text-micro text-white"
                  >
                    @{{ name }}
                  </span>
                  <span v-if="focusSceneName" class="inline-flex items-center rounded-full border border-white/15 bg-white/10 px-2 py-0.5 text-micro text-white">
                    @{{ focusSceneName }}（场景）
                  </span>
                  <span v-if="focusAnchorCount > 0" class="gd-num inline-flex items-center rounded-full border border-white/15 bg-white/10 px-2 py-0.5 text-micro text-white">
                    锚点 ×{{ focusAnchorCount }}
                  </span>
                  <span v-if="!focusHasAnchors" class="rounded-full border border-dashed border-white/20 px-2 py-0.5 text-micro text-neutral-400">
                    {{ focusShot ? '未绑定 · 点击镜头卡补绑' : '等待镜头聚焦' }}
                  </span>
                </div>
              </div>

              <!-- 时码 scrubber + 传输控制 -->
              <div class="flex flex-col gap-2.5 rounded-lg border border-white/10 bg-white/5 p-2.5">
                <div class="gd-num flex items-center justify-between gap-2 text-micro text-neutral-400">
                  <span>{{ previewing ? timecode(focusPlayedMs) : '00:00.0' }}</span>
                  <!-- 镜头区间进度条：播放时按播放头收起，暂停时整段可见 -->
                  <div class="relative h-1.5 min-w-0 flex-1 rounded-full bg-white/20" aria-hidden="true">
                    <div
                      class="absolute inset-y-0 left-0 overflow-hidden rounded-full bg-grad-primary"
                      :style="{ width: `${previewing ? playPercent : 100}%` }"
                    >
                      <div class="h-full w-full" />
                    </div>
                    <div
                      v-if="previewing"
                      class="absolute top-1/2 h-3 w-[2px] -translate-y-1/2 rounded-full bg-white shadow-gd-bloom"
                      :style="{ left: `${playPercent}%` }"
                    />
                  </div>
                  <span>{{ timecode(focusSpanMs) }}</span>
                </div>
                <div class="flex items-center justify-between gap-2">
                  <div class="flex items-center gap-1.5">
                    <button
                      type="button"
                      class="rounded-lg bg-white/10 p-1.5 text-white transition-colors hover:bg-white/20 disabled:cursor-not-allowed disabled:opacity-40"
                      :disabled="focusIndex <= 0"
                      aria-label="上一镜"
                      @click="stepFocus(-1)"
                    >
                      <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                        <path d="M18 5l-7 7 7 7M6 5v14" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      class="flex h-8 w-8 items-center justify-center rounded-lg bg-grad-primary text-white shadow-gd-bloom transition-opacity hover:opacity-90"
                      :title="previewing ? '暂停分镜预览' : '点击播放分镜预览'"
                      :aria-label="previewing ? '暂停分镜预览' : '播放分镜预览'"
                      :aria-pressed="previewing"
                      @click="togglePreview"
                    >
                      <svg v-if="!previewing" class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                        <path d="M8 5.14v13.72c0 .8.87 1.3 1.56.9l11-6.86a1.05 1.05 0 000-1.8l-11-6.86A1.05 1.05 0 008 5.14z" />
                      </svg>
                      <svg v-else class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                        <path d="M7 5h4v14H7zM13 5h4v14h-4z" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      class="rounded-lg bg-white/10 p-1.5 text-white transition-colors hover:bg-white/20 disabled:cursor-not-allowed disabled:opacity-40"
                      :disabled="focusIndex < 0 || focusIndex >= shots.length - 1"
                      aria-label="下一镜"
                      @click="stepFocus(1)"
                    >
                      <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                        <path d="M6 5l7 7-7 7M18 5v14" />
                      </svg>
                    </button>
                    <button
                      type="button"
                      class="rounded-lg p-1.5 transition-colors"
                      :class="loopArmed ? 'bg-white/20 text-primary-300' : 'bg-white/10 text-white hover:bg-white/20'"
                      :aria-pressed="loopArmed"
                      title="循环播放整集预览"
                      @click="loopArmed = !loopArmed"
                    >
                      <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                        <path d="M17 2l4 4-4 4M3 11v-1a4 4 0 014-4h14M7 22l-4-4 4-4M21 13v1a4 4 0 01-4 4H3" />
                      </svg>
                    </button>
                    <span class="gd-num rounded bg-white/10 px-2 py-1 text-micro text-neutral-400" title="固定 1.0x 倍速播放">1.0x</span>
                  </div>
                  <AppButton variant="primary" class="gd-btn-bright" size="sm" :disabled="!focusShot" @click="openFocusDrawer">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M5 3v4M3 5h4M6 17v4M4 19h4M13 3l4.5 11-2 .5L13 19l-3.5-2L7 18.5 11.5 3z" />
                    </svg>
                    优化运镜词
                  </AppButton>
                </div>
              </div>
            </div>
            </div>
            <!-- /监看 + 解算 -->
          </div>
        </section>

        <!-- ============ ③ 镜头序列（左栏：卡头 + 工具栏 + 按场分组列表） ============ -->
        <section class="order-1 flex min-w-0 self-start flex-col overflow-hidden rounded-2xl border border-hairline bg-surface-raised shadow-gd-1">
          <!-- 卡头：标题 / 计数 / 待确认 -->
          <div class="flex flex-col gap-1.5 border-b border-hairline px-4 py-3">
            <div class="flex items-center justify-between gap-2">
              <h2 class="text-title font-semibold text-neutral-800">镜头序列</h2>
              <span class="gd-num text-caption text-neutral-400">{{ shots.length }} 镜</span>
            </div>
            <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-caption text-neutral-400">
              <span class="gd-num">{{ totalTimecode }}</span>
              <span aria-hidden="true">·</span>
              <span class="gd-badge gd-badge--amber"><span class="gd-badge__dot" />待确认 {{ pendingShotCount }}</span>
            </div>
          </div>

          <!-- 工具栏：节奏模型（视觉态）+ 生成 / 重新生成 -->
          <div class="flex flex-col gap-2 border-b border-hairline px-4 py-2.5">
            <div class="flex flex-wrap items-center gap-1">
              <span class="mr-0.5 text-kicker uppercase tracking-wider text-neutral-400">节奏</span>
              <button
                v-for="model in PACING_MODELS"
                :key="model.key"
                type="button"
                class="rounded-full border px-2 py-0.5 text-caption transition-colors duration-gd-fast ease-gd-standard"
                :class="pacingModel === model.key ? 'border-transparent bg-primary-100 font-semibold text-primary-300' : 'border-hairline bg-surface-canvas text-neutral-600 hover:bg-surface-sunken hover:text-neutral-800'"
                :aria-pressed="pacingModel === model.key"
                @click="pacingModel = model.key"
              >
                {{ model.label }}
              </button>
            </div>
            <!-- P1-7：用户可直接指定目标镜头数（留空由导演自动决定 6~12 镜） -->
            <div class="flex items-center gap-2">
              <label class="text-kicker uppercase tracking-wider text-neutral-400" for="sb-target-shots">镜头数</label>
              <input
                id="sb-target-shots"
                v-model.number="targetShotCount"
                type="number"
                min="3"
                max="60"
                placeholder="自动"
                class="w-20 rounded-md border border-hairline bg-surface-canvas px-2 py-1 text-caption text-neutral-800 outline-none transition-colors duration-gd-fast focus:border-primary-200"
              />
              <span class="text-caption text-neutral-400">留空自动（6~12 镜），单镜上限随节奏档位</span>
            </div>
            <AppButton
              :variant="episodeHasStoryboard ? 'secondary' : 'primary'"
              size="sm"
              block
              :loading="generating"
              :disabled="episodes.length === 0"
              @click="handleGenerate"
            >
              {{ generating ? '生成中…' : episodeHasStoryboard ? '重新生成' : '✨ 生成分镜' }}
            </AppButton>
          </div>

          <!-- 序列正文（列表为唯一视图；内部滚动，窄栏适配竖幅缩略图） -->
          <div class="max-h-[calc(100vh-20rem)] min-h-[360px] overflow-y-auto p-3">
            <!-- 剧本加载失败 / 无剧本 -->
            <div v-if="episodesError" class="py-8">
              <EmptyState
                v-if="episodesError === 'no-script'"
                icon="✍️"
                title="项目还没有剧本"
                description="生成分镜前需要先完成剧本创作——先去剧本编辑器用一句话生成或上传解析"
              >
                <template #action>
                  <AppButton variant="primary" size="md" @click="router.push(`/projects/${projectId}/script`)">去写剧本 →</AppButton>
                </template>
              </EmptyState>
              <ErrorRetryBlock v-else message="剧本加载失败" :description="episodesError" @retry="loadEpisodes" />
            </div>

            <!-- 分镜加载中 / 加载失败 -->
            <div v-else-if="detailLoading" class="p-1"><LoadingSkeleton variant="table" :rows="6" /></div>
            <div v-else-if="detailError" class="py-6">
              <ErrorRetryBlock message="分镜加载失败" :description="detailError" @retry="loadCurrentDetail" />
            </div>

            <!-- 空态：当前集未生成分镜 / 分镜板无镜头 -->
            <EmptyState
              v-else-if="!currentStoryboard && selectedEpisodeNo"
              icon="🎞️"
              :title="`第 ${selectedEpisodeNo} 集还没有分镜`"
              :description="episodes.length === 0 ? '项目暂无剧本分集，请先完成剧本' : '点击上方按钮，AI 导演将把剧本拆解为镜头序列（景别 / 运镜 / 时长 / 画面描述）'"
            >
              <template #action>
                <AppButton variant="primary" size="md" :loading="generating" :disabled="episodes.length === 0" @click="handleGenerate">✨ 生成分镜</AppButton>
              </template>
            </EmptyState>
            <EmptyState v-else-if="shots.length === 0" icon="🎞️" title="暂无镜头" description="本集分镜板还没有镜头" />

            <!-- 按场分组列表 -->
            <div v-else class="flex flex-col gap-4">
              <section v-for="group in sceneGroups" :key="group.key || 'unanchored'">
                <header class="mb-2 flex items-center gap-2">
                  <h3 class="truncate font-serif text-xs font-semibold text-neutral-800">{{ group.name }}</h3>
                  <span class="gd-num shrink-0 text-micro text-neutral-400">{{ group.shots.length }} 镜 · {{ (group.durationMs / 1000).toFixed(1) }}s</span>
                  <span class="h-px flex-1 bg-hairline" aria-hidden="true" />
                </header>
                <ul class="flex flex-col gap-1.5">
                  <li
                    v-for="shot in group.shots"
                    :key="shot.id"
                    class="gd-card-flat flex cursor-pointer items-center gap-2.5 p-2 transition-[border-color,background-color] duration-gd-fast ease-gd-standard hover:border-hairline-strong"
                    :class="String(activeShotId ?? '') === String(shot.id) ? 'border-primary-400' : ''"
                    :style="String(activeShotId ?? '') === String(shot.id) ? { backgroundColor: CANVAS_TOKENS.selected } : undefined"
                    @click="onCardSelect(shot.id)"
                  >
                    <!-- 封面缩略（9:16 竖幅；缺图 / .json 占位走暗底渐变族） -->
                    <img
                      v-if="isRenderableMedia(shot.media_url)"
                      :src="shot.media_url!"
                      :alt="`S${shot.shot_no} 关键帧`"
                      class="h-[58px] w-[33px] shrink-0 rounded-md object-cover"
                      @error="hideBrokenImage"
                    />
                    <span v-else class="gd-cv-cover gd-cv-1 flex h-[58px] w-[33px] shrink-0 items-end justify-center rounded-md p-0.5" aria-hidden="true">
                      <span class="gd-num text-[9px] leading-none text-white/90">{{ optionLabel(SHOT_SIZE_OPTIONS, shot.shot_size) }}</span>
                    </span>

                    <!-- 镜号 / 描述 / 时长与状态 -->
                    <div class="flex min-w-0 flex-1 flex-col gap-1">
                      <div class="flex min-w-0 items-center gap-1.5">
                        <span class="gd-num shrink-0 text-xs font-semibold text-neutral-800">SH-{{ String(shot.shot_no).padStart(2, '0') }}</span>
                        <span class="truncate rounded-full border border-hairline px-1.5 py-px text-micro text-neutral-400">
                          {{ optionLabel(SHOT_SIZE_OPTIONS, shot.shot_size) }} · {{ optionLabel(CAMERA_MOVE_OPTIONS, shot.camera_move) }}
                        </span>
                      </div>
                      <p class="line-clamp-2 text-caption leading-snug text-neutral-600">
                        {{ shot.dialogue || shot.action_desc || shot.image_prompt || '（暂无描述）' }}
                      </p>
                      <div class="flex min-w-0 items-center gap-1.5">
                        <span class="gd-num shrink-0 text-micro text-neutral-400">{{ (shot.duration_ms / 1000).toFixed(1) }}s</span>
                        <StatusTag :status="shot.status" size="sm" />
                        <span
                          v-if="jumpWarningIds.has(String(shot.id))"
                          class="inline-flex shrink-0 items-center rounded-full bg-warning-bg px-1.5 py-px text-[9px] text-warning-bright"
                          title="与前一镜景别档位差 ≥2，建议确认过渡切镜节奏"
                        >
                          ⚠ 景别跳跃
                        </span>
                      </div>
                    </div>

                    <!-- 排序：上移 / 下移 -->
                    <span class="flex shrink-0 flex-col items-center">
                      <button
                        type="button"
                        class="rounded p-0.5 text-neutral-400 transition-colors hover:bg-surface-sunken hover:text-primary-600 disabled:cursor-not-allowed disabled:opacity-40"
                        :disabled="isLocked || shot === shots[0]"
                        aria-label="上移镜头"
                        @click.stop="moveShot(shots.indexOf(shot), -1)"
                      >
                        ↑
                      </button>
                      <button
                        type="button"
                        class="rounded p-0.5 text-neutral-400 transition-colors hover:bg-surface-sunken hover:text-primary-600 disabled:cursor-not-allowed disabled:opacity-40"
                        :disabled="isLocked || shot === shots[shots.length - 1]"
                        aria-label="下移镜头"
                        @click.stop="moveShot(shots.indexOf(shot), 1)"
                      >
                        ↓
                      </button>
                    </span>
                  </li>
                </ul>
              </section>
            </div>
          </div>
        </section>
      </div>
    </section>

    <!-- ============ ④ STAGE 03 · 智能分镜质检与草稿审批卡（文档流底部审批区，不再 sticky 悬浮遮挡画布） ============ -->
    <section class="mx-auto w-full max-w-[1400px] px-4 pb-5 pt-4">
      <div class="flex w-full flex-wrap items-center justify-between gap-4 rounded-xl border border-hairline bg-surface-raised px-4 py-3 shadow-gd-1">
        <!-- 状态与确认信息 -->
        <div class="flex min-w-0 flex-wrap items-center gap-4">
          <div class="flex items-center gap-3">
            <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-primary-200 bg-primary-100 font-serif text-caption font-semibold text-primary-300" aria-hidden="true">H3</span>
            <div class="flex min-w-0 flex-col gap-1">
              <h3 class="text-title font-semibold leading-none text-neutral-800">分镜检查与审批</h3>
              <p class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-caption text-neutral-600">
                <span>镜头 <span class="gd-num font-semibold text-neutral-800">{{ shots.length }}</span> 镜</span>
                <span aria-hidden="true">·</span>
                <span>预估成片 <span class="gd-num font-semibold text-neutral-800">{{ totalTimecode }}</span></span>
                <span aria-hidden="true">·</span>
                <span title="粗估按默认 30 积分/秒口径；实际按批量编排时所选视频模型单价估算，以弹层为准">预算粗估 <span class="gd-num font-semibold text-gold-deep">{{ estimatedCredits }} 积分</span></span>
                <span class="text-neutral-400">（{{ creditsPerShot }} / 镜 · 实际以批量编排估算为准）</span>
              </p>
            </div>
          </div>
          <!-- 警示确认 checkbox（景别跳变镜头的 HITL 留痕结构件） -->
          <label class="flex cursor-pointer items-center gap-2 rounded-lg border border-hairline-soft bg-surface-canvas px-3 py-1.5 transition-colors hover:bg-surface-sunken">
            <input v-model="jumpAck" type="checkbox" class="h-4 w-4 accent-primary-500" />
            <span class="text-xs text-neutral-600">
              {{ firstJumpShot ? `已确认第 ${firstJumpShot.shot_no} 镜景别跳跃过渡留痕` : '已确认镜头衔接与景别过渡检查' }}
            </span>
          </label>
        </div>
        <!-- 动作按钮组 -->
        <div class="flex flex-wrap items-center gap-3">
          <AppButton
            variant="secondary"
            size="md"
            :loading="generating"
            :disabled="episodes.length === 0"
            title="重新发起 AI 导演拆镜（当前策略不满意时使用）"
            @click="handleGenerate"
          >
            导演策略重拆（重新生成）
          </AppButton>
          <AppButton
            variant="primary"
            size="md"
            :disabled="shots.length === 0 || isLocked"
            @click="lockConfirmOpen = true"
          >
            {{ isLocked ? '已锁定 · HITL-3 审批中' : '批准分镜并送审（进入 F5）' }}
          </AppButton>
        </div>
      </div>
    </section>

    <!-- ============ P13 镜头详情抽屉 ============ -->
    <ShotDetailDrawer
      v-model="drawerOpen"
      :shot="activeShot"
      :project-id="projectId"
      :shots="shots"
      :storyboard-id="currentStoryboard?.id ?? null"
      :storyboard-status="currentStoryboard?.status ?? ''"
      @updated="onShotUpdated"
      @select-shot="onDrawerSelectShot"
      @decided="onStoryboardDecided"
    />

    <!-- ============ 锁定二次确认 ============ -->
    <ConfirmDialog
      v-model="lockConfirmOpen"
      title="锁定分镜"
      content="锁定后全部镜头不可再编辑、拖拽或绑定锚点，并创建分镜审批单（HITL-3，7 天内有效）。确认锁定？"
      :loading="locking"
      confirm-label="确认锁定"
      @confirm="handleLock"
    />

    <!-- ============ 60005 缺锚点清单弹窗（Modal 基座） ============ -->
    <Modal v-model="missingAnchorsOpen" :max-width="440">
      <template #header>
        <h3 class="text-title text-neutral-800">还有 {{ missingShots.length }} 个镜头未绑定锚点</h3>
      </template>

      <p class="text-caption leading-relaxed text-neutral-400">
        锁定分镜前，每个镜头都至少需要一个角色或场景锚点（保证画面一致性）。请点击镜头卡打开详情抽屉完成绑定。
      </p>
      <ul class="mt-4 max-h-52 space-y-1.5 overflow-y-auto rounded-md bg-surface-canvas p-3">
        <li v-for="item in missingShots" :key="item.shot_id" class="flex items-center gap-2 text-caption text-neutral-600">
          <span class="gd-num flex h-5 w-7 items-center justify-center rounded bg-warning-bg text-micro text-warning-bright">S{{ item.shot_no }}</span>
          <span>第 {{ item.order_index + 1 }} 镜 · 缺少角色 / 场景锚点</span>
        </li>
      </ul>

      <template #footer>
        <AppButton variant="secondary" size="md" @click="missingAnchorsOpen = false">稍后处理</AppButton>
        <AppButton variant="primary" size="md" @click="goFirstMissingShot">去绑定锚点</AppButton>
      </template>
    </Modal>
  </div>
</template>

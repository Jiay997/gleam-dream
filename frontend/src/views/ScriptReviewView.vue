<!--
  文件级注释：P08 · 剧本审批（HITL-1，路由 /projects/:projectId/script/review，AppShell 布局）。
  v7 纸上放映厅 Pro · 对照 F2-05（P08 关口一审批）：
  - 头部信息条改「关口一 · 剧本确认」金色刊眉 + approval_type 徽标 + 发起时间 / expires_at 倒计时；
  - 左栏结构化只读快照（稿纸卡：EP 行标 + 场次行类型胶囊，四类行只读渲染）；
  - 右栏审核结果清单：非 block 项逐项勾选（勾满绿勾），block 项红字无勾选（流程终止提示），
    全部非 block 项勾选后才可 approve（approveDisabled 联动）；
  - 剧本摘要卡保留（集数 / 场次 / 字数 / 角色 dl 行）+ 审批单信息卡；
  - 底部 ApprovalBar（通过 / 驳回 / 去编辑）保留，reject 反馈 ≥10 字校验由 ApprovalBar 承担；
  - decide 流程、多单切换、倒计时、approve 二次确认等功能逻辑一律未动。
  数据：GET /orchestration/approvals?project_id=&status=pending（前端筛 approval_type=script）、
  GET /orchestration/approvals/{id}、GET /projects/{id}/scripts/current（只读快照）。
  decide：POST /orchestration/approvals/{id}/decide → 成功 toast → approve 跳 P09 资产墙 / reject 跳 P06 剧本编辑器。
  空态：EmptyState「当前没有待确认的剧本」+ 返回审批台。
-->
<script setup lang="ts">
/**
 * ScriptReviewView 剧本审批（HITL-1）。
 * props：无。emits：无。
 * 依赖 approvals API（列表 / 详情 / decide）、script API（只读树）、useToast。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { decideApproval, getApprovalDetail, listApprovals, type ApprovalDetail, type ApprovalItem } from '@/api/approvals'
import {
  getProjectCurrentScript,
  submitScriptReview,
  type EpisodeNode,
  type SceneNode,
  type ScriptLineNode,
  type ScriptTree,
} from '@/api/script'
import AppButton from '@/components/common/AppButton.vue'
import ApprovalBar from '@/components/common/ApprovalBar.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import { useToast } from '@/composables/useToast'
import { useProjectStore } from '@/stores/project'
import { formatRelativeTime, formatShortDate } from '@/utils/datetime'
import { formatSceneHeading } from '@/utils/sceneText'

/** 路由实例（decide 后跳审批台 / edit 跳编辑器） */
const route = useRoute()
const router = useRouter()
/** Toast */
const toast = useToast()
/** 项目 store（空态诊断需要读取项目阶段与当前剧本 ID） */
const projectStore = useProjectStore()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ---------- 审批单列表 ---------- */
/** 待办剧本审批单（approval_type=script） */
const scriptApprovals = ref<ApprovalItem[]>([])
/** 列表加载中 */
const listLoading = ref(true)
/** 列表加载失败信息 */
const listError = ref('')
/** 当前选中审批单 ID */
const selectedId = ref<number | null>(null)
/** 最近一张剧本审批单（任意状态，用于空态诊断「已通过 / 已驳回 / 已过期」） */
const latestScriptApproval = ref<ApprovalItem | null>(null)
/** 当前剧本状态（pending_confirm / rejected / draft 等；空串表示未取到） */
const currentScriptStatus = ref('')
/** 重新生成审批单提交中 */
const ensuringApproval = ref(false)

/* ---------- 审批单详情 ---------- */
/** 当前审批单详情（null 表示未加载） */
const approvalDetail = ref<ApprovalDetail | null>(null)
/** 详情加载中 */
const detailLoading = ref(false)

/* ---------- 只读剧本快照 ---------- */
/** 当前剧本树（只读渲染用） */
const scriptTree = ref<ScriptTree | null>(null)
/** 树加载中 */
const treeLoading = ref(false)
/** 树加载失败信息 */
const treeError = ref('')
/** 折叠的集号集合（默认全部展开） */
const collapsedEpisodes = ref<Set<number>>(new Set())

/* ---------- 倒计时 ---------- */
/** 当前时刻（每 30s 刷新一次驱动倒计时重算） */
const nowTick = ref(Date.now())
/** 倒计时定时器 */
let countdownTimer: ReturnType<typeof setInterval> | null = null

/* ---------- 决策提交 ---------- */
/** 决策提交中 */
const deciding = ref(false)
/** approve 二次确认弹窗 */
const approveDialogOpen = ref(false)

/* ==================== 派生数据 ==================== */

/**
 * 剧名加书名号展示（P2-1 修复：后端 title 可能已含《》，先剥离再统一包裹，
 * 避免出现「《《剧名》》」重复书名号）。
 */
const wrappedScriptTitle = computed(() => {
  const raw = scriptTree.value?.script.title?.trim()
  if (!raw) return ''
  const stripped = raw.replace(/^《+/, '').replace(/》+$/, '')
  return `《${stripped}》`
})

/** 当前选中的列表项 */
const selectedItem = computed(() => scriptApprovals.value.find((a) => a.id === selectedId.value) ?? null)

/**
 * 有效期倒计时文案（剩余 X 天 X 小时；已过期提示）。
 * @returns 倒计时文案
 */
const countdownText = computed(() => {
  void nowTick.value
  const expires = selectedItem.value?.expires_at ?? approvalDetail.value?.expires_at
  if (!expires) return '—'
  const diff = new Date(expires).getTime() - Date.now()
  if (Number.isNaN(diff)) return '—'
  if (diff <= 0) return '已过期'
  const days = Math.floor(diff / 86_400_000)
  const hours = Math.floor((diff % 86_400_000) / 3_600_000)
  return `剩余 ${days} 天 ${hours} 小时`
})

/** 是否临近过期（<24h，倒计时标红） */
const expiringSoon = computed(() => {
  void nowTick.value
  const expires = selectedItem.value?.expires_at
  if (!expires) return false
  const diff = new Date(expires).getTime() - Date.now()
  return diff > 0 && diff < 86_400_000
})

/** 合规 flags（审批单详情 / summary 宽松读取；无数据为空数组） */
interface ModerationFlag {
  /** 级别（block/warn/info） */
  severity: string
  /** 描述 */
  message: string
}

/**
 * 从审批单详情宽松提取合规 flags（后端就绪后按实际结构微调）。
 * @returns flags 列表
 */
const moderationFlags = computed<ModerationFlag[]>(() => {
  const detail = approvalDetail.value as (ApprovalDetail & { summary?: Record<string, unknown>; payload?: Record<string, unknown> }) | null
  const raw = detail?.summary?.compliance ?? detail?.payload?.compliance ?? detail?.summary?.moderation ?? detail?.payload?.moderation
  if (!Array.isArray(raw)) return []
  return raw
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => ({
      severity: String(item.severity ?? item.level ?? 'warn'),
      message: String(item.message ?? item.reason ?? item.detail ?? '合规提示'),
    }))
})

/** 是否存在 block 级合规问题（禁用 approve） */
const hasBlockFlag = computed(() => moderationFlags.value.some((flag) => flag.severity.toLowerCase() === 'block'))

/* ---------- 审核结果清单（对照 F2-05：逐项勾选，勾满才可 approve；block 红字无勾选） ---------- */

/** 已勾选的非 block flag 下标集合（切单时重置） */
const checkedFlags = ref<Set<number>>(new Set())

/** 非 block flag 的下标列表 */
const nonBlockFlagIndexes = computed(() =>
  moderationFlags.value.map((flag, index) => ({ flag, index })).filter((item) => item.flag.severity.toLowerCase() !== 'block'),
)

/** 非 block 项是否全部勾选（无清单项时视为通过，不阻塞 approve） */
const allFlagsChecked = computed(() =>
  nonBlockFlagIndexes.value.every((item) => checkedFlags.value.has(item.index)),
)

/**
 * 切换某条审核项勾选状态。
 * @param index flag 下标
 * @returns 无
 */
function toggleFlag(index: number): void {
  const next = new Set(checkedFlags.value)
  if (next.has(index)) {
    next.delete(index)
  } else {
    next.add(index)
  }
  checkedFlags.value = next
}

/** 审核清单进度文案（n / N 已处理） */
const checklistProgress = computed(() => `${checkedFlags.value.size} / ${moderationFlags.value.length} 已处理`)

/* ---------- 剧本统计 ---------- */

/**
 * 归一化行类型。
 * @param line 剧本行
 * @returns 行类型
 */
function lineType(line: ScriptLineNode): string {
  return line.line_type ?? line.type ?? 'action'
}

/**
 * 归一化说话人。
 * @param line 剧本行
 * @returns 说话人
 */
function lineSpeaker(line: ScriptLineNode): string | null {
  return line.speaker ?? line.character ?? null
}

/**
 * 对白展示正文（修复测试报告 #16 的说话人前缀重复）。
 * LLM 生成/解析的 content 偶尔残留「林凡（独白）：」类前缀，而模板已拼
 * speaker 前缀导致重复；此处检测 content 以 speaker 开头且紧跟
 * 「(备注)：」/「：」结构时仅保留正文，其余情况原样返回避免误伤。
 * @param line 剧本行
 * @returns 去重后的展示正文
 */
function dialogueText(line: ScriptLineNode): string {
  const speaker = lineSpeaker(line)
  const content = (line.content ?? '').trim()
  if (!speaker || !content.startsWith(speaker)) return content
  const rest = content.slice(speaker.length).trim()
  const match = rest.match(/^(?:（[^）]*）|\([^)]*\))?[:：]\s*/)
  if (match) {
    const body = rest.slice(match[0].length)
    return body || content
  }
  return content
}

/**
 * 归一化集号。
 * @param episode 剧集节点
 * @param index 列表序号
 * @returns 集号
 */
function episodeNoOf(episode: EpisodeNode, index: number): number {
  return Number(episode.episode_no ?? episode.index ?? index + 1)
}

/**
 * 归一化场次号。
 * @param scene 场次节点
 * @param index 列表序号
 * @returns 场次号
 */
function sceneNoOf(scene: SceneNode, index: number): number {
  return Number(scene.scene_no ?? scene.index ?? index + 1)
}

/**
 * 场次标题文案：共享工具统一翻译英文枚举并拼装（测试报告问题 1）。
 * @param scene 场次节点
 * @returns 展示用场次标题
 */
function sceneHeadingText(scene: SceneNode): string {
  return formatSceneHeading(scene)
}

/** 剧本树剧集（宽松容错） */
const treeEpisodes = computed<EpisodeNode[]>(() => {
  const eps = scriptTree.value?.episodes
  return Array.isArray(eps) ? eps : []
})

/** 剧本统计：集数 / 场次 / 字数 / 主要角色 */
const scriptStats = computed(() => {
  let sceneCount = 0
  let wordCount = 0
  const characters = new Set<string>()
  for (const ep of treeEpisodes.value) {
    const scenes = Array.isArray(ep.scenes) ? ep.scenes : []
    sceneCount += scenes.length
    for (const scene of scenes) {
      const lines = Array.isArray(scene.lines) ? scene.lines : []
      for (const line of lines) {
        wordCount += (line.content ?? '').length
        const speaker = lineSpeaker(line)
        if (speaker && lineType(line) === 'dialogue') characters.add(speaker)
      }
    }
  }
  return { episodes: treeEpisodes.value.length, scenes: sceneCount, words: wordCount, characters: Array.from(characters) }
})

/* ==================== 数据加载 ==================== */

/**
 * 拉取待办剧本审批单列表（默认选中第一单并加载详情）。
 * @returns 无
 */
async function fetchList(): Promise<void> {
  listLoading.value = true
  listError.value = ''
  try {
    // 服务端直接按 approval_type 过滤：避免同项目其他关口待办占满分页，导致剧本待办被截断。
    const [pendingPage, historyPage] = await Promise.all([
      listApprovals({
        project_id: projectId.value,
        approval_type: 'script',
        status: 'pending',
        page_size: 100,
      }),
      listApprovals({
        project_id: projectId.value,
        approval_type: 'script',
        status: 'all',
        page: 1,
        page_size: 10,
      }),
    ])
    scriptApprovals.value = pendingPage.items.filter((a) => a.approval_type === 'script')
    // 只认「当前剧本版本」的历史审批单：避免旧版本的 approved/rejected 误判当前版本状态
    const currentScriptId = projectStore.currentProject?.current_script_id
    latestScriptApproval.value =
      currentScriptId == null
        ? null
        : historyPage.items.find(
            (item) => item.target_id != null && String(item.target_id) === String(currentScriptId),
          ) ?? null
    if (scriptApprovals.value.length > 0) {
      await selectApproval(scriptApprovals.value[0].id)
    } else {
      selectedId.value = null
      approvalDetail.value = null
      await loadScriptStatus()
    }
  } catch (error) {
    listError.value = error instanceof Error ? error.message : '审批单加载失败'
  } finally {
    listLoading.value = false
  }
}

/** 当前剧本 ID（来自项目详情，避免无剧本时请求产生 10002 错误） */
const currentScriptId = computed(() => projectStore.currentProject?.current_script_id ?? null)

/**
 * 读取当前剧本状态（仅用于空态诊断：未提交 / 审核中 / 已通过 / 已驳回）。
 * @returns 无
 */
async function loadScriptStatus(): Promise<void> {
  currentScriptStatus.value = ''
  if (currentScriptId.value == null) return
  try {
    const tree = await getProjectCurrentScript(projectId.value)
    currentScriptStatus.value = String(tree.script.status ?? '')
  } catch {
    // 空态诊断失败不阻塞页面，按钮会引导用户去剧本页
    currentScriptStatus.value = ''
  }
}

/** 空态主操作类型 */
type EmptyAction = 'script' | 'assets' | 'overview' | 'retry' | 'ensure'

/** 空态诊断结果 */
interface EmptyDiagnosis {
  /** 标题 */
  title: string
  /** 说明（原因 + 下一步） */
  description: string
  /** 主按钮文案 */
  primaryLabel: string
  /** 主操作 */
  primaryAction: EmptyAction
}

/** 项目是否仍处于剧本关口阶段（draft/scripting/script_review） */
const isScriptStage = computed(() =>
  ['draft', 'scripting', 'script_review'].includes(String(projectStore.currentProject?.status ?? '')),
)

/**
 * 根据「最近审批单状态 + 当前剧本状态 + 项目阶段」生成可解释的空态。
 * 目标：不再用一句“当前没有待确认的剧本”让用户卡住，而是告诉用户为什么空、下一步去哪。
 * @returns 空态标题 / 说明 / 主按钮
 */
const emptyDiagnosis = computed<EmptyDiagnosis>(() => {
  const latest = latestScriptApproval.value
  const scriptStatus = currentScriptStatus.value

  if (latest?.status === 'pending') {
    return {
      title: '待办正在同步',
      description: '检测到剧本待确认审批单，但列表尚未加载完成；请刷新重试。',
      primaryLabel: '刷新待办',
      primaryAction: 'retry',
    }
  }
  if (latest?.status === 'approved') {
    return {
      title: '剧本已确认通过',
      description: `审批单 #${latest.id} 已通过，可直接进入资产设计完成后续环节。`,
      primaryLabel: '前往资产设计 →',
      primaryAction: 'assets',
    }
  }
  if (latest?.status === 'rejected') {
    return {
      title: '剧本已被驳回',
      description: `审批单 #${latest.id} 已驳回：请按驳回意见修改剧本后重新提交审核。`,
      primaryLabel: '去剧本页修改 →',
      primaryAction: 'script',
    }
  }
  if (latest?.status === 'expired') {
    return {
      title: '剧本审批已过期',
      description: `审批单 #${latest.id} 已超过 7 天有效期；请在剧本页重新提交审核，生成新的待办。`,
      primaryLabel: '重新提交审核 →',
      primaryAction: 'script',
    }
  }
  if (currentScriptId.value == null) {
    return {
      title: '项目还没有剧本',
      description: '先在剧本页生成或上传剧本；提交审核后，待办会出现在审批台与这里。',
      primaryLabel: '去剧本页生成剧本 →',
      primaryAction: 'script',
    }
  }
  if (!isScriptStage.value) {
    return {
      title: '剧本确认已完成',
      description: '项目已进入资产、分镜或生产阶段，当前无需处理剧本审批；可返回审批台查看其他关口。',
      primaryLabel: '返回审批台',
      primaryAction: 'overview',
    }
  }
  if (scriptStatus === 'pending_confirm') {
    return {
      title: '剧本已通过合规审核，但审批单缺失',
      description: '审批单可能被清理或由旧流程生成；点击下方按钮可幂等补齐 HITL-1 待办（不会重复调用模型）。',
      primaryLabel: '重新生成审批单',
      primaryAction: 'ensure',
    }
  }
  if (scriptStatus === 'compliance_review') {
    return {
      title: '合规审核进行中',
      description: '审核完成后会自动创建审批单；请稍后刷新，或返回审批台查看其他待办。',
      primaryLabel: '刷新状态',
      primaryAction: 'retry',
    }
  }
  if (scriptStatus === 'rejected') {
    return {
      title: '合规审核未通过',
      description: '请按剧本页展示的合规提示修改内容，再重新提交审核。',
      primaryLabel: '去剧本页修改 →',
      primaryAction: 'script',
    }
  }
  if (scriptStatus === 'needs_manual_fix') {
    return {
      title: '剧本有低置信度内容待人工校正',
      description: '完成逐行校正后提交审核，审批单会自动出现在审批台。',
      primaryLabel: '去剧本页校正 →',
      primaryAction: 'script',
    }
  }
  if (scriptStatus === '') {
    return {
      title: '暂未读取到剧本状态',
      description: '请刷新重试；若仍无待办，可前往剧本页确认剧本是否已提交审核。',
      primaryLabel: '刷新状态',
      primaryAction: 'retry',
    }
  }
  return {
    title: '剧本尚未提交审核',
    description: '在剧本页完成编辑后点击「提交审核 · 前往审批台」，待办会自动出现在这里。',
    primaryLabel: '去剧本页提交审核 →',
    primaryAction: 'script',
  }
})

/**
 * 幂等补齐 HITL-1 审批单（对 pending_confirm 剧本不会重复调用模型）。
 * @returns 无
 */
async function ensureScriptApproval(): Promise<void> {
  if (currentScriptId.value == null || ensuringApproval.value) return
  ensuringApproval.value = true
  try {
    const result = await submitScriptReview(currentScriptId.value)
    if (result.approval_id != null) {
      toast.success('审批单已重新生成，正在刷新待办')
    } else {
      toast.info('当前项目阶段无需剧本审批单')
    }
    await fetchList()
  } catch {
    // 拦截器已 toast
  } finally {
    ensuringApproval.value = false
  }
}

/**
 * 空态主按钮分发。
 * @param action 主操作类型
 * @returns 无
 */
function handleEmptyAction(action: EmptyAction): void {
  if (action === 'script') {
    void router.push(`/projects/${projectId.value}/script`)
    return
  }
  if (action === 'assets') {
    void router.push(`/projects/${projectId.value}/assets`)
    return
  }
  if (action === 'overview') {
    goOverview()
    return
  }
  if (action === 'ensure') {
    void ensureScriptApproval()
    return
  }
  void fetchList()
}

/**
 * 选中审批单：拉取详情 + 只读剧本快照。
 * @param id 审批单 ID
 * @returns 无
 */
async function selectApproval(id: number): Promise<void> {
  selectedId.value = id
  approvalDetail.value = null
  scriptTree.value = null
  treeError.value = ''
  collapsedEpisodes.value = new Set()
  checkedFlags.value = new Set()

  detailLoading.value = true
  treeLoading.value = true
  try {
    approvalDetail.value = await getApprovalDetail(id)
  } catch {
    // 拦截器已 toast；详情缺失时仅渲染列表信息
  } finally {
    detailLoading.value = false
  }
  await loadSnapshot()
}

/**
 * 拉取只读剧本快照（GET /projects/{id}/scripts/current）。
 * @returns 无
 */
async function loadSnapshot(): Promise<void> {
  treeLoading.value = true
  treeError.value = ''
  try {
    scriptTree.value = await getProjectCurrentScript(projectId.value)
  } catch (error) {
    scriptTree.value = null
    treeError.value = error instanceof Error ? error.message : '剧本快照加载失败'
  } finally {
    treeLoading.value = false
  }
}

/* ==================== 折叠 ==================== */

/**
 * 切换分集折叠。
 * @param episodeNo 集号
 * @returns 无
 */
function toggleEpisode(episodeNo: number): void {
  const next = new Set(collapsedEpisodes.value)
  if (next.has(episodeNo)) {
    next.delete(episodeNo)
  } else {
    next.add(episodeNo)
  }
  collapsedEpisodes.value = next
}

/* ==================== 审批决策 ==================== */

/**
 * 通过（二次确认后 decide approve）。
 * @returns 无
 */
function openApproveConfirm(): void {
  if (!selectedId.value) return
  approveDialogOpen.value = true
}

/**
 * 提交 approve 决策（通过后直达资产环节，PRD H1 approve → 资产规划与生成）。
 * @returns 无
 */
async function confirmApprove(): Promise<void> {
  if (!selectedId.value) return
  deciding.value = true
  try {
    await decideApproval(selectedId.value, { action: 'approve' })
    toast.success('剧本已通过确认，正在前往资产环节')
    approveDialogOpen.value = false
    void router.push(`/projects/${projectId.value}/assets`)
  } catch {
    // 拦截器已 toast（40001 已处理 / 已过期等）
  } finally {
    deciding.value = false
  }
}

/**
 * 提交 reject 决策（反馈 ≥10 字由 ApprovalBar 校验；驳回后回剧本编辑器查看改写进度）。
 * @param comment 驳回原因
 * @returns 无
 */
async function handleReject(comment: string): Promise<void> {
  if (!selectedId.value) return
  deciding.value = true
  try {
    await decideApproval(selectedId.value, { action: 'reject', feedback: comment })
    toast.success('已驳回，编剧将按反馈修改剧本')
    void router.push(`/projects/${projectId.value}/script`)
  } catch {
    // 拦截器已 toast
  } finally {
    deciding.value = false
  }
}

/**
 * 跳转编辑器（edit 决策）。
 * @returns 无
 */
function goEditor(): void {
  void router.push(`/projects/${projectId.value}/script`)
}

/**
 * 返回审批台（/overview，P05）。
 * @returns 无
 */
function goOverview(): void {
  void router.push(`/projects/${projectId.value}/overview`)
}

// 挂载：拉列表 + 启动倒计时刷新
onMounted(() => {
  void fetchList()
  countdownTimer = setInterval(() => {
    nowTick.value = Date.now()
  }, 30_000)
})

// 卸载：清理倒计时定时器
onUnmounted(() => {
  if (countdownTimer) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
})
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <!-- ============ 加载骨架（整页） ============ -->
    <div v-if="listLoading" class="min-h-0 flex-1 overflow-y-auto p-6">
      <LoadingSkeleton variant="text" :rows="4" />
      <div class="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-5">
        <LoadingSkeleton class="lg:col-span-3" variant="text" :rows="8" />
        <LoadingSkeleton class="lg:col-span-2" variant="card" />
      </div>
    </div>

    <!-- ============ 列表加载失败 ============ -->
    <div v-else-if="listError" class="min-h-0 flex-1 overflow-y-auto p-6">
      <ErrorRetryBlock message="审批单加载失败" :description="listError" @retry="fetchList" />
    </div>

    <!-- ============ 空态：无待办剧本审批 ============ -->
    <div v-else-if="scriptApprovals.length === 0" class="min-h-0 flex-1 overflow-y-auto">
      <EmptyState
        icon="👀"
        :title="emptyDiagnosis.title"
        :description="emptyDiagnosis.description"
      >
        <template #action>
          <div class="flex flex-wrap items-center justify-center gap-2.5">
            <AppButton
              variant="primary"
              size="md"
              :loading="ensuringApproval"
              @click="handleEmptyAction(emptyDiagnosis.primaryAction)"
            >
              {{ emptyDiagnosis.primaryLabel }}
            </AppButton>
            <AppButton
              v-if="emptyDiagnosis.primaryAction !== 'overview'"
              variant="secondary"
              size="md"
              @click="goOverview"
            >
              返回审批台
            </AppButton>
          </div>
        </template>
      </EmptyState>
    </div>

    <!-- ============ 数据区 ============ -->
    <template v-else>
      <!-- ===== 头部审批信息条 56px（对照 F2-05「关口一 · 剧本确认」金色刊眉） ===== -->
      <header class="flex h-toolbar shrink-0 items-center gap-3 border-b border-hairline bg-surface-raised px-5">
        <!-- 关口刊眉 + 类型徽标 -->
        <span class="inline-flex shrink-0 items-center gap-2 text-caption font-semibold text-gold-deep">
          <span class="h-1.5 w-1.5 rounded-full bg-gold" aria-hidden="true" />
          关口一 · 剧本确认
        </span>
        <span class="gd-num inline-flex shrink-0 items-center rounded-full bg-surface-sunken px-2 py-0.5 text-caption text-neutral-500">
          approval_type: <b class="ml-1 font-semibold text-neutral-700">{{ selectedItem?.approval_type || 'script' }}</b>
        </span>

        <!-- 多单切换 -->
        <div v-if="scriptApprovals.length > 1" class="flex items-center gap-1.5 overflow-x-auto" role="tablist" aria-label="审批单切换">
          <button
            v-for="item in scriptApprovals"
            :key="item.id"
            type="button"
            class="gd-num shrink-0 rounded-full border px-2.5 py-1 text-xs transition-colors duration-gd-fast"
            :class="item.id === selectedId ? 'border-primary-500 bg-primary-50 font-medium text-primary-700' : 'border-hairline bg-surface-raised text-neutral-500 hover:border-primary-300'"
            role="tab"
            :aria-selected="item.id === selectedId"
            @click="selectApproval(item.id)"
          >
            #{{ item.id }}
          </button>
        </div>

        <div class="flex-1" />

        <!-- 发起时间 + 有效期倒计时 -->
        <span class="shrink-0 text-xs text-neutral-400">
          提交于 {{ formatRelativeTime(selectedItem?.requested_at || selectedItem?.created_at) }}
          <template v-if="selectedItem?.requested_at">（{{ formatShortDate(selectedItem.requested_at) }}）</template>
        </span>
        <span class="gd-num shrink-0 text-xs font-medium" :class="expiringSoon ? 'text-danger' : 'text-gold-deep'">expires_at {{ countdownText }}</span>
      </header>

      <!-- ===== 中部双栏（<1024px 降级为上下单列，避免横向溢出） ===== -->
      <div class="flex min-h-0 flex-1 flex-col lg:flex-row">
        <!-- 左 60%：只读剧本快照（分集折叠） -->
        <section class="w-full min-w-0 flex-1 overflow-y-auto border-b border-hairline bg-surface-raised lg:w-3/5 lg:flex-none lg:border-b-0 lg:border-r" aria-label="剧本只读快照">
          <!-- 详情 / 快照加载中 -->
          <div v-if="detailLoading || treeLoading" class="p-6">
            <LoadingSkeleton variant="text" :rows="10" />
          </div>

          <!-- 快照加载失败 -->
          <div v-else-if="treeError" class="p-6">
            <ErrorRetryBlock message="剧本快照加载失败" :description="treeError" @retry="loadSnapshot" />
          </div>

          <!-- 只读正文（对照 F2-05「剧本快照」结构化稿纸卡） -->
          <div v-else class="mx-auto max-w-2xl px-5 pb-8 pt-5">
            <div class="gd-card px-0 py-0">
              <div class="flex items-center justify-between gap-3 border-b border-hairline-soft px-6 py-4">
                <h2 class="truncate font-serif text-title text-neutral-900">
                  {{ wrappedScriptTitle }}剧本快照
                  <span v-if="scriptTree?.script.version_no" class="gd-num ml-1 text-caption font-normal text-neutral-400">v{{ scriptTree.script.version_no }} · 只读</span>
                </h2>
                <span class="inline-flex shrink-0 items-center gap-1.5 rounded-full bg-warning-bg px-2.5 py-1 text-caption font-medium text-warning">
                  <span class="h-1.5 w-1.5 rounded-full bg-gold" aria-hidden="true" />
                  pending_confirm · 待确认
                </span>
              </div>
              <p v-if="scriptTree?.script.logline" class="gd-lede border-b border-hairline-soft px-6 py-3 !mt-0 text-sm">{{ scriptTree.script.logline }}</p>

              <article v-for="(ep, epIndex) in treeEpisodes" :key="epIndex" class="border-b border-hairline-soft last:border-b-0">
                <!-- 集头（可折叠，EP 行标对照 F2-05） -->
                <button
                  type="button"
                  class="flex w-full items-center gap-2.5 px-6 py-2.5 text-left transition-colors duration-gd-fast hover:bg-surface-canvas"
                  :aria-expanded="!collapsedEpisodes.has(episodeNoOf(ep, epIndex))"
                  @click="toggleEpisode(episodeNoOf(ep, epIndex))"
                >
                  <svg class="h-3.5 w-3.5 text-neutral-400 transition-transform duration-gd-normal" :class="collapsedEpisodes.has(episodeNoOf(ep, epIndex)) ? '-rotate-90' : ''" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                    <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                  <span class="gd-num shrink-0 text-xs font-semibold text-primary-700">EP{{ String(episodeNoOf(ep, epIndex)).padStart(2, '0') }}</span>
                  <span class="truncate text-sm font-medium text-neutral-900">{{ ep.title || '未命名分集' }}</span>
                  <span class="gd-num ml-auto shrink-0 text-xs text-neutral-400">{{ Array.isArray(ep.scenes) ? ep.scenes.length : 0 }} 场</span>
                </button>

                <!-- 场次与行（只读渲染，行类型胶囊对照 F2-05 快照行） -->
                <template v-if="!collapsedEpisodes.has(episodeNoOf(ep, epIndex))">
                  <div v-for="(scene, scIndex) in (Array.isArray(ep.scenes) ? ep.scenes : [])" :key="scIndex" class="px-6 py-2">
                    <h4 class="gd-num text-xs font-semibold tracking-wide text-neutral-500">
                      场 {{ sceneNoOf(scene, scIndex) }} · {{ sceneHeadingText(scene) }}
                    </h4>
                    <div v-for="line in (Array.isArray(scene.lines) ? scene.lines : [])" :key="String(line.id)" class="flex items-start gap-2 py-0.5 pl-2">
                      <span class="mt-0.5 shrink-0 rounded bg-surface-sunken px-1.5 py-0.5 text-micro leading-4 text-neutral-500" aria-hidden="true">{{ lineType(line) }}</span>
                      <p v-if="lineType(line) === 'dialogue'" class="min-w-0 flex-1 text-sm leading-relaxed text-neutral-800">
                        <span class="font-semibold text-primary-700">{{ lineSpeaker(line) || '未命名角色' }}：</span>{{ dialogueText(line) }}
                      </p>
                      <p v-else-if="lineType(line) === 'narration'" class="min-w-0 flex-1 text-sm italic leading-relaxed text-neutral-600">{{ line.content }}</p>
                      <p v-else-if="lineType(line) === 'action'" class="min-w-0 flex-1 rounded bg-surface-sunken px-2 py-0.5 text-sm leading-relaxed text-neutral-600">{{ line.content }}</p>
                      <p v-else class="min-w-0 flex-1 text-sm leading-relaxed text-neutral-400">（{{ line.content }}）</p>
                    </div>
                    <p v-if="(Array.isArray(scene.lines) ? scene.lines : []).length === 0" class="py-1 pl-2 text-xs text-neutral-400">本场暂无内容</p>
                  </div>
                </template>
              </article>
            </div>
          </div>
        </section>

        <!-- 右 40%：摘要 + 合规面板 -->
        <aside class="w-full min-w-0 flex-1 overflow-y-auto bg-surface-canvas p-5 lg:w-2/5 lg:flex-none" aria-label="审核摘要">
          <!-- 剧本摘要卡（结构化 dl 行，对照 F2-05 episode_count / word_count 等） -->
          <section class="gd-card p-6">
            <h3 class="font-serif text-title text-neutral-900">剧本摘要</h3>
            <dl class="mt-3 space-y-2 text-sm">
              <div class="flex justify-between">
                <dt class="text-neutral-500">集数</dt>
                <dd class="gd-num font-medium text-neutral-800">{{ scriptStats.episodes }} 集</dd>
              </div>
              <div class="flex justify-between">
                <dt class="text-neutral-500">场次</dt>
                <dd class="gd-num font-medium text-neutral-800">{{ scriptStats.scenes }} 场</dd>
              </div>
              <div class="flex justify-between">
                <dt class="text-neutral-500">总字数</dt>
                <dd class="gd-num font-medium text-neutral-800">{{ scriptStats.words.toLocaleString('zh-CN') }} 字</dd>
              </div>
              <div class="flex justify-between gap-6">
                <dt class="shrink-0 text-neutral-500">主要角色</dt>
                <dd class="min-w-0 flex-1 text-right">
                  <template v-if="scriptStats.characters.length > 0">
                    <span
                      v-for="name in scriptStats.characters.slice(0, 6)"
                      :key="name"
                      class="gd-num mr-1.5 inline-block rounded bg-primary-50 px-2 py-0.5 text-xs font-medium text-primary-700"
                    >
                      {{ name }}
                    </span>
                    <span v-if="scriptStats.characters.length > 6" class="text-xs text-neutral-400">+{{ scriptStats.characters.length - 6 }}</span>
                  </template>
                  <span v-else class="text-xs text-neutral-400">暂无对白角色</span>
                </dd>
              </div>
            </dl>
            <!-- 驳回记录（若该单曾被处理过） -->
            <p v-if="approvalDetail?.feedback" class="mt-4 rounded-lg bg-surface-canvas p-3 text-xs leading-relaxed text-neutral-500">
              <span class="font-medium text-neutral-700">历史反馈：</span>{{ approvalDetail.feedback }}
            </p>
          </section>

          <!-- 审核结果清单（对照 F2-05：逐项勾选，全部勾选才可 approve；block 项红字无勾选） -->
          <section class="gd-card mt-4 p-6">
            <div class="flex items-center justify-between gap-3">
              <h3 class="text-sm font-semibold text-neutral-900">审核结果清单</h3>
              <span v-if="moderationFlags.length > 0" class="gd-num shrink-0 rounded-full bg-surface-sunken px-2 py-0.5 text-caption text-neutral-500">
                {{ checklistProgress }}
              </span>
            </div>

            <!-- block 级红横幅（流程终止提示，禁用 approve） -->
            <div v-if="hasBlockFlag" class="mt-3 flex items-start gap-2.5 rounded-lg border border-danger-border bg-danger-bg p-4" role="alert">
              <svg class="mt-0.5 h-4 w-4 shrink-0 text-danger" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <p class="text-xs font-medium leading-relaxed text-danger">存在 block 项：需驳回修改后重新提交，暂不可通过</p>
            </div>

            <!-- 清单项 -->
            <ul v-if="moderationFlags.length > 0" class="mt-3 space-y-2.5">
              <li v-for="(flag, index) in moderationFlags" :key="index">
                <!-- 非 block：可勾选行（勾满绿勾） -->
                <button
                  v-if="flag.severity.toLowerCase() !== 'block'"
                  type="button"
                  class="flex w-full items-start gap-3 rounded-lg border p-3.5 text-left transition-[border-color,background-color] duration-gd-fast"
                  :class="checkedFlags.has(index) ? 'border-success-border bg-success-bg' : 'border-hairline bg-surface-raised hover:border-primary-300'"
                  role="checkbox"
                  :aria-checked="checkedFlags.has(index)"
                  @click="toggleFlag(index)"
                >
                  <!-- 勾选框 -->
                  <span
                    class="mt-0.5 flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-md border transition-colors duration-gd-fast"
                    :class="checkedFlags.has(index) ? 'border-success gd-fill-mint' : 'border-hairline-strong bg-surface-raised'"
                    aria-hidden="true"
                  >
                    <svg v-if="checkedFlags.has(index)" class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                      <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </span>
                  <span class="min-w-0 flex-1">
                    <span class="block text-sm leading-relaxed" :class="checkedFlags.has(index) ? 'text-success-strong' : 'text-neutral-700'">{{ flag.message }}</span>
                    <span class="gd-num mt-0.5 block text-caption text-neutral-400">severity: {{ flag.severity }}</span>
                  </span>
                </button>
                <!-- block：红字无勾选（无绕过入口，流程终止） -->
                <div v-else class="flex w-full items-start gap-3 rounded-lg border border-danger-border bg-danger-bg p-3.5" role="alert">
                  <span class="gd-num mt-0.5 flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full gd-fill-rose" aria-hidden="true">
                    <svg class="h-2.5 w-2.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                      <path d="M6 6l12 12M18 6L6 18" stroke-linecap="round" />
                    </svg>
                  </span>
                  <span class="min-w-0 flex-1">
                    <span class="block text-sm font-medium leading-relaxed text-danger">{{ flag.message }}</span>
                    <span class="gd-num mt-0.5 block text-caption text-danger">severity: <b class="font-semibold">block</b> · 无绕过入口，流程终止</span>
                  </span>
                </div>
              </li>
            </ul>
            <p v-else class="mt-3 rounded-lg bg-surface-canvas p-4 text-xs leading-relaxed text-neutral-400">
              合规审核未发现阻断问题，或报告未随审批单返回；确认前请人工复核剧情内容。
            </p>
            <!-- 勾选进度提示（对照 F2-05「其余 N 项通过 review 项逐条处理后不再刷新」） -->
            <p v-if="moderationFlags.length > 0 && !hasBlockFlag && !allFlagsChecked" class="mt-3 text-caption text-warning">
              还有 {{ nonBlockFlagIndexes.length - checkedFlags.size }} 项未确认，全部勾选后可提交审批
            </p>
          </section>

          <!-- 审批单信息 -->
          <section class="gd-card mt-4 p-6">
            <h3 class="font-serif text-title text-neutral-900">审批单信息</h3>
            <dl class="mt-3 space-y-2 text-sm">
              <div class="flex justify-between">
                <dt class="text-neutral-500">单号</dt>
                <dd class="gd-num text-neutral-800">#{{ selectedItem?.id }}</dd>
              </div>
              <div class="flex justify-between">
                <dt class="text-neutral-500">目标剧本</dt>
                <dd class="gd-num text-neutral-800">{{ selectedItem?.target_type || 'script' }} #{{ selectedItem?.target_id ?? '—' }}</dd>
              </div>
              <div class="flex justify-between">
                <dt class="text-neutral-500">有效期至</dt>
                <dd class="gd-num text-neutral-800">{{ selectedItem?.expires_at ? formatShortDate(selectedItem.expires_at) : '—' }}</dd>
              </div>
            </dl>
          </section>
        </aside>
      </div>

      <!-- ===== 底部 ApprovalBar 72px 吸底（对照 F2-05：通过 / 改回并反馈 / 编辑后重审） ===== -->
      <footer class="flex h-footer-bar shrink-0 items-center justify-end gap-3 border-t border-hairline bg-surface-raised px-6">
        <p class="mr-auto text-xs" :class="hasBlockFlag ? 'text-danger' : !allFlagsChecked ? 'text-warning' : 'text-neutral-400'">
          <template v-if="hasBlockFlag">存在 block 项：先处理上方红色条目</template>
          <template v-else-if="!allFlagsChecked">还有 {{ nonBlockFlagIndexes.length - checkedFlags.size }} 项未确认</template>
          <template v-else>审核通过 · 无 block 项，可以提交关口一</template>
        </p>
        <ApprovalBar :loading="deciding" :approve-disabled="hasBlockFlag || !allFlagsChecked" approve-text="通过 · 进入资产制作" reject-text="驳回" edit-text="编辑后重审" @approve="openApproveConfirm" @reject="handleReject" @edit="goEditor" />
      </footer>
    </template>

    <!-- ============ approve 二次确认 ============ -->
    <ConfirmDialog
      v-model="approveDialogOpen"
      title="确认通过该剧本？"
      content="通过后流程将继续推进（资产准备），如需调整内容请选择「驳回」并填写反馈。"
      confirm-label="确认通过"
      :loading="deciding"
      @confirm="confirmApprove"
      @cancel="approveDialogOpen = false"
    />
  </div>
</template>

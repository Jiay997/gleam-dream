<!--
  文件级注释：P06 · 剧本编辑器（路由 /projects/:projectId/script，AppShell 布局）。
  v8 剧本工坊 · 按 stitch_prd/p2（原型 code.html + screen.png）Redesign-Preserve 对齐功能布局：
  - 顶部工作台控制中枢两行：Row1 创作模式 segmented（一句话生成 / 长篇小说与剧本解析 / 自由创作）
    + VERSION 徽标（版本号 · 当前审核中 · Diff 对比）+ 撤销重做 / 历史快照 / 导出文档（无后端能力，
    按原型保留结构、禁用展示）+ 保存状态指示；Row2 集数索引 chips（每集就绪 / 待校正状态 +
    场次浮层导航，承接原左树定位功能）+ 待分配集数余量；
  - 主体双画布：左 ≈70% 手稿区（预检报告横幅 → 手稿卡眉头 EPISODE MANUSCRIPT · 衬线大标题 ·
    核心冲突 · 统计行 → SCENE 场块 ribbon + 角色权重 pills + 舞台动作指示块 + 台词卡 +
    低置信琥珀纠偏卡 + 集尾悬念钩子卡），右 ≈30% AI 编剧助手夜窗面板（Online 徽标绑 SSE 状态 ·
    WRITE_TODOS 任务规划 · 局部改写指令台 + 提示锦囊 · 安全护栏 · 解析置信度 / 版本对比 Tab）；
  - 底部固定 HITL 关口条：关口徽标 + Awaiting Human Approval / 编辑中徽标 + 统计 + 三按钮
    （申请局部重写 / 手动锁定禁用位 / 批准剧本并进入资产规划 = 提交审核，待审单 > 0 时改为前往审批台）；
  - 主题风格沿用 aurora 令牌与 gd-* 体系，未引入新色值 / 字体 / 依赖；
  - 既有功能全量保留：三态加载 / 错误重试 / 生成进度模式（SSE + 轮询兜底 + write_todos）/
    行内编辑 PATCH / 局部改写弹窗 / 上传解析轮询 / 提交审核 / 锚点定位 / 一句话生成弹窗；
  - 原型件的映射差异（radio 候选选项 / TTS 试听 / 插入台词行 / 历史快照与导出等）见重构报告。
-->
<script setup lang="ts">
/**
 * ScriptEditorView 剧本编辑器（剧本工坊）。
 * props：无。emits：无。
 * 依赖 project store、useSSE（progress/approval.created/run.*）、useToast。
 * 布局按 stitch_prd/p2 对齐：顶部控制中枢两行 + 左手稿 / 右 AI 夜窗双画布 + 底部 HITL 关口条。
 */
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { listApprovals } from '@/api/approvals'
import { ApiError } from '@/api/http'
import { getProject } from '@/api/projects'
import {
  generateScript,
  getDocumentConfidenceItems,
  getProjectCurrentScript,
  getScriptDiff,
  getScriptDocument,
  rewriteScript,
  submitScriptReview,
  updateScriptLine,
  uploadScriptDocument,
  type DiffRow,
  type EpisodeNode,
  type SceneNode,
  type ScriptLineNode,
  type ScriptTree,
} from '@/api/script'
import { listStoryboards } from '@/api/storyboard'
import { listScriptRuns, listScriptVersions, restoreScriptVersion, type ScriptVersionSummary } from '@/api/v4'
import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import Modal from '@/components/common/Modal.vue'
import SegmentedControl from '@/components/common/SegmentedControl.vue'
import { useSSE } from '@/composables/useSSE'
import { useUndoStack } from '@/composables/useUndoStack'
import { useToast } from '@/composables/useToast'
import { GENRE_OPTIONS, STYLE_OPTIONS } from '@/constants/options'
import { useProjectStore } from '@/stores/project'
import { formatSceneHeading } from '@/utils/sceneText'

/** 路由实例（锚点 query / 去审批跳转） */
const route = useRoute()
const router = useRouter()
/** Toast */
const toast = useToast()
/** 项目 store（当前项目） */
const projectStore = useProjectStore()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))
/** 当前项目（store 已由路由守卫加载） */
const project = computed(() => projectStore.currentProject)

/** 低置信阈值（<0.75 黄底高亮） */
const LOW_CONFIDENCE = 0.75

/* ==================== 剧本树数据 ==================== */

/** 剧本树（null 表示未加载 / 无剧本） */
const scriptTree = ref<ScriptTree | null>(null)
/** 树加载中 */
const treeLoading = ref(true)
/** 树加载失败信息（空串表示正常） */
const treeError = ref('')
/** 逐行置信度 Map（line_id → confidence，来自 confidence-items） */
const confidenceMap = ref<Map<string, number>>(new Map())

/** 场次视图（打平行号后的渲染模型） */
interface SceneView {
  /** 场次 ID */
  id: string | number
  /** 场次号 */
  sceneNo: number
  /** 场头文案 */
  heading: string
  /** 行渲染列表（line + 全局行号） */
  lines: Array<{ line: ScriptLineNode; no: number }>
}
/** 剧集视图 */
interface EpisodeView {
  /** 剧集 ID */
  id: string | number
  /** 集号 */
  episodeNo: number
  /** 集标题 */
  title: string
  /** 一句话梗概（集眉头「核心冲突」行） */
  logline?: string | null
  /** 结尾钩子（集尾悬念钩子卡数据源） */
  hookText?: string | null
  /** 目标时长秒数（无分镜时的预估成片时长回退数据源） */
  targetSeconds?: number | null
  /** 分镜实际总时长（毫秒；已有分镜板时作为预估成片时长第一口径，P1-7） */
  storyboardDurationMs?: number | null
  /** 场次列表 */
  scenes: SceneView[]
}

/**
 * 将后端剧本树折叠为渲染视图模型（打平全局行号、归一场头文案、透传集级梗概 / 钩子 / 时长）。
 * @param tree 剧本树
 * @param storyboardDurations 集号 → 分镜实际总时长（毫秒，P1-7 时长口径统一）
 * @returns 剧集视图列表
 */
function buildTreeView(
  tree: ScriptTree,
  storyboardDurations: Map<number, number> = new Map(),
): EpisodeView[] {
  const episodes: EpisodeNode[] = Array.isArray(tree.episodes) ? tree.episodes : []
  let lineNo = 0
  return episodes.map((ep, epIndex) => {
    const episodeNo = Number(ep.episode_no ?? ep.index ?? epIndex + 1)
    const scenes: SceneNode[] = Array.isArray(ep.scenes) ? ep.scenes : []
    return {
      id: ep.id,
      episodeNo,
      title: ep.title || `第 ${episodeNo} 集`,
      logline: ep.logline ?? null,
      hookText: ep.hook_text ?? null,
      targetSeconds: typeof ep.target_duration_seconds === 'number' ? ep.target_duration_seconds : null,
      storyboardDurationMs: storyboardDurations.get(episodeNo) ?? null,
      scenes: scenes.map((scene, scIndex) => {
        const sceneNo = Number(scene.scene_no ?? scene.index ?? scIndex + 1)
        const lines = Array.isArray(scene.lines) ? scene.lines : []
        return {
          id: scene.id,
          sceneNo,
          // 修复（测试报告问题 1）：场头中的 ext/int/night 等英文枚举统一本地化，
          // 避免中文界面出现「第 1 场 · ext · … · night」。
          heading: formatSceneHeading(scene, `第 ${sceneNo} 场`),
          lines: lines.map((line) => {
            lineNo += 1
            return { line, no: lineNo }
          }),
        }
      }),
    }
  })
}

/** 集号 → 分镜实际总时长（毫秒） */
const storyboardDurations = ref<Map<number, number>>(new Map())

/**
 * 拉取项目分镜板时长（失败静默，回退集目标时长/字数估算）。
 * 目的：剧本页「预估成片时长」与分镜页、成片时间线使用同一实际口径（P1-7）。
 * @returns 无
 */
async function loadStoryboardDurations(): Promise<void> {
  try {
    const list = await listStoryboards(projectId.value)
    const map = new Map<number, number>()
    for (const board of list) {
      const episodeNo = Number(board.episode_no)
      const durationMs = Number(board.estimated_duration_ms)
      if (Number.isFinite(episodeNo) && durationMs > 0) map.set(episodeNo, durationMs)
    }
    storyboardDurations.value = map
  } catch {
    storyboardDurations.value = new Map()
  }
}

/** 渲染视图（空树为空数组；分镜时长变化时同步重算） */
const treeView = computed<EpisodeView[]>(() =>
  scriptTree.value ? buildTreeView(scriptTree.value, storyboardDurations.value) : [],
)

/** 剧本版本号 */
const scriptVersion = computed(() => scriptTree.value?.script.version_no ?? scriptTree.value?.script.version ?? 1)

/** 剧本总字数（正文累计） */
const wordCount = computed(() => {
  let count = 0
  for (const ep of treeView.value) {
    for (const scene of ep.scenes) {
      for (const item of scene.lines) {
        count += (item.line.content ?? '').length
      }
    }
  }
  return count
})

/** 主要角色集合（dialogue 说话人去重，供 HITL 统计展示） */
const mainCharacters = computed(() => {
  const set = new Set<string>()
  for (const ep of treeView.value) {
    for (const scene of ep.scenes) {
      for (const item of scene.lines) {
        const speaker = lineSpeaker(item.line)
        if (speaker) set.add(speaker)
      }
    }
  }
  return Array.from(set)
})

/** 低置信行视图（预检横幅 / 夜窗置信度卡数据源：行号 + 置信度 + 内容摘要） */
const lowConfidenceLines = computed(() => {
  const items: Array<{ id: string | number; no: number; confidence: number; content: string }> = []
  for (const ep of treeView.value) {
    for (const scene of ep.scenes) {
      for (const item of scene.lines) {
        const conf = lineConfidence(item.line)
        if (conf !== null && conf < LOW_CONFIDENCE) {
          items.push({ id: item.line.id, no: item.no, confidence: conf, content: item.line.content ?? '' })
        }
      }
    }
  }
  return items
})

/**
 * 从右栏置信度卡定位到正文对应行（平滑滚动居中）。
 * @param lineId 行 ID
 * @returns 无
 */
function locateLine(lineId: string | number): void {
  document.getElementById(`line-${lineId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

/* ==================== 工具函数（行渲染归一化） ==================== */

/**
 * 归一化行类型（兼容 line_type / type 两种字段名）。
 * @param line 剧本行
 * @returns 行类型（dialogue/narration/action/stage_direction）
 */
function lineType(line: ScriptLineNode): string {
  return line.line_type ?? line.type ?? 'action'
}

/**
 * 归一化说话人（兼容 speaker / character 两种字段名）。
 * @param line 剧本行
 * @returns 说话人（可空）
 */
function lineSpeaker(line: ScriptLineNode): string | null {
  return line.speaker ?? line.character ?? null
}

/**
 * 查询行置信度：优先行内 meta，其次 confidence-items Map。
 * @param line 剧本行
 * @returns 置信度（无数据返回 null）
 */
function lineConfidence(line: ScriptLineNode): number | null {
  if (typeof line.confidence === 'number') return line.confidence
  return confidenceMap.value.get(String(line.id)) ?? null
}

/**
 * 行是否低置信（< 0.75）。
 * @param line 剧本行
 * @returns 是否低置信
 */
function isLowConfidence(line: ScriptLineNode): boolean {
  const conf = lineConfidence(line)
  return conf !== null && conf < LOW_CONFIDENCE
}

/* ==================== 树加载 ==================== */

/**
 * 拉取当前剧本树 + 待办审批数。
 * @returns 无
 */
async function fetchTree(): Promise<void> {
  treeLoading.value = true
  treeError.value = ''
  try {
    scriptTree.value = await getProjectCurrentScript(projectId.value)
    // P1-7：同步分镜实际时长，供「预估成片时长」与分镜页保持一致口径
    await loadStoryboardDurations()
  } catch (error) {
    scriptTree.value = null
    // 10002「项目/剧本不存在」在首进项目时是正常空态，不应展示成加载失败并诱导反复重试
    if (error instanceof ApiError && error.code === 10002) {
      treeError.value = ''
    } else {
      treeError.value = error instanceof Error ? error.message : '剧本加载失败'
    }
  } finally {
    treeLoading.value = false
  }
  await nextTick()
  scrollToQueryAnchor()
}

/**
 * 按 query 锚点（episode/scene）滚动定位到对应场次。
 * @returns 无
 */
function scrollToQueryAnchor(): void {
  const ep = Number(route.query.episode)
  const sc = Number(route.query.scene)
  if (!Number.isFinite(ep) || ep <= 0) return
  const targetId = Number.isFinite(sc) && sc > 0 ? `scene-${ep}-${sc}` : `ep-${ep}`
  const el = document.getElementById(targetId)
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  if (el) activeSceneKey.value = targetId
}

/* ==================== 集 / 场导航（集数索引 chips） ==================== */

/** 当前高亮场次锚点 key（scene-{ep}-{sc}） */
const activeSceneKey = ref('')

/**
 * 点击场次（浮层 / 锚点）：滚动到正文锚点并高亮。
 * @param episodeNo 集号
 * @param sceneNo 场次号
 * @returns 无
 */
function locateScene(episodeNo: number, sceneNo: number): void {
  const key = `scene-${episodeNo}-${sceneNo}`
  activeSceneKey.value = key
  document.getElementById(key)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/**
 * 点击集数 chip：滚动到集锚点（场次定位走浮层）。
 * @param episodeNo 集号
 * @returns 无
 */
function locateEpisode(episodeNo: number): void {
  activeSceneKey.value = ''
  document.getElementById(`ep-${episodeNo}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** 展开场次浮层的集 ID（0 表示全部收起） */
const openEpMenuId = ref<string | number>(0)

/**
 * 切换某集的场次浮层。
 * @param epId 集 ID
 * @returns 无
 */
function toggleEpMenu(epId: string | number): void {
  openEpMenuId.value = openEpMenuId.value === epId ? 0 : epId
}

/** 当前激活集号（由 activeSceneKey 推导，用于 chip 高亮） */
const activeEpisodeNo = computed(() => {
  const match = /^scene-(\d+)-/.exec(activeSceneKey.value)
  return match ? Number(match[1]) : 0
})

/* ==================== 行内编辑 ==================== */

/** 正在编辑的行 ID（空串表示无） */
const editingLineId = ref<string>('')
/** 编辑草稿内容 */
const editDraft = ref('')
/** 保存状态：idle 空闲 / saving 保存中 / saved 已保存 */
const saveState = ref<'idle' | 'saving' | 'saved'>('idle')
/** saved 态回退定时器 */
let savedTimer: ReturnType<typeof setTimeout> | null = null

/* ---------- V4.0 P0 SF-F2-13：编辑器撤销/重做 + 草稿自动保存（AC-F2-07-2） ---------- */

/** 一次已保存的行编辑记录（撤销栈元素） */
interface LineEditRecord {
  /** 行 ID */
  lineId: string
  /** 变更前内容 */
  before: string
  /** 变更后内容 */
  after: string
  /** 发生时间（毫秒时间戳） */
  at: number
}

/** 撤销/重做栈元素：整份编辑记录列表（快照式，回放一定正确） */
type EditHistorySnapshot = LineEditRecord[]

/** 撤销/重做栈：步骤上限 100（对齐 AC-F2-07-2） */
const editHistory = useUndoStack<EditHistorySnapshot>([], {
  limit: 100,
  autosaveKey: computed(() => `gd:script:draft:${projectId.value}:${scriptVersion.value}`).value,
  autosaveIntervalMs: 10_000,
})

/** 会话内已保存编辑记录（用于撤销/重做与草稿恢复） */
const editRecords = ref<LineEditRecord[]>([])

/** 草稿恢复提示条是否可见 */
const draftBannerVisible = ref(false)
/** 草稿保存时间（用于提示条文案） */
const draftSavedAt = ref<number | null>(null)

/**
 * 在剧本树视图中定位行节点（撤销/重做就地回填内容）。
 * @param lineId 行 ID
 * @returns 行节点；不存在返回 null
 */
function findLineNode(lineId: string): ScriptLineNode | null {
  for (const episode of treeView.value) {
    for (const scene of episode.scenes) {
      const hit = scene.lines.find((item) => String(item.line.id) === lineId)
      if (hit) return hit.line
    }
  }
  return null
}

/**
 * 把编辑记录列表应用到视图（就地改写行内容）。
 * @param records 编辑记录列表
 * @returns 无
 */
function applyRecords(records: LineEditRecord[]): void {
  for (const record of records) {
    const node = findLineNode(record.lineId)
    if (node) node.content = record.after
  }
}

/**
 * 撤销一步（回填上一版本内容，需再次保存才会落库）。
 * @returns 无
 */
function undoEdit(): void {
  if (editRecords.value.length === 0) {
    toast.info('没有可撤销的编辑')
    return
  }
  const removed = editRecords.value[editRecords.value.length - 1]
  const snapshot = editHistory.undo()
  if (snapshot === null) return
  editRecords.value = snapshot
  applyRecords(snapshot)
  // 被撤销的那条：该行内容还原为变更前
  const node = findLineNode(removed.lineId)
  if (node) node.content = removed.before
  toast.success(
    `已撤销一步（剩余 ${snapshot.length} 步）；再次保存后才会落库`,
  )
}

/**
 * 重做一步（回填 after 内容）。
 * @returns 无
 */
function redoEdit(): void {
  const snapshot = editHistory.redo()
  if (snapshot === null) {
    toast.info('没有可重做的编辑')
    return
  }
  editRecords.value = snapshot
  applyRecords(snapshot)
  toast.success('已重做一步；再次保存后才会落库')
}

/**
 * 启动草稿自动保存（每 10s 落一次 localStorage）。
 * @returns 无
 */
function startDraftAutosave(): void {
  editHistory.startAutosave(() => editRecords.value)
}

/**
 * 恢复草稿（把草稿记录回填到视图）。
 * @returns 无
 */
function restoreDraft(): void {
  const draft = editHistory.readDraft()
  if (!draft) {
    toast.warning('没有可恢复的草稿')
    return
  }
  editRecords.value = draft.state
  applyRecords(draft.state)
  draftBannerVisible.value = false
  toast.success(`已恢复 ${draft.state.length} 条草稿编辑；请逐条保存后落库`)
}

/**
 * 丢弃草稿。
 * @returns 无
 */
function discardDraft(): void {
  editHistory.clearDraft()
  draftBannerVisible.value = false
  draftSavedAt.value = null
  toast.info('草稿已丢弃')
}

/**
 * 注册编辑器快捷键：Ctrl/Cmd+Z 撤销、Ctrl/Cmd+Shift+Z（或 Ctrl+Y）重做。
 * 输入框内交由原生撤销处理（避免双栈冲突）。
 * @param event 键盘事件
 * @returns 无
 */
function onEditorKeydown(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA')) return
  const modifier = event.ctrlKey || event.metaKey
  if (!modifier) return
  const key = event.key.toLowerCase()
  if (key === 'z' && !event.shiftKey) {
    event.preventDefault()
    undoEdit()
  } else if ((key === 'z' && event.shiftKey) || key === 'y') {
    event.preventDefault()
    redoEdit()
  }
}

/**
 * 进入行内编辑。
 * @param line 目标行
 * @returns 无
 */
function startEdit(line: ScriptLineNode): void {
  if (saveState.value === 'saving') return
  editingLineId.value = String(line.id)
  editDraft.value = line.content ?? ''
}

/**
 * 取消行内编辑。
 * @returns 无
 */
function cancelEdit(): void {
  editingLineId.value = ''
  editDraft.value = ''
}

/**
 * 保存行内编辑（PATCH /script-lines/{id}，就地的更新树数据并展示保存状态）。
 * 成功后把「前后内容」压入撤销栈（会话内可撤销 ≥100 步）。
 * @param line 目标行
 * @returns 无
 */
async function saveEdit(line: ScriptLineNode): Promise<void> {
  const content = editDraft.value.trim()
  if (!content) {
    toast.warning('行内容不能为空')
    return
  }
  const before = line.content ?? ''
  saveState.value = 'saving'
  try {
    const updated = await updateScriptLine(line.id, { content })
    line.content = updated.content ?? content
    editingLineId.value = ''
    saveState.value = 'saved'
    if (before !== line.content) {
      const record: LineEditRecord = {
        lineId: String(line.id),
        before,
        after: line.content ?? content,
        at: Date.now(),
      }
      editRecords.value = [...editRecords.value, record]
      editHistory.push(editRecords.value)
    }
    if (savedTimer) clearTimeout(savedTimer)
    savedTimer = setTimeout(() => {
      saveState.value = 'idle'
    }, 2000)
  } catch {
    // 拦截器已 toast；失败回退空闲态，保留编辑框由用户重试
    saveState.value = 'idle'
  }
}

/* ==================== 局部改写（弹窗 + 夜窗指令台共用） ==================== */

/** 改写弹窗显示 */
const rewriteOpen = ref(false)
/** 改写目标描述（如「第 2 集 · 第 3 场」） */
const rewriteScopeLabel = ref('')
/** 改写目标集号 */
const rewriteEpisodeNo = ref(0)
/** 改写目标场次号（0 表示整集） */
const rewriteSceneNo = ref(0)
/** 改写反馈输入 */
const rewriteFeedback = ref('')
/** 改写提交中 */
const rewriteSubmitting = ref(false)

/**
 * 打开改写弹窗（场次级 / 集级）。
 * @param episodeNo 集号
 * @param sceneNo 场次号（可空表示整集）
 * @returns 无
 */
function openRewrite(episodeNo: number, sceneNo?: number): void {
  rewriteEpisodeNo.value = episodeNo
  rewriteSceneNo.value = sceneNo ?? 0
  rewriteScopeLabel.value = sceneNo ? `第 ${episodeNo} 集 · 第 ${sceneNo} 场` : `第 ${episodeNo} 集（整集）`
  rewriteFeedback.value = ''
  rewriteOpen.value = true
}

/**
 * 夜窗指令台范围兜底：未从场次入口进入时默认第一集整集。
 * @returns 无
 */
function ensureRewriteScope(): void {
  if (rewriteEpisodeNo.value > 0) return
  const first = treeView.value[0]
  if (!first) return
  rewriteEpisodeNo.value = first.episodeNo
  rewriteSceneNo.value = 0
  rewriteScopeLabel.value = `第 ${first.episodeNo} 集（整集）`
}

/**
 * 指令台提交：兜底默认范围后复用既有 submitRewrite 流程。
 * @returns 无
 */
async function consoleSubmitRewrite(): Promise<void> {
  ensureRewriteScope()
  await submitRewrite()
}

/**
 * 提交局部改写（POST /projects/{id}/scripts/rewrite，feedback ≥5 字）。
 * @returns 无
 */
async function submitRewrite(): Promise<void> {
  const feedback = rewriteFeedback.value.trim()
  if (feedback.length < 5) {
    toast.warning('请填写至少 5 个字的改写要求')
    return
  }
  rewriteSubmitting.value = true
  try {
    await rewriteScript(projectId.value, {
      episode_no: rewriteEpisodeNo.value,
      scene_no: rewriteSceneNo.value > 0 ? rewriteSceneNo.value : undefined,
      feedback,
    })
    toast.success('改写已提交，正在刷新剧本')
    rewriteOpen.value = false
    await fetchTree()
  } catch {
    // 拦截器已 toast
  } finally {
    rewriteSubmitting.value = false
  }
}

/* ==================== 一句话生成 ==================== */

/** 生成弹窗显示 */
const generateOpen = ref(false)
/** 创意输入（预填项目简介） */
const generateIdea = ref('')
/** 生成提交中 */
const generateSubmitting = ref(false)

/**
 * 打开一句话生成弹窗（创意预填项目一句话简介）。
 * @returns 无
 */
function openGenerate(): void {
  generateIdea.value = project.value?.synopsis ?? ''
  generateOpen.value = true
}

/**
 * 提交一句话生成（POST /orchestration/projects/{id}/script-generate → 202 受理进入进度模式）。
 * @returns 无
 */
async function submitGenerate(): Promise<void> {
  if (generateIdea.value.trim().length < 5) {
    toast.warning('请输入至少 5 个字的创意描述')
    return
  }
  generateSubmitting.value = true
  try {
    const accepted = await generateScript(projectId.value, generateIdea.value.trim())
    generateOpen.value = false
    acceptedCredits.value = accepted.estimated_credits ?? 0
    enterProgressMode()
    toast.info(accepted.message || '剧本生成已受理，正在后台执行')
  } catch {
    // 拦截器已 toast（40001 并发冲突 / 状态不允许等）
  } finally {
    generateSubmitting.value = false
  }
}

/* ==================== 生成进度模式 ==================== */

/** 生成步骤定义（key 与后端 progress 事件 step 对齐） */
const GENERATE_STEPS: Array<{ key: string; label: string }> = [
  { key: 'billing_precheck', label: '预算预检' },
  { key: 'generate_outline', label: '故事大纲' },
  { key: 'generate_episodes', label: '分集与对白' },
  { key: 'compliance_review', label: '合规审核' },
  { key: 'script_persisted', label: '剧本入库' },
]

/** 是否处于生成进度模式 */
const progressMode = ref(false)
/** 进度百分比（0~100） */
const progressPercent = ref(0)
/** 已完成步骤 key 集合（有序，按事件到达） */
const doneSteps = ref<string[]>([])
/** 当前进行中的步骤 key */
const activeStepKey = ref('')
/** 生成失败信息（空串表示未失败） */
const progressError = ref('')
/** 上次生成失败原因（从 project.config 持久化读取，刷新后仍可见） */
const lastScriptFailure = computed(() => {
  const cfg = (projectStore.currentProject?.config ?? {}) as Record<string, unknown>
  const failure = cfg['_last_script_failure'] as Record<string, unknown> | undefined
  if (!failure) return ''
  const message = String(failure.message ?? '').trim()
  return message || '上次剧本生成失败，请重试'
})
/** 受理预估积分（202 返回） */
const acceptedCredits = ref(0)
/** 已完成集数（运行态回填，用于进度标签显示真实集号） */
const progressCompletedEpisodes = ref(0)
/** 目标总集数（运行态回填） */
const progressTotalEpisodes = ref(0)

/**
 * 进度标签应显示的集号（修复 E2E 报告问题 6）。
 * 此前恒显示硬编码的 Ep.01，即使已完成 5/10 集仍显示「Ep.01 生成中」。
 * @returns 正在生成的集号（1 起；无数据时回退 1）
 */
const runningEpisodeNo = computed(() => {
  const total = Math.max(1, progressTotalEpisodes.value)
  return Math.min(progressCompletedEpisodes.value + 1, total)
})
/** 进度兜底轮询定时器 */
let progressTimer: ReturnType<typeof setInterval> | null = null

/**
 * 进入生成进度模式（重置步骤状态并启动状态轮询兜底）。
 * @returns 无
 */
function enterProgressMode(): void {
  progressMode.value = true
  progressPercent.value = 0
  doneSteps.value = []
  activeStepKey.value = GENERATE_STEPS[0].key
  progressError.value = ''
  stopProgressTimer()
  // SSE 不可用时兜底：3s 轮询项目状态，scripting → script_review 即完成
  progressTimer = setInterval(() => {
    void pollProjectStatus()
  }, 3000)
}

/**
 * 停止进度轮询定时器。
 * @returns 无
 */
function stopProgressTimer(): void {
  if (progressTimer) {
    clearInterval(progressTimer)
    progressTimer = null
  }
}

/**
 * 轮询项目状态判断生成是否完成 / 失败（SSE 兜底）。
 * 同时拉取 script runs 运行态，用计划节点状态与进度百分比兜底推进
 * WRITE_TODOS 面板（P2-2 修复：SSE 不可用时进度不再静止在 0/5）。
 * @returns 无
 */
async function pollProjectStatus(): Promise<void> {
  try {
    // 运行态兜底：plan 节点状态驱动 WRITE_TODOS，progress_percent 推进主进度
    const runs = await listScriptRuns(projectId.value)
    const active = runs.items.find((r) => r.run_status === 'generating') ?? runs.items[0]
    if (active) {
      const planStatus: Record<string, string> = {}
      for (const node of active.plan ?? []) {
        if (node && typeof node.todo_id === 'string' && typeof node.status === 'string') {
          planStatus[node.todo_id] = node.status
        }
      }
      if (Object.keys(planStatus).length > 0) {
        planNodeStatus.value = { ...planNodeStatus.value, ...planStatus }
        // 与 todo_updated 事件同口径：节点完成映射到主进度步骤
        if (planStatus.outline === 'done' && !doneSteps.value.includes('generate_outline')) {
          doneSteps.value = [...doneSteps.value, 'generate_outline']
        }
        if (planStatus.dialogues === 'done' && !doneSteps.value.includes('generate_episodes')) {
          doneSteps.value = [...doneSteps.value, 'generate_episodes']
        }
      }
      if (active.progress_percent > progressPercent.value) {
        progressPercent.value = active.progress_percent
      }
      // 修复（E2E 报告问题 6）：回填真实集数与已完成集数，进度标签不再硬编码 Ep.01
      progressCompletedEpisodes.value = Number(active.completed_episodes ?? 0)
      progressTotalEpisodes.value = Number(active.total_episodes ?? 0)
    }

    const fresh = await getProject(projectId.value)
    projectStore.setCurrentProject(fresh)
    if (fresh.status === 'script_review') {
      finishProgress()
    } else if (fresh.status === 'failed') {
      progressError.value = lastScriptFailure.value || '生成流程执行失败，请查看活动流或重试'
      stopProgressTimer()
    } else if (fresh.status !== 'scripting' && progressMode.value) {
      // 状态已越出剧本阶段：视为完成并退出进度模式
      finishProgress()
    }
  } catch {
    // 静默：轮询失败等待下一轮
  }
}

/**
 * 处理进度事件：标记步骤完成并推进百分比；同时捕获事件携带的 AI 规划任务清单（write_todos）。
 * @param data progress 事件负载
 * @returns 无
 */
function applyProgressEvent(data: Record<string, unknown>): void {
  const step = String(data.step ?? '')
  const percent = Number(data.progress ?? 0)
  if (step && !doneSteps.value.includes(step)) {
    doneSteps.value.push(step)
  }
  if (percent > progressPercent.value) {
    progressPercent.value = percent
  }
  // 后端事件可能携带 write_todos（AI 规划任务：大纲→人物→分集→对白），优先展示真实清单
  const todos = data.write_todos
  if (Array.isArray(todos) && todos.length > 0) {
    plannedTodos.value = todos
      .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
      .map((item) => ({
        step: Number(item.step ?? 0),
        title: String(item.title ?? `任务 ${item.step ?? ''}`),
        detail: String(item.detail ?? ''),
      }))
  }
  const next = GENERATE_STEPS.find((s) => !doneSteps.value.includes(s.key))
  activeStepKey.value = next?.key ?? ''
}

/** AI 规划任务清单（progress 事件 write_todos 驱动；空表示未收到，回退内置步骤表） */
const plannedTodos = ref<Array<{ step: number; title: string; detail: string }>>([])

/**
 * write_todos 计划节点实时状态（todo_updated 事件 / script runs 轮询驱动）。
 * key 为节点 todo_id（outline/characters/episodes/dialogues），value 为 pending/running/done/failed。
 */
const planNodeStatus = ref<Record<string, string>>({})

/**
 * 处理 todo_updated 事件：记录规划节点状态并映射到主进度步骤。
 * P2-2 修复：WRITE_TODOS 面板不再依赖 SSE progress 的间接推断，节点完成即推进。
 * @param data todo_updated 事件负载
 * @returns 无
 */
function applyTodoUpdate(data: Record<string, unknown>): void {
  const todoId = String(data.todo_id ?? '')
  const status = String(data.status ?? '')
  if (!todoId || !status) return
  planNodeStatus.value = { ...planNodeStatus.value, [todoId]: status }
  // 节点完成同步映射到主进度步骤（驱动步骤条 / 主进度百分比 / 完成判定）
  if (status === 'done') {
    if (todoId === 'outline' && !doneSteps.value.includes('generate_outline')) {
      doneSteps.value = [...doneSteps.value, 'generate_outline']
    }
    if (todoId === 'dialogues' && !doneSteps.value.includes('generate_episodes')) {
      doneSteps.value = [...doneSteps.value, 'generate_episodes']
    }
    const next = GENERATE_STEPS.find((s) => !doneSteps.value.includes(s.key))
    activeStepKey.value = next?.key ?? ''
  }
}

/**
 * 规划任务的完成数量：优先按 todo_updated 事件标记统计（大纲→人物→分集→对白
 * 顺序连续完成数），无事件数据时回退步骤推断。
 * @returns 已完成任务数
 */
const doneTodoCount = computed(() => {
  if (plannedTodos.value.length === 0) return 0
  const statuses = planNodeStatus.value
  if (Object.keys(statuses).length > 0) {
    const order = ['outline', 'characters', 'episodes', 'dialogues']
    let count = 0
    for (const id of order) {
      if (statuses[id] === 'done') count += 1
      else break
    }
    return count
  }
  if (doneSteps.value.includes('generate_episodes')) return plannedTodos.value.length
  if (doneSteps.value.includes('generate_outline')) return plannedTodos.value.length - 1
  return 0
})

/**
 * 完成生成：退出进度模式，刷新树与审批数。
 * @returns 无
 */
function finishProgress(): void {
  stopProgressTimer()
  progressMode.value = false
  progressPercent.value = 100
  toast.success('剧本生成完成，请确认内容')
  void fetchTree()
  void fetchPendingApprovals()
}

/**
 * 手动退出进度模式（后台继续执行，可稍后回来）。
 * @returns 无
 */
function leaveProgressMode(): void {
  stopProgressTimer()
  progressMode.value = false
  void fetchTree()
}

/** 步骤状态：done / active / todo */
function stepState(key: string): 'done' | 'active' | 'todo' {
  if (doneSteps.value.includes(key)) return 'done'
  if (key === activeStepKey.value) return 'active'
  return 'todo'
}

/* ==================== 历史快照 / 导出文档（P2-4 修复：接入真实能力） ==================== */

/** 快照弹窗开关 */
const snapshotOpen = ref(false)
/** 快照列表加载中 */
const snapshotLoading = ref(false)
/** 版本列表 */
const snapshotItems = ref<ScriptVersionSummary[]>([])
/** 正在恢复的版本号（按钮 loading） */
const restoringVersionNo = ref<number | null>(null)

/**
 * 打开历史快照弹窗并拉取版本列表。
 * @returns 无
 */
async function openSnapshots(): Promise<void> {
  snapshotOpen.value = true
  snapshotLoading.value = true
  try {
    const page = await listScriptVersions(projectId.value)
    snapshotItems.value = page.items
  } catch {
    // 拦截器已 toast，列表保持为空展示「暂无快照」
  } finally {
    snapshotLoading.value = false
  }
}

/**
 * 恢复到指定版本（确认后调用后端 restore，成功后刷新剧本树）。
 * @param version 目标版本
 * @returns 无
 */
async function restoreSnapshot(version: ScriptVersionSummary): Promise<void> {
  if (version.is_current) {
    toast.info('该版本已是当前版本')
    return
  }
  const ok = window.confirm(`确认恢复到 v${version.version_no}？当前未保存的编辑将丢失。`)
  if (!ok) return
  restoringVersionNo.value = version.version_no
  try {
    await restoreScriptVersion(projectId.value, version.version_no)
    toast.success(`已恢复到 v${version.version_no}`)
    snapshotOpen.value = false
    await fetchTree()
  } catch {
    // 拦截器已 toast，此处仅兜底捕获避免未处理拒绝
  } finally {
    restoringVersionNo.value = null
  }
}

/**
 * 将当前剧本树导出为 Markdown 文件（纯前端生成，Blob 下载）。
 * @returns 无
 */
function exportScriptMarkdown(): void {
  const tree = scriptTree.value
  if (!tree) {
    toast.warning('暂无剧本可导出，请先生成或导入剧本')
    return
  }
  const meta = tree.script
  const parts: string[] = []
  parts.push(`# ${meta.title ?? '未命名剧本'}`)
  if (meta.logline) parts.push(`> ${meta.logline}`)
  for (const ep of tree.episodes ?? []) {
    const epNo = ep.episode_no ?? ep.index ?? ''
    parts.push(`\n## 第${epNo}集 ${ep.title ?? ''}`.trimEnd())
    if (ep.logline) parts.push(`*梗概：${ep.logline}*`)
    for (const scene of ep.scenes ?? []) {
      const sceneNo = scene.scene_no ?? scene.index ?? ''
      // 导出与界面同口径：场头英文枚举同样本地化（测试报告问题 1）
      parts.push(`\n### ${sceneNo ? `第${sceneNo}场 ` : ''}${formatSceneHeading(scene, '')}`.trimEnd())
      if (scene.summary) parts.push(`*${scene.summary}*`)
      for (const line of scene.lines ?? []) {
        const speaker = line.speaker ?? line.character
        const type = line.line_type ?? line.type
        if (type === 'narration' || type === 'action' || type === 'stage_direction' || !speaker) {
          parts.push(line.content ?? '')
        } else {
          parts.push(`**${speaker}**${line.emotion ? `（${line.emotion}）` : ''}：${line.content ?? ''}`)
        }
      }
    }
  }
  const blob = new Blob([parts.join('\n\n')], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${meta.title ?? '剧本'}-v${meta.version_no ?? 'current'}.md`
  a.click()
  URL.revokeObjectURL(url)
  toast.success('剧本已导出为 Markdown')
}

/* ==================== SSE 订阅 ==================== */

/** 项目级 SSE：progress 驱动进度模式，todo_updated 驱动 WRITE_TODOS，approval.created / run.* 驱动完成与刷新 */
const { status: sseStatus } = useSSE(`/api/v1/projects/${projectId.value}/events`, {
  eventTypes: ['progress', 'todo_updated', 'approval.created', 'run.completed', 'run.failed', 'status_changed'],
  onMessage: (event) => {
    let data: Record<string, unknown> = {}
    try {
      data = JSON.parse(event.data) as Record<string, unknown>
    } catch {
      data = {}
    }
    if (event.type === 'progress' && progressMode.value) {
      let data: Record<string, unknown> = {}
      try {
        data = JSON.parse(event.data) as Record<string, unknown>
      } catch {
        data = {}
      }
      applyProgressEvent(data)
      return
    }
    if (event.type === 'todo_updated' && progressMode.value) {
      // P2-2 修复：WRITE_TODOS 节点完成事件驱动面板与主进度推进
      applyTodoUpdate(data)
      return
    }
    if (event.type === 'run.failed' && progressMode.value) {
      progressError.value = String(data.message ?? '生成流程执行失败')
      stopProgressTimer()
      return
    }
    if ((event.type === 'run.completed' || event.type === 'approval.created') && progressMode.value) {
      finishProgress()
      return
    }
    if (event.type === 'approval.created') {
      void fetchPendingApprovals()
    }
    if (event.type === 'status_changed') {
      void fetchPendingApprovals()
    }
  },
})

/* ==================== 上传解析 ==================== */

/** 隐藏文件选择框引用 */
const fileInput = ref<HTMLInputElement | null>(null)
/** 上传解析中（文档 ID） */
const parsingDocId = ref<string>('')
/** 最近一次上传的源文件名（预检横幅展示） */
const lastSourceName = ref('')
/** 解析轮询定时器 */
let uploadTimer: ReturnType<typeof setInterval> | null = null
/** 解析轮询次数上限（2s × 150 = 5 分钟） */
const UPLOAD_POLL_MAX = 150

/**
 * 触发文件选择框。
 * @returns 无
 */
function pickFile(): void {
  fileInput.value?.click()
}

/**
 * 处理文件选择：上传 → 轮询解析状态直至 parsed / failed。
 * @param event input change 事件
 * @returns 无
 */
function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  const lower = file.name.toLowerCase()
  if (!lower.endsWith('.txt') && !lower.endsWith('.md')) {
    toast.warning('仅支持 txt / md 格式文件')
    return
  }
  lastSourceName.value = file.name
  void handleUpload(file)
}

/**
 * 上传并轮询解析（POST upload → setInterval 轮询 GET /documents/{id}）。
 * @param file 用户选择的文件
 * @returns 无
 */
async function handleUpload(file: File): Promise<void> {
  try {
    const doc = await uploadScriptDocument(projectId.value, file)
    if (doc?.id === undefined || doc?.id === null) {
      toast.warning('上传响应缺少文档 ID，无法跟踪解析进度')
      return
    }
    parsingDocId.value = String(doc.id)
    toast.info('文档已上传，正在解析…')
    let polls = 0
    if (uploadTimer) clearInterval(uploadTimer)
    uploadTimer = setInterval(() => {
      void checkParseStatus(String(doc.id), ++polls)
    }, 2000)
  } catch {
    // 拦截器已 toast
  }
}

/**
 * 轮询解析状态：parsed → 拉置信度与刷新树；failed / 超时 → 提示。
 * @param documentId 文档 ID
 * @param polls 已轮询次数
 * @returns 无
 */
async function checkParseStatus(documentId: string, polls: number): Promise<void> {
  try {
    const doc = await getScriptDocument(documentId)
    if (doc.parse_status === 'parsed') {
      clearUploadTimer()
      parsingDocId.value = ''
      toast.success('解析完成，剧本已导入')
      await loadConfidenceItems(documentId)
      await fetchTree()
    } else if (doc.parse_status === 'failed') {
      clearUploadTimer()
      parsingDocId.value = ''
      toast.error('文档解析失败，请检查文件格式后重试')
    } else if (polls >= UPLOAD_POLL_MAX) {
      clearUploadTimer()
      parsingDocId.value = ''
      toast.warning('解析耗时较长，已停止等待，请稍后刷新查看')
    }
  } catch {
    clearUploadTimer()
    parsingDocId.value = ''
  }
}

/**
 * 停止解析轮询定时器。
 * @returns 无
 */
function clearUploadTimer(): void {
  if (uploadTimer) {
    clearInterval(uploadTimer)
    uploadTimer = null
  }
}

/**
 * 拉取解析文档的逐行置信度明细（低置信行高亮数据源）。
 * @param documentId 文档 ID
 * @returns 无
 */
async function loadConfidenceItems(documentId: string): Promise<void> {
  try {
    const items = await getDocumentConfidenceItems(documentId)
    const map = new Map<string, number>()
    for (const item of items) {
      if (item.line_id !== undefined && typeof item.confidence === 'number') {
        map.set(String(item.line_id), item.confidence)
      }
    }
    confidenceMap.value = map
  } catch {
    // 置信度明细缺失不阻塞主流程
  }
}

/* ==================== 提交审核 ==================== */

/** 提交审核中 */
const reviewSubmitting = ref(false)

/**
 * 提交剧本审核（POST /scripts/{id}/submit-review）。
 *
 * 提交成功后统一跳转「审批台」（/overview）：剧本页只负责提交，
 * 审批单确认与后续流转在审批台集中处理，避免用户停留在编辑器找不到待办。
 * @returns 无
 */
async function submitReview(): Promise<void> {
  const scriptId = scriptTree.value?.script.id
  if (!scriptId) {
    toast.warning('当前还没有剧本：请先生成或导入剧本，再提交审核')
    return
  }
  reviewSubmitting.value = true
  // LLM 合规审核为同步执行（flag 时含逐场弱化改写与二审），耗时 1-3 分钟
  toast.info('正在执行合规审核，约需 1-3 分钟，请勿关闭或刷新页面')
  try {
    const result = await submitScriptReview(scriptId)
    // 合规审核 block：剧本被置为 rejected，不会生成审批单，必须留在剧本页修正
    if (String(result.result ?? '') === 'block' || String(result.script_status ?? '') === 'rejected') {
      toast.warning('合规审核未通过：请按审核意见修改剧本后重新提交')
      await fetchTree()
      return
    }
    toast.success('剧本已提交审核，正在前往审批台确认')
    await fetchPendingApprovals()
    // 提交即进入审批台：审批台首屏会重新拉取待办，HITL-1 卡片可直接确认
    await router.push(`/projects/${projectId.value}/overview`)
  } catch {
    // 拦截器已 toast（40001 状态不允许 / 合规失败等）；失败时留在剧本页便于修正
  } finally {
    reviewSubmitting.value = false
  }
}

/* ==================== 待办审批（HITL 关口条数据源） ==================== */

/** 待办剧本审批单数 */
const pendingScriptCount = ref(0)

/** 项目是否仍处于剧本确认阶段（draft / scripting / script_review） */
const scriptGateStage = computed(() => {
  const status = String(project.value?.status ?? '')
  // 项目数据尚未就绪时不误判，保持可提交
  if (!status) return true
  return ['draft', 'scripting', 'script_review'].includes(status)
})

/**
 * 拉取本项目待办剧本审批数（失败静默为 0）。
 * 服务端直接按 approval_type=script 过滤，避免其他关口待办占满分页导致计数为 0。
 * @returns 无
 */
async function fetchPendingApprovals(): Promise<void> {
  try {
    const page = await listApprovals({
      project_id: projectId.value,
      approval_type: 'script',
      status: 'pending',
      page_size: 100,
    })
    pendingScriptCount.value = page.items.filter((a) => a.approval_type === 'script').length
  } catch {
    pendingScriptCount.value = 0
  }
}

/**
 * 前往审批台（HITL 待办集中入口）。
 *
 * 剧本页的「前往审批台确认」统一落到审批台首屏，用户可看到 HITL-1 待办卡，
 * 再进入剧本审批详情；不再直接跳详情页，避免入口语义与菜单名称不一致。
 * @returns 无
 */
function goReview(): void {
  toast.info('正在前往审批台，请处理 HITL-1 剧本确认待办')
  void router.push(`/projects/${projectId.value}/overview`)
}

/* ==================== 右栏夜窗（write_todos / 指令台 / diff） ==================== */

/** 夜窗是否展开 */
const rightPanelOpen = ref(true)
/** 夜窗当前 Tab：todos 任务规划 / diff 版本对比 */
const rightTab = ref<'todos' | 'diff'>('todos')
/** 夜窗分段选项（统一走 SegmentedControl） */
const RIGHT_TAB_OPTIONS: { value: 'todos' | 'diff'; label: string }[] = [
  { value: 'todos', label: '智能任务' },
  { value: 'diff', label: '版本对比' },
]
/** diff 行数据 */
const diffRows = ref<DiffRow[]>([])
/** diff 加载中 */
const diffLoading = ref(false)
/** diff 已拉取标记（区分未加载与空差异） */
const diffLoaded = ref(false)

/**
 * 归一化差异行类别（兼容 type / kind 字段）。
 * @param row 差异行
 * @returns 类别
 */
function diffKind(row: DiffRow): string {
  return (row.type ?? row.kind ?? 'context').toLowerCase()
}

/**
 * 归一化差异行新内容。
 * @param row 差异行
 * @returns 新内容
 */
function diffNewContent(row: DiffRow): string {
  return row.new_content ?? row.content ?? ''
}

/**
 * 拉取版本 diff（against = 上一版本号）。
 * @returns 无
 */
async function loadDiff(): Promise<void> {
  const scriptId = scriptTree.value?.script.id
  if (!scriptId) return
  const against = Math.max(1, scriptVersion.value - 1)
  diffLoading.value = true
  try {
    diffRows.value = await getScriptDiff(scriptId, against)
    diffLoaded.value = true
  } catch {
    // 拦截器已 toast（diff 服务未就绪等）
  } finally {
    diffLoading.value = false
  }
}

/**
 * VERSION 徽标 Diff 对比入口：切到 diff Tab 并按需拉取。
 * @returns 无
 */
function openDiffPanel(): void {
  rightTab.value = 'diff'
  if (!diffLoaded.value && !diffLoading.value) void loadDiff()
}

/* ==================== 原型对齐派生（stitch_prd/p2） ==================== */

/** 台词口语速率（字/分钟）：无后端时长数据时的预览估算基数 */
const WORDS_PER_MINUTE = 280

/**
 * 秒 → m:ss（预检 / 手稿统计 / 钩子卡点的时长格式）。
 * @param totalSeconds 总秒数
 * @returns 形如 1:52 的标签
 */
function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.round(totalSeconds))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

/**
 * 单集字数（集眉头统计）。
 * @param ep 剧集视图
 * @returns 该集正文累计字数
 */
function episodeWordCount(ep: EpisodeView): number {
  let count = 0
  for (const scene of ep.scenes) {
    for (const item of scene.lines) {
      count += (item.line.content ?? '').length
    }
  }
  return count
}

/**
 * 单集预估成片时长标签：优先分镜实际合计（P1-7 统一口径），
 * 其次后端 target_duration_seconds，最后按字数估算。
 * @param ep 剧集视图
 * @returns m:ss 标签
 */
function episodeDurationLabel(ep: EpisodeView): string {
  if (ep.storyboardDurationMs && ep.storyboardDurationMs > 0) {
    return formatDuration(ep.storyboardDurationMs / 1000)
  }
  if (ep.targetSeconds && ep.targetSeconds > 0) return formatDuration(ep.targetSeconds)
  return formatDuration((episodeWordCount(ep) / WORDS_PER_MINUTE) * 60)
}

/**
 * 单集低置信行数（集数 chip 状态徽标）。
 * @param ep 剧集视图
 * @returns 低置信行数
 */
function episodeLowCount(ep: EpisodeView): number {
  let count = 0
  for (const scene of ep.scenes) {
    for (const item of scene.lines) {
      if (isLowConfidence(item.line)) count += 1
    }
  }
  return count
}

/** 全剧场次总数（手稿眉头统计） */
const totalSceneCount = computed(() => treeView.value.reduce((sum, ep) => sum + ep.scenes.length, 0))

/** 全剧预估成片时长标签（优先各集分镜实际合计，其次集级目标时长，最后按字数估算） */
const totalDurationLabel = computed(() => {
  let storyboardMs = 0
  let hasStoryboard = false
  let targetSeconds = 0
  let hasTarget = false
  for (const ep of treeView.value) {
    if (ep.storyboardDurationMs && ep.storyboardDurationMs > 0) {
      storyboardMs += ep.storyboardDurationMs
      hasStoryboard = true
    }
    if (ep.targetSeconds && ep.targetSeconds > 0) {
      targetSeconds += ep.targetSeconds
      hasTarget = true
    }
  }
  if (hasStoryboard) return formatDuration(storyboardMs / 1000)
  if (hasTarget) return formatDuration(targetSeconds)
  return formatDuration((wordCount.value / WORDS_PER_MINUTE) * 60)
})

/** 整体置信度（已知行置信度均值；无数据返回 null） */
const overallConfidence = computed<number | null>(() => {
  const values: number[] = []
  for (const ep of treeView.value) {
    for (const scene of ep.scenes) {
      for (const item of scene.lines) {
        const conf = lineConfidence(item.line)
        if (conf !== null) values.push(conf)
      }
    }
  }
  if (values.length === 0) return null
  return values.reduce((sum, value) => sum + value, 0) / values.length
})

/** 预检横幅关闭态 */
const bannerDismissed = ref(false)
/** 「跳下一处低置信度」循环游标 */
const lowJumpCursor = ref(0)

/**
 * 循环跳转到下一处低置信行（预检横幅按钮）。
 * @returns 无
 */
function jumpNextLowConfidence(): void {
  const items = lowConfidenceLines.value
  if (items.length === 0) return
  const target = items[lowJumpCursor.value % items.length]
  lowJumpCursor.value += 1
  locateLine(target.id)
}

/**
 * 场次正文累计字数（时码估算基数）。
 * @param scene 场次视图
 * @returns 字数
 */
function sceneWordCount(scene: SceneView): number {
  return scene.lines.reduce((sum, item) => sum + (item.line.content ?? '').length, 0)
}

/** 场次时码区间（按全剧累计字数与口语速率估算；后端暂无场次级时码） */
const sceneTimecodes = computed<Map<string, { start: string; end: string }>>(() => {
  const map = new Map<string, { start: string; end: string }>()
  let cursor = 0
  for (const ep of treeView.value) {
    for (const scene of ep.scenes) {
      const seconds = (sceneWordCount(scene) / WORDS_PER_MINUTE) * 60
      map.set(`${ep.episodeNo}-${scene.sceneNo}`, {
        start: formatDuration(cursor),
        end: formatDuration(cursor + seconds),
      })
      cursor += seconds
    }
  }
  return map
})

/** 说话人权重 pills 圆点配色（与台词卡说话人文字色同序轮换） */
const SPEAKER_DOT_CLASSES = ['bg-primary-500', 'bg-info', 'bg-gold', 'bg-danger']
/** 说话人权重 pills 文字强调色（取令牌既有语义色，不引入新色值） */
const SPEAKER_TEXT_CLASSES = ['text-primary-700', 'text-info-strong', 'text-gold-deep', 'text-danger-strong']

/**
 * 场次说话人权重（dialogue 行数占比，按权重降序最多 4 人）。
 * @param scene 场次视图
 * @returns 说话人与占比
 */
function sceneSpeakers(scene: SceneView): Array<{ name: string; pct: number }> {
  const counts = new Map<string, number>()
  let total = 0
  for (const item of scene.lines) {
    if (lineType(item.line) !== 'dialogue') continue
    const speaker = lineSpeaker(item.line) ?? '未命名角色'
    counts.set(speaker, (counts.get(speaker) ?? 0) + 1)
    total += 1
  }
  if (total === 0) return []
  return Array.from(counts.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
    .map(([name, count]) => ({ name, pct: Math.round((count / total) * 100) }))
}

/**
 * 台词卡说话人的强调色（与权重 pills 同序；不在权重列表时用品牌色）。
 * @param scene 场次视图
 * @param speaker 说话人
 * @returns 文字色类名
 */
function speakerToneClass(scene: SceneView, speaker: string | null): string {
  const index = sceneSpeakers(scene).findIndex((item) => item.name === (speaker ?? '未命名角色'))
  if (index < 0) return 'text-primary-700'
  return SPEAKER_TEXT_CLASSES[index % SPEAKER_TEXT_CLASSES.length]
}

/**
 * 场次置信度摘要（行置信度均值；无数据返回 null）。
 * @param scene 场次视图
 * @returns 均值或 null
 */
function sceneConfidence(scene: SceneView): number | null {
  const values = scene.lines
    .map((item) => lineConfidence(item.line))
    .filter((value): value is number => value !== null)
  if (values.length === 0) return null
  return values.reduce((sum, value) => sum + value, 0) / values.length
}

/** 待分配集数余量（目标集数 − 已生成集数，>0 时展示「+N 集待分配」） */
const pendingEpisodeQuota = computed(() =>
  Math.max(0, (project.value?.target_episode_count ?? 0) - treeView.value.length),
)

/** HITL 条改写入口的默认范围（第一集整集；无剧本时禁用） */
const defaultRewriteEp = computed(() => treeView.value[0]?.episodeNo ?? 0)

/** 指令台受影响场次（改写范围预估） */
const affectedSceneCount = computed(() => {
  if (rewriteEpisodeNo.value <= 0) return 0
  const ep = treeView.value.find((item) => item.episodeNo === rewriteEpisodeNo.value)
  if (!ep) return 0
  return rewriteSceneNo.value > 0 ? 1 : ep.scenes.length
})

/** 指令台快捷提示锦囊（点击填入改写指令，模板文案属 UI 结构件） */
const QUICK_PROMPTS: Array<{ label: string; text: string }> = [
  { label: '⚡ 强化黄金 3 秒戏剧冲突', text: '强化开场黄金 3 秒的戏剧冲突，让第一句台词就抓住观众。' },
  { label: '🎯 压缩台词增强节奏感', text: '压缩台词长度，删减冗余对白，让本段节奏更紧凑。' },
  { label: '💎 注入反转爽点', text: '在本段结尾注入身份反转的打脸爽点，强化钩子张力。' },
]

/**
 * 点击锦囊 chip：填入指令台并兜底改写范围。
 * @param text 指令模板文本
 * @returns 无
 */
function applyQuickPrompt(text: string): void {
  ensureRewriteScope()
  rewriteFeedback.value = text
}

/** 夜窗助手连接态徽标（绑定真实 SSE 状态） */
const agentStatus = computed<{ label: string; cls: string; pulse: boolean }>(() => {
  switch (sseStatus.value) {
    case 'connected':
      return { label: 'Online', cls: 'border-success-border bg-success-bg text-success', pulse: true }
    case 'polling':
      return { label: '轮询模式', cls: 'border-warning-border bg-warning-bg text-warning-strong', pulse: true }
    case 'connecting':
      return { label: '连接中', cls: 'border-info-border bg-info-bg text-info-strong', pulse: true }
    default:
      return { label: '离线', cls: 'border-hairline bg-surface-sunken text-neutral-400', pulse: false }
  }
})

/** 安全护栏徽标（绑定最近一次生成的 compliance_review 步骤状态） */
const complianceBadge = computed<{ label: string; cls: string }>(() => {
  const state = stepState('compliance_review')
  if (state === 'done') return { label: '全部合规', cls: 'bg-success-bg text-success' }
  if (state === 'active') return { label: '审查中', cls: 'bg-info-bg text-info-strong' }
  return { label: '未执行', cls: 'bg-surface-sunken text-neutral-400' }
})

/** 安全护栏说明文案（随合规步骤状态切换） */
const complianceDesc = computed(() => {
  const state = stepState('compliance_review')
  if (state === 'done') return '最近一次生成已通过合规前置审查，敏感词与风险项已核验。'
  if (state === 'active') return '合规前置审查正在执行，风险项将在此汇总。'
  return '尚未执行合规前置审查；重新生成或提交审核后将自动同步结果。'
})

/* ==================== 创作模式 segmented（Row1） ==================== */

/** 创作模式：一句话生成 / 长篇小说与剧本解析（上传解析）/ 自由创作 */
type CreativeMode = 'generate' | 'parse' | 'free'
/** 当前创作模式（默认解析态，与原型 active 一致） */
const creativeMode = ref<CreativeMode>('parse')

/**
 * 切换创作模式：一句话生成打开生成弹窗，解析打开文件选择；自由创作无后端模式（禁用位）。
 * @param mode 目标模式
 * @returns 无
 */
function selectCreativeMode(mode: CreativeMode): void {
  if (mode === 'free') return
  creativeMode.value = mode
  if (mode === 'generate') openGenerate()
  if (mode === 'parse' && parsingDocId.value === '') pickFile()
}

/* ==================== 生命周期 ==================== */

// 挂载：按项目状态决定初始视图（scripting 恢复进度模式，否则拉树）+ 拉审批数
onMounted(async () => {
  await fetchPendingApprovals()
  if (project.value?.status === 'scripting') {
    enterProgressMode()
    treeLoading.value = false
  } else {
    await fetchTree()
  }
  // V4.0 P0 SF-F2-13：撤销/重做快捷键 + 草稿自动保存（10s）+ 崩溃后草稿恢复入口
  window.addEventListener('keydown', onEditorKeydown)
  startDraftAutosave()
  const draft = editHistory.readDraft()
  if (draft && draft.state.length > 0) {
    draftSavedAt.value = draft.savedAt
    draftBannerVisible.value = true
  }
})

// 卸载：清理全部定时器
onUnmounted(() => {
  stopProgressTimer()
  clearUploadTimer()
  if (savedTimer) clearTimeout(savedTimer)
  window.removeEventListener('keydown', onEditorKeydown)
  editHistory.stopAutosave()
})
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <!-- ============ V4.0 编辑器能力条（P0 SF-F2-13）：撤销/重做 + 草稿恢复 ============ -->
    <div
      v-if="draftBannerVisible"
      class="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-warning bg-warning-bg px-4 py-2"
    >
      <span class="text-caption text-warning-strong">
        发现未保存的编辑草稿（{{ editRecords.length }} 条，保存于
        {{ draftSavedAt ? new Date(draftSavedAt).toLocaleString() : '未知时间' }}）
      </span>
      <span class="flex items-center gap-2">
        <AppButton variant="secondary" size="sm" @click="restoreDraft">恢复草稿</AppButton>
        <AppButton variant="ghost" size="sm" @click="discardDraft">丢弃</AppButton>
      </span>
    </div>
    <div class="flex shrink-0 items-center gap-2 border-b border-hairline bg-surface-raised px-4 py-1.5">
      <span class="text-caption text-neutral-500">编辑历史</span>
      <AppButton
        variant="ghost"
        size="sm"
        :disabled="!editHistory.canUndo.value"
        :title="editHistory.canUndo.value ? '撤销上一步正文编辑（Ctrl+Z）' : '暂无可撤销的编辑'"
        @click="undoEdit"
      >
        撤销 (Ctrl+Z)
      </AppButton>
      <AppButton
        variant="ghost"
        size="sm"
        :disabled="!editHistory.canRedo.value"
        :title="editHistory.canRedo.value ? '重做上一步撤销（Ctrl+Shift+Z）' : '暂无可重做的编辑'"
        @click="redoEdit"
      >
        重做 (Ctrl+Shift+Z)
      </AppButton>
      <span class="text-caption text-neutral-500">
        可回退 {{ editHistory.depth.value }} / 100 步 · 每 10s 自动保存草稿
      </span>
    </div>

    <!-- ============ 工作台控制中枢（原型 Row1 创作模式/版本/工具 + Row2 集数索引） ============ -->
    <header class="shrink-0 border-b border-hairline bg-surface-raised">
      <!-- Row 1：创作模式 + VERSION 徽标 + 工具按钮 -->
      <div class="flex h-toolbar items-center gap-2.5 overflow-x-auto px-4">
        <!-- 创作模式 segmented（一句话生成 → 生成弹窗；长篇小说与剧本解析 → 上传解析；自由创作无后端模式） -->
        <div class="inline-flex shrink-0 items-center rounded-md border border-hairline bg-surface-inset p-0.5" role="group" aria-label="创作模式">
          <button
            type="button"
            class="inline-flex h-8 items-center gap-1.5 rounded-sm px-3 text-xs font-medium transition-colors duration-gd-fast"
            :class="creativeMode === 'generate' ? 'bg-surface-raised text-primary-700 shadow-gd-1' : 'text-neutral-500 hover:text-neutral-700'"
            @click="selectCreativeMode('generate')"
          >
            <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            一句话生成
          </button>
          <button
            type="button"
            class="inline-flex h-8 items-center gap-1.5 rounded-sm px-3 text-xs font-medium transition-colors duration-gd-fast"
            :class="creativeMode === 'parse' ? 'bg-surface-raised text-primary-700 shadow-gd-1' : 'text-neutral-500 hover:text-neutral-700'"
            @click="selectCreativeMode('parse')"
          >
            <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M4 19.5A2.5 2.5 0 016.5 17H20V2H6.5A2.5 2.5 0 004 4.5v15zM4 19.5A2.5 2.5 0 006.5 22H20v-5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            长篇小说与剧本解析
            <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-primary-500" :class="parsingDocId === '' && creativeMode === 'parse' ? 'opacity-50' : ''" aria-hidden="true" />
          </button>
          <button
            type="button"
            disabled
            title="自由创作模式开发中"
            class="inline-flex h-8 cursor-not-allowed items-center gap-1.5 rounded-sm px-3 text-xs font-medium text-neutral-500 opacity-40"
          >
            自由创作
          </button>
        </div>

        <div class="h-4 w-px shrink-0 bg-hairline" aria-hidden="true" />

        <!-- VERSION 徽标（版本号 + 审核态 + Diff 对比入口） -->
        <div class="flex shrink-0 items-center gap-2 rounded-md border border-hairline bg-surface-canvas px-2.5 py-1.5">
          <span class="gd-section-label">VERSION</span>
          <span class="gd-num text-xs font-semibold text-neutral-800">v{{ scriptVersion }}</span>
          <span v-if="pendingScriptCount > 0" class="rounded bg-warning-bg px-1.5 py-0.5 text-micro font-semibold text-warning-strong">当前审核中</span>
          <button
            type="button"
            class="ml-0.5 flex shrink-0 items-center gap-0.5 text-xs font-medium text-primary-700 transition-colors duration-gd-fast hover:text-primary-600"
            @click="openDiffPanel"
          >
            <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M8 3v18M16 3v18M3 8h10M11 16h10M3 8l3-3M21 16l-3 3" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            Diff 对比
          </button>
        </div>

        <div class="flex-1" />

        <!-- 保存状态指示（行内自动保存，替代原型「存为草稿」位的真实保存语义） -->
        <span v-if="saveState === 'saving'" class="inline-flex shrink-0 items-center rounded-full border border-hairline bg-surface-canvas px-2 py-0.5 text-caption text-neutral-500">保存中…</span>
        <span v-else-if="saveState === 'saved'" class="inline-flex shrink-0 items-center rounded-full border border-success-border bg-success-bg px-2 py-0.5 text-caption text-success">已保存 ✓</span>

        <!-- 撤销 / 重做统一由上方「编辑历史」入口提供，此处不再重复放置禁用按钮 -->

        <!-- 历史快照 / 导出文档（P2-4 修复：接入版本快照与 Markdown 导出真实能力，不再永久禁用） -->
        <button
          type="button"
          class="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-hairline bg-surface-canvas px-2.5 py-1.5 text-xs font-medium text-neutral-600 transition-colors duration-gd-fast hover:border-primary-200 hover:text-primary-700"
          @click="openSnapshots"
        >
          <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M12 8v4l3 3M21 12a9 9 0 11-18 0 9 9 0 0118 0z" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          历史快照
        </button>
        <button
          type="button"
          class="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-hairline bg-surface-canvas px-2.5 py-1.5 text-xs font-medium text-neutral-600 transition-colors duration-gd-fast hover:border-primary-200 hover:text-primary-700"
          @click="exportScriptMarkdown"
        >
          <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          导出文档 (.md)
        </button>

        <!-- 夜窗助手收起 / 展开 -->
        <button
          type="button"
          class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-neutral-400 transition-colors duration-gd-fast hover:bg-surface-sunken hover:text-neutral-600"
          :aria-label="rightPanelOpen ? '收起 AI 编剧助手' : '展开 AI 编剧助手'"
          @click="rightPanelOpen = !rightPanelOpen"
        >
          <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M4 5h16v14H4zM14 5v14" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
      </div>

      <!-- Row 2：集数索引 chips（原左树定位功能承接于此，chip 弹层列场次） -->
      <div class="flex items-center gap-2 overflow-x-auto px-4 pb-2">
        <div class="flex shrink-0 items-center gap-1.5 pr-1 text-neutral-400">
          <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M4 6h9M4 12h16M4 18h9M17 4v4M15 6h4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <span class="gd-section-label">集数索引</span>
        </div>

        <template v-if="treeView.length > 0">
          <div v-for="ep in treeView" :key="ep.id" class="relative shrink-0">
            <div
              class="flex items-center gap-1 rounded-lg border py-1.5 pl-3 pr-1.5 transition-colors duration-gd-fast"
              :class="activeEpisodeNo === ep.episodeNo ? 'border-primary-200 bg-primary-50' : 'border-hairline bg-surface-canvas hover:border-hairline-strong'"
            >
              <button type="button" class="flex items-center gap-1.5" @click="locateEpisode(ep.episodeNo)">
                <span v-if="episodeLowCount(ep) > 0" class="h-1.5 w-1.5 rounded-full bg-warning" aria-hidden="true" />
                <span v-else class="h-1.5 w-1.5 rounded-full bg-success" aria-hidden="true" />
                <span class="gd-num text-xs font-semibold text-neutral-800">Ep.{{ String(ep.episodeNo).padStart(2, '0') }}</span>
                <span class="text-caption" :class="episodeLowCount(ep) > 0 ? 'text-warning-strong' : 'text-neutral-400'">
                  {{ episodeLowCount(ep) > 0 ? '待人工校正' : '已就绪' }}
                </span>
                <span v-if="episodeLowCount(ep) > 0" class="gd-num rounded bg-warning-bg px-1 py-0.5 text-micro font-semibold text-warning-strong">{{ episodeLowCount(ep) }}处</span>
              </button>
              <button
                v-if="ep.scenes.length > 0"
                type="button"
                class="rounded p-1 text-neutral-400 transition-colors duration-gd-fast hover:text-primary-600"
                :aria-label="`展开第 ${ep.episodeNo} 集场次索引`"
                @click="toggleEpMenu(ep.id)"
              >
                <svg class="h-3.5 w-3.5 transition-transform duration-gd-fast" :class="openEpMenuId === ep.id ? 'rotate-180' : ''" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </button>
            </div>
            <!-- 场次浮层（承接原左树场级导航） -->
            <div v-if="openEpMenuId === ep.id" class="absolute left-0 top-full z-20 mt-1 max-h-56 w-64 overflow-y-auto rounded-lg border border-hairline bg-surface-raised p-1 shadow-gd-2">
              <button
                v-for="scene in ep.scenes"
                :key="scene.id"
                type="button"
                class="flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-xs transition-colors duration-gd-fast"
                :class="activeSceneKey === `scene-${ep.episodeNo}-${scene.sceneNo}` ? 'bg-primary-50 font-medium text-primary-700' : 'text-neutral-600 hover:bg-surface-sunken'"
                @click="locateScene(ep.episodeNo, scene.sceneNo); openEpMenuId = 0"
              >
                <span class="gd-num shrink-0 text-neutral-400">场{{ scene.sceneNo }}</span>
                <span class="truncate">{{ scene.heading }}</span>
              </button>
            </div>
          </div>

          <div v-if="pendingEpisodeQuota > 0" class="shrink-0 rounded bg-surface-sunken px-2 py-1 text-caption text-neutral-400">+ {{ pendingEpisodeQuota }} 集待分配</div>
        </template>

        <!-- 生成进度态 chip（集数索引行的运行中形态） -->
        <div v-else-if="progressMode" class="flex shrink-0 items-center gap-1.5 rounded-lg border border-hairline bg-surface-canvas px-3 py-1.5 opacity-90">
          <svg class="h-3.5 w-3.5 animate-spin text-gd-running" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
          </svg>
          <span class="gd-num text-xs font-semibold text-neutral-800">第 {{ runningEpisodeNo }} / {{ progressTotalEpisodes || '—' }} 集</span>
          <span class="text-caption text-gd-running">生成中 {{ progressPercent }}%</span>
        </div>

        <p v-else class="text-caption text-neutral-400">暂无剧集——用上方创作模式开始</p>
      </div>
    </header>

    <!-- ============ 进度模式可离开提示 ============ -->
    <div v-if="progressMode && !progressError" class="flex h-9 shrink-0 items-center justify-center gap-2 bg-gd-running-bg text-xs text-gd-running" role="status">
      <svg class="h-3.5 w-3.5 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
        <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
      </svg>
      剧本正在后台生成，你可以离开此页，稍后回来查看；完成后将自动进入待审批状态。
      <AppButton variant="ghost" size="sm" @click="leaveProgressMode">返回编辑</AppButton>
    </div>

    <!-- ============ 主体双画布：左手稿区 ≈70% / 右 AI 夜窗 ≈30%（<1024px 上下堆叠） ============ -->
    <div class="flex min-h-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
      <!-- ===== 左画布：手稿主区 ===== -->
      <section class="min-h-0 min-w-0 flex-1 bg-surface-raised lg:overflow-y-auto">
        <div class="flex flex-col gap-5 p-4 lg:p-6">
          <!-- 生成进度模式（中栏主形态） -->
          <div v-if="progressMode" class="gd-panel mx-auto w-full max-w-xl px-6 py-12">
            <h2 class="text-base font-semibold text-neutral-900">剧本生成中</h2>
            <!-- 进度百分比 -->
            <div class="mt-4 flex items-center gap-3">
              <div class="h-2 flex-1 overflow-hidden rounded-full bg-surface-sunken">
                <div class="h-full rounded-full bg-primary-500 transition-[width] duration-gd-slow" :style="{ width: `${progressPercent}%` }" />
              </div>
              <span class="gd-num w-12 text-right text-sm font-semibold text-primary-700">{{ progressPercent }}%</span>
            </div>
            <p v-if="acceptedCredits > 0" class="gd-num mt-2 text-xs text-neutral-400">预估消耗 {{ acceptedCredits }} 积分（以实际结算为准）</p>

            <!-- write_todos 步骤列表（优先 AI 规划清单，回退内置流程步骤） -->
            <ol v-if="plannedTodos.length > 0" class="mt-6 space-y-1">
              <li
                v-for="(todo, index) in plannedTodos"
                :key="`todo-${todo.step}`"
                class="rounded-lg px-3 py-2.5"
                :class="index === doneTodoCount && index < plannedTodos.length ? 'bg-primary-50' : ''"
              >
                <div class="flex items-center gap-3">
                  <span
                    class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs"
                    :class="{ 'gd-fill-mint': index < doneTodoCount, 'gd-fill-brand': index === doneTodoCount, 'bg-surface-sunken text-neutral-400': index > doneTodoCount }"
                    aria-hidden="true"
                  >
                    <template v-if="index < doneTodoCount">✓</template>
                    <svg v-else-if="index === doneTodoCount" class="h-3 w-3 animate-spin" viewBox="0 0 24 24" fill="none">
                      <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                      <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                    </svg>
                    <template v-else>{{ todo.step || index + 1 }}</template>
                  </span>
                  <span class="text-sm" :class="index > doneTodoCount ? 'text-neutral-400' : index < doneTodoCount ? 'text-neutral-500' : 'font-medium text-neutral-900'">
                    {{ todo.title }}
                  </span>
                </div>
                <p v-if="todo.detail && index <= doneTodoCount" class="mt-1 pl-8 text-xs leading-relaxed text-neutral-400">{{ todo.detail }}</p>
              </li>
            </ol>
            <ol v-else class="mt-6 space-y-1">
              <li v-for="step in GENERATE_STEPS" :key="step.key" class="flex items-center gap-3 rounded-lg px-3 py-2.5" :class="stepState(step.key) === 'active' ? 'bg-primary-50' : ''">
                <span
                  class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs"
                  :class="{ 'gd-fill-mint': stepState(step.key) === 'done', 'gd-fill-brand': stepState(step.key) === 'active', 'bg-surface-sunken text-neutral-400': stepState(step.key) === 'todo' }"
                  aria-hidden="true"
                >
                  <template v-if="stepState(step.key) === 'done'">✓</template>
                  <svg v-else-if="stepState(step.key) === 'active'" class="h-3 w-3 animate-spin" viewBox="0 0 24 24" fill="none">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                  </svg>
                  <template v-else>·</template>
                </span>
                <span class="text-sm" :class="stepState(step.key) === 'done' ? 'text-neutral-500' : stepState(step.key) === 'active' ? 'font-medium text-neutral-900' : 'text-neutral-400'">
                  {{ step.label }}
                </span>
              </li>
            </ol>

            <!-- 失败态 -->
            <div v-if="progressError" class="mt-6 rounded-lg border border-danger-border bg-danger-bg p-4">
              <p class="text-sm font-medium text-danger">生成失败</p>
              <p class="mt-1 text-xs leading-relaxed text-neutral-600">{{ progressError }}</p>
              <div class="mt-3 flex gap-2">
                <AppButton variant="primary" size="sm" @click="openGenerate">重新发起</AppButton>
                <AppButton variant="secondary" size="sm" @click="leaveProgressMode">返回编辑</AppButton>
              </div>
            </div>
          </div>

          <template v-else>
            <!-- Q-018：无剧本时的持久失败横幅（刷新后仍可见） -->
            <div
              v-if="lastScriptFailure && treeView.length === 0"
              class="mb-4 rounded-lg border border-danger-border bg-danger-bg p-4"
            >
              <p class="text-sm font-medium text-danger">上次生成失败</p>
              <p class="mt-1 text-xs leading-relaxed text-neutral-600">{{ lastScriptFailure }}</p>
              <div class="mt-3 flex gap-2">
                <AppButton variant="primary" size="sm" @click="openGenerate">重新发起</AppButton>
              </div>
            </div>

            <!-- 加载骨架 -->
            <div v-if="treeLoading" class="gd-card p-6">
              <LoadingSkeleton variant="text" :rows="10" />
            </div>

            <!-- 加载失败（后端剧本路由未就绪等） -->
            <div v-else-if="treeError" class="gd-card p-6">
              <ErrorRetryBlock message="剧本加载失败" :description="treeError" @retry="fetchTree" />
              <div class="mt-4 text-center">
                <AppButton variant="primary" size="md" @click="openGenerate">✨ 先用一句话生成一份剧本</AppButton>
              </div>
            </div>

            <!-- 空态：无剧本 -->
            <EmptyState
              v-else-if="treeView.length === 0"
              icon="✍️"
              title="还没有剧本"
              description="用一句话描述你的创意，AI 编剧将在几分钟内生成完整剧本；也可以上传 txt/md 文档直接解析导入"
            >
              <template #action>
                <div class="flex gap-2.5">
                  <AppButton variant="primary" size="md" @click="openGenerate">✨ 一句话生成</AppButton>
                  <AppButton variant="secondary" size="md" @click="pickFile">上传 txt/md</AppButton>
                </div>
              </template>
            </EmptyState>

            <template v-else>
              <!-- 预检报告横幅（解析完成 + 整体置信度 + 跳下一处低置信度 + 关闭） -->
              <div v-if="!bannerDismissed" class="gd-card flex flex-col gap-3 p-4 md:flex-row md:items-center md:justify-between">
                <div class="flex min-w-0 items-start gap-3 md:items-center">
                  <span class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-warning-bg text-warning-strong">
                    <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                      <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </span>
                  <div class="min-w-0">
                    <div class="flex flex-wrap items-center gap-2">
                      <h2 class="text-title text-neutral-900">文档解析完成 · 预检报告</h2>
                      <span v-if="overallConfidence !== null" class="gd-num rounded-full bg-success-bg px-2 py-0.5 text-caption font-semibold text-success">{{ (overallConfidence * 100).toFixed(1) }}% 整体置信度</span>
                      <span v-else-if="lowConfidenceLines.length === 0" class="rounded-full bg-success-bg px-2 py-0.5 text-caption font-semibold text-success">就绪 · 无低置信项</span>
                      <span v-else class="rounded-full bg-warning-bg px-2 py-0.5 text-caption font-semibold text-warning-strong">{{ lowConfidenceLines.length }} 处待校核</span>
                    </div>
                    <p class="mt-0.5 text-caption leading-relaxed text-neutral-500">
                      <template v-if="lastSourceName">源文件 <span class="font-semibold text-neutral-700">《{{ lastSourceName }}》</span> </template>已结构化拆解为
                      <span class="gd-num font-semibold text-neutral-700">{{ treeView.length }}</span> 集共
                      <span class="gd-num font-semibold text-neutral-700">{{ totalSceneCount }}</span> 个场次<template v-if="lowConfidenceLines.length > 0">，检出
                      <span class="font-semibold text-warning-strong underline decoration-warning">{{ lowConfidenceLines.length }} 处低置信度字段（&lt; {{ LOW_CONFIDENCE }}）</span>需人工纠偏锁定</template><template v-else>，未检出低置信度字段</template>。
                    </p>
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-2 self-end md:self-center">
                  <AppButton v-if="lowConfidenceLines.length > 0" variant="ghost" class="gd-btn--amber" size="sm" @click="jumpNextLowConfidence">
                    <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                      <path d="M12 8v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                    跳下一处低置信度
                  </AppButton>
                  <button
                    type="button"
                    class="rounded-lg p-1.5 text-neutral-400 transition-colors duration-gd-fast hover:bg-surface-sunken hover:text-neutral-600"
                    aria-label="关闭横幅"
                    @click="bannerDismissed = true"
                  >
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                      <path d="M6 6l12 12M18 6L6 18" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </button>
                </div>
              </div>

              <!-- 结构化手稿卡（眉头 + 分集 + 场块） -->
              <div class="gd-card overflow-hidden px-0 py-0">
                <!-- 手稿眉头：统一光条 + 28px 衬线标题 + 核心冲突 + 统计行 -->
                <div class="flex flex-col gap-4 border-b border-hairline-soft px-5 pb-4 pt-6 lg:flex-row lg:items-end lg:justify-between lg:px-8">
                  <div class="min-w-0">
                    <div class="flex min-w-0 items-center gap-3">
                      <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
                      <h1 class="gd-masthead truncate text-display-lg" :title="scriptTree?.script.title ?? project?.title ?? '剧本手稿'">
                        {{ scriptTree?.script.title || project?.title || '剧本手稿' }}
                      </h1>
                      <span v-if="treeView.length > 0" class="inline-flex shrink-0 items-center rounded-full bg-surface-sunken px-2 py-0.5 text-caption text-neutral-500">可编辑</span>
                    </div>
                    <p v-if="scriptTree?.script.logline" class="gd-measure mt-1 text-xs leading-relaxed text-neutral-500">核心冲突：{{ scriptTree.script.logline }}</p>
                  </div>
                  <div class="flex shrink-0 items-center gap-4">
                    <div class="flex flex-col">
                      <span class="text-caption text-neutral-400">场次数量</span>
                      <span class="gd-num text-sm font-semibold text-neutral-800">{{ totalSceneCount }} 场</span>
                    </div>
                    <div class="h-6 w-px bg-hairline-soft" aria-hidden="true" />
                    <div class="flex flex-col">
                      <span class="text-caption text-neutral-400">剧本字数</span>
                      <span class="gd-num text-sm font-semibold text-neutral-800">{{ wordCount.toLocaleString('zh-CN') }} <span class="text-caption font-normal text-neutral-400">字</span></span>
                    </div>
                    <div class="h-6 w-px bg-hairline-soft" aria-hidden="true" />
                    <div class="flex flex-col">
                      <span class="text-caption text-neutral-400">预估成片时长</span>
                      <span class="gd-num text-sm font-bold text-primary-700">{{ totalDurationLabel }}</span>
                    </div>
                  </div>
                </div>

                <!-- 分集手稿域 -->
                <article v-for="ep in treeView" :id="`ep-${ep.episodeNo}`" :key="ep.id" class="scroll-mt-4 border-b border-hairline-soft last:border-b-0">
                  <!-- 集头（衬线集标题 + 本集统计 + 改写入口） -->
                  <header class="flex flex-col gap-3 bg-surface-canvas px-5 py-4 lg:flex-row lg:items-end lg:justify-between lg:px-8">
                    <div class="min-w-0">
                      <p class="gd-section-label">Episode {{ String(ep.episodeNo).padStart(2, '0') }}</p>
                      <h2 class="mt-0.5 font-serif text-lg text-neutral-900">第 {{ ep.episodeNo }} 集：{{ ep.title }}</h2>
                      <p v-if="ep.logline" class="gd-measure mt-0.5 text-caption leading-relaxed text-neutral-500">{{ ep.logline }}</p>
                    </div>
                    <div class="flex shrink-0 items-center gap-4">
                      <div class="flex flex-col text-right">
                        <span class="text-caption text-neutral-400">本集场次</span>
                        <span class="gd-num text-sm font-semibold text-neutral-800">{{ ep.scenes.length }} 场</span>
                      </div>
                      <div class="h-6 w-px bg-hairline-soft" aria-hidden="true" />
                      <div class="flex flex-col text-right">
                        <span class="text-caption text-neutral-400">本集字数</span>
                        <span class="gd-num text-sm font-semibold text-neutral-800">{{ episodeWordCount(ep).toLocaleString('zh-CN') }} <span class="text-caption font-normal text-neutral-400">字</span></span>
                      </div>
                      <div class="h-6 w-px bg-hairline-soft" aria-hidden="true" />
                      <div class="flex flex-col text-right">
                        <span class="text-caption text-neutral-400">预估成片时长</span>
                        <span class="gd-num text-sm font-bold text-primary-700">{{ episodeDurationLabel(ep) }}</span>
                      </div>
                      <AppButton variant="ghost" size="sm" class="ml-1" @click="openRewrite(ep.episodeNo)">AI 改写本集</AppButton>
                    </div>
                  </header>

                  <!-- 场块 -->
                  <section
                    v-for="scene in ep.scenes"
                    :id="`scene-${ep.episodeNo}-${scene.sceneNo}`"
                    :key="scene.id"
                    class="scroll-mt-4 border-b border-hairline-soft px-5 py-5 last:border-b-0 lg:px-8"
                  >
                    <!-- 场 ribbon：SCENE 徽标 + 场头 + 时码 + 置信度 -->
                    <div class="flex flex-wrap items-center justify-between gap-3 rounded-lg bg-surface-canvas px-4 py-2.5">
                      <div class="flex min-w-0 items-center gap-2">
                        <span class="gd-fill-brand gd-num shrink-0 rounded px-2 py-0.5 text-micro font-bold">SCENE {{ String(scene.sceneNo).padStart(2, '0') }}</span>
                        <h3 class="truncate font-serif text-sm font-semibold text-neutral-800">第 {{ scene.sceneNo }} 场 · {{ scene.heading }}</h3>
                      </div>
                      <div class="flex shrink-0 items-center gap-2 gd-num text-caption text-neutral-400">
                        <span class="flex items-center gap-1" title="按时长估算的预览时码">
                          <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                            <path d="M12 8v4l3 3M21 12a9 9 0 11-18 0 9 9 0 0118 0z" stroke-linecap="round" stroke-linejoin="round" />
                          </svg>
                          {{ sceneTimecodes.get(`${ep.episodeNo}-${scene.sceneNo}`)?.start }} - {{ sceneTimecodes.get(`${ep.episodeNo}-${scene.sceneNo}`)?.end }}
                        </span>
                        <span aria-hidden="true">•</span>
                        <span
                          v-if="sceneConfidence(scene) !== null"
                          class="rounded px-2 py-0.5 font-medium"
                          :class="(sceneConfidence(scene) ?? 1) < LOW_CONFIDENCE ? 'bg-warning-bg font-semibold text-warning-strong' : 'bg-success-bg text-success'"
                        >
                          置信度: {{ (sceneConfidence(scene) ?? 0).toFixed(2) }} ({{ (sceneConfidence(scene) ?? 1) < LOW_CONFIDENCE ? '待人工校核' : '高' }})
                        </span>
                      </div>
                    </div>

                    <!-- 登场角色与戏份权重 pills -->
                    <div v-if="sceneSpeakers(scene).length > 0" class="mt-3 flex flex-wrap items-center gap-2">
                      <span class="text-caption uppercase text-neutral-400">登场角色与戏份权重:</span>
                      <span
                        v-for="(speaker, speakerIndex) in sceneSpeakers(scene)"
                        :key="`sp-${scene.id}-${speaker.name}`"
                        class="inline-flex items-center gap-1.5 rounded-full bg-surface-canvas px-3 py-1 text-xs"
                      >
                        <span class="h-2 w-2 rounded-full" :class="SPEAKER_DOT_CLASSES[speakerIndex % SPEAKER_DOT_CLASSES.length]" aria-hidden="true" />
                        <span class="font-semibold" :class="SPEAKER_TEXT_CLASSES[speakerIndex % SPEAKER_TEXT_CLASSES.length]">@{{ speaker.name }}</span>
                        <span class="gd-num text-micro font-medium text-neutral-400">{{ speaker.pct }}%</span>
                      </span>
                    </div>

                    <!-- 行渲染：舞台动作指示块 / 台词卡 / 旁白 + 低置信琥珀纠偏卡（保留行号与行内编辑） -->
                    <div class="mt-3 flex flex-col gap-2">
                      <p v-if="scene.lines.length === 0" class="text-xs text-neutral-400">本场暂无内容</p>
                      <div
                        v-for="item in scene.lines"
                        :id="`line-${item.line.id}`"
                        :key="item.line.id"
                        class="group relative scroll-mt-4"
                        :class="isLowConfidence(item.line) ? 'rounded-xl border border-warning-border bg-warning-bg p-3' : 'py-0.5'"
                        @dblclick="startEdit(item.line)"
                      >
                        <!-- 低置信块头（对照原型「低置信度字段」卡头） -->
                        <div v-if="isLowConfidence(item.line) && editingLineId !== String(item.line.id)" class="mb-2 flex flex-wrap items-center justify-between gap-2">
                          <div class="flex flex-wrap items-center gap-2">
                            <svg class="h-4 w-4 text-warning" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                              <path d="M12 3v4m6.5-1.5l-2.5 2.5M21 12h-4M4.5 5.5L7 8M3 12h4m7 9l4-4m-8 4v-4m-4.5-1.5L8 15" stroke-linecap="round" />
                            </svg>
                            <span class="text-xs font-bold text-warning-strong">低置信度字段 · 待人工校核</span>
                            <span class="gd-num rounded bg-warning-bg-deep px-1.5 py-0.5 text-micro font-bold text-warning-strong">置信度: {{ (lineConfidence(item.line) ?? 0).toFixed(2) }}</span>
                          </div>
                          <span class="text-micro text-neutral-400">双击正文或点下方编辑逐行纠偏</span>
                        </div>

                        <div class="flex items-start gap-2.5">
                          <!-- 行号槽（保留全局行号定位能力） -->
                          <span class="gd-num w-7 shrink-0 select-none pt-1.5 text-right text-caption leading-5 text-neutral-400">{{ item.no }}</span>

                          <div class="min-w-0 flex-1">
                            <!-- 编辑态 -->
                            <template v-if="editingLineId === String(item.line.id)">
                              <textarea
                                v-model="editDraft"
                                rows="2"
                                class="gd-input resize-none"
                                @keydown.esc="cancelEdit"
                                @keydown.meta.enter="saveEdit(item.line)"
                                @keydown.ctrl.enter="saveEdit(item.line)"
                              />
                              <div class="mt-2 flex items-center gap-1.5">
                                <AppButton variant="primary" size="sm" :loading="saveState === 'saving'" @click="saveEdit(item.line)">保存纠偏</AppButton>
                                <AppButton variant="ghost" size="sm" @click="cancelEdit">取消</AppButton>
                              </div>
                            </template>

                            <template v-else>
                              <!-- 舞台动作指示块（action / stage_direction） -->
                              <div v-if="lineType(item.line) === 'action' || lineType(item.line) === 'stage_direction'" class="flex items-start gap-2.5 rounded-lg bg-surface-canvas p-3">
                                <svg class="mt-0.5 h-4 w-4 shrink-0 text-neutral-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                                  <path d="M15 10l6-3v10l-6-3M3 6h12v12H3z" stroke-linecap="round" stroke-linejoin="round" />
                                </svg>
                                <div class="min-w-0">
                                  <p class="gd-section-label">舞台动作指示 [Action / Direction]</p>
                                  <p
                                    class="mt-0.5 cursor-text text-sm leading-relaxed"
                                    :class="lineType(item.line) === 'stage_direction' ? 'text-neutral-400' : 'text-neutral-700'"
                                    @click="startEdit(item.line)"
                                  >
                                    {{ lineType(item.line) === 'stage_direction' ? `（${item.line.content}）` : item.line.content }}
                                  </p>
                                </div>
                              </div>

                              <!-- 台词卡（说话人 + 情绪标注 + 台词 + 字数） -->
                              <div v-else-if="lineType(item.line) === 'dialogue'" class="rounded-lg border border-hairline-soft bg-surface-raised p-3">
                                <div class="flex flex-wrap items-center justify-between gap-2">
                                  <div class="flex min-w-0 flex-wrap items-center gap-2">
                                    <span class="font-serif text-base font-semibold" :class="speakerToneClass(scene, lineSpeaker(item.line))">{{ lineSpeaker(item.line) || '未命名角色' }}</span>
                                    <span v-if="item.line.emotion" class="rounded bg-surface-canvas px-2 py-0.5 text-caption text-neutral-500">【情绪：{{ item.line.emotion }}】</span>
                                  </div>
                                  <span class="gd-num shrink-0 text-caption text-neutral-400">台词 {{ (item.line.content ?? '').length }} 字</span>
                                </div>
                                <p class="mt-1 cursor-text border-l-2 border-primary-200 pl-2 font-serif text-base italic leading-relaxed text-neutral-800" @click="startEdit(item.line)">
                                  {{ item.line.content }}
                                </p>
                              </div>

                              <!-- 旁白（斜体） -->
                              <p v-else-if="lineType(item.line) === 'narration'" class="cursor-text px-1 py-0.5 text-sm italic leading-relaxed text-neutral-500" @click="startEdit(item.line)">
                                {{ item.line.content }}
                              </p>
                            </template>
                          </div>

                          <!-- 行 hover 操作：编辑 / 改写 -->
                          <div v-if="editingLineId !== String(item.line.id)" class="flex shrink-0 items-center gap-0.5 pt-1 opacity-0 transition-opacity duration-gd-fast focus-within:opacity-100 group-hover:opacity-100">
                            <button type="button" class="rounded p-1 text-neutral-400 transition-colors hover:bg-surface-sunken hover:text-primary-600" title="编辑本行" @click="startEdit(item.line)">
                              <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                                <path d="M16.86 4.49l2.65 2.65L7.5 19.15l-3.39.74.74-3.39L16.86 4.49z" stroke-linecap="round" stroke-linejoin="round" />
                              </svg>
                            </button>
                            <button type="button" class="rounded p-1 text-neutral-400 transition-colors hover:bg-surface-sunken hover:text-primary-600" title="AI 改写（本场范围）" @click="openRewrite(ep.episodeNo, scene.sceneNo)">
                              <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                                <path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.2 2.2m8.4 8.4l2.2 2.2m0-12.8l-2.2 2.2M7.8 16.2l-2.2 2.2" stroke-linecap="round" />
                              </svg>
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>

                    <!-- 场尾操作（对照原型右对齐操作行） -->
                    <div class="mt-3 flex justify-end gap-2">
                      <AppButton variant="ghost" size="sm" @click="openRewrite(ep.episodeNo, scene.sceneNo)">✦ 智能改写本场</AppButton>
                    </div>
                  </section>

                  <!-- 集尾悬念钩子卡（绑定真实 hook_text / target_duration_seconds） -->
                  <div v-if="ep.hookText" class="m-5 flex items-start gap-3.5 rounded-xl border border-primary-200 bg-primary-50 p-4 lg:mx-8">
                    <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full gd-fill-brand">
                      <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                        <path d="M12 17a5 5 0 005-5V5l3 3M12 17a5 5 0 01-5-5V5L4 8m8 9v4" stroke-linecap="round" stroke-linejoin="round" />
                      </svg>
                    </span>
                    <div class="min-w-0 flex-1">
                      <div class="flex flex-wrap items-center justify-between gap-2">
                        <h4 class="text-sm font-semibold text-primary-800">短剧集尾强钩子 (Cliffhanger Hook Suggestion)</h4>
                        <span v-if="ep.targetSeconds && ep.targetSeconds > 0" class="gd-num text-micro font-bold text-primary-700">高潮悬念卡点 · {{ formatDuration(ep.targetSeconds) }}</span>
                      </div>
                      <p class="mt-1 text-caption leading-relaxed text-neutral-600">{{ ep.hookText }}</p>
                    </div>
                  </div>
                </article>
              </div>
            </template>
          </template>
        </div>
      </section>

      <!-- ===== 右画布：AI 编剧助手夜窗面板（深色夜窗 + 极光氛围斑） ===== -->
      <aside v-if="rightPanelOpen" class="relative flex max-h-[45vh] shrink-0 flex-col overflow-hidden border-t border-hairline bg-surface-inset lg:max-h-none lg:w-[30%] lg:min-h-0 lg:max-w-[440px] lg:border-l lg:border-t-0">
        <!-- 极光氛围斑（装饰，取现有渐变令牌） -->
        <div class="pointer-events-none absolute -left-16 -top-16 h-48 w-80 rounded-full bg-grad-primary opacity-25 blur-3xl" aria-hidden="true" />
        <div class="pointer-events-none absolute right-0 top-0 h-32 w-64 bg-grad-gold opacity-10 blur-3xl" aria-hidden="true" />

        <!-- 面板头：Screenwriter Agent + Online 徽标（绑 SSE 状态） -->
        <div class="relative z-10 flex shrink-0 items-center justify-between gap-2 border-b border-white/10 px-4 py-3">
          <div class="flex min-w-0 items-center gap-2.5">
            <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-grad-primary shadow-gd-bloom">
              <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M12 3a4 4 0 014 4c0 1.1-.5 2.1-1.2 2.8.9.9 1.5 2 1.7 3.2H7.5c.2-1.2.8-2.3 1.7-3.2A4 4 0 0112 3zM7 16h10M8 20h8" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <div class="min-w-0">
              <div class="truncate text-sm font-semibold text-neutral-800">AI 编剧助手</div>
              <div class="gd-num text-micro tracking-wider text-neutral-400">SCREENWRITER AGENT</div>
            </div>
          </div>
          <div class="flex shrink-0 items-center gap-1.5">
            <span class="gd-num inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-micro font-medium" :class="agentStatus.cls">
              <span class="h-1.5 w-1.5 rounded-full bg-current" :class="{ 'animate-pulse': agentStatus.pulse }" aria-hidden="true" />
              {{ agentStatus.label }}
            </span>
            <button
              type="button"
              class="rounded p-1 text-neutral-400 transition-colors duration-gd-fast hover:bg-white/5 hover:text-neutral-600"
              aria-label="收起 AI 编剧助手"
              @click="rightPanelOpen = false"
            >
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M9 6l6 6-6 6" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </button>
          </div>
        </div>

        <!-- 面板 Tab：智能任务 / 版本对比 -->
        <div class="relative z-10 shrink-0 px-3 pt-3">
          <SegmentedControl v-model="rightTab" :options="RIGHT_TAB_OPTIONS" block size="sm" aria-label="助手面板视图" />
        </div>

        <!-- 智能任务 Tab：WRITE_TODOS + 指令台 + 安全护栏 + 解析置信度 -->
        <div v-if="rightTab === 'todos'" class="relative z-10 flex-1 overflow-y-auto px-4 py-3">
          <!-- 智能体任务规划 (WRITE_TODOS) -->
          <div class="rounded-lg border border-white/10 bg-white/[0.04] p-3.5">
            <div class="flex items-center justify-between gap-2">
              <span class="gd-section-label">智能体任务规划 (WRITE_TODOS)</span>
              <span class="gd-num shrink-0 text-micro font-bold text-primary-300">
                {{ plannedTodos.length > 0 ? `${doneTodoCount} / ${plannedTodos.length} 阶段完成` : `${doneSteps.length} / ${GENERATE_STEPS.length} 步完成` }}
              </span>
            </div>

            <!-- AI 规划任务清单（真实 write_todos） -->
            <ol v-if="plannedTodos.length > 0" class="mt-2.5 flex flex-col gap-1">
              <li
                v-for="(todo, index) in plannedTodos"
                :key="`panel-todo-${todo.step}`"
                class="flex items-start gap-2.5 rounded-lg p-2"
                :class="index === doneTodoCount && index < plannedTodos.length ? 'bg-white/[0.05]' : ''"
              >
                <span
                  class="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-micro"
                  :class="{ 'gd-fill-mint': index < doneTodoCount, 'gd-fill-brand': index === doneTodoCount, 'border-white/10 bg-white/[0.06] text-neutral-400': index > doneTodoCount }"
                  aria-hidden="true"
                >
                  <template v-if="index < doneTodoCount">✓</template>
                  <svg v-else-if="index === doneTodoCount" class="h-3 w-3 animate-spin" viewBox="0 0 24 24" fill="none">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                  </svg>
                  <template v-else>{{ todo.step || index + 1 }}</template>
                </span>
                <div class="min-w-0">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="text-xs font-medium" :class="index > doneTodoCount ? 'text-neutral-400' : 'text-neutral-800'">{{ todo.title }}</span>
                    <span v-if="index === doneTodoCount && index < plannedTodos.length" class="gd-num text-micro font-bold text-warning-strong">运行中</span>
                  </div>
                  <p v-if="todo.detail && index <= doneTodoCount" class="mt-0.5 text-caption leading-relaxed text-neutral-400">{{ todo.detail }}</p>
                </div>
              </li>
            </ol>

            <!-- 内置流程步骤（未收到规划清单时的兜底） -->
            <ol v-else class="mt-2.5 flex flex-col gap-1">
              <li
                v-for="step in GENERATE_STEPS"
                :key="`panel-step-${step.key}`"
                class="flex items-center gap-2.5 rounded-lg p-2"
                :class="stepState(step.key) === 'active' ? 'bg-white/[0.05]' : ''"
              >
                <span
                  class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-micro"
                  :class="{ 'gd-fill-mint': stepState(step.key) === 'done', 'gd-fill-brand': stepState(step.key) === 'active', 'border-white/10 bg-white/[0.06] text-neutral-400': stepState(step.key) === 'todo' }"
                  aria-hidden="true"
                >
                  <template v-if="stepState(step.key) === 'done'">✓</template>
                  <svg v-else-if="stepState(step.key) === 'active'" class="h-3 w-3 animate-spin" viewBox="0 0 24 24" fill="none">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                  </svg>
                  <template v-else>·</template>
                </span>
                <span class="text-xs" :class="stepState(step.key) === 'todo' ? 'text-neutral-400' : 'text-neutral-700'">{{ step.label }}</span>
              </li>
            </ol>
          </div>

          <!-- 局部改写与智能体指令台 -->
          <div class="mt-4 flex flex-col gap-2.5">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-1.5">
                <svg class="h-4 w-4 text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M12 20h9M16.5 3.5a2.121 2.121 0 013 3L7 19l-4 1 1-4L16.5 3.5z" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                <span class="text-sm font-medium text-neutral-800">局部改写与智能体指令台</span>
              </div>
              <span class="shrink-0 text-micro text-neutral-400">支持自然语言指令</span>
            </div>
            <div class="relative">
              <textarea
                v-model="rewriteFeedback"
                rows="3"
                maxlength="500"
                placeholder="输入改写指令，例如：将第 2 场的挑衅台词强化，突出角色的狂妄背景…"
                class="gd-input gd-input--on-dark resize-none"
              />
              <span class="gd-num absolute bottom-2.5 right-3 text-micro text-neutral-400">{{ rewriteFeedback.length }} / 500 字</span>
            </div>
            <div class="flex items-center justify-between gap-2 rounded-md bg-white/[0.03] px-3 py-1.5 text-caption text-neutral-400">
              <span class="flex min-w-0 items-center gap-1">
                <svg class="h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M4 21v-7m0-4V3m8 18v-9m0-4V3m8 18v-5m0-4V3M1 14h6M9 8h6M17 16h6" stroke-linecap="round" />
                </svg>
                <span class="truncate">范围: {{ rewriteScopeLabel || '默认第一集（整集）' }}</span>
              </span>
              <span v-if="affectedSceneCount > 0" class="gd-num shrink-0 font-semibold text-neutral-500">受影响 {{ affectedSceneCount }} 场</span>
            </div>
            <AppButton
              variant="primary"
              size="md"
              block
              :loading="rewriteSubmitting"
              :disabled="treeView.length === 0"
              :title="treeView.length === 0 ? '还没有剧本内容：请先生成或导入剧本，再使用局部改写' : '按当前范围与指令执行 AI 局部改写'"
              @click="consoleSubmitRewrite"
            >
              执行局部改写
            </AppButton>

            <!-- 常用提示锦囊 -->
            <div class="flex flex-col gap-1.5 pt-1">
              <span class="gd-section-label">常用提示锦囊</span>
              <div class="flex flex-wrap gap-1.5">
                <button
                  v-for="prompt in QUICK_PROMPTS"
                  :key="prompt.label"
                  type="button"
                  class="rounded-full bg-white/[0.05] px-2.5 py-1 text-caption text-neutral-300 transition-colors duration-gd-fast hover:bg-white/10"
                  @click="applyQuickPrompt(prompt.text)"
                >
                  {{ prompt.label }}
                </button>
              </div>
            </div>
          </div>

          <!-- 安全护栏（合规审查状态） -->
          <div class="mt-4 rounded-lg border border-white/10 bg-white/[0.04] p-3.5">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-1.5">
                <svg class="h-4 w-4 text-success" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6l8-3z" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                <span class="gd-section-label">安全护栏 (Safety Shield)</span>
              </div>
              <span class="shrink-0 rounded px-2 py-0.5 text-micro font-bold" :class="complianceBadge.cls">{{ complianceBadge.label }}</span>
            </div>
            <p class="mt-2 text-caption leading-relaxed text-neutral-400">{{ complianceDesc }}</p>
          </div>

          <!-- 解析置信度卡（保留既有低置信定位功能） -->
          <div v-if="lowConfidenceLines.length > 0" class="mt-4 rounded-lg border border-warning-border bg-warning-bg p-3.5">
            <div class="flex items-center justify-between gap-2">
              <h3 class="text-xs font-semibold text-warning-strong">解析置信度</h3>
              <span class="gd-num shrink-0 rounded-full bg-warning-bg-deep px-2 py-0.5 text-micro font-medium text-warning-strong">needs_manual_fix · {{ lowConfidenceLines.length }} 处</span>
            </div>
            <ul class="mt-2.5 space-y-2">
              <li v-for="lowLine in lowConfidenceLines.slice(0, 5)" :key="`low-${lowLine.id}`" class="flex items-center justify-between gap-3 text-xs">
                <button type="button" class="gd-num min-w-0 flex-1 truncate text-left text-neutral-500 transition-colors hover:text-primary-300" :title="lowLine.content" @click="locateLine(lowLine.id)">
                  第 {{ lowLine.no }} 行
                </button>
                <span class="gd-num shrink-0 rounded-full bg-warning-bg-deep px-2 py-0.5 text-micro font-medium text-warning-strong">{{ lowLine.confidence.toFixed(2) }}</span>
              </li>
            </ul>
            <p v-if="lowConfidenceLines.length > 5" class="mt-2 text-caption text-neutral-400">其余 {{ lowConfidenceLines.length - 5 }} 处已在正文中以琥珀纠偏卡标出</p>
          </div>
        </div>

        <!-- 版本对比 Tab（保留既有 diff 功能） -->
        <div v-else class="relative z-10 flex flex-1 flex-col overflow-hidden px-3 py-3">
          <div class="shrink-0 pb-2">
            <AppButton variant="secondary" size="sm" block :loading="diffLoading" @click="loadDiff">对比上一版（v{{ Math.max(1, scriptVersion - 1) }}）</AppButton>
          </div>
          <!-- diff 三色图例 -->
          <div class="flex shrink-0 items-center gap-2 pb-2" aria-hidden="true">
            <span class="rounded bg-success-bg px-1.5 py-0.5 text-micro text-success">新增</span>
            <span class="rounded bg-danger-bg px-1.5 py-0.5 text-micro text-danger">删除</span>
            <span class="rounded bg-warning-bg px-1.5 py-0.5 text-micro text-warning">修改</span>
          </div>
          <div class="flex-1 overflow-y-auto pb-2">
            <p v-if="!diffLoaded && !diffLoading" class="py-8 text-center text-xs text-neutral-400">点击上方按钮查看版本差异</p>
            <p v-else-if="diffLoaded && diffRows.length === 0" class="py-8 text-center text-xs text-neutral-400">两个版本内容一致</p>
            <div v-else class="space-y-0.5 font-mono text-xs leading-relaxed">
              <p
                v-for="(row, index) in diffRows"
                :key="index"
                class="rounded px-2 py-1"
                :class="{
                  'bg-success-bg text-success': diffKind(row) === 'added',
                  'bg-danger-bg text-danger': diffKind(row) === 'removed',
                  'bg-warning-bg text-gd-pending': diffKind(row) === 'changed',
                  'text-neutral-500': diffKind(row) !== 'added' && diffKind(row) !== 'removed' && diffKind(row) !== 'changed',
                }"
              >
                <span class="mr-1.5 select-none opacity-60">{{ diffKind(row) === 'added' ? '+' : diffKind(row) === 'removed' ? '−' : '~' }}</span>
                {{ diffNewContent(row) || row.old_content || '（空行）' }}
              </p>
            </div>
          </div>
        </div>
      </aside>
    </div>

    <!-- ============ 底部固定 HITL 关口条（关口一 · 剧本结构化确认） ============ -->
    <div v-if="scriptTree && !progressMode" class="flex shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-hairline bg-surface-raised px-4 py-2.5">
      <!-- 左：关口状态 -->
      <div class="flex min-w-0 items-center gap-3">
        <span
          class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-primary-200 bg-primary-50 text-primary-700"
          :class="pendingScriptCount > 0 ? 'animate-pulse' : ''"
        >
          <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7l2 2 4-4" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </span>
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-sm font-semibold text-neutral-900">HITL 关口一 · 剧本结构化确认</span>
            <span v-if="pendingScriptCount > 0" class="rounded bg-warning-bg px-1.5 py-0.5 text-micro font-bold text-warning-strong">待人工确认 · {{ pendingScriptCount }} 单</span>
            <span v-else class="rounded bg-surface-sunken px-1.5 py-0.5 text-micro font-semibold text-neutral-400">编辑中</span>
          </div>
          <p class="truncate text-caption text-neutral-500">
            {{ pendingScriptCount > 0 ? `有 ${pendingScriptCount} 单剧本待确认（HITL-1），请前往审批台处理` : '逐行校正与低置信纠偏完成后，提交审核并前往审批台确认；通过后进入资产规划。' }}
          </p>
        </div>
      </div>

      <!-- 中：快捷统计（单集 → 全剧口径，绑定真实派生数据） -->
      <div class="hidden items-center gap-4 rounded-full bg-surface-canvas px-4 py-1.5 lg:flex">
        <span class="text-xs text-neutral-400">剧本字数: <b class="gd-num font-semibold text-neutral-800">{{ wordCount.toLocaleString('zh-CN') }}</b></span>
        <span class="text-neutral-400" aria-hidden="true">•</span>
        <span class="text-xs text-neutral-400">预估时长: <b class="gd-num font-semibold text-primary-700">{{ totalDurationLabel }}</b></span>
        <span class="text-neutral-400" aria-hidden="true">•</span>
        <span class="text-xs text-neutral-400">登场角色: <b class="gd-num font-semibold text-neutral-800">{{ mainCharacters.length }} 人</b></span>
      </div>

      <!-- 右：决策操作 -->
      <div class="flex shrink-0 items-center gap-2">
        <AppButton variant="ghost" size="sm" :disabled="defaultRewriteEp === 0" @click="openRewrite(defaultRewriteEp)">申请局部重写（附带反馈）</AppButton>
        <button
          type="button"
          disabled
          title="版本锁定能力待接入：当前行内编辑始终开放"
          class="hidden cursor-not-allowed items-center gap-1.5 rounded-md border border-hairline bg-surface-canvas px-3 py-1.5 text-xs font-medium text-neutral-400 opacity-60 md:inline-flex"
        >
          手动锁定编辑
        </button>
        <AppButton v-if="pendingScriptCount > 0" variant="primary" size="sm" @click="goReview">前往审批台确认 →</AppButton>
        <AppButton
          v-else-if="!scriptGateStage"
          variant="secondary"
          size="sm"
          title="项目已进入资产或后续阶段，剧本确认已完成"
          @click="goReview"
        >
          剧本确认已完成 · 查看审批台 →
        </AppButton>
        <AppButton v-else variant="primary" size="sm" :loading="reviewSubmitting" @click="submitReview">提交审核 · 前往审批台</AppButton>
      </div>
    </div>

    <!-- ============ 一句话生成弹窗 ============ -->
    <Modal v-model="generateOpen" title="一句话生成剧本" :max-width="480">
      <label for="generate-idea" class="mb-2 block text-caption text-neutral-600">创意描述（≥5 字）</label>
      <textarea
        id="generate-idea"
        v-model="generateIdea"
        rows="4"
        maxlength="1000"
        placeholder="例如：外卖骑手在暴雨夜救下神秘老人，获得预知 60 秒未来的能力，从此卷入一场城市阴谋…"
        class="gd-input resize-none"
      />
      <!-- P2-5 修复：字数即时校验（不足 5 字实时红字提示，不再等到提交才报错） -->
      <p class="mt-1 text-caption" :class="generateIdea.trim().length >= 5 ? 'text-neutral-400' : 'text-danger'">
        {{ generateIdea.length }}/1000 字<template v-if="generateIdea.trim().length < 5"> · 还需 {{ 5 - generateIdea.trim().length }} 字</template>
      </p>

      <!-- 项目参数预览（题材 / 画风 / 篇幅随项目预填，随生成请求由后端读取） -->
      <dl class="mt-4 space-y-2 rounded-lg bg-surface-canvas p-3 text-xs">
        <div class="flex justify-between">
          <dt class="text-neutral-500">题材</dt>
          <dd class="text-neutral-700">{{ GENRE_OPTIONS.find((g) => g.key === project?.genre)?.label ?? project?.genre ?? '—' }}</dd>
        </div>
        <div class="flex justify-between">
          <dt class="text-neutral-500">画风</dt>
          <dd class="text-neutral-700">{{ STYLE_OPTIONS.find((s) => s.key === project?.style_preset_key)?.label ?? '—' }}</dd>
        </div>
        <div class="flex justify-between">
          <dt class="text-neutral-500">篇幅</dt>
          <dd class="gd-num text-neutral-700">{{ project?.target_episode_count ?? '—' }} 集</dd>
        </div>
        <div class="flex justify-between border-t border-hairline pt-2">
          <dt class="text-neutral-500">粗略预估成本</dt>
          <dd class="font-medium text-neutral-700">提交后执行预算预检并冻结实际所需积分</dd>
        </div>
      </dl>

      <template #footer>
        <AppButton variant="secondary" size="md" :disabled="generateSubmitting" @click="generateOpen = false">取消</AppButton>
        <AppButton
          variant="primary"
          size="md"
          :disabled="generateSubmitting || generateIdea.trim().length < 5"
          :title="generateIdea.trim().length < 5 ? '创意描述至少 5 个字' : ''"
          :loading="generateSubmitting"
          @click="submitGenerate"
        >开始生成</AppButton>
      </template>
    </Modal>

    <!-- ============ 局部改写弹窗（场 / 集 / HITL 条入口共用） ============ -->
    <Modal v-model="rewriteOpen" title="AI 局部改写" :max-width="440">
      <p class="text-caption text-neutral-400">改写范围：{{ rewriteScopeLabel }}</p>
      <label for="rewrite-feedback" class="mb-2 mt-4 block text-caption text-neutral-600">改写要求（≥5 字）</label>
      <textarea
        id="rewrite-feedback"
        v-model="rewriteFeedback"
        rows="3"
        maxlength="500"
        placeholder="例如：节奏太拖，把冲突提前；女主台词更口语化一些…"
        class="gd-input resize-none"
      />
      <p class="mt-1 text-caption" :class="rewriteFeedback.trim().length >= 5 ? 'text-neutral-400' : 'text-danger'">{{ rewriteFeedback.trim().length }}/5 字</p>
      <template #footer>
        <AppButton variant="secondary" size="md" :disabled="rewriteSubmitting" @click="rewriteOpen = false">取消</AppButton>
        <AppButton variant="primary" size="md" :loading="rewriteSubmitting" @click="submitRewrite">提交改写</AppButton>
      </template>
    </Modal>

    <!-- ============ 历史快照弹窗（P2-4 修复：版本列表 + 恢复） ============ -->
    <Modal v-model="snapshotOpen" title="历史快照" :max-width="520">
      <div v-if="snapshotLoading" class="py-6 text-center text-caption text-neutral-400">加载中…</div>
      <div v-else-if="snapshotItems.length === 0" class="py-6 text-center text-caption text-neutral-400">暂无快照（保存或审核通过后自动生成）</div>
      <ul v-else class="max-h-80 space-y-1.5 overflow-y-auto">
        <li
          v-for="v in snapshotItems"
          :key="v.version_no"
          class="flex items-center justify-between gap-3 rounded-md border border-hairline px-3 py-2"
        >
          <div class="min-w-0">
            <p class="truncate text-xs font-medium text-neutral-800">
              v{{ v.version_no }}
              <span v-if="v.is_current" class="ml-1 rounded bg-primary-50 px-1.5 py-0.5 text-[10px] text-primary-700">当前</span>
              <span v-if="v.version_label" class="ml-1 text-neutral-400">{{ v.version_label }}</span>
            </p>
            <p class="mt-0.5 truncate text-caption text-neutral-400">
              {{ v.source_type }} · {{ v.total_words }} 字 · {{ v.created_at ? v.created_at.slice(0, 16).replace('T', ' ') : '—' }}
            </p>
          </div>
          <AppButton
            size="sm"
            :variant="v.is_current ? 'secondary' : 'primary'"
            :disabled="v.is_current || restoringVersionNo !== null"
            :loading="restoringVersionNo === v.version_no"
            @click="restoreSnapshot(v)"
          >
            {{ v.is_current ? '当前版本' : '恢复' }}
          </AppButton>
        </li>
      </ul>
      <template #footer>
        <AppButton variant="secondary" size="md" @click="snapshotOpen = false">关闭</AppButton>
      </template>
    </Modal>

    <!-- 隐藏文件选择框（创作模式 segmented「长篇小说与剧本解析」入口） -->
    <input ref="fileInput" type="file" accept=".txt,.md" class="hidden" @change="onFileChange" />
  </div>
</template>

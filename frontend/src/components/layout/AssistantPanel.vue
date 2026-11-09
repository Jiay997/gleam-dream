<!--
  文件级注释：右侧 AI 创作助手面板（PRD V6.4 第 6 章多智能体架构）。
  以当前项目上下文为输入，提供 10 个 Agent 场景的咨询、建议与受控动作；
  写操作以动作卡片形式出现，必须用户确认后由服务端执行（HITL）。

  尺寸交互：
  - 桌面端（≥1280px）：拖拽面板左边缘调整宽度（280~720px，且保证主内容最小宽度），
    支持双击复位、方向键/Home/End 键盘调整，宽度持久化；
  - 窄屏：切换为底部抽屉（遮挡主内容），拖拽顶部手柄调整高度（最小约 360px / 55vh，
    最大 90vh），同样支持双击复位与键盘调整，高度持久化。
-->
<script setup lang="ts">
/**
 * AssistantPanel AI 创作助手面板。
 * props：
 * - narrow：是否为窄屏（由 AppShell 视口断点传入），窄屏渲染为底部抽屉。
 * 无 emit；依赖 ui（面板开关/尺寸）、project（项目列表与当前项目）、
 * assistant（会话状态）三个 store。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AssistantMessageItem from './AssistantMessageItem.vue'
import AppButton from '@/components/common/AppButton.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import { useAssistantStore } from '@/stores/assistant'
import { useProjectStore } from '@/stores/project'
import {
  ASSISTANT_PANEL_DEFAULT_WIDTH,
  ASSISTANT_PANEL_MAX_WIDTH,
  ASSISTANT_PANEL_MIN_HEIGHT,
  ASSISTANT_PANEL_MIN_WIDTH,
  useUiStore,
} from '@/stores/ui'

const props = withDefaults(
  defineProps<{
    /** 是否窄屏（<1280px）：渲染为底部抽屉 */
    narrow?: boolean
  }>(),
  { narrow: false },
)

const route = useRoute()
const uiStore = useUiStore()
const projectStore = useProjectStore()
const assistant = useAssistantStore()

/** 项目状态中文映射 */
const STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  scripting: '剧本生成中',
  script_review: '剧本审核',
  asset_preparing: '资产准备',
  asset_review: '资产审核',
  storyboarding: '分镜中',
  storyboard_review: '分镜审核',
  rendering: '生成中',
  post_producing: '后期中',
  final_review: '成片审核',
  completed: '已完成',
  archived: '已归档',
  paused: '已暂停',
  failed: '失败',
}

/** 消息滚动容器 */
const messageListRef = ref<HTMLElement | null>(null)
/** 输入框 */
const inputRef = ref<HTMLTextAreaElement | null>(null)
/** 当前选中的项目 ID（面板内选择，优先于路由） */
const selectedProjectId = ref('')
/** 当前选中的集号（与 store 同步） */
const selectedEpisode = ref(1)
/** 视口宽度（响应窗口缩放，用于收敛面板上限） */
const viewportWidth = ref(typeof window === 'undefined' ? 1440 : window.innerWidth)
/** 视口高度 */
const viewportHeight = ref(typeof window === 'undefined' ? 900 : window.innerHeight)
/** 桌面面板宽度（拖拽时实时更新，结束时持久化） */
const panelWidth = ref(uiStore.assistantPanelWidth)
/** 抽屉高度（px） */
const panelHeight = ref(
  uiStore.assistantPanelHeight ||
    Math.round((typeof window === 'undefined' ? 900 : window.innerHeight) * 0.6),
)
/** 是否正在拖拽尺寸 */
const resizing = ref(false)

/** 拖拽轴：width / height / null */
let resizeAxis: 'width' | 'height' | null = null
/** 拖拽起始坐标 */
let dragStart = 0
/** 拖拽起始尺寸 */
let dragOrigin = 0

/** 路由中的项目 ID（项目列表页为空） */
const routeProjectId = computed(() => {
  const value = route.params.projectId
  return typeof value === 'string' ? value : ''
})

/** 项目下拉选项 */
const projectOptions = computed(() => projectStore.projects)

/** 实际生效的项目 ID */
const activeProjectId = computed(() => selectedProjectId.value || routeProjectId.value)

/** 上下文切片 */
const context = computed(() => assistant.contextData?.context ?? null)
/** 分集列表 */
const episodes = computed(() => context.value?.episodes ?? [])
/** 当前项目标题 */
const projectTitle = computed(
  () => context.value?.project.title || projectStore.currentProject?.title || '',
)
/** 当前项目状态文案 */
const statusLabel = computed(
  () => STATUS_LABELS[context.value?.project.status ?? ''] ?? context.value?.project.status ?? '-',
)
/** 预算文案 */
const budgetLabel = computed(() => {
  const budget = context.value?.budget
  if (!budget) return '预算未知'
  if (budget.budget_credits <= 0) return '预算不限'
  return `积分 ${budget.spent_credits}/${budget.budget_credits}`
})
/** 当前集分镜板摘要 */
const currentStoryboard = computed(() => {
  const ep = assistant.episodeNo
  return context.value?.storyboards.find((row) => row.episode_no === ep) ?? null
})
/** 是否已归档（动作全部只读） */
const archived = computed(() => context.value?.project.status === 'archived')
/** 是否无项目 */
const noProject = computed(() => !activeProjectId.value)
/** 是否无消息 */
const emptyConversation = computed(() => assistant.messages.length === 0 && !assistant.loading)
/** 快捷场景（最多展示 10 个） */
const scenarioChips = computed(() => assistant.scenarios.slice(0, 10))

/** 桌面面板最大宽度：为主内容预留 480px，并扣除全局侧栏 232px */
const maxPanelWidth = computed(() => {
  const available = viewportWidth.value - 232 - 480
  return Math.max(ASSISTANT_PANEL_MIN_WIDTH, Math.min(ASSISTANT_PANEL_MAX_WIDTH, available))
})

/** 抽屉最小高度：360px 与 55vh 取小，并保证不低于全局最小 */
const minPanelHeight = computed(() =>
  Math.max(ASSISTANT_PANEL_MIN_HEIGHT, Math.min(360, Math.round(viewportHeight.value * 0.55))),
)
/** 抽屉最大高度：90vh */
const maxPanelHeight = computed(() =>
  Math.max(minPanelHeight.value, Math.round(viewportHeight.value * 0.9)),
)
/** 抽屉默认高度：60vh */
const defaultPanelHeight = computed(() =>
  Math.min(maxPanelHeight.value, Math.max(minPanelHeight.value, Math.round(viewportHeight.value * 0.6))),
)

/**
 * 数值收敛到区间。
 * @param value 原始值
 * @param min 下限
 * @param max 上限
 * @returns 收敛后的值
 */
function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

/**
 * 滚动消息区到底部。
 * @returns 无
 */
function scrollToBottom(): void {
  void nextTick(() => {
    const container = messageListRef.value
    if (container) container.scrollTop = container.scrollHeight
  })
}

/**
 * 同步视口尺寸并收敛当前面板尺寸。
 * @returns 无
 */
function syncViewportSize(): void {
  viewportWidth.value = window.innerWidth
  viewportHeight.value = window.innerHeight
  panelWidth.value = clamp(panelWidth.value, ASSISTANT_PANEL_MIN_WIDTH, maxPanelWidth.value)
  panelHeight.value = clamp(
    panelHeight.value,
    minPanelHeight.value,
    maxPanelHeight.value,
  )
}

/**
 * 开始拖拽调整宽度（桌面端左边缘）。
 * @param event 指针事件
 * @returns 无
 */
function startWidthResize(event: PointerEvent): void {
  if (props.narrow || event.button !== 0) return
  event.preventDefault()
  resizeAxis = 'width'
  resizing.value = true
  dragStart = event.clientX
  dragOrigin = panelWidth.value
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}

/**
 * 开始拖拽调整高度（移动端顶部手柄）。
 * @param event 指针事件
 * @returns 无
 */
function startHeightResize(event: PointerEvent): void {
  if (!props.narrow || event.button !== 0) return
  event.preventDefault()
  resizeAxis = 'height'
  resizing.value = true
  dragStart = event.clientY
  dragOrigin = panelHeight.value
  document.body.style.cursor = 'row-resize'
  document.body.style.userSelect = 'none'
}

/**
 * 拖拽移动：桌面向左拖增大宽度，移动端向上拖增大高度。
 * @param event 指针事件
 * @returns 无
 */
function onResizeMove(event: PointerEvent): void {
  if (!resizeAxis) return
  if (resizeAxis === 'width') {
    const next = dragOrigin + (dragStart - event.clientX)
    panelWidth.value = clamp(next, ASSISTANT_PANEL_MIN_WIDTH, maxPanelWidth.value)
  } else {
    const next = dragOrigin + (dragStart - event.clientY)
    panelHeight.value = clamp(next, minPanelHeight.value, maxPanelHeight.value)
  }
}

/**
 * 结束拖拽并持久化最终尺寸。
 * @returns 无
 */
function onResizeEnd(): void {
  if (!resizeAxis) return
  if (resizeAxis === 'width') {
    uiStore.setAssistantPanelWidth(panelWidth.value)
  } else {
    uiStore.setAssistantPanelHeight(panelHeight.value)
  }
  resizeAxis = null
  resizing.value = false
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
}

/**
 * 桌面宽度键盘调整（← 增大 / → 减小 / Home 复位 / End 最大）。
 * @param event 键盘事件
 * @returns 无
 */
function onWidthKeydown(event: KeyboardEvent): void {
  const step = event.shiftKey ? 48 : 16
  let next = panelWidth.value
  if (event.key === 'ArrowLeft') next += step
  else if (event.key === 'ArrowRight') next -= step
  else if (event.key === 'Home') next = ASSISTANT_PANEL_DEFAULT_WIDTH
  else if (event.key === 'End') next = maxPanelWidth.value
  else return
  event.preventDefault()
  panelWidth.value = clamp(next, ASSISTANT_PANEL_MIN_WIDTH, maxPanelWidth.value)
  uiStore.setAssistantPanelWidth(panelWidth.value)
}

/**
 * 抽屉高度键盘调整（↑ 增高 / ↓ 降低 / Home 复位 / End 最大）。
 * @param event 键盘事件
 * @returns 无
 */
function onHeightKeydown(event: KeyboardEvent): void {
  const step = event.shiftKey ? 48 : 24
  let next = panelHeight.value
  if (event.key === 'ArrowUp') next += step
  else if (event.key === 'ArrowDown') next -= step
  else if (event.key === 'Home') next = defaultPanelHeight.value
  else if (event.key === 'End') next = maxPanelHeight.value
  else return
  event.preventDefault()
  panelHeight.value = clamp(next, minPanelHeight.value, maxPanelHeight.value)
  uiStore.setAssistantPanelHeight(panelHeight.value)
}

/**
 * 双击复位宽度。
 * @returns 无
 */
function resetWidth(): void {
  panelWidth.value = clamp(
    ASSISTANT_PANEL_DEFAULT_WIDTH,
    ASSISTANT_PANEL_MIN_WIDTH,
    maxPanelWidth.value,
  )
  uiStore.setAssistantPanelWidth(panelWidth.value)
}

/**
 * 双击复位高度。
 * @returns 无
 */
function resetHeight(): void {
  panelHeight.value = defaultPanelHeight.value
  uiStore.setAssistantPanelHeight(panelHeight.value)
}

/**
 * 文档级按键：窄屏抽屉支持 Esc 关闭。
 * @param event 键盘事件
 * @returns 无
 */
function onDocKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && props.narrow && !resizing.value) {
    uiStore.toggleAssistantPanel()
  }
}

/**
 * 初始化：加载项目列表并打开默认项目会话。
 * @returns 无
 */
async function bootstrap(): Promise<void> {
  if (projectStore.projects.length === 0) {
    try {
      await projectStore.loadProjects()
    } catch {
      // 项目列表失败时保留路由项目上下文
    }
  }
  const initial =
    routeProjectId.value ||
    assistant.projectId ||
    projectStore.currentProjectId ||
    String(projectStore.projects[0]?.id ?? '')
  if (!initial) return
  selectedProjectId.value = initial
  await assistant.open(initial, selectedEpisode.value)
  selectedEpisode.value = assistant.episodeNo
  scrollToBottom()
}

/**
 * 切换项目（仅切换助手上下文，不改变当前页面路由）。
 * @param event 下拉变更事件
 * @returns 无
 */
async function onProjectChange(event: Event): Promise<void> {
  const target = event.target as HTMLSelectElement
  const nextId = target.value
  if (!nextId || nextId === activeProjectId.value) return
  selectedProjectId.value = nextId
  selectedEpisode.value = 1
  await assistant.open(nextId, 1)
  selectedEpisode.value = assistant.episodeNo
  scrollToBottom()
}

/**
 * 切换集号并刷新上下文。
 * @param event 下拉变更事件
 * @returns 无
 */
async function onEpisodeChange(event: Event): Promise<void> {
  const next = Number((event.target as HTMLSelectElement).value)
  selectedEpisode.value = Number.isFinite(next) ? Math.max(1, next) : 1
  await assistant.setEpisode(selectedEpisode.value)
  selectedEpisode.value = assistant.episodeNo
  scrollToBottom()
}

/**
 * 点击快捷场景：把提示词填入输入框并聚焦（不自动发送）。
 * @param prompt 提示词
 * @returns 无
 */
function applyScenario(prompt: string): void {
  assistant.draft = prompt
  inputRef.value?.focus()
}

/**
 * 点击建议：填入输入框。
 * @param text 建议文本
 * @returns 无
 */
function applySuggestion(text: string): void {
  assistant.draft = text
  inputRef.value?.focus()
}

/**
 * 发送消息。
 * @returns 无
 */
async function onSend(): Promise<void> {
  if (assistant.sending || archived.value) return
  const ok = await assistant.send(assistant.draft)
  if (ok) scrollToBottom()
}

/**
 * 输入框快捷键：Ctrl/⌘ + Enter 发送。
 * @param event 键盘事件
 * @returns 无
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault()
    void onSend()
  }
}

/**
 * 确认执行动作。
 * @param actionId 动作 ID
 * @returns 无
 */
async function onExecute(actionId: number): Promise<void> {
  if (archived.value) return
  const ok = await assistant.execute(actionId)
  if (ok) scrollToBottom()
}

/**
 * 新建对话（软删除当前会话并重建）。
 * @returns 无
 */
async function onResetConversation(): Promise<void> {
  await assistant.clear()
  selectedEpisode.value = assistant.episodeNo
  scrollToBottom()
}

/**
 * 关闭面板。
 * @returns 无
 */
function onClose(): void {
  uiStore.toggleAssistantPanel()
}

/** 消息数量变化时自动滚动到底部 */
watch(
  () => assistant.messages.length,
  (next, previous) => {
    if (next !== previous) scrollToBottom()
  },
)

/** store 中的集号变化时同步下拉 */
watch(
  () => assistant.episodeNo,
  (value) => {
    selectedEpisode.value = value
  },
)

/** 窄屏形态切换时，将当前尺寸收敛到对应范围 */
watch(
  () => props.narrow,
  (narrow) => {
    if (narrow) {
      panelHeight.value = clamp(
        uiStore.assistantPanelHeight || defaultPanelHeight.value,
        minPanelHeight.value,
        maxPanelHeight.value,
      )
    } else {
      panelWidth.value = clamp(
        uiStore.assistantPanelWidth,
        ASSISTANT_PANEL_MIN_WIDTH,
        maxPanelWidth.value,
      )
    }
  },
)

/** 路由项目变化时切换助手上下文 */
watch(routeProjectId, (value) => {
  if (value && value !== selectedProjectId.value) {
    selectedProjectId.value = value
    void assistant.open(value, 1).then(() => {
      selectedEpisode.value = assistant.episodeNo
      scrollToBottom()
    })
  }
})

onMounted(() => {
  syncViewportSize()
  panelWidth.value = clamp(
    uiStore.assistantPanelWidth,
    ASSISTANT_PANEL_MIN_WIDTH,
    maxPanelWidth.value,
  )
  panelHeight.value = clamp(
    uiStore.assistantPanelHeight || defaultPanelHeight.value,
    minPanelHeight.value,
    maxPanelHeight.value,
  )
  window.addEventListener('resize', syncViewportSize)
  window.addEventListener('pointermove', onResizeMove)
  window.addEventListener('pointerup', onResizeEnd)
  window.addEventListener('pointercancel', onResizeEnd)
  document.addEventListener('keydown', onDocKeydown)
  void bootstrap()
})

onBeforeUnmount(() => {
  onResizeEnd()
  window.removeEventListener('resize', syncViewportSize)
  window.removeEventListener('pointermove', onResizeMove)
  window.removeEventListener('pointerup', onResizeEnd)
  window.removeEventListener('pointercancel', onResizeEnd)
  document.removeEventListener('keydown', onDocKeydown)
  // 保留会话状态（store 常驻），只清理滚动引用
  messageListRef.value = null
})
</script>

<template>
  <Teleport to="body" :disabled="!narrow">
    <!-- 窄屏遮罩：点击关闭抽屉 -->
    <div
      v-if="narrow"
      class="fixed inset-0 z-drawer bg-overlay backdrop-blur-[2px]"
      aria-hidden="true"
      @click="onClose"
    />

    <aside
      class="flex flex-col bg-panel"
      :class="
        narrow
          ? 'fixed inset-x-0 bottom-0 z-drawer overflow-hidden rounded-t-xl border-t border-hairline shadow-gd-3'
          : 'relative shrink-0 border-l border-hairline'
      "
      :style="narrow ? { height: `${panelHeight}px` } : { width: `${panelWidth}px` }"
      :role="narrow ? 'dialog' : undefined"
      :aria-modal="narrow ? 'true' : undefined"
      aria-label="AI 创作助手"
    >
      <!-- 桌面端：左边缘宽度拖拽手柄（键盘可操作） -->
      <div
        v-if="!narrow"
        class="group absolute -left-1 top-0 z-sticky flex h-full w-2 cursor-col-resize touch-none items-center justify-center"
        role="separator"
        aria-orientation="vertical"
        aria-label="调整 AI 助手面板宽度"
        :aria-valuemin="ASSISTANT_PANEL_MIN_WIDTH"
        :aria-valuemax="maxPanelWidth"
        :aria-valuenow="Math.round(panelWidth)"
        tabindex="0"
        @pointerdown="startWidthResize"
        @dblclick="resetWidth"
        @keydown="onWidthKeydown"
      >
        <span
          class="h-10 w-0.5 rounded-full transition-colors"
          :class="resizing ? 'bg-primary-500' : 'bg-transparent group-hover:bg-primary-300'"
        />
      </div>

      <!-- 窄屏：顶部高度拖拽手柄 -->
      <div
        v-else
        class="flex h-5 w-full shrink-0 cursor-row-resize touch-none items-center justify-center"
        role="separator"
        aria-orientation="horizontal"
        aria-label="调整 AI 助手面板高度"
        :aria-valuemin="minPanelHeight"
        :aria-valuemax="maxPanelHeight"
        :aria-valuenow="Math.round(panelHeight)"
        tabindex="0"
        @pointerdown="startHeightResize"
        @dblclick="resetHeight"
        @keydown="onHeightKeydown"
      >
        <span
          class="h-1 w-10 rounded-full transition-colors"
          :class="resizing ? 'bg-primary-500' : 'bg-neutral-300/60'"
        />
      </div>

      <header class="flex h-12 shrink-0 items-center justify-between border-b border-hairline px-3">
        <h3 class="flex items-center gap-1.5 text-sm font-semibold text-neutral-800">
          <svg class="h-4 w-4 text-primary-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <path
              d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          AI 创作助手
        </h3>
        <div class="flex items-center gap-1">
          <button
            type="button"
            class="rounded-md px-2 py-1 text-micro text-neutral-400 transition-colors hover:bg-surface-sunken hover:text-neutral-600 disabled:opacity-40"
            title="新建对话"
            :disabled="noProject || assistant.sending"
            @click="onResetConversation"
          >
            新对话
          </button>
          <button
            type="button"
            class="rounded-md p-1 text-neutral-400 transition-colors hover:bg-surface-sunken hover:text-neutral-600"
            title="收起面板"
            @click="onClose"
          >
            <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M6 18L18 6M6 6l12 12" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </div>
      </header>

      <!-- 上下文：项目 / 集号 / 状态与预算 -->
      <div v-if="!noProject" class="shrink-0 space-y-2 border-b border-hairline px-3 py-2">
        <select class="gd-select h-8 w-full text-caption" :value="activeProjectId" @change="onProjectChange">
          <option v-if="projectOptions.length === 0" :value="activeProjectId">{{ projectTitle || '当前项目' }}</option>
          <option v-for="project in projectOptions" :key="project.id" :value="String(project.id)">
            {{ project.title }}
          </option>
        </select>
        <div class="flex items-center gap-2">
          <select
            v-if="episodes.length > 0"
            class="gd-select h-7 w-24 text-caption"
            :value="selectedEpisode"
            aria-label="聚焦集号"
            @change="onEpisodeChange"
          >
            <option v-for="episode in episodes" :key="episode.episode_no" :value="episode.episode_no">
              第 {{ episode.episode_no }} 集
            </option>
          </select>
          <span v-else class="text-micro text-neutral-400">尚无分集</span>
          <span class="rounded-full border border-hairline px-2 py-0.5 text-micro text-neutral-500">
            {{ statusLabel }}
          </span>
          <span class="rounded-full border border-hairline px-2 py-0.5 text-micro text-neutral-500">
            {{ budgetLabel }}
          </span>
        </div>
        <div v-if="context" class="flex flex-wrap items-center gap-1.5 text-micro text-neutral-400">
          <span v-if="context.script">剧本 v{{ context.script.version_no }}</span>
          <span v-else>尚无剧本</span>
          <span v-if="currentStoryboard">· 分镜 {{ currentStoryboard.shot_count }} 镜</span>
          <span v-if="context.story_bible">
            · Story Bible {{ Math.round(context.story_bible.completeness * 100) }}%
          </span>
          <span v-if="context.approvals.length" class="text-warning-strong">
            · {{ context.approvals.length }} 项待审批
          </span>
        </div>
      </div>

      <!-- 消息区 -->
      <div ref="messageListRef" class="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        <div v-if="noProject" class="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
          <span class="flex h-12 w-12 items-center justify-center rounded-2xl bg-primary-50 text-primary-500" aria-hidden="true">
            <svg class="h-6 w-6" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
              <path
                d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            </svg>
          </span>
          <p class="text-sm font-medium text-neutral-700">先选择一个项目</p>
          <p class="text-xs leading-relaxed text-neutral-400">
            创作助手会读取当前项目的剧本、分镜、配音与 Story Bible，提供分镜建议、台词润色与一致性检查。
          </p>
        </div>

        <ErrorRetryBlock
          v-else-if="assistant.error && assistant.messages.length === 0 && !assistant.loading"
          message="助手加载失败"
          :description="assistant.error"
          @retry="assistant.retry()"
        />

        <div v-else-if="assistant.loading" class="space-y-3 py-2">
          <div class="h-16 animate-pulse rounded-lg bg-surface-sunken" />
          <div class="h-16 animate-pulse rounded-lg bg-surface-sunken" />
          <div class="h-16 animate-pulse rounded-lg bg-surface-sunken" />
        </div>

        <div v-else-if="emptyConversation" class="py-4">
          <p class="text-sm font-medium text-neutral-700">你好，我是创作助手</p>
          <p class="mt-1 text-caption leading-relaxed text-neutral-400">
            我可以基于当前项目回答剧本、分镜、配音、连续性与发行问题。涉及改写、生成等写操作时，会先给出待确认的动作卡片。
          </p>
        </div>

        <template v-else>
          <AssistantMessageItem
            v-for="message in assistant.messages"
            :key="message.id"
            :message="message"
            :executing-ids="assistant.executing"
            @execute="onExecute"
            @dismiss="assistant.dismissAction"
            @suggestion="applySuggestion"
          />
          <div v-if="assistant.sending" class="my-2 flex items-center gap-2">
            <span class="h-1.5 w-1.5 animate-pulse rounded-full bg-primary-500" />
            <span class="text-caption text-neutral-400">助手正在结合项目上下文思考…</span>
          </div>
        </template>
      </div>

      <!-- 输入区 -->
      <div
        class="shrink-0 border-t border-hairline px-3 py-2"
        :style="narrow ? { paddingBottom: 'max(env(safe-area-inset-bottom), 8px)' } : undefined"
      >
        <div v-if="archived" class="mb-2 rounded-md border border-warning-border bg-warning-bg px-2 py-1.5 text-micro leading-relaxed text-warning-strong">
          项目已归档，只可咨询，无法执行写操作。
        </div>
        <div v-else-if="assistant.degraded" class="mb-2 rounded-md border border-warning-border bg-warning-bg px-2 py-1.5 text-micro leading-relaxed text-warning-strong">
          模型服务暂时不可用，问题已保留，可稍后重试。
        </div>
        <div v-else-if="assistant.error && assistant.messages.length > 0" class="mb-2 flex items-start justify-between gap-2 rounded-md border border-danger-border bg-danger-bg px-2 py-1.5">
          <span class="text-micro leading-relaxed text-danger-strong">{{ assistant.error }}</span>
          <button type="button" class="shrink-0 text-micro text-neutral-500 underline" @click="assistant.error = ''">
            知道了
          </button>
        </div>

        <div v-if="scenarioChips.length" class="mb-2 flex gap-1.5 overflow-x-auto pb-1">
          <button
            v-for="scenario in scenarioChips"
            :key="scenario.key"
            type="button"
            class="shrink-0 rounded-full border px-2 py-1 text-micro transition-colors"
            :class="
              assistant.scenarioHint === scenario.key
                ? 'border-primary-200 bg-primary-50 text-primary-300'
                : 'border-hairline bg-surface-sunken text-neutral-500 hover:border-primary-200 hover:text-primary-300'
            "
            :title="scenario.description"
            @click="applyScenario(scenario.prompt)"
          >
            {{ scenario.label }}
          </button>
        </div>

        <textarea
          ref="inputRef"
          v-model="assistant.draft"
          class="gd-textarea min-h-[64px] w-full resize-none text-sm"
          maxlength="2000"
          placeholder="问剧本、分镜、配音或连续性问题…（Ctrl+Enter 发送）"
          :disabled="noProject || assistant.sending || archived"
          @keydown="onKeydown"
        />
        <div class="mt-2 flex items-center justify-between gap-2">
          <span class="text-micro text-neutral-400">{{ assistant.draft.length }}/2000</span>
          <AppButton
            size="sm"
            :loading="assistant.sending"
            :disabled="noProject || archived || !assistant.draft.trim()"
            @click="onSend"
          >
            发送
          </AppButton>
        </div>
        <p class="mt-1.5 text-center text-micro text-neutral-400">
          AI 生成内容仅供参考，写操作需确认后执行。
        </p>
      </div>
    </aside>
  </Teleport>
</template>

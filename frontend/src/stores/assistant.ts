/**
 * 文件级注释：AI 创作助手会话 store。
 *
 * 维护当前项目/集号下的会话、消息、动作与上下文；面板关闭（v-if 卸载）
 * 后状态仍保留，重新打开无需重新拉取。切换项目时中止进行中的请求并重置。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  clearAssistantConversation,
  executeAssistantAction,
  getAssistantContext,
  getAssistantConversation,
  sendAssistantMessage,
  type AssistantAction,
  type AssistantContextResponse,
  type AssistantConversation,
  type AssistantMessage,
  type AssistantScenario,
} from '@/api/assistant'

/** 生成客户端消息 ID（优先生成 UUID，非安全上下文时退化） */
function createClientMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `msg-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

/** 乐观占位消息 ID 起点（真实 ID 为正数） */
let optimisticSeq = -1

/**
 * 助手 store：会话 / 消息 / 动作 / 上下文。
 */
export const useAssistantStore = defineStore('assistant', () => {
  /** 当前项目 ID（空串表示未选择） */
  const projectId = ref('')
  /** 当前聚焦集号 */
  const episodeNo = ref(1)
  /** 会话元信息 */
  const conversation = ref<AssistantConversation | null>(null)
  /** 消息列表（时间正序） */
  const messages = ref<AssistantMessage[]>([])
  /** 会话动作（全局视图，含已失败/已过期） */
  const actions = ref<AssistantAction[]>([])
  /** 场景清单 */
  const scenarios = ref<AssistantScenario[]>([])
  /** Agent 中文名映射 */
  const agentLabels = ref<Record<string, string>>({})
  /** 上下文响应 */
  const contextData = ref<AssistantContextResponse | null>(null)
  /** 首屏加载中 */
  const loading = ref(false)
  /** 发送中 */
  const sending = ref(false)
  /** 正在执行的动作 ID 集合 */
  const executing = ref<number[]>([])
  /** 是否处于模型降级状态 */
  const degraded = ref(false)
  /** 最近一次错误文案 */
  const error = ref('')
  /** 输入草稿（切换面板不丢失） */
  const draft = ref('')
  /** 已本地忽略的动作 ID */
  const dismissed = ref<number[]>([])
  /** 最近一次回复的意图（供面板高亮场景） */
  const scenarioHint = ref<string | null>(null)

  /** 进行中的请求控制器 */
  let controller: AbortController | null = null

  /** 是否已加载过当前项目会话 */
  const initialized = computed(() => Boolean(conversation.value) && projectId.value !== '')
  /** 待确认动作（未过期、未本地忽略） */
  const pendingActions = computed(() =>
    actions.value.filter(
      (item) =>
        item.status === 'pending' &&
        !dismissed.value.includes(item.id) &&
        messages.value.some((msg) => msg.actions.some((act) => act.id === item.id)),
    ),
  )

  /**
   * 打开项目会话：项目变化时重建，集号变化时同步会话。
   * @param nextProjectId 项目 ID
   * @param nextEpisodeNo 聚焦集号
   * @returns 无
   */
  async function open(nextProjectId: string, nextEpisodeNo = 1): Promise<void> {
    const normalized = String(nextProjectId || '')
    if (!normalized) {
      reset()
      return
    }
    if (normalized !== projectId.value) {
      reset()
      projectId.value = normalized
      episodeNo.value = Math.max(1, nextEpisodeNo)
      await refreshAll()
      return
    }
    if (episodeNo.value !== Math.max(1, nextEpisodeNo)) {
      episodeNo.value = Math.max(1, nextEpisodeNo)
      await refreshAll()
    }
  }

  /**
   * 切换聚焦集号（同时刷新上下文与会话）。
   * @param nextEpisodeNo 新集号
   * @returns 无
   */
  async function setEpisode(nextEpisodeNo: number): Promise<void> {
    const target = Math.max(1, Number(nextEpisodeNo) || 1)
    if (target === episodeNo.value) return
    episodeNo.value = target
    await refreshAll()
  }

  /**
   * 同时刷新上下文与会话详情。
   * @returns 无
   */
  async function refreshAll(): Promise<void> {
    if (!projectId.value) return
    controller?.abort()
    controller = new AbortController()
    loading.value = true
    error.value = ''
    try {
      const [contextResponse, detail] = await Promise.all([
        getAssistantContext(projectId.value, episodeNo.value),
        getAssistantConversation(projectId.value, episodeNo.value),
      ])
      contextData.value = contextResponse
      scenarios.value = detail.scenarios.length > 0 ? detail.scenarios : contextResponse.scenarios
      agentLabels.value = contextResponse.agent_labels
      conversation.value = detail.conversation
      messages.value = detail.messages
      actions.value = detail.actions
      episodeNo.value = detail.conversation.episode_no
    } catch (err) {
      error.value = err instanceof Error ? err.message : '助手加载失败'
    } finally {
      loading.value = false
    }
  }

  /**
   * 仅刷新上下文（动作执行后数据可能变化）。
   * @returns 无
   */
  async function refreshContext(): Promise<void> {
    if (!projectId.value) return
    try {
      const response = await getAssistantContext(projectId.value, episodeNo.value)
      contextData.value = response
      agentLabels.value = response.agent_labels
    } catch {
      // 上下文刷新失败不阻断会话，保留旧值
    }
  }

  /**
   * 发送消息（乐观插入用户气泡，失败回滚并恢复草稿）。
   * @param content 消息内容
   * @returns 是否发送成功
   */
  async function send(content: string): Promise<boolean> {
    const text = content.trim()
    if (!text || sending.value || !projectId.value) return false
    sending.value = true
    error.value = ''
    degraded.value = false
    const optimisticId = optimisticSeq--
    const controllerForSend = new AbortController()
    controller = controllerForSend
    const optimistic: AssistantMessage = {
      id: optimisticId,
      role: 'user',
      content: text,
      agent_key: 'general',
      agent_label: '我',
      intent: null,
      actions: [],
      self_check: null,
      status: 'ok',
      created_at: new Date().toISOString(),
    }
    messages.value = [...messages.value, optimistic]
    draft.value = ''
    try {
      const result = await sendAssistantMessage(
        projectId.value,
        {
          content: text,
          episode_no: episodeNo.value,
          client_message_id: createClientMessageId(),
        },
        controllerForSend.signal,
      )
      // 用服务端真实消息替换占位（保留其他消息）
      const withoutOptimistic = messages.value.filter((item) => item.id !== optimisticId)
      messages.value = [...withoutOptimistic, result.message]
      conversation.value = result.conversation
      episodeNo.value = result.conversation.episode_no
      actions.value = mergeActions(actions.value, result.actions)
      degraded.value = result.degraded
      scenarioHint.value = result.message.agent_key
      void refreshContext()
      return true
    } catch (err) {
      messages.value = messages.value.filter((item) => item.id !== optimisticId)
      draft.value = text
      error.value = err instanceof Error ? err.message : '消息发送失败，请重试'
      return false
    } finally {
      sending.value = false
    }
  }

  /**
   * 合并动作列表（按 ID 去重，后者覆盖前者）。
   * @param current 现有动作
   * @param incoming 新动作
   * @returns 合并后的动作列表
   */
  function mergeActions(current: AssistantAction[], incoming: AssistantAction[]): AssistantAction[] {
    const map = new Map<number, AssistantAction>()
    for (const item of current) map.set(item.id, item)
    for (const item of incoming) map.set(item.id, item)
    return Array.from(map.values()).sort((a, b) => a.id - b.id)
  }

  /**
   * 把动作状态同步到消息内的动作快照。
   * @param action 最新动作
   * @returns 无
   */
  function patchActionInMessages(action: AssistantAction): void {
    let matched = false
    for (const message of messages.value) {
      const index = message.actions.findIndex((item) => item.id === action.id)
      if (index >= 0) {
        message.actions[index] = { ...action }
        matched = true
      }
    }
    if (!matched && conversation.value) {
      // 动作快照缺失时补到最新一条助手消息，避免界面看不到状态
      const lastAssistant = [...messages.value].reverse().find((item) => item.role === 'assistant')
      if (lastAssistant) {
        lastAssistant.actions = [...lastAssistant.actions, action]
      }
    }
  }

  /**
   * 确认执行动作（HITL）；成功后刷新上下文并把系统回执追加到消息流。
   * @param actionId 动作 ID
   * @returns 是否执行成功
   */
  async function execute(actionId: number): Promise<boolean> {
    if (!projectId.value || executing.value.includes(actionId)) return false
    executing.value = [...executing.value, actionId]
    error.value = ''
    try {
      const result = await executeAssistantAction(projectId.value, actionId)
      actions.value = mergeActions(actions.value, [result.action])
      patchActionInMessages(result.action)
      if (result.message) {
        messages.value = [...messages.value, result.message]
      }
      void refreshContext()
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : '动作执行失败，请稍后重试'
      // 后端可能已落失败态：重新拉取会话以同步卡片状态
      try {
        const detail = await getAssistantConversation(projectId.value, episodeNo.value)
        messages.value = detail.messages
        actions.value = detail.actions
      } catch {
        // 同步失败时保留本地状态
      }
      return false
    } finally {
      executing.value = executing.value.filter((id) => id !== actionId)
    }
  }

  /**
   * 本地忽略动作卡片（不改变服务端状态）。
   * @param actionId 动作 ID
   * @returns 无
   */
  function dismissAction(actionId: number): void {
    if (!dismissed.value.includes(actionId)) {
      dismissed.value = [...dismissed.value, actionId]
    }
    // 本地隐藏卡片：从消息快照中移除，刷新后由服务端状态决定是否再次出现
    for (const message of messages.value) {
      message.actions = message.actions.filter((item) => item.id !== actionId)
    }
  }

  /**
   * 清空会话（软删除后重建空会话）。
   * @returns 无
   */
  async function clear(): Promise<void> {
    if (!projectId.value) return
    controller?.abort()
    try {
      await clearAssistantConversation(projectId.value)
    } catch (err) {
      error.value = err instanceof Error ? err.message : '清空失败，请稍后重试'
      return
    }
    conversation.value = null
    messages.value = []
    actions.value = []
    dismissed.value = []
    degraded.value = false
    await refreshAll()
  }

  /**
   * 重试加载（错误态下的重试入口）。
   * @returns 无
   */
  async function retry(): Promise<void> {
    await refreshAll()
  }

  /**
   * 重置全部状态并中止进行中的请求。
   * @returns 无
   */
  function reset(): void {
    controller?.abort()
    controller = null
    projectId.value = ''
    episodeNo.value = 1
    conversation.value = null
    messages.value = []
    actions.value = []
    scenarios.value = []
    agentLabels.value = {}
    contextData.value = null
    loading.value = false
    sending.value = false
    executing.value = []
    degraded.value = false
    error.value = ''
    draft.value = ''
    dismissed.value = []
    scenarioHint.value = null
  }

  return {
    projectId,
    episodeNo,
    conversation,
    messages,
    actions,
    scenarios,
    agentLabels,
    contextData,
    loading,
    sending,
    executing,
    degraded,
    error,
    draft,
    dismissed,
    scenarioHint,
    initialized,
    pendingActions,
    open,
    setEpisode,
    refreshAll,
    refreshContext,
    send,
    execute,
    dismissAction,
    clear,
    retry,
    reset,
  }
})

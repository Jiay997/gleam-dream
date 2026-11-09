/**
 * 文件级注释：AI 创作助手 API 客户端（对齐 backend/app/api/assistant.py）。
 *
 * 端点：
 * - GET    /projects/{id}/assistant/context          上下文徽标与场景清单；
 * - GET    /projects/{id}/assistant/conversation     会话详情与最近消息；
 * - POST   /projects/{id}/assistant/messages         发送消息并生成回复；
 * - POST   /projects/{id}/assistant/actions/{aid}/execute 确认执行动作；
 * - DELETE /projects/{id}/assistant/conversation     清空会话。
 *
 * 约定：http 拦截器已解包统一信封并统一 toast 错误，本模块只负责类型与路径；
 * 对话请求单独放宽超时（全局 15s 不足以覆盖 LLM 生成）。
 */
import { http } from './http'

/** 助手场景（快捷 chips） */
export interface AssistantScenario {
  /** 场景键（与 Agent 标识一致） */
  key: string
  /** 中文标题 */
  label: string
  /** 一句话说明 */
  description: string
  /** 预填提示词 */
  prompt: string
}

/** 助手动作（服务端参数不下发，仅展示预览） */
export interface AssistantAction {
  /** 动作 ID */
  id: number
  /** 动作类型 */
  action_type: string
  /** 卡片标题 */
  title: string
  /** 一句话说明 */
  summary: string
  /** 风险提示 */
  risk: string | null
  /** 参数可读摘要 */
  detail: string
  /** 状态 pending/executing/accepted/succeeded/failed/cancelled/expired */
  status: string
  /** 失败原因 */
  error_message: string | null
  /** 执行结果引用 */
  result_ref: Record<string, unknown> | null
  /** 确认过期时间 */
  expires_at: string | null
  /** 创建时间 */
  created_at: string | null
}

/** 助手消息 */
export interface AssistantMessage {
  /** 消息 ID */
  id: number
  /** 角色 user/assistant/system */
  role: string
  /** 正文 */
  content: string
  /** Agent 标识 */
  agent_key: string
  /** Agent 中文名 */
  agent_label: string
  /** 意图关键词 */
  intent: string | null
  /** 动作快照 */
  actions: AssistantAction[]
  /** 自检结果 */
  self_check: Record<string, unknown> | null
  /** 状态 ok/degraded/failed */
  status: string
  /** 创建时间 */
  created_at: string | null
}

/** 会话元信息 */
export interface AssistantConversation {
  /** 会话 ID */
  id: number
  /** 项目 ID */
  project_id: number
  /** 聚焦集号 */
  episode_no: number
  /** 会话标题 */
  title: string
  /** 上下文版本 */
  context_version: string | null
  /** 最近消息时间 */
  last_message_at: string | null
}

/** 项目上下文切片 */
export interface AssistantContext {
  /** 项目摘要 */
  project: {
    id: number
    title: string
    status: string
    synopsis: string | null
    genre: string | null
    style_preset_key: string
    aspect_ratio: string
    target_episode_count: number
    pipeline_mode: string
  }
  /** 聚焦集号 */
  episode_no: number
  /** 当前剧本摘要 */
  script: {
    id: number
    version_no: number
    status: string
    title: string | null
    logline: string | null
    total_episodes: number
    total_words: number
    version_label: string | null
  } | null
  /** 分集列表 */
  episodes: Array<{
    episode_no: number
    title: string | null
    status: string
    scene_count: number
    line_count: number
  }>
  /** 当前集场次摘要 */
  current_scenes: Array<{
    scene_no: number
    title: string | null
    location: string | null
    time_of_day: string | null
    summary: string
  }>
  /** 主要说话人 */
  characters: string[]
  /** 分镜板统计 */
  storyboards: Array<{
    episode_no: number
    status: string
    shot_count: number
    estimated_duration_ms: number
  }>
  /** Story Bible 完整度 */
  story_bible: {
    completeness: number
    threshold: number
    character_count: number
    scene_count: number
    prop_count: number
    warning: string | null
  } | null
  /** 配音报告 */
  voice: {
    line_count: number
    voiced_count: number
    total_ms: number
    missing_voice_characters: string[]
    align_degraded: boolean
  } | null
  /** 最近任务 */
  render_tasks: Array<{
    id: number
    task_type: string
    status: string
    retry_count: number
    created_at: string | null
  }>
  /** 待审批项 */
  approvals: Array<{
    id: number
    approval_type: string
    requested_at: string | null
  }>
  /** 预算状态 */
  budget: {
    budget_credits: number
    spent_credits: number
    remaining: number | null
    exhausted: boolean
  }
  /** 上下文版本 */
  context_version: string
}

/** 上下文接口响应 */
export interface AssistantContextResponse {
  /** 上下文切片 */
  context: AssistantContext
  /** 场景清单 */
  scenarios: AssistantScenario[]
  /** Agent 中文名映射 */
  agent_labels: Record<string, string>
}

/** 会话详情响应 */
export interface AssistantConversationDetail {
  /** 会话元信息 */
  conversation: AssistantConversation
  /** 最近 50 条消息（时间正序） */
  messages: AssistantMessage[]
  /** 会话内全部动作 */
  actions: AssistantAction[]
  /** 场景清单 */
  scenarios: AssistantScenario[]
  /** 上下文版本 */
  context_version: string | null
}

/** 发送消息响应 */
export interface AssistantSendResult {
  /** 会话元信息 */
  conversation: AssistantConversation
  /** 助手回复 */
  message: AssistantMessage
  /** 本次回复的动作提议 */
  actions: AssistantAction[]
  /** 是否降级（模型不可用） */
  degraded: boolean
  /** 上下文版本 */
  context_version: string | null
}

/** 动作执行响应 */
export interface AssistantExecuteResult {
  /** 最新动作状态 */
  action: AssistantAction
  /** 系统回执消息 */
  message: AssistantMessage | null
}

/**
 * 查询助手上下文与场景清单。
 * @param projectId 项目 ID
 * @param episodeNo 聚焦集号
 * @returns 上下文响应
 */
export function getAssistantContext(
  projectId: string | number,
  episodeNo = 1,
): Promise<AssistantContextResponse> {
  return http.get<AssistantContextResponse>(`/projects/${projectId}/assistant/context`, {
    params: { episode_no: episodeNo },
  })
}

/**
 * 读取当前用户在该项目的助手会话（无则自动创建）。
 * @param projectId 项目 ID
 * @param episodeNo 聚焦集号
 * @returns 会话详情
 */
export function getAssistantConversation(
  projectId: string | number,
  episodeNo = 1,
): Promise<AssistantConversationDetail> {
  return http.get<AssistantConversationDetail>(
    `/projects/${projectId}/assistant/conversation`,
    { params: { episode_no: episodeNo } },
  )
}

/**
 * 发送助手消息并生成回复。
 * @param projectId 项目 ID
 * @param payload 消息内容 / 集号 / 客户端消息 ID
 * @param signal 取消信号（切换项目或组件卸载时中止）
 * @returns 助手回复与动作
 * @throws ApiError 10004 限流 / 20003 无权限 / 网络异常
 */
export function sendAssistantMessage(
  projectId: string | number,
  payload: { content: string; episode_no: number; client_message_id: string },
  signal?: AbortSignal,
): Promise<AssistantSendResult> {
  return http.post<AssistantSendResult>(`/projects/${projectId}/assistant/messages`, payload, {
    timeout: 60000,
    signal,
  })
}

/**
 * 确认执行助手动作（客户端只提交 action_id）。
 * @param projectId 项目 ID
 * @param actionId 动作 ID
 * @returns 最新动作状态与系统回执
 * @throws ApiError 10001 动作过期 / 30002 预算超限 / 下游业务错误
 */
export function executeAssistantAction(
  projectId: string | number,
  actionId: number,
): Promise<AssistantExecuteResult> {
  return http.post<AssistantExecuteResult>(
    `/projects/${projectId}/assistant/actions/${actionId}/execute`,
    { confirm: true },
    { timeout: 60000 },
  )
}

/**
 * 清空当前用户的助手会话。
 * @param projectId 项目 ID
 * @returns 删除结果
 */
export function clearAssistantConversation(
  projectId: string | number,
): Promise<{ deleted: boolean }> {
  return http.delete<{ deleted: boolean }>(`/projects/${projectId}/assistant/conversation`)
}

/**
 * 文件级注释：模型配置域 API（四类模型的查询 / 新增 / 编辑 / 删除 / 测试连接 / 设为默认）。
 * 后端端点位于 /api/v1/model-configs，密钥仅以掩码返回。
 */
import type { AxiosRequestConfig } from 'axios'

import { http } from './http'

/** 管理端口令本地存储键（模型配置写操作需 X-Admin-Token） */
export const ADMIN_TOKEN_KEY = 'gd_admin_token'

/**
 * 构造管理端请求配置（自动附带 X-Admin-Token）。
 * @returns 含管理端请求头的 axios 配置；未设置口令时返回 undefined（后端将返回 20003）
 */
function adminConfig(): AxiosRequestConfig | undefined {
  let token = ''
  try {
    token = localStorage.getItem(ADMIN_TOKEN_KEY) ?? ''
  } catch {
    token = ''
  }
  return token ? { headers: { 'X-Admin-Token': token } } : undefined
}

/** 模型类型：文本 / 图片 / 语音 / 视频 */
export type ModelCapability = 'llm' | 'image' | 'tts' | 'video'

/** 调用协议 */
export type ModelProtocol =
  | 'openai_chat'
  | 'dashscope_image'
  | 'dashscope_video'
  | 'dashscope_tts'
  | 'agnes_image'
  | 'agnes_video'
  | 'qwen_tts'
  | 'stub'

/** 计费单位 */
export type ModelUnit = 'per_image' | 'per_second' | 'per_1k_token' | 'per_call'

/** 单条模型配置（密钥已掩码） */
export interface ModelConfigItem {
  /** 配置 ID */
  id: number
  /** 模型键（唯一） */
  model_key: string
  /** 展示名称 */
  model_name: string
  /** 提供商标识（记账用） */
  provider: string
  /** 模型类型 */
  capability: ModelCapability
  /** 调用协议 */
  protocol: ModelProtocol | ''
  /** 接口地址 */
  base_url: string
  /** 三方 API 模型名 */
  api_model: string
  /** 是否已配置密钥 */
  api_key_configured: boolean
  /** 掩码密钥 */
  api_key_masked: string
  /** 扩展参数（如音色） */
  extra_config: Record<string, unknown>
  /** 计费单位 */
  unit: ModelUnit
  /** 单价（积分） */
  unit_price_credits: number
  /** 质量分 */
  quality_score: number | null
  /** 是否启用 */
  is_available: boolean
  /** 是否为该类型默认模型（路由 priority 1） */
  is_default: boolean
  /** 绑定的生成任务类型 */
  task_types: string[]
  /** 更新时间 */
  updated_at: string | null
}

/** 类型元信息（协议与计费单位候选） */
export interface ModelCapabilityMeta {
  /** 模型类型 */
  capability: ModelCapability
  /** 可用协议 */
  protocols: ModelProtocol[]
  /** 可用计费单位 */
  units: ModelUnit[]
  /** 绑定任务类型 */
  task_types: string[]
}

/** 列表响应 */
export interface ModelConfigList {
  /** 配置列表 */
  items: ModelConfigItem[]
  /** 类型元信息 */
  capability_meta: ModelCapabilityMeta[]
}

/** 测试连接结果 */
export interface ModelConfigTestResult {
  /** 是否连接成功 */
  ok: boolean
  /** 结果说明 */
  message: string
  /** 耗时（毫秒） */
  latency_ms: number
  /** 附加详情（不含密钥） */
  detail: Record<string, unknown>
}

/** 新增配置请求体 */
export interface ModelConfigCreatePayload {
  capability: ModelCapability
  model_key: string
  model_name: string
  protocol: ModelProtocol
  base_url?: string
  api_key?: string
  api_model?: string
  extra_config?: Record<string, unknown>
  unit: ModelUnit
  unit_price_credits: number
  quality_score?: number
  is_available: boolean
  is_default: boolean
}

/** 编辑配置请求体（api_key 缺省表示不修改，空串表示清除） */
export interface ModelConfigUpdatePayload {
  model_name?: string
  protocol?: ModelProtocol
  base_url?: string
  api_key?: string
  api_model?: string
  extra_config?: Record<string, unknown>
  unit?: ModelUnit
  unit_price_credits?: number
  quality_score?: number
  is_available?: boolean
}

/**
 * 查询模型配置列表。
 * @param capability 可选类型过滤
 * @returns 配置列表与类型元信息
 */
export function listModelConfigs(capability?: ModelCapability): Promise<ModelConfigList> {
  return http.get<ModelConfigList>('/model-configs', {
    params: capability ? { capability } : undefined,
    ...adminConfig(),
  })
}

/**
 * 新增模型配置。
 * @param payload 新增请求体
 * @returns 新建配置
 * @throws ApiError 10001 参数非法 / 90001 模型键重复
 */
export function createModelConfig(payload: ModelConfigCreatePayload): Promise<ModelConfigItem> {
  return http.post<ModelConfigItem>('/model-configs', payload, adminConfig())
}

/**
 * 编辑模型配置。
 * @param configId 配置 ID
 * @param payload 编辑请求体
 * @returns 更新后的配置
 * @throws ApiError 10002 配置不存在 / 10001 参数非法
 */
export function updateModelConfig(
  configId: number,
  payload: ModelConfigUpdatePayload,
): Promise<ModelConfigItem> {
  return http.patch<ModelConfigItem>(`/model-configs/${configId}`, payload, adminConfig())
}

/**
 * 删除模型配置（连带清理路由与计费规则）。
 * @param configId 配置 ID
 * @returns 无
 * @throws ApiError 90001 该模型为某任务类型最后可用模型
 */
export function deleteModelConfig(configId: number): Promise<void> {
  return http.delete<void>(`/model-configs/${configId}`, adminConfig())
}

/**
 * 测试配置连通性（真实最小调用；视频仅提交任务）。
 * @param configId 配置 ID
 * @returns 测试结果
 */
export function testModelConfig(configId: number): Promise<ModelConfigTestResult> {
  return http.post<ModelConfigTestResult>(`/model-configs/${configId}/test`, undefined, adminConfig())
}

/**
 * 将模型设为该类型默认模型（生成流程优先调用）。
 * @param configId 配置 ID
 * @returns 更新后的配置
 */
export function activateModelConfig(configId: number): Promise<ModelConfigItem> {
  return http.post<ModelConfigItem>(`/model-configs/${configId}/activate`, undefined, adminConfig())
}

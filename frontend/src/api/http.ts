/**
 * 文件级注释：HTTP 请求封装（axios 单例）。
 * - baseURL '/api/v1'，超时 15s；
 * - 请求拦截器：注入 Authorization Bearer（读取 localStorage，避免与 store 循环依赖），
 *   写请求自动注入 Idempotency-Key（调用方可预置同名头覆盖）；
 * - 响应拦截器：统一解包 { code, message, data, request_id } 信封——code===0 返回 data；
 *   code===20001/20002（或 HTTP 401）先尝试 refreshToken 静默续期并重放原请求一次
 *   （D12 修复：single-flight 防并发重放，认证端点本身不重放），失败才清理凭证跳登录；
 *   其余抛出 ApiError 并按 ERROR_MESSAGES 映射为中文 toast；HTTP 层错误（网络 / 超时）
 *   同样转为统一提示。
 * - lastRequestId：响应式保存最近一次 request_id，供底部状态栏展示链路追踪。
 */
import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { ref } from 'vue'

import { useToast } from '@/composables/useToast'
import { STORAGE_KEYS } from '@/constants/storage'
import type { ApiEnvelope } from './types'

/** 业务错误：携带后端业务码 / 数据 / 追踪 ID */
export class ApiError extends Error {
  /** 业务码（无信封时为 HTTP 状态码，网络层错误为 -1） */
  readonly code: number
  /** 后端附加数据 */
  readonly data?: unknown
  /** 链路追踪 ID */
  readonly requestId?: string

  /**
   * @param code 业务码
   * @param message 已映射的中文提示
   * @param data 后端附加数据
   * @param requestId 链路追踪 ID
   */
  constructor(code: number, message: string, data?: unknown, requestId?: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.data = data
    this.requestId = requestId
  }
}

/** 最近一次响应的 request_id（响应式，供状态栏展示） */
export const lastRequestId = ref('')

/** 错误码 → 中文提示映射表（集中维护，避免散落各处） */
export const ERROR_MESSAGES: Record<number, string> = {
  10004: '请求过于频繁，请稍后再试',
  20001: '登录状态已失效，请重新登录',
  20002: '登录凭证已过期，请重新登录',
  20003: '暂无权限执行该操作',
  20004: '账号已被封禁',
  30001: '积分不足，请前往充值',
  30002: '预估超出剩余预算，请调整后重试',
  30003: '订单已支付，请勿重复支付',
  30004: '支付回调签名校验失败',
  30005: '冻结记录不存在或已失效',
  40001: '当前状态不允许执行该操作',
  40004: '项目已归档，请先恢复后再操作',
  40005: '集数超出上限',
  50001: '资产尚未锁定',
  50003: '尚未完成真人授权',
  60001: '分镜尚未锁定',
  60005: '缺少资产锚点，无法生成',
  60006: '并发任务超出上限，请稍后重试',
  70003: '当前套餐权益不足，请升级套餐',
  80001: '模型服务不可用，请稍后重试',
}

/**
 * 判断响应体是否为统一信封结构。
 * @param value 任意响应体
 * @returns 是否为 { code, message, ... } 信封
 */
function isEnvelope(value: unknown): value is ApiEnvelope<unknown> {
  return typeof value === 'object' && value !== null && 'code' in value && 'message' in value
}

/**
 * 生成幂等键：优先使用 crypto.randomUUID，环境不支持时回退随机串。
 * @returns UUID 字符串
 */
function createIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `idem-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

/**
 * 清理本地登录凭证（token / refreshToken / user）。
 * @returns 无
 */
function clearAuthStorage(): void {
  localStorage.removeItem(STORAGE_KEYS.token)
  localStorage.removeItem(STORAGE_KEYS.refreshToken)
  localStorage.removeItem(STORAGE_KEYS.user)
}

/**
 * 凭证失效处理：清理本地存储并跳转登录页（动态 import 路由避免循环依赖）。
 * @returns 无
 */
async function redirectToLogin(): Promise<void> {
  clearAuthStorage()
  const { default: router } = await import('@/router')
  if (router.currentRoute.value.path !== '/login') {
    await router.push({ path: '/login', query: { redirect: router.currentRoute.value.fullPath } })
  }
}

/**
 * 业务码是否为"凭证失效需续期或跳登录"。
 * @param code 业务码
 * @returns 是否需要进入续期/登出处理
 */
function isAuthExpired(code: number): boolean {
  return code === 20001 || code === 20002
}

/* ---------- 会话静默续期（D12 修复） ---------- */

/** 请求配置扩展：标记是否已因凭证失效重放过（防续期死循环） */
type RetryFlaggedConfig = InternalAxiosRequestConfig & { __authRetried?: boolean }

/** 进行中的续期 Promise（single-flight：并发 401 共享同一次 refresh 调用） */
let refreshing: Promise<boolean> | null = null

/**
 * 判断是否为认证端点本身（登录/注册/验证码/刷新）。
 * 这类请求的 401 属业务语义（如密码错误），不应触发续期重放。
 * @param url 请求路径
 * @returns 是否认证端点
 */
function isAuthEndpoint(url?: string): boolean {
  return !!url && url.includes('/auth/')
}

/**
 * 用 refreshToken 静默续期（裸 axios 直连，绕过本实例拦截器避免递归）。
 * 成功时仅更新 access_token（后端 refresh 不轮换 refreshToken）。
 * 修复（E2E 报告问题 2）：续期成功后广播 `gd:token-refreshed` 事件，
 * 使 auth store 与 SSE 订阅（EventSource 无法携带 Authorization 头）
 * 能感知新令牌并重建连接，避免项目事件流持续 401。
 * @returns 是否续期成功
 */
async function refreshSession(): Promise<boolean> {
  const refreshToken = localStorage.getItem(STORAGE_KEYS.refreshToken)
  if (!refreshToken) return false
  try {
    const resp = await axios.post('/api/v1/auth/refresh', { refresh_token: refreshToken }, { timeout: 15000 })
    const body = resp.data as ApiEnvelope<{ access_token?: string; refresh_token?: string }> | undefined
    const accessToken = body?.data?.access_token
    if (body?.code !== 0 || !accessToken) return false
    localStorage.setItem(STORAGE_KEYS.token, accessToken)
    // 后端刷新令牌轮换：同步写入新的 refresh_token，避免下次续期失败
    if (body.data?.refresh_token) {
      localStorage.setItem(STORAGE_KEYS.refreshToken, body.data.refresh_token)
    }
    // 通知 auth store 与 SSE 连接层（令牌已更新）
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent('gd:token-refreshed', {
          detail: { accessToken, refreshToken: body.data?.refresh_token ?? refreshToken },
        }),
      )
    }
    return true
  } catch {
    return false
  }
}

/**
 * 单飞续期：首个 401 发起 refresh，期间其余 401 共享同一 Promise 结果。
 * @returns 是否续期成功
 */
function refreshOnce(): Promise<boolean> {
  if (!refreshing) {
    refreshing = refreshSession().finally(() => {
      refreshing = null
    })
  }
  return refreshing
}

/**
 * 凭证失效统一处理（D12）：先静默续期并重放原请求一次；续期失败才清理
 * 凭证并跳转登录（返回 null，由调用方继续抛出业务错误）。
 * @param config 原请求配置
 * @returns 重放响应（已走完整拦截器链，信封已解包）；null = 续期失败已跳登录
 */
async function replayAfterRefresh(config: InternalAxiosRequestConfig): Promise<AxiosResponse | null> {
  const flagged = config as RetryFlaggedConfig
  if (flagged.__authRetried || isAuthEndpoint(config.url)) return null
  const ok = await refreshOnce()
  if (!ok) {
    void redirectToLogin().catch(() => undefined)
    return null
  }
  const replayConfig = { ...config } as RetryFlaggedConfig
  replayConfig.__authRetried = true
  replayConfig.headers.set('Authorization', `Bearer ${localStorage.getItem(STORAGE_KEYS.token) ?? ''}`)
  return instance.request(replayConfig)
}

/**
 * 按错误码解析最终展示文案：优先取集中映射表，其次后端 message，最后通用兜底。
 *
 * 20003（无权限）是例外：该码被多个业务复用（管理端口令缺失、仅项目 Owner 可操作
 * 等），后端 message 才是可执行的具体原因；若被通用文案覆盖，用户只会看到
 * "暂无权限执行该操作"而无从下手（模型配置页 403 空态即由此放大）。
 *
 * @param code 业务码
 * @param fallback 后端 message
 * @returns 中文提示文案
 */
function resolveMessage(code: number, fallback?: string): string {
  const detail = fallback?.trim()
  // 20003 / 40001 是例外：这些码被多个业务复用（无权限主体不同、状态机
  // from/allowed 不同），后端 message 才是可执行的具体原因；若被通用文案
  // 覆盖，用户只会看到笼统提示而无从下手（修复测试报告 #30 的错误码吞噬）。
  if ((code === 20003 || code === 40001) && detail) return detail
  return ERROR_MESSAGES[code] ?? (detail || '操作失败，请稍后重试')
}

/**
 * 弹出错误 toast。
 * @param message 文案
 * @returns 无
 */
function toastError(message: string): void {
  useToast().error(message)
}

/** axios 实例 */
const instance: AxiosInstance = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
})

/* ---------- 请求拦截器：鉴权头 + 幂等键 ---------- */
instance.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem(STORAGE_KEYS.token)
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  const method = (config.method ?? 'get').toLowerCase()
  const isWrite = method === 'post' || method === 'put' || method === 'patch' || method === 'delete'
  // 写请求自动注入幂等键；调用方可通过 config.headers 预置同名头实现幂等覆盖
  if (isWrite && !config.headers.has('Idempotency-Key')) {
    config.headers.set('Idempotency-Key', createIdempotencyKey())
  }
  return config
})

/* ---------- 响应拦截器：解包信封 / 统一错误 ---------- */
instance.interceptors.response.use(
  async (response: AxiosResponse) => {
    // 二进制响应不做信封解包
    if (response.config.responseType === 'blob' || response.config.responseType === 'arraybuffer') {
      return response
    }
    const body: unknown = response.data
    if (isEnvelope(body)) {
      lastRequestId.value = body.request_id ?? lastRequestId.value
      if (body.code === 0) {
        // 约定：拦截器完成解包，业务函数直接拿到 data（类型经 request<T> 断言保证）
        return body.data as unknown as AxiosResponse
      }
      if (isAuthExpired(body.code)) {
        // D12：先尝试静默续期并重放一次，失败才跳登录
        const replayed = await replayAfterRefresh(response.config)
        if (replayed !== null) return replayed
        if (!isAuthEndpoint(response.config.url)) {
          void redirectToLogin().catch(() => undefined)
        }
      }
      const apiError = new ApiError(body.code, resolveMessage(body.code, body.message), body.data, body.request_id)
      toastError(apiError.message)
      throw apiError
    }
    return response
  },
  async (error: unknown) => {
    // HTTP 层错误（非 2xx / 网络异常 / 超时）
    if (axios.isAxiosError(error) && error.response) {
      const rawBody: unknown = error.response.data
      const enveloped = isEnvelope(rawBody) ? rawBody : null
      let code = error.response.status
      let message = `请求失败（HTTP ${error.response.status}）`
      const authExpired = enveloped ? isAuthExpired(enveloped.code) : error.response.status === 401
      if (authExpired && error.config) {
        // D12：先尝试静默续期并重放一次；认证端点本身/已重放过 → 直接跳登录
        const replayed = await replayAfterRefresh(error.config)
        if (replayed !== null) return replayed
        if (!enveloped || isAuthEndpoint(error.config.url)) {
          void redirectToLogin().catch(() => undefined)
        }
      }
      if (enveloped) {
        code = enveloped.code
        message = resolveMessage(enveloped.code, enveloped.message)
      } else if (error.response.status === 401) {
        message = ERROR_MESSAGES[20001]
      }
      toastError(message)
      throw new ApiError(code, message, enveloped?.data, enveloped?.request_id)
    }
    if (axios.isAxiosError(error) && error.code === 'ECONNABORTED') {
      toastError('请求超时，请检查网络后重试')
      throw new ApiError(-1, '请求超时，请检查网络后重试')
    }
    toastError('网络连接异常，请检查网络后重试')
    throw new ApiError(-1, '网络连接异常，请检查网络后重试')
  },
)

/**
 * 发起请求并返回解包后的业务数据。
 * 响应拦截器已完成信封解包，axios 原始返回类型（响应对象）在此收窄为业务数据 T。
 * @param config axios 请求配置
 * @returns 业务数据（信封已由拦截器解包）
 * @throws ApiError 业务错误 / HTTP 错误
 */
async function request<T>(config: AxiosRequestConfig): Promise<T> {
  return (await instance.request<T, T>(config)) as T
}

/**
 * 供 SSE 连接层使用的静默续期入口（EventSource 无法携带 Authorization、
 * 也无法读取 401 状态码，需要主动续期后重连）。
 * @returns 是否续期成功（并发调用共享同一次 refresh）
 */
export function refreshAccessToken(): Promise<boolean> {
  return refreshOnce()
}

/** 类型化请求方法集合：泛型 T 即解包后的业务数据类型 */
export const http = {
  /**
   * GET 请求
   * @param url 请求路径（不含 /api/v1 前缀）
   * @param config 追加配置（params 等）
   * @returns 业务数据
   */
  get: <T>(url: string, config?: AxiosRequestConfig): Promise<T> => request<T>({ ...config, url, method: 'GET' }),
  /**
   * POST 请求
   * @param url 请求路径
   * @param data 请求体
   * @param config 追加配置
   * @returns 业务数据
   */
  post: <T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> =>
    request<T>({ ...config, url, method: 'POST', data }),
  /**
   * PUT 请求
   * @param url 请求路径
   * @param data 请求体
   * @param config 追加配置
   * @returns 业务数据
   */
  put: <T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> =>
    request<T>({ ...config, url, method: 'PUT', data }),
  /**
   * PATCH 请求
   * @param url 请求路径
   * @param data 请求体
   * @param config 追加配置
   * @returns 业务数据
   */
  patch: <T>(url: string, data?: unknown, config?: AxiosRequestConfig): Promise<T> =>
    request<T>({ ...config, url, method: 'PATCH', data }),
  /**
   * DELETE 请求
   * @param url 请求路径
   * @param config 追加配置
   * @returns 业务数据
   */
  delete: <T>(url: string, config?: AxiosRequestConfig): Promise<T> => request<T>({ ...config, url, method: 'DELETE' }),
  /**
   * 下载二进制文件（不做信封解包）
   * @param url 请求路径
   * @param config 追加配置
   * @returns Blob 数据
   */
  download: async (url: string, config?: AxiosRequestConfig): Promise<Blob> => {
    const response = await instance.request<Blob, AxiosResponse<Blob>>({
      ...config,
      url,
      method: 'GET',
      responseType: 'blob',
    })
    return response.data
  },
}

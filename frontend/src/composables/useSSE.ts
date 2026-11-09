/**
 * 文件级注释：Server-Sent Events（SSE）组合式函数。
 * 封装 EventSource：指数退避重连（1/2/4/8/16s 共 5 次），超过后降级为 30s 轮询；
 * 重连时浏览器原生携带 Last-Event-ID 续传；对外暴露连接状态并在组件卸载时自动清理。
 */
import { getCurrentInstance, isRef, onMounted, onUnmounted, ref, unref, watch, type Ref } from 'vue'

import { STORAGE_KEYS } from '@/constants/storage'
import { refreshAccessToken } from '@/api/http'

/** SSE 连接状态 */
export type SseStatus = 'connected' | 'connecting' | 'polling' | 'offline'

/**
 * SSE 地址来源：字符串、响应式 Ref，或返回地址的函数。
 * 函数形式在每次重连时求值，可动态读取最新令牌（E2E 报告问题 2）。
 */
export type SseUrlSource = string | Ref<string> | (() => string)

/** SSE 处理回调集合 */
export interface UseSSEHandlers {
  /** 收到默认（未命名）事件 */
  onMessage?: (event: MessageEvent<string>) => void
  /** 连接发生错误 */
  onError?: (error: unknown) => void
  /** 轮询回调（SSE 降级后周期调用） */
  onPoll?: () => void
  /** 需要监听的命名事件类型列表（如 task.updated） */
  eventTypes?: string[]
  /** 轮询间隔毫秒，默认 30000 */
  pollIntervalMs?: number
}

/** 指数退避序列：1/2/4/8/16 秒 */
const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000]
/** 最大重连次数 */
const MAX_RETRY = BACKOFF_MS.length
/** 默认轮询间隔 */
const DEFAULT_POLL_MS = 30000

/**
 * 建立 SSE 订阅。
 * @param url 事件流地址（支持 Ref 或函数；函数在每次重连时求值）
 * @param handlers 事件回调集合
 * @returns 连接状态 ref、手动重连与关闭方法
 */
export function useSSE(url: SseUrlSource, handlers: UseSSEHandlers = {}) {
  /** 当前连接状态 */
  const status = ref<SseStatus>('offline')

  let source: EventSource | null = null
  let retryCount = 0
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  let pollTimer: ReturnType<typeof setInterval> | null = null
  /** 轮询周期计数（用于周期性探测恢复实时连接） */
  let pollCycles = 0
  /** 每隔多少个轮询周期探测一次 SSE 恢复 */
  const POLL_PROBE_EVERY = 5
  /** 上次凭证续期时间（10 秒内只续期一次，避免重连风暴触发大量 refresh） */
  let lastAuthRetryAt = 0
  let disposed = false

  /**
   * 为 API 路径自动附加 access_token 查询参数。
   * EventSource 无法发送 Authorization 头，后端 SSE 端点支持
   * `?access_token=` 鉴权；此处在每次连接时读取最新令牌。
   * @param target 原始事件流地址
   * @returns 已带令牌的地址（非 /api 路径原样返回）
   */
  function withAccessToken(target: string): string {
    if (!target || !target.startsWith('/api/')) return target
    if (target.includes('access_token=')) return target
    const token = localStorage.getItem(STORAGE_KEYS.token)
    if (!token) return target
    const sep = target.includes('?') ? '&' : '?'
    return `${target}${sep}access_token=${encodeURIComponent(token)}`
  }

  /**
   * 求值当前连接地址（字符串 / Ref / 函数）。
   * @returns 最终事件流地址（已附加令牌）
   */
  function resolveTarget(): string {
    const raw = typeof url === 'function' ? url() : unref(url)
    return withAccessToken(raw)
  }

  /**
   * 清理所有重连 / 轮询定时器。
   * @returns 无
   */
  function clearTimers(): void {
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  /**
   * 关闭并置空 EventSource 实例。
   * @returns 无
   */
  function closeSource(): void {
    if (source) {
      source.close()
      source = null
    }
  }

  /**
   * 启动轮询降级：立即执行一次 onPoll，之后按固定间隔轮询。
   * 每 POLL_PROBE_EVERY 个周期主动探测一次 SSE 恢复（成功 onopen 会
   * 停止轮询并回到 connected），避免连接恢复后仍长期停留在轮询模式
   * （P2-3 / P1-3 修复：「连接中→轮询→实时」来回跳的根因是 5 次重试
   * 耗尽后永久降级；此处给出自愈路径）。
   * @returns 无
   */
  function startPolling(): void {
    if (pollTimer || disposed) return
    status.value = 'polling'
    const interval = handlers.pollIntervalMs ?? DEFAULT_POLL_MS
    handlers.onPoll?.()
    pollTimer = setInterval(() => {
      if (disposed) return
      handlers.onPoll?.()
      pollCycles += 1
      if (pollCycles % POLL_PROBE_EVERY === 0) {
        retryCount = 0
        connect()
      }
    }, interval)
  }

  /**
   * 停止轮询（SSE 恢复连接时调用）。
   * @returns 无
   */
  function stopPolling(): void {
    if (pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  }

  /**
   * 按指数退避安排下一次重连；超过最大次数则降级轮询。
   * @returns 无
   */
  function scheduleRetry(): void {
    if (disposed) return
    if (retryCount >= MAX_RETRY) {
      startPolling()
      return
    }
    const delay = BACKOFF_MS[retryCount]
    retryCount += 1
    status.value = 'connecting'
    reconnectTimer = setTimeout(connect, delay)
  }

  /**
   * 建立 EventSource 连接（重连入口）。
   * @returns 无
   * @throws EventSource 构造失败时进入重试流程（不向上抛出）
   */
  function connect(): void {
    if (disposed) return
    const target = resolveTarget()
    if (!target) {
      status.value = 'offline'
      return
    }
    clearTimers()
    closeSource()
    status.value = 'connecting'
    try {
      source = new EventSource(target)
    } catch (error) {
      handlers.onError?.(error)
      scheduleRetry()
      return
    }
    source.onopen = () => {
      retryCount = 0
      stopPolling()
      status.value = 'connected'
    }
    source.onmessage = (event: MessageEvent<string>) => {
      handlers.onMessage?.(event)
    }
    if (handlers.eventTypes?.length) {
      for (const type of handlers.eventTypes) {
        source.addEventListener(type, (event) => {
          handlers.onMessage?.(event as MessageEvent<string>)
        })
      }
    }
    // EventSource 断开时浏览器会自动携带 Last-Event-ID 重连，
    // 但为统一控制退避节奏，这里主动关闭并走自管重连。
    source.onerror = () => {
      closeSource()
      handlers.onError?.(new Error('SSE 连接中断'))
      void retryWithFreshToken()
    }
  }

  /**
   * 连接中断后的重连决策：先尝试静默续期再重连（修复 E2E 报告问题 2）。
   * EventSource 无法读取 401 状态码，而访问令牌过期后服务端会拒绝握手，
   * 表现与网络中断一致。此处以 10 秒节流主动续期：成功后立即重连（新令牌
   * 由 resolveTarget 重新读取），失败则回到原有指数退避，网络故障场景不受影响。
   * @returns 无
   */
  async function retryWithFreshToken(): Promise<void> {
    if (disposed) return
    const now = Date.now()
    if (now - lastAuthRetryAt > 10000) {
      lastAuthRetryAt = now
      const refreshed = await refreshAccessToken()
      if (refreshed && !disposed) {
        retryCount = 0
        stopPolling()
        connect()
        return
      }
    }
    scheduleRetry()
  }

  /**
   * 销毁订阅：停止重连 / 轮询并关闭连接。
   * @returns 无
   */
  function dispose(): void {
    disposed = true
    clearTimers()
    closeSource()
    status.value = 'offline'
  }

  /**
   * 手动重置重连计数并立即重连。
   * @returns 无
   */
  function reconnect(): void {
    retryCount = 0
    stopPolling()
    connect()
  }

  // URL 响应式变化时重置计数并重连
  if (isRef(url)) {
    watch(url, () => {
      retryCount = 0
      stopPolling()
      connect()
    })
  }

  // 组件内自动管理生命周期；组件外需自行调用 close
  if (getCurrentInstance()) {
    onMounted(connect)
    onUnmounted(dispose)
  } else {
    connect()
  }

  return { status, reconnect, close: dispose }
}

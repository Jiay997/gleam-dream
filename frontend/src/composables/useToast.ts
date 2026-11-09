/**
 * 文件级注释：全局轻提示（Toast）组合式函数。
 * 模块级单例状态：顶部右侧浮层，成功 / 常规 3s 自动消失，错误 5s。
 * 可在组件外（如 axios 拦截器）安全调用。
 */
import { readonly, reactive } from 'vue'

/** Toast 类型 */
export type ToastType = 'success' | 'error' | 'warning' | 'info'

/** 单条 Toast 数据 */
export interface ToastItem {
  /** 唯一 ID */
  id: number
  /** 类型 */
  type: ToastType
  /** 文案 */
  message: string
  /** 展示时长（毫秒） */
  duration: number
}

/** 默认展示时长（成功 / 警告 / 信息） */
const DEFAULT_DURATION = 3000
/** 错误展示时长 */
const ERROR_DURATION = 5000

/** 模块级共享状态（单例） */
const state = reactive<{ items: ToastItem[] }>({ items: [] })

/** 自增序列 */
let seq = 0
/** 定时器表，用于提前关闭时清理 */
const timers = new Map<number, ReturnType<typeof setTimeout>>()
/** 去重窗口内最近一次同类型同文案 Toast（修复 E2E 报告建议项：重复提示弹两次） */
const recentKeys = new Map<string, { id: number; at: number }>()
/** 去重窗口（毫秒）：窗口内相同文案刷新计时而非新增一条 */
const DEDUPE_WINDOW_MS = 1200

/**
 * 移除指定 Toast 并清理定时器。
 * @param id Toast 唯一 ID
 * @returns 无
 */
function dismiss(id: number): void {
  const index = state.items.findIndex((item) => item.id === id)
  if (index >= 0) {
    state.items.splice(index, 1)
  }
  const timer = timers.get(id)
  if (timer) {
    clearTimeout(timer)
    timers.delete(id)
  }
}

/**
 * 推入一条 Toast 并安排自动关闭。
 * @param type Toast 类型
 * @param message 文案
 * @param duration 自定义时长（毫秒），缺省按类型取 3s / 错误 5s
 * @returns Toast 唯一 ID
 */
function push(type: ToastType, message: string, duration?: number): number {
  const key = `${type}:${message}`
  const now = Date.now()
  const recent = recentKeys.get(key)
  if (recent && now - recent.at < DEDUPE_WINDOW_MS && state.items.some((item) => item.id === recent.id)) {
    // 同一提示在去重窗口内重复触发：刷新倒计时，不再叠加一条
    const oldTimer = timers.get(recent.id)
    if (oldTimer) clearTimeout(oldTimer)
    const repeatDuration = duration ?? (type === 'error' ? ERROR_DURATION : DEFAULT_DURATION)
    timers.set(recent.id, setTimeout(() => dismiss(recent.id), repeatDuration))
    recent.at = now
    return recent.id
  }
  const id = ++seq
  const finalDuration = duration ?? (type === 'error' ? ERROR_DURATION : DEFAULT_DURATION)
  state.items.push({ id, type, message, duration: finalDuration })
  timers.set(
    id,
    setTimeout(() => dismiss(id), finalDuration),
  )
  recentKeys.set(key, { id, at: now })
  return id
}

/** Toast 对外 API */
export interface ToastApi {
  /** 当前 Toast 列表（只读，请勿直接修改） */
  items: readonly ToastItem[]
  /** 手动关闭指定 Toast */
  dismiss: (id: number) => void
  /** 成功提示（3s） */
  success: (message: string, duration?: number) => number
  /** 错误提示（5s） */
  error: (message: string, duration?: number) => number
  /** 警告提示（3s） */
  warning: (message: string, duration?: number) => number
  /** 信息提示（3s） */
  info: (message: string, duration?: number) => number
}

/**
 * 获取全局 Toast API（组件内外均可调用）。
 * @returns Toast API 集合
 */
export function useToast(): ToastApi {
  return {
    items: readonly(state.items),
    dismiss,
    success: (message, duration) => push('success', message, duration),
    error: (message, duration) => push('error', message, duration),
    warning: (message, duration) => push('warning', message, duration),
    info: (message, duration) => push('info', message, duration),
  }
}

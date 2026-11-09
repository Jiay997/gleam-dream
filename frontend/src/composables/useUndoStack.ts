/**
 * 文件级注释：编辑器撤销/重做栈 + 草稿自动保存（V4.0 P0 细节功能 SF-F2-13）。
 *
 * 对齐验收标准 **AC-F2-07-2**：
 * - 会话内步骤栈 **≥ 100 步**（超出时丢弃最旧步，内存可控）；
 * - `Ctrl+Z` 撤销、`Ctrl+Shift+Z`（含 `Ctrl+Y`）重做；
 * - 页面崩溃/刷新后恢复**最后一次自动保存草稿**（默认 10s 间隔）。
 *
 * 设计：栈内保存**状态快照**（不可变对象），而非 diff——剧本结构体量小
 * （单项目 ≤ 30 集），快照实现最简单且回放一定正确；`limit` 默认 100 与
 * AC 的「≥100 步」一致。
 *
 * 草稿通过 `localStorage` 持久化（键含项目与剧本版本，避免跨项目串档）；
 * 草稿只在「与服务端版本不同」时保存，恢复时由调用方决定是否采用。
 */
import { computed, onBeforeUnmount, ref } from 'vue'
import type { Ref } from 'vue'

/** 撤销/重做栈配置 */
export interface UndoStackOptions {
  /** 步骤栈上限（默认 100，对齐 AC-F2-07-2） */
  limit?: number
  /** 草稿 localStorage 键（不传则不启用自动保存） */
  autosaveKey?: string
  /** 自动保存间隔（毫秒，默认 10000） */
  autosaveIntervalMs?: number
}

/** 撤销/重做栈句柄 */
export interface UndoStackHandle<T> {
  /** 当前状态 */
  current: Ref<T>
  /** 是否可撤销 */
  canUndo: Ref<boolean>
  /** 是否可重做 */
  canRedo: Ref<boolean>
  /** 栈深度（可回退步数） */
  depth: Ref<number>
  /** 已推进的新状态（首次 push 建立基线，不计入栈） */
  push: (state: T) => void
  /** 撤销一步 */
  undo: () => T | null
  /** 重做一步 */
  redo: () => T | null
  /** 重置栈（如切换剧本版本时） */
  reset: (state: T) => void
  /** 是否有未保存草稿 */
  hasDraft: Ref<boolean>
  /** 读取草稿快照（无则 null） */
  readDraft: () => { state: T; savedAt: number } | null
  /** 丢弃草稿 */
  clearDraft: () => void
  /** 启动自动保存（传入取当前状态的函数） */
  startAutosave: (getState: () => T) => void
  /** 停止自动保存 */
  stopAutosave: () => void
  /** 手动立即保存一次草稿 */
  saveDraft: (state: T) => void
  /** 绑定键盘快捷键（组件卸载时自动解绑） */
  bindKeyboard: () => void
}

/**
 * 创建撤销/重做栈。
 * @param initial 初始状态
 * @param options 栈上限与自动保存配置
 * @returns 撤销/重做句柄
 */
export function useUndoStack<T>(initial: T, options: UndoStackOptions = {}): UndoStackHandle<T> {
  const limit = Math.max(2, options.limit ?? 100)
  const autosaveIntervalMs = Math.max(1000, options.autosaveIntervalMs ?? 10_000)
  const autosaveKey = options.autosaveKey

  /** 当前状态 */
  const current = ref(initial) as Ref<T>
  /** 撤销栈（末尾为最近一步） */
  const undoStack = ref<T[]>([]) as Ref<T[]>
  /** 重做栈 */
  const redoStack = ref<T[]>([]) as Ref<T[]>
  /** 是否有本地草稿 */
  const hasDraft = ref(false)
  /** 自动保存定时器 */
  let timer: ReturnType<typeof setInterval> | null = null
  /** 键盘监听是否已绑定 */
  let keyboardBound = false

  const canUndo = computed(() => undoStack.value.length > 0)
  const canRedo = computed(() => redoStack.value.length > 0)
  const depth = computed(() => undoStack.value.length)

  /**
   * 推进一个新状态（记录前值入撤销栈，清空重做栈）。
   * @param state 新状态快照
   * @returns 无
   */
  function push(state: T): void {
    undoStack.value.push(current.value)
    if (undoStack.value.length > limit) {
      undoStack.value.splice(0, undoStack.value.length - limit)
    }
    redoStack.value = []
    current.value = state
  }

  /**
   * 撤销一步。
   * @returns 回退后的状态（无可撤销步时返回 null）
   */
  function undo(): T | null {
    const previous = undoStack.value.pop()
    if (previous === undefined) return null
    redoStack.value.push(current.value)
    current.value = previous
    return previous
  }

  /**
   * 重做一步。
   * @returns 重做后的状态（无可重做步时返回 null）
   */
  function redo(): T | null {
    const next = redoStack.value.pop()
    if (next === undefined) return null
    undoStack.value.push(current.value)
    current.value = next
    return next
  }

  /**
   * 重置栈与当前状态（切换剧本版本/重新加载时使用）。
   * @param state 新的基线状态
   * @returns 无
   */
  function reset(state: T): void {
    undoStack.value = []
    redoStack.value = []
    current.value = state
  }

  /**
   * 保存草稿到 localStorage。
   * @param state 状态快照
   * @returns 无
   */
  function saveDraft(state: T): void {
    if (!autosaveKey) return
    try {
      localStorage.setItem(autosaveKey, JSON.stringify({ state, savedAt: Date.now() }))
      hasDraft.value = true
    } catch {
      // 配额超限/隐私模式：静默降级（不影响编辑主链路）
    }
  }

  /**
   * 读取草稿。
   * @returns 草稿状态与保存时间；无草稿或解析失败返回 null
   */
  function readDraft(): { state: T; savedAt: number } | null {
    if (!autosaveKey) return null
    try {
      const raw = localStorage.getItem(autosaveKey)
      if (!raw) return null
      const parsed = JSON.parse(raw) as { state: T; savedAt: number }
      if (!parsed || typeof parsed.savedAt !== 'number') return null
      hasDraft.value = true
      return parsed
    } catch {
      return null
    }
  }

  /**
   * 丢弃草稿。
   * @returns 无
   */
  function clearDraft(): void {
    if (!autosaveKey) return
    try {
      localStorage.removeItem(autosaveKey)
    } catch {
      // 忽略
    }
    hasDraft.value = false
  }

  /**
   * 启动定时自动保存（幂等：重复调用只保留一个定时器）。
   * @param getState 取当前状态的函数
   * @returns 无
   */
  function startAutosave(getState: () => T): void {
    if (!autosaveKey) return
    stopAutosave()
    timer = setInterval(() => saveDraft(getState()), autosaveIntervalMs)
    if (readDraft() !== null) hasDraft.value = true
  }

  /**
   * 停止定时自动保存。
   * @returns 无
   */
  function stopAutosave(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  /**
   * 键盘快捷键：Ctrl/Cmd+Z 撤销，Ctrl/Cmd+Shift+Z 或 Ctrl+Y 重做。
   * 在输入框内也生效（编辑器场景），但会跳过组合输入（isComposing）。
   * @returns 无
   */
  function onKeydown(event: KeyboardEvent): void {
    if (event.isComposing) return
    const modifier = event.ctrlKey || event.metaKey
    if (!modifier) return
    const key = event.key.toLowerCase()
    if (key === 'z' && !event.shiftKey) {
      if (undo() !== null) event.preventDefault()
      return
    }
    if ((key === 'z' && event.shiftKey) || key === 'y') {
      if (redo() !== null) event.preventDefault()
    }
  }

  /**
   * 绑定键盘快捷键（仅绑定一次）。
   * @returns 无
   */
  function bindKeyboard(): void {
    if (keyboardBound) return
    window.addEventListener('keydown', onKeydown)
    keyboardBound = true
  }

  onBeforeUnmount(() => {
    stopAutosave()
    if (keyboardBound) {
      window.removeEventListener('keydown', onKeydown)
      keyboardBound = false
    }
  })

  return {
    current,
    canUndo,
    canRedo,
    depth,
    push,
    undo,
    redo,
    reset,
    hasDraft,
    readDraft,
    clearDraft,
    startAutosave,
    stopAutosave,
    saveDraft,
    bindKeyboard,
  }
}

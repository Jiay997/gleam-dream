/**
 * 文件级注释：界面状态 store。
 * 维护全局侧栏折叠（持久化）、右侧 AI 助手面板开关（默认收起）
 * 与移动端导航抽屉开关（<1024px 时 TopBar 汉堡按钮唤起）。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { STORAGE_KEYS } from '@/constants/storage'

/** AI 助手面板默认宽度（px） */
export const ASSISTANT_PANEL_DEFAULT_WIDTH = 320
/** AI 助手面板最小宽度（px） */
export const ASSISTANT_PANEL_MIN_WIDTH = 280
/** AI 助手面板最大宽度（px，视口不足时由面板再收敛） */
export const ASSISTANT_PANEL_MAX_WIDTH = 720
/** AI 助手底部抽屉最小高度（px） */
export const ASSISTANT_PANEL_MIN_HEIGHT = 260

/**
 * 界面 store：侧栏折叠 / 助手面板开关与尺寸。
 */
export const useUiStore = defineStore('ui', () => {
  /** 全局侧栏是否折叠（持久化） */
  const sidebarCollapsed = ref(tryReadCollapsed())
  /** 右侧 AI 助手面板是否展开（默认收起） */
  const assistantPanelOpen = ref(false)
  /** AI 助手面板宽度（px，桌面端拖拽后持久化） */
  const assistantPanelWidth = ref(tryReadAssistantWidth())
  /** AI 助手底部抽屉高度（px，移动端拖拽后持久化；0 表示按 60vh 自适应） */
  const assistantPanelHeight = ref(tryReadAssistantHeight())
  /** 移动端导航抽屉是否打开（<1024px 生效，桌面端始终关闭） */
  const mobileNavOpen = ref(false)

  /**
   * 打开移动端导航抽屉。
   * @returns 无
   */
  function openMobileNav(): void {
    mobileNavOpen.value = true
  }

  /**
   * 关闭移动端导航抽屉。
   * @returns 无
   */
  function closeMobileNav(): void {
    mobileNavOpen.value = false
  }

  /**
   * 从 localStorage 读取侧栏折叠状态。
   * @returns 是否折叠
   */
  function tryReadCollapsed(): boolean {
    try {
      return localStorage.getItem(STORAGE_KEYS.sidebarCollapsed) === '1'
    } catch {
      return false
    }
  }

  /**
   * 从 localStorage 读取助手面板宽度并收敛到合法区间。
   * @returns 面板宽度（px）
   */
  function tryReadAssistantWidth(): number {
    try {
      const raw = Number(localStorage.getItem(STORAGE_KEYS.assistantPanelWidth))
      if (!Number.isFinite(raw) || raw <= 0) return ASSISTANT_PANEL_DEFAULT_WIDTH
      return Math.min(ASSISTANT_PANEL_MAX_WIDTH, Math.max(ASSISTANT_PANEL_MIN_WIDTH, Math.round(raw)))
    } catch {
      return ASSISTANT_PANEL_DEFAULT_WIDTH
    }
  }

  /**
   * 从 localStorage 读取助手底部抽屉高度。
   * @returns 高度（px）；未设置或非法时为 0（由面板按视口 60% 计算）
   */
  function tryReadAssistantHeight(): number {
    try {
      const raw = Number(localStorage.getItem(STORAGE_KEYS.assistantPanelHeight))
      if (!Number.isFinite(raw) || raw <= 0) return 0
      return Math.max(ASSISTANT_PANEL_MIN_HEIGHT, Math.round(raw))
    } catch {
      return 0
    }
  }

  /**
   * 写入数值型持久化键（失败时静默忽略）。
   * @param key localStorage 键名
   * @param value 数值
   * @returns 无
   */
  function persistNumber(key: string, value: number): void {
    try {
      localStorage.setItem(key, String(value))
    } catch {
      // 忽略持久化失败（隐私模式 / 配额不足）
    }
  }

  /**
   * 设置并持久化助手面板宽度（收敛到全局最小/最大值）。
   * @param width 目标宽度（px）
   * @returns 无
   */
  function setAssistantPanelWidth(width: number): void {
    const clamped = Math.min(
      ASSISTANT_PANEL_MAX_WIDTH,
      Math.max(ASSISTANT_PANEL_MIN_WIDTH, Math.round(width)),
    )
    assistantPanelWidth.value = clamped
    persistNumber(STORAGE_KEYS.assistantPanelWidth, clamped)
  }

  /**
   * 设置并持久化助手底部抽屉高度（上限由面板按视口裁剪）。
   * @param height 目标高度（px）
   * @returns 无
   */
  function setAssistantPanelHeight(height: number): void {
    const clamped = Math.max(ASSISTANT_PANEL_MIN_HEIGHT, Math.round(height))
    assistantPanelHeight.value = clamped
    persistNumber(STORAGE_KEYS.assistantPanelHeight, clamped)
  }

  /**
   * 切换侧栏折叠状态并持久化。
   * @returns 无
   */
  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
    try {
      localStorage.setItem(STORAGE_KEYS.sidebarCollapsed, sidebarCollapsed.value ? '1' : '0')
    } catch {
      // 忽略持久化失败
    }
  }

  /**
   * 切换 AI 助手面板展开状态。
   * @returns 无
   */
  function toggleAssistantPanel(): void {
    assistantPanelOpen.value = !assistantPanelOpen.value
  }

  return {
    sidebarCollapsed,
    assistantPanelOpen,
    assistantPanelWidth,
    assistantPanelHeight,
    mobileNavOpen,
    toggleSidebar,
    toggleAssistantPanel,
    setAssistantPanelWidth,
    setAssistantPanelHeight,
    openMobileNav,
    closeMobileNav,
  }
})

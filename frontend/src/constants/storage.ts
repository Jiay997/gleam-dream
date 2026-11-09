/**
 * 文件级注释：localStorage 键名常量。
 * 集中管理所有持久化键名，供 http 拦截器与 Pinia store 共用，避免魔法字符串与循环依赖。
 */

/** localStorage 键名集合 */
export const STORAGE_KEYS = {
  /** 访问令牌 */
  token: 'gd_access_token',
  /** 刷新令牌 */
  refreshToken: 'gd_refresh_token',
  /** 当前用户信息（JSON 序列化） */
  user: 'gd_user',
  /** 侧栏折叠状态 */
  sidebarCollapsed: 'gd_sidebar_collapsed',
  /** AI 助手面板宽度（px，桌面端拖拽调整） */
  assistantPanelWidth: 'gd_assistant_panel_width',
  /** AI 助手底部抽屉高度（px，移动端拖拽调整） */
  assistantPanelHeight: 'gd_assistant_panel_height',
} as const

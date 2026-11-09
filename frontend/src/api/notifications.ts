/**
 * 文件级注释：通知域 API（与《通知中心》契约对齐）。
 * - GET  /notifications               通知列表（type/unread/page 筛选分页）；
 * - POST /notifications/{id}/read     标记单条已读；
 * - POST /notifications/read-all      全部标记已读；
 * - GET  /notifications/unread-count  未读数量。
 */
import { http } from './http'

/** 通知跳转负载（payload 中宽松约定：项目 / 审批 ID 供「去处理」跳转） */
export interface NotificationPayload {
  /** 关联项目 ID */
  project_id?: number | string
  /** 关联审批单 ID */
  approval_id?: number | string
  /** 其他扩展字段 */
  [key: string]: unknown
}

/** 站内通知（/notifications 列表项） */
export interface NotificationItem {
  /** 通知 ID */
  id: number | string
  /** 通知类型（approval 审批待办 / task 生成完成 / system 系统 / credit 积分） */
  type?: string
  /** 标题 */
  title: string
  /** 正文内容 */
  content?: string
  /** 是否未读 */
  unread?: boolean
  /** 跳转负载（去处理按钮消费） */
  payload?: NotificationPayload | null
  /** 创建时间（ISO8601） */
  created_at?: string | null
}

/** 通知列表查询参数 */
export interface ListNotificationsParams {
  /** 类型筛选（缺省全部） */
  type?: string
  /** 未读过滤（true 仅未读） */
  unread?: boolean
  /** 页码（≥1） */
  page?: number
  /** 每页条数 */
  page_size?: number
}

/**
 * 获取通知列表。
 * @param params 类型 / 未读 / 分页参数
 * @returns 分页通知列表
 */
export function listNotifications(params?: ListNotificationsParams): Promise<{ items: NotificationItem[]; total: number }> {
  return http.get<{ items: NotificationItem[]; total: number }>('/notifications', { params })
}

/**
 * 获取未读通知数。
 * @returns { count }
 */
export function getUnreadCount(): Promise<{ count: number }> {
  return http.get<{ count: number }>('/notifications/unread-count')
}

/**
 * 标记单条通知已读。
 * @param notificationId 通知 ID
 * @returns 无
 */
export function markNotificationRead(notificationId: string | number): Promise<void> {
  return http.post<void>(`/notifications/${notificationId}/read`)
}

/**
 * 标记全部通知已读。
 * @returns 无
 */
export function markAllNotificationsRead(): Promise<void> {
  return http.post<void>('/notifications/read-all')
}

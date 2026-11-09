/**
 * 文件级注释：通知状态 store。
 * 维护未读数与通知列表，顶栏铃铛与通知中心页共用。
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

import { getUnreadCount, listNotifications, markAllNotificationsRead, markNotificationRead } from '@/api/notifications'
import type { NotificationItem } from '@/api/notifications'

/**
 * 通知 store：未读数 / 列表 / 已读操作。
 */
export const useNotificationStore = defineStore('notification', () => {
  /** 未读数量 */
  const unreadCount = ref(0)
  /** 通知列表（最近一页） */
  const items = ref<NotificationItem[]>([])
  /** 加载中标记 */
  const loading = ref(false)

  /**
   * 刷新未读数量。
   * @returns 最新未读数
   */
  async function refreshUnread(): Promise<number> {
    const { count } = await getUnreadCount()
    unreadCount.value = count
    return count
  }

  /**
   * 加载通知列表（第一页，可按类型筛选）。
   * @param force 是否强制刷新（默认有缓存则跳过）
   * @param type 类型筛选（缺省全部）
   * @returns 通知列表
   */
  async function load(force = false, type?: string): Promise<NotificationItem[]> {
    if (!force && items.value.length > 0 && !type) {
      return items.value
    }
    loading.value = true
    try {
      const result = await listNotifications({ page: 1, page_size: 50, type: type || undefined })
      items.value = result.items
      return result.items
    } finally {
      loading.value = false
    }
  }

  /**
   * 标记单条已读并同步未读数。
   * @param notificationId 通知 ID
   * @returns 无
   */
  async function markRead(notificationId: string | number): Promise<void> {
    await markNotificationRead(notificationId)
    const target = items.value.find((item) => item.id === notificationId)
    if (target && target.unread) {
      target.unread = false
      unreadCount.value = Math.max(0, unreadCount.value - 1)
    }
  }

  /**
   * 全部标记已读。
   * @returns 无
   */
  async function markAllRead(): Promise<void> {
    await markAllNotificationsRead()
    for (const item of items.value) {
      item.unread = false
    }
    unreadCount.value = 0
  }

  return { unreadCount, items, loading, refreshUnread, load, markRead, markAllRead }
})

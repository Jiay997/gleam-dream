<!--
  文件级注释：P22a · 通知中心（路由 /notifications，AppShell 布局）· v7 纸上放映厅 Pro · 对照 F1-04-s1。
  刊头（PageHeader masthead + 右侧「全部已读」）+ 左列表卡 380px（类型筛选 Tab 全部/审批待办/
  生成完成/系统/积分 —— Phosphor 矢量图标，全部 Tab 展示未读徽标 99+ 封顶；通知列表：类型图标
  方块（语义洗底）/ 标题未读加粗
  + 未读蓝点 / 内容摘要行 / 相对时间；未读置顶——拉取后稳定排序，已读不重排）+ 右详情面板
  （类型胶囊 chip + 衬线大标题 + 完整正文 + 「前往处理」主按钮与「查看项目」次按钮，
  按 payload.project_id / approval_id / link 跳转）。
  数据走 GET /notifications?type=&unread=&page=，POST read / read-all，三态与加载更多保留。
-->
<script setup lang="ts">
/**
 * NotificationsView 通知中心。
 * props：无。emits：无。
 * 依赖 notifications API、notification store（未读数同步）、useToast。
 */
import { computed, onMounted, ref, type Component } from 'vue'
import { useRouter } from 'vue-router'

import { listNotifications } from '@/api/notifications'
import type { NotificationItem } from '@/api/notifications'
import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useToast } from '@/composables/useToast'
import { NOTIFICATION_TYPE_OPTIONS } from '@/constants/options'
import { useNotificationStore } from '@/stores/notification'
import { formatRelativeTime } from '@/utils/datetime'
import {
  PhClipboardText,
  PhCoins,
  PhFilmSlate,
  PhMegaphone,
  PhTray,
} from '@phosphor-icons/vue'

/** 路由实例（去处理跳转） */
const router = useRouter()
/** Toast 提示 */
const toast = useToast()
/** 通知 store（未读数与已读动作全局同步） */
const notificationStore = useNotificationStore()

/** 每页条数 */
const PAGE_SIZE = 20

/* ==================== 列表查询 ==================== */

/** 当前类型筛选（空串=全部） */
const typeFilter = ref('')
/** 当前页通知 */
const items = ref<NotificationItem[]>([])
/** 总条数 */
const total = ref(0)
/** 当前页码 */
const page = ref(1)
/** 加载中（首屏） */
const loading = ref(true)
/** 加载更多中 */
const loadingMore = ref(false)
/** 加载失败信息 */
const errorMsg = ref('')

/** 是否还有更多 */
const hasMore = computed(() => items.value.length < total.value)

/**
 * 未读置顶稳定排序（对照 F1-04-s1：未读项排在前，其余保持接口顺序；
 * 仅在拉取时排序一次，选中后标记已读不重排，避免正在查看的项跳动）。
 * @param list 接口返回的通知列表
 * @returns 排序后的列表
 */
function sortUnreadFirst(list: NotificationItem[]): NotificationItem[] {
  return [...list]
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      if (a.item.unread !== b.item.unread) return a.item.unread ? -1 : 1
      return a.index - b.index
    })
    .map((entry) => entry.item)
}

/**
 * 拉取通知列表（reset=true 时回到第一页重置列表）。
 * @param reset 是否重置为第一页
 * @returns 无
 */
async function fetchNotifications(reset = true): Promise<void> {
  if (reset) {
    loading.value = true
    page.value = 1
  } else {
    loadingMore.value = true
  }
  errorMsg.value = ''
  try {
    const targetPage = reset ? 1 : page.value + 1
    const result = await listNotifications({
      type: typeFilter.value || undefined,
      page: targetPage,
      page_size: PAGE_SIZE,
    })
    const merged = reset ? result.items : [...items.value, ...result.items]
    items.value = sortUnreadFirst(merged)
    total.value = result.total
    page.value = targetPage
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '通知加载失败'
  } finally {
    loading.value = false
    loadingMore.value = false
  }
}

/**
 * 类型 Tab 切换。
 * @param key 类型键（空串=全部）
 * @returns 无
 */
function pickType(key: string): void {
  typeFilter.value = key
  void fetchNotifications()
}

/* ==================== 类型视觉映射（对照 F1-04-s1 类型胶囊） ==================== */

/** 通知类型 → 胶囊底色/前景色（暖域语义色） */
const TYPE_CHIP: Record<string, string> = {
  approval: 'bg-info-bg text-info',
  task: 'bg-success-bg text-success',
  credit: 'bg-gold-fog text-gold-deep',
  system: 'bg-surface-sunken text-neutral-500',
}

/** 通知类型 → 列表字标底色 */
const TYPE_MARK: Record<string, string> = {
  approval: 'bg-info-bg text-info',
  task: 'bg-success-bg text-success',
  credit: 'bg-gold-fog text-gold-deep',
  system: 'bg-surface-sunken text-neutral-500',
}

/** 通知类型 → 矢量图标组件（替代 emoji，统一线性图标语言） */
const TYPE_ICONS: Record<string, Component> = {
  '': PhTray,
  approval: PhClipboardText,
  task: PhFilmSlate,
  system: PhMegaphone,
  credit: PhCoins,
}

/**
 * 查询类型矢量图标（未知类型回退托盘图标）。
 * @param type 类型键
 * @returns 图标组件
 */
function typeIcon(type: string): Component {
  return TYPE_ICONS[type] ?? PhTray
}

/**
 * 查询类型展示标签，未知类型回退「通知」。
 * @param type 类型键
 * @returns 类型标签
 */
function typeMeta(type: string): string {
  const option = NOTIFICATION_TYPE_OPTIONS.find((item) => item.key === (type ?? ''))
  return option?.label ?? '通知'
}

/**
 * 查询类型胶囊样式。
 * @param type 类型键
 * @returns 胶囊类
 */
function typeChipCls(type: string): string {
  return TYPE_CHIP[type] ?? TYPE_CHIP.system
}

/**
 * 查询类型字标样式。
 * @param type 类型键
 * @returns 字标类
 */
function typeMarkCls(type: string): string {
  return TYPE_MARK[type] ?? TYPE_MARK.system
}

/* ==================== 选中与详情 ==================== */

/** 当前选中通知 ID */
const selectedId = ref<number | string>('')

/** 当前选中通知 */
const selected = computed(() => items.value.find((item) => item.id === selectedId.value) ?? null)

/** 当前选中通知的 payload（宽松读取） */
const selectedPayload = computed<Record<string, unknown>>(() => {
  const payload = selected.value?.payload
  return typeof payload === 'object' && payload !== null ? (payload as Record<string, unknown>) : {}
})

/** 当前选中通知的关联项目 ID（存在时显示「查看项目」次按钮） */
const selectedProjectId = computed<string>(() => {
  const projectId = selectedPayload.value.project_id
  return projectId !== undefined && projectId !== null && projectId !== '' ? String(projectId) : ''
})

/**
 * 选中通知：首次点击时自动标记已读并同步未读数。
 * @param item 通知项
 * @returns 无
 */
async function pickNotification(item: NotificationItem): Promise<void> {
  selectedId.value = item.id
  if (item.unread) {
    try {
      await notificationStore.markRead(item.id)
    } catch {
      // 拦截器已 toast；已读失败不影响详情查看
    }
  }
}

/**
 * 「前往处理」跳转目标：优先 payload.link，其次按 project_id 跳审批台，
 * 仅 approval_id 时回审批台待办上下文（审批详情无独立路由）。
 * @returns 目标路径或空串（不可跳转）
 */
const processTarget = computed<string>(() => {
  const payload = selected.value?.payload
  if (!payload) return ''
  if (typeof payload.link === 'string' && payload.link) return payload.link
  const projectId = payload.project_id
  if (projectId !== undefined && projectId !== null && projectId !== '') {
    if (payload.approval_id !== undefined && payload.approval_id !== null) {
      // 有审批负载：跳审批台的待办上下文（审批入口在审批台关口面板）
      return `/projects/${projectId}/overview`
    }
    return `/projects/${projectId}/overview`
  }
  return ''
})

/**
 * 点击「前往处理」跳转。
 * @returns 无
 */
function goProcess(): void {
  if (processTarget.value) {
    void router.push(processTarget.value)
  }
}

/**
 * 点击「查看项目」跳转审批台。
 * @returns 无
 */
function goProject(): void {
  if (selectedProjectId.value) {
    void router.push(`/projects/${selectedProjectId.value}/overview`)
  }
}

/* ==================== 全部已读 ==================== */

/** 全部已读提交中 */
const markingAll = ref(false)

/**
 * 全部标记已读（store 同步顶栏未读数）。
 * @returns 无
 */
async function markAll(): Promise<void> {
  markingAll.value = true
  try {
    await notificationStore.markAllRead()
    for (const item of items.value) {
      item.unread = false
    }
    toast.success('已全部标记为已读')
  } catch {
    // 拦截器已 toast
  } finally {
    markingAll.value = false
  }
}

// 挂载：首屏列表 + 未读数
onMounted(() => {
  void fetchNotifications()
  void notificationStore.refreshUnread().catch(() => undefined)
})
</script>

<template>
  <div class="mx-auto max-w-content p-6 lg:p-8">
    <!-- ============ 刊头（masthead + 全部已读） ============ -->
    <PageHeader title="通知中心">
      <template #actions>
        <AppButton variant="secondary" size="md" :loading="markingAll" :disabled="notificationStore.unreadCount === 0" @click="markAll">
          全部已读
        </AppButton>
      </template>
    </PageHeader>

    <div class="flex gap-5">
      <!-- ==================== 左列表 380px ==================== -->
      <div class="w-full shrink-0 md:w-[380px]">
        <!-- 类型筛选 Tab -->
        <div class="flex rounded-lg border border-hairline bg-surface-inset p-0.5" role="tablist" aria-label="通知类型筛选">
          <button
            v-for="option in NOTIFICATION_TYPE_OPTIONS"
            :key="option.key"
            type="button"
            role="tab"
            class="relative flex h-8 flex-1 items-center justify-center gap-1 rounded-md text-[13px] font-medium transition-[background-color,color,box-shadow] duration-gd-fast ease-gd-standard"
            :class="typeFilter === option.key ? 'bg-surface-raised text-neutral-800 shadow-gd-1' : 'text-neutral-500 hover:text-neutral-700'"
            :aria-selected="typeFilter === option.key"
            @click="pickType(option.key)"
          >
            <component
              :is="typeIcon(option.key)"
              class="h-3.5 w-3.5 shrink-0"
              :weight="typeFilter === option.key ? 'fill' : 'regular'"
              aria-hidden="true"
            />
            {{ option.label }}
            <!-- 全部 Tab 未读徽标（蓝点 + 99+ 封顶） -->
            <span
              v-if="option.key === '' && notificationStore.unreadCount > 0"
              class="gd-num absolute -right-0.5 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-info px-1 text-micro text-[#04121a]"
            >
              {{ notificationStore.unreadCount > 99 ? '99+' : notificationStore.unreadCount }}
            </span>
          </button>
        </div>

        <!-- 列表卡片 -->
        <div class="mt-3 overflow-hidden gd-card-flat">
          <!-- 首屏骨架 -->
          <LoadingSkeleton v-if="loading" variant="text" :rows="6" class="p-4" />
          <!-- 加载失败 -->
          <ErrorRetryBlock v-else-if="errorMsg" message="通知加载失败" :description="errorMsg" @retry="fetchNotifications()" />
          <!-- 空态（矢量图标，替代 emoji） -->
          <EmptyState v-else-if="items.length === 0" title="暂无通知" description="有新的审批待办或生成结果时会在这里提醒你">
            <template #icon>
              <PhTray class="h-8 w-8 text-neutral-400" weight="light" aria-hidden="true" />
            </template>
          </EmptyState>
          <!-- 通知列表（未读置顶） -->
          <template v-else>
            <ul class="max-h-[560px] divide-y divide-hairline-soft overflow-y-auto">
              <li v-for="item in items" :key="item.id">
                <button
                  type="button"
                  class="flex w-full items-start gap-3 border-l-2 px-4 py-3.5 text-left transition-colors duration-gd-fast"
                  :class="[
                    // 左侧紫条 + 紫洗底只留给「选中」，未读则靠字重 + 小圆点表达。
                    // 两者共用同一套紫色之前，「未读」看起来就像「已选中」，
                    // 而右侧详情面板还是空的，形成明显的状态自相矛盾。
                    selectedId === item.id ? 'border-primary-500 bg-primary-100' : 'border-transparent bg-surface-raised hover:bg-surface-canvas',
                  ]"
                  @click="pickNotification(item)"
                >
                  <!-- 类型图标方块（语义洗底 + 同色图标） -->
                  <span
                    class="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg"
                    :class="typeMarkCls(item.type ?? '')"
                    aria-hidden="true"
                  >
                    <component :is="typeIcon(item.type ?? '')" class="h-4 w-4" weight="fill" />
                  </span>
                  <!-- 标题 + 摘要 + 时间 -->
                  <div class="min-w-0 flex-1">
                    <p class="flex items-center gap-1.5 truncate text-sm" :class="item.unread ? 'font-semibold text-neutral-900' : 'text-neutral-600'">
                      <span class="truncate">{{ item.title }}</span>
                      <span v-if="item.unread" class="h-1.5 w-1.5 shrink-0 rounded-full bg-primary-500" aria-label="未读" />
                    </p>
                    <p v-if="item.content" class="mt-0.5 truncate text-caption text-neutral-400">
                      {{ item.content.replace(/\s+/g, ' ') }}
                    </p>
                    <p class="gd-num mt-1 text-caption text-neutral-300">{{ formatRelativeTime(item.created_at ?? undefined) }}</p>
                  </div>
                </button>
              </li>
            </ul>
            <!-- 加载更多 -->
            <div v-if="hasMore" class="border-t border-hairline-soft p-2.5">
              <AppButton variant="ghost" size="sm" block :loading="loadingMore" @click="fetchNotifications(false)">加载更多</AppButton>
            </div>
          </template>
        </div>
      </div>

      <!-- ==================== 右详情面板 ==================== -->
      <div class="hidden min-w-0 flex-1 md:block">
        <div class="min-h-[420px] gd-card p-6 sm:p-8">
          <template v-if="selected">
            <!-- 类型胶囊 chip（对照 F1-04-s1：approval · 关口审批）+ 时间贴右 -->
            <div class="flex items-start justify-between gap-3">
              <span class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-caption font-medium" :class="typeChipCls(selected.type ?? '')">
                <span class="h-1.5 w-1.5 rounded-full bg-current opacity-70" aria-hidden="true"></span>
                {{ selected.type || 'notification' }} · {{ typeMeta(selected.type ?? '') }}
              </span>
              <!-- 时间属于「元信息」，跟着类型 chip 走；原先跟操作按钮同一行 ml-auto，
                   会被读成「无需处理 …… 5 分钟前」，像按钮的后缀注记。 -->
              <span class="gd-num shrink-0 pt-1 text-caption text-neutral-300">{{ formatRelativeTime(selected.created_at ?? undefined) }}</span>
            </div>

            <!-- 衬线大标题 -->
            <h2 class="mt-3 font-serif text-2xl font-semibold leading-snug text-neutral-900">{{ selected.title }}</h2>

            <!-- 完整内容 -->
            <div class="mt-4 text-[15px] leading-[1.9] text-neutral-700">
              <p class="whitespace-pre-wrap">{{ selected.content || '（无正文内容）' }}</p>
            </div>

            <!-- 操作区：前往处理 + 查看项目 -->
            <div class="mt-8 flex flex-wrap items-center gap-3">
              <AppButton v-if="processTarget" variant="primary" size="md" @click="goProcess">前往处理</AppButton>
              <AppButton v-else variant="secondary" size="md" disabled>无需处理</AppButton>
              <AppButton v-if="selectedProjectId" variant="secondary" size="md" @click="goProject">查看项目</AppButton>
            </div>
          </template>

          <!-- 未选中占位（矢量图标，替代 emoji） -->
          <div v-else class="flex h-full min-h-[380px] flex-col items-center justify-center text-center">
            <span class="flex h-14 w-14 items-center justify-center rounded-2xl border border-hairline bg-surface-canvas text-neutral-400" aria-hidden="true">
              <PhTray class="h-6 w-6" weight="light" />
            </span>
            <p class="mt-4 text-sm text-neutral-500">在左侧选择一条通知查看详情</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

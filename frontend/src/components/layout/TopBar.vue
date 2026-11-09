<!--
  文件级注释：顶栏（极夜 · 极光：60px 玻璃条 / 下缘 1px 光谱细线 / blur16）。
  结构保持不变：
  - 左侧面包屑「制片办公室 / 页面名」；
  - 中部全局搜索（/projects 页已有页面级搜索，此处隐藏避免双搜索框）；
  - 右侧操作区：积分徽章（琥珀配额色）、通知铃铛（玫红未读点）、助手开关、头像菜单。
  刊头不由顶栏承载：衬线刊头统一回到内容区 PageHeader（route.meta.titleInTopBar 仅保留兼容）。
-->
<script setup lang="ts">
/**
 * TopBar 顶栏。
 * 无 props / emit；依赖 auth / project / billing / notification / ui store。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { useBillingStore } from '@/stores/billing'
import { useNotificationStore } from '@/stores/notification'
import { useProjectStore } from '@/stores/project'
import { useUiStore } from '@/stores/ui'
import CreditBadge from '@/components/common/CreditBadge.vue'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const projectStore = useProjectStore()
const billingStore = useBillingStore()
const notificationStore = useNotificationStore()
const uiStore = useUiStore()

/** 头像菜单展开状态 */
const avatarMenuOpen = ref(false)
/** 头像菜单根元素引用（点击外部关闭） */
const avatarMenuRef = ref<HTMLDivElement | null>(null)
/** 全局搜索关键词（回车或点击放大镜后跳转项目工作台过滤） */
const globalKeyword = ref('')

/** 面包屑段：[制片办公室, (项目名), 页面名] */
const breadcrumbs = computed<string[]>(() => {
  const crumbs = ['制片办公室']
  if (route.meta.projectScoped && projectStore.currentProject) {
    crumbs.push(projectStore.currentProject.title)
  }
  crumbs.push(route.meta.title ?? '')
  return crumbs
})

/** 头像展示字符（昵称首字，缺省"客"） */
const avatarChar = computed(() => (authStore.user?.nickname ? authStore.user.nickname.charAt(0) : '客'))

/**
 * 切换头像菜单。
 * @returns 无
 */
function toggleAvatarMenu(): void {
  avatarMenuOpen.value = !avatarMenuOpen.value
}

/**
 * 菜单项跳转后关闭菜单。
 * @param path 目标路径
 * @returns 无
 */
 function goMenu(path: string): void {
  avatarMenuOpen.value = false
  void router.push(path)
}

/**
 * 退出登录并跳转登录页。
 * @returns 无
 */
async function handleLogout(): Promise<void> {
  avatarMenuOpen.value = false
  await authStore.logout()
  await router.push('/login')
}

/**
 * 提交全局搜索：跳转项目工作台并把关键词写入 `?q=`，由工作台客户端过滤项目名。
 * 关键词为空时仍跳转工作台并清空既有筛选，避免"输入无效"的死交互。
 * @returns 无
 */
function submitGlobalSearch(): void {
  const keyword = globalKeyword.value.trim()
  void router.push({
    path: '/projects',
    query: keyword ? { q: keyword } : {},
  })
}

/**
 * 文档级点击：点击头像菜单外部时收起。
 * @param event 鼠标事件
 * @returns 无
 */
function onDocumentClick(event: MouseEvent): void {
  if (avatarMenuOpen.value && avatarMenuRef.value && !avatarMenuRef.value.contains(event.target as Node)) {
    avatarMenuOpen.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', onDocumentClick)
  // 顶栏数据预取：积分与未读数（失败静默，拦截器已提示网络类错误）
  if (authStore.isLoggedIn) {
    void billingStore.refresh().catch(() => undefined)
    void notificationStore.refreshUnread().catch(() => undefined)
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocumentClick)
})
</script>

<template>
  <!-- 顶栏必须自建高层级层叠上下文（drawer=50，低于 modal=100 / toast=110）：
       内容区按钮带 isolation:isolate 会生成 stacking context，若顶栏 z-index 为 auto，
       其下缘的头像菜单即使写 z-30 也只能困在顶栏内部，会被内容区按钮压住。 -->
  <header
    class="relative z-drawer flex h-[var(--gd-topbar-h)] shrink-0 items-center gap-3 border-b border-[var(--gd-line-soft)] bg-[rgba(4,7,15,0.55)] px-4 backdrop-blur-[16px] backdrop-saturate-125 lg:gap-4 lg:px-6"
  >
    <!-- 顶栏下缘的光谱细线：1px，只做「层级分界」，不承载信息 -->
    <span
      class="pointer-events-none absolute inset-x-0 bottom-0 h-px bg-[image:var(--gd-grad-hairline)] opacity-60"
      aria-hidden="true"
    />

    <!-- 移动端汉堡按钮（<1024px 显示，唤起全局导航抽屉；44px 触控目标） -->
    <button
      type="button"
      class="-ml-1.5 flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-[var(--gd-ink-2)] transition-colors duration-200 hover:bg-[rgba(255,255,255,0.045)] hover:text-[var(--gd-ink)] lg:hidden"
      aria-label="打开导航菜单"
      aria-haspopup="dialog"
      @click="uiStore.openMobileNav()"
    >
      <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
        <path d="M3.75 6.75h16.5M3.75 12h16.5M3.75 17.25h16.5" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </button>

    <!-- 面包屑：12.5px，末段 brand 字色（不是纯白，避免与刊头争第一眼） -->
    <nav class="flex min-w-0 items-center gap-1.5 overflow-hidden text-[12.5px]" aria-label="面包屑">
      <template v-for="(crumb, index) in breadcrumbs" :key="`${crumb}-${index}`">
        <span
          v-if="index > 0"
          class="shrink-0 text-[var(--gd-ink-3)]"
          :class="index < breadcrumbs.length - 1 ? 'hidden sm:inline' : ''"
          aria-hidden="true"
        >/</span>
        <span
          class="truncate"
          :class="[
            index === breadcrumbs.length - 1
              ? 'min-w-0 font-semibold text-[var(--gd-ink)]'
              : 'text-[var(--gd-ink-3)]',
            index === 0 ? 'shrink-0' : '',
            index > 0 && index < breadcrumbs.length - 1 ? 'hidden min-w-0 sm:inline' : '',
          ]"
        >
          {{ crumb }}
        </span>
      </template>
    </nav>

    <!-- 全局搜索（/projects 已有页面级搜索，此处隐藏避免双搜索框） -->
    <div v-if="route.path !== '/projects'" class="hidden min-w-0 flex-1 justify-center md:flex">
      <form
        class="gd-field-group flex h-9 w-64 items-center gap-2 px-3 text-[12.5px] text-[var(--gd-ink-3)]"
        role="search"
        @submit.prevent="submitGlobalSearch"
      >
        <button type="submit" class="shrink-0 transition-colors hover:text-[var(--gd-ink)]" aria-label="搜索项目">
          <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <path d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <input
          v-model="globalKeyword"
          type="search"
          class="w-full bg-transparent text-[12.5px] text-[var(--gd-ink)] outline-none placeholder:text-[var(--gd-ink-3)]"
          placeholder="搜索项目名…（回车）"
          aria-label="按项目名搜索"
        />
      </form>
    </div>

    <!-- 右侧操作区：积分 → 通知 → 助手 → 头像 -->
    <div class="ml-auto flex shrink-0 items-center gap-2 lg:gap-2.5">
      <CreditBadge />

      <!-- 通知铃铛（32px 玻璃圆钮 + 玫红未读点） -->
      <RouterLink
        to="/notifications"
        class="relative flex h-8 w-8 items-center justify-center rounded-full border border-[var(--gd-line)] bg-[rgba(255,255,255,0.03)] text-[var(--gd-ink-2)] transition-[color,border-color,background-color] duration-200 hover:border-[var(--gd-line-brand)] hover:bg-[rgba(139,124,255,0.1)] hover:text-[var(--gd-ink)]"
        title="通知中心"
      >
        <svg class="h-[15px] w-[15px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <path
            d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
        <span
          v-if="notificationStore.unreadCount > 0"
          class="absolute right-2 top-1.5 h-1.5 w-1.5 rounded-full bg-[var(--gd-rose)] shadow-[0_0_8px_var(--gd-rose)]"
          aria-hidden="true"
        />
      </RouterLink>

      <!-- 助手面板开关（桌面为右侧栏，移动端为底部抽屉） -->
      <button
        type="button"
        class="flex h-8 w-8 items-center justify-center rounded-full border transition-[color,border-color,background-color] duration-200"
        :class="
          uiStore.assistantPanelOpen
            ? 'border-[var(--gd-line-brand)] bg-[rgba(139,124,255,0.16)] text-[var(--gd-ink)]'
            : 'border-[var(--gd-line)] bg-[rgba(255,255,255,0.03)] text-[var(--gd-ink-2)] hover:border-[var(--gd-line-brand)] hover:text-[var(--gd-ink)]'
        "
        title="AI 创作助手"
        @click="uiStore.toggleAssistantPanel()"
      >
        <svg class="h-[15px] w-[15px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <path
            d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </button>

      <!-- 头像菜单（星紫→洋红渐变圆 + 未读点） -->
      <div ref="avatarMenuRef" class="relative">
        <button
          type="button"
          class="relative flex h-8 w-8 items-center justify-center rounded-full bg-[linear-gradient(135deg,#6a55f0,#f07be0)] text-xs font-semibold text-[#fffaf0] shadow-[inset_0_1px_0_rgba(255,255,255,0.28),0_0_20px_-8px_rgba(139,124,255,0.9)] transition-[filter] duration-200 hover:brightness-110"
          aria-haspopup="menu"
          :aria-expanded="avatarMenuOpen"
          :aria-label="`账号菜单：${authStore.user?.nickname ?? '未登录'}`"
          @click="toggleAvatarMenu"
        >
          {{ avatarChar }}
          <span
            v-if="notificationStore.unreadCount > 0"
            class="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full border-2 border-[var(--gd-canvas)] bg-[var(--gd-rose)]"
            aria-hidden="true"
          />
        </button>
        <div
          v-if="avatarMenuOpen"
          class="absolute right-0 top-10 z-30 w-48 overflow-hidden rounded-[14px] border border-[var(--gd-line)] bg-[rgba(22,32,58,0.97)] py-1.5 shadow-[var(--gd-elev-overlay)] backdrop-blur-[16px]"
          role="menu"
        >
          <div class="border-b border-[var(--gd-line-soft)] px-3 py-2">
            <p class="truncate text-[13px] font-medium text-[var(--gd-ink)]">{{ authStore.user?.nickname ?? '未登录' }}</p>
            <p class="truncate text-[11px] text-[var(--gd-ink-3)]">{{ authStore.user?.phone ?? '' }}</p>
          </div>
          <button type="button" class="block w-full px-3 py-2 text-left text-[13px] text-[var(--gd-ink-2)] transition-colors duration-150 hover:bg-[rgba(255,255,255,0.045)] hover:text-[var(--gd-ink)]" role="menuitem" @click="goMenu('/settings/profile')">
            个人设置
          </button>
          <button type="button" class="block w-full px-3 py-2 text-left text-[13px] text-[var(--gd-ink-2)] transition-colors duration-150 hover:bg-[rgba(255,255,255,0.045)] hover:text-[var(--gd-ink)]" role="menuitem" @click="goMenu('/notifications')">
            通知中心
          </button>
          <button type="button" class="block w-full border-t border-[var(--gd-line-soft)] px-3 py-2 text-left text-[13px] text-[var(--gd-rose)] transition-colors duration-150 hover:bg-[var(--gd-rose-wash)]" role="menuitem" @click="handleLogout">
            退出登录
          </button>
        </div>
      </div>
    </div>
  </header>
</template>


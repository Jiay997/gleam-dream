<!--
  文件级注释：应用整体框架（AppShell）· 方向 B「杂志感编辑风」v2。
  结构：左侧全局导航（固定 240px，近中性暖灰）+ 顶栏 64px + 项目内导航（项目上下文时）+
  内容区（router-view）+ 右侧 AI 助手面板（可收起）+ 底部状态条 32px。
  响应式：<1024px 全局导航转为覆盖式抽屉（汉堡按钮唤起 / 遮罩点击 / Esc / 路由跳转均可关闭）；
  <1280px 助手面板由右侧栏切换为底部抽屉形态（开关状态跨断点保留，尺寸各自独立）。
  方向 B v2 变更点：
  - 侧栏固定 240px 近中性暖灰（--gd-sidebar-bg），与台面靠发丝线分区，不再随视口折叠；
  - 台面统一走 surface-canvas（页面底 = 浅暖色台面，白色卡片读作"纸"）；
  - 内容区最大宽度由各视图读 max-w-content（= --gd-content-max，令牌单源）收敛为 1600px 居中；
  - router-view 增加 220ms 淡入上移过渡（尊重 prefers-reduced-motion）。
  SSE 订阅在此建立并镜像状态到 project store（占位流地址，后端就绪后接入）。
-->
<script setup lang="ts">
/**
 * AppShell 应用框架。
 * 无 props / emit；依赖 ui / project / auth store，useSSE 建立实时通道。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { useBillingStore } from '@/stores/billing'
import { useProjectStore } from '@/stores/project'
import { useUiStore } from '@/stores/ui'
import { useSSE, type SseStatus } from '@/composables/useSSE'

import GlobalSidebar from './GlobalSidebar.vue'
import TopBar from './TopBar.vue'
import ProjectSidebar from './ProjectSidebar.vue'
import StatusBar from './StatusBar.vue'
import AssistantPanel from './AssistantPanel.vue'

const route = useRoute()
const uiStore = useUiStore()
const projectStore = useProjectStore()
const authStore = useAuthStore()
const billingStore = useBillingStore()

/** 是否为窄屏（<1280px） */
const isNarrowScreen = ref(false)

/**
 * 同步视口断点：回宽屏收敛移动端抽屉。
 * 助手面板在窄屏切换为底部抽屉形态，打开状态跨断点保留（不再强制收起）。
 * @returns 无
 */
function syncViewport(): void {
  isNarrowScreen.value = window.innerWidth < 1280
  if (!isNarrowScreen.value) {
    // 回到桌面宽度时收敛移动端抽屉，避免桌面残留遮罩
    uiStore.closeMobileNav()
  }
}

onMounted(() => {
  syncViewport()
  window.addEventListener('resize', syncViewport)
  // Esc 关闭移动端抽屉
  document.addEventListener('keydown', onDocKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', syncViewport)
  document.removeEventListener('keydown', onDocKeydown)
})

/**
 * 文档级按键：Esc 关闭移动端导航抽屉。
 * @param event 键盘事件
 * @returns 无
 */
function onDocKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape' && uiStore.mobileNavOpen) {
    uiStore.closeMobileNav()
  }
}

// 窄屏联动：收敛移动端导航抽屉；助手面板由组件自行切换为底部抽屉形态
watch(isNarrowScreen, () => {
  uiStore.closeMobileNav()
})

// 是否显示项目内导航（当前项目已加载且处于项目域路由）
const showProjectNav = computed(() => Boolean(projectStore.currentProject) && route.meta.projectScoped === true)

// 路由跳转后自动收起移动端抽屉
watch(
  () => route.fullPath,
  () => {
    if (uiStore.mobileNavOpen) uiStore.closeMobileNav()
  },
)

/** 是否展示助手面板（窄屏改为底部抽屉，仍由同一开关控制） */
const showAssistant = computed(() => uiStore.assistantPanelOpen)

/**
 * SSE 事件分发：
 * - status_changed 同步当前项目详情到 store，保证底部状态条与页面状态
 *   判定（如分镜生成失败自动回滚）实时一致；
 * - 积分变动类事件（渲染/资产/分镜/合成任务终结）触发顶栏余额刷新
 *   （修复测试报告 #31：渲染结算后顶部余额/冻结数需手动刷新才更新），
 *   8s 节流避免批次事件风暴导致连续请求。
 * @param event 消息事件
 * @returns 无
 */
const CREDIT_EVENTS = new Set([
  'render.task.updated',
  'render.batch.completed',
  'asset.generated',
  'asset.generation_failed',
  'storyboard.generated',
  'storyboard_generate.failed',
  'post.render.completed',
])
/** 上次余额刷新时间戳（节流用） */
let lastBillingRefresh = 0

function onSseMessage(event: MessageEvent<string>): void {
  // P4-b 修复：项目状态变更（含后端失败自动回滚）立即刷新当前项目，
  // 其余事件类型（任务进度 / 资产状态）待对应消费方接入
  if (event.type === 'status_changed' && projectStore.currentProject) {
    void projectStore.refreshCurrentProject()
  }
  // #31：任务终结类事件 → 顶栏余额/冻结数实时刷新（8s 节流）
  if (CREDIT_EVENTS.has(event.type)) {
    const now = Date.now()
    if (now - lastBillingRefresh > 8000) {
      lastBillingRefresh = now
      void billingStore.refresh()
    }
  }
}

/**
 * SSE 降级轮询回调（占位：轮询刷新当前项目与任务列表）。
 * @returns 无
 */
function onSsePoll(): void {
  // 占位：projectStore.loadProjects(true) 等
}

// 登录后建立全局 SSE 订阅（D22/D23 修复：EventSource 无法带 header，
// token 经 ?access_token= 查询参数传递，后端 get_current_user 内建支持）
const sseUrl = computed<string>(() =>
  authStore.isLoggedIn && authStore.token
    ? `/api/v1/events/stream?access_token=${authStore.token}`
    : '',
)
const { status } = useSSE(sseUrl, { onMessage: onSseMessage, onPoll: onSsePoll })

// 镜像 SSE 状态到 project store（供状态条消费）
watch(
  status,
  (value: SseStatus) => {
    projectStore.setSseStatus(value)
  },
  { immediate: true },
)
</script>

<template>
  <!-- 壳层根节点不铺底色：让 body::before 的六层极光透上来（aurora §3.1 光层配比） -->
  <div class="flex h-screen w-screen overflow-hidden bg-transparent">
    <!-- 左侧全局导航（桌面态：<1024px 隐藏，由下方抽屉接管） -->
    <GlobalSidebar class="hidden lg:flex" />

    <!-- 右侧主区域 -->
    <div class="flex min-w-0 flex-1 flex-col">
      <TopBar />
      <ProjectSidebar v-if="showProjectNav" />

      <!-- 内容行：主内容 + 辅助面板 -->
      <div class="flex min-h-0 flex-1">
        <main class="min-w-0 flex-1 overflow-y-auto">
          <!-- 路由切换过渡：不引入中间包裹层，避免破坏画布类页面的 h-full 高度传递 -->
          <RouterView v-slot="{ Component }">
            <Transition name="gd-page" mode="out-in">
              <component :is="Component" :key="route.fullPath" />
            </Transition>
          </RouterView>
        </main>
        <AssistantPanel v-if="showAssistant" :narrow="isNarrowScreen" />
      </div>

      <StatusBar />
    </div>

    <!-- ============ 移动端导航抽屉（<1024px） ============ -->
    <Transition name="gd-fade">
      <div
        v-if="uiStore.mobileNavOpen"
        class="fixed inset-0 z-drawer bg-overlay backdrop-blur-[8px] lg:hidden"
        aria-hidden="true"
        @click="uiStore.closeMobileNav()"
      />
    </Transition>
    <Transition name="gd-drawer">
      <GlobalSidebar
        v-if="uiStore.mobileNavOpen"
        :force-expanded="true"
        class="fixed inset-y-0 left-0 z-modal shadow-gd-4 lg:hidden"
        role="dialog"
        aria-modal="true"
        aria-label="导航菜单"
        @close="uiStore.closeMobileNav()"
      />
    </Transition>
  </div>
</template>

<style scoped>
.gd-page-enter-active {
  transition: opacity var(--gd-duration-normal) var(--gd-ease-out),
    transform var(--gd-duration-normal) var(--gd-ease-out);
}

.gd-page-leave-active {
  transition: opacity var(--gd-duration-fast) var(--gd-ease-standard);
}

.gd-page-enter-from {
  opacity: 0;
  transform: translateY(4px);
}

.gd-page-leave-to {
  opacity: 0;
}

/* 遮罩淡入淡出 */
.gd-fade-enter-active,
.gd-fade-leave-active {
  transition: opacity var(--gd-duration-normal) var(--gd-ease-out);
}

.gd-fade-enter-from,
.gd-fade-leave-to {
  opacity: 0;
}

/* 抽屉滑入滑出（尊重 prefers-reduced-motion：全局已有降级规则） */
.gd-drawer-enter-active,
.gd-drawer-leave-active {
  transition: transform var(--gd-duration-slow) var(--gd-ease-out);
}

.gd-drawer-enter-from,
.gd-drawer-leave-to {
  transform: translateX(-100%);
}
</style>

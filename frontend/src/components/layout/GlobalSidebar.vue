<!--
  文件级注释：全局导航侧栏（232px 玻璃导航轨）· 主题「极夜 · 极光」。
  视觉契约（aurora §7.7 侧边导航）：
  - 容器：rgba(8,13,26,.6) + blur18 + 右缘 1px 冷光细线，与画布靠「明度差 + 细线」分区；
  - 导航项：38px 高 / r8 / 13.5px；选中态 = 星紫 34% 描边 + 135° 紫洗渐变 +
    左侧 2px 光谱条 + 24px 微光晕 + 文字近白 600（不铺满高饱和底，避免与主按钮争焦点）；
  - 分组标题：9.5px / +0.2em 全大写 / 三级墨色（场记板上的字）；
  - 品牌区：星紫渐变形标 + 奶油色衬线品牌名（全站唯一暖色材料）。
   功能契约不变：forceExpanded（移动端抽屉关闭按钮）、close 事件、路由前缀激活判定、
  底部用户卡（点击进入个人设置）与退出登录。
  v3：资产库改为全局入口（/assets 资产档案库 → 项目资产档案），不再依赖当前项目上下文；
  链路内的「资产设计」（STAGE 03）由项目内导航承载，两者入口与层级严格分离。
  v4：新增「项目工具」动态分组（仅项目上下文显示）——连续性 / 参数中心 / V4 能力
  三个扩展模块从项目内链路流程条移出，统一改为从左侧侧边栏打开。
-->
<script setup lang="ts">
/**
 * GlobalSidebar 全局侧栏。
 * props：forceExpanded —— 移动端抽屉态显示关闭按钮（桌面端不传）。
 * emit：close —— 移动端抽屉关闭按钮 / 导航项点击回调。
 * 依赖 auth store（用户信息 / 登出）、project store（当前项目上下文）。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'

defineProps<{
  /** 移动端抽屉态：显示关闭按钮 */
  forceExpanded?: boolean
}>()

const emit = defineEmits<{
  /** 移动端抽屉请求关闭 */
  close: []
}>()

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()
const projectStore = useProjectStore()

/** 导航项 */
interface NavItem {
  /** 唯一标识 */
  key: string
  /** 中文名称 */
  label: string
  /** 图标 path（24x24 stroke，1.5 描边） */
  icon: string
  /** 目标路由 */
  to: string
}

/** 导航分组（两段：创作 / 事务） */
interface NavSection {
  /** 分组刊眉 */
  label: string
  /** 组内导航项 */
  items: NavItem[]
}

/** 静态导航分组（资产库为全局资产档案库入口，不随项目上下文变化） */
const NAV_SECTIONS: NavSection[] = [
  {
    label: '创作',
    items: [
      {
        key: 'workspace',
        label: '项目工作台',
        icon: 'M3.75 6A2.25 2.25 0 016 3.75h2.25A2.25 2.25 0 0110.5 6v2.25a2.25 2.25 0 01-2.25 2.25H6a2.25 2.25 0 01-2.25-2.25V6zM13.5 6a2.25 2.25 0 012.25-2.25H18A2.25 2.25 0 0120.25 6v2.25A2.25 2.25 0 0118 10.5h-2.25a2.25 2.25 0 01-2.25-2.25V6zM3.75 15.75A2.25 2.25 0 016 13.5h2.25a2.25 2.25 0 012.25 2.25V18a2.25 2.25 0 01-2.25 2.25H6A2.25 2.25 0 013.75 18v-2.25zM13.5 15.75a2.25 2.25 0 012.25-2.25H18a2.25 2.25 0 012.25 2.25V18A2.25 2.25 0 0118 20.25h-2.25A2.25 2.25 0 0113.5 18v-2.25z',
        to: '/projects',
      },
      {
        key: 'assets',
        label: '资产库',
        icon: 'M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M2.25 18.75h16.5a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5H3.75A1.5 1.5 0 002.25 6v11.25a1.5 1.5 0 001.5 1.5zm10.5-11.25h.008v.008h-.008V7.5zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z',
        to: '/assets',
      },
    ],
  },
  {
    label: '事务',
    items: [
      {
        key: 'billing',
        label: '计费中心',
        icon: 'M2.25 8.25h19.5M2.25 9h19.5m-16.5 5.25h6m-6 2.25h3m-3.75 3h15a2.25 2.25 0 002.25-2.25V6.75A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25v10.5A2.25 2.25 0 004.5 19.5z',
        to: '/billing',
      },
      {
        key: 'team',
        label: '团队与成员',
        icon: 'M15 19.128a9.38 9.38 0 002.625.372 9.337 9.337 0 004.121-.952 4.125 4.125 0 00-7.533-2.493M15 19.128v-.003c0-1.113-.285-2.16-.786-3.07M15 19.128v.106A12.318 12.318 0 018.624 21c-2.331 0-4.512-.645-6.374-1.766l-.001-.109a6.375 6.375 0 0111.964-3.07M12 6.375a3.375 3.375 0 11-6.75 0 3.375 3.375 0 016.75 0zm8.25 2.25a2.625 2.625 0 11-5.25 0 2.625 2.625 0 015.25 0z',
        to: '/settings/team',
      },
      {
        key: 'platform',
        label: '平台授权',
        icon: 'M13.19 8.688a4.5 4.5 0 011.242 7.244l-4.5 4.5a4.5 4.5 0 01-6.364-6.364l1.757-1.757m13.35-.622l1.757-1.757a4.5 4.5 0 00-6.364-6.364l-4.5 4.5a4.5 4.5 0 001.242 7.244',
        to: '/settings/platform-accounts',
      },
      {
        key: 'models',
        label: '模型配置',
        icon: 'M8.25 3v1.5M4.5 8.25H3m18 0h-1.5M4.5 12H3m18 0h-1.5m-15 3.75H3m18 0h-1.5M8.25 19.5V21M12 3v1.5m0 15V21m3.75-18v1.5m0 15V21m-9-1.5h10.5a2.25 2.25 0 002.25-2.25V6.75a2.25 2.25 0 00-2.25-2.25H6.75A2.25 2.25 0 004.5 6.75v10.5a2.25 2.25 0 002.25 2.25zm.75-12h9v9h-9v-9z',
        to: '/settings/models',
      },
      {
        key: 'settings',
        label: '设置',
        icon: 'M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0h9.75',
        to: '/settings/profile',
      },
    ],
  },
]

/**
 * 计算导航项是否激活（前缀匹配；/assets 同时覆盖项目资产档案 /assets/:projectId）。
 * @param item 导航项
 * @returns 是否激活
 */
function isActive(item: NavItem): boolean {
  return route.path === item.to || route.path.startsWith(`${item.to}/`)
}

/**
 * 「项目工具」动态分组：仅在项目上下文中显示（当前项目 ID 非空）。
 * 承载从项目内链路流程条移出的三个扩展模块入口。
 */
const projectTools = computed<NavSection>(() => ({
  label: '项目工具',
  items: [
    {
      key: 'continuity',
      label: '连续性',
      icon: 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z',
      to: `/projects/${projectStore.currentProjectId}/continuity`,
    },
    {
      key: 'params',
      label: '参数中心',
      icon: 'M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z',
      to: `/projects/${projectStore.currentProjectId}/params`,
    },
    {
      key: 'v4',
      label: 'V4 能力',
      icon: 'M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z',
      to: `/projects/${projectStore.currentProjectId}/v4`,
    },
  ],
}))

/** 头像展示字符（昵称首字，缺省"客"） */
const avatarChar = computed(() => (authStore.user?.nickname ? authStore.user.nickname.charAt(0) : '客'))

/**
 * 退出登录并跳转登录页。
 * @returns 无
 */
async function handleLogout(): Promise<void> {
  emit('close')
  await authStore.logout()
  await router.push('/login')
}
</script>

<template>
  <aside
    class="flex w-sidebar shrink-0 flex-col border-r border-[var(--gd-line-soft)] bg-[rgba(8,13,26,0.6)] backdrop-blur-[18px] backdrop-saturate-150"
  >
    <!-- 品牌区：星紫渐变形标 + 奶油色衬线品牌名（唯一暖色材料） -->
    <div class="flex h-[60px] shrink-0 items-center gap-2.5 px-5">
      <span
        class="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-[image:var(--gd-grad-brand)] font-serif text-[13px] font-semibold text-[#fffaf0] shadow-[0_0_18px_-6px_rgba(139,124,255,0.9),inset_0_1px_0_rgba(255,255,255,0.25)]"
      >
        梦
      </span>
      <p class="min-w-0 flex-1 truncate font-serif text-[15px] font-semibold tracking-[0.02em] text-[var(--gd-ink-heading)]">
        浮光造梦
      </p>
      <!-- 移动端抽屉关闭按钮（44px 触控目标） -->
      <button
        v-if="forceExpanded"
        type="button"
        class="-mr-1.5 flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-[var(--gd-ink-3)] transition-colors duration-200 hover:bg-[rgba(255,255,255,0.045)] hover:text-[var(--gd-ink)]"
        aria-label="关闭导航"
        @click="emit('close')"
      >
        <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
          <path d="M6 18L18 6M6 6l12 12" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </button>
    </div>

    <!-- 导航分组：aurora 导航项（38px / r8 / 紫洗渐变激活 + 左侧光谱条） -->
    <nav class="flex-1 overflow-y-auto px-3 pb-3" aria-label="主导航">
      <section v-for="section in NAV_SECTIONS" :key="section.label">
        <p class="gd-nav__group pl-2 pr-2">{{ section.label }}</p>
        <div class="space-y-0.5">
          <RouterLink
            v-for="item in section.items"
            :key="item.key"
            :to="item.to"
            class="gd-nav__item"
            :class="isActive(item) ? 'is-active' : ''"
            :title="item.label"
            :aria-current="isActive(item) ? 'page' : undefined"
            @click="emit('close')"
          >
            <svg
              class="gd-nav__icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
              aria-hidden="true"
            >
              <path :d="item.icon" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <span class="truncate">{{ item.label }}</span>
          </RouterLink>
        </div>
      </section>

      <!-- 项目工具动态分组：仅项目上下文显示（连续性 / 参数中心 / V4 能力） -->
      <section v-if="projectStore.currentProject">
        <p class="gd-nav__group pl-2 pr-2">{{ projectTools.label }}</p>
        <div class="space-y-0.5">
          <RouterLink
            v-for="item in projectTools.items"
            :key="item.key"
            :to="item.to"
            class="gd-nav__item"
            :class="isActive(item) ? 'is-active' : ''"
            :title="item.label"
            :aria-current="isActive(item) ? 'page' : undefined"
            @click="emit('close')"
          >
            <svg
              class="gd-nav__icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.5"
              aria-hidden="true"
            >
              <path :d="item.icon" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <span class="truncate">{{ item.label }}</span>
          </RouterLink>
        </div>
      </section>
    </nav>

    <!-- 底部：用户卡（整卡是「个人设置」入口，玻璃材质 + 悬停点亮品牌描边）+ 幽灵退出按钮 -->
    <div class="shrink-0 space-y-2 px-3 pb-4">
      <RouterLink
        to="/settings/profile"
        class="group flex cursor-pointer items-center gap-2.5 rounded-[14px] border border-[var(--gd-line)] bg-[rgba(255,255,255,0.03)] p-2.5 transition-[background-color,border-color,box-shadow] duration-200 hover:border-[var(--gd-line-brand)] hover:bg-[rgba(139,124,255,0.1)] hover:shadow-[0_0_18px_-10px_rgba(139,124,255,0.9)] active:bg-[rgba(139,124,255,0.14)]"
        :title="`个人设置 · ${authStore.user?.nickname ?? '未登录'}`"
        aria-label="打开个人设置"
        @click="emit('close')"
      >
        <span
          class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[linear-gradient(135deg,#6a55f0,#f07be0)] text-sm font-semibold text-[#fffaf0] shadow-[inset_0_1px_0_rgba(255,255,255,0.25)]"
          aria-hidden="true"
        >
          {{ avatarChar }}
        </span>
        <div class="min-w-0 flex-1 leading-tight">
          <p class="truncate text-[13px] font-medium text-[var(--gd-ink)]">{{ authStore.user?.nickname ?? '未登录' }}</p>
          <p class="mt-0.5 truncate text-[11px] text-[var(--gd-ink-3)]">{{ authStore.user?.phone ?? '' }}</p>
        </div>
        <!-- 指向性提示：悬停时右移 0.5px 并提亮，暗示「可进入」 -->
        <svg
          class="h-3.5 w-3.5 shrink-0 text-[var(--gd-ink-3)] transition-[color,transform] duration-200 group-hover:translate-x-0.5 group-hover:text-[var(--gd-ink)]"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          aria-hidden="true"
        >
          <path d="M8.25 4.5l7.5 7.5-7.5 7.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </RouterLink>
      <button
        type="button"
        class="flex h-10 w-full items-center justify-center gap-2 rounded-[14px] border border-[var(--gd-line)] bg-transparent text-[13px] font-medium text-[var(--gd-ink-2)] transition-[background-color,border-color,color] duration-200 hover:border-[var(--gd-line-brand)] hover:bg-[rgba(139,124,255,0.1)] hover:text-[var(--gd-ink)]"
        @click="handleLogout"
      >
        <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <path d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15m3 0l3-3m0 0l-3-3m3 3H9" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        退出登录
      </button>
    </div>
  </aside>
</template>

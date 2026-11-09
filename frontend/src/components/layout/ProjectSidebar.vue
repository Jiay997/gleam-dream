<!--
  文件级注释：项目内导航（链路流程条）。
  仅在项目上下文中显示：审批台 / 剧本 / 资产设计 / 配音轨 / 分镜 / 生成 / 编排 / 后期 / 分发，
  每个 Tab 带流程序号角标（后续任务接入真实进度）。
  v2：连续性 / 参数中心 / V4 能力三个扩展模块移出链路流程，改为从左侧全局侧栏
  「项目工具」分组打开；照片合成角色取消独立入口，功能内嵌至资产设计的角色模块。
-->
<script setup lang="ts">
/**
 * ProjectSidebar 项目内导航。
 * 无 props / emit；依赖 project store 提供当前项目 ID。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import { useProjectStore } from '@/stores/project'

const route = useRoute()
const projectStore = useProjectStore()

/** 项目内导航项 */
interface ProjectTab {
  /** 唯一标识 */
  key: string
  /** 中文名称 */
  label: string
  /** 路径模板（{id} 替换为项目 ID） */
  pathTemplate: string
  /** 流程序号（1-9） */
  stage: number
}

/**
 * 流程 Tab：9 个主流程。扩展模块（连续性 / 参数中心 / V4 能力）已移至
 * GlobalSidebar「项目工具」分组；照片合成角色内嵌于资产设计的角色模块。
 */
const TABS: ProjectTab[] = [
  // 第一项承载 HITL 关口待办与项目状态，统一命名为「审批台」；
  // 路由仍为 /overview（保持既有深链与守卫兼容），标题/面包屑/菜单名一致。
  { key: 'overview', label: '审批台', pathTemplate: '/projects/{id}/overview', stage: 1 },
  { key: 'script', label: '剧本', pathTemplate: '/projects/{id}/script', stage: 2 },
  { key: 'assets', label: '资产设计', pathTemplate: '/projects/{id}/assets', stage: 3 },
  // V6.0 生产链路「配音先行」：台词在剧本页产出后先配音，再进入分镜绑定台词与视频生成。
  // 配音轨原先排在「后期」之后，会让用户误以为配音是剪辑阶段的附属步骤，且在生成前漏掉关口 V。
  { key: 'voice', label: '配音轨', pathTemplate: '/projects/{id}/voice', stage: 4 },
  { key: 'storyboard', label: '分镜', pathTemplate: '/projects/{id}/storyboard', stage: 5 },
  { key: 'render', label: '生成', pathTemplate: '/projects/{id}/render-tasks', stage: 6 },
  // 编排先确定合成顺序，再由后期执行剪辑合成：两者顺序不可反接，否则用户会在后期做完合成后又回到编排。
  { key: 'composition', label: '编排', pathTemplate: '/projects/{id}/composition', stage: 7 },
  { key: 'post', label: '后期', pathTemplate: '/projects/{id}/post', stage: 8 },
  { key: 'distribute', label: '分发', pathTemplate: '/projects/{id}/distribute', stage: 9 },
]

/** 解析后的 Tab 路径（依赖当前项目） */
const resolvedTabs = computed<Array<ProjectTab & { to: string }>>(() => {
  const projectId = projectStore.currentProjectId
  return TABS.map((tab) => ({ ...tab, to: tab.pathTemplate.replace('{id}', projectId) }))
})

/**
 * 计算 Tab 是否激活（前缀匹配，覆盖 outline/review 等子路由）。
 * @param tab 导航 Tab
 * @returns 是否激活
 */
function isActive(tab: ProjectTab & { to: string }): boolean {
  return route.path === tab.to || route.path.startsWith(`${tab.to}/`)
}
</script>

<template>
  <!--
    窄屏（<1024）7 个 Tab 放不下：改为横向滚动，而不是让文字逐字换行。
    shrink-0 + whitespace-nowrap 保证每个 Tab 保持单行完整形态。
  -->
  <nav
    v-if="projectStore.currentProject"
    class="gd-panel-rail flex h-12 shrink-0 items-stretch gap-1 overflow-x-auto border-b border-hairline px-4"
    aria-label="项目内导航"
  >
    <RouterLink
      v-for="tab in resolvedTabs"
      :key="tab.key"
      :to="tab.to"
      class="relative flex shrink-0 items-center gap-1.5 whitespace-nowrap border-b-2 px-3 text-sm font-medium transition-[color,border-color,background-color] duration-gd-fast ease-gd-standard"
      :class="isActive(tab) ? 'border-primary-500 text-primary-700' : 'border-transparent text-neutral-500 hover:bg-surface-canvas hover:text-neutral-700'"
    >
      <!-- 流程序号角标（占位：后续接入真实阶段进度） -->
      <span
        class="gd-num flex h-4 w-4 items-center justify-center rounded-full text-micro"
        :class="isActive(tab) ? 'gd-fill-brand' : 'bg-surface-inset text-neutral-500'"
        aria-hidden="true"
      >
        {{ tab.stage }}
      </span>
      {{ tab.label }}
    </RouterLink>
  </nav>
</template>

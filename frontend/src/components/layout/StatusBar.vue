<!--
  文件级注释：底部状态条（32px）。
  展示 SSE 连接状态圆点、当前项目状态、预算使用（已用 / 总预算）与链路追踪 ID 占位。
-->
<script setup lang="ts">
/**
 * StatusBar 底部状态条。
 * 无 props / emit；依赖 project store（SSE 状态 / 当前项目 / 预算）与 http 模块（trace ID）。
 */
import { computed } from 'vue'

import { lastRequestId } from '@/api/http'
import type { SseStatus } from '@/composables/useSSE'
import { useProjectStore } from '@/stores/project'
import { statusInfo } from '@/constants/statusTone'

const projectStore = useProjectStore()

/** SSE 状态 → 圆点颜色 / 文案 */
const SSE_META: Record<SseStatus, { color: string; label: string }> = {
  connected: { color: 'var(--gd-locked)', label: '实时连接' },
  connecting: { color: 'var(--gd-pending)', label: '连接中' },
  polling: { color: 'var(--gd-warning)', label: '轮询模式' },
  offline: { color: 'var(--gd-neutral-300)', label: '未连接' },
}

/** 当前 SSE 元信息 */
const sseMeta = computed(() => SSE_META[projectStore.sseStatus])

/** 已用预算（无项目时不展示数值，避免展示演示值误导用户） */
const usedBudget = computed(() => projectStore.currentProject?.spent_credits ?? null)

/** 总预算（同上） */
const totalBudget = computed(() => projectStore.currentProject?.budget_credits ?? null)

/** 预算使用百分比（无项目时 0） */
const budgetPercent = computed(() => {
  if (usedBudget.value === null || totalBudget.value === null || totalBudget.value <= 0) return 0
  return Math.round((usedBudget.value / totalBudget.value) * 100)
})

/** 预算条颜色（超 80% 转警示色） */
const budgetColor = computed(() => (budgetPercent.value >= 80 ? 'var(--gd-warning)' : 'var(--gd-primary-500)'))

/** 项目状态视觉信息（无项目时为 null） */
const projectTone = computed(() => (projectStore.currentProject ? statusInfo(projectStore.currentProject.status) : null))
</script>

<template>
  <footer class="flex h-8 shrink-0 items-center gap-5 border-t border-hairline bg-surface-canvas px-4 text-xs text-neutral-500">
    <!-- SSE 连接态 -->
    <span class="flex items-center gap-1.5">
      <span class="h-2 w-2 rounded-full" :style="{ backgroundColor: sseMeta.color }" aria-hidden="true" />
      {{ sseMeta.label }}
    </span>

    <span class="hidden h-3 w-px bg-hairline md:block" aria-hidden="true" />

    <!-- 项目状态（小屏隐藏，为状态条瘦身） -->
    <span v-if="projectStore.currentProject && projectTone" class="hidden items-center gap-1.5 md:flex">
      <span class="h-2 w-2 rounded-full" :style="{ backgroundColor: projectTone.color }" aria-hidden="true" />
      {{ projectTone.label }}
    </span>
    <span v-else class="hidden text-neutral-400 md:inline">未打开项目</span>

    <span class="hidden h-3 w-px bg-hairline sm:block" aria-hidden="true" />

    <!-- 预算使用（小屏隐藏；无项目时只展示占位，不展示演示数值） -->
    <span class="hidden items-center gap-2 sm:flex">
      <span>预算</span>
      <template v-if="usedBudget !== null && totalBudget !== null">
        <span class="gd-num text-neutral-700">{{ usedBudget.toLocaleString('zh-CN') }} / {{ totalBudget.toLocaleString('zh-CN') }}</span>
        <span class="relative h-1.5 w-20 overflow-hidden rounded-full bg-surface-inset" aria-hidden="true">
          <span class="absolute inset-y-0 left-0 rounded-full transition-[width,background-color] duration-gd-slow ease-gd-out" :style="{ width: `${Math.min(budgetPercent, 100)}%`, backgroundColor: budgetColor }" />
        </span>
        <span class="gd-num">{{ budgetPercent }}%</span>
      </template>
      <span v-else class="text-neutral-400">-</span>
    </span>

    <!-- 链路追踪（占位） -->
    <span class="ml-auto hidden font-mono text-caption text-neutral-400 lg:inline">
      trace: {{ lastRequestId || '-' }}
    </span>
  </footer>
</template>

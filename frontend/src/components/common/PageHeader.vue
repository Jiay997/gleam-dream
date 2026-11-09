<!--
  文件级注释：页面头组件（主题「极夜 · 极光」· 统一刊头）。
  统一规格（全站除项目工作台外的页面共用）：
  - 左侧 4px 星紫光条（色彩签名，占位高度对齐标题字身）；
  - 28px 衬线刊头（奶油色 #F0EAD8 + 双层辉光，沿用 .gd-masthead）；
  - 不再承载刊眉 / 英文副标 / 导语 / 底部光谱分隔线——标题即刊头，右侧仅保留操作区；
  - 项目工作台（ProjectsView）自绘刊头，不使用本组件。
-->
<script setup lang="ts">
/**
 * PageHeader 页面头。
 * props：title —— 页面标题（固定文案或资源名称）。
 * slot：actions —— 右侧操作区（按钮组等）。
 * 说明：副标题/刊眉/字号档位已收敛为全局唯一规格，不再提供可选参数。
 */
defineProps<{
  /** 页面标题 */
  title: string
}>()
</script>

<template>
  <header class="mb-6 flex flex-wrap items-center justify-between gap-4">
    <div class="flex min-w-0 items-center gap-3">
      <!-- 刊头光条：全站统一 4px × 28px，高度与 28px 标题字身齐平 -->
      <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
      <h1 class="gd-masthead truncate text-display-lg">{{ title }}</h1>
    </div>
    <div v-if="$slots.actions" class="flex shrink-0 flex-wrap items-center gap-2.5">
      <slot name="actions" />
    </div>
  </header>
</template>

<!--
  文件级注释：加载骨架屏组件（方向 B v2）。
  形态变体：card（封面块 + 文本行）/ table（表头 + 行）/ text（多行文本）/
  chart（柱状图占位），统一使用全局 .gd-skeleton（surface-sunken 扫光动画）。
  v2 变更：骨架底走 surface-sunken；卡片内距对齐 p-6；新增可选 chart 变体
  （既有 variant 名称与默认值保持不变）。
-->
<script setup lang="ts">
/**
 * LoadingSkeleton 骨架屏。
 * props：
 * - variant：形态 card/table/text/chart，默认 card；
 * - rows：行数（text/table/chart 形态生效），默认 3。
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 骨架形态 */
    variant?: 'card' | 'table' | 'text' | 'chart'
    /** 行数 */
    rows?: number
  }>(),
  { variant: 'card', rows: 3 },
)

/** 行索引序列 */
const rowList = computed(() => Array.from({ length: props.rows }, (_, index) => index))
</script>

<template>
  <!-- 卡片形态：大图块 + 两行文本（容器复用 .gd-card，与真实卡片同规格） -->
  <div v-if="variant === 'card'" class="gd-card w-full p-6">
    <div class="gd-skeleton aspect-video w-full" />
    <div class="gd-skeleton mt-4 h-4 w-2/3" />
    <div class="gd-skeleton mt-2 h-4 w-1/3" />
  </div>

  <!-- 表格形态：表头 + 数据行 -->
  <div v-else-if="variant === 'table'" class="gd-card-flat w-full overflow-hidden">
    <div class="flex gap-4 border-b border-hairline bg-neutral-100/70 px-4 py-3">
      <div v-for="cell in rowList" :key="`head-${cell}`" class="gd-skeleton h-3 flex-1" />
    </div>
    <div v-for="row in rowList" :key="`row-${row}`" class="flex items-center gap-4 border-b border-hairline px-4 py-3 last:border-b-0">
      <div class="gd-skeleton h-3 flex-1" />
      <div class="gd-skeleton h-3 flex-1" />
      <div class="gd-skeleton h-3 w-16" />
    </div>
  </div>

  <!-- 图表形态：柱状图占位（贴合数据看板的真实布局） -->
  <div v-else-if="variant === 'chart'" class="gd-card w-full p-6">
    <div class="gd-skeleton h-4 w-1/3" />
    <div class="mt-6 flex h-40 items-end gap-3">
      <div
        v-for="bar in rowList"
        :key="`bar-${bar}`"
        class="gd-skeleton flex-1"
        :style="{ height: `${30 + ((bar * 37) % 70)}%` }"
      />
    </div>
  </div>

  <!-- 文本形态：多行不等宽 -->
  <div v-else class="w-full space-y-2.5">
    <div v-for="line in rowList" :key="`line-${line}`" class="gd-skeleton h-4" :style="{ width: line % 3 === 2 ? '60%' : '100%' }" />
  </div>
</template>

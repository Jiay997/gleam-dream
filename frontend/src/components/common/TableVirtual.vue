<!--
  文件级注释：轻量虚拟滚动表格。
  行高固定场景下按可视区域裁剪渲染行，支持自定义列与作用域插槽单元格。
-->
<script setup lang="ts" generic="T">
/**
 * TableVirtual 虚拟滚动表格。
 * props：
 * - rows：行数据数组；
 * - columns：列配置（key / title / width / align）；
 * - rowHeight：行高像素，默认 48；
 * - height：视口高度像素，默认 480。
 * slot：cell —— 作用域插槽 { row, column, index } 自定义单元格（缺省输出字符串值）。
 */
import { computed, ref } from 'vue'

/** 列配置 */
export interface TableColumn {
  /** 列标识（对应行对象字段名） */
  key: string
  /** 列标题 */
  title: string
  /** 列宽像素（缺省均分剩余空间） */
  width?: number
  /** 对齐方式 */
  align?: 'left' | 'center' | 'right'
}

const props = withDefaults(
  defineProps<{
    /** 行数据 */
    rows: T[]
    /** 列配置 */
    columns: TableColumn[]
    /** 行高像素 */
    rowHeight?: number
    /** 视口高度像素 */
    height?: number
  }>(),
  { rowHeight: 48, height: 480 },
)

/** 上下 overscan 行数 */
const OVERSCAN = 5

/** 当前滚动位置 */
const scrollTop = ref(0)

/** 总高度 */
const totalHeight = computed(() => props.rows.length * props.rowHeight)

/** 起始渲染索引（含 overscan） */
const startIndex = computed(() => Math.max(0, Math.floor(scrollTop.value / props.rowHeight) - OVERSCAN))

/** 可视行容量（含 overscan） */
const visibleCount = computed(() => Math.ceil(props.height / props.rowHeight) + OVERSCAN * 2)

/** 结束渲染索引 */
const endIndex = computed(() => Math.min(props.rows.length, startIndex.value + visibleCount.value))

/** 当前渲染行切片 */
const visibleRows = computed(() => props.rows.slice(startIndex.value, endIndex.value))

/** 渲染区垂直偏移 */
const offsetY = computed(() => startIndex.value * props.rowHeight)

/**
 * 滚动事件处理：记录滚动位置。
 * @param event 滚动事件
 * @returns 无
 */
function onScroll(event: Event): void {
  scrollTop.value = (event.target as HTMLDivElement).scrollTop
}

/**
 * 缺省单元格文本：取行对象字段并字符串化。
 * @param row 行数据
 * @param key 列标识
 * @returns 单元格文本
 */
function cellText(row: T, key: string): string {
  const value = (row as unknown as Record<string, unknown>)[key]
  if (value === null || value === undefined) return ''
  return String(value)
}

/**
 * 计算列样式（定宽 / 均分 + 对齐）。
 * @param column 列配置
 * @returns 样式对象（可选键允许 undefined）
 */
function columnStyle(column: TableColumn): Record<string, string | undefined> {
  return {
    width: column.width ? `${column.width}px` : undefined,
    flex: column.width ? '0 0 auto' : '1 1 0%',
    textAlign: column.align ?? 'left',
  }
}
</script>

<template>
  <div class="gd-card-flat w-full overflow-hidden">
    <!-- 表头 -->
    <div class="flex bg-surface-sunken text-caption font-medium text-neutral-500">
      <div v-for="column in columns" :key="column.key" class="px-4 py-2.5" :style="columnStyle(column)">
        {{ column.title }}
      </div>
    </div>

    <!-- 虚拟滚动视口 -->
    <div class="overflow-y-auto" :style="{ height: `${height}px` }" @scroll="onScroll">
      <div v-if="rows.length === 0" class="flex h-full items-center justify-center text-sm text-neutral-400">暂无数据</div>
      <div v-else class="relative" :style="{ height: `${totalHeight}px` }">
        <div :style="{ transform: `translateY(${offsetY}px)` }">
          <div
            v-for="(row, rowIndex) in visibleRows"
            :key="startIndex + rowIndex"
            class="flex items-center border-t border-hairline transition-colors hover:bg-surface-canvas"
            :style="{ height: `${rowHeight}px` }"
          >
            <div v-for="column in columns" :key="column.key" class="truncate px-4 text-sm text-neutral-700" :style="columnStyle(column)">
              <slot name="cell" :row="row" :column="column" :index="startIndex + rowIndex">{{ cellText(row, column.key) }}</slot>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

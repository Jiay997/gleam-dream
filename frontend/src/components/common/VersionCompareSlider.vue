<!--
  文件级注释：版本对比滑块组件。
  左右两版图片以垂直分割线对比展示，拖动手柄调整对比位置；
  用于资产 / 镜头的新旧版本比对。
-->
<script setup lang="ts">
/**
 * VersionCompareSlider 版本对比滑块。
 * props：
 * - beforeSrc：旧版图片地址（左侧）；
 * - afterSrc：新版图片地址（右侧）；
 * - beforeLabel：旧版角标文案，默认"旧版"；
 * - afterLabel：新版角标文案，默认"新版"；
 * - initialPercent：初始分割位置百分比，默认 50。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 旧版图片地址 */
    beforeSrc: string
    /** 新版图片地址 */
    afterSrc: string
    /** 旧版角标文案 */
    beforeLabel?: string
    /** 新版角标文案 */
    afterLabel?: string
    /** 初始分割位置百分比 */
    initialPercent?: number
  }>(),
  { beforeLabel: '旧版', afterLabel: '新版', initialPercent: 50 },
)

/** 分割位置百分比 */
const percent = ref(props.initialPercent)
/** 是否拖拽中 */
const dragging = ref(false)
/** 容器引用 */
const containerRef = ref<HTMLDivElement | null>(null)

/**
 * 依据指针事件更新分割位置。
 * @param event 指针事件
 * @returns 无
 */
function updateFromPointer(event: PointerEvent): void {
  const rect = containerRef.value?.getBoundingClientRect()
  if (!rect || rect.width === 0) return
  const next = ((event.clientX - rect.left) / rect.width) * 100
  percent.value = Math.min(100, Math.max(0, next))
}

/**
 * 开始拖拽（绑定全局指针事件）。
 * @param event 指针事件
 * @returns 无
 */
function startDrag(event: PointerEvent): void {
  dragging.value = true
  updateFromPointer(event)
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', stopDrag)
}

/**
 * 拖拽中更新位置。
 * @param event 指针事件
 * @returns 无
 */
function onMove(event: PointerEvent): void {
  if (dragging.value) updateFromPointer(event)
}

/**
 * 结束拖拽（解绑全局事件）。
 * @returns 无
 */
function stopDrag(): void {
  dragging.value = false
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', stopDrag)
}

onMounted(() => {
  // 组件卸载兜底清理
  window.addEventListener('pointerup', stopDrag, { once: true })
})

onBeforeUnmount(() => {
  stopDrag()
})
</script>

<template>
  <div
    ref="containerRef"
    class="relative h-full w-full select-none overflow-hidden rounded-lg bg-surface-sunken"
    :class="dragging ? 'cursor-grabbing' : 'cursor-grab'"
    @pointerdown="startDrag"
  >
    <!-- 新版（底层全幅） -->
    <img :src="afterSrc" :alt="afterLabel" class="absolute inset-0 h-full w-full object-cover" draggable="false" />
    <!-- 旧版（按百分比裁剪覆盖；内层图片按比例放大还原为全幅视觉） -->
    <div class="absolute inset-y-0 left-0 overflow-hidden" :style="{ width: `${percent}%` }">
      <img :src="beforeSrc" :alt="beforeLabel" class="absolute inset-y-0 left-0 h-full object-cover" :style="{ width: `${10000 / Math.max(percent, 1)}%` }" draggable="false" />
    </div>

    <!-- 分割线与手柄 -->
    <div class="absolute inset-y-0 w-0.5 bg-white shadow-gd-2" :style="{ left: `calc(${percent}% - 1px)` }" aria-hidden="true">
      <span class="absolute left-1/2 top-1/2 flex h-8 w-8 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border-2 border-white gd-fill-brand shadow-gd-3 transition-transform duration-gd-fast ease-gd-out" :class="dragging ? 'scale-110' : ''">
        <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M8 7l-5 5 5 5M16 7l5 5-5 5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </span>
    </div>

    <!-- 角标（遮罩走 scrim 令牌，禁止裸 rgba/hex） -->
    <span class="absolute left-2 top-2 rounded-sm bg-scrim-soft px-1.5 py-0.5 text-xs text-white">{{ beforeLabel }}</span>
    <span class="absolute right-2 top-2 rounded-sm bg-scrim-soft px-1.5 py-0.5 text-xs text-white">{{ afterLabel }}</span>
  </div>
</template>

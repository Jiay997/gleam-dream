<!--
  文件级注释：全局 Toast 浮层宿主。
  渲染 useToast 的单例队列：固定于视口顶部右侧，按类型着色，入场右滑 / 出场淡出。
-->
<script setup lang="ts">
/**
 * ToastHost 轻提示宿主（挂载于 App 根层级，全局唯一实例）。
 * 无 props / emit；数据源为 useToast() 单例。
 */
import { useToast } from '@/composables/useToast'
import type { ToastType } from '@/composables/useToast'

const { items, dismiss } = useToast()

/** 类型 → 样式映射（浅底彩色文字走 -strong 档以保证 AA 对比度） */
const TYPE_STYLES: Record<ToastType, { container: string; icon: string }> = {
  success: { container: 'border-success-border bg-success-bg text-success-strong', icon: '✓' },
  error: { container: 'border-danger-border bg-danger-bg text-danger-strong', icon: '✕' },
  warning: { container: 'border-warning-border bg-warning-bg text-warning-strong', icon: '!' },
  info: { container: 'border-info-border bg-info-bg text-info-strong', icon: 'i' },
}
</script>

<template>
  <Teleport to="body">
    <div class="pointer-events-none fixed right-4 top-[calc(var(--gd-topbar-h)+12px)] z-toast flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2" role="status" aria-live="polite">
      <TransitionGroup name="gd-toast">
        <div
          v-for="item in items"
          :key="item.id"
          class="pointer-events-auto flex items-start gap-2.5 rounded-lg border px-4 py-3 shadow-gd-3"
          :class="TYPE_STYLES[item.type].container"
          @click="dismiss(item.id)"
        >
          <span
            class="mt-0.5 flex h-[18px] w-[18px] shrink-0 items-center justify-center rounded-full border border-current text-micro"
            aria-hidden="true"
          >
            {{ TYPE_STYLES[item.type].icon }}
          </span>
          <p class="flex-1 text-sm leading-snug">{{ item.message }}</p>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.gd-toast-enter-active {
  transition: opacity var(--gd-duration-normal) var(--gd-ease-out),
    transform var(--gd-duration-normal) var(--gd-ease-out);
}

.gd-toast-leave-active {
  transition: opacity var(--gd-duration-fast) ease-in,
    transform var(--gd-duration-fast) ease-in;
}

.gd-toast-enter-from {
  opacity: 0;
  transform: translateX(24px);
}

.gd-toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}
</style>

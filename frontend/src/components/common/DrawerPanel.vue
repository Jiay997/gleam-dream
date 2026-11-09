<!--
  文件级注释：右侧抽屉面板组件。
  宽度 480px（标准）/ 640px（超宽）；遮罩 rgba(27,33,48,0.32)；
  面板 300ms 右滑入场；支持 ESC 关闭。
-->
<script setup lang="ts">
/**
 * DrawerPanel 右侧抽屉。
 * props：
 * - modelValue：显示状态（v-model）；
 * - title：抽屉标题；
 * - width：宽度 480 / 640，默认 480。
 * emit：update:modelValue / close（等同关闭）。
 * slot：default 正文；footer 底部操作区。
 */
import { onBeforeUnmount, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 显示状态 */
    modelValue: boolean
    /** 抽屉标题 */
    title?: string
    /** 宽度（480 标准 / 640 超宽） */
    width?: 480 | 640
  }>(),
  { title: '', width: 480 },
)

const emit = defineEmits<{
  /** v-model 更新 */
  'update:modelValue': [value: boolean]
  /** 关闭事件 */
  close: []
}>()

/**
 * 关闭抽屉。
 * @returns 无
 */
function close(): void {
  emit('update:modelValue', false)
  emit('close')
}

/**
 * ESC 键关闭处理。
 * @param event 键盘事件
 * @returns 无
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') close()
}

// 打开时监听 ESC，关闭时移除
watch(
  () => props.modelValue,
  (open) => {
    if (open) {
      document.addEventListener('keydown', onKeydown)
    } else {
      document.removeEventListener('keydown', onKeydown)
    }
  },
)

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <Teleport to="body">
    <!-- 遮罩（方向 B：统一使用 --gd-overlay 令牌，收敛硬编码色值）
         暗场里单靠「压黑」分不出层级（画布本身只有 0.2%~0.5% 亮度），
         必须叠一层 blur，让背景退成「失焦的景」而不是「另一种黑」。 -->
    <Transition name="gd-overlay">
      <div v-if="modelValue" class="fixed inset-0 z-drawer bg-overlay backdrop-blur-[8px]" @click="close" />
    </Transition>
    <!-- 面板：冰玻璃面（--gd-surface-glass + backdrop-blur），左缘一道 1px 亮边说明「这是贴在画布右缘的另一层」 -->
    <Transition name="gd-drawer">
      <aside
        v-if="modelValue"
        class="fixed inset-y-0 right-0 z-drawer flex flex-col border-l border-[rgba(255,255,255,0.10)] bg-[var(--gd-surface-glass)] shadow-gd-3 backdrop-blur-[16px]"
        :style="{ width: `${width}px`, maxWidth: '100vw' }"
        role="dialog"
        aria-modal="true"
      >
        <header class="flex h-14 shrink-0 items-center justify-between border-b border-hairline px-5">
          <h3 class="text-title text-neutral-900">{{ title }}</h3>
          <button type="button" class="rounded-md p-1.5 text-neutral-400 transition-colors duration-gd-fast hover:bg-surface-sunken hover:text-neutral-600" aria-label="关闭抽屉" @click="close">
            <svg class="h-[18px] w-[18px]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M6 18L18 6M6 6l12 12" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </header>
        <div class="flex-1 overflow-y-auto p-5">
          <slot />
        </div>
        <footer v-if="$slots.footer" class="shrink-0 border-t border-hairline px-5 py-4">
          <slot name="footer" />
        </footer>
      </aside>
    </Transition>
  </Teleport>
</template>

<style scoped>
.gd-overlay-enter-active,
.gd-overlay-leave-active {
  transition: opacity var(--gd-duration-normal) ease;
}

.gd-overlay-enter-from,
.gd-overlay-leave-to {
  opacity: 0;
}

.gd-drawer-enter-active {
  transition: transform var(--gd-duration-slow) var(--gd-ease-out);
}

.gd-drawer-leave-active {
  transition: transform var(--gd-duration-slow) var(--gd-ease-in-out);
}

.gd-drawer-enter-from,
.gd-drawer-leave-to {
  transform: translateX(100%);
}
</style>

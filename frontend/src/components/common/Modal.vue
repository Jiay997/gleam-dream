<!--
  文件级注释：弹窗基座（Modal）。
  方向 B 变更点：把原先散落在 5 处手写 Teleport 弹窗的公共规格收敛为一个基座：
  - 遮罩统一使用 --gd-overlay 令牌（原 rgba(27,33,48,0.32) 硬编码 8 处）；
  - 面板直接用设计系统原生的 .gd-modal__panel（panel 半透底 + 顶部白光渐层 + 1px 亮描边
    + 顶边 1px 光谱线 + --gd-elev-overlay 投影），与 aurora 的弹层配方逐条对齐；
  - 进出场过渡（遮罩淡入 + 面板淡入上移 6px），尊重 prefers-reduced-motion；
  - 新增焦点陷阱：打开时焦点移入面板，Tab 在面板内循环，Esc 关闭，关闭后焦点归还原元素；
  - 滚动锁定，避免背景跟随滚动。
  props：modelValue / title / maxWidth / closeOnOverlay / showHeader。
  slots：default 正文；footer 底部操作区。
-->
<script setup lang="ts">
/**
 * Modal 弹窗基座。
 * props：
 * - modelValue：显示状态（v-model）；
 * - title：可选标题（传入则渲染头部，可被 header 插槽覆盖）；
 * - maxWidth：面板最大宽度（px），默认 420；
 * - closeOnOverlay：点击遮罩是否关闭，默认 true。
 * emit：update:modelValue / close。
 * slot：default 正文；header 自定义头部；footer 底部操作区（不渲染则不显示底栏）。
 */
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 显示状态 */
    modelValue: boolean
    /** 标题（可选） */
    title?: string
    /** 面板最大宽度（px） */
    maxWidth?: number
    /** 点击遮罩是否关闭 */
    closeOnOverlay?: boolean
  }>(),
  { title: '', maxWidth: 420, closeOnOverlay: true },
)

const emit = defineEmits<{
  /** v-model 更新 */
  'update:modelValue': [value: boolean]
  /** 关闭事件 */
  close: []
}>()

/** 模块级自增序号：生成实例级唯一标题元素 id（aria-labelledby 引用） */
let modalSeq = 0

/** 面板元素引用（焦点陷阱与初始聚焦目标） */
const panelRef = ref<HTMLDivElement | null>(null)
/** 标题元素 id（有 title 时用于 aria-labelledby，保证 AX 树命名稳定可达） */
const titleId = `gd-modal-title-${++modalSeq}`
/** 打开前的活动元素（关闭后归还焦点） */
let lastActive: HTMLElement | null = null
/** 打开前的 body 溢出值（滚动锁定） */
let lastOverflow = ''

/**
 * 关闭弹窗并广播事件。
 * @returns 无
 */
function close(): void {
  emit('update:modelValue', false)
  emit('close')
}

/**
 * 遮罩点击：按 closeOnOverlay 决定是否关闭。
 * @returns 无
 */
function onOverlayClick(): void {
  if (props.closeOnOverlay) close()
}

/**
 * 焦点陷阱：Tab / Shift+Tab 在面板内循环，Esc 关闭。
 * @param event 键盘事件
 * @returns 无
 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    event.stopPropagation()
    close()
    return
  }
  if (event.key !== 'Tab' || !panelRef.value) return
  const focusables = panelRef.value.querySelectorAll<HTMLElement>(
    'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )
  if (focusables.length === 0) {
    event.preventDefault()
    panelRef.value.focus()
    return
  }
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  const active = document.activeElement as HTMLElement | null
  if (event.shiftKey && (active === first || active === panelRef.value)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && active === last) {
    event.preventDefault()
    first.focus()
  }
}

// 打开：记录来源元素、锁定滚动、焦点移入面板；关闭：还原焦点与滚动
watch(
  () => props.modelValue,
  async (open) => {
    if (open) {
      lastActive = document.activeElement as HTMLElement | null
      lastOverflow = document.body.style.overflow
      document.body.style.overflow = 'hidden'
      await nextTick()
      panelRef.value?.focus()
    } else {
      document.body.style.overflow = lastOverflow
      lastActive?.focus?.()
    }
  },
)

onBeforeUnmount(() => {
  document.body.style.overflow = lastOverflow
})
</script>

<template>
  <Teleport to="body">
    <Transition name="gd-modal">
      <div
        v-if="modelValue"
        class="fixed inset-0 z-modal flex items-center justify-center bg-overlay p-4 backdrop-blur-[8px]"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="title ? titleId : undefined"
        :aria-label="title ? undefined : '对话框'"
        @click.self="onOverlayClick"
        @keydown="onKeydown"
      >
        <!-- 面板直接用设计系统的 .gd-modal__panel（aurora.css 5.8 节）：
             半透明 panel 底 + 顶部 6% 白光渐层 + 1px 亮描边 + 顶边 1px 光谱线。
             暗场里「把背景压黑」几乎看不出来（画布本来就在 0.2%~0.5% 亮度），
             真正让弹窗浮起来的是遮罩的 blur + 面板自身的描边与顶光，这三点缺一不可。 -->
        <div
          ref="panelRef"
          tabindex="-1"
          class="gd-modal__panel flex max-h-[calc(100dvh-2rem)] w-full flex-col outline-none"
          :style="{ maxWidth: `${maxWidth}px` }"
        >
          <!-- 头部（标题或自定义插槽）：固定不滚动 -->
          <header v-if="title || $slots.header" class="flex shrink-0 items-start justify-between gap-4 px-6 pb-1 pt-6">
            <slot name="header">
              <h3 :id="titleId" class="text-title text-neutral-900">{{ title }}</h3>
            </slot>
          </header>

          <!-- 正文：超高时在面板内部滚动，保证首尾字段与操作按钮始终可达 -->
          <div class="min-h-0 flex-1 overflow-y-auto px-6 pb-6 pt-4">
            <slot />
          </div>

          <!-- 底部操作区：固定不滚动 -->
          <footer v-if="$slots.footer" class="flex shrink-0 justify-end gap-2 border-t border-hairline px-6 py-4">
            <slot name="footer" />
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.gd-modal-enter-active,
.gd-modal-leave-active {
  transition: opacity var(--gd-duration-normal) var(--gd-ease-out);
}

.gd-modal-enter-active > div,
.gd-modal-leave-active > div {
  transition: opacity var(--gd-duration-normal) var(--gd-ease-out),
    transform var(--gd-duration-normal) var(--gd-ease-out);
}

.gd-modal-enter-from,
.gd-modal-leave-to {
  opacity: 0;
}

.gd-modal-enter-from > div,
.gd-modal-leave-to > div {
  opacity: 0;
  transform: translateY(6px) scale(0.985);
}
</style>

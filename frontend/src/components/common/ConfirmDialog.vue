<!--
  文件级注释：确认弹窗组件（方向 B：改为基于 Modal 基座实现）。
  三种模式：normal 普通确认 / danger 危险操作（需输入 confirmText 解锁）/
  strong 强输入确认（同 danger，视觉更强警示）。支持 ESC / 遮罩关闭与异步 loading。
  对外 props / emits 与旧版完全一致，调用方无需改动。
-->
<script setup lang="ts">
/**
 * ConfirmDialog 确认弹窗。
 * props：
 * - modelValue：显示状态（v-model）；
 * - title：弹窗标题；
 * - content：说明文案；
 * - variant：normal/danger/strong，默认 normal；
 * - confirmText：danger/strong 模式下要求用户输入的目标文本；
 * - confirmLabel：确认按钮文案，默认"确认"；
 * - cancelLabel：取消按钮文案，默认"取消"；
 * - loading：确认请求进行中。
 * emit：update:modelValue / confirm（校验通过后触发）/ cancel。
 * slot：default —— 自定义正文（优先于 content）。
 */
import { computed, ref, watch } from 'vue'

import AppButton from './AppButton.vue'
import Modal from './Modal.vue'

const props = withDefaults(
  defineProps<{
    /** 显示状态 */
    modelValue: boolean
    /** 标题 */
    title: string
    /** 说明文案 */
    content?: string
    /** 弹窗模式 */
    variant?: 'normal' | 'danger' | 'strong'
    /** 需输入的确认文本 */
    confirmText?: string
    /** 确认按钮文案 */
    confirmLabel?: string
    /** 取消按钮文案 */
    cancelLabel?: string
    /** 确认请求进行中 */
    loading?: boolean
  }>(),
  {
    content: '',
    variant: 'normal',
    confirmText: '',
    confirmLabel: '确认',
    cancelLabel: '取消',
    loading: false,
  },
)

const emit = defineEmits<{
  /** v-model 更新 */
  'update:modelValue': [value: boolean]
  /** 确认（校验通过后触发） */
  confirm: []
  /** 取消 */
  cancel: []
}>()

/** 用户输入的确认文本 */
const typed = ref('')

// 每次打开重置输入
watch(
  () => props.modelValue,
  (open) => {
    if (open) typed.value = ''
  },
)

/** 是否需要输入确认文本 */
const requiresInput = computed(() => props.variant === 'danger' || props.variant === 'strong')

/** 确认按钮是否可用 */
const canConfirm = computed(() => !requiresInput.value || typed.value === props.confirmText)

/**
 * 关闭弹窗并触发 cancel。
 * @returns 无
 */
function handleCancel(): void {
  emit('update:modelValue', false)
  emit('cancel')
}

/**
 * 点击确认：校验通过才对外触发。
 * @returns 无
 */
function handleConfirm(): void {
  if (!canConfirm.value || props.loading) return
  emit('confirm')
}
</script>

<template>
  <Modal :model-value="modelValue" :max-width="420" @update:model-value="emit('update:modelValue', $event)" @close="emit('cancel')">
    <!-- 头部：危险模式带警示图标 -->
    <template #header>
      <div class="flex items-start gap-3">
        <span
          v-if="variant !== 'normal'"
          class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-danger-bg text-danger-strong"
          aria-hidden="true"
        >
          <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path
              d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
        </span>
        <h3 class="pt-1 text-title text-neutral-900">{{ title }}</h3>
      </div>
    </template>

    <!-- 正文 -->
    <div class="text-sm leading-relaxed text-neutral-600">
      <slot>{{ content }}</slot>
    </div>

    <!-- 危险 / 强输入模式：输入确认文本解锁 -->
    <div v-if="requiresInput" class="mt-4">
      <label class="mb-2 block text-caption text-neutral-500">
        请输入 <span class="gd-num font-semibold text-danger-strong">{{ confirmText }}</span> 以确认操作
      </label>
      <input
        v-model="typed"
        type="text"
        class="gd-input"
        :class="typed && typed !== confirmText ? 'border-danger-border' : ''"
        autocomplete="off"
      />
    </div>

    <!-- 底部操作区 -->
    <template #footer>
      <AppButton variant="secondary" size="md" :disabled="loading" @click="handleCancel">{{ cancelLabel }}</AppButton>
      <AppButton :variant="variant === 'normal' ? 'primary' : 'danger'" size="md" :loading="loading" :disabled="!canConfirm" @click="handleConfirm">
        {{ confirmLabel }}
      </AppButton>
    </template>
  </Modal>
</template>

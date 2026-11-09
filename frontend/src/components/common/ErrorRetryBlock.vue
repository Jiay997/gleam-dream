<!--
  文件级注释：错误重试块组件。
  数据加载失败时的兜底展示：错误标题 + 描述 + 重试按钮。
-->
<script setup lang="ts">
/**
 * ErrorRetryBlock 错误重试。
 * props：
 * - message：错误标题，默认"加载失败"；
 * - description：错误补充描述。
 * emit：retry —— 点击重试按钮。
 */
import AppButton from './AppButton.vue'

withDefaults(
  defineProps<{
    /** 错误标题 */
    message?: string
    /** 错误描述 */
    description?: string
  }>(),
  { message: '加载失败', description: '' },
)

const emit = defineEmits<{
  /** 点击重试 */
  retry: []
}>()
</script>

<template>
  <div class="flex flex-col items-center justify-center rounded-lg border border-danger-border bg-danger-bg py-12 text-center">
    <svg class="h-10 w-10 text-danger-strong" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
      <path
        d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    </svg>
    <p class="mt-4 text-sm font-medium text-neutral-800">{{ message }}</p>
    <p v-if="description" class="mt-1.5 max-w-sm text-caption leading-relaxed text-neutral-500">{{ description }}</p>
    <AppButton class="mt-5" variant="secondary" size="sm" @click="emit('retry')">重试</AppButton>
  </div>
</template>

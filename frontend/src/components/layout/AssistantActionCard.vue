<!--
  文件级注释：AI 创作助手动作确认卡片。
  展示服务端返回的动作预览（标题/摘要/参数可读摘要/风险），
  写操作必须由用户点击「确认执行」后才会调用后端（HITL）。
-->
<script setup lang="ts">
/**
 * AssistantActionCard 动作确认卡片。
 * props：
 * - action：动作快照（服务端已清洗，不含原始参数）；
 * - executing：是否正在执行。
 * emit：
 * - execute(actionId)：确认执行；
 * - dismiss(actionId)：本地忽略。
 */
import { computed } from 'vue'

import AppButton from '@/components/common/AppButton.vue'
import type { AssistantAction } from '@/api/assistant'

const props = defineProps<{
  /** 动作快照 */
  action: AssistantAction
  /** 是否正在执行 */
  executing: boolean
}>()

const emit = defineEmits<{
  /** 确认执行动作 */
  execute: [actionId: number]
  /** 本地忽略动作 */
  dismiss: [actionId: number]
}>()

/** 状态 → 文案与配色 */
const STATUS_META: Record<string, { label: string; className: string }> = {
  pending: { label: '待确认', className: 'border-primary-200 bg-primary-50' },
  executing: { label: '执行中', className: 'border-hairline bg-surface-sunken' },
  accepted: { label: '已受理', className: 'border-info-border bg-info-bg' },
  succeeded: { label: '已完成', className: 'border-success-border bg-success-bg' },
  failed: { label: '失败', className: 'border-danger-border bg-danger-bg' },
  cancelled: { label: '已取消', className: 'border-hairline bg-surface-sunken' },
  expired: { label: '已过期', className: 'border-hairline bg-surface-sunken' },
}

/** 当前动作状态元信息 */
const meta = computed(() => STATUS_META[props.action.status] ?? STATUS_META.pending)
/** 是否可确认 */
const canExecute = computed(() => props.action.status === 'pending' && !props.executing)

/**
 * 点击确认执行。
 * @returns 无
 */
function onExecute(): void {
  if (canExecute.value) emit('execute', props.action.id)
}

/**
 * 点击忽略（仅本地隐藏）。
 * @returns 无
 */
function onDismiss(): void {
  emit('dismiss', props.action.id)
}
</script>

<template>
  <div class="mt-3 rounded-lg border p-3" :class="meta.className">
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0">
        <p class="text-caption font-semibold text-neutral-800">{{ action.title }}</p>
        <p v-if="action.summary" class="mt-1 text-caption leading-relaxed text-neutral-500">
          {{ action.summary }}
        </p>
        <p v-if="action.detail" class="mt-1 break-words text-micro text-neutral-400">
          {{ action.detail }}
        </p>
      </div>
      <span class="shrink-0 rounded-full border border-hairline px-2 py-0.5 text-micro text-neutral-500">
        {{ meta.label }}
      </span>
    </div>

    <p v-if="action.risk" class="mt-2 rounded-md bg-warning-bg px-2 py-1 text-micro leading-relaxed text-warning-strong">
      风险：{{ action.risk }}
    </p>
    <p v-if="action.status === 'failed' && action.error_message" class="mt-2 break-words text-micro leading-relaxed text-danger-strong">
      {{ action.error_message }}
    </p>
    <p v-if="action.status === 'expired'" class="mt-2 text-micro text-neutral-400">
      动作已过期，请重新向助手提问。
    </p>
    <p v-if="action.status === 'accepted'" class="mt-2 text-micro text-neutral-400">
      长任务已受理，可在对应任务页查看进度。
    </p>

    <div v-if="action.status === 'pending' || executing" class="mt-3 flex items-center gap-2">
      <AppButton size="sm" :loading="executing" :disabled="!canExecute" @click="onExecute">
        确认执行
      </AppButton>
      <AppButton size="sm" variant="ghost" :disabled="executing" @click="onDismiss">
        忽略
      </AppButton>
    </div>
  </div>
</template>

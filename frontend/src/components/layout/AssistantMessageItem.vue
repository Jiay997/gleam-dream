<!--
  文件级注释：AI 创作助手消息条目。
  区分用户气泡、助手回答（Agent 标签 + 正文 + 建议 + 风险 + 动作卡片）
  与系统回执（动作执行结果），统一使用纯文本保留换行渲染。
-->
<script setup lang="ts">
/**
 * AssistantMessageItem 消息条目。
 * props：
 * - message：消息（用户 / 助手 / 系统）；
 * - executingIds：正在执行的动作 ID 列表。
 * emit：
 * - execute(actionId)：确认执行动作；
 * - dismiss(actionId)：本地忽略动作；
 * - suggestion(text)：点击建议填入输入框。
 */
import { computed } from 'vue'

import AssistantActionCard from './AssistantActionCard.vue'
import type { AssistantMessage } from '@/api/assistant'

const props = defineProps<{
  /** 消息内容 */
  message: AssistantMessage
  /** 正在执行的动作 ID */
  executingIds: number[]
}>()

const emit = defineEmits<{
  /** 确认执行动作 */
  execute: [actionId: number]
  /** 忽略动作 */
  dismiss: [actionId: number]
  /** 采纳建议（填入输入框） */
  suggestion: [text: string]
}>()

/** 自检字段（后端为任意 JSONB） */
const selfCheck = computed<Record<string, unknown>>(() =>
  props.message.self_check && typeof props.message.self_check === 'object'
    ? (props.message.self_check as Record<string, unknown>)
    : {},
)

/** 后续建议 */
const suggestions = computed<string[]>(() => {
  const raw = selfCheck.value.suggestions
  return Array.isArray(raw) ? raw.map(String).filter(Boolean).slice(0, 3) : []
})

/** 风险与质检问题 */
const riskNotes = computed<string[]>(() => {
  const items: string[] = []
  const issues = selfCheck.value.issues
  const risks = selfCheck.value.risk_notes
  if (Array.isArray(issues)) items.push(...issues.map(String))
  if (Array.isArray(risks)) items.push(...risks.map(String))
  return Array.from(new Set(items.filter(Boolean))).slice(0, 4)
})

/** 是否经过自检修正 */
const corrected = computed(() => selfCheck.value.corrected === true)

/** 是否为系统回执 */
const isSystem = computed(() => props.message.role === 'system')
/** 是否为用户消息 */
const isUser = computed(() => props.message.role === 'user')

/**
 * 点击建议。
 * @param text 建议文本
 * @returns 无
 */
function onSuggestion(text: string): void {
  emit('suggestion', text)
}
</script>

<template>
  <div v-if="isSystem" class="my-2 flex justify-center">
    <p class="max-w-full rounded-full border border-hairline bg-surface-sunken px-3 py-1 text-center text-micro leading-relaxed text-neutral-500">
      {{ message.content }}
    </p>
  </div>

  <div v-else-if="isUser" class="my-2 flex justify-end">
    <div class="max-w-[86%] rounded-lg rounded-br-sm border border-primary-200 bg-primary-50 px-3 py-2">
      <p class="whitespace-pre-wrap break-words text-sm leading-relaxed text-neutral-800">{{ message.content }}</p>
    </div>
  </div>

  <div v-else class="my-2">
    <div class="rounded-lg rounded-bl-sm border border-hairline bg-surface-raised/80 px-3 py-2">
      <div class="mb-1.5 flex items-center gap-2">
        <span class="rounded-full bg-primary-100 px-2 py-0.5 text-micro font-medium text-primary-300">
          {{ message.agent_label || '创作助手' }}
        </span>
        <span v-if="message.status === 'degraded'" class="text-micro text-warning-strong">模型降级</span>
        <span v-if="corrected" class="text-micro text-success-strong">已自检修正</span>
      </div>

      <p class="whitespace-pre-wrap break-words text-sm leading-relaxed text-neutral-700">{{ message.content }}</p>

      <ul v-if="suggestions.length" class="mt-2 space-y-1">
        <li v-for="(item, index) in suggestions" :key="index">
          <button
            type="button"
            class="w-full rounded-md border border-hairline bg-surface-sunken px-2 py-1 text-left text-caption leading-relaxed text-neutral-500 transition-colors hover:border-primary-200 hover:text-primary-300"
            @click="onSuggestion(item)"
          >
            {{ item }}
          </button>
        </li>
      </ul>

      <div v-if="riskNotes.length" class="mt-2 rounded-md border border-warning-border bg-warning-bg px-2 py-1.5">
        <p v-for="(item, index) in riskNotes" :key="index" class="text-micro leading-relaxed text-warning-strong">
          · {{ item }}
        </p>
      </div>

      <AssistantActionCard
        v-for="action in message.actions"
        :key="action.id"
        :action="action"
        :executing="executingIds.includes(action.id)"
        @execute="emit('execute', $event)"
        @dismiss="emit('dismiss', $event)"
      />
    </div>
  </div>
</template>

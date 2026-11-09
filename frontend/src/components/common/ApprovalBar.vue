<!--
  文件级注释：审批操作栏组件。
  提供 通过 / 驳回 / 修改 三种动作；驳回时展开反馈输入框（≥10 字校验）；
  支持通过 extra 插槽扩展自定义操作。
-->
<script setup lang="ts">
/**
 * ApprovalBar 审批操作栏。
 * props：
 * - loading：请求进行中（禁用全部按钮）；
 * - approveDisabled：通过按钮禁用；
 * - rejectDisabled：驳回按钮禁用；
 * - approveText / rejectText / editText：按钮文案，默认 通过/驳回/修改；
 * - showEdit：是否显示"修改"按钮，默认 true。
 * emit：approve（通过）；reject(comment)（驳回，携带 ≥10 字意见）；edit（修改）。
 * slot：extra —— 追加自定义操作区。
 */
import { computed, ref } from 'vue'

import AppButton from './AppButton.vue'

const props = withDefaults(
  defineProps<{
    /** 请求进行中 */
    loading?: boolean
    /** 通过按钮禁用 */
    approveDisabled?: boolean
    /** 驳回按钮禁用 */
    rejectDisabled?: boolean
    /** 通过按钮文案 */
    approveText?: string
    /** 驳回按钮文案 */
    rejectText?: string
    /** 修改按钮文案 */
    editText?: string
    /** 是否显示修改按钮 */
    showEdit?: boolean
  }>(),
  {
    loading: false,
    approveDisabled: false,
    rejectDisabled: false,
    approveText: '通过',
    rejectText: '驳回',
    editText: '修改',
    showEdit: true,
  },
)

const emit = defineEmits<{
  /** 通过 */
  approve: []
  /** 驳回（携带意见，≥10 字） */
  reject: [comment: string]
  /** 修改 */
  edit: []
}>()

/** 驳回意见输入是否展开 */
const rejectInputVisible = ref(false)
/** 驳回意见 */
const comment = ref('')
/** 校验错误提示 */
const errorMessage = ref('')

/** 驳回意见是否有效（≥10 字） */
const isCommentValid = computed(() => comment.value.trim().length >= 10)

/**
 * 点击通过。
 * @returns 无
 */
function handleApprove(): void {
  if (props.loading || props.approveDisabled) return
  emit('approve')
}

/**
 * 展开驳回输入框。
 * @returns 无
 */
function openRejectInput(): void {
  if (props.loading || props.rejectDisabled) return
  rejectInputVisible.value = true
  errorMessage.value = ''
}

/**
 * 提交驳回：校验通过后触发 reject。
 * @returns 无
 */
function confirmReject(): void {
  if (!isCommentValid.value) {
    errorMessage.value = '请填写至少 10 个字的驳回原因，便于创作侧修改'
    return
  }
  emit('reject', comment.value.trim())
  rejectInputVisible.value = false
  comment.value = ''
  errorMessage.value = ''
}

/**
 * 取消驳回输入。
 * @returns 无
 */
function cancelReject(): void {
  rejectInputVisible.value = false
  comment.value = ''
  errorMessage.value = ''
}
</script>

<template>
  <div>
    <!-- 驳回意见输入态 -->
    <div v-if="rejectInputVisible" class="rounded-lg border border-danger-border bg-danger-bg p-4">
      <label class="mb-2 block text-xs font-medium text-neutral-700" for="approval-reject-comment">驳回原因（必填，≥10 字）</label>
      <textarea
        id="approval-reject-comment"
        v-model="comment"
        rows="3"
        class="gd-input resize-none"
        placeholder="请描述问题与修改建议，例如：角色表情与剧本情绪不符，请重新生成第 3 版…"
      />
      <div class="mt-2 flex items-center justify-between">
        <span class="text-xs" :class="errorMessage ? 'text-danger-strong' : 'text-neutral-400'">
          {{ errorMessage || `${comment.trim().length}/10 字` }}
        </span>
        <div class="flex gap-2">
          <AppButton variant="ghost" size="sm" :disabled="loading" @click="cancelReject">取消</AppButton>
          <AppButton variant="danger" size="sm" :loading="loading" @click="confirmReject">提交驳回</AppButton>
        </div>
      </div>
    </div>

    <!-- 常规操作态：底部操作栏，高度对齐 --gd-footer-bar-h（72px） -->
    <div v-else class="flex h-footer-bar items-center gap-2.5">
      <AppButton variant="primary" size="md" :loading="loading" :disabled="approveDisabled" @click="handleApprove">
        <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        {{ approveText }}
      </AppButton>
      <AppButton variant="danger" size="md" :loading="loading" :disabled="rejectDisabled" @click="openRejectInput">{{ rejectText }}</AppButton>
      <AppButton v-if="showEdit" variant="secondary" size="md" :disabled="loading" @click="emit('edit')">{{ editText }}</AppButton>
      <slot name="extra" />
    </div>
  </div>
</template>

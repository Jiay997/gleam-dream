<!--
  文件级注释：费用预估弹窗。
  展示预估消耗积分与当前余额对比；余额不足时内嵌警示与"去充值"引导；
  确认动作仅在余额充足（或已充值返回）时可用。
-->
<script setup lang="ts">
/**
 * CostEstimateDialog 费用预估弹窗。
 * props：
 * - modelValue：显示状态（v-model）；
 * - estimatedCredits：预估消耗积分；
 * - title：弹窗标题，默认"消耗预估"；
 * - description：补充说明；
 * - loading：确认请求进行中。
 * emit：update:modelValue / confirm（余额充足时点击"开始生成"触发）。
 * slot：detail —— 可选明细区（渲染在费用明细之下，如逐镜成本列表）。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { useBillingStore } from '@/stores/billing'
import AppButton from './AppButton.vue'
import CreditAmountText from './CreditAmountText.vue'
import Modal from './Modal.vue'

const props = withDefaults(
  defineProps<{
    /** 显示状态 */
    modelValue: boolean
    /** 预估消耗积分 */
    estimatedCredits: number
    /** 弹窗标题 */
    title?: string
    /** 补充说明 */
    description?: string
    /** 确认请求进行中 */
    loading?: boolean
  }>(),
  { title: '消耗预估', description: '', loading: false },
)

const emit = defineEmits<{
  /** v-model 更新 */
  'update:modelValue': [value: boolean]
  /** 确认开始生成 */
  confirm: []
}>()

const router = useRouter()
const billingStore = useBillingStore()

/** 当前余额 */
const balance = computed(() => billingStore.balance)

/** 余额是否不足 */
const insufficient = computed(() => props.estimatedCredits > balance.value)

/** 扣除后余额 */
const afterBalance = computed(() => balance.value - props.estimatedCredits)

/**
 * 关闭弹窗。
 * @returns 无
 */
function close(): void {
  emit('update:modelValue', false)
}

/**
 * 前往充值（关闭弹窗跳转计费中心）。
 * @returns 无
 */
function goRecharge(): void {
  close()
  void router.push('/billing')
}

/**
 * 确认开始生成。
 * @returns 无
 */
function handleConfirm(): void {
  if (insufficient.value || props.loading) return
  emit('confirm')
}
</script>

<template>
  <Modal :model-value="modelValue" :max-width="420" @update:model-value="emit('update:modelValue', $event)">
    <template #header>
      <div>
        <h3 class="text-title text-neutral-900">{{ title }}</h3>
        <p v-if="description" class="mt-1 text-caption text-neutral-500">{{ description }}</p>
      </div>
    </template>

    <!-- 费用明细 -->
    <dl class="space-y-3 rounded-md bg-surface-canvas p-4 text-sm">
      <div class="flex items-center justify-between">
        <dt class="text-neutral-500">预估消耗</dt>
        <dd class="flex items-center gap-1">
          <CreditAmountText :amount="estimatedCredits" />
          <span class="text-neutral-400">积分</span>
        </dd>
      </div>
      <div class="flex items-center justify-between">
        <dt class="text-neutral-500">当前余额</dt>
        <dd class="flex items-center gap-1">
          <CreditAmountText :amount="balance" />
          <span class="text-neutral-400">积分</span>
        </dd>
      </div>
      <div class="flex items-center justify-between border-t border-hairline pt-3">
        <dt class="font-medium text-neutral-700">扣除后余额</dt>
        <dd class="flex items-center gap-1">
          <CreditAmountText :amount="afterBalance" :colored="false" :class="insufficient ? 'text-danger-strong' : ''" />
          <span class="text-neutral-400">积分</span>
        </dd>
      </div>
    </dl>

    <!-- 余额不足内嵌引导 -->
    <div
      v-if="insufficient"
      class="mt-4 flex items-center justify-between rounded-md border border-warning-border bg-warning-bg px-4 py-3"
    >
      <p class="text-caption text-warning-strong">
        余额不足，预计还差 <span class="gd-num">{{ (estimatedCredits - balance).toLocaleString('zh-CN') }}</span> 积分
      </p>
      <AppButton variant="text" size="sm" @click="goRecharge">去充值 →</AppButton>
    </div>

    <!-- 可选明细区（逐镜成本等自定义内容） -->
    <div v-if="$slots.detail" class="mt-4">
      <slot name="detail" />
    </div>

    <template #footer>
      <AppButton variant="secondary" size="md" :disabled="loading" @click="close">取消</AppButton>
      <AppButton variant="primary" size="md" :loading="loading" :disabled="insufficient" @click="handleConfirm">
        {{ insufficient ? '余额不足' : '开始生成' }}
      </AppButton>
    </template>
  </Modal>
</template>

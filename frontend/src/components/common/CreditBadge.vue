<!--
  文件级注释：顶栏积分徽章。
  展示当前积分余额；余额低于基准 20%（或绝对值低于 100）进入警示态；
  点击跳转计费中心。余额未通过 props 提供时自动读取 billing store。
-->
<script setup lang="ts">
/**
 * CreditBadge 积分徽章。
 * props：
 * - balance：余额（缺省读 billing store）；
 * - baseline：基准额度（如累计充值 / 项目预算），低于其 20% 时警示。
 * emit：无（点击内部跳转 /billing）。
 */
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'

import { useBillingStore } from '@/stores/billing'

const props = defineProps<{
  /** 余额（缺省读 store） */
  balance?: number
  /** 警示基准额度 */
  baseline?: number
}>()

const router = useRouter()
const billingStore = useBillingStore()

/** 展示余额 */
const displayBalance = computed(() => props.balance ?? billingStore.balance)

/**
 * 是否处于「余额未加载」中间态。
 * 修复（测试报告问题 4）：账户未拉取前 store 余额恒为 0，直接展示会让用户
 * 误以为积分被清零；此处以占位符替代 0，待账户返回后再显示真实值。
 */
const isLoading = computed(() => props.balance === undefined && billingStore.creditAccount === null)

/** 是否处于余额不足警示态（< 基准 20%，无基准时 < 100；加载中不告警） */
const isLowBalance = computed(() => {
  if (isLoading.value) return false
  if (props.baseline !== undefined && props.baseline > 0) {
    return displayBalance.value < props.baseline * 0.2
  }
  return displayBalance.value < 100
})

onMounted(() => {
  if (!props.balance && billingStore.creditAccount === null) {
    void billingStore.refresh().catch(() => undefined)
  }
})
</script>

<template>
  <div class="flex items-center gap-2">
    <!-- 冻结金额（效果图 .chip.plain：有冻结才显示，点击同样进入计费中心） -->
    <button
      v-if="billingStore.frozenAmount > 0"
      type="button"
      class="gd-num hidden h-7 items-center rounded-full bg-surface-sunken px-2.5 text-xs text-neutral-500 transition-colors duration-gd-fast hover:text-neutral-700 sm:inline-flex"
      title="进行中任务预扣积分，点击前往计费中心"
      @click="router.push('/billing')"
    >
      冻结 {{ billingStore.frozenAmount.toLocaleString('zh-CN') }}
    </button>
    <button
      type="button"
      class="gd-num inline-flex h-8 items-center gap-1.5 rounded-full px-2 text-sm font-medium transition-colors duration-gd-fast"
      :class="isLowBalance ? 'text-warning-strong hover:bg-warning-bg' : 'text-neutral-800 hover:bg-surface-sunken'"
      title="前往计费中心"
      @click="router.push('/billing')"
    >
      <!-- 金点：效果图顶栏积分的数据着色（暖金 = 成果与数据语义） -->
      <span
        class="h-1.5 w-1.5 shrink-0 rounded-full"
        :class="isLowBalance ? 'bg-warning' : 'bg-gold'"
        aria-hidden="true"
      />
      <svg v-if="isLowBalance" class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <path
          d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
      <span>{{ isLoading ? '—' : displayBalance.toLocaleString('zh-CN') }}</span>
      <span class="text-xs font-normal text-neutral-500">积分</span>
    </button>
  </div>
</template>

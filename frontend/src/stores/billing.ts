/**
 * 文件级注释：计费状态 store。
 * 维护积分账户（余额 / 冻结），顶栏 CreditBadge 与费用预估弹窗共用。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { getCreditAccount } from '@/api/billing'
import type { CreditAccount } from '@/api/types'

/**
 * 计费 store：积分账户与刷新动作。
 */
export const useBillingStore = defineStore('billing', () => {
  /** 积分账户（null 表示未加载） */
  const creditAccount = ref<CreditAccount | null>(null)
  /** 加载中标记 */
  const loading = ref(false)

  /** 可用余额（未加载时为 0；后端字段 balance_credits） */
  const balance = computed(() => creditAccount.value?.balance_credits ?? 0)

  /** 冻结金额（进行中任务预扣；后端字段 frozen_credits） */
  const frozenAmount = computed(() => creditAccount.value?.frozen_credits ?? 0)

  /**
   * 刷新积分账户。
   * @returns 最新积分账户
   */
  async function refresh(): Promise<CreditAccount> {
    loading.value = true
    try {
      creditAccount.value = await getCreditAccount()
      return creditAccount.value
    } finally {
      loading.value = false
    }
  }

  return { creditAccount, balance, frozenAmount, loading, refresh }
})

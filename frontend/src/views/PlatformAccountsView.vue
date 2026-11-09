<!--
  文件级注释：P19 · 平台授权管理（路由 /settings/platform-accounts，AppShell 布局，880px 单栏）。
  v7 纸上放映厅 Pro · 对照 F7-02（P19 平台授权管理）：
  - 页顶红色警示横幅（存在过期授权时）：「N 个平台授权已过期：Token 于 X 失效，新分发已被
    阻断，历史记录不受影响」+ 一键重新授权（品牌实底：这是修复动作，不是破坏动作）；
  - 三态平台卡：已授权（白纸卡）/ 临期（<7 天黄色警示底 + 「临期 · 剩 N 天」chip）/
    已过期（红调卡：失效时间红字 +「分发入口置灰」+ 一键重新授权品牌实底）/ 已解除（去授权）；
  - 授权卡列表：平台图标 / 账号名 / 状态徽章（active 绿 / expired 黄 / revoked 灲）/
    授权与到期时间 / 操作（续期=重新授权 mock / 解除授权 ConfirmDialog）。
  顶部 token 安全提示条（AES 加密存储说明）+「一键检查授权状态」按钮保留。
  空态引导「尚未授权任何平台」。
  备注：分发目标勾选 + 分发记录表（F7-02 主帧下部两卡）依赖具体项目上下文，
  分发动作已在 P18 导出分发中心落地，本页保持授权管理单一职责（见重构报告）。
-->
<script setup lang="ts">
/**
 * PlatformAccountsView 平台授权管理。
 * props：无。emits：无。
 * 依赖 export API（平台授权增删查）、useToast、ConfirmDialog。
 */
import { computed, onMounted, ref, type Component } from 'vue'

import { createPlatformAccount, deletePlatformAccount, listPlatformAccounts } from '@/api/export'
import type { PlatformAccountItem } from '@/api/export'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import { PLATFORM_OPTIONS, platformOption } from '@/constants/options'
import { formatRelativeTime, formatShortDate } from '@/utils/datetime'
import {
  PhLightning,
  PhLink,
  PhScissors,
  PhTiktokLogo,
  PhVideoCamera,
} from '@phosphor-icons/vue'

/** 平台 key → 矢量图标组件（替代 emoji，统一线性图标语言） */
const PLATFORM_ICONS: Record<string, Component> = {
  douyin: PhTiktokLogo,
  kuaishou: PhLightning,
  wechat_channels: PhVideoCamera,
  jianying: PhScissors,
}

/**
 * 查询平台矢量图标（未知平台回退链接图标）。
 * @param platform 平台 key
 * @returns 图标组件
 */
function platformIcon(platform: string): Component {
  return PLATFORM_ICONS[platform] ?? PhLink
}

/** Toast 提示 */
const toast = useToast()

/* ==================== 授权列表 ==================== */

/** 授权账号列表 */
const accounts = ref<PlatformAccountItem[]>([])
/** 加载中 */
const loading = ref(true)
/** 加载失败信息 */
const errorMsg = ref('')
/** 一键检查进行中 */
const checking = ref(false)
/** 最后检查时间（ISO 字符串，空串表示从未检查） */
const lastCheckedAt = ref('')

/**
 * 拉取授权账号列表（一键检查与刷新共用）。
 * @returns 无
 */
async function fetchAccounts(): Promise<void> {
  errorMsg.value = ''
  try {
    accounts.value = await listPlatformAccounts()
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '授权列表加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 一键检查授权状态：逐平台重新拉取授权列表并记录检查时间。
 * 沙箱后端未提供单账号查询端点，此处以全量列表刷新模拟逐项检查。
 * @returns 无
 */
async function checkAll(): Promise<void> {
  checking.value = true
  try {
    for (const _platform of PLATFORM_OPTIONS) {
      // 逐平台发起一次刷新请求，模拟授权状态巡检
      accounts.value = await listPlatformAccounts()
    }
    lastCheckedAt.value = new Date().toISOString()
    const activeCount = accounts.value.filter((item) => item.status === 'active').length
    toast.success(`检查完成：${activeCount} 个平台授权有效`)
  } catch {
    // 拦截器已 toast
  } finally {
    checking.value = false
  }
}

/* ==================== 状态标签（active 绿 / expired 黄 / revoked 中性） ==================== */

/**
 * 授权状态 → StatusTag 入参映射。
 * 授权状态（active/expired/revoked）不在业务五态枚举内，映射到 tone 相同的内置状态
 * 并覆盖展示文案，复用 StatusTag 的色板与冗余编码。
 * @param status 授权状态
 * @returns StatusTag 的 status 与 label
 */
function authStatusTag(status: string): { status: string; label: string } {
  const map: Record<string, { status: string; label: string }> = {
    active: { status: 'completed', label: '已授权' },
    expired: { status: 'pending', label: '已过期' },
    revoked: { status: 'cancelled', label: '已解除' },
  }
  return map[status] ?? { status: 'cancelled', label: status }
}

/**
 * 授权账号展示名（沙箱为 mock 平台账号标识）。
 * @param account 授权账号
 * @returns 账号名
 */
function accountName(account: PlatformAccountItem): string {
  return account.platform_user_id || `${platformOption(account.platform).label}创作账号`
}

/**
 * token 距到期是否不足 7 天（黄色警示条）。
 * @param account 授权账号
 * @returns 是否临期
 */
function expiringSoon(account: PlatformAccountItem): boolean {
  if (!account.token_expires_at || account.status !== 'active') return false
  const expires = new Date(account.token_expires_at).getTime()
  if (Number.isNaN(expires)) return false
  return expires - Date.now() < 7 * 24 * 60 * 60 * 1000
}

/**
 * token 剩余天数（向上取整，最小 0；临期 chip 文案）。
 * @param account 授权账号
 * @returns 剩余天数
 */
function daysLeft(account: PlatformAccountItem): number {
  if (!account.token_expires_at) return 0
  const expires = new Date(account.token_expires_at).getTime()
  if (Number.isNaN(expires)) return 0
  return Math.max(0, Math.ceil((expires - Date.now()) / (24 * 60 * 60 * 1000)))
}

/** 已过期授权账号列表（页顶红色警示横幅数据源，F7-02） */
const expiredAccounts = computed(() => accounts.value.filter((item) => item.status === 'expired'))

/* ==================== 续期 / 解除授权 ==================== */

/** 续期（重新授权）进行中的账号 ID */
const renewingId = ref<string | number>('')

/**
 * 续期授权：以同平台重新走 mock 授权流程。
 * @param account 授权账号
 * @returns 无
 */
async function renewAccount(account: PlatformAccountItem): Promise<void> {
  renewingId.value = account.id
  try {
    await createPlatformAccount({ platform: account.platform, mock_authorize: true })
    toast.success(`${platformOption(account.platform).label}授权已续期`)
    await fetchAccounts()
  } catch {
    // 拦截器已 toast
  } finally {
    renewingId.value = ''
  }
}

/** 解除授权目标账号（null 表示弹窗关闭） */
const revokeTarget = ref<PlatformAccountItem | null>(null)
/** 解除授权弹窗显示（computed 双向代理，供 ConfirmDialog v-model） */
const revokeDialogOpen = computed({
  get: () => revokeTarget.value !== null,
  set: (value: boolean) => {
    if (!value) revokeTarget.value = null
  },
})
/** 解除授权提交中 */
const revoking = ref(false)

/**
 * 确认解除授权（DELETE，撤销后平台侧 token 同步失效）。
 * @returns 无
 */
async function confirmRevoke(): Promise<void> {
  const target = revokeTarget.value
  if (!target) return
  revoking.value = true
  try {
    await deletePlatformAccount(target.id)
    toast.success(`${platformOption(target.platform).label}授权已解除`)
    revokeTarget.value = null
    await fetchAccounts()
  } catch {
    // 拦截器已 toast
  } finally {
    revoking.value = false
  }
}

// 挂载：首次拉取授权列表
onMounted(() => {
  void fetchAccounts()
})
</script>

<template>
  <div class="p-6 lg:p-8">
    <div class="mx-auto w-full max-w-content-lg">
      <!-- ============ 页头（档位与设置域其他页统一为 masthead） ============ -->
      <PageHeader title="平台授权管理">
        <template #actions>
          <AppButton variant="secondary" size="md" :loading="checking" @click="checkAll">一键检查授权状态</AppButton>
        </template>
      </PageHeader>

      <!-- ============ token 安全提示条 ============ -->
      <div class="mb-6 flex items-start gap-2.5 rounded-lg border border-info-border bg-info-bg px-4 py-3" role="note">
        <svg class="mt-0.5 h-4 w-4 shrink-0 text-info" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p class="text-xs leading-relaxed text-info">
          安全说明：平台授权 Token 全程加密存储（AES-256-GCM），服务端仅在分发发布时解密使用，任何界面都不会展示完整 Token；解除授权后将立即删除密文并通知平台吊销。
        </p>
      </div>

      <!-- ============ 页顶红色警示横幅（存在过期授权，F7-02） ============ -->
      <div
        v-if="expiredAccounts.length > 0"
        class="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-danger-border bg-danger-bg px-4 py-3"
        role="alert"
      >
        <svg class="h-4 w-4 shrink-0 text-danger" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M12 9v2m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p class="min-w-0 flex-1 text-sm leading-relaxed text-danger">
          <span class="font-semibold">{{ expiredAccounts.length }} 个平台授权已过期：</span>{{ platformOption(expiredAccounts[0].platform).label }} 的 Token 于
          <span class="gd-num font-semibold">{{ formatShortDate(expiredAccounts[0].token_expires_at ?? undefined) }}</span>
          失效，新分发已被阻断，历史记录不受影响。
        </p>
        <AppButton
          variant="primary"
          size="sm"
          class="ml-auto"
          :loading="renewingId === expiredAccounts[0].id"
          @click="renewAccount(expiredAccounts[0])"
        >
          一键重新授权
        </AppButton>
      </div>

      <!-- ============ 加载骨架 ============ -->
      <div v-if="loading" class="space-y-3">
        <LoadingSkeleton v-for="i in 3" :key="i" variant="card" />
      </div>

      <!-- ============ 加载失败 ============ -->
      <ErrorRetryBlock v-else-if="errorMsg" message="授权列表加载失败" :description="errorMsg" @retry="fetchAccounts" />

      <!-- ============ 空态引导（矢量图标，替代 emoji） ============ -->
      <div v-else-if="accounts.length === 0" class="gd-card-flat py-4">
        <EmptyState
          title="尚未授权任何平台"
          description="前往导出分发中心选择项目，在右侧「平台授权」卡片中完成首次授权后，即可在此统一管理"
        >
          <template #icon>
            <PhLink class="h-8 w-8 text-neutral-400" weight="light" aria-hidden="true" />
          </template>
        </EmptyState>
      </div>

      <!-- ============ 授权卡列表（三态：已授权白卡 / 临期黄调 / 过期红调） ============ -->
      <ul v-else class="space-y-3">
        <li
          v-for="account in accounts"
          :key="account.id"
          class="gd-card gd-card-interactive p-6"
          :class="
            account.status === 'expired'
              ? 'border-danger/30 bg-danger/5'
              : expiringSoon(account)
                ? 'border-warning-border bg-warning-bg/40'
                : ''
          "
        >
          <!-- 主信息行（窄屏时操作区自动换行到第二行并右对齐，避免账号名被压扁） -->
          <div class="flex flex-wrap items-center gap-x-4 gap-y-3">
            <span
              class="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl border border-hairline"
              :class="account.status === 'expired' ? 'border-danger/25 bg-danger/10 text-danger' : 'border-hairline bg-surface-sunken text-neutral-400'"
              aria-hidden="true"
            >
              <component :is="platformIcon(account.platform)" class="h-5 w-5" weight="duotone" />
            </span>
            <div class="min-w-[180px] flex-1">
              <div class="flex flex-wrap items-center gap-2.5">
                <h3 class="truncate text-sm font-semibold text-neutral-900">{{ platformOption(account.platform).label }}</h3>
                <StatusTag :status="authStatusTag(account.status).status" :label="authStatusTag(account.status).label" size="sm" />
                <!-- 临期 chip（<7 天，F7-02-s2） -->
                <span
                  v-if="expiringSoon(account)"
                  class="inline-flex items-center gap-1.5 rounded-full bg-warning-bg px-2 py-0.5 text-caption font-medium text-gd-pending"
                >
                  <span class="h-1.5 w-1.5 rounded-full bg-gd-pending" aria-hidden="true" />临期 · 剩 {{ daysLeft(account) }} 天
                </span>
              </div>
              <p class="mt-0.5 truncate text-xs text-neutral-500">{{ accountName(account) }}</p>
            </div>

            <!-- 操作区（过期 → 一键重新授权实底；已解除 → 去授权） -->
            <div class="ml-auto flex shrink-0 items-center gap-2">
              <AppButton
                v-if="account.status === 'expired'"
                variant="primary"
                size="sm"
                :loading="renewingId === account.id"
                title="重新走一遍授权流程以恢复分发能力"
                @click="renewAccount(account)"
              >
                一键重新授权
              </AppButton>
              <AppButton
                v-else
                variant="secondary"
                size="sm"
                :loading="renewingId === account.id"
                :title="account.status === 'revoked' ? '重新授权该平台' : '重新走一遍授权流程以延长有效期'"
                @click="renewAccount(account)"
              >
                {{ account.status === 'revoked' ? '去授权' : '续期' }}
              </AppButton>
              <AppButton variant="ghost" size="sm" class="!text-danger hover:!bg-danger-bg" @click="revokeTarget = account">解除授权</AppButton>
            </div>
          </div>

          <!-- 时间信息行（过期账号红字标注失效时间） -->
          <dl class="mt-3 flex flex-wrap gap-x-8 gap-y-1.5 border-t border-hairline pt-3 text-xs">
            <div class="flex items-center gap-1.5">
              <dt class="text-neutral-400">授权时间</dt>
              <dd class="text-neutral-600">{{ formatRelativeTime(account.authorized_at ?? undefined) }}</dd>
            </div>
            <div class="flex items-center gap-1.5">
              <dt class="text-neutral-400">到期时间</dt>
              <dd class="gd-num" :class="account.status === 'expired' ? 'font-semibold text-danger' : 'text-neutral-600'">
                {{ formatShortDate(account.token_expires_at ?? undefined) }}
              </dd>
            </div>
            <div v-if="account.status === 'expired'" class="flex items-center gap-1.5">
              <dt class="text-danger">分发入口置灰</dt>
              <dd class="text-neutral-400">重新授权后恢复</dd>
            </div>
          </dl>

          <!-- 临期黄色警示条（<7 天） -->
          <div v-if="expiringSoon(account)" class="mt-3 flex items-center gap-2 rounded-md border border-warning-border bg-warning-bg px-3 py-2" role="alert">
            <svg class="h-3.5 w-3.5 shrink-0 text-gd-pending" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <p class="text-xs text-gd-pending">授权即将在 7 天内到期，到期后该平台分发将失败，建议尽快点击「续期」。</p>
          </div>
        </li>
      </ul>

      <!-- 最后检查时间 -->
      <p v-if="lastCheckedAt" class="mt-4 text-center text-xs text-neutral-400">
        上次检查：{{ formatRelativeTime(lastCheckedAt) }}
      </p>
    </div>

    <!-- ============ 解除授权二次确认 ============ -->
    <ConfirmDialog
      v-model="revokeDialogOpen"
      title="确认解除该平台授权？"
      :content="`解除后「${revokeTarget ? platformOption(revokeTarget.platform).label : ''}」将无法再一键分发，加密存储的 Token 将被立即删除；如需再次使用请重新授权。`"
      confirm-label="解除授权"
      :loading="revoking"
      @confirm="confirmRevoke"
      @cancel="revokeTarget = null"
    />
  </div>
</template>

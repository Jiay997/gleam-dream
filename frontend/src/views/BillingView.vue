<!--
  文件级注释：P8 · 计费中心与预算安全体系（路由 /billing，AppShell 布局）· v8 ·
  功能布局按 stitch_prd/p8/code.html 原型 1:1 还原（主题沿用 aurora / gd-* 令牌不变）：
  刊头卡（徽标行 + 大标题 + 副标 | 立即充值 / 升级套餐 / 导出 CSV 三按钮组）→
  余额概览 4 卡（可用 / 冻结金雾 / 累计消耗 / 夜窗 7 日趋势）→
  单项目预算熔断护栏（项目选择器 + 配额进度 + 80%/100% 阈值卡 + 预算滑杆调整面板）→
  会员套餐三卡（GPU POOL TIERS，当前套餐描边 + CURRENT ACTIVE 浮标）→
  只追加流水台账（内嵌筛选条 + 7 列表 + 完整性页脚 + 分页）。
  数据全部来自现有接口：billing（账户/套餐/流水/订单/回调/订阅）+ projects
  （budget_credits / spent_credits / PATCH 更新预算上限）；7 日趋势由结算流水本地聚合派生；
  原型中无数据源的纯展示件（熔断双开关、趋势卡底部四项统计的业务分项）按结构对齐并以
  真实可派生指标或禁用占位呈现，不伪装接口数据。
  三态（LoadingSkeleton / ErrorRetryBlock / 内容）与全部功能保留：
  充值（档位与自定义输入已移入弹窗，创建订单 → 沙箱回调 → toast 到账）、
  升级订阅、流水类型筛选与分页、CSV 导出（新增，当前筛选前 100 条）。
  支付渠道（微信 / 支付宝）为沙箱视觉选择态：后端创建订单仅接收积分数，渠道不参与契约。
-->
<script setup lang="ts">
/**
 * BillingView 计费中心与预算安全体系。
 * props：无。emits：无。
 * 依赖 billing API（套餐 / 账户 / 流水 / 订单 / 回调 / 订阅）、projects API（护栏项目域数据）、
 * billing store、useToast、TableVirtual、StatusTag。
 */
import { computed, onMounted, ref, watch } from 'vue'

import {
  createRechargeOrder,
  createSubscription,
  getCreditAccount,
  listPlans,
  listTransactions,
  notifyPaymentWebhook,
} from '@/api/billing'
import type { PlanDetail, SubscriptionInfo, TransactionItem } from '@/api/billing'
import { listProjects, updateProject } from '@/api/projects'
import type { Project } from '@/api/types'
import AppButton from '@/components/common/AppButton.vue'
import CreditAmountText from '@/components/common/CreditAmountText.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import Modal from '@/components/common/Modal.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import TableVirtual from '@/components/common/TableVirtual.vue'
import type { TableColumn } from '@/components/common/TableVirtual.vue'
import { useToast } from '@/composables/useToast'
import { useBillingStore } from '@/stores/billing'
import { formatCents } from '@/utils/format'

/** Toast 提示 */
const toast = useToast()
/** 计费 store（余额与账户刷新） */
const billingStore = useBillingStore()

/* ==================== 账户 / 套餐数据 ==================== */

/** 套餐列表 */
const plans = ref<PlanDetail[]>([])
/** 当前订阅态 */
const subscription = ref<SubscriptionInfo | null>(null)
/** 累计消耗（账户摘要字段，store 未覆盖） */
const totalConsumed = ref(0)
/** 页面加载中 */
const loading = ref(true)
/** 加载失败信息 */
const errorMsg = ref('')

/** 累计发放（充值 + 周期赠送；账户摘要字段） */
const totalGranted = computed(() => billingStore.creditAccount?.total_granted_credits ?? 0)

/** 发放利用率（累计消耗 / 累计发放；无发放记录时为 0） */
const grantedUsagePercent = computed(() =>
  totalGranted.value > 0 ? Math.min(100, Math.round((totalConsumed.value / totalGranted.value) * 100)) : 0,
)

/** 当前套餐键（未订阅按 free 兜底） */
const currentPlanKey = computed(() => subscription.value?.plan_key ?? 'free')

/**
 * 拉取账户与套餐数据（进入页面与操作后刷新共用）。
 * @returns 无
 */
async function fetchSummary(): Promise<void> {
  try {
    const [account, planResult] = await Promise.all([getCreditAccount(), listPlans()])
    billingStore.creditAccount = account
    totalConsumed.value = account.total_consumed_credits ?? 0
    plans.value = [...planResult.items].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0))
    subscription.value = planResult.subscription
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '计费数据加载失败'
  } finally {
    loading.value = false
  }
}

/* ==================== 单项目预算熔断护栏（项目域真实数据） ==================== */

/** 护栏可选项目列表（近更新优先，最多 50 个） */
const guardrailProjects = ref<Project[]>([])
/** 选中的项目 ID（空串 = 未选择） */
const selectedProjectId = ref<string | number | ''>('')
/** 预算上限草稿（滑杆绑定值，0 = 不限） */
const budgetDraft = ref(0)
/** 预算保存中 */
const savingBudget = ref(false)

/** 当前选中的项目 */
const selectedProject = computed(
  () => guardrailProjects.value.find((project) => project.id === selectedProjectId.value) ?? null,
)

/** 选中项目已消耗积分 */
const guardrailSpent = computed(() => selectedProject.value?.spent_credits ?? 0)

/** 选中项目预算上限（0 = 不限） */
const guardrailBudget = computed(() => selectedProject.value?.budget_credits ?? 0)

/** 预算消耗百分比（不限预算时为 0） */
const guardrailPercent = computed(() =>
  guardrailBudget.value > 0 ? Math.min(100, (guardrailSpent.value / guardrailBudget.value) * 100) : 0,
)

/** 80% 软预警线积分 */
const softLimitCredits = computed(() => Math.round(guardrailBudget.value * 0.8))

/** 软预警是否已触发 */
const softTriggered = computed(() => guardrailBudget.value > 0 && guardrailSpent.value >= softLimitCredits.value)

/** 软预警剩余余量（积分） */
const softRemaining = computed(() => Math.max(0, softLimitCredits.value - guardrailSpent.value))

/** 100% 硬熔断是否已触发 */
const hardTriggered = computed(() => guardrailBudget.value > 0 && guardrailSpent.value >= guardrailBudget.value)

/**
 * 拉取项目列表供护栏选择器使用（近更新优先）。
 * @returns 无
 */
async function fetchProjects(): Promise<void> {
  try {
    const result = await listProjects({ page: 1, page_size: 50, sort: '-updated_at' })
    guardrailProjects.value = result.items
    const first = result.items[0]
    if (first && selectedProjectId.value === '') {
      selectedProjectId.value = first.id
    }
  } catch {
    // 拦截器已 toast；护栏选择器回退空态
  }
}

/**
 * 应用新预算上限（PATCH /projects/{id}，乐观锁冲突由拦截器提示）。
 * @returns 无
 */
async function saveBudget(): Promise<void> {
  const project = selectedProject.value
  if (!project) return
  savingBudget.value = true
  try {
    const updated = await updateProject(project.id, { budget_credits: budgetDraft.value })
    guardrailProjects.value = guardrailProjects.value.map((item) => (item.id === project.id ? updated : item))
    budgetDraft.value = updated.budget_credits ?? 0
    toast.success(
      `已更新「${project.title}」预算上限：${budgetDraft.value > 0 ? `${budgetDraft.value.toLocaleString('zh-CN')} 积分` : '不限'}`,
    )
  } catch {
    // 拦截器已 toast（10006 乐观锁冲突 / 30002 预算超限等）
  } finally {
    savingBudget.value = false
  }
}

// 切换项目时同步预算滑杆草稿值
watch(selectedProjectId, () => {
  budgetDraft.value = selectedProject.value?.budget_credits ?? 0
})

/* ==================== 近 7 日算力趋势（结算流水派生） ==================== */

/** 单日趋势桶 */
interface TrendDay {
  /** 日期标签（M/D） */
  label: string
  /** 当日结算消耗合计（正数积分） */
  total: number
  /** 当日结算笔数 */
  count: number
  /** 是否今日 */
  isToday: boolean
}

/** 近 7 日趋势（由结算流水按本地日期聚合；无数据时全 0） */
const trendDays = ref<TrendDay[]>([])

/**
 * 本地日期键（yyyy-mm-dd）。
 * @param date 日期
 * @returns 键字符串
 */
function localDateKey(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

/**
 * 拉取近 100 条结算流水并按本地日期聚合为近 7 日趋势。
 * @returns 无
 */
async function fetchTrend(): Promise<void> {
  try {
    const result = await listTransactions({ page: 1, page_size: 100, transaction_type: 'settle' })
    const buckets = new Map<string, TrendDay>()
    for (let offset = 6; offset >= 0; offset -= 1) {
      const day = new Date()
      day.setDate(day.getDate() - offset)
      buckets.set(localDateKey(day), {
        label: `${day.getMonth() + 1}/${String(day.getDate()).padStart(2, '0')}`,
        total: 0,
        count: 0,
        isToday: offset === 0,
      })
    }
    for (const tx of result.items) {
      const bucket = buckets.get(localDateKey(new Date(tx.created_at)))
      if (bucket) {
        bucket.total += Math.abs(tx.amount_credits)
        bucket.count += 1
      }
    }
    trendDays.value = [...buckets.values()]
  } catch {
    // 拦截器已 toast；趋势卡保持全 0 空态
  }
}

/** 趋势柱高基准（峰值日消耗，至少 1 避免除零） */
const trendMax = computed(() => Math.max(1, ...trendDays.value.map((day) => day.total)))

/** 近 7 日消耗合计（积分） */
const trendSum = computed(() => trendDays.value.reduce((sum, day) => sum + day.total, 0))

/** 近 7 日结算笔数 */
const trendCount = computed(() => trendDays.value.reduce((sum, day) => sum + day.count, 0))

/** 日均消耗（积分） */
const trendAvg = computed(() => Math.round(trendSum.value / 7))

/** 峰值日消耗（积分） */
const trendPeak = computed(() => trendDays.value.reduce((peak, day) => Math.max(peak, day.total), 0))

/**
 * 单柱高度百分比（相对峰值）。
 * @param day 趋势桶
 * @returns 高度百分比（0~100）
 */
function trendBarHeight(day: TrendDay): number {
  return Math.round((day.total / trendMax.value) * 100)
}

/* ==================== 套餐权益展示 ==================== */

/**
 * 套餐价格展示（分转元，免费版显示「免费」）。
 * @param plan 套餐
 * @returns 价格文案
 */
function planPrice(plan: PlanDetail): string {
  return plan.price_cents === 0 ? '免费' : formatCents(plan.price_cents)
}

/**
 * 套餐周期文案。
 * @param plan 套餐
 * @returns 「/月」或空
 */
function planPeriodSuffix(plan: PlanDetail): string {
  return plan.price_cents === 0 ? '' : plan.billing_period === 'year' ? '/年' : '/月'
}

/**
 * 套餐权益精简摘要（每期积分 + 并发 + 分辨率/水印，卡内描述行展示）。
 * @param plan 套餐
 * @returns 摘要文案
 */
function planSummary(plan: PlanDetail): string {
  const maxRes = plan.benefits?.max_resolution ? plan.benefits.max_resolution.toUpperCase() : '720P'
  const watermark = plan.benefits?.no_watermark === true ? '去水印' : '带水印'
  return `每期赠 ${plan.credits_per_period.toLocaleString('zh-CN')} 积分 · ${plan.concurrency_limit} 路并发 · ${maxRes} · ${watermark}`
}

/** 套餐权益清单项（对齐原型 ✓/✗ 双态清单） */
interface PlanPerk {
  /** 权益文案 */
  text: string
  /** 是否具备（false 渲染为 ✗ 弱化行） */
  enabled: boolean
}

/**
 * 套餐权益清单（并发 / 分辨率与 4K / 水印 / 周期积分，全部由 benefits 派生）。
 * @param plan 套餐
 * @returns 权益项数组
 */
function planPerks(plan: PlanDetail): PlanPerk[] {
  const benefits = plan.benefits ?? {}
  const maxRes = benefits.max_resolution ? benefits.max_resolution.toUpperCase() : '720P'
  const noWatermark = benefits.no_watermark === true
  return [
    { text: `${plan.concurrency_limit} 路并发渲染队列`, enabled: true },
    { text: `${maxRes} 视频生成${benefits.four_k === true ? ' · 4K 原画解锁' : ''}`, enabled: true },
    { text: noWatermark ? '高清导出无水印' : '导出带平台浮水印', enabled: noWatermark },
    { text: `每周期赠 ${plan.credits_per_period.toLocaleString('zh-CN')} 积分`, enabled: true },
  ]
}

/**
 * 套餐档位徽标样式（免费中性 / 付费琥珀 / 当前套餐品牌紫）。
 * @param plan 套餐
 * @returns 徽标类
 */
function planTierClasses(plan: PlanDetail): string {
  if (plan.key === currentPlanKey.value) return 'bg-primary-100 text-primary-700'
  if (plan.price_cents === 0) return 'bg-neutral-100 text-neutral-500'
  return 'bg-warning-bg text-warning-strong'
}

/**
 * 立即开通 / 升级套餐（POST /billing/subscriptions 沙箱直接开通并发放周期积分）。
 * @param plan 目标套餐
 * @returns 无
 */
async function upgradePlan(plan: PlanDetail): Promise<void> {
  upgradingKey.value = plan.key
  try {
    const result = await createSubscription(plan.key)
    toast.success(`已开通${plan.name}，到账 ${(result.granted_credits ?? 0).toLocaleString('zh-CN')} 积分`)
    await fetchSummary()
  } catch {
    // 拦截器已 toast（10002 套餐不存在等）
  } finally {
    upgradingKey.value = ''
  }
}

/** 升级请求进行中的套餐键 */
const upgradingKey = ref('')

/**
 * 平滑滚动至套餐权益区（刊头「升级套餐权益」入口）。
 * @returns 无
 */
function scrollToPlans(): void {
  document.getElementById('plans-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/* ==================== 积分充值（沙箱模拟支付） ==================== */

/** 快捷积分包（积分 → 价格分，1 积分 = 1 分；四档对齐原型充值抽屉） */
const CREDIT_PACKS: Array<{ credits: number }> = [{ credits: 1000 }, { credits: 5000 }, { credits: 10000 }, { credits: 50000 }]
/** 选中的积分包积分数（0 表示自定义） */
const selectedPack = ref(1000)
/** 自定义充值积分数 */
const customCredits = ref<number | null>(null)
/** 充值提交中 */
const recharging = ref(false)
/** 充值弹窗显示 */
const rechargeModalOpen = ref(false)
/** 支付渠道（沙箱视觉选择态；后端契约仅接收积分数） */
const payChannel = ref<'wechat' | 'alipay'>('wechat')

/** 支付渠道选项（名称 + 图标） */
const PAY_CHANNELS: Array<{ key: 'wechat' | 'alipay'; label: string; icon: string }> = [
  { key: 'wechat', label: '微信支付', icon: '💬' },
  { key: 'alipay', label: '支付宝', icon: '🅐' },
]

/** 本次实际充值积分数 */
const effectiveCredits = computed(() => (selectedPack.value === 0 ? (customCredits.value ?? 0) : selectedPack.value))

/**
 * 选择积分包（切到自定义时清空输入）。
 * @param credits 积分数（0 表示自定义输入）
 * @returns 无
 */
function pickPack(credits: number): void {
  selectedPack.value = credits
  if (credits !== 0) customCredits.value = null
}

/**
 * 从沙箱 pay_url 解析签名参数并回放支付回调，模拟支付成功。
 * @param payUrl 模拟支付链接（携带 out_trade_no 与固定签名）
 * @returns 回调结果
 */
async function replaySandboxPayment(payUrl: string): Promise<void> {
  const params = new URL(payUrl, window.location.origin).searchParams
  const outTradeNo = params.get('out_trade_no')
  const sign = params.get('sign')
  if (!outTradeNo || !sign) {
    throw new Error('模拟支付链接缺少回调参数')
  }
  await notifyPaymentWebhook({ out_trade_no: outTradeNo, status: 'success', sign })
}

/**
 * 打开充值弹窗（档位选择已在弹窗内完成）。
 * @returns 无
 */
function openRechargeModal(): void {
  rechargeModalOpen.value = true
}

/**
 * 弹窗内确认支付：校验 → 创建订单 → 沙箱模拟支付成功 → 刷新账户与流水。
 * @returns 无
 */
async function submitRecharge(): Promise<void> {
  if (effectiveCredits.value < 100) {
    toast.warning('单次至少充值 100 积分')
    return
  }
  recharging.value = true
  try {
    const order = await createRechargeOrder({ credit_pack_credits: effectiveCredits.value })
    await replaySandboxPayment(order.pay_url)
    toast.success(`充值成功，${effectiveCredits.value.toLocaleString('zh-CN')} 积分已到账`)
    rechargeModalOpen.value = false
    await Promise.all([fetchSummary(), fetchTransactions()])
  } catch {
    // 拦截器已 toast（参数 / 网络错误）
  } finally {
    recharging.value = false
  }
}

/* ==================== 积分流水台账 ==================== */

/** 流水类型筛选（空串=全部） */
const typeFilter = ref('')
/** 当前页流水 */
const transactions = ref<TransactionItem[]>([])
/** 总条数 */
const txTotal = ref(0)
/** 当前页码 */
const txPage = ref(1)
/** 每页条数 */
const TX_PAGE_SIZE = 20
/** 流水加载中 */
const txLoading = ref(false)

/** 总页数 */
const txTotalPages = computed(() => Math.max(1, Math.ceil(txTotal.value / TX_PAGE_SIZE)))

/** 流水类型筛选选项（与后端 transaction_type CHECK 约束逐项对齐；空 key 为「全部」pill）
    CHECK: grant / recharge / freeze / unfreeze / settle / refund / expire / adjust
    少一项的直接后果是表格里裸奔一个英文枚举值（如 expire 原样显示），所以这 8 项必须齐全。 */
const TX_TYPE_OPTIONS: Array<{ key: string; label: string }> = [
  { key: '', label: '全部' },
  { key: 'recharge', label: '充值' },
  { key: 'freeze', label: '冻结' },
  { key: 'settle', label: '结算' },
  { key: 'unfreeze', label: '解冻' },
  { key: 'refund', label: '返还' },
  { key: 'grant', label: '赠送' },
  { key: 'expire', label: '过期' },
  { key: 'adjust', label: '调整' },
]

/** 流水类型中文映射（含未知类型原样回退） */
const TX_TYPE_LABELS: Record<string, string> = Object.fromEntries(
  TX_TYPE_OPTIONS.filter((item) => item.key).map((item) => [item.key, item.label]),
)

/**
 * 查询流水类型中文标签。
 * @param type 流水类型
 * @returns 中文标签
 */
function txTypeLabel(type: string): string {
  return TX_TYPE_LABELS[type] ?? type
}

/**
 * 流水徽章样式：收入（正）绿 / 支出（负）红（胶囊形态，对齐原型业务类型列）。
 * @param item 流水
 * @returns 徽章类
 */
function txTypeClasses(item: TransactionItem): string {
  return item.amount_credits >= 0 ? 'bg-success-bg text-success-strong' : 'bg-danger-bg text-danger-strong'
}

/** 流水表列配置（列序对齐原型 7 列：流水号&时间 / 关联项目 / 业务类型 / 业务说明 / 变动 / 余额 / 凭证） */
const TX_COLUMNS: TableColumn[] = [
  { key: 'id', title: '流水号 & 时间戳 (UTC)', width: 150 },
  { key: 'project', title: '关联项目 / 阶段', width: 170 },
  { key: 'transaction_type', title: '业务类型', width: 120 },
  { key: 'description', title: '业务说明 / 算力备注', width: 190 },
  { key: 'amount_credits', title: '变动积分', width: 100, align: 'right' },
  { key: 'balance_after_credits', title: '变动后余额', width: 110, align: 'right' },
  { key: 'reference', title: '任务凭证', width: 110, align: 'center' },
]

/**
 * ISO 时间转 UTC 展示（yyyy-mm-dd hh:mm:ss）。
 * @param iso ISO8601 时间
 * @returns UTC 文本
 */
function formatUtc(iso: string): string {
  return iso.replace('T', ' ').slice(0, 19)
}

/**
 * 拉取积分流水（当前筛选与页码）。
 * @returns 无
 */
async function fetchTransactions(): Promise<void> {
  txLoading.value = true
  try {
    const result = await listTransactions({
      page: txPage.value,
      page_size: TX_PAGE_SIZE,
      transaction_type: typeFilter.value || undefined,
    })
    transactions.value = result.items
    txTotal.value = result.total
  } catch {
    // 拦截器已 toast
  } finally {
    txLoading.value = false
  }
}

/**
 * 类型筛选 pill 切换：回到第 1 页重新拉取。
 * @param key 类型键（空串=全部）
 * @returns 无
 */
function pickType(key: string): void {
  if (typeFilter.value === key) return
  typeFilter.value = key
  txPage.value = 1
  void fetchTransactions()
}

/**
 * 流水翻页。
 * @param target 目标页码
 * @returns 无
 */
function goTxPage(target: number): void {
  if (target < 1 || target > txTotalPages.value || target === txPage.value) return
  txPage.value = target
  void fetchTransactions()
}

/* ==================== 账本流水导出（CSV） ==================== */

/** 导出进行中 */
const exporting = ref(false)

/**
 * 导出当前筛选的流水台账为 CSV（含 BOM，Excel 中文兼容；单次最多 100 条）。
 * @returns 无
 */
async function exportCsv(): Promise<void> {
  exporting.value = true
  try {
    const result = await listTransactions({ page: 1, page_size: 100, transaction_type: typeFilter.value || undefined })
    const escapeCell = (value: unknown): string => `"${String(value).replace(/"/g, '""')}"`
    const lines = [
      ['流水ID', '类型', '变动积分', '变动后余额', '关联项目', '备注', '创建时间(UTC)'].join(','),
      ...result.items.map((tx) =>
        [
          tx.id,
          txTypeLabel(tx.transaction_type),
          tx.amount_credits,
          tx.balance_after_credits,
          tx.project_id ?? '',
          tx.description ?? '',
          tx.created_at,
        ]
          .map(escapeCell)
          .join(','),
      ),
    ]
    const blob = new Blob([`\uFEFF${lines.join('\n')}`], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `billing-ledger-${localDateKey(new Date())}.csv`
    anchor.click()
    URL.revokeObjectURL(url)
    toast.success(`已导出 ${result.items.length} 条流水（当前筛选，单次上限 100 条）`)
  } catch {
    // 拦截器已 toast
  } finally {
    exporting.value = false
  }
}

// 挂载：账户 + 套餐 + 流水 + 护栏项目 + 趋势聚合
onMounted(() => {
  void fetchSummary()
  void fetchTransactions()
  void fetchProjects()
  void fetchTrend()
})
</script>

<template>
  <div class="mx-auto max-w-content p-6 lg:p-8">
    <!-- ============ 刊头（统一光条 + 28px 衬线标题 | 三按钮组）· 对齐 stitch_prd/p8 ============ -->
    <header class="gd-card relative mb-6 overflow-hidden p-6 lg:p-8">
      <div class="pointer-events-none absolute -right-20 -top-24 h-72 w-72 rounded-full bg-primary-100 blur-3xl" aria-hidden="true"></div>
      <div class="relative z-10 flex flex-col gap-5 xl:flex-row xl:items-center xl:justify-between">
        <div class="flex min-w-0 items-center gap-3">
          <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true"></span>
          <h1 class="gd-masthead text-display-lg">P8 计费中心与预算安全体系</h1>
        </div>
        <div class="relative z-10 flex shrink-0 flex-wrap items-center gap-2.5">
          <AppButton variant="primary" size="md" @click="openRechargeModal">立即充值（微信/支付宝）</AppButton>
          <AppButton variant="secondary" size="md" @click="scrollToPlans">升级套餐权益</AppButton>
          <AppButton variant="subtle" size="md" :loading="exporting" @click="exportCsv">导出账本流水 (CSV)</AppButton>
        </div>
      </div>
    </header>

    <!-- ============ 加载骨架（对齐新布局：4 卡 + 护栏 + 台账） ============ -->
    <div v-if="loading" class="flex flex-col gap-5">
      <div class="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-4">
        <LoadingSkeleton v-for="i in 4" :key="i" variant="card" />
      </div>
      <LoadingSkeleton variant="card" />
      <LoadingSkeleton variant="table" :rows="6" />
    </div>

    <!-- ============ 加载失败 ============ -->
    <ErrorRetryBlock v-else-if="errorMsg" message="计费数据加载失败" :description="errorMsg" @retry="fetchSummary" />

    <template v-else>
      <div class="flex flex-col gap-5">
        <!-- ============ S1 余额概览（3 白卡 + 1 夜窗趋势卡并排） ============ -->
        <section class="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-4" aria-label="余额概览">
          <!-- 卡 1：可用积分余额 -->
          <section class="gd-card flex flex-col justify-between p-6">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2" title="可直接用于生成任务的积分 = 余额 - 冻结中">
                <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-primary-50 text-primary-300">
                  <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                    <path
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      d="M21 12a2.25 2.25 0 00-2.25-2.25H15a3 3 0 11-6 0H5.25A2.25 2.25 0 003 12m18 0v6a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 18v-6m18 0V9M3 12V9m18 0a2.25 2.25 0 00-2.25-2.25H5.25A2.25 2.25 0 003 9m18 0V6a2.25 2.25 0 00-2.25-2.25H5.25A2.25 2.25 0 003 6v3"
                    />
                  </svg>
                </div>
                <span class="text-caption font-semibold text-neutral-500">可用积分余额</span>
              </div>
              <span class="rounded-full bg-neutral-100 px-2 py-0.5 gd-num text-micro text-neutral-500">1 pts = ¥0.01</span>
            </div>
            <div class="my-3 flex flex-col gap-1">
              <p class="flex items-baseline gap-2">
                <span class="gd-num text-[34px] font-semibold leading-none text-neutral-900">{{ billingStore.balance.toLocaleString('zh-CN') }}</span>
                <span class="text-caption text-neutral-400">积分</span>
              </p>
              <p class="gd-num text-caption text-neutral-400">折合资金基准：{{ formatCents(billingStore.balance) }} CNY</p>
            </div>
            <div class="flex items-center justify-between gap-2 rounded-lg bg-surface-sunken p-2.5">
              <div class="flex items-center gap-1.5">
                <svg class="h-3.5 w-3.5 text-gold-deep" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                  <path
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    d="M21 11.25v8.25a1.5 1.5 0 01-1.5 1.5H4.5a1.5 1.5 0 01-1.5-1.5v-8.25M12 4.875A2.625 2.625 0 109.375 7.5H12m0-2.625V7.5m0-2.625A2.625 2.625 0 1114.625 7.5H12m0 0V21m-8.625-9.75h18c.621 0 1.125-.504 1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125h-18c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125z"
                  />
                </svg>
                <span class="text-micro text-neutral-500">累计发放:</span>
                <span class="gd-num text-micro font-semibold text-neutral-800">{{ totalGranted.toLocaleString('zh-CN') }} pts</span>
              </div>
              <span class="rounded bg-primary-50 px-1.5 py-0.5 gd-num text-micro font-semibold text-primary-700">充值+赠送</span>
            </div>
          </section>

          <!-- 卡 2：执行中冻结积分（金雾卡，主题沿用现有金色体系） -->
          <section class="gd-card border-gold-light bg-gold-fog flex flex-col justify-between p-6">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2" title="进行中任务预扣的积分，任务结束按实际消耗结算、剩余自动解冻">
                <div class="flex h-8 w-8 items-center justify-center rounded-lg border border-gold-light bg-surface-sunken text-gold-deep">
                  <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                    <path
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z"
                    />
                  </svg>
                </div>
                <span class="text-caption font-semibold text-gold-deep">执行中冻结积分</span>
              </div>
              <span class="rounded-full bg-surface-sunken px-2 py-0.5 gd-num text-micro text-gold-deep">
                PIPELINE {{ billingStore.frozenAmount > 0 ? 'HOLDING' : 'IDLE' }}
              </span>
            </div>
            <div class="my-3 flex flex-col gap-1">
              <p class="flex items-baseline gap-2 text-gold-deep">
                <CreditAmountText :amount="billingStore.frozenAmount" :colored="false" class="text-[34px] !leading-none" />
                <span class="text-caption text-gold">积分</span>
              </p>
              <p class="flex items-center gap-1 text-micro font-medium" :class="billingStore.frozenAmount > 0 ? 'text-gold-deep' : 'text-success'">
                <svg class="h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                <span>{{ billingStore.frozenAmount > 0 ? '任务结算实扣 · 剩余自动解冻' : '当前无阻断 / 运行中锁定任务' }}</span>
              </p>
            </div>
            <div class="rounded-lg bg-surface-sunken p-2.5">
              <p class="text-micro leading-tight text-gold-deep/80">PRD GR-4 契约：任务启动自动预检冻结，任务成功实扣，失败全额解冻返还。</p>
            </div>
          </section>

          <!-- 卡 3：累计已消耗 -->
          <section class="gd-card flex flex-col justify-between p-6">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2" title="账户创建以来所有任务实际结算消耗的积分总和">
                <div class="flex h-8 w-8 items-center justify-center rounded-lg bg-info-bg text-info-strong">
                  <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                    <path
                      stroke-linecap="round"
                      stroke-linejoin="round"
                      d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"
                    />
                  </svg>
                </div>
                <span class="text-caption font-semibold text-neutral-500">累计已消耗</span>
              </div>
              <span class="rounded-full bg-neutral-100 px-2 py-0.5 gd-num text-micro text-neutral-500">开户至今</span>
            </div>
            <div class="my-3 flex flex-col gap-1">
              <p class="flex items-baseline gap-2">
                <CreditAmountText :amount="totalConsumed" :colored="false" class="text-[34px] !leading-none" />
                <span class="text-caption text-neutral-400">积分</span>
              </p>
              <p class="gd-num text-caption text-neutral-400">折合资金基准：{{ formatCents(totalConsumed) }} CNY</p>
            </div>
            <div class="flex items-center justify-between gap-2 rounded-lg bg-surface-sunken p-2.5">
              <div class="flex items-center gap-1.5">
                <span class="text-micro text-neutral-500">发放利用率:</span>
                <span class="gd-num text-micro font-semibold text-neutral-800">{{ grantedUsagePercent }}%</span>
              </div>
              <span class="text-micro font-medium text-primary-700">成本可审计</span>
            </div>
          </section>

          <!-- 卡 4：近 7 日算力趋势（夜窗深色卡，结算流水本地聚合） -->
          <section class="gd-night-window gd-night-window-sm relative flex flex-col justify-between p-5" aria-label="近 7 日算力趋势">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2">
                <span class="h-2 w-2 animate-pulse rounded-full bg-primary-300" aria-hidden="true"></span>
                <span class="text-caption font-semibold uppercase tracking-wider text-neutral-400">近 7 日算力趋势</span>
              </div>
              <span class="gd-num text-micro text-primary-300">日均 {{ trendAvg.toLocaleString('zh-CN') }} pts/日</span>
            </div>
            <div class="relative my-3 flex h-20 items-end justify-between gap-1.5" role="img" aria-label="近 7 日结算消耗柱状图">
              <div v-if="trendCount === 0" class="pointer-events-none absolute inset-x-0 top-4 text-center text-micro text-neutral-500">
                暂无近 7 日结算记录
              </div>
              <div v-for="day in trendDays" :key="day.label" class="flex h-full flex-1 flex-col items-center justify-end gap-1">
                <div class="flex w-full flex-1 items-end overflow-hidden rounded-t-sm bg-state-hover">
                  <div
                    class="w-full rounded-t-sm bg-grad-primary transition-[height] duration-gd-slow"
                    :class="day.isToday ? 'shadow-gd-bloom' : ''"
                    :style="{ height: `${trendBarHeight(day)}%` }"
                  ></div>
                </div>
                <span class="gd-num text-[10px] leading-none" :class="day.isToday ? 'font-semibold text-primary-300' : 'text-neutral-500'">
                  {{ day.isToday ? '今日' : day.label }}
                </span>
              </div>
            </div>
            <div class="flex items-center justify-between gap-2 rounded bg-state-hover px-2 py-1 gd-num text-[11px] text-neutral-400">
              <span>结算 {{ trendCount }} 笔</span>
              <span>消耗 {{ trendSum.toLocaleString('zh-CN') }}</span>
              <span>日均 {{ trendAvg.toLocaleString('zh-CN') }}</span>
              <span>峰值 {{ trendPeak.toLocaleString('zh-CN') }}</span>
            </div>
          </section>
        </section>

        <!-- ============ S2 单项目预算熔断阀与成本护栏（项目域真实数据） ============ -->
        <section class="gd-card flex flex-col gap-5 p-6" aria-label="单项目预算熔断与成本护栏">
          <!-- 卡头：徽标 + 标题 + PRD 徽标 | 项目选择器 + 状态 -->
          <div class="flex flex-col justify-between gap-4 md:flex-row md:items-center">
            <div class="flex items-start gap-3">
              <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-primary-100 text-primary-300">
                <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                  <path
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z"
                  />
                </svg>
              </div>
              <div class="flex min-w-0 flex-col gap-0.5">
                <div class="flex flex-wrap items-center gap-2">
                  <h2 class="text-title text-neutral-900">单项目预算熔断阀与成本护栏</h2>
                  <span class="rounded-full bg-neutral-100 px-2 py-0.5 gd-num text-micro font-semibold text-primary-300">PRD US-F1-08</span>
                </div>
                <p class="text-caption text-neutral-500">为单个项目设定配额硬顶，防御提示词死循环或批处理异常导致的账户透支与高额账单。</p>
              </div>
            </div>
            <!-- 项目选择器（真实项目列表 + 项目状态徽标） -->
            <div class="flex items-center gap-2 self-start rounded-xl bg-surface-sunken px-4 py-2 md:self-auto">
              <span class="shrink-0 text-caption text-neutral-500">目标项目:</span>
              <div class="relative flex min-w-0 items-center gap-1">
                <select
                  v-model="selectedProjectId"
                  class="appearance-none bg-transparent pr-5 text-sm font-semibold text-neutral-900 outline-none"
                  aria-label="选择预算护栏目标项目"
                >
                  <option v-if="guardrailProjects.length === 0" value="" disabled>暂无项目</option>
                  <option v-for="project in guardrailProjects" :key="project.id" :value="project.id">{{ project.title }}</option>
                </select>
                <svg class="pointer-events-none absolute right-0 h-3.5 w-3.5 text-neutral-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                  <path stroke-linecap="round" stroke-linejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
                </svg>
              </div>
              <StatusTag v-if="selectedProject" :status="selectedProject.status" size="sm" />
            </div>
          </div>

          <!-- 主体 12 栅格：左 8 进度可视化 + 右 4 预算调整面板 -->
          <div class="grid grid-cols-1 items-stretch gap-5 rounded-xl bg-surface-canvas p-5 lg:grid-cols-12">
            <!-- 左：配额进度 + 图例 + 复合熔断进度条 + 阈值卡 -->
            <div class="flex flex-col gap-4 lg:col-span-8">
              <div class="flex flex-wrap items-end justify-between gap-3">
                <div class="flex flex-col">
                  <span class="gd-section-label">当前消耗配额进度</span>
                  <div class="mt-1 flex flex-wrap items-baseline gap-2">
                    <span class="gd-num text-[30px] font-semibold leading-tight text-neutral-900">{{ guardrailSpent.toLocaleString('zh-CN') }}</span>
                    <span class="text-title text-neutral-400">/</span>
                    <span class="gd-num text-title text-neutral-400">{{ guardrailBudget > 0 ? `${guardrailBudget.toLocaleString('zh-CN')} pts` : '不限' }}</span>
                    <span v-if="guardrailBudget > 0" class="gd-num ml-1 text-caption font-bold text-primary-300">({{ guardrailPercent.toFixed(1) }}%)</span>
                  </div>
                </div>
                <div class="flex items-center gap-4">
                  <div class="flex items-center gap-1.5 text-micro">
                    <span class="h-2.5 w-2.5 rounded-sm bg-primary-500" aria-hidden="true"></span>
                    <span class="text-neutral-500">实耗</span>
                  </div>
                  <div class="flex items-center gap-1.5 text-micro">
                    <span class="h-2.5 w-2.5 rounded-sm bg-warning" aria-hidden="true"></span>
                    <span class="text-neutral-500">80% 软预警</span>
                  </div>
                  <div class="flex items-center gap-1.5 text-micro">
                    <span class="h-2.5 w-2.5 rounded-sm bg-danger" aria-hidden="true"></span>
                    <span class="text-neutral-500">100% 硬熔断</span>
                  </div>
                </div>
              </div>
              <!-- 复合熔断进度条（主填充 + 80% 琥珀刻线 + 100% 红断链线） -->
              <div
                class="gd-progress-dark relative h-4 w-full overflow-hidden rounded-full"
                role="progressbar"
                :aria-valuenow="Math.round(guardrailPercent)"
                aria-valuemin="0"
                aria-valuemax="100"
                aria-label="项目预算消耗进度"
              >
                <div
                  class="absolute inset-y-0 left-0 rounded-full bg-grad-primary transition-[width] duration-gd-slow"
                  :style="{ width: `${guardrailPercent}%` }"
                ></div>
                <div v-if="guardrailBudget > 0" class="absolute inset-y-0 left-[80%] z-20 w-0.5 bg-warning" aria-hidden="true"></div>
                <div v-if="guardrailBudget > 0" class="absolute inset-y-0 right-0 z-20 w-1 bg-danger" aria-hidden="true"></div>
              </div>
              <!-- 阈值卡：80% 软预警 + 100% 硬熔断 -->
              <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div class="flex items-start gap-3 rounded-lg bg-surface-raised p-4">
                  <div class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded bg-warning-bg text-warning-strong">
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                      <path
                        stroke-linecap="round"
                        stroke-linejoin="round"
                        d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                      />
                    </svg>
                  </div>
                  <div class="flex min-w-0 flex-col gap-1">
                    <div class="flex flex-wrap items-center justify-between gap-1.5">
                      <span class="text-caption font-semibold text-neutral-900">
                        80% 软预警线 ({{ guardrailBudget > 0 ? `${softLimitCredits.toLocaleString('zh-CN')} pts` : '—' }})
                      </span>
                      <span
                        v-if="guardrailBudget > 0"
                        class="rounded px-1.5 py-0.5 gd-num text-micro font-semibold"
                        :class="softTriggered ? 'gd-fill-amber' : 'bg-success-bg text-success-strong'"
                      >
                        {{ softTriggered ? '已触发' : '安全 (未触发)' }}
                      </span>
                    </div>
                    <p v-if="guardrailBudget > 0" class="text-caption leading-normal text-neutral-500">
                      {{ softTriggered ? '已越过软预警线，站内实时提醒已推送，不阻断任务。' : `达到时触发站内实时提醒，当前余量 ${softRemaining.toLocaleString('zh-CN')} pts，不阻断任务。` }}
                    </p>
                    <p v-else class="text-caption leading-normal text-neutral-500">该项目未设置预算上限（不限），软预警线未启用。</p>
                  </div>
                </div>
                <div class="flex items-start gap-3 rounded-lg bg-surface-raised p-4">
                  <div class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded bg-danger-bg text-danger-strong">
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
                    </svg>
                  </div>
                  <div class="flex min-w-0 flex-col gap-1">
                    <div class="flex flex-wrap items-center justify-between gap-1.5">
                      <span class="text-caption font-semibold text-neutral-900">
                        100% 硬熔断阀 ({{ guardrailBudget > 0 ? `${guardrailBudget.toLocaleString('zh-CN')} pts` : '—' }})
                      </span>
                      <span
                        v-if="guardrailBudget > 0"
                        class="rounded px-1.5 py-0.5 gd-num text-micro font-semibold"
                        :class="hardTriggered ? 'gd-fill-rose' : 'bg-neutral-100 text-primary-300'"
                      >
                        {{ hardTriggered ? '已熔断' : '熔断保护监听中' }}
                      </span>
                      <span v-else class="rounded bg-neutral-100 px-1.5 py-0.5 gd-num text-micro font-semibold text-neutral-500">未启用</span>
                    </div>
                    <p class="text-caption leading-normal text-neutral-500">达到时立即硬性挂起后续生成队列，杜绝透支，需主动二次授权。</p>
                  </div>
                </div>
              </div>
            </div>
            <!-- 右：预算调整面板（滑杆 + 应用 + 熔断开关） -->
            <div class="flex flex-col gap-3 rounded-xl bg-surface-raised p-5 lg:col-span-4">
              <div class="flex flex-col gap-1">
                <span class="text-caption font-semibold text-neutral-900">调整项目预算上限 (pts)</span>
                <span class="text-caption text-neutral-500">0 表示不限预算 · 修改后对该项目后续流水即时生效</span>
              </div>
              <div class="flex items-center gap-3">
                <input
                  v-model.number="budgetDraft"
                  type="range"
                  min="0"
                  max="100000"
                  step="500"
                  :disabled="!selectedProject || savingBudget"
                  class="h-2 min-w-0 flex-1 cursor-pointer accent-primary-500 disabled:cursor-not-allowed"
                  aria-label="调整项目预算上限"
                />
                <span class="gd-num w-16 shrink-0 text-right text-caption font-semibold text-primary-300">
                  {{ budgetDraft > 0 ? budgetDraft.toLocaleString('zh-CN') : '不限' }}
                </span>
              </div>
              <AppButton variant="primary" size="sm" block :loading="savingBudget" :disabled="!selectedProject" @click="saveBudget">
                应用预算上限
              </AppButton>
              <!-- 熔断策略开关（原型结构件；后端暂无对应字段，固定开启占位） -->
              <div class="flex flex-col gap-1 pt-1">
                <label class="flex cursor-not-allowed items-center justify-between gap-3 rounded-lg p-2">
                  <span class="flex flex-col">
                    <span class="text-caption font-medium text-neutral-800">超限硬暂停保护</span>
                    <span class="text-micro text-neutral-500">强制挂起未启动任务 (SF-F1)</span>
                  </span>
                  <input type="checkbox" checked disabled class="h-4 w-4 accent-primary-500" aria-label="超限硬暂停保护（固定开启）" />
                </label>
                <label class="flex cursor-not-allowed items-center justify-between gap-3 rounded-lg p-2">
                  <span class="flex flex-col">
                    <span class="text-caption font-medium text-neutral-800">单镜超预估 1.5 倍熔断</span>
                    <span class="text-micro text-neutral-500">模型重试次数过载拦截 (NFR-K3)</span>
                  </span>
                  <input type="checkbox" checked disabled class="h-4 w-4 accent-primary-500" aria-label="单镜超预估 1.5 倍熔断（固定开启）" />
                </label>
              </div>
            </div>
          </div>
        </section>

        <!-- ============ S3 会员套餐权益与并发梯度（GPU POOL TIERS，三卡并排） ============ -->
        <section id="plans-section" class="flex scroll-mt-6 flex-col gap-4" aria-label="会员套餐权益与并发梯度">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div class="flex flex-wrap items-center gap-2">
              <h2 class="text-title text-neutral-900">会员套餐权益与并发梯度</h2>
              <span class="rounded-full bg-primary-100 px-2 py-0.5 gd-num text-micro font-semibold text-primary-700">GPU POOL TIERS</span>
            </div>
            <span class="text-caption text-neutral-400">支持按月/年灵活调优 · 订阅积分随周期自动发放</span>
          </div>
          <div class="grid grid-cols-1 gap-5 lg:grid-cols-3">
            <p v-if="plans.length === 0" class="text-caption text-neutral-500">暂无在售套餐，请稍后再试。</p>
            <!-- gd-card 自带 overflow:hidden，浮动徽标挂在外层 relative 包裹上避免被裁切 -->
            <div v-for="plan in plans" :key="plan.key" class="relative">
              <!-- 当前激活浮动徽标（对齐原型 CURRENT ACTIVE 悬浮章） -->
              <span
                v-if="plan.key === currentPlanKey"
                class="gd-fill-brand absolute -top-3 right-6 z-10 rounded-full px-3 py-0.5 gd-num text-micro font-semibold"
              >
                当前激活状态 · CURRENT ACTIVE
              </span>
              <article
                class="gd-card flex h-full flex-col justify-between p-6"
                :class="plan.key === currentPlanKey ? 'border-primary-500 shadow-brand ring-1 ring-primary-500' : ''"
              >
              <div class="flex flex-col gap-4">
                <div class="flex items-center justify-between gap-2">
                  <h3 class="flex items-center gap-1.5 text-base font-semibold text-neutral-900">
                    {{ plan.name }}
                    <svg v-if="plan.key === currentPlanKey" class="h-4 w-4 text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                      <path
                        stroke-linecap="round"
                        stroke-linejoin="round"
                        d="M9 12.75L11.25 15 15 9.75M21 12c0 1.268-.63 2.39-1.593 3.068a3.745 3.745 0 01-1.043 3.296 3.745 3.745 0 01-3.296 1.043A3.745 3.745 0 0112 21c-1.268 0-2.39-.63-3.068-1.593a3.746 3.746 0 01-3.296-1.043 3.745 3.745 0 01-1.043-3.296A3.745 3.745 0 013 12c0-1.268.63-2.39 1.593-3.068a3.745 3.745 0 011.043-3.296 3.746 3.746 0 013.296-1.043A3.746 3.746 0 0112 3c1.268 0 2.39.63 3.068 1.593a3.746 3.746 0 013.296 1.043 3.746 3.746 0 011.043 3.296A3.745 3.745 0 0121 12z"
                      />
                    </svg>
                  </h3>
                  <span class="rounded px-2 py-0.5 gd-num text-micro font-semibold" :class="planTierClasses(plan)">{{ plan.key.toUpperCase() }}</span>
                </div>
                <div class="flex flex-wrap items-baseline gap-1">
                  <span class="gd-num text-[28px] font-semibold text-neutral-900">{{ planPrice(plan) }}</span>
                  <span class="text-caption text-neutral-400">
                    {{ planPeriodSuffix(plan) }}<template v-if="plan.key === currentPlanKey && subscription?.expires_at">（到期 {{ subscription.expires_at.slice(0, 10) }}）</template>
                  </span>
                </div>
                <p class="text-caption leading-relaxed text-neutral-500">{{ planSummary(plan) }}</p>
                <!-- 权益清单（✓/✗ 双态，由 benefits 派生） -->
                <ul class="flex flex-col gap-2.5 pt-1 text-sm text-neutral-600">
                  <li v-for="perk in planPerks(plan)" :key="perk.text" class="flex items-center gap-2" :class="perk.enabled ? '' : 'text-neutral-400'">
                    <svg v-if="perk.enabled" class="h-4 w-4 shrink-0 text-primary-300" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                    </svg>
                    <svg v-else class="h-4 w-4 shrink-0 text-neutral-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                      <path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12" />
                    </svg>
                    <span>{{ perk.text }}</span>
                  </li>
                </ul>
              </div>
              <div class="mt-5">
                <AppButton v-if="plan.key === currentPlanKey" variant="secondary" size="md" block disabled>当前方案</AppButton>
                <AppButton v-else variant="primary" size="md" block :loading="upgradingKey === plan.key" @click="upgradePlan(plan)">
                  立即{{ plan.price_cents > 0 ? '升级' : '开通' }}
                </AppButton>
              </div>
              </article>
            </div>
          </div>
        </section>

        <!-- ============ S4 只追加不可篡改积分流水台账（APPEND-ONLY SECURE LEDGER） ============ -->
        <section class="gd-card flex flex-col gap-5 p-6" aria-label="积分流水台账">
          <!-- 卡头：徽标 + 标题 + LEDGER 徽标 | 类型筛选内嵌条 -->
          <div class="flex flex-col justify-between gap-4 lg:flex-row lg:items-center">
            <div class="flex items-start gap-3">
              <div class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-neutral-100 text-primary-300">
                <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                  <path
                    stroke-linecap="round"
                    stroke-linejoin="round"
                    d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25"
                  />
                </svg>
              </div>
              <div class="flex min-w-0 flex-col gap-0.5">
                <div class="flex flex-wrap items-center gap-2">
                  <h2 class="text-title text-neutral-900">只追加不可篡改积分流水台账</h2>
                  <span class="rounded-full bg-success-bg px-2 py-0.5 gd-num text-micro font-semibold text-success-strong">APPEND-ONLY SECURE LEDGER</span>
                </div>
                <p class="text-caption text-neutral-500">禁止物理删除与回溯修改，失败全额解冻返还（PRD GR-4），全程透明审计。</p>
              </div>
            </div>
            <!-- 类型筛选（功能同前，形态对齐原型内嵌筛选条） -->
            <div class="flex flex-wrap items-center gap-1 self-start rounded-xl bg-surface-sunken p-1 lg:self-auto" role="group" aria-label="按流水类型筛选">
              <button
                v-for="option in TX_TYPE_OPTIONS"
                :key="option.key"
                type="button"
                class="rounded-lg px-3 py-1 text-caption transition-colors duration-gd-fast"
                :class="typeFilter === option.key ? 'bg-surface-raised font-semibold text-neutral-900 shadow-gd-1' : 'text-neutral-500 hover:text-neutral-800'"
                :aria-pressed="typeFilter === option.key"
                @click="pickType(option.key)"
              >
                {{ option.label }}
              </button>
            </div>
          </div>

          <!-- 流水空态（EmptyState + 去充值） -->
          <EmptyState
            v-if="!txLoading && transactions.length === 0"
            class="my-6"
            icon="🧾"
            title="暂无积分流水"
            description="充值、生成任务的冻结与结算记录都会出现在这里。"
          >
            <AppButton variant="primary" size="md" @click="openRechargeModal">去充值</AppButton>
          </EmptyState>

          <template v-else>
            <!-- 流水加载骨架 -->
            <LoadingSkeleton v-if="txLoading" variant="table" :rows="5" />

            <!-- 流水表（7 列，双行单元格；thead 下沉底由 TableVirtual 内建） -->
            <div v-else class="overflow-x-auto">
              <div class="min-w-[980px]">
                <TableVirtual :rows="transactions" :columns="TX_COLUMNS" :height="420" :row-height="64">
                  <template #cell="{ row, column }">
                    <!-- 流水号 & 时间戳 -->
                    <template v-if="column.key === 'id'">
                      <div class="flex flex-col gap-0.5">
                        <span class="gd-num text-caption font-semibold text-neutral-900">#{{ row.id }}</span>
                        <span class="gd-num text-micro text-neutral-500">{{ formatUtc(row.created_at) }}</span>
                      </div>
                    </template>
                    <!-- 关联项目 / 阶段 -->
                    <template v-else-if="column.key === 'project'">
                      <div class="flex min-w-0 flex-col gap-0.5">
                        <span class="text-caption font-medium text-neutral-800">{{ row.project_id ? `项目 #${row.project_id}` : '平台账户' }}</span>
                        <span class="truncate text-micro text-neutral-500">{{ row.description || '—' }}</span>
                      </div>
                    </template>
                    <!-- 业务类型（胶囊徽标 + 色点） -->
                    <template v-else-if="column.key === 'transaction_type'">
                      <span class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium" :class="txTypeClasses(row)">
                        <span class="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true"></span>
                        {{ txTypeLabel(row.transaction_type) }}
                      </span>
                    </template>
                    <!-- 业务说明 / 算力备注（原型「调用模型与算力规格」列位，数据源为流水备注） -->
                    <template v-else-if="column.key === 'description'">
                      <div class="flex min-w-0 flex-col gap-0.5">
                        <span class="truncate text-caption text-neutral-700">{{ row.description || '—' }}</span>
                        <span class="gd-num text-micro text-neutral-500">{{ row.related_freeze_id ? `冻结链路 #${row.related_freeze_id}` : '直记入账' }}</span>
                      </div>
                    </template>
                    <!-- 变动积分（有符号正绿负红） -->
                    <template v-else-if="column.key === 'amount_credits'">
                      <CreditAmountText :amount="row.amount_credits" :show-sign="true" />
                    </template>
                    <!-- 变动后余额 -->
                    <template v-else-if="column.key === 'balance_after_credits'">
                      <span class="gd-num text-sm text-neutral-700">{{ row.balance_after_credits.toLocaleString('zh-CN') }}</span>
                    </template>
                    <!-- 任务凭证（冻结链路 ID 或系统入账标记） -->
                    <template v-else-if="column.key === 'reference'">
                      <span class="gd-num text-micro" :class="row.related_freeze_id ? 'text-primary-300' : 'text-neutral-500'">
                        {{ row.related_freeze_id ? `FRZ-${row.related_freeze_id}` : 'SYS-LEDGER' }}
                      </span>
                    </template>
                  </template>
                </TableVirtual>
              </div>
            </div>

            <!-- 页脚：记录数 + 账本完整性 | 分页 -->
            <div class="flex flex-col items-center justify-between gap-3 sm:flex-row">
              <div class="flex flex-wrap items-center justify-center gap-2 text-caption text-neutral-500">
                <span>共 <span class="gd-num text-neutral-800">{{ txTotal }}</span> 条流水记录</span>
                <span aria-hidden="true">·</span>
                <span>账本完整性: <span class="gd-num font-semibold text-neutral-800">APPEND-ONLY · 仅追加不篡改</span></span>
              </div>
              <nav class="flex items-center gap-1.5" aria-label="流水分页">
                <AppButton variant="secondary" size="sm" :disabled="txPage <= 1" @click="goTxPage(txPage - 1)">上一页</AppButton>
                <span class="gd-num px-2 text-xs text-neutral-500">{{ txPage }} / {{ txTotalPages }}</span>
                <AppButton variant="secondary" size="sm" :disabled="txPage >= txTotalPages" @click="goTxPage(txPage + 1)">下一页</AppButton>
                <span class="gd-num ml-3 text-xs text-neutral-400">共 {{ txTotal }} 条</span>
              </nav>
            </div>
          </template>
        </section>
      </div>
    </template>

    <!-- ============ 充值弹窗（档位格 + 自定义 + 渠道 + 实付金额）· 对齐原型充值抽屉 ============ -->
    <Modal v-model="rechargeModalOpen" title="算力积分快捷充值" :max-width="520">
      <p class="text-caption text-neutral-500">1 积分 = ¥0.01 CNY · 支付成功后积分秒级到账。</p>

      <!-- 档位格 2×2（四档对齐原型） -->
      <div class="mt-4 grid grid-cols-2 gap-3" role="group" aria-label="选择充值档位">
        <button
          v-for="pack in CREDIT_PACKS"
          :key="pack.credits"
          type="button"
          class="flex flex-col justify-between gap-1 rounded-xl border p-3.5 text-left transition-colors duration-gd-fast"
          :class="selectedPack === pack.credits ? 'border-primary-500 bg-primary-50 ring-1 ring-primary-500' : 'border-hairline bg-surface-sunken hover:border-hairline-strong'"
          :aria-pressed="selectedPack === pack.credits"
          @click="pickPack(pack.credits)"
        >
          <span class="flex items-baseline justify-between gap-2">
            <span class="gd-num text-base font-semibold" :class="selectedPack === pack.credits ? 'text-primary-700' : 'text-neutral-900'">
              {{ formatCents(pack.credits) }}
            </span>
            <span class="gd-num text-micro text-neutral-500">{{ pack.credits.toLocaleString('zh-CN') }} pts</span>
          </span>
          <span class="text-caption text-neutral-500">到账 {{ pack.credits.toLocaleString('zh-CN') }} 积分</span>
        </button>
      </div>

      <!-- 自定义积分输入 -->
      <label
        class="mt-3 flex cursor-pointer items-center gap-3 rounded-xl border px-3.5 py-2.5 transition-colors duration-gd-fast"
        :class="selectedPack === 0 ? 'border-primary-500 bg-primary-50 ring-1 ring-primary-500' : 'border-hairline bg-surface-sunken'"
      >
        <span class="shrink-0 text-caption text-neutral-500">自定义积分</span>
        <input
          v-model.number="customCredits"
          type="number"
          min="100"
          step="100"
          placeholder="≥ 100 积分"
          class="gd-num min-w-0 flex-1 bg-transparent text-right text-sm text-neutral-900 outline-none placeholder:text-neutral-400"
          aria-label="自定义充值积分数"
          @focus="pickPack(0)"
        />
      </label>

      <!-- 支付渠道卡（沙箱视觉选择态） -->
      <div class="mt-4 flex flex-col gap-2">
        <span class="text-caption font-semibold text-neutral-800">选择支付方式</span>
        <div class="grid grid-cols-2 gap-3" role="radiogroup" aria-label="选择支付渠道">
          <button
            v-for="channel in PAY_CHANNELS"
            :key="channel.key"
            type="button"
            role="radio"
            class="flex h-11 items-center justify-center gap-2 rounded-lg border text-sm transition-colors duration-gd-fast"
            :class="payChannel === channel.key ? 'border-primary-500 bg-primary-50 font-medium text-primary-700' : 'border-hairline bg-surface-sunken text-neutral-600 hover:border-hairline-strong'"
            :aria-checked="payChannel === channel.key"
            @click="payChannel = channel.key"
          >
            <span aria-hidden="true">{{ channel.icon }}</span>
            {{ channel.label }}
          </button>
        </div>
      </div>

      <!-- 底部：实付金额 + 确认提交 -->
      <div class="mt-6 flex items-center justify-between gap-3 border-t border-hairline pt-4">
        <div class="flex flex-col">
          <span class="text-micro text-neutral-500">实际支付金额</span>
          <span class="gd-num text-2xl font-semibold text-neutral-900">{{ formatCents(effectiveCredits) }}</span>
        </div>
        <div class="flex items-center gap-2.5">
          <AppButton variant="secondary" size="md" :disabled="recharging" @click="rechargeModalOpen = false">取消</AppButton>
          <AppButton variant="primary" size="md" :loading="recharging" @click="submitRecharge">确认并拉起收银台</AppButton>
        </div>
      </div>
    </Modal>
  </div>
</template>

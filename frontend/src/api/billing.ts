/**
 * 文件级注释：计费域 API（与 backend/app/api/billing.py 签名对齐）。
 * - GET  /billing/plans             套餐列表 + 当前用户订阅态；
 * - GET  /billing/credit-account    积分账户摘要（available = balance - frozen）；
 * - GET  /billing/transactions      积分流水分页（transaction_type 筛选）；
 * - POST /billing/orders            创建订单（plan_key / credit_pack_credits 二选一，沙箱返回模拟支付链接）；
 * - POST /billing/webhooks/payment  支付回调（沙箱联调：前端可回放 pay_url 签名模拟支付成功）；
 * - POST /billing/subscriptions     直接开通订阅（沙箱联调端点，跳过支付并发放周期积分）。
 */
import { http } from './http'
import type { CreditAccount, Paginated } from './types'

/** 套餐权益位（与后端 plans.benefits JSONB 种子结构对齐） */
export interface PlanBenefits {
  /** 最高可用分辨率（720p/1080p/4k） */
  max_resolution?: string
  /** 是否允许去水印（true 时导出可关闭水印） */
  no_watermark?: boolean
  /** 是否解锁 4K 导出 */
  four_k?: boolean
  /** 是否强制水印 */
  watermark?: boolean
  /** 兼容后端后续扩展的权益位 */
  [key: string]: unknown
}

/** 套餐详情（GET /billing/plans items 元素，与后端 list_plans 输出对齐） */
export interface PlanDetail {
  /** 套餐 ID（自增） */
  id: number | string
  /** 套餐键（free/pro/studio） */
  key: string
  /** 显示名 */
  name: string
  /** 计费周期（month/year） */
  billing_period: string
  /** 价格（人民币分） */
  price_cents: number
  /** 币种（CNY） */
  currency: string
  /** 每周期赠送积分 */
  credits_per_period: number
  /** 并发生成任务上限 */
  concurrency_limit: number
  /** 权益位 JSONB */
  benefits: PlanBenefits | null
  /** 展示排序 */
  sort_order?: number
}

/** 当前用户订阅态（GET /billing/plans subscription 字段） */
export interface SubscriptionInfo {
  /** 订阅的套餐 ID */
  plan_id: number | string
  /** 订阅的套餐键（free/pro/studio） */
  plan_key: string | null
  /** 订阅状态（active） */
  status: string
  /** 生效时间（ISO8601） */
  started_at: string | null
  /** 到期时间（ISO8601） */
  expires_at: string | null
  /** 是否自动续费 */
  auto_renew: boolean
}

/** 套餐列表响应数据 */
export interface PlanListResult {
  /** 在售套餐列表 */
  items: PlanDetail[]
  /** 当前用户订阅态（未登录 / 未订阅为 null） */
  subscription: SubscriptionInfo | null
}

/** 积分流水（GET /billing/transactions items 元素，与后端 list_transactions 输出对齐） */
export interface TransactionItem {
  /** 流水 ID */
  id: number | string
  /** 类型（grant/recharge/freeze/settle/unfreeze/refund） */
  transaction_type: string
  /** 变动金额（积分，正收入 / 负支出） */
  amount_credits: number
  /** 变动后余额 */
  balance_after_credits: number
  /** 变动后冻结金额 */
  frozen_after_credits?: number | null
  /** 关联冻结 ID（冻结 / 结算 / 解冻链路） */
  related_freeze_id?: number | string | null
  /** 关联项目 ID（可空） */
  project_id?: number | string | null
  /** 备注说明 */
  description?: string | null
  /** 创建时间（ISO8601） */
  created_at: string
}

/** 创建订单响应（沙箱返回模拟支付链接） */
export interface OrderResult {
  /** 业务订单号 */
  order_no: string
  /** 商户订单号（与 order_no 一致，回调幂等键） */
  out_trade_no: string
  /** 订单类型（subscription/credit_pack） */
  order_type: string
  /** 订单积分数 */
  credits: number
  /** 订单金额（分） */
  amount_cents: number
  /** 订单状态（created） */
  status: string
  /** 模拟支付链接（携带固定签名，可解析出 sign 供回调回放） */
  pay_url: string
}

/** 支付回调响应 */
export interface PaymentWebhookResult {
  /** 商户订单号 */
  out_trade_no: string
  /** 是否重复回调（幂等短路） */
  duplicate: boolean
  /** 回调后订单状态 */
  order_status: string | null
  /** 本次入账积分数 */
  granted_credits: number
}

/** 直接开通订阅响应 */
export interface SubscriptionResult {
  /** 开通的套餐摘要 */
  plan: Record<string, unknown>
  /** 本次发放周期积分 */
  granted_credits: number
  /** 发放后的积分账户 */
  credit_account: CreditAccount | null
}

/** 创建订单请求体（套餐 / 积分充值包二选一） */
export interface OrderCreatePayload {
  /** 套餐键 */
  plan_key?: string
  /** 充值包积分数 */
  credit_pack_credits?: number
}

/**
 * 获取在售套餐列表与当前用户订阅态。
 * @returns { items, subscription }
 */
export function listPlans(): Promise<PlanListResult> {
  return http.get<PlanListResult>('/billing/plans')
}

/**
 * 获取当前用户积分账户（可用余额 = balance_credits - frozen_credits）。
 * @returns 积分账户摘要
 * @throws ApiError 30005 积分账户不存在
 */
export function getCreditAccount(): Promise<CreditAccount> {
  return http.get<CreditAccount>('/billing/credit-account')
}

/**
 * 获取积分流水分页。
 * @param params 分页 / 类型筛选 / 排序参数
 * @returns 分页流水列表
 */
export function listTransactions(params?: {
  page?: number
  page_size?: number
  sort?: string
  transaction_type?: string
}): Promise<Paginated<TransactionItem>> {
  return http.get<Paginated<TransactionItem>>('/billing/transactions', { params })
}

/**
 * 创建充值订单（沙箱返回模拟支付链接 pay_url）。
 * @param payload plan_key / credit_pack_credits 二选一
 * @returns 订单号 / 金额 / 模拟支付链接
 * @throws ApiError 10001 参数二选一约束失败 / 10002 套餐不存在或已下架
 */
export function createRechargeOrder(payload: OrderCreatePayload): Promise<OrderResult> {
  return http.post<OrderResult>('/billing/orders', payload)
}

/**
 * 支付结果回调通知（联调 / 测试环境模拟支付成功使用）。
 * @param payload 支付渠道回调负载（out_trade_no/status/sign）
 * @returns 无
 */
export function notifyPaymentWebhook(payload: {
  out_trade_no: string
  status: string
  sign: string
}): Promise<PaymentWebhookResult> {
  return http.post<PaymentWebhookResult>('/billing/webhooks/payment', payload)
}

/**
 * 直接开通 / 续期订阅（沙箱联调端点，跳过支付并立即发放周期积分）。
 * @param planKey 套餐键（free/pro/studio）
 * @returns { plan, granted_credits, credit_account }
 * @throws ApiError 10002 套餐不存在或已下架
 */
export function createSubscription(planKey: string): Promise<SubscriptionResult> {
  return http.post<SubscriptionResult>('/billing/subscriptions', { plan_key: planKey })
}

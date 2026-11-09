/**
 * 文件级注释：审批域 API（HITL 四类关口审批单，与 backend/app/api/orchestration.py 对齐）。
 * 端点清单：
 * - GET  /orchestration/approvals?status=&project_id=&page=&page_size=  审批单列表（查询前惰性过期）；
 * - GET  /orchestration/approvals/{id}                                  审批单详情（含 edited_payload）；
 * - POST /orchestration/approvals/{id}/decide                            审批决策（202，流程断点恢复）。
 * 可见范围：当前用户为 Owner 或项目成员的项目；project_id 不在可见范围时返回空集。
 */
import { http } from './http'

/** 审批单类型（后端 CHECK 约束四值） */
export type ApprovalType = 'script' | 'asset' | 'storyboard' | 'final_cut'

/** 审批单状态 */
export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'edited' | 'expired'

/** 审批单（后端 _approval_out 输出形状，列表 / 详情共用） */
export interface ApprovalItem {
  /** 审批单 ID */
  id: number
  /** 所属项目 ID */
  project_id: number
  /** 关口类型（script/asset/storyboard/final_cut） */
  approval_type: ApprovalType
  /** 目标类型（script/asset/shot/timeline） */
  target_type: string | null
  /** 目标 ID（多态，如剧本 ID） */
  target_id: number | null
  /** 审批状态 */
  status: ApprovalStatus
  /** 决策（approve/reject/edit，未决策为 null） */
  decision: string | null
  /** 反馈原文（reject/edit 时写入） */
  feedback: string | null
  /** 发起时间 */
  requested_at: string | null
  /** 决策时间 */
  decided_at: string | null
  /** 决策人用户 ID */
  decided_by_user_id: number | null
  /** 过期时刻（默认发起后 7 天） */
  expires_at: string | null
  /** 是否携带人工编辑载荷 */
  has_edited_payload: boolean
  /** 创建时间 */
  created_at: string | null
}

/** 审批单详情（列表项 + 人工编辑载荷） */
export interface ApprovalDetail extends ApprovalItem {
  /** 人工编辑内容（仅编辑过时返回，结构 {episodes:[...]}） */
  edited_payload?: Record<string, unknown> | null
}

/** 审批决策请求体 */
export interface ApprovalDecisionPayload {
  /** 决策动作：approve 通过 / reject 驳回（feedback 必填）/ edit 直改 */
  action: 'approve' | 'reject' | 'edit'
  /** 反馈原文（reject 时必填 ≥10 字；edit 时建议填写） */
  feedback?: string
  /** 人工编辑内容（edit 时必填） */
  edited_payload?: Record<string, unknown>
}

/** 审批单分页（后端 {items, page, page_size, total}） */
export interface ApprovalPage {
  /** 当前页数据 */
  items: ApprovalItem[]
  /** 页码 */
  page: number
  /** 页大小 */
  page_size: number
  /** 总条数 */
  total: number
}

/**
 * 查询审批单列表（默认待办；查询前惰性把过期 pending 单置 expired）。
 * @param params 状态（pending/approved/rejected/edited/expired/all，默认 pending）/ 项目过滤 / 关口类型过滤 / 分页
 * @returns 分页审批单列表
 */
export function listApprovals(params?: {
  status?: string
  project_id?: string | number
  /** 审批类型过滤（后端已支持 script/asset/storyboard/final_cut） */
  approval_type?: string
  page?: number
  page_size?: number
}): Promise<ApprovalPage> {
  return http.get<ApprovalPage>('/orchestration/approvals', { params })
}

/**
 * 查询审批单详情（含审批台摘要所需的项目 / 剧本引用）。
 * @param approvalId 审批单 ID
 * @returns 审批单详情
 * @throws ApiError 10002 不存在 / 20003 无权限
 */
export function getApprovalDetail(approvalId: string | number): Promise<ApprovalDetail> {
  return http.get<ApprovalDetail>(`/orchestration/approvals/${approvalId}`)
}

/**
 * 提交审批决策（202 受理；approve 结算推进 / reject 局部改写 / edit 直改剧本）。
 * @param approvalId 审批单 ID
 * @param payload 决策动作与反馈
 * @returns 决策受理信息（后端 202 data，结构宽松）
 * @throws ApiError 40001 已处理 / 已过期；20003 无权限
 */
export function decideApproval(
  approvalId: string | number,
  payload: ApprovalDecisionPayload,
): Promise<Record<string, unknown>> {
  return http.post<Record<string, unknown>>(`/orchestration/approvals/${approvalId}/decide`, payload)
}

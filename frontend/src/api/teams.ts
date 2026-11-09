/**
 * 文件级注释：团队域 API（与 backend/app/api/teams.py、projects.py 成员端点对齐）。
 * - POST   /teams                                创建团队（创建者为 owner）；
 * - GET    /teams                                我参与的团队列表（含我的角色）；
 * - POST   /teams/{team_id}/members              按手机号邀请成员（member/guest/admin）；
 * - DELETE /teams/{team_id}/members/{user_id}    移除成员（级联清理项目授权）；
 * - GET    /projects/{project_id}/members        项目成员授权列表；
 * - PUT    /projects/{project_id}/members/{user_id}  设置项目成员权限（view/edit/export）。
 */
import { http } from './http'

/** 团队（GET /teams items 元素，与后端 team_out 序列化对齐） */
export interface TeamItem {
  /** 团队 ID */
  id: number | string
  /** 团队名称 */
  name: string
  /** 团队创建者用户 ID */
  owner_user_id: number | string
  /** 席位上限 */
  seat_limit?: number | null
  /** 团队状态 */
  status?: string
  /** 当前用户在该团队中的角色（owner/admin/member/guest） */
  my_role?: string | null
  /** 创建时间（ISO8601） */
  created_at?: string | null
}

/** 团队成员（team_member_out 序列化形状） */
export interface TeamMemberItem {
  /** 团队 ID */
  team_id: number | string
  /** 成员用户 ID */
  user_id: number | string
  /** 团队角色（owner/admin/member/guest） */
  role: string
  /** 邀请人用户 ID */
  invited_by?: number | string | null
  /** 成员昵称（缺失为 null） */
  nickname?: string | null
  /** 加入时间（ISO8601） */
  joined_at?: string | null
}

/** 项目成员授权记录（project_member_out 序列化形状） */
export interface ProjectMemberItem {
  /** 成员用户 ID */
  user_id: number | string
  /** 权限级别（view/edit/export） */
  permission: string
  /** 授权人用户 ID */
  granted_by?: number | string | null
  /** 成员昵称（缺失为 null） */
  nickname?: string | null
  /** 是否为项目 Owner（owner 行权限不可变更） */
  is_owner?: boolean
  /** 授权时间（ISO8601） */
  created_at?: string | null
}

/** 项目成员列表响应（Owner 记录 + project_members 授权） */
export interface ProjectMembersResult {
  /** 项目 Owner 用户 ID */
  owner_user_id: number | string
  /** 成员授权列表 */
  items: ProjectMemberItem[]
}

/** 创建团队请求体 */
export interface TeamCreatePayload {
  /** 团队名称（1~128 字符） */
  name: string
}

/** 邀请成员请求体（按手机号定位用户） */
export interface TeamMemberAddPayload {
  /** 被邀请人手机号 */
  phone: string
  /** 团队角色（member/guest/admin，owner 不可通过邀请设置） */
  role: 'member' | 'guest' | 'admin'
}

/** 项目权限设置请求体 */
export interface ProjectPermissionSetPayload {
  /** 权限级别 */
  permission: 'view' | 'edit' | 'export'
}

/**
 * 创建团队（当前用户成为 owner）。
 * @param payload 团队名称
 * @returns 团队详情（my_role=owner）
 * @throws ApiError 20001 未登录时由拦截器统一提示
 */
export function createTeam(payload: TeamCreatePayload): Promise<TeamItem> {
  return http.post<TeamItem>('/teams', payload)
}

/**
 * 查询我参与的团队列表（含我的角色）。
 * @returns 团队数组
 */
export function listTeams(): Promise<TeamItem[]> {
  return http.get<TeamItem[]>('/teams')
}

/**
 * 按手机号邀请用户加入团队（仅 owner/admin）。
 * @param teamId 团队 ID
 * @param payload 手机号与角色
 * @returns 团队成员记录
 * @throws ApiError 10002 团队/用户不存在 / 20003 无权限或目标为 owner
 */
export function inviteTeamMember(teamId: string | number, payload: TeamMemberAddPayload): Promise<TeamMemberItem> {
  return http.post<TeamMemberItem>(`/teams/${teamId}/members`, payload)
}

/**
 * 移除团队成员（级联删除其项目授权；不能移除 owner）。
 * @param teamId 团队 ID
 * @param userId 被移除用户 ID
 * @returns { removed, revoked_project_grants }
 * @throws ApiError 10002 成员不存在 / 20003 无权限或目标为 owner
 */
export function removeTeamMember(teamId: string | number, userId: string | number): Promise<{ removed: boolean; revoked_project_grants: number }> {
  return http.delete<{ removed: boolean; revoked_project_grants: number }>(`/teams/${teamId}/members/${userId}`)
}

/**
 * 查询项目成员授权列表。
 * @param projectId 项目 ID
 * @returns { owner_user_id, items }
 * @throws ApiError 10002 项目不存在 / 20003 无权限
 */
export function listProjectMembers(projectId: string | number): Promise<ProjectMembersResult> {
  return http.get<ProjectMembersResult>(`/projects/${projectId}/members`)
}

/**
 * 设置项目成员权限（view/edit/export；Owner 权限不可变更）。
 * @param projectId 项目 ID
 * @param userId 目标用户 ID
 * @param payload 权限级别
 * @returns 成员授权记录
 * @throws ApiError 20003 目标为 Owner 时由拦截器统一提示
 */
export function setProjectPermission(projectId: string | number, userId: string | number, payload: ProjectPermissionSetPayload): Promise<ProjectMemberItem> {
  return http.put<ProjectMemberItem>(`/projects/${projectId}/members/${userId}`, payload)
}

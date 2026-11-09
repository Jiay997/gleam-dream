/**
 * 文件级注释：项目域 API（与 backend/app/api/projects.py 签名对齐）。
 * - POST   /projects                        创建项目；
 * - GET    /projects?scope=&status=&genre=&page=&page_size=&sort=  分页列表；
 * - GET/PATCH/DELETE /projects/{id}         详情 / 更新（乐观锁）/ 软删除；
 * - POST   /projects/{id}/transition        状态机流转（target_status）；
 * - POST   /projects/{id}/archive           归档（终态）；
 * - 快照 / 回滚 / 时间线。
 */
import { http } from './http'
import type { Paginated, Project, ProjectSnapshot, TimelineEvent } from './types'

/** 项目列表查询参数（与后端 list_projects Query 参数一致） */
export interface ListProjectsParams {
  /** 可见范围：created 我创建 / joined 我参与 / all 全部，默认 created */
  scope?: 'created' | 'joined' | 'all'
  /** 状态筛选（项目 14 态之一） */
  status?: string
  /** 题材筛选 */
  genre?: string
  /** 页码（≥1） */
  page?: number
  /** 每页条数（1~100） */
  page_size?: number
  /** 排序字段（- 前缀倒序，如 -updated_at / -created_at / -spent_credits） */
  sort?: string
}

/** 创建项目请求体（与后端 ProjectCreateRequest 对齐，title 必填） */
export interface ProjectCreatePayload {
  /** 项目标题（1~255 字符） */
  title: string
  /** 一句话简介 */
  synopsis?: string
  /** 题材 */
  genre?: string
  /** 画风预设键（live_action/anime/chinese_style/three_d） */
  style_preset_key?: 'live_action' | 'anime' | 'chinese_style' | 'three_d'
  /** 画幅比例（9:16/16:9/1:1） */
  aspect_ratio?: '9:16' | '16:9' | '1:1'
  /** 目标集数（1~30） */
  target_episode_count?: number
  /** 预算上限（积分，0 不限） */
  budget_credits?: number
}

/** 更新项目请求体（PATCH 语义，仅显式传入字段生效） */
export interface ProjectUpdatePayload {
  /** 新标题 */
  title?: string
  /** 新简介 */
  synopsis?: string | null
  /** 新题材 */
  genre?: string | null
  /** 新预算上限（积分） */
  budget_credits?: number
  /** 新配置（整体覆盖） */
  config?: Record<string, unknown>
  /** 乐观锁版本号（不一致返回 10006） */
  lock_version?: number
}

/**
 * 获取项目分页列表。
 * @param params 范围 / 过滤 / 分页 / 排序参数
 * @returns 分页项目列表
 */
export function listProjects(params?: ListProjectsParams): Promise<Paginated<Project>> {
  return http.get<Paginated<Project>>('/projects', { params })
}

/**
 * 获取项目详情。
 * @param projectId 项目 ID
 * @returns 项目详情
 */
export function getProject(projectId: string | number): Promise<Project> {
  return http.get<Project>(`/projects/${projectId}`)
}

/**
 * 创建项目（一句话创意 → 项目，owner=当前用户，status=draft）。
 * @param payload 项目标题与初始设定
 * @returns 新项目
 * @throws ApiError 40005 集数超限 / 30002 预算超限
 */
export function createProject(payload: ProjectCreatePayload): Promise<Project> {
  return http.post<Project>('/projects', payload)
}

/**
 * 更新项目基础信息（title/synopsis/genre/budget_credits，乐观锁 lock_version）。
 * @param projectId 项目 ID
 * @param payload 更新字段
 * @returns 更新后的项目
 */
export function updateProject(projectId: string | number, payload: ProjectUpdatePayload): Promise<Project> {
  return http.patch<Project>(`/projects/${projectId}`, payload)
}

/**
 * 删除项目（软删除，危险操作，自动携带幂等键）。
 * @param projectId 项目 ID
 * @returns { id, deleted }
 * @throws ApiError 40004 已归档 / 20003 无权限时由拦截器统一提示
 */
export function deleteProject(projectId: string | number): Promise<{ id: number | string; deleted: boolean }> {
  return http.delete<{ id: number | string; deleted: boolean }>(`/projects/${projectId}`)
}

/**
 * 项目状态流转（按状态机白名单，非法流转返回 40001 与 allowed 列表）。
 * @param projectId 项目 ID
 * @param targetStatus 目标状态
 * @param reason 流转原因（驳回 / 拦截时填写）
 * @returns 流转后的项目
 */
export function transitionProject(projectId: string | number, targetStatus: string, reason?: string): Promise<Project> {
  return http.post<Project>(`/projects/${projectId}/transition`, { target_status: targetStatus, reason })
}

/**
 * 归档项目（终态，重复归档返回 40004）。
 * @param projectId 项目 ID
 * @returns 归档后的项目
 */
export function archiveProject(projectId: string | number): Promise<Project> {
  return http.post<Project>(`/projects/${projectId}/archive`)
}

/**
 * 恢复归档项目（POST /projects/{id}/unarchive，仅 Owner）。
 * 归档为只读终态，恢复后回到归档前状态；用于给「归档」提供显式回退路径。
 * @param projectId 项目 ID
 * @returns 恢复后的项目
 * @throws ApiError 20003 非 Owner / 40001 项目未处于归档状态
 */
export function unarchiveProject(projectId: string | number): Promise<Project> {
  return http.post<Project>(`/projects/${projectId}/unarchive`)
}

/**
 * 恢复暂停或失败的项目（POST /projects/{id}/resume）。
 * status=paused 还原 _paused_before；status=failed 还原 _failed_before
 * （失败恢复入口：修复 failed 项目无出边导致的死锁）。
 * @param projectId 项目 ID
 * @returns 恢复后的项目
 * @throws ApiError 40001 项目未处于暂停或失败状态时由拦截器统一提示
 */
export function resumeProject(projectId: string | number): Promise<Project> {
  return http.post<Project>(`/projects/${projectId}/resume`)
}

/**
 * 获取项目快照列表。
 * @param projectId 项目 ID
 * @param params 分页参数
 * @returns 分页快照列表
 */
export function listProjectSnapshots(projectId: string | number, params?: { page?: number; page_size?: number }): Promise<Paginated<ProjectSnapshot>> {
  return http.get<Paginated<ProjectSnapshot>>(`/projects/${projectId}/snapshots`, { params })
}

/**
 * 回滚到指定快照。
 * @param projectId 项目 ID
 * @param snapshotId 快照 ID
 * @returns 回滚结果与项目详情
 */
export function rollbackSnapshot(projectId: string | number, snapshotId: string | number): Promise<{ rolled_back: boolean; project: Project }> {
  return http.post<{ rolled_back: boolean; project: Project }>(`/projects/${projectId}/snapshots/${snapshotId}/rollback`)
}

/**
 * 获取项目时间线事件。
 * @param projectId 项目 ID
 * @param params 分页参数
 * @returns 分页时间线事件
 */
export function listProjectTimeline(projectId: string | number, params?: { page?: number; page_size?: number }): Promise<Paginated<TimelineEvent>> {
  return http.get<Paginated<TimelineEvent>>(`/projects/${projectId}/timeline`, { params })
}

/**
 * 文件级注释：项目状态 store。
 * 维护当前项目（与项目域路由同步）、项目列表缓存与 SSE 连接状态镜像；
 * 进入项目域路由时由路由守卫调用 ensureProject 拉取详情。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { getProject, listProjects } from '@/api/projects'
import type { Project } from '@/api/types'
import type { SseStatus } from '@/composables/useSSE'

/**
 * 项目 store：当前项目 / 列表缓存 / SSE 状态。
 */
export const useProjectStore = defineStore('project', () => {
  /** 当前项目详情 */
  const currentProject = ref<Project | null>(null)
  /** 项目列表缓存 */
  const projects = ref<Project[]>([])
  /** 列表加载中标记 */
  const projectsLoading = ref(false)
  /** SSE 连接状态（由 AppShell 镜像写入，供状态栏展示） */
  const sseStatus = ref<SseStatus>('offline')

  /** 当前项目 ID（无项目时为空串，统一转 string 供路由拼接） */
  const currentProjectId = computed(() => (currentProject.value ? String(currentProject.value.id) : ''))

  /**
   * 写入当前项目。
   * @param project 项目对象或 null
   * @returns 无
   */
  function setCurrentProject(project: Project | null): void {
    currentProject.value = project
  }

  /**
   * 确保当前项目已加载：与目标 ID 一致时直接复用，否则拉取详情。
   * @param projectId 目标项目 ID
   * @returns 项目详情
   * @throws ApiError 项目不存在 / 无权限时由拦截器统一提示
   */
  async function ensureProject(projectId: string): Promise<Project> {
    if (currentProject.value?.id === projectId) {
      return currentProject.value
    }
    const project = await getProject(projectId)
    currentProject.value = project
    return project
  }

  /**
   * 加载项目列表（默认走缓存，force=true 强制刷新）。
   * @param force 是否强制刷新
   * @returns 项目列表
   */
  async function loadProjects(force = false): Promise<Project[]> {
    if (!force && projects.value.length > 0) {
      return projects.value
    }
    projectsLoading.value = true
    try {
      const page = await listProjects({ page: 1, page_size: 100 })
      projects.value = page.items
      return page.items
    } finally {
      projectsLoading.value = false
    }
  }

  /**
   * 刷新当前项目详情（SSE status_changed 事件驱动）。
   * 后端状态机流转（含生成失败自动回滚）后同步状态条与页面判定，
   * 避免底部状态栏停留在过期的「生成中」状态。
   * @returns 无
   */
  async function refreshCurrentProject(): Promise<void> {
    if (!currentProject.value) return
    try {
      currentProject.value = await getProject(String(currentProject.value.id))
    } catch {
      // 静默失败：后续事件或页面刷新会重新拉取
    }
  }

  /**
   * 清空当前项目（离开项目域时调用）。
   * @returns 无
   */
  function clearCurrentProject(): void {
    currentProject.value = null
  }

  /**
   * 更新 SSE 连接状态镜像。
   * @param value 目标状态
   * @returns 无
   */
  function setSseStatus(value: SseStatus): void {
    sseStatus.value = value
  }

  return {
    currentProject,
    currentProjectId,
    projects,
    projectsLoading,
    sseStatus,
    setCurrentProject,
    ensureProject,
    loadProjects,
    refreshCurrentProject,
    clearCurrentProject,
    setSseStatus,
  }
})

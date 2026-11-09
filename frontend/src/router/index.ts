/**
 * 文件级注释：Vue Router 路由表与全局守卫。
 * - 全部页面视图懒加载；
 * - meta 携带 title / requiresAuth / projectScoped / permission / bare；
 * - 守卫职责：未登录访问受保护页 → 跳登录（携带 redirect）；已登录访问登录页 → 跳工作台；
 *   进入项目域路由时确保 currentProject 已加载（失败回退项目列表）。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { useProjectStore } from '@/stores/project'
import { useToast } from '@/composables/useToast'

/** 路由 meta 类型扩展 */
declare module 'vue-router' {
  interface RouteMeta {
    /** 页面标题（拼接至 document.title） */
    title?: string
    /** 是否需要登录 */
    requiresAuth?: boolean
    /** 是否为项目域路由（:projectId 上下文） */
    projectScoped?: boolean
    /** 所需权限标识 */
    permission?: string
    /** 是否为无壳裸页（登录 / 注册，不渲染 AppShell） */
    bare?: boolean
    /**
     * 是否由顶栏承载页面刊头（衬线大标题 + 全局操作区同排）。
     * 置真时顶栏隐藏面包屑、高度升为 80px，且页面自身不再渲染 PageHeader 标题，
     * 避免"顶栏小标题 + 页面大标题"在同一屏内重复。当前仅 /projects 使用。
     */
    titleInTopBar?: boolean
  }
}

/** 路由表 */
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/projects' },

  /* ---------- 裸页（无 AppShell） ---------- */
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { title: '登录', requiresAuth: false, bare: true },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { title: '注册', requiresAuth: false, bare: true },
  },
  {
    path: '/onboarding',
    name: 'onboarding',
    component: () => import('@/views/OnboardingView.vue'),
    meta: { title: '新手引导', requiresAuth: true, bare: true },
  },

  /* ---------- 工作台 ---------- */
  {
    path: '/projects',
    name: 'projects',
    component: () => import('@/views/ProjectsView.vue'),
    // titleInTopBar：刊头「项目创作中心」由 TopBar 以衬线大字承载（对齐方向 B 参考图构图）
    meta: { title: '项目创作中心', requiresAuth: true, titleInTopBar: true },
  },
  {
    path: '/projects/new',
    name: 'project-create',
    component: () => import('@/views/ProjectCreateView.vue'),
    meta: { title: '新建项目', requiresAuth: true },
  },

  /* ---------- 资产库（全局档案域） ----------
     与链路 STAGE 03「资产设计」严格分层：
     /assets            → 项目管理库（选择项目，浏览已定稿资产概况）
     /assets/:projectId → 该项目只读资产档案（角色 / 场景 / 道具）
     设计与生成仍在 /projects/:projectId/assets（AssetWallView）。 */
  {
    path: '/assets',
    name: 'asset-library',
    component: () => import('@/views/AssetLibraryView.vue'),
    meta: { title: '资产库', requiresAuth: true },
  },
  {
    path: '/assets/:projectId',
    name: 'project-asset-library',
    component: () => import('@/views/ProjectAssetLibraryView.vue'),
    meta: { title: '项目资产档案', requiresAuth: true },
  },

  /* ---------- 项目域 ---------- */
  {
    path: '/projects/:projectId/overview',
    name: 'project-overview',
    component: () => import('@/views/ProjectOverviewView.vue'),
    meta: { title: '审批台', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/script',
    name: 'project-script',
    component: () => import('@/views/ScriptEditorView.vue'),
    meta: { title: '剧本编辑器', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/script/outline',
    name: 'script-outline',
    component: () => import('@/views/ScriptOutlineView.vue'),
    meta: { title: '剧本大纲', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/script/review',
    name: 'script-review',
    component: () => import('@/views/ScriptReviewView.vue'),
    meta: { title: '剧本审核', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/assets',
    name: 'project-assets',
    component: () => import('@/views/AssetWallView.vue'),
    meta: { title: '资产设计', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/assets/characters/:assetId',
    name: 'asset-character-detail',
    component: () => import('@/views/CharacterDetailView.vue'),
    meta: { title: '角色资产详情', requiresAuth: true, projectScoped: true },
  },
  {
    // 音色详情与试听台：唯一 ID 展示、实时试听、样音重新生成与绑定角色
    path: '/projects/:projectId/assets/voices/:voiceId',
    name: 'asset-voice-detail',
    component: () => import('@/views/VoiceDetailView.vue'),
    meta: { title: '音色详情', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/assets/authorizations/new',
    name: 'asset-authorization-new',
    component: () => import('@/views/FaceAuthorizationView.vue'),
    meta: { title: '形象授权', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/storyboard',
    name: 'project-storyboard',
    component: () => import('@/views/StoryboardCanvasView.vue'),
    meta: { title: '分镜画布', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/storyboard/shots/:shotId',
    name: 'storyboard-shot',
    component: () => import('@/views/ShotDrawerView.vue'),
    meta: { title: '镜头详情', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/render-tasks',
    name: 'render-task-center',
    component: () => import('@/views/RenderTaskCenterView.vue'),
    meta: { title: '生成任务中心', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/post',
    name: 'project-post',
    component: () => import('@/views/PostEditorView.vue'),
    meta: { title: '后期编辑器', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/post/review',
    name: 'post-final-review',
    component: () => import('@/views/FinalReviewView.vue'),
    meta: { title: '成片审核', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/distribute',
    name: 'project-distribute',
    component: () => import('@/views/DistributeView.vue'),
    meta: { title: '分发', requiresAuth: true, projectScoped: true },
  },
  {
    // V4.0 能力中心：把 V4 增量能力（版本/导出/模板/资产重抽/变体/节奏/操作历史/
    // 逐角色调参/字幕编辑/混音/导出历史与合规/真人授权撤回）集中一页承载
    path: '/projects/:projectId/v4',
    name: 'project-v4',
    component: () => import('@/views/V4CapabilityView.vue'),
    meta: { title: 'V4 能力中心', requiresAuth: true, projectScoped: true },
  },
  {
    path: '/projects/:projectId/voice',
    name: 'project-voice',
    component: () => import('@/views/VoiceTrackView.vue'),
    meta: { title: '配音轨', requiresAuth: true, projectScoped: true },
  },
  {
    // P3-c 修复：配音轨旧复数路径（/voices）显式重定向到正确页面，
    // 不再落 catch-all 静默跳回项目列表
    path: '/projects/:projectId/voices',
    redirect: (to) => ({ path: to.path.replace(/\/voices$/, '/voice'), query: to.query }),
  },
  {
    // P4-f 修复：审批台旧路径（/approval）显式重定向到审批台（overview Tab 1），
    // 不再落 catch-all 静默跳回项目列表
    path: '/projects/:projectId/approval',
    redirect: (to) => ({ path: to.path.replace(/\/approval$/, '/overview'), query: to.query }),
  },
  {
    // V6.1 F10：单一语义图、结构守卫、双视图、确定性编译与主路径回写
    path: '/projects/:projectId/composition',
    name: 'project-composition',
    component: () => import('@/views/CompositionView.vue'),
    meta: { title: '合成编排', requiresAuth: true, projectScoped: true },
  },
  {
    // V6.2 F11：参数定义/覆盖/版本/审计/合规锁定/重评估
    path: '/projects/:projectId/params',
    name: 'project-params',
    component: () => import('@/views/ParamCenterView.vue'),
    meta: { title: '参数中心', requiresAuth: true, projectScoped: true },
  },
  {
    // V6.3 5A + F2.13：Story Bible、连续性事实、L1/L2 校验与逻辑规则
    path: '/projects/:projectId/continuity',
    name: 'project-continuity',
    component: () => import('@/views/ContinuityView.vue'),
    meta: { title: '连续性与 Story Bible', requiresAuth: true, projectScoped: true },
  },

  /* ---------- 全局功能 ---------- */
  {
    path: '/billing',
    name: 'billing',
    component: () => import('@/views/BillingView.vue'),
    meta: { title: '计费中心', requiresAuth: true, permission: 'billing.view' },
  },
  {
    path: '/settings/team',
    name: 'settings-team',
    component: () => import('@/views/TeamManageView.vue'),
    meta: { title: '团队管理', requiresAuth: true, permission: 'team.manage' },
  },
  {
    path: '/settings/platform-accounts',
    name: 'settings-platform-accounts',
    component: () => import('@/views/PlatformAccountsView.vue'),
    meta: { title: '平台授权', requiresAuth: true, permission: 'platform.manage' },
  },
  {
    // 模型配置：四类模型（文本/图片/语音/视频）接入参数维护、连通性测试与默认路由绑定
    path: '/settings/models',
    name: 'settings-models',
    component: () => import('@/views/ModelSettingsView.vue'),
    meta: { title: '模型配置', requiresAuth: true },
  },
  {
    path: '/notifications',
    name: 'notifications',
    component: () => import('@/views/NotificationsView.vue'),
    meta: { title: '通知中心', requiresAuth: true },
  },
  {
    path: '/settings/profile',
    name: 'settings-profile',
    component: () => import('@/views/ProfileSettingsView.vue'),
    meta: { title: '个人设置', requiresAuth: true },
  },

  /* ---------- 兜底 ---------- */
  { path: '/:pathMatch(.*)*', name: 'not-found', redirect: '/projects' },
]

/** 路由实例（hash 模式无需服务端配置，这里使用 history 模式 + Vite dev 兜底） */
const router = createRouter({
  history: createWebHistory(),
  routes,
})

/**
 * 全局前置守卫：标题 / 鉴权 / 项目上下文 / 未知路径提示。
 * @param to 目标路由
 * @returns true 放行；否则返回重定向位置
 */
router.beforeEach(async (to) => {
  // 页面标题
  document.title = to.meta.title ? `${to.meta.title} · 浮光造梦` : '浮光造梦 · GleamDream Studio'

  // P4-f 伴生修复：未知路径经 catch-all 兜底重定向时给出提示，不再静默
  if (to.redirectedFrom?.name === 'not-found') {
    useToast().info(`页面不存在：${to.redirectedFrom.fullPath}，已返回项目列表`)
  }

  const auth = useAuthStore()

  // 未登录访问受保护页面 → 登录页（记录回跳地址）
  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  // 已登录访问登录 / 注册页 → 工作台
  if (auth.isLoggedIn && (to.name === 'login' || to.name === 'register')) {
    return { path: '/projects' }
  }

  // 项目域路由：确保当前项目已加载
  if (to.meta.projectScoped) {
    const projectId = to.params.projectId
    if (typeof projectId === 'string' && projectId !== '') {
      const projectStore = useProjectStore()
      if (projectStore.currentProject?.id !== projectId) {
        try {
          await projectStore.ensureProject(projectId)
        } catch {
          // 项目不存在 / 无权限：回退项目列表（错误 toast 已由拦截器弹出）
          return { path: '/projects' }
        }
      }
    }
  }

  return true
})

export default router

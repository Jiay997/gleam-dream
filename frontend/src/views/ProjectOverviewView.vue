<!--
  文件级注释：P05 · 项目审批台（路由 /projects/:projectId/overview，AppShell 布局）
  页面身份：HITL 四关口 + 关口 V 的待办审批入口，同时承载项目状态、成本、快照与活动流；
  项目内导航第一项、顶栏面包屑与页面刊眉统一称「审批台」。
  · v7 纸上放映厅 Pro · 对照 F8-02。
  结构：项目头（衬线大标题 + StatusTag + gd-num 元信息；右侧「预算进度」金色细条
  + 「继续创作·去确认关口→」紫钮）
  → 六阶段 Stepper（step-dot 规格：待处理 bg-surface-sunken / 当前 bg-primary-500 白字 /
    完成 bg-gold-fog text-gold-deep border-gold-light；连接线发丝，完成段金）
  → 主体 12 列：左 8 列 HITL 四关口面板 2×2（glyph 方字块 + 阶段 chip + pending 审批单
    计数 + 去确认），右 4 列吸顶栏（成本面板·金家族数字 / 快照面板·回滚胶囊常显 /
    活动流最近 20 条）。
  数据：GET /projects/{id}、GET /projects/{id}/snapshots、GET /orchestration/approvals?status=pending。
  实时：useSSE 订阅 /projects/{id}/events（status_changed/approval.created/progress 等）驱动
  活动流与数据刷新；因 EventSource 无法携带 Authorization 头，另设 12s setInterval 轮询兜底
  （onUnmounted 清理）。三态完整：骨架 / 重试 / 空态。
  v7 删除元素：头部 64px emoji 封面占位块（效果图项目头无封面）。
-->
<script setup lang="ts">
/**
 * ProjectOverviewView 项目审批台（原项目总览）。
 * props：无。emits：无。
 * 依赖 project store（currentProject 同步）、useSSE 实时事件、useToast 轻提示。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { listApprovals, type ApprovalItem } from '@/api/approvals'
import { getProject, listProjectSnapshots, rollbackSnapshot, unarchiveProject } from '@/api/projects'
import type { Project, ProjectSnapshot } from '@/api/types'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import CreditAmountText from '@/components/common/CreditAmountText.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useSSE } from '@/composables/useSSE'
import { useToast } from '@/composables/useToast'
import { HITL_GATES, PROJECT_STAGES, STYLE_OPTIONS, genreLabelOf, stageIndexOf } from '@/constants/options'
import { projectStatusLabel } from '@/constants/statusTone'
import { useProjectStore } from '@/stores/project'
import { formatRelativeTime, firstChar } from '@/utils/datetime'

/** 路由实例（关口 / 环节跳转） */
const route = useRoute()
const router = useRouter()
/** Toast */
const toast = useToast()
/** 项目 store（同步 currentProject） */
const projectStore = useProjectStore()

/** 当前项目 ID（路由参数） */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ---------- 数据状态 ---------- */
/** 项目详情（加载完成后与 store 同步） */
const project = ref<Project | null>(null)
/** 快照列表（最近 10 条展示） */
const snapshots = ref<ProjectSnapshot[]>([])
/** 待办审批单（全类型，供四关口统计） */
const approvals = ref<ApprovalItem[]>([])
/** 首屏加载中 */
const loading = ref(true)
/** 首屏加载失败信息（空串表示正常） */
const errorMsg = ref('')

/* ---------- 活动流（SSE 事件 log） ---------- */
/** 活动条目 */
interface ActivityEntry {
  /** 唯一键（时间 + 序号） */
  key: string
  /** 事件类型 */
  type: string
  /** 展示文案 */
  text: string
  /** 时间（ISO，SSE 实时事件取当前时刻） */
  time: string
}
/** 活动流条目（最新在前，最多 20 条） */
const activities = ref<ActivityEntry[]>([])
/** 活动流自增序号（生成唯一 key） */
let activitySeq = 0

/* ---------- 快照回滚 ---------- */
/** 待回滚快照（null 表示弹窗关闭） */
const rollbackTarget = ref<ProjectSnapshot | null>(null)
/** 回滚弹窗显示状态（computed 双向代理） */
const rollbackDialogOpen = computed({
  get: () => rollbackTarget.value !== null,
  set: (value: boolean) => {
    if (!value) rollbackTarget.value = null
  },
})
/** 回滚提交中 */
const rollbackSaving = ref(false)

/* ---------- 派生数据 ---------- */

/** 预算消耗百分比（budget=0 不限时返回 0） */
const budgetPercent = computed(() => {
  const budget = project.value?.budget_credits ?? 0
  if (budget <= 0) return 0
  return Math.min(100, Math.round(((project.value?.spent_credits ?? 0) / budget) * 100))
})

/** 预算上限文案（0 = 不限预算） */
const budgetText = computed(() => {
  const budget = project.value?.budget_credits ?? 0
  return budget > 0 ? budget.toLocaleString('zh-CN') : '不限'
})

/** 阶段位置信息（14 态 → 六阶段 index + 失败标记） */
const stageInfo = computed(() => stageIndexOf(project.value?.status ?? 'draft', project.value?.config ?? null))

/** 审核态 → 审批台直达路径（关口等待期主任务是确认，CTA 不落到创作工作页） */
const REVIEW_STATUS_PATHS: Record<string, string> = {
  script_review: '/projects/{id}/script/review',
  storyboard_review: '/projects/{id}/storyboard',
  final_review: '/projects/{id}/post/review',
}

/** 「进入当前环节」按钮目标路径（completed 越界时分发页） */
const currentStagePath = computed(() => {
  const status = project.value?.status ?? 'draft'
  const reviewPath = REVIEW_STATUS_PATHS[status]
  if (reviewPath) return reviewPath.replace('{id}', projectId.value)
  const index = Math.min(6, stageInfo.value.index)
  return PROJECT_STAGES[index - 1].pathTemplate.replace('{id}', projectId.value)
})

/** 「进入当前环节」按钮文案 */
const currentStageLabel = computed(() => {
  const status = project.value?.status ?? 'draft'
  if (status === 'completed') return '查看成片分发'
  if (status === 'scripting') return '查看生成进度'
  if (status === 'script_review') return '继续创作 · 去确认关口'
  if (status === 'storyboard_review') return '继续创作 · 去确认关口'
  if (status === 'final_review') return '继续创作 · 去确认关口'
  return '继续创作'
})

/**
 * 计算单个阶段在 Stepper 中的标记：done 已完成 / active 当前 / failed 失败 / todo 未来。
 * @param indexOneBased 阶段序号（1 起）
 * @returns 阶段标记
 */
function stageMark(indexOneBased: number): 'done' | 'active' | 'failed' | 'todo' {
  const current = stageInfo.value.index
  if (indexOneBased < current) return 'done'
  if (indexOneBased === current) return stageInfo.value.failed ? 'failed' : 'active'
  return 'todo'
}

/** 四关口统计：pending 审批单计数 + 关口阶段 */
const gateStats = computed(() => {
  const current = stageInfo.value.index
  return HITL_GATES.map((gate) => {
    const pendingCount = approvals.value.filter((a) => a.approval_type === gate.approvalType).length
    let phase: 'pending' | 'done' | 'active' | 'idle' = 'idle'
    if (pendingCount > 0) {
      phase = 'pending'
    } else if (current > gate.stageIndex || current > 6) {
      phase = 'done'
    } else if (current === gate.stageIndex && current <= 6) {
      phase = 'active'
    }
    return { gate, pendingCount, phase }
  })
})

/**
 * 剧本确认阶段是否缺少 HITL-1 待办。
 * 用于审批台直接提示“审批单缺失/尚未提交”，并提供修复入口，避免用户只能在详情页空态里打转。
 */
const scriptApprovalGap = computed(() => {
  const status = String(project.value?.status ?? '')
  if (!['scripting', 'script_review'].includes(status)) return false
  if (project.value?.current_script_id == null) return false
  const scriptGate = gateStats.value.find((item) => item.gate.key === 'script')
  return (scriptGate?.pendingCount ?? 0) === 0
})

/** 关口阶段中文文案 */
function gatePhaseLabel(phase: 'pending' | 'done' | 'active' | 'idle'): string {
  if (phase === 'pending') return '待确认'
  if (phase === 'done') return '已通过'
  if (phase === 'active') return '进行中'
  return '未开始'
}

/**
 * 关口 glyph 方字块（F8-02：剧 / 资 / 镜 / 发 单字衬线块）。
 * 取「HITL-N 中文」段的第一个字。
 * @param label 关口标签
 * @returns 单字
 */
function gateGlyph(label: string): string {
  const zh = label.split(' ')[1] ?? label
  return firstChar(zh)
}

/**
 * 题材中文标签。
 * @param key 题材 key
 * @returns 中文标签（未设回退）
 */
function genreLabel(key?: string | null): string {
  // 修复（测试报告 #4）：AI 写入的英文枚举（urban_romance 等）经共享兜底映射转中文
  return genreLabelOf(key) ?? '未设题材'
}

/** 画风中文标签 */
const styleLabel = computed(() => STYLE_OPTIONS.find((s) => s.key === project.value?.style_preset_key)?.label ?? '—')

/* ---------- 数据加载 ---------- */

/**
 * 拉取审批台三组数据：项目详情 / 快照 / 待办审批单。
 * @param silent 静默模式（轮询刷新用，不触发骨架与错误块）
 * @returns 无
 */
async function fetchAll(silent = false): Promise<void> {
  if (!silent) {
    loading.value = true
    errorMsg.value = ''
  }
  try {
    const [projectRes, snapshotRes, approvalRes] = await Promise.all([
      getProject(projectId.value),
      listProjectSnapshots(projectId.value).catch(() => ({ items: [] as ProjectSnapshot[], total: 0 })),
      listApprovals({ project_id: projectId.value, status: 'pending', page_size: 100 }).catch(() => ({ items: [] as ApprovalItem[], total: 0 })),
    ])
    project.value = projectRes
    projectStore.setCurrentProject(projectRes)
    snapshots.value = snapshotRes.items.slice(0, 10)
    approvals.value = approvalRes.items
  } catch (error) {
    if (!silent) {
      errorMsg.value = error instanceof Error ? error.message : '加载失败'
    }
  } finally {
    if (!silent) loading.value = false
  }
}

/* ---------- SSE 订阅 ---------- */

/** 需监听的命名事件类型（与后端 event_bus 发布一致） */
const SSE_EVENT_TYPES = ['status_changed', 'progress', 'approval.created', 'run.completed', 'run.failed', 'notification.approval_pending']

/**
 * 把 SSE 事件负载归一化为活动流文案。
 * @param type 事件类型
 * @param data 事件负载（已 JSON.parse，解析失败为 null）
 * @returns 展示文案
 */
function describeEvent(type: string, data: Record<string, unknown> | null): string {
  switch (type) {
    case 'status_changed':
      return `项目状态：${projectStatusLabel(String(data?.from ?? ''))} → ${projectStatusLabel(String(data?.to ?? ''))}`
    case 'progress':
      return `生成进度 ${Number(data?.progress ?? 0)}%：${String(data?.detail ?? data?.step ?? '')}`
    case 'approval.created': {
      const summary = (data?.summary ?? {}) as Record<string, unknown>
      return `新增剧本审批单：${String(summary.script_title ?? '未命名剧本')} v${String(summary.version_no ?? '-')}`
    }
    case 'run.completed': {
      const cost = Number(data?.actual_cost_credits ?? 0)
      return cost > 0 ? `流程执行完成，实际消耗 ${cost} 积分` : '流程执行完成'
    }
    case 'run.failed':
      return `流程执行失败：${String(data?.message ?? '未知原因')}`
    case 'notification.approval_pending':
      return String(data?.title ?? '收到新的待办通知')
    default:
      return `收到事件 ${type}`
  }
}

/**
 * 追加一条活动流（最新在前，裁剪到 20 条）。
 * @param type 事件类型
 * @param data 事件负载
 * @returns 无
 */
function pushActivity(type: string, data: Record<string, unknown> | null): void {
  activitySeq += 1
  activities.value.unshift({ key: `${Date.now()}-${activitySeq}`, type, text: describeEvent(type, data), time: new Date().toISOString() })
  if (activities.value.length > 20) activities.value.length = 20
}

/** 项目级 SSE 订阅（useSSE 内部指数退避重连 + 降级轮询；连接由组件卸载自动清理） */
useSSE(`/api/v1/projects/${projectId.value}/events`, {
  eventTypes: SSE_EVENT_TYPES,
  onMessage: (event) => {
    let data: Record<string, unknown> | null = null
    try {
      data = JSON.parse(event.data) as Record<string, unknown>
    } catch {
      data = null
    }
    pushActivity(event.type, data)
    // 状态变更 / 审批创建：立即静默刷新数据
    if (event.type === 'status_changed' || event.type === 'approval.created' || event.type === 'run.completed' || event.type === 'run.failed') {
      void fetchAll(true)
    }
  },
})

/* ---------- 轮询兜底（SSE 鉴权不可用时保证准实时） ---------- */
/** 轮询定时器句柄 */
let pollTimer: ReturnType<typeof setInterval> | null = null

/* ---------- 快照回滚 ---------- */

/**
 * 确认回滚（POST /projects/{id}/snapshots/{sid}/rollback），成功后刷新全量数据。
 * @returns 无
 */
async function confirmRollback(): Promise<void> {
  const target = rollbackTarget.value
  if (!target) return
  rollbackSaving.value = true
  try {
    await rollbackSnapshot(projectId.value, target.id)
    toast.success('已回滚到所选快照')
    rollbackTarget.value = null
    await fetchAll(true)
  } catch {
    // 拦截器已 toast（40004 已归档 / 10002 快照不存在等）
  } finally {
    rollbackSaving.value = false
  }
}

/* ---------- 导航 ---------- */

/** 项目是否已归档（归档为只读终态，需从审批台显式恢复） */
const isArchived = computed(() => project.value?.status === 'archived')
/** 恢复归档提交中 */
const restoring = ref(false)

/**
 * 恢复归档项目（POST /projects/{id}/unarchive）：给归档项目一个明确出口，
 * 避免从工作台归档后进入只读审批台成为死胡同。
 * @returns 无
 */
async function restoreArchived(): Promise<void> {
  if (restoring.value) return
  restoring.value = true
  try {
    await unarchiveProject(projectId.value)
    toast.success('项目已恢复，可继续创作')
    await fetchAll(true)
  } catch {
    // 拦截器已 toast（20003 非 Owner / 40001 未归档）
  } finally {
    restoring.value = false
  }
}

/**
 * 跳转当前环节页。
 * @returns 无
 */
function goCurrentStage(): void {
  void router.push(currentStagePath.value)
}

/**
 * 关口「去确认」：script 类型跳剧本审批页，其余跳对应阶段主页。
 * @param routeTemplate 关口路由模板
 * @returns 无
 */
function goGate(routeTemplate: string): void {
  void router.push(routeTemplate.replace('{id}', projectId.value))
}

// 挂载：首屏拉取 + 启动轮询兜底
onMounted(() => {
  void fetchAll()
  pollTimer = setInterval(() => {
    void fetchAll(true)
  }, 12_000)
})

// 卸载：清理轮询定时器
onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
})
</script>

<template>
  <div class="mx-auto max-w-content p-6 lg:p-8">
    <!-- ============ 加载骨架 ============ -->
    <div v-if="loading" class="space-y-4">
      <LoadingSkeleton variant="card" />
      <LoadingSkeleton variant="text" :rows="4" />
    </div>

    <!-- ============ 加载失败 ============ -->
    <ErrorRetryBlock v-else-if="errorMsg" message="审批台加载失败" :description="errorMsg" @retry="fetchAll" />

    <!-- ============ 空态（项目不存在） ============ -->
    <EmptyState
      v-else-if="!project"
      icon="🎬"
      title="项目不存在或已删除"
      description="该项目可能已被删除，或当前账号无权访问"
      action-text="返回项目列表"
      @action="router.push('/projects')"
    />

    <!-- ============ 数据区 ============ -->
    <template v-else>
      <!-- ============ 项目头（F8-02：衬线标题 + chip + 元信息；右侧预算进度 + 紫钮） ============ -->
      <header class="flex flex-wrap items-start justify-between gap-x-6 gap-y-4">
        <div class="min-w-0">
          <div class="flex flex-wrap items-center gap-3">
            <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
            <h1 class="gd-masthead truncate text-display-lg" :title="project.title">{{ project.title }}</h1>
            <StatusTag :status="project.status" size="sm" />
          </div>
          <p class="gd-num mt-1.5 truncate text-caption text-neutral-500">
            {{ genreLabel(project.genre) }} · {{ styleLabel }} · {{ project.aspect_ratio || '—' }} · 目标 {{ project.target_episode_count ?? '—' }} 集 · 更新于 {{ formatRelativeTime(project.updated_at) }}
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-5">
          <!-- 预算进度（金色细条） -->
          <div
            class="w-48"
            :title="`预算消耗 ${project.spent_credits ?? 0} / ${project.budget_credits ?? 0} 积分`"
          >
            <div class="flex items-baseline justify-between gap-2">
              <span class="text-caption text-neutral-500">预算进度</span>
              <span class="gd-num inline-flex items-center gap-1 text-xs text-neutral-700">
                <CreditAmountText :amount="project.spent_credits ?? 0" :colored="false" class="text-xs" />
                / {{ budgetText }}
              </span>
            </div>
            <div
              class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-sunken"
              role="progressbar"
              :aria-valuenow="budgetPercent"
              aria-valuemin="0"
              aria-valuemax="100"
              aria-label="预算消耗进度"
            >
              <div
                class="h-full rounded-full bg-gradient-to-r from-gold to-gold-light transition-[width] duration-gd-slow"
                :class="budgetPercent >= 90 ? 'from-danger to-danger' : ''"
                :style="{ width: `${budgetPercent}%` }"
              />
            </div>
          </div>
          <AppButton
            v-if="isArchived"
            variant="primary"
            size="md"
            :loading="restoring"
            title="恢复归档后可继续创作"
            @click="restoreArchived"
          >
            恢复归档
          </AppButton>
          <AppButton v-else variant="primary" size="md" @click="goCurrentStage">{{ currentStageLabel }} →</AppButton>
        </div>
      </header>

      <!-- ============ 六阶段 Stepper（step-dot：sunken / 紫当前 / 金完成，发丝连接线） ============ -->
      <nav class="gd-card mt-6 flex items-center px-5 py-4" aria-label="创作流程">
        <template v-for="(stage, index) in PROJECT_STAGES" :key="stage.key">
          <button
            type="button"
            class="flex items-center gap-2"
            :class="stageMark(index + 1) === 'todo' ? 'cursor-default' : ''"
            @click="stageMark(index + 1) !== 'todo' && goGate(stage.pathTemplate)"
          >
            <span
              class="gd-num flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-xs font-semibold transition-colors duration-gd-normal"
              :class="{
                'border-gold-light bg-gold-fog text-gold-deep': stageMark(index + 1) === 'done',
                'border-primary-500 gd-fill-brand shadow-gd-1': stageMark(index + 1) === 'active',
                'border-danger bg-danger-bg text-danger': stageMark(index + 1) === 'failed',
                'border-transparent bg-surface-sunken text-neutral-400': stageMark(index + 1) === 'todo',
              }"
              :aria-current="stageMark(index + 1) === 'active' ? 'step' : undefined"
            >
              <template v-if="stageMark(index + 1) === 'done'">✓</template>
              <template v-else-if="stageMark(index + 1) === 'failed'">✕</template>
              <template v-else>{{ index + 1 }}</template>
            </span>
            <span
              class="text-sm"
              :class="{
                'font-medium text-neutral-900': stageMark(index + 1) === 'active',
                'text-gold-deep': stageMark(index + 1) === 'done',
                'text-danger font-medium': stageMark(index + 1) === 'failed',
                'text-neutral-400': stageMark(index + 1) === 'todo',
              }"
            >
              {{ stage.label }}
            </span>
          </button>
          <!-- 连接线：发丝；已完成段金 -->
          <div v-if="index < PROJECT_STAGES.length - 1" class="mx-3 h-px flex-1" :class="index + 1 < stageInfo.index ? 'bg-gold' : 'bg-hairline'" />
        </template>
      </nav>

      <!-- ============ 主体：左 8 列关口 + 右 4 列吸顶 ============ -->
      <div class="mt-8 grid grid-cols-1 gap-5 xl:grid-cols-12">
        <!-- 左：HITL 四关口 2×2 -->
        <section class="xl:col-span-8" aria-label="审批台">
          <!-- 页面身份刊眉：让用户明确当前处于「审批台」，而不是笼统的项目总览 -->
          <div class="mb-4 flex flex-wrap items-end justify-between gap-2">
            <div class="min-w-0">
              <h2 class="font-serif text-display-lg text-neutral-900">审批台</h2>
              <p class="mt-1 text-caption leading-relaxed text-neutral-500">
                HITL 关口待办集中处理：确认后流程自动推进到下一环节；待办为空时可在右侧查看项目状态与快照。
              </p>
            </div>
            <span class="gd-section-label shrink-0">APPROVAL DESK</span>
          </div>

          <!-- 剧本确认阶段缺少 HITL-1 待办：直接给出修复入口，避免“审批台 ↔ 空态”循环 -->
          <div
            v-if="scriptApprovalGap"
            class="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-warning-border bg-warning-bg px-4 py-3"
            role="status"
          >
            <p class="min-w-0 flex-1 text-caption leading-relaxed text-warning-strong">
              剧本已进入确认阶段，但暂未找到 HITL-1 待办：可能是审批单被清理或由旧流程未创建。
              进入剧本审核页可查看具体原因并一键补齐审批单。
            </p>
            <AppButton variant="secondary" size="sm" @click="goGate('/projects/{id}/script/review')">
              前往剧本审核页修复 →
            </AppButton>
          </div>

          <div class="grid grid-cols-1 gap-5 md:grid-cols-2">
            <article
              v-for="item in gateStats"
              :key="item.gate.key"
              class="gd-card gd-card-interactive flex flex-col p-6"
              :class="item.phase === 'pending' ? 'border-warning-border' : ''"
            >
              <div class="flex items-start justify-between gap-3">
                <div class="flex min-w-0 items-center gap-3">
                  <!-- glyph 方字块（剧 / 资 / 镜 / 发） -->
                  <span class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-surface-sunken font-serif text-base text-neutral-700" aria-hidden="true">
                    {{ gateGlyph(item.gate.label) }}
                  </span>
                  <h3 class="truncate text-sm font-semibold text-neutral-900">{{ item.gate.label }}</h3>
                </div>
                <!-- 关口状态标签（待确认黄 / 已通过绿 / 进行中蓝 / 未开始灰） -->
                <span
                  class="inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium"
                  :class="{
                    'border-warning-border bg-warning-bg text-gd-pending': item.phase === 'pending',
                    'border-success-border bg-success-bg text-success': item.phase === 'done',
                    'border-gd-running-border bg-gd-running-bg text-gd-running': item.phase === 'active',
                    'border-hairline bg-surface-sunken text-neutral-500': item.phase === 'idle',
                  }"
                >
                  <span class="h-1.5 w-1.5 rounded-full" :class="{ 'bg-gd-pending': item.phase === 'pending', 'bg-success': item.phase === 'done', 'bg-gd-running': item.phase === 'active', 'bg-neutral-400': item.phase === 'idle' }" aria-hidden="true" />
                  {{ gatePhaseLabel(item.phase) }}
                </span>
              </div>
              <p class="mt-3 text-xs leading-relaxed text-neutral-500">
                <template v-if="item.phase === 'pending'">有 {{ item.pendingCount }} 单待确认，请及时处理以免超时过期</template>
                <template v-else-if="item.phase === 'done'">关口已确认通过，流程继续推进</template>
                <template v-else-if="item.phase === 'active'">当前环节进行中，完成后将在此等待确认</template>
                <template v-else>尚未到达该关口</template>
              </p>
              <div class="mt-auto flex items-center justify-between border-t border-hairline-soft pt-3">
                <span class="gd-num text-xs text-neutral-400">{{ item.pendingCount > 0 ? `${item.pendingCount} 单待办` : '无待办' }}</span>
                <AppButton :variant="item.phase === 'pending' ? 'primary' : 'secondary'" size="sm" @click="goGate(item.gate.routeTemplate)">
                  {{ item.phase === 'pending' ? '去确认关口 →' : '查看' }}
                </AppButton>
              </div>
            </article>
          </div>

          <!-- 关口 V（配音确认）：V6.0 生产链路为「剧本 → 配音 → 分镜 → 视频」，
               四关口面板不含配音，单独提供入口，避免用户漏掉配音先行环节。 -->
          <div class="gd-card mt-5 flex flex-wrap items-center justify-between gap-3 p-5">
            <div class="flex min-w-0 items-center gap-3">
              <span class="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-surface-sunken font-serif text-base text-neutral-700" aria-hidden="true">V</span>
              <div class="min-w-0">
                <h3 class="text-sm font-semibold text-neutral-900">HITL-5 配音确认（关口 V）</h3>
                <p class="mt-0.5 text-caption leading-relaxed text-neutral-500">
                  台词产出后先配音，再进入分镜台词绑定与视频生成；通过关口 V 后整集音轨锁定。
                </p>
              </div>
            </div>
            <AppButton variant="secondary" size="sm" @click="goGate('/projects/{id}/voice')">前往配音轨 →</AppButton>
          </div>

          <!-- 无任何待办的引导空态（嵌在左栏底部，不遮挡关口面板） -->
          <div v-if="approvals.length === 0" class="mt-5 rounded-lg border border-dashed border-hairline bg-surface-raised px-5 py-4 text-center text-xs text-neutral-400">
            当前没有待确认的审批单——四关口将在对应环节完成后自动出现待办
          </div>
        </section>

        <!-- 右：吸顶侧栏 -->
        <aside class="xl:col-span-4">
          <div class="space-y-5 xl:sticky xl:top-6">
            <!-- 成本面板（F8-02：金家族数字 + 金渐变进度条） -->
            <section class="gd-card p-6">
              <div class="flex items-center justify-between">
                <h3 class="text-sm font-semibold text-neutral-900">成本</h3>
                <span class="text-caption text-neutral-400">预算感知</span>
              </div>
              <div class="mt-3 flex items-end justify-between gap-3">
                <div>
                  <p class="text-caption text-neutral-400">已消耗</p>
                  <CreditAmountText :amount="project.spent_credits ?? 0" :colored="false" class="gd-num text-2xl" />
                </div>
                <div class="text-right">
                  <p class="text-caption text-neutral-400">预估总额</p>
                  <p class="gd-num text-xl text-gold-deep">约 {{ budgetText }}<template v-if="(project.budget_credits ?? 0) > 0"> 积分</template></p>
                </div>
              </div>
              <div
                class="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-sunken"
                role="progressbar"
                :aria-valuenow="budgetPercent"
                aria-valuemin="0"
                aria-valuemax="100"
                aria-label="预算消耗进度"
              >
                <div
                  class="h-full rounded-full bg-gradient-to-r from-gold to-gold-light transition-[width] duration-gd-slow"
                  :class="budgetPercent >= 90 ? 'from-danger to-danger' : ''"
                  :style="{ width: `${budgetPercent}%` }"
                />
              </div>
              <p class="gd-num mt-2 text-xs" :class="budgetPercent >= 90 ? 'text-danger' : 'text-neutral-400'">
                已消耗 {{ budgetPercent }}%<template v-if="budgetPercent >= 90">，接近预算上限</template>
              </p>
            </section>

            <!-- 快照面板（F8-02：标题行 + 回滚胶囊常显） -->
            <section class="gd-card p-6">
              <div class="flex items-center justify-between">
                <h3 class="text-sm font-semibold text-neutral-900">快照</h3>
                <span class="gd-num text-caption text-neutral-400">最近 {{ snapshots.length }} 条</span>
              </div>
              <ul v-if="snapshots.length > 0" class="mt-1 divide-y divide-hairline-soft">
                <li v-for="snapshot in snapshots" :key="snapshot.id" class="flex items-center gap-3 py-3">
                  <div class="min-w-0 flex-1">
                    <p class="truncate text-sm text-neutral-800" :title="snapshot.label ?? snapshot.trigger ?? ''">
                      {{ snapshot.label || snapshot.trigger || '自动快照' }}
                    </p>
                    <p class="gd-num mt-0.5 truncate text-caption text-neutral-400">
                      s-{{ snapshot.sequence_no ?? snapshot.id }} · {{ formatRelativeTime(snapshot.created_at) }}
                    </p>
                  </div>
                  <AppButton variant="secondary" size="sm" @click="rollbackTarget = snapshot">回滚</AppButton>
                </li>
              </ul>
              <p v-else class="mt-3 py-4 text-center text-xs text-neutral-400">暂无快照——关键环节完成后将自动留存</p>
            </section>

            <!-- 活动流面板 -->
            <section class="gd-card p-6">
              <div class="flex items-center justify-between">
                <h3 class="text-sm font-semibold text-neutral-900">活动流</h3>
                <span class="gd-num text-caption text-neutral-400">最近 {{ activities.length }} 条</span>
              </div>
              <ul v-if="activities.length > 0" class="mt-3 max-h-72 space-y-2 overflow-y-auto pr-1">
                <li v-for="entry in activities" :key="entry.key" class="flex gap-2.5">
                  <span class="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full" :class="entry.type === 'run.failed' ? 'bg-danger' : entry.type === 'approval.created' ? 'bg-gd-pending' : 'bg-primary-400'" aria-hidden="true" />
                  <div class="min-w-0 flex-1">
                    <p class="text-xs leading-relaxed text-neutral-700" :class="entry.type === 'run.failed' ? 'text-danger' : ''">{{ entry.text }}</p>
                    <p class="gd-num mt-0.5 text-caption text-neutral-400">{{ formatRelativeTime(entry.time) }}</p>
                  </div>
                </li>
              </ul>
              <div v-else class="py-6 text-center">
                <p class="text-xs text-neutral-400">暂无动态</p>
                <p class="mt-1 text-caption text-neutral-400">项目事件（状态流转 / 审批 / 生成进度）将在此实时滚动</p>
              </div>
            </section>
          </div>
        </aside>
      </div>
    </template>

    <!-- ============ 回滚二次确认 ============ -->
    <ConfirmDialog
      v-model="rollbackDialogOpen"
      title="确认回滚到该快照？"
      :content="`回滚后项目状态将恢复到「${rollbackTarget?.label || rollbackTarget?.trigger || `#${rollbackTarget?.sequence_no ?? ''}`}」创建时的样子，当前进度之后的变更将不可见（可再次回滚恢复）。`"
      confirm-label="确认回滚"
      :loading="rollbackSaving"
      @confirm="confirmRollback"
      @cancel="rollbackTarget = null"
    />
  </div>
</template>

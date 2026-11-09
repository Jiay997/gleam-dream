<!--
  文件级注释：V4.0 生成仪表盘与人工干预面板（F5-07~F5-10）。

  承载的后端能力（`api/v4.ts`）：
  - `getRenderDashboard`：六态计数 / 总进度 / ETA（波动标注）/ 实时消耗（AC-F5-07-1~2）；
  - `manualPassTask`：人工标记合格（须填 ≥5 字理由，强制通过留痕，AC-F5-09-1~3）；
  - `abandonRenderTask`：放弃并全额返还（AC-F5-04-3）；
  - `batchRetryRender`：失败批量重试（支持延迟重试，AC-F5-08-1~2）；
  - `lockEpisodeVersions`：按集一键锁定镜头版本（AC-F5-10-1~2）；
  - `jumpRenderQueue`：单镜头插队（每批次免费 3 次，AC-F5-01-3）。

  数据来源：`renderTasks` 由父视图以 v-model 传入（复用已有列表，避免重复拉取）；
  失败任务展示**用户语言文案**（`user_language_error`，AC-F5-02-4）与可折叠原始错误码。
-->
<script setup lang="ts">
/**
 * RenderDashboardPanel 生成仪表盘与人工干预面板。
 *
 * props：
 * - projectId：项目 ID（必填，用于仪表盘与按集锁定）；
 * - tasks：父视图的渲染任务列表（用于失败/待人工清单）；
 * - batchKey：当前批次键（插队用，可空）；
 * - episodeId：集 ID（提供时展示「锁定本集全部镜头」）。
 * emit：
 * - refresh：请求父视图刷新任务列表。
 */
import { computed, onMounted, ref, watch } from 'vue'

import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import Modal from '@/components/common/Modal.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import {
  abandonRenderTask,
  batchRetryRender,
  getRenderDashboard,
  jumpRenderQueue,
  lockEpisodeVersions,
  manualPassTask,
} from '@/api/v4'
import type { RenderDashboard } from '@/api/v4'

/** 组件 props */
const props = withDefaults(
  defineProps<{
    /** 项目 ID */
    projectId: string | number
    /** 渲染任务列表（父视图提供） */
    tasks?: Array<Record<string, unknown>>
    /** 当前批次键（插队用） */
    batchKey?: string
    /** 集 ID（按集锁定入口） */
    episodeId?: number | string | null
  }>(),
  { tasks: () => [], batchKey: '', episodeId: null },
)

/** 组件事件 */
const emit = defineEmits<{
  /** 请求刷新任务列表 */
  refresh: []
}>()

const toast = useToast()

/** 仪表盘数据 */
const dashboard = ref<RenderDashboard | null>(null)
/** 仪表盘加载中 */
const loading = ref(false)

/** 六态展示定义（顺序即展示顺序） */
const STATE_ITEMS = [
  { key: 'queued', label: '排队', tone: 'pending' },
  { key: 'running', label: '生成中', tone: 'running' },
  { key: 'qc_review', label: '质检中', tone: 'running' },
  { key: 'succeeded', label: '成功', tone: 'locked' },
  { key: 'failed', label: '失败', tone: 'failed' },
  { key: 'manual_required', label: '待人工', tone: 'blocked' },
] as const

/** 失败/待人工任务（可干预清单） */
const exceptionTasks = computed(() =>
  (props.tasks as Array<Record<string, unknown>>).filter((task) => {
    const status = String(task.status ?? '')
    return status === 'failed' || status === 'manual_required' || status === 'moderation_rejected'
  }),
)

/** 多选中的任务 ID（批量重试用） */
const selectedIds = ref<number[]>([])

/** 人工合格弹窗 */
const passDialogVisible = ref(false)
/** 当前操作的任务 ID */
const activeTaskId = ref<number | null>(null)
/** 放行理由 */
const passReason = ref('')
/** 延迟重试分钟数 */
const delayMinutes = ref(0)
/** 提交中标记 */
const submitting = ref(false)

/**
 * 拉取仪表盘数据。
 * @returns 无
 */
async function loadDashboard(): Promise<void> {
  loading.value = true
  try {
    dashboard.value = await getRenderDashboard(props.projectId)
  } finally {
    loading.value = false
  }
}

/**
 * ETA 文案：波动大时提示「波动较大」不给精确承诺（AC-F5-07-2）。
 * @returns ETA 展示文案
 */
const etaText = computed(() => {
  const data = dashboard.value
  if (!data) return '—'
  if (data.eta_seconds === null) return data.eta_hint
  const minutes = Math.ceil(data.eta_seconds / 60)
  return data.eta_volatile ? `约 ${minutes} 分钟（波动较大）` : `约 ${minutes} 分钟`
})

/**
 * 勾选/取消某个任务。
 * @param taskId 任务 ID
 * @returns 无
 */
function toggleSelect(taskId: number): void {
  const index = selectedIds.value.indexOf(taskId)
  if (index >= 0) selectedIds.value.splice(index, 1)
  else selectedIds.value.push(taskId)
}

/**
 * 打开人工合格弹窗。
 * @param taskId 任务 ID
 * @returns 无
 */
function openPassDialog(taskId: number): void {
  activeTaskId.value = taskId
  passReason.value = ''
  passDialogVisible.value = true
}

/**
 * 确认人工标记合格。
 * @returns 无
 */
async function confirmManualPass(): Promise<void> {
  if (activeTaskId.value === null) return
  if (passReason.value.trim().length < 5) {
    toast.error('放行理由不少于 5 字')
    return
  }
  submitting.value = true
  try {
    await manualPassTask(activeTaskId.value, passReason.value.trim())
    toast.success('已人工标记合格（已写入审计）')
    passDialogVisible.value = false
    await loadDashboard()
    emit('refresh')
  } finally {
    submitting.value = false
  }
}

/**
 * 放弃选定任务（全额返还）。
 * @param taskId 任务 ID
 * @returns 无
 */
async function abandon(taskId: number): Promise<void> {
  submitting.value = true
  try {
    const result = await abandonRenderTask(taskId)
    toast.success(`已放弃，返还 ${result.refunded_credits} 积分`)
    await loadDashboard()
    emit('refresh')
  } finally {
    submitting.value = false
  }
}

/**
 * 批量重试选定失败任务。
 * @returns 无
 */
async function retrySelected(): Promise<void> {
  if (selectedIds.value.length === 0) {
    toast.warning('请先勾选需要重试的镜头任务')
    return
  }
  submitting.value = true
  try {
    const result = await batchRetryRender(selectedIds.value, delayMinutes.value)
    const skippedCount = result.skipped.length
    toast.success(
      delayMinutes.value > 0
        ? `已登记 ${result.delayed_task_ids.length} 个任务的延迟重试（${delayMinutes.value} 分钟）`
        : `已重试 ${result.accepted.length} 个任务${skippedCount ? `，跳过 ${skippedCount} 个` : ''}`,
    )
    selectedIds.value = []
    await loadDashboard()
    emit('refresh')
  } finally {
    submitting.value = false
  }
}

/**
 * 单镜头插队。
 * @param taskId 任务 ID
 * @returns 无
 */
async function jump(taskId: number): Promise<void> {
  if (!props.batchKey) {
    toast.warning('缺少批次键，无法插队')
    return
  }
  submitting.value = true
  try {
    const result = await jumpRenderQueue(props.batchKey, taskId)
    toast.success(`已插队至队首（本批次已用 ${result.jump_used}/${result.free_quota} 次）`)
    emit('refresh')
  } finally {
    submitting.value = false
  }
}

/** 集 ID（可选；无则不展示按集锁定入口） */
const episodeId = computed(() => props.episodeId ?? null)

/**
 * 按集锁定镜头版本。
 * @returns 无
 */
async function lockEpisode(): Promise<void> {
  if (!episodeId.value) {
    toast.warning('缺少集信息，无法按集锁定')
    return
  }
  submitting.value = true
  try {
    const result = await lockEpisodeVersions(Number(episodeId.value))
    const skipped = result.skipped?.length ?? 0
    toast.success(
      `已锁定 ${result.locked.length} 个镜头${skipped ? `，${skipped} 个无生成版本已跳过` : ''}`,
    )
    emit('refresh')
  } finally {
    submitting.value = false
  }
}

watch(() => props.projectId, loadDashboard)
onMounted(loadDashboard)
</script>

<template>
  <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
    <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-title text-neutral-900">生成总览</h3>
      <div class="flex items-center gap-2">
        <AppButton variant="ghost" size="sm" :loading="loading" @click="loadDashboard">刷新</AppButton>
        <AppButton v-if="episodeId" variant="secondary" size="sm" :loading="submitting" @click="lockEpisode">
          锁定本集全部镜头
        </AppButton>
      </div>
    </header>

    <div v-if="dashboard" class="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      <button
        v-for="item in STATE_ITEMS"
        :key="item.key"
        type="button"
        class="rounded-md border border-hairline bg-surface-inset px-3 py-2 text-left transition hover:border-hairline-strong"
      >
        <div class="text-caption text-neutral-500">{{ item.label }}</div>
        <div class="mt-1 text-title tabular-nums text-neutral-900">{{ dashboard.counts[item.key] ?? 0 }}</div>
      </button>
    </div>

    <div v-if="dashboard" class="mt-4 grid gap-3 sm:grid-cols-3">
      <div class="rounded-md border border-hairline bg-surface-inset px-3 py-2">
        <div class="text-caption text-neutral-500">总进度</div>
        <div class="mt-1 flex items-center gap-2">
          <div class="h-1.5 flex-1 overflow-hidden rounded-full bg-neutral-200">
            <div class="h-full rounded-full bg-info" :style="{ width: `${dashboard.progress_percent}%` }" />
          </div>
          <span class="text-body tabular-nums text-neutral-900">{{ dashboard.progress_percent }}%</span>
        </div>
      </div>
      <div class="rounded-md border border-hairline bg-surface-inset px-3 py-2">
        <div class="text-caption text-neutral-500">预计剩余</div>
        <div class="mt-1 text-body text-neutral-900">{{ etaText }}</div>
      </div>
      <div class="rounded-md border border-hairline bg-surface-inset px-3 py-2">
        <div class="text-caption text-neutral-500">实时消耗 / 可用</div>
        <div class="mt-1 text-body tabular-nums text-neutral-900">
          {{ dashboard.consumed_credits }} / {{ dashboard.available_credits }} 积分
        </div>
      </div>
    </div>

    <div class="mt-5">
      <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h4 class="text-subtitle text-neutral-900">待处理（失败 / 待人工）</h4>
        <div class="flex items-center gap-2">
          <label class="flex items-center gap-1 text-caption text-neutral-600">
            延迟
            <select v-model.number="delayMinutes" class="rounded border border-hairline bg-surface-canvas px-1 py-0.5">
              <option :value="0">立即</option>
              <option :value="15">15 分钟</option>
              <option :value="30">30 分钟</option>
              <option :value="60">60 分钟</option>
            </select>
          </label>
          <AppButton variant="secondary" size="sm" :loading="submitting" @click="retrySelected">
            重试所选（{{ selectedIds.length }}）
          </AppButton>
        </div>
      </div>

      <EmptyState
        v-if="exceptionTasks.length === 0"
        title="全部处理完毕 ✓"
        description="当前没有失败或待人工的镜头任务。"
        icon-path="M4.5 12.75l6 6 9-13.5"
      />
      <ul v-else class="space-y-2">
        <li
          v-for="task in exceptionTasks"
          :key="String(task.id)"
          class="rounded-md border border-hairline bg-surface-inset p-3"
        >
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2">
              <input
                type="checkbox"
                class="h-4 w-4"
                :checked="selectedIds.includes(Number(task.id))"
                @change="toggleSelect(Number(task.id))"
              />
              <span class="text-body text-neutral-900">
                镜头 {{ (task.shot as Record<string, unknown> | null)?.shot_no ?? task.shot_id ?? '—' }}
              </span>
              <StatusTag :status="String(task.status)" size="sm" />
              <span v-if="task.manual_pass" class="text-caption text-neutral-500">已强制通过</span>
            </div>
            <div class="flex items-center gap-2">
              <AppButton variant="text" size="sm" @click="jump(Number(task.id))">插队</AppButton>
              <AppButton variant="secondary" size="sm" @click="openPassDialog(Number(task.id))">
                人工标记合格
              </AppButton>
              <AppButton variant="danger" size="sm" @click="abandon(Number(task.id))">放弃</AppButton>
            </div>
          </div>
          <p v-if="task.user_language_error" class="mt-2 text-caption text-danger-strong">
            {{ task.user_language_error }}
          </p>
          <details v-if="task.failure_reason" class="mt-1">
            <summary class="cursor-pointer text-caption text-neutral-500">查看原始错误</summary>
            <pre class="mt-1 whitespace-pre-wrap break-all text-caption text-neutral-500">{{
              task.failure_reason
            }}</pre>
          </details>
        </li>
      </ul>
    </div>

    <Modal v-model="passDialogVisible" title="人工标记合格" :max-width="480">
      <p class="text-caption leading-relaxed text-neutral-600">
        该操作会强制通过质检并进入正常结算，原始质检得分保留，操作将写入审计日志。
        <strong class="text-neutral-900">同一镜头仅可强制通过一次。</strong>
      </p>
      <label class="mt-4 block">
        <span class="text-caption text-neutral-600">放行理由（≥5 字，必填）</span>
        <textarea
          v-model="passReason"
          rows="3"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
          placeholder="例如：质检误判，画面人物五官清晰正常"
        />
      </label>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="passDialogVisible = false">取消</AppButton>
          <AppButton variant="primary" :loading="submitting" @click="confirmManualPass">确认放行</AppButton>
        </div>
      </template>
    </Modal>
  </section>
</template>

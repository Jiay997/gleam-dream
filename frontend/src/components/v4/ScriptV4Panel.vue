<!--
  文件级注释：V4.0 剧本域面板（F2-08 续写 / F2-09 模板 / F2-10 导出 / F2-11 版本 /
  F2-12 生成终止）。

  承载的后端能力（`api/v4.ts`）：
  - 版本列表 / 命名备注 / 历史版本恢复（AC-F2-11-1~2）；
  - 剧本导出 md/docx/txt，触发浏览器下载（AC-F2-10-1~3）；
  - 续写 N 集（集数上限 30 由后端 10001 拦截，AC-F2-08-1~3）；
  - 组合模板包列表与套用（AC-F2-09-1~3）；
  - 生成任务进度与**终止生成**（保留已完成集、按集结算返还，AC-F2-12-1~3）。

  props：`projectId`（必填）。emit：`reload`（请求父视图重新拉取剧本树）。
-->
<script setup lang="ts">
/**
 * ScriptV4Panel 剧本域 V4 能力面板。
 *
 * props：
 * - projectId：项目 ID；
 * - episodeCount：当前总集数（续写上限提示用）。
 * emit：
 * - reload：请求父视图重新加载剧本树。
 */
import { computed, onMounted, ref } from 'vue'

import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import Modal from '@/components/common/Modal.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import {
  applyScriptPack,
  cancelScriptRun,
  continueScript,
  exportScript,
  labelScriptVersion,
  listScriptPacks,
  listScriptRuns,
  listScriptVersions,
  restoreScriptVersion,
} from '@/api/v4'
import type { ScriptRunSummary, ScriptTemplatePack, ScriptVersionSummary } from '@/api/v4'

/** 组件 props */
const props = withDefaults(
  defineProps<{
    /** 项目 ID */
    projectId: string | number
    /** 当前总集数 */
    episodeCount?: number
  }>(),
  { episodeCount: 0 },
)

/** 组件事件 */
const emit = defineEmits<{
  /** 请求重新加载剧本树 */
  reload: []
}>()

const toast = useToast()

/** 版本列表 */
const versions = ref<ScriptVersionSummary[]>([])
/** 版本上限 */
const versionLimit = ref(30)
/** 模板包 */
const packs = ref<ScriptTemplatePack[]>([])
/** 生成任务 */
const runs = ref<ScriptRunSummary[]>([])
/** 加载中 */
const loading = ref(false)
/** 提交中 */
const submitting = ref(false)

/** 恢复确认弹窗 */
const restoreVisible = ref(false)
/** 待恢复版本 */
const restoreTarget = ref<ScriptVersionSummary | null>(null)
/** 恢复备注 */
const restoreLabel = ref('')

/** 续写弹窗 */
const continueVisible = ref(false)
/** 续写集数 */
const continueCount = ref(1)
/** 续写要点 */
const continueOutline = ref('')

/** 当前运行中任务 */
const activeRun = computed(() => runs.value.find((run) => run.run_status === 'generating') ?? null)

/**
 * 当前总集数（D31 修复）：父组件 episodeCount 未传时从版本列表兜底，
 * 避免续写弹窗误显示「当前 0 集」。
 */
const currentEpisodeCount = computed(() => {
  if (props.episodeCount > 0) return props.episodeCount
  const latest = versions.value.reduce<ScriptVersionSummary | null>(
    (acc, v) => (acc === null || v.version_no > acc.version_no ? v : acc),
    null,
  )
  return latest?.total_episodes ?? 0
})

/** 集数上限提示 */
const episodeLimitHint = computed(
  () => `当前 ${currentEpisodeCount.value} 集，单项目上限 30 集；续写后总集数不得超过 30。`,
)

/**
 * 加载版本、模板与运行态。
 * @returns 无
 */
async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [versionPayload, packPayload, runPayload] = await Promise.all([
      listScriptVersions(props.projectId).catch(() => null),
      listScriptPacks().catch(() => null),
      listScriptRuns(props.projectId).catch(() => null),
    ])
    versions.value = versionPayload?.items ?? []
    versionLimit.value = versionPayload?.version_limit ?? 30
    packs.value = packPayload?.items ?? []
    runs.value = runPayload?.items ?? []
  } finally {
    loading.value = false
  }
}

/**
 * 导出剧本（md/docx/txt）。
 * @param format 导出格式
 * @returns 无
 */
async function doExport(format: 'md' | 'docx' | 'txt'): Promise<void> {
  submitting.value = true
  try {
    await exportScript(props.projectId, format)
    toast.success(`已开始下载 ${format.toUpperCase()} 文件`)
  } finally {
    submitting.value = false
  }
}

/**
 * 打开恢复确认。
 * @param version 目标版本
 * @returns 无
 */
function openRestore(version: ScriptVersionSummary): void {
  restoreTarget.value = version
  restoreLabel.value = `恢复自 v${version.version_no}`
  restoreVisible.value = true
}

/**
 * 确认恢复历史版本。
 * @returns 无
 */
async function confirmRestore(): Promise<void> {
  if (!restoreTarget.value) return
  submitting.value = true
  try {
    const result = await restoreScriptVersion(
      props.projectId,
      restoreTarget.value.id,
      restoreLabel.value || undefined,
    )
    toast.success(`已恢复 v${result.restored_from} 为新版本 v${result.script.version_no}`)
    restoreVisible.value = false
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 修改版本命名备注。
 * @param version 目标版本
 * @returns 无
 */
async function renameVersion(version: ScriptVersionSummary): Promise<void> {
  const label = window.prompt('版本备注（≤50 字）', version.version_label ?? '')
  if (label === null) return
  submitting.value = true
  try {
    await labelScriptVersion(props.projectId, version.id, label.slice(0, 50))
    toast.success('版本备注已更新')
    await loadAll()
  } finally {
    submitting.value = false
  }
}

/**
 * 套用组合模板包。
 * @param pack 模板包
 * @returns 无
 */
async function applyPack(pack: ScriptTemplatePack): Promise<void> {
  submitting.value = true
  try {
    const result = await applyScriptPack(props.projectId, pack.template_id)
    toast.success(`已套用「${pack.name}」；生成参数已回填（可覆盖）`)
    if (result.deviated_fields.length > 0) {
      toast.warning(`以下字段已偏离预设：${result.deviated_fields.join('、')}`)
    }
  } finally {
    submitting.value = false
  }
}

/**
 * 终止生成（保留已完成集，按集结算返还）。
 * @returns 无
 */
async function cancelActiveRun(): Promise<void> {
  if (!activeRun.value) return
  const confirmed = window.confirm(
    `将保留已完成 ${activeRun.value.completed_episodes} 集，未执行部分解冻返还。确认终止？`,
  )
  if (!confirmed) return
  submitting.value = true
  try {
    const result = await cancelScriptRun(props.projectId, activeRun.value.run_id)
    toast.success(
      `已终止：保留 ${result.retained_episodes} 集，结算 ${result.settled_credits} 积分，返还 ${result.refunded_credits} 积分`,
    )
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 提交续写。
 * @returns 无
 */
async function submitContinue(): Promise<void> {
  if (continueOutline.value.trim().length < 5) {
    toast.warning('续写要点不少于 5 字')
    return
  }
  submitting.value = true
  try {
    // D31 修复：后端 202 后台受理（LLM 续写可达 80 秒+），前端轮询版本列表感知结果
    const baseVersion = versions.value.reduce((max, v) => Math.max(max, v.version_no), 0)
    await continueScript(props.projectId, continueCount.value, continueOutline.value.trim())
    toast.info(`续写任务已受理，正在后台生成（约 1~2 分钟）…`)
    continueVisible.value = false
    continueOutline.value = ''
    for (let i = 0; i < 36; i++) {
      await new Promise((resolve) => setTimeout(resolve, 5000))
      const payload = await listScriptVersions(props.projectId).catch(() => null)
      if ((payload?.items ?? []).some((v) => v.version_no > baseVersion)) {
        toast.success('续写完成，已生成新版本')
        await loadAll()
        emit('reload')
        return
      }
    }
    toast.warning('续写仍在后台进行，可稍后点击「刷新」查看结果')
  } finally {
    submitting.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
    <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-title text-neutral-900">剧本版本与导出</h3>
      <div class="flex flex-wrap items-center gap-2">
        <AppButton variant="ghost" size="sm" :loading="loading" @click="loadAll">刷新</AppButton>
        <AppButton variant="ghost" size="sm" :loading="submitting" @click="doExport('md')">导出 MD</AppButton>
        <AppButton variant="ghost" size="sm" :loading="submitting" @click="doExport('txt')">导出 TXT</AppButton>
        <AppButton variant="ghost" size="sm" :loading="submitting" @click="doExport('docx')">导出 DOCX</AppButton>
        <AppButton variant="secondary" size="sm" @click="continueVisible = true">续写集数</AppButton>
      </div>
    </header>

    <!-- 生成进度与终止（AC-F2-12-1） -->
    <div v-if="activeRun" class="mb-4 rounded-md border border-info bg-surface-inset p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-2">
          <StatusTag status="running" size="sm" label="正在生成" />
          <span class="text-body text-neutral-900">
            已完成 {{ activeRun.completed_episodes }} / {{ activeRun.total_episodes }} 集
          </span>
        </div>
        <AppButton variant="danger" size="sm" :loading="submitting" @click="cancelActiveRun">终止生成</AppButton>
      </div>
      <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-neutral-200">
        <div class="h-full rounded-full bg-info" :style="{ width: `${activeRun.progress_percent}%` }" />
      </div>
      <ul class="mt-2 flex flex-wrap gap-3">
        <li v-for="node in activeRun.plan" :key="node.todo_id" class="text-caption text-neutral-600">
          {{ node.node_name }}：
          <span :class="node.status === 'done' ? 'text-success-strong' : 'text-neutral-500'">
            {{ node.status === 'done' ? '完成' : node.status === 'running' ? '进行中' : '待执行' }}
          </span>
        </li>
      </ul>
    </div>

    <!-- 版本列表（AC-F2-11） -->
    <div class="mb-4">
      <div class="mb-2 flex items-center justify-between">
        <h4 class="text-subtitle text-neutral-900">历史版本</h4>
        <span class="text-caption text-neutral-500">共 {{ versions.length }} / 上限 {{ versionLimit }}</span>
      </div>
      <EmptyState
        v-if="versions.length === 0"
        title="还没有历史版本"
        description="第一次保存后即可在此对比与恢复。"
        icon-path="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
      />
      <ul v-else class="divide-y divide-hairline rounded-md border border-hairline">
        <li
          v-for="version in versions"
          :key="version.id"
          class="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
        >
          <div class="flex min-w-0 items-center gap-2">
            <span class="text-body font-medium text-neutral-900">v{{ version.version_no }}</span>
            <span v-if="version.is_current" class="rounded bg-info-bg px-1.5 py-0.5 text-caption text-info-strong">
              当前
            </span>
            <span class="truncate text-caption text-neutral-600">
              {{ version.version_label || '未命名' }} · {{ version.total_episodes }} 集 ·
              {{ version.total_words }} 字
            </span>
          </div>
          <div class="flex items-center gap-1">
            <AppButton variant="text" size="sm" @click="renameVersion(version)">备注</AppButton>
            <AppButton
              v-if="!version.is_current"
              variant="text"
              size="sm"
              @click="openRestore(version)"
            >
              设为当前
            </AppButton>
          </div>
        </li>
      </ul>
    </div>

    <!-- 模板包（AC-F2-09） -->
    <div>
      <h4 class="mb-2 text-subtitle text-neutral-900">组合模板包</h4>
      <p class="mb-2 text-caption text-neutral-600">
        题材 × 画风预设，一键带入生成参数；套用后仍可手改，被手改的字段会标记「已偏离预设」。
      </p>
      <div class="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <button
          v-for="pack in packs"
          :key="pack.template_id"
          type="button"
          class="rounded-md border border-hairline bg-surface-inset p-3 text-left transition hover:border-hairline-strong disabled:opacity-60"
          :disabled="submitting"
          @click="applyPack(pack)"
        >
          <div class="text-body font-medium text-neutral-900">{{ pack.name }}</div>
          <div class="mt-1 text-caption text-neutral-600">{{ pack.description }}</div>
          <div class="mt-1 text-caption text-neutral-500">
            {{ pack.episode_target }} 集 · 单集 {{ pack.word_target }} 字 · {{ pack.pacing }} · {{ pack.ending }}
          </div>
        </button>
      </div>
    </div>

    <Modal v-model="restoreVisible" title="设为当前版本" :max-width="420">
      <p class="text-caption leading-relaxed text-neutral-600">
        将以 v{{ restoreTarget?.version_no }} 的内容生成**新版本**（版本号 +1），
        原历史版本仍可访问，版本链不破坏。
      </p>
      <label class="mt-3 block">
        <span class="text-caption text-neutral-600">新版本备注</span>
        <input
          v-model="restoreLabel"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
        />
      </label>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="restoreVisible = false">取消</AppButton>
          <AppButton variant="primary" :loading="submitting" @click="confirmRestore">确认恢复</AppButton>
        </div>
      </template>
    </Modal>

    <Modal v-model="continueVisible" title="续写新集" :max-width="460">
      <p class="text-caption text-neutral-600">{{ episodeLimitHint }}</p>
      <label class="mt-3 block">
        <span class="text-caption text-neutral-600">续写集数（1~10）</span>
        <input
          v-model.number="continueCount"
          type="number"
          min="1"
          max="10"
          class="mt-1 w-24 rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
        />
      </label>
      <label class="mt-3 block">
        <span class="text-caption text-neutral-600">续写要点（≥5 字）</span>
        <textarea
          v-model="continueOutline"
          rows="3"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
          placeholder="例如：林晚发现钥匙对应的老宅，引出十年前的失踪案"
        />
      </label>
      <p class="mt-2 text-caption text-neutral-500">
        仅基于既有大纲与最后一集展开，原集内容零改动。
      </p>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="continueVisible = false">取消</AppButton>
          <AppButton variant="primary" :loading="submitting" @click="submitContinue">开始续写</AppButton>
        </div>
      </template>
    </Modal>
  </section>
</template>

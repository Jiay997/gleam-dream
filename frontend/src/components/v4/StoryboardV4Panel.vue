<!--
  文件级注释：V4.0 分镜域面板（F4-03 静帧重出 / F4-07 节奏模式 / F4-08 复制与批量改 /
  F4-09 操作历史 / F4-10 分镜模板）。

  承载的后端能力（`api/v4.ts`）：
  - 节奏模式三选一（快节奏/均衡/慢节奏）+ 题材推荐（AC-F4-07-1~3）；
  - 批量修改镜头参数（先摘要后执行，对白超 6s 单独标红，AC-F4-08-2~3）；
  - 复制镜头（继承 prompt/运镜/时长/情绪/锚点，AC-F4-08-1）；
  - 画布操作历史与回退到任意一步（AC-F4-09-1~2）；
  - 静帧重出（≤3 次，AC-F4-03-3）；
  - 分镜模板列表与自定义保存（AC-F4-10-1~2）。

  props：`projectId`、`storyboardId`、`shotIds`（当前选中镜头）。
  emit：`reload`（请求父视图刷新分镜板）。
-->
<script setup lang="ts">
/**
 * StoryboardV4Panel 分镜域 V4 能力面板。
 *
 * props：
 * - projectId：项目 ID；
 * - storyboardId：分镜板 ID；
 * - shotIds：当前选中/全部镜头 ID（批量操作目标）。
 * emit：
 * - reload：请求父视图刷新分镜板。
 */
import { computed, onMounted, ref } from 'vue'

import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import Modal from '@/components/common/Modal.vue'
import { useToast } from '@/composables/useToast'
import {
  batchPatchShots,
  composeStoryboardFrames,
  copyShot,
  getDirectorNotes,
  getStoryboardFramesQc,
  getStoryboardOps,
  listStoryboardTemplates,
  putDirectorNote,
  redrawShotFrame,
  rollbackStoryboard,
  saveStoryboardTemplate,
} from '@/api/v4'
import type {
  DirectorNotesPayload,
  PacingMode,
  StoryboardOpStep,
  StoryboardTemplateInfo,
} from '@/api/v4'

/** 组件 props */
const props = withDefaults(
  defineProps<{
    /** 项目 ID */
    projectId: string | number
    /** 分镜板 ID */
    storyboardId: string | number
    /** 目标镜头 ID 列表 */
    shotIds?: number[]
  }>(),
  { shotIds: () => [] },
)

/** 组件事件 */
const emit = defineEmits<{
  /** 请求刷新分镜板 */
  reload: []
}>()

const toast = useToast()

/** 导演策略 */
const notes = ref<DirectorNotesPayload | null>(null)
/** 操作历史 */
const ops = ref<StoryboardOpStep[]>([])
/** 模板集合 */
const templates = ref<{ builtin: StoryboardTemplateInfo[]; custom: StoryboardTemplateInfo[]; custom_limit: number } | null>(
  null,
)
/** 加载中 */
const loading = ref(false)
/** 提交中 */
const submitting = ref(false)

/** 批量改弹窗 */
const batchVisible = ref(false)
/** 批量改摘要 */
const batchPreview = ref<{
  affected: number
  changes: Array<{ field: string; from: unknown; to: unknown }>
  qc_flags?: Array<{ shot_no: number; message: string }>
} | null>(null)
/** 批量改表单 */
const batchForm = ref<{ camera_move: string; duration_ms: number | null; emotion_tag: string }>({
  camera_move: '',
  duration_ms: null,
  emotion_tag: '',
})

/** 模板保存弹窗 */
const templateVisible = ref(false)
/** 模板表单 */
const templateForm = ref({ name: '', kind: 'custom', rules: '对话以过肩中近景为主\n情绪转折切特写' })

/** 当前节奏模式 */
const pacingMode = computed<PacingMode>(() => notes.value?.project_note?.pacing_mode ?? 'balanced')

/**
 * 加载策略、历史与模板。
 * @returns 无
 */
async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [notePayload, opsPayload, templatePayload] = await Promise.all([
      getDirectorNotes(props.projectId).catch(() => null),
      getStoryboardOps(props.storyboardId).catch(() => null),
      listStoryboardTemplates().catch(() => null),
    ])
    notes.value = notePayload
    ops.value = opsPayload?.items ?? []
    templates.value = templatePayload
  } finally {
    loading.value = false
  }
}

/**
 * 切换节奏模式（写入全剧策略）。
 * @param mode 节奏模式
 * @returns 无
 */
async function changePacing(mode: PacingMode): Promise<void> {
  submitting.value = true
  try {
    const result = await putDirectorNote(props.projectId, { pacing_mode: mode })
    toast.success(result.message || `节奏模式已切换为「${result.pacing_profile.label}」`)
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 请求批量修改摘要。
 * @returns 无
 */
async function previewBatch(): Promise<void> {
  if (props.shotIds.length === 0) {
    toast.warning('请先选择镜头')
    return
  }
  const changes: Record<string, string | number> = {}
  if (batchForm.value.camera_move) changes.camera_move = batchForm.value.camera_move
  if (batchForm.value.duration_ms !== null) changes.duration_ms = Number(batchForm.value.duration_ms)
  if (batchForm.value.emotion_tag) changes.emotion_tag = batchForm.value.emotion_tag
  if (Object.keys(changes).length === 0) {
    toast.warning('至少选择一项待修改参数')
    return
  }
  submitting.value = true
  try {
    const result = await batchPatchShots(props.shotIds, changes, false)
    batchPreview.value = result
    batchVisible.value = true
  } finally {
    submitting.value = false
  }
}

/**
 * 确认执行批量修改。
 * @returns 无
 */
async function confirmBatch(): Promise<void> {
  const changes: Record<string, string | number> = {}
  if (batchForm.value.camera_move) changes.camera_move = batchForm.value.camera_move
  if (batchForm.value.duration_ms !== null) changes.duration_ms = Number(batchForm.value.duration_ms)
  if (batchForm.value.emotion_tag) changes.emotion_tag = batchForm.value.emotion_tag
  submitting.value = true
  try {
    const result = await batchPatchShots(props.shotIds, changes, true)
    toast.success(`已修改 ${result.affected} 个镜头`)
    if (result.qc_flags && result.qc_flags.length > 0) {
      toast.warning(`有 ${result.qc_flags.length} 个镜头对白超时长，已在画布标红`)
    }
    batchVisible.value = false
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 复制指定镜头到集尾。
 * @param shotId 源镜头 ID
 * @returns 无
 */
async function duplicate(shotId: number): Promise<void> {
  submitting.value = true
  try {
    const result = await copyShot(shotId, 'tail')
    toast.success(`已复制镜头（新镜号 ${(result.new_shot as Record<string, unknown>).shot_no}）`)
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 重出静帧。
 * @param shotId 镜头 ID
 * @returns 无
 */
async function redraw(shotId: number): Promise<void> {
  submitting.value = true
  try {
    const result = await redrawShotFrame(shotId, undefined, 'generate')
    toast.success(`静帧已重出（剩余 ${result.remaining} 次）`)
    if (result.suggest_change_prompt) {
      toast.warning('该镜头重出次数已用尽，建议修改 prompt 后重新拆解')
    }
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 重出整幅要素画面（本地合成，0 成本）。
 * @param shotId 镜头 ID
 * @returns 无
 */
async function composeFrame(shotId: number): Promise<void> {
  submitting.value = true
  try {
    const result = await redrawShotFrame(shotId, undefined, 'compose')
    const degraded = result.degraded?.length ? `（降级：${result.degraded.join('、')}）` : ''
    toast.success(`整幅画面已合成${degraded}`)
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 批量合成整板首帧并展示 QC 门禁结果。
 * @returns 无
 */
async function composeAllFrames(): Promise<void> {
  submitting.value = true
  try {
    const result = await composeStoryboardFrames(props.storyboardId)
    const qc = await getStoryboardFramesQc(props.storyboardId)
    toast.success(`已合成 ${result.succeeded}/${result.total} 镜`)
    if (!qc.passed) {
      const first = qc.issues[0]
      toast.warning(first ? `QC 未通过：${first.message}` : 'QC 未通过')
    }
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 回退到指定操作步骤。
 * @param seq 目标步骤
 * @returns 无
 */
async function rollback(seq: number): Promise<void> {
  const preview = await rollbackStoryboard(props.storyboardId, seq, false)
  const confirmed = window.confirm(
    `将回退到第 ${seq} 步，后续 ${preview.truncated_steps} 步历史会被截断。确认？`,
  )
  if (!confirmed) return
  submitting.value = true
  try {
    const result = await rollbackStoryboard(props.storyboardId, seq, true)
    toast.success(`已回退到第 ${result.target_seq} 步（恢复 ${result.restored_shots ?? 0} 个镜头）`)
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 保存自定义分镜模板。
 * @returns 无
 */
async function saveTemplate(): Promise<void> {
  const rules = templateForm.value.rules
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
  if (!templateForm.value.name.trim()) {
    toast.warning('请填写模板名称')
    return
  }
  if (rules.length === 0) {
    toast.warning('至少填写 1 条机位规则')
    return
  }
  submitting.value = true
  try {
    await saveStoryboardTemplate({
      name: templateForm.value.name.trim(),
      kind: templateForm.value.kind,
      rules,
    })
    toast.success('分镜模板已保存（跨项目可用）')
    templateVisible.value = false
    await loadAll()
  } finally {
    submitting.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
    <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-title text-neutral-900">节奏与画布操作</h3>
      <div class="flex flex-wrap items-center gap-2">
        <AppButton variant="ghost" size="sm" :loading="loading" @click="loadAll">刷新</AppButton>
        <AppButton variant="secondary" size="sm" @click="previewBatch">批量改镜头</AppButton>
        <AppButton variant="secondary" size="sm" @click="templateVisible = true">保存模板</AppButton>
      </div>
    </header>

    <!-- 节奏模式（AC-F4-07） -->
    <div class="rounded-md border border-hairline bg-surface-inset p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span class="text-subtitle text-neutral-900">叙事节奏模式</span>
        <span v-if="notes" class="text-caption text-neutral-500">
          题材推荐：{{ notes.recommended_pacing }}
        </span>
      </div>
      <div class="mt-2 flex flex-wrap gap-2">
        <button
          v-for="mode in notes?.available_modes ?? []"
          :key="mode.value"
          type="button"
          class="rounded-md border px-3 py-1.5 text-left transition"
          :class="
            pacingMode === mode.value
              ? 'border-info bg-info-bg text-info-strong'
              : 'border-hairline bg-surface-canvas text-neutral-700 hover:border-hairline-strong'
          "
          :disabled="submitting"
          @click="changePacing(mode.value)"
        >
          <span class="text-body font-medium">{{ mode.label }}</span>
          <span class="ml-2 text-caption">{{ mode.shot_duration_min }}~{{ mode.shot_duration_max }}ms</span>
        </button>
      </div>
      <p v-if="notes?.project_note" class="mt-2 text-caption text-neutral-600">
        {{ (notes.project_note.strategy_notes?.pacing_hint as string) || '' }}
      </p>
    </div>

    <!-- 操作历史（AC-F4-09） -->
    <div class="mt-4">
      <h4 class="mb-2 text-subtitle text-neutral-900">操作历史（最近 100 步）</h4>
      <EmptyState
        v-if="ops.length === 0"
        title="还没有画布操作"
        description="增删镜头、拖拽排序、批量修改后即可在此回退。"
        icon-path="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
      />
      <ul v-else class="max-h-56 space-y-1 overflow-auto pr-1">
        <li
          v-for="step in [...ops].reverse()"
          :key="step.seq"
          class="flex items-center justify-between gap-2 rounded border border-hairline px-2 py-1.5"
        >
          <span class="text-caption text-neutral-600">
            #{{ step.seq }} · {{ step.op_type }} · 镜头 {{ step.target_shot_ids.join(',') }} ·
            {{ step.at.slice(11, 19) }}
          </span>
          <AppButton variant="text" size="sm" :loading="submitting" @click="rollback(step.seq)">
            回退到此步
          </AppButton>
        </li>
      </ul>
    </div>

    <!-- 分镜模板（AC-F4-10） -->
    <div class="mt-4">
      <h4 class="mb-2 text-subtitle text-neutral-900">分镜模板</h4>
      <div class="grid gap-2 sm:grid-cols-3">
        <div
          v-for="template in templates?.builtin ?? []"
          :key="template.template_id"
          class="rounded-md border border-hairline bg-surface-inset p-2"
        >
          <div class="text-body text-neutral-900">{{ template.name }}</div>
          <div class="mt-1 text-caption text-neutral-600">{{ template.description }}</div>
          <ul class="mt-1 list-disc pl-4 text-caption text-neutral-500">
            <li v-for="rule in template.rules.slice(0, 2)" :key="rule">{{ rule }}</li>
          </ul>
        </div>
      </div>
      <p v-if="templates" class="mt-2 text-caption text-neutral-500">
        自定义模板 {{ templates.custom.length }} / {{ templates.custom_limit }} 个（跨项目可用）。
      </p>
    </div>

    <!-- 镜头快捷操作 -->
    <div v-if="shotIds.length" class="mt-4">
      <h4 class="mb-2 text-subtitle text-neutral-900">镜头快捷操作</h4>
      <div class="flex flex-wrap gap-1.5">
        <AppButton
          v-for="shotId in shotIds.slice(0, 8)"
          :key="shotId"
          variant="ghost"
          size="sm"
          :loading="submitting"
          @click="duplicate(shotId)"
        >
          复制 #{{ shotId }}
        </AppButton>
        <AppButton
          v-for="shotId in shotIds.slice(0, 8)"
          :key="`compose-${shotId}`"
          variant="subtle"
          size="sm"
          :loading="submitting"
          @click="composeFrame(shotId)"
        >
          重出整幅画面 #{{ shotId }}
        </AppButton>
        <AppButton variant="ghost" size="sm" :loading="submitting" @click="composeAllFrames">
          批量合成整板首帧
        </AppButton>
        <AppButton
          v-for="shotId in shotIds.slice(0, 8)"
          :key="`redraw-${shotId}`"
          variant="ghost"
          size="sm"
          :loading="submitting"
          @click="redraw(shotId)"
        >
          纯生成 #{{ shotId }}
        </AppButton>
      </div>
    </div>

    <Modal v-model="batchVisible" title="确认批量修改" :max-width="480">
      <p class="text-body text-neutral-900">{{ batchPreview?.affected }} 个镜头将修改</p>
      <ul class="mt-2 space-y-1">
        <li
          v-for="item in batchPreview?.changes ?? []"
          :key="item.field"
          class="text-caption text-neutral-600"
        >
          {{ item.field }}：{{ String(item.from) }} → {{ String(item.to) }}
        </li>
      </ul>
      <ul v-if="batchPreview?.qc_flags?.length" class="mt-2 space-y-1">
        <li v-for="flag in batchPreview.qc_flags" :key="flag.shot_no" class="text-caption text-danger-strong">
          镜头 {{ flag.shot_no }}：{{ flag.message }}
        </li>
      </ul>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="batchVisible = false">取消</AppButton>
          <AppButton variant="primary" :loading="submitting" @click="confirmBatch">确认保存</AppButton>
        </div>
      </template>
    </Modal>

    <Modal v-model="templateVisible" title="保存自定义分镜模板" :max-width="480">
      <label class="block">
        <span class="text-caption text-neutral-600">模板名称</span>
        <input
          v-model="templateForm.name"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
          placeholder="例如：我的对话模板"
        />
      </label>
      <label class="mt-3 block">
        <span class="text-caption text-neutral-600">机位规则（每行一条，≤10 条，每条 ≤50 字）</span>
        <textarea
          v-model="templateForm.rules"
          rows="5"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
        />
      </label>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="templateVisible = false">取消</AppButton>
          <AppButton variant="primary" :loading="submitting" @click="saveTemplate">保存</AppButton>
        </div>
      </template>
    </Modal>
  </section>
</template>

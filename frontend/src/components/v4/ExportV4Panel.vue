<!--
  文件级注释：V4.0 导出与分发面板（F7-06 历史复用 / F7-07 规格自检 /
  F7-08 AI 内容标识 / F7-09 分发重试撤回）。

  承载的后端能力（`api/v4.ts`）：
  - `listExportHistory`：导出历史（时间/集数/参数摘要/文件状态/90 天保留，AC-F7-06-1）；
  - `reuseExportJob`：参数复用重新导出（缓存命中仅重打包，AC-F7-06-2）；
  - `prespecCheck`：目标平台规格自检（阻断项禁用导出并给建议，AC-F7-07-1~2）；
  - `listExportPresets` / `saveExportPreset` / `deleteExportPreset`：预设（上限 10，AC-F7-01-3）；
  - AI 标识：导出任务携带 `ai_label`（显式片尾标识不可关闭 + 隐式元数据，
    NFR-C6 / AC-F7-08-1，**无关闭入口**）；
  - `retryDistribution` / `withdrawDistribution`：分发失败自助恢复与撤回（AC-F7-09-1~2）。

  props：`projectId`、`distributions`（父视图分发记录）。emit：`reload`。
-->
<script setup lang="ts">
/**
 * ExportV4Panel 导出与分发 V4 能力面板。
 *
 * props：
 * - projectId：项目 ID；
 * - distributions：分发记录列表（父视图提供）。
 * emit：
 * - reload：请求父视图刷新分发记录。
 */
import { computed, onMounted, ref } from 'vue'

import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import Modal from '@/components/common/Modal.vue'
import { useToast } from '@/composables/useToast'
import {
  deleteExportPreset,
  listExportHistory,
  listExportPresets,
  prespecCheck,
  retryDistribution,
  reuseExportJob,
  saveExportPreset,
  withdrawDistribution,
} from '@/api/v4'
import type { ExportHistoryItem, ExportPresetInfo } from '@/api/v4'

/** 组件 props */
const props = withDefaults(
  defineProps<{
    /** 项目 ID */
    projectId: string | number
    /** 分发记录列表 */
    distributions?: Array<Record<string, unknown>>
  }>(),
  { distributions: () => [] },
)

/** 组件事件 */
const emit = defineEmits<{
  /** 请求刷新分发记录 */
  reload: []
}>()

const toast = useToast()

/** 导出历史 */
const history = ref<ExportHistoryItem[]>([])
/** 保留天数 */
const retentionDays = ref(90)
/** 导出预设 */
const presets = ref<ExportPresetInfo[]>([])
/** 预设上限 */
const presetLimit = ref(10)
/** 加载中 */
const loading = ref(false)
/** 提交中 */
const submitting = ref(false)

/** 规格自检表单 */
const specForm = ref<{ platform: string; resolution: string }>({ platform: 'douyin', resolution: '1080p' })
/** 自检结果 */
const specResult = ref<{
  platform_label: string
  checks: Array<{ item: string; ok: boolean; blocking: boolean; detail: string }>
  blocked: boolean
  blocked_items: string[]
  recommendations: string[]
  degraded: boolean
  degraded_hint: string | null
} | null>(null)

/** 预设保存弹窗 */
const presetVisible = ref(false)
/** 预设表单 */
const presetForm = ref({ name: '', resolution: '1080p', aspect: '9:16', watermark: true })

/** AI 标识预览弹窗 */
const aiLabelVisible = ref(false)
/** 当前查看的导出任务 */
const aiLabelJob = ref<ExportHistoryItem | null>(null)

/** 可重试/可撤回的分发记录 */
const actionableDistributions = computed(() =>
  (props.distributions as Array<Record<string, unknown>>).filter((record) => {
    const status = String(record.status ?? '')
    return status === 'failed' || status === 'succeeded' || status === 'withdrawn' || status === 'confirming'
  }),
)

/**
 * 加载历史与预设。
 * @returns 无
 */
async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [historyPayload, presetPayload] = await Promise.all([
      listExportHistory(props.projectId).catch(() => null),
      listExportPresets().catch(() => null),
    ])
    history.value = historyPayload?.items ?? []
    retentionDays.value = historyPayload?.retention_days ?? 90
    presets.value = presetPayload?.items ?? []
    presetLimit.value = presetPayload?.limit ?? 10
  } finally {
    loading.value = false
  }
}

/**
 * 执行目标平台规格自检。
 * @returns 无
 */
async function runSpecCheck(): Promise<void> {
  submitting.value = true
  try {
    const result = await prespecCheck(props.projectId, {
      platform: specForm.value.platform,
      resolution: specForm.value.resolution,
    })
    specResult.value = result
    if (result.blocked) toast.warning('存在阻断项，导出按钮应禁用')
    else toast.success('规格自检通过')
  } finally {
    submitting.value = false
  }
}

/**
 * 复用历史参数重新导出。
 * @param job 历史任务
 * @returns 无
 */
async function reuse(job: ExportHistoryItem): Promise<void> {
  submitting.value = true
  try {
    const result = await reuseExportJob(job.id, { resolution: job.resolution })
    toast.success(result.message)
    if (result.latest_timeline_changed) toast.warning('成片版本已更新，将导出当前最新成片')
    await loadAll()
  } finally {
    submitting.value = false
  }
}

/**
 * 查看 AI 标识配置（合规展示，无关闭入口）。
 * @param job 导出任务
 * @returns 无
 */
function showAiLabel(job: ExportHistoryItem): void {
  aiLabelJob.value = job
  aiLabelVisible.value = true
}

/**
 * 保存导出预设。
 * @returns 无
 */
async function savePreset(): Promise<void> {
  if (!presetForm.value.name.trim()) {
    toast.warning('请填写预设名称')
    return
  }
  submitting.value = true
  try {
    await saveExportPreset({
      name: presetForm.value.name.trim(),
      resolution: presetForm.value.resolution,
      aspect: presetForm.value.aspect,
      watermark: presetForm.value.watermark,
    })
    toast.success('导出预设已保存（套用时权益按当下会员状态实时校验）')
    presetVisible.value = false
    await loadAll()
  } finally {
    submitting.value = false
  }
}

/**
 * 删除导出预设。
 * @param preset 预设
 * @returns 无
 */
async function removePreset(preset: ExportPresetInfo): Promise<void> {
  submitting.value = true
  try {
    await deleteExportPreset(preset.id)
    toast.success('预设已删除')
    await loadAll()
  } finally {
    submitting.value = false
  }
}

/**
 * 分发重试。
 * @param recordId 分发记录 ID
 * @returns 无
 */
async function retry(recordId: number): Promise<void> {
  submitting.value = true
  try {
    const result = await retryDistribution(recordId)
    toast.success(result.message)
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 分发撤回。
 * @param recordId 分发记录 ID
 * @returns 无
 */
async function withdraw(recordId: number): Promise<void> {
  if (!window.confirm('撤回后该条分发记录标记为已撤回（依平台能力可能需后台手动删除）。确认？')) return
  submitting.value = true
  try {
    const result = await withdrawDistribution(recordId)
    toast.success(result.message)
    emit('reload')
  } finally {
    submitting.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
    <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-title text-neutral-900">导出历史与合规</h3>
      <div class="flex flex-wrap items-center gap-2">
        <AppButton variant="ghost" size="sm" :loading="loading" @click="loadAll">刷新</AppButton>
        <AppButton variant="secondary" size="sm" @click="presetVisible = true">保存为预设</AppButton>
      </div>
    </header>

    <!-- 规格自检（AC-F7-07） -->
    <div class="rounded-md border border-hairline bg-surface-inset p-3">
      <div class="flex flex-wrap items-end gap-2">
        <label class="block">
          <span class="text-caption text-neutral-600">目标平台</span>
          <select
            v-model="specForm.platform"
            class="mt-1 rounded-md border border-hairline bg-surface-canvas px-2 py-1.5 text-body"
          >
            <option value="douyin">抖音</option>
            <option value="kuaishou">快手</option>
            <option value="wechat_channels">视频号</option>
            <option value="jianying">剪映草稿</option>
          </select>
        </label>
        <label class="block">
          <span class="text-caption text-neutral-600">分辨率</span>
          <select
            v-model="specForm.resolution"
            class="mt-1 rounded-md border border-hairline bg-surface-canvas px-2 py-1.5 text-body"
          >
            <option value="720p">720p</option>
            <option value="1080p">1080p</option>
            <option value="4k">4K</option>
          </select>
        </label>
        <AppButton variant="primary" size="sm" :loading="submitting" @click="runSpecCheck">规格自检</AppButton>
      </div>
      <div v-if="specResult" class="mt-2 space-y-1">
        <p v-if="specResult.degraded" class="text-caption text-warning-strong">{{ specResult.degraded_hint }}</p>
        <ul class="space-y-0.5">
          <li
            v-for="check in specResult.checks"
            :key="check.item"
            class="text-caption"
            :class="check.ok ? 'text-neutral-600' : check.blocking ? 'text-danger-strong' : 'text-warning-strong'"
          >
            {{ check.ok ? '✓' : check.blocking ? '✗' : '!' }} {{ check.item }}：{{ check.detail }}
          </li>
        </ul>
        <p v-if="specResult.blocked" class="text-caption text-danger-strong">
          阻断项：{{ specResult.blocked_items.join('、') }}；建议：{{ specResult.recommendations.join('；') }}
        </p>
      </div>
    </div>

    <!-- 导出历史（AC-F7-06） -->
    <div class="mt-4">
      <div class="mb-2 flex items-center justify-between">
        <h4 class="text-subtitle text-neutral-900">导出历史</h4>
        <span class="text-caption text-neutral-500">保留 {{ retentionDays }} 天</span>
      </div>
      <EmptyState
        v-if="history.length === 0"
        title="还没有导出记录"
        description="完成成片审批后即可导出，历史参数可一键复用。"
        icon-path="M3 16.5V18a2.25 2.25 0 002.25 2.25h13.5A2.25 2.25 0 0021 18v-1.5M16.5 12L12 16.5 7.5 12M12 3v13.5"
      />
      <ul v-else class="divide-y divide-hairline rounded-md border border-hairline">
        <li
          v-for="job in history"
          :key="job.id"
          class="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
        >
          <div class="min-w-0">
            <div class="text-body text-neutral-900">{{ job.parameter_summary }}</div>
            <div class="text-caption text-neutral-500">
              {{ job.created_at?.slice(0, 19).replace('T', ' ') }} ·
              {{ job.status }} ·
              {{ job.file_available ? '文件可下载' : '文件已过期' }}
            </div>
          </div>
          <div class="flex flex-wrap items-center gap-1">
            <span
              v-if="job.ai_label_applied"
              class="rounded bg-success-bg px-1.5 py-0.5 text-caption text-success-strong"
            >
              AI 标识已落片
            </span>
            <AppButton variant="text" size="sm" @click="showAiLabel(job)">查看标识</AppButton>
            <AppButton variant="secondary" size="sm" :loading="submitting" @click="reuse(job)">
              复用参数导出
            </AppButton>
            <a
              v-if="job.file_available && job.output?.download_url"
              :href="job.output.download_url"
              class="gd-btn-text text-caption"
              target="_blank"
              rel="noreferrer"
              >下载</a
            >
          </div>
          <!-- 成片预览修复：MP4 导出产物可在历史行内直接播放（JSON 占位不渲染） -->
          <video
            v-if="job.file_available && job.output?.download_url?.toLowerCase().endsWith('.mp4')"
            :src="job.output.download_url"
            controls
            preload="metadata"
            class="mt-2 w-full max-w-[260px] basis-full rounded-md border border-hairline bg-black"
          />
        </li>
      </ul>
    </div>

    <!-- 导出预设 -->
    <div class="mt-4">
      <h4 class="mb-2 text-subtitle text-neutral-900">导出预设（{{ presets.length }}/{{ presetLimit }}）</h4>
      <ul v-if="presets.length" class="flex flex-wrap gap-2">
        <li
          v-for="preset in presets"
          :key="preset.id"
          class="flex items-center gap-2 rounded-md border border-hairline bg-surface-inset px-2 py-1"
        >
          <span class="text-body text-neutral-900">{{ preset.name }}</span>
          <span class="text-caption text-neutral-500">{{ preset.params.resolution }}</span>
          <AppButton variant="text" size="sm" :loading="submitting" @click="removePreset(preset)">删除</AppButton>
        </li>
      </ul>
      <p v-else class="text-caption text-neutral-500">尚未保存预设；套用预设时权益项按当下会员状态实时校验。</p>
    </div>

    <!-- 分发重试与撤回（AC-F7-09） -->
    <div class="mt-4">
      <h4 class="mb-2 text-subtitle text-neutral-900">分发记录处理</h4>
      <EmptyState
        v-if="actionableDistributions.length === 0"
        title="暂无需要处理的分发记录"
        description="分发失败后可在此自助重试或撤回。"
        icon-path="M12 9v3.75m0 3.75h.008M12 3l9 16.5H3L12 3z"
      />
      <ul v-else class="space-y-1">
        <li
          v-for="record in actionableDistributions"
          :key="String(record.id)"
          class="flex flex-wrap items-center justify-between gap-2 rounded border border-hairline px-2 py-1.5"
        >
          <div class="min-w-0">
            <div class="text-body text-neutral-900">
              {{ record.platform }} · {{ record.title ?? '未命名' }}
            </div>
            <div class="text-caption text-neutral-500">
              状态 {{ record.status }}
              <span v-if="record.retry_count"> · 已重试 {{ record.retry_count }} 次</span>
            </div>
            <p v-if="record.fail_reason_user" class="text-caption text-danger-strong">
              {{ record.fail_reason_user }}
            </p>
          </div>
          <div class="flex items-center gap-1">
            <AppButton variant="secondary" size="sm" :loading="submitting" @click="retry(Number(record.id))">
              重试
            </AppButton>
            <AppButton variant="danger" size="sm" :loading="submitting" @click="withdraw(Number(record.id))">
              撤回
            </AppButton>
          </div>
        </li>
      </ul>
    </div>

    <Modal v-model="aiLabelVisible" title="AI 生成内容标识" :max-width="460">
      <p class="text-caption leading-relaxed text-neutral-600">
        依据《人工智能生成合成内容标识办法》，导出母版必须嵌入显式标识（片尾固定位置）
        并在元数据中携带隐式标识；<strong class="text-neutral-900">用户不可关闭</strong>。
      </p>
      <div class="mt-3 space-y-2">
        <div class="rounded-md border border-hairline bg-surface-inset p-2">
          <div class="text-caption text-neutral-500">显式标识（片尾）</div>
          <div class="mt-1 text-body text-neutral-900">
            {{ aiLabelJob?.ai_label?.visible?.text ?? '本内容由 AI 辅助生成' }}
          </div>
          <div class="text-caption text-neutral-500">
            位置 {{ aiLabelJob?.ai_label?.visible?.position ?? 'tail' }} · 可关闭：
            {{ aiLabelJob?.ai_label?.visible?.closable ? '是' : '否' }}
          </div>
        </div>
        <div class="rounded-md border border-hairline bg-surface-inset p-2">
          <div class="text-caption text-neutral-500">隐式标识（元数据）</div>
          <pre class="mt-1 whitespace-pre-wrap break-all text-caption text-neutral-600">{{
            JSON.stringify(aiLabelJob?.ai_label?.implicit?.metadata ?? {}, null, 2)
          }}</pre>
        </div>
      </div>
    </Modal>

    <Modal v-model="presetVisible" title="保存导出预设" :max-width="420">
      <label class="block">
        <span class="text-caption text-neutral-600">预设名称</span>
        <input
          v-model="presetForm.name"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
          placeholder="例如：抖音竖屏 1080p"
        />
      </label>
      <div class="mt-3 grid grid-cols-2 gap-2">
        <label class="block">
          <span class="text-caption text-neutral-600">分辨率</span>
          <select
            v-model="presetForm.resolution"
            class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-2 py-1.5 text-body"
          >
            <option value="720p">720p</option>
            <option value="1080p">1080p</option>
            <option value="4k">4K</option>
          </select>
        </label>
        <label class="block">
          <span class="text-caption text-neutral-600">画面比例</span>
          <select
            v-model="presetForm.aspect"
            class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-2 py-1.5 text-body"
          >
            <option value="9:16">9:16</option>
            <option value="16:9">16:9</option>
            <option value="1:1">1:1</option>
            <option value="4:3">4:3</option>
          </select>
        </label>
      </div>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="presetVisible = false">取消</AppButton>
          <AppButton variant="primary" :loading="submitting" @click="savePreset">保存</AppButton>
        </div>
      </template>
    </Modal>
  </section>
</template>

<!--
  文件级注释：V4.0 资产域面板（F3-03 成本透明 / F3-08 场景变体 / F3-10 批量重抽 /
  F3-11 标签搜索）。

  承载的后端能力（`api/v4.ts`）：
  - `updateAssetLabels` + 列表 `search/labels` 过滤：资产墙按名称/标签前缀检索（AC-F3-11-1~2）；
  - `getRegenEstimate`：重抽成本透明——「本次预估 X + 已累计消耗 Y / 已重抽 N 次」
    并在超基线时提示换描述策略（AC-F3-03-2，P0 细节功能 SF-F3-12）；
  - `batchRegenerate`：多角色同反馈批量重抽，先预估后执行，逐角色独立（AC-F3-10-1~2）；
  - `generateSceneVariants` / `listSceneVariants`：场景时段/天气变体与缺失提示（AC-F3-08-1~2）。

  props：`assets`（父视图资产列表）、`selectedAssetId`（当前详情资产）。
  emit：`refresh`（请求父视图刷新资产列表）。
-->
<script setup lang="ts">
/**
 * AssetV4Panel 资产域 V4 能力面板。
 *
 * props：
 * - assets：资产列表（父视图提供）；
 * - selectedAssetId：当前选中资产 ID（可空，决定单价/变体区展示对象）。
 * emit：
 * - refresh：请求父视图刷新。
 */
import { computed, ref, watch } from 'vue'

import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import Modal from '@/components/common/Modal.vue'
import { useToast } from '@/composables/useToast'
import {
  batchRegenerate,
  generateSceneVariants,
  getRegenEstimate,
  listSceneVariants,
  updateAssetLabels,
} from '@/api/v4'
import type { BatchRegenBlocked, RegenEstimate, SceneVariantCoverage } from '@/api/v4'

/** 组件 props */
const props = withDefaults(
  defineProps<{
    /** 资产列表 */
    assets?: Array<Record<string, unknown>>
    /** 当前选中资产 ID */
    selectedAssetId?: number | string | null
  }>(),
  { assets: () => [], selectedAssetId: null },
)

/** 组件事件 */
const emit = defineEmits<{
  /** 请求刷新资产列表 */
  refresh: []
}>()

const toast = useToast()

/** 搜索关键词 */
const keyword = ref('')
/** 标签过滤（逗号分隔，AND 语义） */
const labelFilter = ref('')
/** 多选资产 ID（批量重抽） */
const selectedIds = ref<number[]>([])
/** 批量反馈 */
const feedback = ref('')
/** 提交中标记 */
const submitting = ref(false)

/** 确认弹窗可见 */
const confirmVisible = ref(false)
/** 预估结果 */
const estimate = ref<{
  total: number
  estimated_credits: number
  formula: string
  blocked: BatchRegenBlocked[]
} | null>(null)

/** 标签编辑弹窗 */
const labelDialogVisible = ref(false)
/** 标签输入（逗号分隔） */
const labelInput = ref('')

/** 单资产成本预估 */
const singleEstimate = ref<RegenEstimate | null>(null)
/** 场景变体覆盖 */
const variantCoverage = ref<SceneVariantCoverage | null>(null)

/** 按关键词 + 标签过滤后的资产（前端即时过滤，500ms 内返回，AC-F3-11-1） */
const filteredAssets = computed(() => {
  const text = keyword.value.trim().toLowerCase()
  const labels = labelFilter.value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
  return (props.assets as Array<Record<string, unknown>>).filter((asset) => {
    const name = String(asset.name ?? '').toLowerCase()
    const assetLabels = (asset.labels as string[] | null) ?? []
    const textHit = !text || name.includes(text) || assetLabels.some((l) => l.toLowerCase().includes(text))
    const labelHit = labels.every((label) => assetLabels.includes(label))
    return textHit && labelHit
  })
})

/** 当前选中资产 */
const activeAsset = computed(
  () =>
    (props.assets as Array<Record<string, unknown>>).find(
      (asset) => String(asset.id) === String(props.selectedAssetId),
    ) ?? null,
)

/**
 * 拉取选中资产的重抽成本与变体覆盖。
 * @returns 无
 */
async function loadActiveDetail(): Promise<void> {
  if (!props.selectedAssetId) {
    singleEstimate.value = null
    variantCoverage.value = null
    return
  }
  singleEstimate.value = await getRegenEstimate(props.selectedAssetId)
  if (activeAsset.value?.asset_type === 'scene') {
    variantCoverage.value = await listSceneVariants(props.selectedAssetId)
  } else {
    variantCoverage.value = null
  }
}

/**
 * 勾选/取消资产。
 * @param assetId 资产 ID
 * @returns 无
 */
function toggleSelect(assetId: number): void {
  const index = selectedIds.value.indexOf(assetId)
  if (index >= 0) selectedIds.value.splice(index, 1)
  else selectedIds.value.push(assetId)
}

/**
 * 先请求批量重抽预估（不执行）。
 * @returns 无
 */
async function requestPreview(): Promise<void> {
  if (selectedIds.value.length === 0) {
    toast.warning('请先勾选需要重抽的资产')
    return
  }
  if (feedback.value.trim().length < 5) {
    toast.warning('重抽反馈不少于 5 字')
    return
  }
  submitting.value = true
  try {
    const preview = await batchRegenerate(selectedIds.value, feedback.value.trim(), false)
    estimate.value = {
      total: preview.total,
      estimated_credits: preview.estimated_credits,
      formula: preview.formula ?? '',
      blocked: preview.blocked,
    }
    confirmVisible.value = true
  } finally {
    submitting.value = false
  }
}

/**
 * 确认执行批量重抽。
 * @returns 无
 */
async function confirmBatch(): Promise<void> {
  submitting.value = true
  try {
    const result = await batchRegenerate(selectedIds.value, feedback.value.trim(), true)
    const blockedCount = result.blocked.length
    toast.success(
      `已受理 ${result.accepted.length} 个角色重抽${blockedCount ? `，${blockedCount} 个被拦截` : ''}`,
    )
    confirmVisible.value = false
    selectedIds.value = []
    emit('refresh')
  } finally {
    submitting.value = false
  }
}

/**
 * 打开标签编辑弹窗。
 * @returns 无
 */
function openLabelDialog(): void {
  labelInput.value = ((activeAsset.value?.labels as string[] | null) ?? []).join('，')
  labelDialogVisible.value = true
}

/**
 * 保存标签（≤10 个、单个 ≤12 字，由后端二次校验）。
 * @returns 无
 */
async function saveLabels(): Promise<void> {
  if (!props.selectedAssetId) return
  const labels = labelInput.value
    .split(/[,，]/)
    .map((item) => item.trim())
    .filter(Boolean)
  submitting.value = true
  try {
    await updateAssetLabels(props.selectedAssetId, labels)
    toast.success('标签已更新')
    labelDialogVisible.value = false
    emit('refresh')
  } finally {
    submitting.value = false
  }
}

/**
 * 生成场景变体（同类型最多 2 项）。
 * @param tags 变体标签
 * @returns 无
 */
async function makeVariants(tags: string[]): Promise<void> {
  if (!props.selectedAssetId) return
  submitting.value = true
  try {
    const result = await generateSceneVariants(props.selectedAssetId, tags)
    toast.success(`已受理 ${result.accepted_variants.length} 个变体（预估 ${result.estimated_credits} 积分）`)
    await loadActiveDetail()
    emit('refresh')
  } finally {
    submitting.value = false
  }
}

watch(() => props.selectedAssetId, loadActiveDetail, { immediate: true })
</script>

<template>
  <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
    <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-title text-neutral-900">资产检索与重抽</h3>
      <AppButton v-if="activeAsset" variant="secondary" size="sm" @click="openLabelDialog">编辑标签</AppButton>
    </header>

    <div class="grid gap-2 sm:grid-cols-2">
      <label class="block">
        <span class="text-caption text-neutral-600">按名称 / 标签搜索</span>
        <input
          v-model="keyword"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-1.5 text-body"
          placeholder="输入角色名或标签"
        />
      </label>
      <label class="block">
        <span class="text-caption text-neutral-600">标签过滤（逗号分隔，需同时命中）</span>
        <input
          v-model="labelFilter"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-1.5 text-body"
          placeholder="例如：女主,主线"
        />
      </label>
    </div>

    <div v-if="singleEstimate" class="mt-4 rounded-md border border-hairline bg-surface-inset p-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <span class="text-subtitle text-neutral-900">重抽成本</span>
        <span class="text-body tabular-nums text-neutral-900">
          本次预估 {{ singleEstimate.per_version_credits }} 积分
        </span>
      </div>
      <p class="mt-1 text-caption text-neutral-600">{{ singleEstimate.hint }}</p>
      <p v-if="singleEstimate.suggest_change_strategy" class="mt-1 text-caption text-warning-strong">
        累计消耗已超成本基线 {{ singleEstimate.cost_baseline }} 积分的 2 倍，建议换描述策略而非继续盲抽（不阻断）。
      </p>
    </div>

    <div class="mt-4">
      <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h4 class="text-subtitle text-neutral-900">批量重抽（多角色同反馈）</h4>
        <span class="text-caption text-neutral-500">已选 {{ selectedIds.length }} 个</span>
      </div>
      <EmptyState
        v-if="filteredAssets.length === 0"
        title="没有匹配的资产"
        description="调整搜索关键词或标签过滤条件后重试。"
        icon-path="M21 21l-4.35-4.35M17 11a6 6 0 11-12 0 6 6 0 0112 0z"
      />
      <template v-else>
        <ul class="max-h-64 space-y-1 overflow-auto pr-1">
          <li
            v-for="asset in filteredAssets"
            :key="String(asset.id)"
            class="flex items-center justify-between gap-2 rounded border border-hairline px-2 py-1.5"
          >
            <label class="flex min-w-0 items-center gap-2">
              <input
                type="checkbox"
                class="h-4 w-4"
                :checked="selectedIds.includes(Number(asset.id))"
                @change="toggleSelect(Number(asset.id))"
              />
              <span class="truncate text-body text-neutral-900">{{ asset.name }}</span>
              <span class="text-caption text-neutral-500">{{ asset.asset_type }}</span>
            </label>
            <span class="flex flex-wrap gap-1">
              <span
                v-for="label in (asset.labels as string[] | null) ?? []"
                :key="label"
                class="rounded bg-neutral-100 px-1.5 py-0.5 text-caption text-neutral-600"
                >{{ label }}</span
              >
            </span>
          </li>
        </ul>
        <div class="mt-2 flex flex-wrap items-center gap-2">
          <input
            v-model="feedback"
            class="min-w-[12rem] flex-1 rounded-md border border-hairline bg-surface-canvas px-3 py-1.5 text-body"
            placeholder="统一重抽反馈（5~100 字），例如：太老、换发型"
          />
          <AppButton variant="primary" size="sm" :loading="submitting" @click="requestPreview">
            查看预估并重抽
          </AppButton>
        </div>
      </template>
    </div>

    <div v-if="activeAsset?.asset_type === 'scene'" class="mt-4 rounded-md border border-hairline bg-surface-inset p-3">
      <h4 class="text-subtitle text-neutral-900">场景变体</h4>
      <p v-if="variantCoverage" class="mt-1 text-caption text-neutral-600">
        已生成 {{ variantCoverage.variants.length }} 个变体；缺失
        {{ variantCoverage.missing.length }} 个（分镜引用缺失变体时质检会提示「场景变体缺失」）。
      </p>
      <div class="mt-2 flex flex-wrap gap-1.5">
        <AppButton
          v-for="tag in [['dawn', '清晨'], ['day', '白天'], ['dusk', '黄昏'], ['night', '夜晚']]"
          :key="tag[0]"
          variant="ghost"
          size="sm"
          :loading="submitting"
          @click="makeVariants([tag[0]])"
        >
          {{ tag[1] }}
        </AppButton>
        <AppButton variant="secondary" size="sm" :loading="submitting" @click="makeVariants(['rain'])">
          雨天
        </AppButton>
        <AppButton variant="secondary" size="sm" :loading="submitting" @click="makeVariants(['snow'])">
          雪天
        </AppButton>
      </div>
    </div>

    <Modal v-model="confirmVisible" title="确认批量重抽" :max-width="460">
      <p class="text-body text-neutral-900">{{ estimate?.formula }}</p>
      <p class="mt-2 text-caption text-neutral-600">
        逐角色独立执行（独立版本与质检循环），单角色失败不阻塞其余。
      </p>
      <ul v-if="estimate && estimate.blocked.length" class="mt-3 space-y-1">
        <li v-for="item in estimate.blocked" :key="item.asset_id" class="text-caption text-warning-strong">
          {{ item.asset_name ?? item.asset_id }}：{{ item.reason }}
        </li>
      </ul>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="confirmVisible = false">取消</AppButton>
          <AppButton variant="primary" :loading="submitting" @click="confirmBatch">
            确认消耗 {{ estimate?.estimated_credits ?? 0 }} 积分
          </AppButton>
        </div>
      </template>
    </Modal>

    <Modal v-model="labelDialogVisible" title="编辑资产标签" :max-width="420">
      <label class="block">
        <span class="text-caption text-neutral-600">标签（逗号分隔，最多 10 个，单个 ≤12 字）</span>
        <input
          v-model="labelInput"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
          placeholder="例如：女主，主线，古装"
        />
      </label>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="labelDialogVisible = false">取消</AppButton>
          <AppButton variant="primary" :loading="submitting" @click="saveLabels">保存</AppButton>
        </div>
      </template>
    </Modal>
  </section>
</template>

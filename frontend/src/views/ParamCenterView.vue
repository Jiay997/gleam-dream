<!--
  文件级注释：V6 · 参数中心页（路由 /projects/:projectId/params，覆盖 F11）。

  页面结构：
  - 分组导航（A~H）+ 参数编辑表：每项展示定义、边界、生效范围、危险/锁定标记；
  - 变更提交：仅提交与生效值不同的键，危险参数强制填写 ≥10 字理由；
  - 版本与审计：历史版本回滚、恢复默认、变更审计；
  - 重评估：按当前阈值重算历史裁决（只读对照）。

  数据来源：`api/v6.ts` F11 分组。合规锁定参数由后端返回 20003，页面不自行
  放行；前端标记仅用于提前提示。
-->
<script setup lang="ts">
/**
 * ParamCenterView 参数中心页面。
 *
 * 参数采用“定义（ParamDef）+ 覆盖（项目/集）+ 版本快照”的不可变模型：
 * 保存即产生新版本，回滚也以新版本形式落库。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  getEffectiveParams,
  listParamAudits,
  listParamVersions,
  reassessParams,
  restoreParams,
  rollbackParams,
  saveParams,
} from '@/api/v6'
import type { EffectiveParams, ParamDefinition } from '@/api/v6'
import AppButton from '@/components/common/AppButton.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const toast = useToast()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))
/** 集号（用于集级覆盖与重评估） */
const epNo = ref(1)
/** 生效参数 */
const config = ref<EffectiveParams | null>(null)
/** 编辑草稿（参数键 → 值） */
const draft = ref<Record<string, unknown>>({})
/** 变更理由 */
const reason = ref('')
/** 当前分组 */
const activeGroup = ref('A')
/** 版本列表 */
const versions = ref<Array<Record<string, unknown>>>([])
/** 审计列表 */
const audits = ref<Array<Record<string, unknown>>>([])
/** 回滚目标版本 */
const rollbackVersion = ref<number | null>(null)
/** 是否加载中 */
const loading = ref(false)
/** 是否保存中 */
const saving = ref(false)

/** 分组标签 */
const GROUP_LABELS: Record<string, string> = {
  A: '剧本与解析',
  B: '分镜与时长',
  C: '资产一致性',
  D: '生成与模型',
  E: '配音',
  F: '后期',
  G: '合规',
  H: '连续性',
}

/** 当前分组定义 */
const activeItems = computed<ParamDefinition[]>(
  () => config.value?.groups?.find((group) => group.group === activeGroup.value)?.items ?? [],
)

/** 已修改的键值 */
const changes = computed<Record<string, unknown>>(() => {
  const result: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(draft.value)) {
    if (config.value && config.value.values[key] !== value) result[key] = value
  }
  return result
})

/** 是否包含危险参数 */
const hasDangerousChange = computed(() =>
  activeItems.value.some(
    (item) => item.is_dangerous && Object.prototype.hasOwnProperty.call(changes.value, item.param_key),
  ) ||
  (config.value?.groups ?? []).some((group) =>
    group.items.some(
      (item) => item.is_dangerous && Object.prototype.hasOwnProperty.call(changes.value, item.param_key),
    ),
  ),
)

/**
 * 加载参数、版本与审计。
 * @returns 无
 */
async function loadData(): Promise<void> {
  if (!projectId.value) return
  loading.value = true
  try {
    const [paramConfig, versionPage, auditPage] = await Promise.all([
      getEffectiveParams(projectId.value, epNo.value),
      listParamVersions(projectId.value).catch(() => null),
      listParamAudits(projectId.value).catch(() => null),
    ])
    config.value = paramConfig
    draft.value = { ...paramConfig.values }
    versions.value = versionPage?.items ?? []
    audits.value = auditPage?.items ?? []
    if (versions.value.length) rollbackVersion.value = Number(versions.value[0].version_no)
  } finally {
    loading.value = false
  }
}

/**
 * 按定义类型归一化输入值。
 * @param item 参数定义
 * @param raw 原始输入
 * @returns 归一化后的值
 */
function normalize(item: ParamDefinition, raw: unknown): unknown {
  if (item.value_type === 'bool') return Boolean(raw)
  if (item.value_type === 'int') {
    const value = Number(raw)
    return Number.isFinite(value) ? Math.round(value) : item.default_value
  }
  if (item.value_type === 'float') {
    const value = Number(raw)
    return Number.isFinite(value) ? value : item.default_value
  }
  return raw
}

/**
 * 更新草稿值。
 * @param item 参数定义
 * @param event 输入事件
 * @returns 无
 */
function updateDraft(item: ParamDefinition, event: Event): void {
  const target = event.target as HTMLInputElement
  const raw = item.value_type === 'bool' ? target.checked : target.value
  draft.value = { ...draft.value, [item.param_key]: normalize(item, raw) }
}

/**
 * 保存参数变更（危险参数需理由）。
 * @returns 无
 */
async function save(): Promise<void> {
  if (!Object.keys(changes.value).length) {
    toast.info('没有需要保存的变更')
    return
  }
  saving.value = true
  try {
    await saveParams(projectId.value, changes.value, reason.value)
    toast.success('参数已保存并生成新版本')
    reason.value = ''
    await loadData()
  } finally {
    saving.value = false
  }
}

/**
 * 恢复全部参数为默认值。
 * @returns 无
 */
async function restore(): Promise<void> {
  const confirmed = window.confirm('确认恢复全部参数为默认值？该操作会生成新版本。')
  if (!confirmed) return
  await restoreParams(projectId.value, [], reason.value || '恢复默认参数')
  toast.success('已恢复默认参数')
  await loadData()
}

/**
 * 回滚到指定历史版本。
 * @returns 无
 */
async function rollback(): Promise<void> {
  if (rollbackVersion.value == null) return
  if (reason.value.trim().length < 4) {
    toast.warning('请填写回滚理由')
    return
  }
  await rollbackParams(projectId.value, rollbackVersion.value, reason.value)
  toast.success('已回滚并生成新版本')
  await loadData()
}

/**
 * 按当前参数重评估历史裁决。
 * @returns 无
 */
async function reassess(): Promise<void> {
  const result = await reassessParams(projectId.value, epNo.value)
  const diff = Number(result.diff_count ?? 0)
  toast.success(`重评估完成，差异 ${diff} 条`)
  await loadData()
}

onMounted(loadData)
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <header class="shrink-0 px-6 pb-4 pt-5 lg:px-8">
      <PageHeader title="参数中心">
        <template #actions>
          <label class="flex items-center gap-2 text-caption text-neutral-600">
            集号
            <input v-model.number="epNo" type="number" min="1" class="gd-input w-20" @change="loadData" />
          </label>
          <AppButton variant="secondary" :loading="loading" @click="loadData">刷新</AppButton>
          <AppButton variant="ghost" @click="reassess">重评估</AppButton>
        </template>
      </PageHeader>
    </header>

    <main class="min-h-0 flex-1 overflow-y-auto px-6 pb-10 lg:px-8">
      <div class="mx-auto flex max-w-[1200px] flex-col gap-4">
        <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
          <div class="flex flex-wrap items-center gap-2">
            <button
              v-for="(label, code) in GROUP_LABELS"
              :key="code"
              type="button"
              class="rounded-md border px-3 py-1.5 text-caption transition"
              :class="
                activeGroup === code
                  ? 'border-primary-300 bg-primary-50 text-primary-700'
                  : 'border-hairline bg-surface-inset text-neutral-600'
              "
              @click="activeGroup = code"
            >
              {{ code }} · {{ label }}
            </button>
            <span class="ml-auto font-mono text-caption text-neutral-500">
              配置版本 v{{ config?.version ?? 0 }}
            </span>
          </div>

          <div class="mt-4 flex flex-col gap-2">
            <div
              v-for="item in activeItems"
              :key="item.param_key"
              class="flex flex-wrap items-center justify-between gap-3 rounded-md border border-hairline bg-surface-inset p-3"
            >
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="text-body-sm text-neutral-900">{{ item.label }}</span>
                  <span
                    v-if="item.is_locked"
                    class="rounded-full border border-hairline px-2 py-0.5 text-micro text-neutral-500"
                  >
                    合规锁定
                  </span>
                  <span
                    v-if="item.is_dangerous"
                    class="rounded-full border border-danger-200 px-2 py-0.5 text-micro text-danger-600"
                  >
                    危险参数
                  </span>
                  <span class="font-mono text-micro text-neutral-400">{{ item.param_key }}</span>
                </div>
                <p v-if="item.description" class="mt-1 text-caption text-neutral-500">{{ item.description }}</p>
                <p class="mt-1 font-mono text-micro text-neutral-400">
                  范围 {{ item.hard_min ?? '—' }} ~ {{ item.hard_max ?? '—' }} {{ item.unit || '' }} ·
                  {{ item.effect_scope }}
                </p>
              </div>
              <div class="flex shrink-0 items-center gap-2">
                <input
                  v-if="item.value_type === 'bool'"
                  type="checkbox"
                  :checked="Boolean(draft[item.param_key])"
                  :disabled="item.is_locked"
                  @change="updateDraft(item, $event)"
                />
                <input
                  v-else
                  :value="String(draft[item.param_key] ?? '')"
                  :type="item.value_type === 'int' || item.value_type === 'float' ? 'number' : 'text'"
                  :step="item.value_type === 'float' ? 0.01 : 1"
                  :disabled="item.is_locked"
                  class="gd-input w-32"
                  @input="updateDraft(item, $event)"
                />
                <span class="w-10 text-caption text-neutral-500">{{ item.unit || '' }}</span>
              </div>
            </div>
          </div>

          <div class="mt-4 flex flex-wrap items-end gap-3">
            <label class="flex min-w-[280px] flex-1 flex-col gap-1 text-caption text-neutral-600">
              变更理由{{ hasDangerousChange ? '（危险参数必填，≥10 字）' : '（可选）' }}
              <input v-model="reason" class="gd-input" placeholder="说明为什么调整这些参数" />
            </label>
            <AppButton :loading="saving" :disabled="!Object.keys(changes).length" @click="save">
              保存变更（{{ Object.keys(changes).length }}）
            </AppButton>
            <AppButton variant="ghost" @click="restore">恢复默认</AppButton>
          </div>
        </section>

        <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
          <h2 class="text-title-sm text-neutral-900">版本与回滚</h2>
          <div class="mt-3 flex flex-wrap items-end gap-3">
            <label class="flex flex-col gap-1 text-caption text-neutral-600">
              目标版本
              <select v-model.number="rollbackVersion" class="gd-input w-40">
                <option v-for="version in versions" :key="String(version.version_no)" :value="Number(version.version_no)">
                  v{{ version.version_no }} · {{ version.source }}
                </option>
              </select>
            </label>
            <AppButton variant="secondary" @click="rollback">回滚到此版本</AppButton>
          </div>
          <ul class="mt-3 flex flex-col gap-1 text-caption text-neutral-600">
            <li v-for="version in versions" :key="String(version.version_no)" class="font-mono">
              v{{ version.version_no }} · {{ version.source }} · {{ version.created_at || '' }}
            </li>
          </ul>
        </section>

        <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
          <h2 class="text-title-sm text-neutral-900">变更审计</h2>
          <p v-if="!audits.length" class="mt-2 text-caption text-neutral-500">尚无变更记录。</p>
          <ul v-else class="mt-2 flex flex-col gap-2">
            <li
              v-for="audit in audits"
              :key="String(audit.id)"
              class="rounded-md border border-hairline bg-surface-inset p-2 text-caption text-neutral-600"
            >
              <span class="font-mono">{{ audit.param_key }}</span> ·
              {{ String(audit.old_value) }} → {{ String(audit.new_value) }} ·
              {{ audit.is_dangerous ? '危险' : '常规' }} · {{ audit.created_at || '' }}
              <p v-if="audit.reason" class="mt-1 text-neutral-500">理由：{{ audit.reason }}</p>
            </li>
          </ul>
        </section>
      </div>
    </main>
  </div>
</template>

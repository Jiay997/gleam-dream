<!--
  文件级注释：V6 · 连续性与 Story Bible 页（路由 /projects/:projectId/continuity，覆盖 5A + F2.13）。

  页面结构：
  - Story Bible：角色 / 场景 / 道具设定与完整度检查（缺失项明确提示，不冒充完整）；
  - 连续性事实：从剧本重建、按场查询；
  - 校验：L1 规则 + L2 视觉一致性（缺检记 unavailable，页面突出降级）；
  - 逻辑规则（F2.13）：LR-1~LR-6 问题清单与豁免；
  - 结构化反馈：按维度提交反馈、查看反馈与反例库。

  数据来源：`api/v6.ts` 5A / F2.13 分组。
-->
<script setup lang="ts">
/**
 * ContinuityView 连续性与 Story Bible 页面。
 *
 * 所有“缺失即缺失”的结论都由后端给出：L2 无法比对时返回 unavailable，
 * 页面用降级标记提示，而不是显示为通过。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  createFeedback,
  createLogicWaiver,
  getStoryBible,
  listFacts,
  listFeedback,
  listLogicWaivers,
  listNegativeExamples,
  rebuildFacts,
  runContinuityChecks,
  runLogicCheck,
  upsertBibleCharacter,
  upsertBibleProp,
  upsertBibleScene,
} from '@/api/v6'
import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const toast = useToast()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))
/** 当前集号 */
const epNo = ref(1)
/** Story Bible */
const bible = ref<{
  characters: Array<Record<string, unknown>>
  scenes: Array<Record<string, unknown>>
  props: Array<Record<string, unknown>>
  completeness: Record<string, unknown>
  warning: string | null
} | null>(null)
/** 连续性事实 */
const facts = ref<Array<Record<string, unknown>>>([])
/** 校验汇总 */
const checkSummary = ref<Record<string, number> | null>(null)
/** 是否缺检降级 */
const degraded = ref(false)
/** 逻辑问题 */
const logicIssues = ref<Array<Record<string, unknown>>>([])
/** 豁免列表 */
const waivers = ref<Array<Record<string, unknown>>>([])
/** 反馈列表 */
const feedbacks = ref<Array<Record<string, unknown>>>([])
/** 反例库 */
const negatives = ref<Array<Record<string, unknown>>>([])
/** 是否加载中 */
const loading = ref(false)

/** 角色表单 */
const characterForm = ref({ char_key: '', name: '', base_look: '' })
/** 场景表单 */
const sceneForm = ref({ scene_no: 1, location: '', lighting_base: '' })
/** 道具表单 */
const propForm = ref({ prop_key: '', name: '' })
/** 豁免表单 */
const waiverForm = ref({ rule_code: 'LR-2', target_ref: '', reason: '' })
/** 反馈表单 */
const feedbackForm = ref({ target_type: 'shot', target_id: 1, text: '', expectation_text: '' })

/**
 * 加载 Story Bible、事实、校验与反馈数据。
 * @returns 无
 */
async function loadData(): Promise<void> {
  if (!projectId.value) return
  loading.value = true
  try {
    const [bibleData, factPage, waiverPage, feedbackPage, negativePage] = await Promise.all([
      getStoryBible(projectId.value, epNo.value).catch(() => null),
      listFacts(projectId.value, epNo.value).catch(() => null),
      listLogicWaivers(projectId.value).catch(() => null),
      listFeedback(projectId.value).catch(() => null),
      listNegativeExamples(projectId.value).catch(() => null),
    ])
    bible.value = bibleData
    facts.value = factPage?.items ?? []
    waivers.value = waiverPage?.items ?? []
    feedbacks.value = feedbackPage?.items ?? []
    negatives.value = negativePage?.items ?? []
  } finally {
    loading.value = false
  }
}

/**
 * 保存 Story Bible 角色。
 * @returns 无
 */
async function saveCharacter(): Promise<void> {
  if (!characterForm.value.char_key.trim() || !characterForm.value.name.trim()) {
    toast.warning('请填写角色键与名称')
    return
  }
  await upsertBibleCharacter(projectId.value, {
    char_key: characterForm.value.char_key.trim(),
    name: characterForm.value.name.trim(),
    base_look: characterForm.value.base_look || null,
  })
  toast.success('角色设定已保存')
  await loadData()
}

/**
 * 保存 Story Bible 场景。
 * @returns 无
 */
async function saveScene(): Promise<void> {
  await upsertBibleScene(projectId.value, epNo.value, sceneForm.value.scene_no, {
    location: sceneForm.value.location || null,
    lighting_base: sceneForm.value.lighting_base || null,
    key_points: [],
    elapsed_minutes: 0,
  })
  toast.success('场景设定已保存')
  await loadData()
}

/**
 * 保存 Story Bible 道具。
 * @returns 无
 */
async function saveProp(): Promise<void> {
  if (!propForm.value.prop_key.trim() || !propForm.value.name.trim()) {
    toast.warning('请填写道具键与名称')
    return
  }
  await upsertBibleProp(projectId.value, {
    prop_key: propForm.value.prop_key.trim(),
    name: propForm.value.name.trim(),
  })
  toast.success('道具设定已保存')
  await loadData()
}

/**
 * 从剧本重建连续性事实。
 * @returns 无
 */
async function rebuild(): Promise<void> {
  await rebuildFacts(projectId.value, epNo.value, true)
  toast.success('连续性事实已重建')
  await loadData()
}

/**
 * 执行 L1 + L2 连续性校验。
 * @returns 无
 */
async function runChecks(): Promise<void> {
  const result = await runContinuityChecks(projectId.value, epNo.value)
  checkSummary.value = result.summary
  degraded.value = result.degraded
  toast.success(`校验完成，降级 ${result.degraded ? '是' : '否'}`)
}

/**
 * 执行 F2.13 逻辑规则检查。
 * @returns 无
 */
async function runLogic(): Promise<void> {
  const result = await runLogicCheck(projectId.value, epNo.value)
  logicIssues.value = result.issues
  toast.success(`检查完成，发现 ${result.issues.length} 个问题`)
}

/**
 * 创建逻辑规则豁免。
 * @returns 无
 */
async function addWaiver(): Promise<void> {
  if (waiverForm.value.reason.trim().length < 10) {
    toast.warning('豁免理由不少于 10 个字')
    return
  }
  if (!waiverForm.value.target_ref.trim()) {
    toast.warning('请填写问题定位引用')
    return
  }
  await createLogicWaiver(
    projectId.value,
    waiverForm.value.rule_code,
    waiverForm.value.target_ref.trim(),
    waiverForm.value.reason.trim(),
  )
  toast.success('豁免已创建')
  waiverForm.value.reason = ''
  await loadData()
}

/**
 * 写入结构化反馈。
 * @returns 无
 */
async function addFeedback(): Promise<void> {
  if (!feedbackForm.value.text.trim()) {
    toast.warning('请填写反馈内容')
    return
  }
  await createFeedback(projectId.value, {
    target_type: feedbackForm.value.target_type,
    target_id: Number(feedbackForm.value.target_id),
    dimensions: ['face'],
    expectation: 'keep_identity',
    expectation_text: feedbackForm.value.expectation_text || undefined,
    text: feedbackForm.value.text.trim(),
    source_gate: 'manual',
  })
  toast.success('反馈已提交')
  feedbackForm.value.text = ''
  await loadData()
}

/**
 * 把后端返回的未知值格式化为列表文本。
 * @param value 原始值（通常为字符串数组）
 * @returns 顿号分隔文本；空值返回破折号
 */
function listText(value: unknown): string {
  if (Array.isArray(value)) return value.join('、') || '—'
  return value ? String(value) : '—'
}

onMounted(loadData)
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <header class="shrink-0 px-6 pb-4 pt-5 lg:px-8">
      <PageHeader title="连续性与 Story Bible">
        <template #actions>
          <label class="flex items-center gap-2 text-caption text-neutral-600">
            集号
            <input v-model.number="epNo" type="number" min="1" class="gd-input w-20" @change="loadData" />
          </label>
          <AppButton variant="secondary" :loading="loading" @click="loadData">刷新</AppButton>
          <AppButton variant="secondary" @click="runChecks">运行连续性校验</AppButton>
          <AppButton @click="runLogic">运行逻辑检查</AppButton>
        </template>
      </PageHeader>
    </header>

    <main class="min-h-0 flex-1 overflow-y-auto px-6 pb-10 lg:px-8">
      <div class="mx-auto flex max-w-[1200px] flex-col gap-4">
        <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-title-sm text-neutral-900">Story Bible 完整度</h2>
            <span
              class="text-caption"
              :class="bible?.warning ? 'text-danger-600' : 'text-neutral-500'"
            >
              {{ bible?.warning || '设定完整' }}
            </span>
          </div>
          <p v-if="bible" class="mt-2 font-mono text-caption text-neutral-500">
            已填写 {{ bible.completeness.filled ?? 0 }} / {{ bible.completeness.checks ?? 0 }}
          </p>
          <div class="mt-3 grid gap-3 lg:grid-cols-3">
            <div class="rounded-md border border-hairline bg-surface-inset p-3">
              <h3 class="text-body-sm text-neutral-800">角色</h3>
              <ul class="mt-1 flex flex-col gap-1 text-caption text-neutral-600">
                <li v-for="item in bible?.characters ?? []" :key="String(item.id)">
                  {{ item.name }} · {{ item.base_look || '未填外形' }}
                </li>
                <li v-if="!(bible?.characters ?? []).length">暂无角色设定</li>
              </ul>
              <div class="mt-2 flex flex-col gap-1">
                <input v-model="characterForm.char_key" class="gd-input" placeholder="角色键（如 lin_wan）" />
                <input v-model="characterForm.name" class="gd-input" placeholder="角色名" />
                <input v-model="characterForm.base_look" class="gd-input" placeholder="基础外形描述" />
                <AppButton size="sm" variant="secondary" @click="saveCharacter">保存角色</AppButton>
              </div>
            </div>
            <div class="rounded-md border border-hairline bg-surface-inset p-3">
              <h3 class="text-body-sm text-neutral-800">场景</h3>
              <ul class="mt-1 flex flex-col gap-1 text-caption text-neutral-600">
                <li v-for="item in bible?.scenes ?? []" :key="String(item.id)">
                  {{ item.scene_no }} · {{ item.location || '未填地点' }}
                </li>
                <li v-if="!(bible?.scenes ?? []).length">暂无场景设定</li>
              </ul>
              <div class="mt-2 flex flex-col gap-1">
                <input v-model.number="sceneForm.scene_no" type="number" min="1" class="gd-input" />
                <input v-model="sceneForm.location" class="gd-input" placeholder="地点" />
                <input v-model="sceneForm.lighting_base" class="gd-input" placeholder="光线基线" />
                <AppButton size="sm" variant="secondary" @click="saveScene">保存场景</AppButton>
              </div>
            </div>
            <div class="rounded-md border border-hairline bg-surface-inset p-3">
              <h3 class="text-body-sm text-neutral-800">道具</h3>
              <ul class="mt-1 flex flex-col gap-1 text-caption text-neutral-600">
                <li v-for="item in bible?.props ?? []" :key="String(item.id)">
                  {{ item.name }}
                </li>
                <li v-if="!(bible?.props ?? []).length">暂无道具设定</li>
              </ul>
              <div class="mt-2 flex flex-col gap-1">
                <input v-model="propForm.prop_key" class="gd-input" placeholder="道具键" />
                <input v-model="propForm.name" class="gd-input" placeholder="道具名" />
                <AppButton size="sm" variant="secondary" @click="saveProp">保存道具</AppButton>
              </div>
            </div>
          </div>
        </section>

        <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <h2 class="text-title-sm text-neutral-900">连续性事实与校验</h2>
            <AppButton size="sm" variant="secondary" @click="rebuild">从剧本重建事实</AppButton>
          </div>
          <div v-if="checkSummary" class="mt-3 flex flex-wrap gap-3 text-caption">
            <span class="rounded-md border border-hairline px-2 py-1">通过 {{ checkSummary.pass ?? 0 }}</span>
            <span class="rounded-md border border-hairline px-2 py-1">失败 {{ checkSummary.fail ?? 0 }}</span>
            <span class="rounded-md border border-hairline px-2 py-1">缺检 {{ checkSummary.unavailable ?? 0 }}</span>
            <span v-if="degraded" class="rounded-md border border-danger-200 px-2 py-1 text-danger-600">
              本次存在缺检降级，未冒充通过
            </span>
          </div>
          <EmptyState
            v-if="!facts.length"
            title="尚无连续性事实"
            description="点击「从剧本重建事实」按场次抽取角色、服装、时间与道具状态。"
            icon-path="M3.75 6A2.25 2.25 0 016 3.75h3v16.5H6A2.25 2.25 0 013.75 18V6z"
          />
          <div v-else class="mt-3 overflow-x-auto">
            <table class="w-full min-w-[720px] border-collapse text-left text-body-sm">
              <thead class="text-caption text-neutral-500">
                <tr class="border-b border-hairline">
                  <th class="py-2 pr-3">集/场</th>
                  <th class="py-2 pr-3">摘要</th>
                  <th class="py-2 pr-3">角色</th>
                  <th class="py-2 pr-3">完整度</th>
                  <th class="py-2">版本</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="fact in facts" :key="String(fact.id)" class="border-b border-hairline align-top">
                  <td class="py-2 pr-3 font-mono text-caption text-neutral-500">
                    {{ fact.ep_no }}-{{ fact.scene_no }}
                  </td>
                  <td class="py-2 pr-3 text-neutral-800">{{ fact.summary || '—' }}</td>
                  <td class="py-2 pr-3 text-neutral-600">{{ listText(fact.characters) }}</td>
                  <td class="py-2 pr-3 font-mono text-caption text-neutral-600">
                    {{ fact.completeness ?? '—' }}
                  </td>
                  <td class="py-2 font-mono text-caption text-neutral-500">v{{ fact.version }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
          <h2 class="text-title-sm text-neutral-900">逻辑规则（F2.13）</h2>
          <p v-if="!logicIssues.length" class="mt-2 text-caption text-neutral-500">
            尚未运行检查，或当前没有逻辑问题。
          </p>
          <ul v-else class="mt-2 flex flex-col gap-2">
            <li
              v-for="(issue, index) in logicIssues"
              :key="index"
              class="rounded-md border border-hairline bg-surface-inset p-2 text-caption text-neutral-700"
            >
              <span class="mr-2 font-mono text-danger-600">{{ issue.rule_code }}</span>
              {{ issue.message }}
              <span class="ml-2 font-mono text-neutral-400">{{ issue.target_ref }}</span>
            </li>
          </ul>
          <div class="mt-3 flex flex-wrap items-end gap-2">
            <select v-model="waiverForm.rule_code" class="gd-input w-28">
              <option value="LR-1">LR-1</option>
              <option value="LR-2">LR-2</option>
              <option value="LR-3">LR-3</option>
              <option value="LR-4">LR-4</option>
              <option value="LR-5">LR-5</option>
              <option value="LR-6">LR-6</option>
            </select>
            <input v-model="waiverForm.target_ref" class="gd-input w-48" placeholder="定位引用（ep:scene）" />
            <input v-model="waiverForm.reason" class="gd-input min-w-[260px] flex-1" placeholder="豁免理由（≥10 字）" />
            <AppButton size="sm" variant="secondary" @click="addWaiver">创建豁免</AppButton>
          </div>
          <ul v-if="waivers.length" class="mt-2 flex flex-col gap-1 text-caption text-neutral-600">
            <li v-for="waiver in waivers" :key="String(waiver.id)" class="font-mono">
              {{ waiver.rule_code }} · {{ waiver.target_ref }} · {{ waiver.reason }}
            </li>
          </ul>
        </section>

        <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
          <h2 class="text-title-sm text-neutral-900">结构化反馈与反例库</h2>
          <div class="mt-3 flex flex-wrap items-end gap-2">
            <select v-model="feedbackForm.target_type" class="gd-input w-32">
              <option value="shot">镜头</option>
              <option value="asset">资产</option>
              <option value="script">剧本</option>
            </select>
            <input v-model.number="feedbackForm.target_id" type="number" min="1" class="gd-input w-24" />
            <input v-model="feedbackForm.text" class="gd-input min-w-[260px] flex-1" placeholder="具体问题（如：脸型偏差明显）" />
            <AppButton size="sm" variant="secondary" @click="addFeedback">提交反馈</AppButton>
          </div>
          <div class="mt-3 grid gap-3 lg:grid-cols-2">
            <div>
              <h3 class="text-body-sm text-neutral-800">最近反馈（{{ feedbacks.length }}）</h3>
              <ul class="mt-1 flex flex-col gap-1 text-caption text-neutral-600">
                <li v-for="feedback in feedbacks" :key="String(feedback.id)">
                  {{ feedback.target_type }}#{{ feedback.target_id }} · {{ feedback.text }}
                </li>
              </ul>
            </div>
            <div>
              <h3 class="text-body-sm text-neutral-800">反例库（{{ negatives.length }}）</h3>
              <ul class="mt-1 flex flex-col gap-1 text-caption text-neutral-600">
                <li v-for="negative in negatives" :key="String(negative.id)">
                  {{ negative.dimension }} · {{ negative.wrong_text }} → {{ negative.right_text }}
                </li>
              </ul>
            </div>
          </div>
        </section>
      </div>
    </main>
  </div>
</template>

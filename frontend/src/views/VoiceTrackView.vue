<!--
  文件级注释：V6 · 配音轨页（路由 /projects/:projectId/voice，覆盖 F9 + F2.12）。

  页面结构（v2 版式：主工作区 + 参数轨）：
  - 刊头：集号 / 刷新 / 生成整集配音（页面唯一主行动）；
  - 左侧主区：台词与配音合并为一张「台本-音频」清单。后端保证一条台词对应
    至多一个配音片段（uq_voice_clips_track_line），因此把原先「台词清单 + 配音轨」
    两张卡合并成一行一句，消除同一内容在页面上出现两次的重复阅读；
  - 右侧参数轨：角色音色 / 气口 → 完成度 → 关口 V 审批，按「设置 → 校验 → 决策」
    的操作动线自上而下排列，审批与调参不再混在同一卡片里；
  - 配音详情仍以弹窗承载（完整属性 + 试听 + 词级时间戳）。

  数据来源：`api/v6.ts`（F9 配音轨 / F2.12 台词清单）。所有写操作完成后统一
  `reload`，保证列表与报告不出现前后不一致。
-->
<script setup lang="ts">
/**
 * VoiceTrackView 配音轨页面。
 *
 * 依赖当前路由项目上下文；集号默认为 1，可手动切换。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { listAssets } from '@/api/assets'
import type { AssetItem } from '@/api/assets'
import {
  approveVoiceTrack,
  confirmVoiceParams,
  generateVoiceTrack,
  getVoiceParams,
  getVoiceReport,
  getVoiceTrack,
  listLines,
  regenerateVoiceClip,
  splitLine,
  updateLine,
  updateVoiceTrack,
} from '@/api/v6'
import type { ScriptLineItem, VoiceClipItem, VoiceReport, VoiceTrackDetail } from '@/api/v6'
import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import Modal from '@/components/common/Modal.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import { EMOTION_TAG_OPTIONS, optionLabel } from '@/constants/storyboard'

const route = useRoute()
const router = useRouter()
const toast = useToast()

/**
 * 情绪中文标签（修复测试报告 #18：英文枚举 sadness/surprise 等直接展示）。
 * @param key 情绪枚举 key
 * @returns 中文标签；未收录回退原样 key
 */
function emotionLabel(key?: string | null): string {
  return optionLabel(EMOTION_TAG_OPTIONS, key)
}

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))
/** 当前集号 */
const epNo = ref(1)
/** 角色资产 */
const characters = ref<AssetItem[]>([])
/** 当前选中角色 */
const characterId = ref<number | null>(null)
/** 当前音色 ID（初始取角色已绑定音色） */
const voiceId = ref<number>(1)
/** 语速 */
const rate = ref(1)
/** 音调 */
const pitch = ref(0)
/** 台词清单 */
const lines = ref<ScriptLineItem[]>([])
/** 配音轨 */
const track = ref<VoiceTrackDetail | null>(null)
/** 完成度报告 */
const report = ref<VoiceReport | null>(null)
/** 气口毫秒 */
const gapMs = ref(120)
/** 是否加载中 */
const loading = ref(false)
/** 是否生成中 */
const generating = ref(false)
/** 正在编辑的台词 ID */
const editingId = ref<number | null>(null)
/** 编辑缓冲文本 */
const editingText = ref('')
/** 详情弹窗当前展示的配音片段（null = 弹窗关闭） */
const detailClip = ref<VoiceClipItem | null>(null)
/** 详情弹窗显示状态 */
const detailOpen = ref(false)

/**
 * 加载页面数据（角色 / 台词 / 轨道 / 报告）。
 * @returns 无
 */
async function loadData(): Promise<void> {
  if (!projectId.value) return
  loading.value = true
  try {
    const [assetPage, linePage, trackData, reportData] = await Promise.all([
      listAssets(projectId.value, { page: 1, page_size: 100 }),
      listLines(projectId.value, epNo.value),
      getVoiceTrack(projectId.value, epNo.value).catch(() => null),
      getVoiceReport(projectId.value, epNo.value).catch(() => null),
    ])
    characters.value = (assetPage.items ?? []).filter((item) => item.asset_type === 'character')
    if (characterId.value == null && characters.value.length) {
      characterId.value = characters.value[0].id
    }
    // P2-9：与后端配音口径对齐——只展示可配音的对白/旁白行。
    // 此前把动作/转场等非台词行也列进清单（实测 30 行 vs 实际可配 16 行），
    // 导致完成率分母偏大、进度永远显示不满。
    lines.value = (linePage.items ?? []).filter(
      (line) => line.line_type === 'dialogue' || line.line_type === 'narration',
    )
    track.value = trackData
    report.value = reportData
    if (trackData) gapMs.value = trackData.track?.gap_ms ?? trackData.gap_ms
  } finally {
    loading.value = false
  }
}

/**
 * 读取当前角色的配音参数并回填表单。
 * @returns 无
 */
async function loadVoiceParams(): Promise<void> {
  if (!projectId.value || characterId.value == null) return
  const param = await getVoiceParams(projectId.value, characterId.value).catch(() => null)
  if (param) {
    voiceId.value = Number(param.voice_id ?? voiceId.value)
    rate.value = Number(param.rate ?? 1)
    pitch.value = Number(param.pitch ?? 0)
  }
}

/**
 * 确认角色音色参数（新版本生效）。
 * @returns 无
 */
async function saveVoiceParams(): Promise<void> {
  if (characterId.value == null) {
    toast.warning('请先选择角色')
    return
  }
  await confirmVoiceParams(projectId.value, characterId.value, {
    voice_id: voiceId.value,
    rate: rate.value,
    pitch: pitch.value,
  })
  toast.success('音色参数已生效')
}

/** 是否具备生成配音的前置条件（本集存在可配音台词且当前无生成任务） */
const canGenerateVoice = computed(() => lines.value.length > 0 && !generating.value)

/** 「生成整集配音」按钮不可用原因（hover 提示，避免点击后才报错） */
const generateHint = computed(() => {
  if (lines.value.length === 0) return '本集没有可配音台词：请先在剧本页补充对白 / 旁白'
  if (generating.value) return '正在生成整集配音，请稍候…'
  return '按当前角色音色逐句合成并写回配音轨'
})

/** 「关口 V 审批」按钮不可用原因（hover 提示） */
const approveHint = computed(() => {
  if (lines.value.length === 0) return '本集没有可配音台词，无法提交配音审批'
  if (!track.value) return '请先生成整集配音，再提交关口 V 审批'
  return '通过后整集配音锁定，作为合成与成片交付的唯一音轨来源'
})

/**
 * 跳转剧本编辑页（空态引导：先补台词再回本页配音）。
 * @returns 无
 */
function goScriptEditor(): void {
  void router.push(`/projects/${projectId.value}/script`)
}

/**
 * 生成整集配音。
 * 前置条件未满足时直接提示并返回，避免向后端发起无效请求。
 * @returns 无
 */
async function generate(): Promise<void> {
  if (lines.value.length === 0) {
    toast.warning('本集没有可配音台词：请先在剧本页补充对白 / 旁白')
    return
  }
  if (generating.value) return
  generating.value = true
  try {
    const result = await generateVoiceTrack(projectId.value, epNo.value)
    toast.success(`已生成 ${result.generated} 句，跳过 ${result.skipped} 句`)
    await loadData()
  } finally {
    generating.value = false
  }
}

/**
 * 调整配音轨气口。
 * @returns 无
 */
async function saveGap(): Promise<void> {
  await updateVoiceTrack(projectId.value, epNo.value, gapMs.value)
  toast.success('气口已更新')
  await loadData()
}

/**
 * 关口 V 审批整集配音。
 * @param decision 通过或打回
 * @returns 无
 */
async function approve(decision: 'approve' | 'reject'): Promise<void> {
  const result = await approveVoiceTrack(projectId.value, epNo.value, decision)
  toast.success(decision === 'approve' ? '配音已通过并锁定' : '已打回配音')
  track.value = result.track ?? track.value
  await loadData()
}

/**
 * 开始编辑某句台词。
 * @param line 台词
 * @returns 无
 */
function startEdit(line: ScriptLineItem): void {
  editingId.value = line.id
  editingText.value = line.content
}

/**
 * 保存台词编辑（文本变化会重算预估时长）。
 * @param line 台词
 * @returns 无
 */
async function saveEdit(line: ScriptLineItem): Promise<void> {
  if (editingText.value.trim() === '') {
    toast.warning('台词不能为空')
    return
  }
  try {
    await updateLine(projectId.value, line.id, { content: editingText.value.trim() })
    editingId.value = null
    toast.success('台词已更新')
    await loadData()
  } catch {
    // 拦截器已 toast，此处仅兜底捕获避免未处理拒绝
  }
}

/**
 * 拆分超长台词为两条（不传 position，由后端在句读标点处智能切分，
 * 修复测试报告 #22 的字符中点切断问题）。
 * @param line 台词
 * @returns 无
 */
async function split(line: ScriptLineItem): Promise<void> {
  try {
    await splitLine(projectId.value, line.id, {})
    toast.success('已拆分为两句')
    await loadData()
  } catch {
    // 拦截器已 toast，此处仅兜底捕获避免未处理拒绝
  }
}

/**
 * 单句重配（行内按钮与详情弹窗共用）。
 * @param clipId 配音片段 ID
 * @param fromDetail 是否从详情弹窗发起（完成后关闭弹窗）
 * @returns 无
 */
async function regenerate(clipId: number, fromDetail = false): Promise<void> {
  // P3-f 修复：未配音句（clip 缺失或 id 为空）直接提示，避免拼出
  // /voice/clips/null/regenerate 触发后端 422
  if (clipId === null || clipId === undefined || Number.isNaN(Number(clipId))) {
    toast.warning('该句尚未生成配音，请先完成整集配音生成后再重配')
    return
  }
  try {
    await regenerateVoiceClip(projectId.value, clipId, { rate: rate.value, pitch: pitch.value })
    toast.success('单句已重配')
    if (fromDetail) {
      closeDetail()
    }
    await loadData()
  } catch {
    // 拦截器已 toast，此处仅兜底捕获避免未处理拒绝
  }
}

/**
 * 打开某条配音的详情弹窗（查看完整属性并试听）。
 * @param clip 配音片段
 * @returns 无
 */
function openDetail(clip: VoiceClipItem): void {
  detailClip.value = clip
  detailOpen.value = true
}

/**
 * 关闭详情弹窗并清空选中片段。
 * @returns 无
 */
function closeDetail(): void {
  detailOpen.value = false
  detailClip.value = null
}

/**
 * 播放某句配音。
 * @param url 音频地址
 * @returns 无
 */
function play(url: string | null): void {
  if (!url) {
    toast.warning('该句暂无可试听音频')
    return
  }
  const audio = new Audio(url)
  // 资源加载失败（如后端 /files 静态服务缺失或文件未落盘）时给出明确提示，
  // 避免点击后无任何反馈被误判为“按钮失效”。
  audio.addEventListener('error', () => {
    toast.warning('音频加载失败，请确认后端文件服务可用')
  })
  void audio.play().catch(() => {
    toast.warning('浏览器拒绝播放，请检查音频格式或再次点击')
  })
}

/* ---------- 版面派生数据（只读，不改变数据流） ---------- */

/** 台词 ID → 配音片段映射（后端唯一约束保证一句一片） */
const clipByLine = computed<Map<number, VoiceClipItem>>(() => {
  const map = new Map<number, VoiceClipItem>()
  track.value?.clips.forEach((clip) => map.set(clip.line_id, clip))
  return map
})

/** 清单行：一条台词 + 它对应的配音片段（未生成时为 null） */
interface LineRow {
  /** 台词 */
  line: ScriptLineItem
  /** 配音片段 */
  clip: VoiceClipItem | null
}

/** 合并后的清单行（保持台词原有顺序） */
const rows = computed<LineRow[]>(() =>
  lines.value.map((line) => ({ line, clip: clipByLine.value.get(line.id) ?? null })),
)

/** 已配音句数（优先取完成度报告，缺失时回退到片段计数） */
const voicedCount = computed(() => {
  if (report.value) return report.value.voiced_count
  return track.value?.clips.filter((clip) => clip.status === 'voiced').length ?? 0
})

/** 完成率（0–100 整数，按台词条数计算；报告缺失时回退到本地计数） */
const coveragePct = computed(() => {
  const total = report.value?.line_count ?? lines.value.length
  const done = report.value?.voiced_count ?? voicedCount.value
  return total > 0 ? Math.round((done / total) * 100) : 0
})

/** 对齐降级片段数（由片段直接统计，报告只提供布尔汇总） */
const degradedCount = computed(() => track.value?.clips.filter((clip) => clip.align_degraded).length ?? 0)

/** 整集配音总时长文案 */
const totalDurationLabel = computed(() =>
  // 后端返回的整集时长位于 data.track.total_ms（顶层 total_ms 为兼容旧结构保留）
  track.value
    ? `${(((track.value.track?.total_ms ?? track.value.total_ms) ?? 0) / 1000).toFixed(1)}s`
    : '—',
)

/**
 * 毫秒 → 秒的紧凑文案。
 * @param ms 毫秒；空值表示尚未测量
 * @returns 形如 `1.24s`，或 `—`
 */
function msToSec(ms: number | null | undefined): string {
  return ms == null ? '—' : `${(ms / 1000).toFixed(2)}s`
}

/** 台词行号补零（版面统一两位，便于纵向扫读） */
function lineNoLabel(no: number): string {
  return String(no).padStart(2, '0')
}

watch(characterId, loadVoiceParams)
watch(epNo, loadData)
onMounted(async () => {
  await loadData()
  await loadVoiceParams()
})
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <header class="shrink-0 px-6 pb-4 pt-5 lg:px-8">
      <PageHeader title="配音轨">
        <template #actions>
          <label class="flex items-center gap-2 rounded-lg border border-hairline bg-surface-canvas px-3 py-1.5 text-caption text-ink-mute">
            <span class="shrink-0">集号</span>
            <input
              v-model.number="epNo"
              type="number"
              min="1"
              class="gd-num w-12 bg-transparent text-center text-caption text-ink focus:outline-none"
              aria-label="集号"
            />
          </label>
          <AppButton variant="ghost" :loading="loading" @click="loadData">刷新</AppButton>
          <AppButton
            :loading="generating"
            :disabled="!canGenerateVoice"
            :title="generateHint"
            @click="generate"
          >
            生成整集配音
          </AppButton>
        </template>
      </PageHeader>
    </header>

    <main class="min-h-0 flex-1 overflow-y-auto px-6 pb-10 lg:px-8">
      <div class="mx-auto grid max-w-[1320px] grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <!-- ============ 左：台词 ↔ 配音 主工作区 ============ -->
        <section class="self-start overflow-hidden rounded-xl border border-hairline bg-surface-canvas">
          <div class="flex flex-wrap items-center justify-between gap-3 border-b border-hairline px-4 py-3">
            <div class="flex min-w-0 items-center gap-2.5">
              <h2 class="text-title font-semibold text-ink">台词与配音</h2>
              <span class="gd-num text-caption text-ink-mute">{{ lines.length }} 句</span>
            </div>
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-ink-mute">
              <span>已配音 <b class="gd-num text-ink">{{ voicedCount }}</b></span>
              <span class="gd-junction-v h-3" aria-hidden="true" />
              <span>总时长 <b class="gd-num text-ink">{{ totalDurationLabel }}</b></span>
              <StatusTag v-if="track" :status="track.status" size="sm" />
            </div>
          </div>

          <EmptyState
            v-if="!lines.length"
            title="本集暂无台词"
            description="配音需要台词清单：先在剧本编辑页生成或补充对白 / 旁白，再回到本页配音。"
            action-text="去剧本编辑页 →"
            icon-path="M3.75 6A2.25 2.25 0 016 3.75h3v16.5H6A2.25 2.25 0 013.75 18V6z"
            @action="goScriptEditor"
          />

          <ul v-else class="divide-y divide-hairline">
            <li
              v-for="row in rows"
              :key="row.line.id"
              class="group flex items-start gap-3 px-4 py-3 transition-colors hover:bg-state-hover"
            >
              <!-- 行号：纵向扫读的锚点 -->
              <span
                class="gd-num mt-0.5 flex h-6 w-7 shrink-0 items-center justify-center rounded border border-hairline bg-surface-inset text-micro text-ink-mute"
              >
                {{ lineNoLabel(row.line.line_no) }}
              </span>

              <!-- 台词主体 -->
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span class="text-caption font-medium text-ink">{{ row.line.speaker_name || '旁白 / 动作' }}</span>
                  <StatusTag :status="row.line.voice_status" size="sm" />
                  <!-- P1 修复：展示实际配音情绪，供人设契合度/情绪表达评审 -->
                  <span
                    v-if="row.clip?.emotion || row.line.emotion_tag"
                    class="inline-flex items-center rounded-full bg-surface-inset px-2 py-0.5 text-micro text-ink-soft"
                  >
                    情绪 · {{ emotionLabel(row.clip?.emotion || row.line.emotion_tag) }}
                  </span>
                  <span
                    v-if="row.clip?.align_degraded"
                    class="inline-flex items-center rounded-full bg-warning-bg px-2 py-0.5 text-micro text-warning-bright"
                  >
                    占位音·非真实人声
                  </span>
                  <span class="gd-num text-micro text-ink-mute">预估 {{ msToSec(row.line.est_ms) }}</span>
                  <span class="gd-num text-micro text-ink-mute">
                    实测 {{ msToSec(row.clip?.duration_ms ?? row.line.actual_ms) }}
                  </span>
                </div>

                <input
                  v-if="editingId === row.line.id"
                  v-model="editingText"
                  class="gd-input gd-field-sm mt-1.5 w-full"
                  aria-label="编辑台词"
                  @keyup.enter="saveEdit(row.line)"
                />
                <p v-else class="mt-1 text-sm leading-relaxed text-ink">{{ row.line.content }}</p>
              </div>

              <!-- 操作区：音频操作在上、文本操作在下，避免五个按钮挤成一行 -->
              <div class="flex shrink-0 flex-col items-end gap-0.5">
                <div v-if="row.clip" class="flex items-center gap-0.5">
                  <AppButton size="sm" variant="text" @click="play(row.clip.audio_url)">试听</AppButton>
                  <AppButton size="sm" variant="text" @click="regenerate(row.clip.id)">重配</AppButton>
                  <AppButton size="sm" variant="text" @click="openDetail(row.clip)">详情</AppButton>
                </div>
                <span v-else class="flex h-8 items-center pr-1 text-caption text-ink-mute">待生成</span>
                <div class="flex items-center gap-0.5">
                  <AppButton
                    v-if="editingId === row.line.id"
                    size="sm"
                    variant="text"
                    @click="saveEdit(row.line)"
                  >
                    保存
                  </AppButton>
                  <AppButton v-else size="sm" variant="text" @click="startEdit(row.line)">编辑</AppButton>
                  <AppButton size="sm" variant="text" @click="split(row.line)">拆分</AppButton>
                </div>
              </div>
            </li>
          </ul>
        </section>

        <!-- ============ 右：参数轨（设置 → 校验 → 决策） ============ -->
        <aside class="flex flex-col gap-4 self-start">
          <!-- ① 角色音色 + 气口 -->
          <section class="rounded-xl border border-hairline bg-surface-canvas p-4">
            <div class="flex items-center justify-between gap-2">
              <h2 class="text-title font-semibold text-ink">角色音色</h2>
              <span class="gd-section-label">VOICE PARAMS</span>
            </div>

            <div class="mt-3 flex flex-col gap-3">
              <label class="flex flex-col gap-1.5 text-caption text-ink-mute">
                角色
                <select v-model.number="characterId" class="gd-input w-full">
                  <option :value="null" disabled>请选择角色</option>
                  <option v-for="item in characters" :key="item.id" :value="item.id">{{ item.name }}</option>
                </select>
              </label>

              <div class="grid grid-cols-3 gap-2">
                <label class="flex min-w-0 flex-col gap-1.5 text-caption text-ink-mute">
                  音色 ID
                  <input v-model.number="voiceId" type="number" min="1" class="gd-input w-full" />
                </label>
                <label class="flex min-w-0 flex-col gap-1.5 text-caption text-ink-mute">
                  语速
                  <input v-model.number="rate" type="number" step="0.05" min="0.5" max="2" class="gd-input w-full" />
                </label>
                <label class="flex min-w-0 flex-col gap-1.5 text-caption text-ink-mute">
                  音调
                  <input v-model.number="pitch" type="number" step="1" min="-12" max="12" class="gd-input w-full" />
                </label>
              </div>

              <AppButton
                variant="secondary"
                block
                :disabled="characterId == null"
                :title="characterId == null ? '请先选择角色，再确认音色参数' : '保存后作用于本角色后续配音生成'"
                @click="saveVoiceParams"
              >
                确认音色参数
              </AppButton>
              <p class="text-caption leading-relaxed text-ink-mute">
                参数作用于本角色后续生成；已配音的句子可用行内「重配」逐句刷新。
              </p>
            </div>

            <div class="my-4 h-px bg-hairline" aria-hidden="true" />

            <div class="flex items-end gap-2">
              <label class="flex flex-1 flex-col gap-1.5 text-caption text-ink-mute">
                气口（句间停顿）
                <span class="flex items-center gap-1.5">
                  <input v-model.number="gapMs" type="number" min="0" max="2000" class="gd-input gd-field-sm w-full" />
                  <span class="text-caption text-ink-mute">ms</span>
                </span>
              </label>
              <AppButton
                size="sm"
                variant="secondary"
                :disabled="!track"
                :title="track ? '保存句间停顿到配音轨' : '请先生成整集配音，再调整气口'"
                @click="saveGap"
              >
                保存
              </AppButton>
            </div>
          </section>

          <!-- ② 完成度 -->
          <section class="rounded-xl border border-hairline bg-surface-canvas p-4">
            <h2 class="text-title font-semibold text-ink">完成度</h2>

            <div class="mt-3 flex items-end justify-between gap-2">
              <span class="gd-num text-display text-ink">{{ coveragePct }}<span class="text-caption text-ink-mute">%</span></span>
              <span class="text-caption text-ink-mute">已配音 {{ voicedCount }} / {{ lines.length }} 句</span>
            </div>
            <div class="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-inset" role="progressbar" :aria-valuenow="coveragePct" aria-valuemin="0" aria-valuemax="100">
              <div class="h-full rounded-full bg-grad-primary" :style="{ width: `${coveragePct}%` }" />
            </div>

            <dl class="mt-4 grid grid-cols-2 gap-x-3 gap-y-3">
              <div>
                <dt class="text-caption text-ink-mute">台词条数</dt>
                <dd class="gd-num text-title font-semibold text-ink">{{ report?.line_count ?? lines.length }}</dd>
              </div>
              <div>
                <dt class="text-caption text-ink-mute">已配音</dt>
                <dd class="gd-num text-title font-semibold text-ink">{{ voicedCount }}</dd>
              </div>
              <div>
                <dt class="text-caption text-ink-mute">占位音</dt>
                <dd class="gd-num text-title font-semibold" :class="degradedCount > 0 ? 'text-warning-bright' : 'text-ink'">
                  {{ degradedCount }}
                </dd>
              </div>
              <div>
                <dt class="text-caption text-ink-mute">生成失败</dt>
                <dd class="gd-num text-title font-semibold" :class="(report?.failed_count ?? 0) > 0 ? 'text-danger-strong' : 'text-ink'">
                  {{ report?.failed_count ?? 0 }}
                </dd>
              </div>
            </dl>
            <p
              v-if="degradedCount > 0"
              class="mt-3 rounded-lg border border-warning-border bg-warning-bg px-3 py-2 text-caption text-warning-strong"
            >
              本集有 {{ degradedCount }} 句为沙箱占位音频（非真实人声），合成时会被后端拦截（70005）。请点击对应行「重配」生成真实配音。
            </p>
          </section>

          <!-- ③ 关口 V 审批 -->
          <section class="rounded-xl border border-primary-200 bg-primary-50 p-4">
            <div class="flex items-center justify-between gap-2">
              <div class="flex items-center gap-2">
                <span class="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-primary-200 bg-primary-100 font-serif text-caption font-semibold text-primary-300" aria-hidden="true">V</span>
                <h2 class="text-title font-semibold text-ink">关口 V · 配音审批</h2>
              </div>
              <StatusTag v-if="track" :status="track.status" size="sm" />
            </div>
            <p class="mt-2 text-caption leading-relaxed text-ink-soft">
              通过后整集配音锁定，作为合成与成片交付的唯一音轨来源；打回则解除锁定，可继续改稿重配。
            </p>
            <div class="mt-3 flex gap-2">
              <AppButton
                variant="secondary"
                block
                :disabled="!track"
                :title="approveHint"
                @click="approve('reject')"
              >
                打回修改
              </AppButton>
              <AppButton
                variant="primary"
                block
                :disabled="!track"
                :title="approveHint"
                @click="approve('approve')"
              >
                通过并锁定
              </AppButton>
            </div>
          </section>
        </aside>
      </div>
    </main>

    <!-- ============ 配音详情弹窗（点击行内「详情」打开：属性查看 + 试听 + 重配） ============ -->
    <Modal
      :model-value="detailOpen"
      title="配音详情"
      :max-width="560"
      @update:model-value="detailOpen = $event"
      @close="closeDetail"
    >
      <template v-if="detailClip">
        <!-- 台词全文 -->
        <p class="rounded-md bg-surface-canvas px-3 py-2.5 text-sm leading-relaxed text-ink">
          {{ detailClip.text }}
        </p>

        <!-- 属性网格 -->
        <dl class="mt-4 grid grid-cols-2 gap-x-4 gap-y-2.5 text-sm">
          <div class="flex justify-between gap-3">
            <dt class="shrink-0 text-caption text-ink-mute">说话人</dt>
            <dd class="truncate text-ink">{{ detailClip.speaker_name || '旁白' }}</dd>
          </div>
          <!-- P1 修复：详情页展示实际情绪，便于逐句复核 -->
          <div class="flex justify-between gap-3">
            <dt class="shrink-0 text-caption text-ink-mute">情绪</dt>
            <dd class="truncate text-ink">{{ detailClip.emotion ? emotionLabel(detailClip.emotion) : '未设置' }}</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="shrink-0 text-caption text-ink-mute">状态</dt>
            <dd class="text-ink"><StatusTag :status="detailClip.status" size="sm" /></dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="shrink-0 text-caption text-ink-mute">音频时长</dt>
            <dd class="gd-num text-ink">{{ (detailClip.duration_ms / 1000).toFixed(2) }} s</dd>
          </div>
          <div class="flex justify-between gap-3">
            <dt class="shrink-0 text-caption text-ink-mute">台词 ID</dt>
            <dd class="gd-num text-ink">#{{ detailClip.line_id }}</dd>
          </div>
          <div class="col-span-2 flex justify-between gap-3">
            <dt class="shrink-0 text-caption text-ink-mute">词级对齐</dt>
            <dd>
              <span v-if="detailClip.align_degraded" class="text-caption text-danger-strong">占位音·非真实人声（沙箱合成，禁止用于成片）</span>
              <span v-else class="text-caption text-ink-mute">正常 · {{ detailClip.words.length }} 词</span>
            </dd>
          </div>
        </dl>

        <!-- 试听播放器 -->
        <div class="mt-4 rounded-lg border border-hairline bg-surface-canvas p-3">
          <p class="gd-section-label mb-2">试听</p>
          <audio v-if="detailClip.audio_url" :src="detailClip.audio_url" controls class="w-full" preload="none">
            您的浏览器不支持音频播放
          </audio>
          <p v-else class="text-caption text-ink-mute">该片段暂无音频文件（未生成或生成失败），可尝试重新配音。</p>
        </div>

        <!-- 词级时间戳（前 12 词预览） -->
        <details v-if="detailClip.words.length" class="mt-3">
          <summary class="cursor-pointer text-caption text-ink-mute hover:text-ink">
            词级时间戳（{{ detailClip.words.length }} 词）
          </summary>
          <ul class="mt-2 flex flex-wrap gap-1.5">
            <li
              v-for="(word, index) in detailClip.words.slice(0, 24)"
              :key="index"
              class="gd-num rounded border border-hairline bg-surface-inset px-1.5 py-0.5 text-caption text-ink-soft"
            >
              {{ word.w }} <span class="text-ink-mute">{{ word.start_ms }}–{{ word.end_ms }}ms</span>
            </li>
            <li v-if="detailClip.words.length > 24" class="self-center text-caption text-ink-mute">
              …其余 {{ detailClip.words.length - 24 }} 词省略
            </li>
          </ul>
        </details>
      </template>

      <template #footer>
        <AppButton variant="ghost" @click="closeDetail">关闭</AppButton>
        <AppButton
          v-if="detailClip"
          variant="secondary"
          @click="regenerate(detailClip.id, true)"
        >
          重新配音
        </AppButton>
      </template>
    </Modal>
  </div>
</template>

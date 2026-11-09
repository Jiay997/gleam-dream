<!--
  文件级注释：V4.0 后期域面板（F6-07 逐角色调参 / F6-08 字幕逐条编辑 /
  F6-09 BGM 替换 / F6-10 混音与响度）。

  承载的后端能力（`api/v4.ts`）：
  - `patchVoiceParams` / `previewVoice`：单角色语速 0.80~1.50、语调 ±3 档，
    重试听仅重合成该角色并展示受影响台词数（AC-F6-07-1~3，P0 细节功能 SF-F6-03）；
  - `listSubtitleLines` / `patchSubtitleLine` / `batchStyleSubtitles`：
    逐条改文本与出入点（音画误差 >100ms 拒绝保存）+ 批量样式（AC-F6-08-1~3）；
  - `replaceBgm`：手动换曲（授权范围与临期提示，AC-F6-09-1~2）；
  - `getMixProfile` / `updateMixProfile`：逐轨音量 ±6dB 与全剧响度 -14 LUFS（AC-F6-10-1~2）。

  props：`projectId`、`characters`（角色资产列表）。emit：`reload`。
-->
<script setup lang="ts">
/**
 * PostV4Panel 后期域 V4 能力面板。
 *
 * props：
 * - projectId：项目 ID；
 * - characters：角色资产列表（含 id / name / voice_id）。
 * emit：
 * - reload：请求父视图刷新时间线。
 */
import { onMounted, ref } from 'vue'

import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { useToast } from '@/composables/useToast'
import {
  batchStyleSubtitles,
  getMixProfile,
  listSubtitleLines,
  patchSubtitleLine,
  patchVoiceParams,
  previewVoice,
  replaceBgm,
  updateMixProfile,
} from '@/api/v4'
import type { MixProfileInfo, SubtitleLineItem, VoiceBindingInfo } from '@/api/v4'

/** 组件 props */
const props = withDefaults(
  defineProps<{
    /** 项目 ID */
    projectId: string | number
    /** 角色资产列表 */
    characters?: Array<Record<string, unknown>>
  }>(),
  { characters: () => [] },
)

/** 组件事件 */
const emit = defineEmits<{
  /** 请求刷新时间线 */
  reload: []
}>()

const toast = useToast()

/** 字幕行 */
const subtitles = ref<SubtitleLineItem[]>([])
/** 字幕单句上限 */
const maxChars = ref(20)
/** 混音画像 */
const mixProfile = ref<MixProfileInfo | null>(null)
/** 配音绑定 */
const voiceBinding = ref<VoiceBindingInfo | null>(null)
/** 选中的角色 ID */
const activeCharacterId = ref<number | null>(null)
/** 语速 */
const rate = ref(1.0)
/** 语调 */
const pitchShift = ref(0)
/** 字幕样式 ID（批量应用） */
const styleId = ref<number | null>(null)
/** BGM 曲目 ID（替换） */
const bgmTrackId = ref<number | null>(null)
/** 提交中 */
const submitting = ref(false)
/** 加载中 */
const loading = ref(false)
/** 字幕行编辑草稿（id → 文本） */
const subtitleDraft = ref<Record<number, string>>({})

/**
 * 加载字幕、混音与角色配音参数。
 * @returns 无
 */
async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [subtitlePayload, mixPayload] = await Promise.all([
      listSubtitleLines(props.projectId).catch(() => null),
      getMixProfile(props.projectId).catch(() => null),
    ])
    subtitles.value = subtitlePayload?.items ?? []
    maxChars.value = subtitlePayload?.max_chars ?? 20
    mixProfile.value = mixPayload?.profile ?? null
    if (props.characters.length > 0 && activeCharacterId.value === null) {
      activeCharacterId.value = Number(props.characters[0].id)
    }
  } finally {
    loading.value = false
  }
}

/**
 * 保存角色语速/语调。
 * @returns 无
 */
async function saveVoiceParams(): Promise<void> {
  if (activeCharacterId.value === null) {
    toast.warning('请先选择角色')
    return
  }
  submitting.value = true
  try {
    const result = await patchVoiceParams(activeCharacterId.value, {
      rate: rate.value,
      pitch_shift: pitchShift.value,
      emotion_override: [],
    })
    voiceBinding.value = result.binding
    toast.success(`参数已保存（受影响台词 ${result.affected_line_count} 句）`)
    if (result.qc_warning) {
      toast.warning('调慢语速导致台词超出镜头时长：可「调回语速」或「解锁分镜改时长」')
    }
  } finally {
    submitting.value = false
  }
}

/**
 * 重新试听该角色（仅该角色台词重合成）。
 * @returns 无
 */
async function retryPreview(): Promise<void> {
  if (activeCharacterId.value === null) return
  submitting.value = true
  try {
    const result = await previewVoice(activeCharacterId.value)
    voiceBinding.value = result.binding
    toast.success(`试听已更新，全剧应用将影响 ${result.affected_line_count} 句台词`)
  } finally {
    submitting.value = false
  }
}

/**
 * 保存单条字幕文本。
 * @param line 字幕行
 * @returns 无
 */
async function saveSubtitle(line: SubtitleLineItem): Promise<void> {
  const text = subtitleDraft.value[line.id]
  if (text === undefined || text === line.text) return
  submitting.value = true
  try {
    await patchSubtitleLine(line.id, { text })
    toast.success('字幕已更新（不改动音频）')
    delete subtitleDraft.value[line.id]
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 微调字幕出入点（±0.5s）。
 * @param line 字幕行
 * @param deltaMs 偏移毫秒
 * @returns 无
 */
async function nudgeSubtitle(line: SubtitleLineItem, deltaMs: number): Promise<void> {
  submitting.value = true
  try {
    await patchSubtitleLine(line.id, {
      start_ms: Math.max(0, line.start_ms + deltaMs),
      end_ms: Math.max(1, line.end_ms + deltaMs),
    })
    toast.success('出入点已微调')
    await loadAll()
    emit('reload')
  } catch {
    // 音画同步超差由拦截器统一提示（70001），此处静默
  } finally {
    submitting.value = false
  }
}

/**
 * 批量应用字幕样式。
 * @returns 无
 */
async function applyStyle(): Promise<void> {
  if (styleId.value === null) {
    toast.warning('请先选择字幕样式')
    return
  }
  submitting.value = true
  try {
    const result = await batchStyleSubtitles({ style_id: styleId.value, scope: 'all' })
    toast.success(`已应用样式至 ${result.applied} 条字幕`)
    if (result.out_of_safe_area.length > 0) {
      toast.warning(`有 ${result.out_of_safe_area.length} 条超出安全区，请检查`)
    }
    await loadAll()
  } finally {
    submitting.value = false
  }
}

/**
 * 手动替换 BGM。
 * @returns 无
 */
async function changeBgm(): Promise<void> {
  if (bgmTrackId.value === null) {
    toast.warning('请填写 BGM 曲目 ID')
    return
  }
  if (!mixProfile.value) {
    toast.warning('时间线尚未生成，无法替换 BGM')
    return
  }
  submitting.value = true
  try {
    const result = await replaceBgm(mixProfile.value.timeline_id, bgmTrackId.value)
    toast.success('BGM 已替换并重新卡点')
    if (result.license_warning) toast.warning(result.license_warning)
    if (result.distribution_blocked) toast.warning('该曲仅授权平台内使用，分发前会被拦截')
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

/**
 * 更新逐轨音量偏移。
 * @param track 轨道名
 * @param value 偏移 dB
 * @returns 无
 */
async function changeTrackVolume(track: string, value: number): Promise<void> {
  submitting.value = true
  try {
    const result = await updateMixProfile(props.projectId, { track_volumes: { [track]: value } })
    mixProfile.value = result.profile
    if (result.clipped_tracks.length > 0) toast.warning('音量超出 0dBFS，请回调')
    else toast.success('混音已更新（手动优先于自动混音）')
  } finally {
    submitting.value = false
  }
}

/**
 * 切换响度标准化。
 * @returns 无
 */
async function toggleNormalize(): Promise<void> {
  if (!mixProfile.value) return
  submitting.value = true
  try {
    const result = await updateMixProfile(props.projectId, {
      normalize_enabled: !mixProfile.value.normalize_enabled,
    })
    mixProfile.value = result.profile
    toast.success(result.profile.normalize_enabled ? '已开启响度标准化（-14 LUFS）' : '已关闭响度标准化')
  } finally {
    submitting.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
    <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-title text-neutral-900">配音与混音</h3>
      <AppButton variant="ghost" size="sm" :loading="loading" @click="loadAll">刷新</AppButton>
    </header>

    <!-- 逐角色调参（AC-F6-07，P0 SF-F6-03） -->
    <div class="rounded-md border border-hairline bg-surface-inset p-3">
      <h4 class="text-subtitle text-neutral-900">逐角色试听与调参</h4>
      <div class="mt-2 flex flex-wrap items-end gap-3">
        <label class="block">
          <span class="text-caption text-neutral-600">角色</span>
          <select
            v-model.number="activeCharacterId"
            class="mt-1 rounded-md border border-hairline bg-surface-canvas px-2 py-1.5 text-body"
          >
            <option v-for="char in characters" :key="String(char.id)" :value="Number(char.id)">
              {{ char.name }}
            </option>
          </select>
        </label>
        <label class="block">
          <span class="text-caption text-neutral-600">语速 {{ rate.toFixed(2) }}（0.80~1.50）</span>
          <input
            v-model.number="rate"
            type="range"
            min="0.80"
            max="1.50"
            step="0.05"
            class="mt-2 block w-40"
          />
        </label>
        <label class="block">
          <span class="text-caption text-neutral-600">语调 {{ pitchShift }}（±3 档）</span>
          <input v-model.number="pitchShift" type="range" min="-3" max="3" step="1" class="mt-2 block w-32" />
        </label>
        <AppButton variant="secondary" size="sm" :loading="submitting" @click="saveVoiceParams">保存参数</AppButton>
        <AppButton variant="ghost" size="sm" :loading="submitting" @click="retryPreview">重新试听该角色</AppButton>
      </div>
      <p v-if="voiceBinding" class="mt-2 text-caption text-neutral-600">
        受影响台词 {{ voiceBinding.affected_line_count }} 句；样音
        {{ voiceBinding.preview_confirmed ? '已确认' : '未确认' }}；历史 {{ voiceBinding.history_count }} 版。
      </p>
    </div>

    <!-- 字幕逐条编辑（AC-F6-08） -->
    <div class="mt-4">
      <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h4 class="text-subtitle text-neutral-900">字幕逐条编辑</h4>
        <div class="flex items-center gap-2">
          <input
            v-model.number="styleId"
            type="number"
            min="1"
            class="w-20 rounded-md border border-hairline bg-surface-canvas px-2 py-1 text-body"
            placeholder="样式 ID"
          />
          <AppButton variant="secondary" size="sm" :loading="submitting" @click="applyStyle">
            批量应用样式
          </AppButton>
        </div>
      </div>
      <EmptyState
        v-if="subtitles.length === 0"
        title="还没有字幕"
        description="生成字幕后可在此逐条改错字与微调出入点。"
        icon-path="M4 6h16M4 12h10M4 18h7"
      />
      <ul v-else class="max-h-64 space-y-1 overflow-auto pr-1">
        <li
          v-for="line in subtitles"
          :key="line.id"
          class="flex flex-wrap items-center gap-2 rounded border border-hairline px-2 py-1.5"
        >
          <span class="w-10 text-caption tabular-nums text-neutral-500">#{{ line.line_index }}</span>
          <span class="w-24 text-caption tabular-nums text-neutral-500">
            {{ (line.start_ms / 1000).toFixed(1) }}s~{{ (line.end_ms / 1000).toFixed(1) }}s
          </span>
          <input
            v-model="subtitleDraft[line.id]"
            class="min-w-[10rem] flex-1 rounded border border-hairline bg-surface-canvas px-2 py-1 text-body"
            :placeholder="line.text"
            :maxlength="maxChars * 4"
          />
          <span v-if="line.edited" class="text-caption text-success-strong">已编辑</span>
          <span v-if="line.out_of_safe_area" class="text-caption text-danger-strong">超安全区</span>
          <AppButton variant="text" size="sm" :loading="submitting" @click="nudgeSubtitle(line, -500)">-0.5s</AppButton>
          <AppButton variant="text" size="sm" :loading="submitting" @click="nudgeSubtitle(line, 500)">+0.5s</AppButton>
          <AppButton variant="text" size="sm" :loading="submitting" @click="saveSubtitle(line)">保存</AppButton>
        </li>
      </ul>
      <p class="mt-1 text-caption text-neutral-500">
        单句字幕建议 ≤{{ maxChars }} 字；出入点调整导致音画误差 &gt;100ms 将被拒绝保存。
      </p>
    </div>

    <!-- BGM 与混音（AC-F6-09 / F6-10） -->
    <div class="mt-4 grid gap-3 sm:grid-cols-2">
      <div class="rounded-md border border-hairline bg-surface-inset p-3">
        <h4 class="text-subtitle text-neutral-900">手动替换 BGM</h4>
        <div class="mt-2 flex items-center gap-2">
          <input
            v-model.number="bgmTrackId"
            type="number"
            min="1"
            class="w-24 rounded-md border border-hairline bg-surface-canvas px-2 py-1.5 text-body"
            placeholder="曲目 ID"
          />
          <AppButton variant="secondary" size="sm" :loading="submitting" @click="changeBgm">替换并卡点</AppButton>
        </div>
        <p class="mt-1 text-caption text-neutral-500">试听免费；替换后自动重新卡点与侧链闪避。</p>
      </div>

      <div class="rounded-md border border-hairline bg-surface-inset p-3">
        <div class="flex items-center justify-between">
          <h4 class="text-subtitle text-neutral-900">混音与响度</h4>
          <AppButton variant="text" size="sm" :loading="submitting" @click="toggleNormalize">
            {{ mixProfile?.normalize_enabled ? '关闭标准化' : '开启标准化' }}
          </AppButton>
        </div>
        <p v-if="mixProfile" class="mt-1 text-caption text-neutral-600">
          目标 {{ mixProfile.loudness_target }} LUFS · 真峰值 ≤ {{ mixProfile.true_peak_db }} dBTP
          <span v-if="mixProfile.clipped" class="text-danger-strong">· 超 0dBFS</span>
        </p>
        <div class="mt-2 space-y-2">
          <label v-for="track in ['dialogue', 'bgm', 'sfx']" :key="track" class="flex items-center gap-2">
            <span class="w-16 text-caption text-neutral-600">{{ track }}</span>
            <input
              type="range"
              min="-6"
              max="6"
              step="0.5"
              :value="mixProfile?.track_volumes?.[track] ?? 0"
              class="flex-1"
              @change="
                changeTrackVolume(track, Number(($event.target as HTMLInputElement).value))
              "
            />
          </label>
        </div>
      </div>
    </div>
  </section>
</template>

<!--
  文件级注释：P10+ · 音色详情与试听台（路由 /projects/:projectId/assets/voices/:voiceId）。

  设计目标（对齐「极夜 · 极光」主题，不引入新色值 / 字体 / 依赖）：
  - 信息层级：顶部返回条（音色名 + 唯一 ID + 操作）→ 摘要条（引擎 / 绑定角色 / 样音 / 状态）
    → 左「试听台」（播放器 + 试听文本 + 语速 / 音调 + 重新生成样音）→ 右「音色档案」
    （唯一 ID 复制 / 属性 / 风格标签）+「绑定角色」+「配音引用」；
  - 试听反馈：波形逐条点亮 + 播放 / 暂停 / 进度拖动 + 合成中态 + 沙盒占位提示；
  - 唯一 ID：页面多处展示 #ID 与 VO_xxx 短编号，支持一键复制 voice_id 引用片段，
    供后续配音模块按 ID 引用；
  - 重新生成：带二次确认，成功后替换音色库样音并立即试听，刷新后仍可播放；
  - 响应式：单列（手机）→ 12 栅格双列（≥lg），头部操作自动换行。
-->
<script setup lang="ts">
/**
 * VoiceDetailView 音色详情与试听台。
 * props：无。emits：无。
 * 依赖 assets API（详情 / 试听 / 重新生成样音）、useVoicePreview、useToast。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  getTtsVoice,
  previewTtsVoice,
  regenerateTtsVoiceSample,
  VOICE_GENDER_LABELS,
  voiceIdTag,
  voiceStyleLabel,
  voiceStyleTagList,
  type TtsVoiceDetail,
  type VoicePreviewResult,
} from '@/api/assets'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { formatAudioTime, useVoicePreview } from '@/composables/useVoicePreview'
import { useToast } from '@/composables/useToast'
import { firstChar, formatRelativeTime, formatShortDate } from '@/utils/datetime'

/** 路由实例 */
const route = useRoute()
const router = useRouter()
/** Toast 提示 */
const toast = useToast()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))
/** 音色 ID（路由参数） */
const voiceId = computed(() => String(route.params.voiceId ?? ''))
/** 音色 ID 数值（播放器按 ID 标记当前音色） */
const voiceIdNumber = computed(() => Number(voiceId.value))

/** 音色详情 */
const detail = ref<TtsVoiceDetail | null>(null)
/** 详情加载中 */
const loading = ref(true)
/** 详情加载失败信息 */
const loadError = ref('')
/** 首次加载完成标记（避免刷新时覆盖用户已编辑的试听文本） */
let initialized = false

/** 试听文本 */
const previewText = ref('')
/** 语速倍率 */
const rate = ref(1)
/** 音调偏移（半音） */
const pitch = ref(0)
/** 试听合成中（按钮级 loading） */
const previewLoading = ref(false)
/** 重新生成样音中 */
const regenerating = ref(false)
/** 重新生成确认弹窗 */
const regenDialogOpen = ref(false)
/** 最近一次实时合成的试听结果（未持久化） */
const lastPreview = ref<VoicePreviewResult | null>(null)

/** 共享音频播放器（列表与详情同源逻辑） */
const { playingId, loadingId, positionMs, durationMs, progress, error: playbackError, toggle, playUrl, seek, stop } = useVoicePreview()

/** 波形柱数量（试听台上的进度可视化） */
const WAVE_BAR_COUNT = 48
/** 波形柱高度（百分比；确定性排布，避免每次渲染抖动） */
const waveBars = Array.from({ length: WAVE_BAR_COUNT }, (_, index) => 22 + ((index * 37) % 66))

/** 是否可用（下架音色禁止试听 / 重新生成） */
const isAvailable = computed(() => detail.value?.status === 'available')

/** 试听文本上限 */
const maxTextLength = computed(() => detail.value?.max_preview_text_length ?? 80)

/** 实际参与合成的文本（空则回退默认示例句） */
const previewTextNormalized = computed(() => previewText.value.trim() || detail.value?.sample_text || '')

/** 试听文本字数 */
const previewTextLength = computed(() => previewText.value.length)

/** 风格标签数组 */
const styleTags = computed(() => voiceStyleTagList(detail.value?.style_tags))

/** 音色短编号（VO_003） */
const voiceCode = computed(() => (detail.value ? voiceIdTag(detail.value.id) : 'VO_—'))

/** 当前可播放地址：优先最近一次试听结果，其次音色库已存样音 */
const currentAudioUrl = computed<string>(() => lastPreview.value?.audio_url ?? detail.value?.sample_audio_url ?? '')

/** 当前音频时长（毫秒） */
const currentAudioDuration = computed(
  () => lastPreview.value?.duration_ms ?? detail.value?.sample_duration_ms ?? 0,
)

/** 是否正在播放当前音色 */
const isPlaying = computed(() => playingId.value === voiceIdNumber.value)
/** 是否正在合成当前音色 */
const isSynthesizing = computed(() => loadingId.value === voiceIdNumber.value || previewLoading.value)

/** 播放器状态文案（带脉冲点的实时态） */
const playerState = computed<{ label: string; live: boolean }>(() => {
  if (isSynthesizing.value) return { label: '合成中', live: true }
  if (isPlaying.value) return { label: '试听中', live: true }
  if (positionMs.value > 0) return { label: '已暂停', live: false }
  if (currentAudioUrl.value) return { label: '待播放', live: false }
  return { label: '待生成', live: false }
})

/** 语速展示文案 */
const rateLabel = computed(() => `${rate.value.toFixed(2)}×`)
/** 音调展示文案（带正负号） */
const pitchLabel = computed(() => `${pitch.value > 0 ? '+' : ''}${pitch.value} 半音`)

/** 当前试听参数是否已偏离已生成音频（提示重新生成） */
const previewStale = computed(() => {
  if (!currentAudioUrl.value) return false
  const base = lastPreview.value
  if (base) {
    return (
      base.text !== previewTextNormalized.value ||
      Math.abs(base.rate - rate.value) > 0.001 ||
      base.pitch !== pitch.value
    )
  }
  // 只有音色库样音时：样音以默认示例句 / 1.0× / 0 半音合成
  return (
    previewTextNormalized.value !== (detail.value?.sample_text ?? '') ||
    Math.abs(rate.value - 1) > 0.001 ||
    pitch.value !== 0
  )
})

/** 音色档案属性行（按真实字段生成，无数据不造假） */
const infoRows = computed<Array<{ label: string; value: string; mono?: boolean }>>(() => {
  const current = detail.value
  if (!current) return []
  const rows: Array<{ label: string; value: string; mono?: boolean }> = [
    { label: '音色键', value: current.voice_key, mono: true },
    { label: '性别', value: VOICE_GENDER_LABELS[current.gender] ?? current.gender },
    { label: '语言', value: current.language, mono: true },
    { label: '引擎音色', value: current.engine_voice, mono: true },
    { label: '提供方', value: current.provider || '—', mono: true },
    { label: '样音时长', value: current.sample_duration_ms ? formatAudioTime(current.sample_duration_ms) : '未生成', mono: true },
    { label: '更新时间', value: current.updated_at ? formatRelativeTime(current.updated_at) : '—' },
  ]
  if (current.license_expires_at) {
    rows.push({ label: '授权到期', value: formatShortDate(current.license_expires_at), mono: true })
  }
  if (current.offline_at) {
    rows.push({ label: '下架时间', value: formatShortDate(current.offline_at), mono: true })
  }
  return rows
})

/** 配音模块引用片段（voice_id 为稳定主键） */
const referenceSnippet = computed(() => {
  const current = detail.value
  if (!current) return ''
  return [`voice_id: ${current.id}`, `voice_key: "${current.voice_key}"`, `engine_voice: "${current.engine_voice}"`].join(
    '\n',
  )
})

/** 重新生成确认文案 */
const regenDialogContent = computed(() => {
  const current = detail.value
  const engine = current?.engine_voice ?? '当前引擎音色'
  return `将以 ${engine} 重新合成并替换音色库样音。试听文本「${previewTextNormalized.value}」，语速 ${rateLabel.value}，音调 ${pitchLabel.value}。已绑定角色的配音引用不受影响。`
})

/**
 * 拉取音色详情（首次加载写入默认试听文本，刷新不覆盖用户编辑）。
 * @returns 无
 */
async function fetchDetail(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const data = await getTtsVoice(voiceId.value)
    detail.value = data
    if (!initialized) {
      previewText.value = data.sample_text
      initialized = true
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '音色详情加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 实时合成试听音频。
 * @param options.autoplay 合成成功后是否立即播放
 * @param options.notify 是否弹出结果提示（列表式静默试听传 false）
 * @returns 试听结果；不可用 / 参数非法 / 合成失败时返回 null
 */
async function generatePreview(options: { autoplay?: boolean; notify?: boolean } = {}): Promise<VoicePreviewResult | null> {
  const current = detail.value
  if (!current) return null
  if (current.status !== 'available') {
    toast.warning('该音色已下架，无法试听')
    return null
  }
  const text = previewTextNormalized.value
  if (!text) {
    toast.warning('请先输入试听文本')
    return null
  }
  if (text.length > maxTextLength.value) {
    toast.warning(`试听文本不能超过 ${maxTextLength.value} 字`)
    return null
  }

  previewLoading.value = true
  try {
    const result = await previewTtsVoice(current.id, { text, rate: rate.value, pitch: pitch.value })
    lastPreview.value = result
    if (options.notify !== false) {
      if (result.sandbox) {
        toast.warning('当前为沙盒模式，试听音频为占位音')
      } else {
        toast.success('试听音频已生成')
      }
    }
    if (options.autoplay) {
      await playUrl(current.id, result.audio_url, result.duration_ms)
    }
    return result
  } catch {
    // 拦截器已 toast（模型不可用 / 参数非法等）
    return null
  } finally {
    previewLoading.value = false
  }
}

/**
 * 播放 / 暂停：优先播放已有样音，无样音时先实时合成再播放。
 * @returns 无
 */
function handlePlayToggle(): void {
  const current = detail.value
  if (!current) return
  if (current.status !== 'available') {
    toast.warning('该音色已下架，无法试听')
    return
  }
  if (currentAudioUrl.value) {
    void toggle(current.id, currentAudioUrl.value, currentAudioDuration.value)
    return
  }
  void toggle(current.id, async () => {
    const result = await generatePreview({ notify: false })
    if (!result) throw new Error('preview failed')
    return result.audio_url
  })
}

/**
 * 点击「生成试听」：按当前文本 / 语速 / 音调合成并自动播放。
 * @returns 无
 */
function handleGeneratePreview(): void {
  void generatePreview({ autoplay: true })
}

/**
 * 停止播放并复位进度。
 * @returns 无
 */
function handleStop(): void {
  stop()
}

/**
 * 拖动进度条跳转播放位置。
 * @param event 原生 input 事件
 * @returns 无
 */
function handleSeek(event: Event): void {
  const target = event.target as HTMLInputElement
  seek(target.valueAsNumber)
}

/**
 * 恢复默认示例句。
 * @returns 无
 */
function resetPreviewText(): void {
  previewText.value = detail.value?.sample_text ?? ''
}

/**
 * 打开「重新生成样音」确认弹窗。
 * @returns 无
 */
function openRegenDialog(): void {
  if (!detail.value) return
  if (detail.value.status !== 'available') {
    toast.warning('该音色已下架，无法重新生成样音')
    return
  }
  regenDialogOpen.value = true
}

/**
 * 提交重新生成样音（持久化到音色库并立即试听）。
 * @returns 无
 */
async function confirmRegenerate(): Promise<void> {
  const current = detail.value
  if (!current) return
  regenerating.value = true
  try {
    const result = await regenerateTtsVoiceSample(current.id, {
      text: previewTextNormalized.value,
      rate: rate.value,
      pitch: pitch.value,
    })
    lastPreview.value = result
    detail.value = {
      ...current,
      sample_audio_url: result.audio_url,
      sample_duration_ms: result.duration_ms,
      updated_at: result.generated_at,
    }
    regenDialogOpen.value = false
    toast.success('音色样音已重新生成，已替换音色库样音')
    if (result.sandbox) {
      toast.warning('当前为沙盒模式，样音为占位音频')
    }
    await playUrl(current.id, result.audio_url, result.duration_ms)
  } catch {
    // 拦截器已 toast
  } finally {
    regenerating.value = false
  }
}

/**
 * 复制文本到剪贴板（优先 Clipboard API，HTTP 环境下回退到隐藏 textarea）。
 * @param text 待复制文本
 * @returns 是否复制成功
 */
async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // 继续走回退方案
  }
  try {
    const textarea = document.createElement('textarea')
    textarea.value = text
    textarea.style.position = 'fixed'
    textarea.style.opacity = '0'
    document.body.appendChild(textarea)
    textarea.select()
    const ok = document.execCommand('copy')
    textarea.remove()
    return ok
  } catch {
    return false
  }
}

/**
 * 复制唯一音色 ID（配音模块引用主键）。
 * @returns 无
 */
async function copyVoiceId(): Promise<void> {
  const current = detail.value
  if (!current) return
  const ok = await copyToClipboard(`voice_id: ${current.id}`)
  if (ok) toast.success(`已复制 voice_id: ${current.id}`)
  else toast.warning('复制失败，请手动选择文本复制')
}

/**
 * 复制完整配音引用片段（voice_id / voice_key / engine_voice）。
 * @returns 无
 */
async function copyReference(): Promise<void> {
  if (!referenceSnippet.value) return
  const ok = await copyToClipboard(referenceSnippet.value)
  if (ok) toast.success('引用片段已复制')
  else toast.warning('复制失败，请手动选择文本复制')
}

/**
 * 返回音色列表（保留音色 Tab）。
 * @returns 无
 */
function goBack(): void {
  void router.push(`/projects/${projectId.value}/assets?tab=voice`)
}

/**
 * 跳转绑定角色详情。
 * @param characterId 角色资产 ID
 * @returns 无
 */
function goCharacter(characterId: number): void {
  void router.push(`/projects/${projectId.value}/assets/characters/${characterId}`)
}

/**
 * 判断波形柱是否已被播放进度点亮。
 * @param index 柱索引
 * @returns 是否点亮
 */
function isBarActive(index: number): boolean {
  if (progress.value <= 0) return false
  return (index + 1) / WAVE_BAR_COUNT <= progress.value
}

/** 路由音色 ID 变化时重新加载（同一页面内切换音色） */
watch(voiceId, () => {
  initialized = false
  lastPreview.value = null
  stop()
  void fetchDetail()
})

/** 挂载即加载详情 */
onMounted(() => {
  void fetchDetail()
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <!-- ============ 顶部返回条（返回 + 音色名 + 状态 + 唯一 ID + 操作） ============ -->
    <header class="shrink-0 border-b border-hairline bg-surface-raised px-4 py-3 sm:px-5">
      <div class="flex flex-wrap items-center gap-3">
        <button
          type="button"
          class="rounded-full p-1.5 text-neutral-400 transition-colors hover:bg-surface-sunken hover:text-neutral-700"
          aria-label="返回音色列表"
          @click="goBack"
        >
          <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />

        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <h1 class="gd-masthead truncate text-display">{{ detail?.name ?? '音色详情' }}</h1>
            <StatusTag
              v-if="detail"
              :status="detail.status === 'available' ? 'approved' : 'deprecated'"
              :label="detail.status === 'available' ? '可用' : '已下架'"
              size="sm"
            />
          </div>
          <p class="gd-num mt-0.5 truncate text-caption text-neutral-400">
            VOICE {{ voiceCode }}
            <template v-if="detail"> · {{ detail.voice_key }} · ID #{{ detail.id }}</template>
          </p>
        </div>

        <div class="ml-auto flex flex-wrap items-center gap-2">
          <AppButton variant="ghost" size="sm" :disabled="!detail" @click="copyVoiceId">复制 ID</AppButton>
          <AppButton variant="ghost" size="sm" :loading="loading" @click="fetchDetail">刷新</AppButton>
          <AppButton
            variant="secondary"
            size="sm"
            :disabled="!detail || !isAvailable"
            :loading="regenerating"
            @click="openRegenDialog"
          >
            ↻ 重新生成样音
          </AppButton>
        </div>
      </div>
    </header>

    <!-- ============ 加载态 ============ -->
    <div v-if="loading" class="min-h-0 flex-1 overflow-y-auto">
      <div class="mx-auto grid w-full max-w-[1200px] grid-cols-1 gap-6 px-5 py-6 lg:grid-cols-12 lg:px-8">
        <div class="space-y-6 lg:col-span-7">
          <LoadingSkeleton variant="card" />
          <LoadingSkeleton variant="text" :rows="6" />
        </div>
        <div class="space-y-6 lg:col-span-5">
          <LoadingSkeleton variant="card" />
          <LoadingSkeleton variant="card" />
        </div>
      </div>
    </div>

    <!-- ============ 错误态 ============ -->
    <div v-else-if="loadError" class="min-h-0 flex-1 overflow-y-auto p-6 lg:p-8">
      <ErrorRetryBlock message="音色详情加载失败" :description="loadError" @retry="fetchDetail" />
    </div>

    <!-- ============ 数据区 ============ -->
    <div v-else-if="detail" class="min-h-0 flex-1 overflow-y-auto">
      <div class="mx-auto w-full max-w-[1200px] px-5 py-6 lg:px-8">
        <!-- 面包屑 + 导语（信息层级：资产设计 → 音色库 → 当前音色） -->
        <nav class="flex flex-wrap items-center gap-1.5 text-caption text-neutral-400" aria-label="面包屑">
          <button type="button" class="transition-colors hover:text-neutral-700" @click="goBack">资产设计</button>
          <span aria-hidden="true">/</span>
          <button type="button" class="transition-colors hover:text-neutral-700" @click="goBack">音色库</button>
          <span aria-hidden="true">/</span>
          <span class="text-neutral-600">{{ detail.name }}</span>
        </nav>
        <p class="gd-lede mt-2 max-w-3xl">
          {{ detail.name }} · {{ voiceStyleTagList(detail.style_tags).map(voiceStyleLabel).join(' / ') || '系统声线' }}，
          唯一 ID <span class="gd-num text-primary-300">#{{ detail.id }}</span>。在此试听样音、调整语速音调，
          或重新生成音色库样音；配音模块可直接按 voice_id 引用。
        </p>

        <!-- 摘要条：引擎音色 / 绑定角色 / 样音时长 / 状态 -->
        <div class="mt-4 flex flex-wrap items-center gap-2.5">
          <span class="gd-badge gd-badge--violet">引擎 · {{ detail.engine_voice }}</span>
          <span class="gd-badge gd-badge--cyan">{{ detail.language }} · {{ VOICE_GENDER_LABELS[detail.gender] ?? detail.gender }}</span>
          <span class="gd-badge">
            <span class="gd-badge__dot" aria-hidden="true" />
            绑定 {{ detail.bound_character_count }} 个角色
          </span>
          <span class="gd-badge gd-badge--mint">
            样音 {{ detail.sample_duration_ms ? formatAudioTime(detail.sample_duration_ms) : '待生成' }}
          </span>
        </div>

        <div class="mt-6 grid grid-cols-1 gap-6 lg:grid-cols-12">
          <!-- ==================== 左：试听台 + 样音存档 ==================== -->
          <div class="min-w-0 space-y-6 lg:col-span-7">
            <section class="gd-card overflow-hidden p-0" aria-label="音色试听台">
              <!-- 卡头 -->
              <div class="flex flex-wrap items-center justify-between gap-3 border-b border-hairline px-4 py-3.5 sm:px-5">
                <div class="min-w-0">
                  <span class="gd-section-label">试听台 · PREVIEW CONSOLE</span>
                  <h2 class="mt-0.5 text-title text-neutral-900">听见这个声音</h2>
                </div>
                <div class="flex shrink-0 items-center gap-2 text-caption">
                  <span
                    class="h-2 w-2 rounded-full"
                    :class="playerState.live ? 'animate-pulse bg-info' : 'bg-neutral-400'"
                    aria-hidden="true"
                  />
                  <span class="text-neutral-500">{{ playerState.label }}</span>
                  <span v-if="lastPreview?.sandbox" class="gd-badge gd-badge--amber">沙盒占位</span>
                </div>
              </div>

              <!-- 播放器（夜窗深底 + 波形进度） -->
              <div class="p-4 sm:p-5">
                <div class="gd-night-window relative overflow-hidden p-4 sm:p-5">
                  <div
                    class="pointer-events-none absolute -right-16 -top-20 h-56 w-56 rounded-full bg-primary-500/20 blur-3xl"
                    aria-hidden="true"
                  />
                  <div class="relative flex items-center gap-4">
                    <!-- 播放 / 暂停 / 合成中 -->
                    <button
                      type="button"
                      class="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-grad-primary text-white shadow-gd-bloom transition-transform duration-gd-fast ease-gd-standard hover:scale-[1.04] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-300 disabled:cursor-not-allowed disabled:opacity-60"
                      :disabled="!isAvailable || isSynthesizing"
                      :aria-label="isPlaying ? '暂停试听' : '播放试听'"
                      @click="handlePlayToggle"
                    >
                      <svg v-if="isSynthesizing" class="h-6 w-6 animate-spin" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                        <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-opacity="0.25" stroke-width="3" />
                        <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="3" stroke-linecap="round" />
                      </svg>
                      <svg v-else-if="isPlaying" class="h-6 w-6" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                        <rect x="7" y="5" width="4" height="14" rx="1.2" />
                        <rect x="13" y="5" width="4" height="14" rx="1.2" />
                      </svg>
                      <svg v-else class="ml-0.5 h-6 w-6" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                        <path d="M8 5.14v13.72c0 .8.87 1.3 1.56.9l11-6.86a1.05 1.05 0 000-1.8l-11-6.86A1.05 1.05 0 008 5.14z" />
                      </svg>
                    </button>

                    <!-- 波形（播放时逐条点亮并轻微起伏） -->
                    <div class="flex h-24 min-w-0 flex-1 items-end gap-[3px]" aria-hidden="true">
                      <span
                        v-for="(height, index) in waveBars"
                        :key="index"
                        class="voice-wave__bar min-w-0 flex-1 rounded-full transition-colors duration-gd-fast"
                        :class="[
                          isBarActive(index) ? 'bg-primary-400' : 'bg-white/15',
                          isPlaying ? 'is-live' : '',
                        ]"
                        :style="{ height: `${height}%`, animationDelay: `-${(index % 9) * 0.11}s` }"
                      />
                    </div>
                  </div>

                  <!-- 进度条 + 时间码 -->
                  <div class="relative mt-4 flex items-center gap-3">
                    <span class="gd-num w-10 shrink-0 text-right text-caption text-player-hint">
                      {{ formatAudioTime(positionMs) }}
                    </span>
                    <input
                      type="range"
                      class="gd-audio-range w-full min-w-0"
                      min="0"
                      :max="Math.max(durationMs, currentAudioDuration, 1)"
                      step="100"
                      :value="positionMs"
                      :disabled="!currentAudioUrl"
                      :style="{
                        background: `linear-gradient(to right, var(--gd-primary-500) ${progress * 100}%, var(--gd-progress-track) ${progress * 100}%)`,
                      }"
                      aria-label="试听播放进度"
                      @input="handleSeek"
                    />
                    <span class="gd-num w-10 shrink-0 text-caption text-player-hint">
                      {{ formatAudioTime(durationMs || currentAudioDuration) }}
                    </span>
                  </div>

                  <p v-if="playbackError" class="relative mt-2 text-caption text-danger-bright" role="alert">
                    {{ playbackError }}
                  </p>
                  <p v-else class="relative mt-2 truncate text-caption text-player-hint">
                    {{ isPlaying ? '正在试听 · 波形随进度点亮' : currentAudioUrl ? '点击播放键试听样音' : '尚无样音，生成试听后即可播放' }}
                  </p>
                </div>

                <!-- 试听参数：文本 / 语速 / 音调 -->
                <div class="mt-5 space-y-4">
                  <div>
                    <div class="flex items-center justify-between gap-3">
                      <label for="voice-preview-text" class="text-caption font-medium text-neutral-600">试听文本</label>
                      <span class="gd-num text-caption" :class="previewTextLength > maxTextLength ? 'text-danger' : 'text-neutral-400'">
                        {{ previewTextLength }}/{{ maxTextLength }}
                      </span>
                    </div>
                    <textarea
                      id="voice-preview-text"
                      v-model="previewText"
                      rows="2"
                      :maxlength="maxTextLength"
                      class="gd-textarea mt-1.5 w-full resize-none"
                      :class="previewTextLength > maxTextLength ? 'gd-field-invalid' : ''"
                      :placeholder="detail.sample_text"
                    />
                    <div class="mt-1.5 flex flex-wrap items-center gap-2 text-caption text-neutral-400">
                      <button type="button" class="transition-colors hover:text-primary-300" @click="resetPreviewText">
                        恢复默认示例句
                      </button>
                      <span aria-hidden="true">·</span>
                      <span>留空时使用该音色的默认示例句</span>
                    </div>
                  </div>

                  <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <div>
                      <div class="flex items-center justify-between gap-2">
                        <label for="voice-rate" class="text-caption font-medium text-neutral-600">语速</label>
                        <span class="gd-num text-caption text-primary-300">{{ rateLabel }}</span>
                      </div>
                      <input
                        id="voice-rate"
                        v-model.number="rate"
                        type="range"
                        class="gd-audio-range mt-3 w-full"
                        min="0.5"
                        max="2"
                        step="0.05"
                        :style="{ background: `linear-gradient(to right, var(--gd-primary-500) ${((rate - 0.5) / 1.5) * 100}%, var(--gd-progress-track) ${((rate - 0.5) / 1.5) * 100}%)` }"
                        aria-label="语速倍率"
                      />
                      <div class="mt-1 flex justify-between text-micro text-neutral-400">
                        <span>0.5×</span><span>1.0×</span><span>2.0×</span>
                      </div>
                    </div>
                    <div>
                      <div class="flex items-center justify-between gap-2">
                        <label for="voice-pitch" class="text-caption font-medium text-neutral-600">音调</label>
                        <span class="gd-num text-caption text-primary-300">{{ pitchLabel }}</span>
                      </div>
                      <input
                        id="voice-pitch"
                        v-model.number="pitch"
                        type="range"
                        class="gd-audio-range mt-3 w-full"
                        min="-12"
                        max="12"
                        step="1"
                        :style="{ background: `linear-gradient(to right, var(--gd-primary-500) ${((pitch + 12) / 24) * 100}%, var(--gd-progress-track) ${((pitch + 12) / 24) * 100}%)` }"
                        aria-label="音调偏移（半音）"
                      />
                      <div class="mt-1 flex justify-between text-micro text-neutral-400">
                        <span>-12</span><span>0</span><span>+12</span>
                      </div>
                    </div>
                  </div>

                  <p v-if="previewStale" class="rounded-md bg-warning-bg px-2.5 py-1.5 text-caption text-warning-bright">
                    参数已调整：当前播放的是旧样音，点击「生成试听」或「重新生成样音」后生效。
                  </p>
                </div>
              </div>

              <!-- 操作条 -->
              <div class="flex flex-wrap items-center gap-2 border-t border-hairline bg-surface-canvas px-4 py-3.5 sm:px-5">
                <AppButton
                  variant="primary"
                  size="sm"
                  :loading="previewLoading"
                  :disabled="!isAvailable"
                  @click="handleGeneratePreview"
                >
                  生成试听
                </AppButton>
                <AppButton variant="ghost" size="sm" :disabled="!isAvailable" @click="openRegenDialog">
                  重新生成样音
                </AppButton>
                <AppButton v-if="isPlaying || positionMs > 0" variant="ghost" size="sm" @click="handleStop">
                  停止播放
                </AppButton>
                <span class="ml-auto text-caption text-neutral-400">实时合成 · 不写入配音时间线</span>
              </div>
            </section>

            <!-- 样音存档（重新生成的持久化结果） -->
            <section class="gd-card p-4 sm:p-5" aria-label="样音存档">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="min-w-0">
                  <span class="gd-section-label">样音存档 · SAMPLE ARCHIVE</span>
                  <p class="mt-1 max-w-xl text-sm leading-relaxed text-neutral-600">
                    重新生成会把新样音登记到媒体库并替换音色库样音，刷新后仍可播放；已绑定角色的配音引用不变。
                  </p>
                </div>
                <AppButton
                  variant="secondary"
                  size="sm"
                  :disabled="!isAvailable"
                  :loading="regenerating"
                  @click="openRegenDialog"
                >
                  ↻ 重新生成
                </AppButton>
              </div>
              <dl class="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 text-sm sm:grid-cols-4">
                <div>
                  <dt class="text-caption text-neutral-400">样音状态</dt>
                  <dd class="mt-0.5 text-neutral-700">{{ detail.sample_audio_url ? '已存档' : '待生成' }}</dd>
                </div>
                <div>
                  <dt class="text-caption text-neutral-400">样音时长</dt>
                  <dd class="gd-num mt-0.5 text-neutral-700">
                    {{ detail.sample_duration_ms ? formatAudioTime(detail.sample_duration_ms) : '—' }}
                  </dd>
                </div>
                <div>
                  <dt class="text-caption text-neutral-400">引擎音色</dt>
                  <dd class="gd-num mt-0.5 text-neutral-700">{{ detail.engine_voice }}</dd>
                </div>
                <div>
                  <dt class="text-caption text-neutral-400">更新时间</dt>
                  <dd class="gd-num mt-0.5 text-neutral-700">{{ detail.updated_at ? formatRelativeTime(detail.updated_at) : '—' }}</dd>
                </div>
              </dl>
            </section>
          </div>

          <!-- ==================== 右：音色档案 + 绑定角色 + 配音引用 ==================== -->
          <div class="min-w-0 space-y-6 lg:col-span-5">
            <!-- 音色档案（唯一 ID 为视觉主线） -->
            <section class="gd-card p-4 sm:p-5" aria-label="音色档案">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <span class="gd-section-label">音色档案 · VOICE PROFILE</span>
                  <h2 class="mt-0.5 truncate font-serif text-display text-neutral-900">{{ detail.name }}</h2>
                </div>
                <StatusTag
                  :status="detail.status === 'available' ? 'approved' : 'deprecated'"
                  :label="detail.status === 'available' ? '可用' : '已下架'"
                  size="sm"
                />
              </div>

              <!-- 唯一 ID 面板 -->
              <div class="mt-4 rounded-lg border border-primary-200 bg-primary-50 px-4 py-3">
                <div class="flex items-end justify-between gap-3">
                  <div class="min-w-0">
                    <span class="gd-section-label text-primary-300">唯一音色 ID</span>
                    <div class="mt-0.5 flex items-baseline gap-2">
                      <span class="gd-num text-3xl text-primary-900">#{{ detail.id }}</span>
                      <span class="gd-num truncate text-caption text-primary-300">{{ voiceCode }}</span>
                    </div>
                  </div>
                  <AppButton variant="ghost" size="sm" @click="copyVoiceId">复制 voice_id</AppButton>
                </div>
                <p class="mt-1 text-caption leading-relaxed text-neutral-400">
                  配音模块按 voice_id 引用；名称或引擎映射变更不影响该 ID。
                </p>
              </div>

              <!-- 属性表 -->
              <dl class="mt-4 grid grid-cols-1 gap-x-5 sm:grid-cols-2">
                <div
                  v-for="row in infoRows"
                  :key="row.label"
                  class="flex items-center justify-between gap-3 border-b border-hairline-soft py-2"
                >
                  <dt class="shrink-0 text-caption text-neutral-400">{{ row.label }}</dt>
                  <dd class="truncate text-sm text-neutral-700" :class="row.mono ? 'gd-num' : ''">{{ row.value }}</dd>
                </div>
              </dl>

              <!-- 风格标签 -->
              <div class="mt-4">
                <span class="gd-section-label">风格标签 · STYLE</span>
                <div v-if="styleTags.length > 0" class="mt-2 flex flex-wrap gap-1.5">
                  <span v-for="tag in styleTags" :key="tag" class="gd-tag">
                    {{ voiceStyleLabel(tag) }}
                    <span class="gd-num ml-1 opacity-55">#{{ tag }}</span>
                  </span>
                </div>
                <p v-else class="mt-2 text-caption text-neutral-400">暂无风格标签</p>
              </div>
            </section>

            <!-- 绑定角色 -->
            <section class="gd-card p-4 sm:p-5" aria-label="绑定角色">
              <div class="flex items-center justify-between gap-3">
                <div>
                  <span class="gd-section-label">绑定角色 · CAST</span>
                  <h2 class="mt-0.5 text-title text-neutral-900">正在使用该音色</h2>
                </div>
                <span class="gd-num text-caption text-neutral-400">共 {{ detail.bound_character_count }} 个</span>
              </div>

              <ul v-if="detail.bound_characters.length > 0" class="mt-3 divide-y divide-hairline">
                <li v-for="character in detail.bound_characters" :key="character.id">
                  <button
                    type="button"
                    class="group flex w-full items-center gap-3 py-2.5 text-left transition-colors hover:bg-state-hover"
                    :aria-label="`查看角色 ${character.name}`"
                    @click="goCharacter(character.id)"
                  >
                    <span
                      class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary-100 font-serif text-sm font-semibold text-primary-900"
                      aria-hidden="true"
                    >
                      {{ firstChar(character.name) }}
                    </span>
                    <span class="min-w-0 flex-1">
                      <span class="block truncate text-sm font-medium text-neutral-800">{{ character.name }}</span>
                      <span class="gd-num block text-caption text-neutral-400">ASSET_ID: {{ character.id }}</span>
                    </span>
                    <svg
                      class="h-4 w-4 shrink-0 text-neutral-400 transition-transform group-hover:translate-x-0.5"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.8"
                      aria-hidden="true"
                    >
                      <path d="m9 5 7 7-7 7" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </button>
                </li>
              </ul>
              <p v-else class="mt-3 rounded-lg bg-surface-canvas px-3 py-2.5 text-caption leading-relaxed text-neutral-500">
                暂无角色绑定该音色。可在角色详情页的「音色绑定」卡中选择并试听。
              </p>
              <p
                v-if="detail.bound_character_count > detail.bound_characters.length"
                class="mt-2 text-caption text-neutral-400"
              >
                仅展示最近 {{ detail.bound_characters.length }} 个，共 {{ detail.bound_character_count }} 个。
              </p>
            </section>

            <!-- 配音引用片段 -->
            <section class="gd-card p-4 sm:p-5" aria-label="配音引用">
              <div class="flex items-start justify-between gap-3">
                <div>
                  <span class="gd-section-label">配音引用 · REFERENCE</span>
                  <h2 class="mt-0.5 text-title text-neutral-900">按 ID 接入配音模块</h2>
                </div>
                <AppButton variant="ghost" size="sm" @click="copyReference">复制片段</AppButton>
              </div>
              <pre class="gd-num mt-3 overflow-x-auto rounded-lg bg-surface-inset px-3 py-3 text-caption leading-relaxed text-neutral-300">{{ referenceSnippet }}</pre>
              <p class="mt-2 text-caption leading-relaxed text-neutral-500">
                voice_id 为稳定主键；voice_key 与 engine_voice 用于排障与展示。试听文本、语速与音调可在配音时逐条覆盖。
              </p>
            </section>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 重新生成样音确认弹窗 ============ -->
    <ConfirmDialog
      v-model="regenDialogOpen"
      title="重新生成音色样音？"
      :content="regenDialogContent"
      confirm-label="重新生成"
      :loading="regenerating"
      @confirm="confirmRegenerate"
      @cancel="regenDialogOpen = false"
    />
  </div>
</template>

<style scoped>
/* 自定义音频进度条：与 aurora 令牌同源（轨道用 --gd-progress-track，填充用品牌紫） */
.gd-audio-range {
  -webkit-appearance: none;
  appearance: none;
  height: 6px;
  border-radius: 9999px;
  outline: none;
  cursor: pointer;
}

.gd-audio-range:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.gd-audio-range::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 14px;
  height: 14px;
  border-radius: 9999px;
  border: 2px solid var(--gd-primary-500);
  background: var(--gd-ink);
  box-shadow: 0 0 10px rgba(139, 124, 255, 0.5);
}

.gd-audio-range::-moz-range-thumb {
  width: 14px;
  height: 14px;
  border-radius: 9999px;
  border: 2px solid var(--gd-primary-500);
  background: var(--gd-ink);
  box-shadow: 0 0 10px rgba(139, 124, 255, 0.5);
}

.gd-audio-range:focus-visible {
  outline: 2px solid var(--gd-primary-500);
  outline-offset: 3px;
}

/* 播放中的波形：以柱底为轴轻微起伏，形成可感知的播放反馈 */
.voice-wave__bar.is-live {
  animation: voice-wave 900ms var(--gd-ease-in-out) infinite alternate;
  transform-origin: bottom;
}

@keyframes voice-wave {
  from {
    transform: scaleY(0.45);
  }
  to {
    transform: scaleY(1);
  }
}

@media (prefers-reduced-motion: reduce) {
  .voice-wave__bar.is-live {
    animation: none;
  }
}
</style>

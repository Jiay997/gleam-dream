<!--
  文件级注释：P17 · 成片确认（HITL-4，路由 /projects/:projectId/post/review，Task 15）。
  v7 纸上放映厅 Pro · 对照 F6-02（P17 成片确认 · 关口四）：
  - 刊头「关口四 · 成片确认」+ approval_type: final_cut 胶囊 + 审批倒计时（expires_at 静态换算）；
  - 左 60% 带水印预览（9:16 大预览 + 「预览 · 成片」角标 + 演示预览横幅）+ 播放控制条；
  - 预览下方合规横幅：片尾 AI 生成内容标识（合规强制 · 不可关闭）；
  - 右 40% 面板组：后期质检摘要（passed 胶囊 + 音画同步 / 字幕完整性 / 内容合规行式布局）、
    成本汇总（渲染阶段实际消耗聚合 CreditAmountText）、时间线信息；
  - 底部操作栏：编辑时间线 / 退回修改（danger）/ 通过并生成母版（primary）；
    退回弹窗（F6-02-s1）：时间码范围（起–止可编辑）+ 处理动作 chips
    （re_tts 重新配音 / re_bgm 换曲 / re_mix 重混音 / re_subtitle 改字幕）+ 反馈说明
    （≥10 字校验，≤200 字计数）；确认后按时间码 + 动作 + 说明组合为 feedback 提交
    POST /orchestration/approvals/{id}/decide action=reject → 回 P16。
  通过二次确认 → decide approve → 跳 P18 分发。过期审批红色横幅（expires_at < now 禁用操作）。
  数据：GET /orchestration/approvals?project_id=&approval_type=final_cut&status=pending 取第一单；
  无单显示「未合成或已确认」空态 + 跳 P16。
  决策端点差异说明：任务稿写的 POST /projects/{id}/post/decide 后端未实现，
  实际对齐 orchestration.py 的 approvals/{id}/decide（approvals.ts 已就绪）。
  备注：合成中分段进度（TTS / 字幕 / BGM / 响度 / 组装，F6-02-s2）需后端提供分阶段
  进度数据，当前 API 仅返回整体 status，暂未落地（见重构报告）。
-->
<script setup lang="ts">
/**
 * FinalReviewView 成片确认（HITL-4）。
 * props：无。emits：无。
 * 依赖 approvals API（pending final_cut 单 / decide）、post API（timeline 播放）、
 * render API（结算聚合）、ApprovalBar、ConfirmDialog。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { decideApproval, listApprovals, type ApprovalItem } from '@/api/approvals'
import { getProjectTimeline, type TimelineClipItem, type TimelineData } from '@/api/post'
import { listRenderTasks } from '@/api/render'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import CreditAmountText from '@/components/common/CreditAmountText.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import Modal from '@/components/common/Modal.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import { PLAYER } from '@/constants/canvasTokens'
import { formatRelativeTime, formatShortDate } from '@/utils/datetime'

const route = useRoute()
const router = useRouter()
const toast = useToast()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ==================== 审批单加载 ==================== */

/** 待审成片审批单 */
const approval = ref<ApprovalItem | null>(null)
/** 审批单加载中 */
const approvalLoading = ref(true)
/** 审批单加载失败文案 */
const approvalError = ref('')
/** 是否已过期（expires_at < now） */
const isExpired = computed(() => {
  const expires = approval.value?.expires_at
  if (!expires) return false
  const time = new Date(expires).getTime()
  return !Number.isNaN(time) && time < Date.now()
})

/** 审批倒计时文案（HH:MM:SS 静态换算，过期显示「已过期」；F6-02 刊头倒计时） */
const countdownLabel = computed(() => {
  const expires = approval.value?.expires_at
  if (!expires) return '—'
  const time = new Date(expires).getTime()
  if (Number.isNaN(time)) return '—'
  const diff = time - Date.now()
  if (diff <= 0) return '已过期'
  const totalSeconds = Math.floor(diff / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds].map((n) => String(n).padStart(2, '0')).join(':')
})

/**
 * 加载待审成片审批单（后端查询前惰性过期，此处再防御性判断 expires_at）。
 * @returns 无
 */
async function loadApproval(): Promise<void> {
  approvalLoading.value = true
  approvalError.value = ''
  try {
    const page = await listApprovals({
      project_id: projectId.value,
      approval_type: 'final_cut',
      status: 'pending',
      page: 1,
      page_size: 5,
    })
    // 双保险：后端已支持 approval_type 过滤，前端再按类型收口，避免串关口
    approval.value = page.items.find((item) => item.approval_type === 'final_cut') ?? null
  } catch (error) {
    approval.value = null
    approvalError.value = error instanceof Error ? error.message : '审批单加载失败'
  } finally {
    approvalLoading.value = false
  }
}

/* ==================== 时间线与播放 ==================== */

/** 时间线聚合数据 */
const timelineData = ref<TimelineData | null>(null)
/** 片段列表 */
const clips = ref<TimelineClipItem[]>([])
/** 时间线加载中 */
const timelineLoading = ref(false)
/** 时间线加载失败文案 */
const timelineError = ref('')
/** 是否播放中 */
const playing = ref(false)
/** 播放指针（毫秒） */
const playheadMs = ref(0)
/** 播放定时器 */
let playTimer: ReturnType<typeof setInterval> | null = null

/** 视频片段（按起点排序） */
const videoClips = computed(() =>
  clips.value.filter((clip) => clip.track_type === 'video').sort((a, b) => a.start_ms - b.start_ms),
)

/** 字幕片段（按起点排序） */
const subtitleClips = computed(() =>
  clips.value.filter((clip) => clip.track_type === 'subtitle').sort((a, b) => a.start_ms - b.start_ms),
)

/** 音频片段（按起点排序） */
const audioClips = computed(() =>
  clips.value.filter((clip) => clip.track_type === 'audio').sort((a, b) => a.start_ms - b.start_ms),
)

/** 混流轨数（修复 E2E 报告 P-11）：与 P6 后期合成页保持同一口径——
 *  固定四轨（画面 / 字幕 / 音频 / BGM）定义，而非「出现片段的轨集合」，
 *  避免 BGM 轨暂无片段时被误显示为 3 轨。 */
const trackCount = 4

/** 总时长（毫秒） */
const totalMs = computed(() => {
  const fromTimeline = timelineData.value?.timeline?.duration_ms ?? 0
  if (fromTimeline > 0) return fromTimeline
  return clips.value.reduce((max, clip) => Math.max(max, clip.end_ms), 0)
})

/**
 * 加载时间线（预览版；失败仅影响播放区，不阻塞审批操作）。
 * @returns 无
 */
async function loadTimeline(): Promise<void> {
  if (!approval.value) return
  timelineLoading.value = true
  timelineError.value = ''
  try {
    const data = await getProjectTimeline(projectId.value, 'preview')
    timelineData.value = data
    clips.value = Array.isArray(data.clips) ? [...data.clips] : []
    playheadMs.value = 0
  } catch (error) {
    timelineData.value = null
    clips.value = []
    timelineError.value = error instanceof Error ? error.message : '时间线加载失败'
  } finally {
    timelineLoading.value = false
  }
}

/**
 * 播放 tick：推进指针，播完自动暂停。
 * @returns 无
 */
function tick(): void {
  if (playheadMs.value + 100 >= totalMs.value) {
    playheadMs.value = totalMs.value
    pause()
    return
  }
  playheadMs.value += 100
}

/**
 * 开始播放（修复测试报告 #34：totalMs<=0 时静默返回导致点击无反应）。
 * 时间线为空/加载失败时给出 toast 说明原因，而不是无声忽略。
 * @returns 无
 */
function play(): void {
  if (playing.value) return
  if (timelineLoading.value) {
    toast.warning('预览时间线加载中，请稍候')
    return
  }
  if (totalMs.value <= 0) {
    toast.warning(timelineError.value ? `预览时间线不可用：${timelineError.value}` : '预览时间线暂无内容，无法播放')
    return
  }
  if (playheadMs.value >= totalMs.value) playheadMs.value = 0
  playing.value = true
  playTimer = setInterval(tick, 100)
}

/**
 * 暂停播放。
 * @returns 无
 */
function pause(): void {
  playing.value = false
  if (playTimer) {
    clearInterval(playTimer)
    playTimer = null
  }
}

/**
 * 毫秒 → mm:ss 时间码。
 * @param ms 毫秒
 * @returns 时间码
 */
function timecode(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000)
  return `${String(Math.floor(totalSeconds / 60)).padStart(2, '0')}:${String(totalSeconds % 60).padStart(2, '0')}`
}

/** 当前指针所在视频片段 */
const currentVideoClip = computed(() =>
  videoClips.value.find((clip) => playheadMs.value >= clip.start_ms && playheadMs.value < clip.end_ms) ?? null,
)

/** 当前指针所在字幕文本 */
const currentSubtitle = computed(
  () => subtitleClips.value.find((clip) => playheadMs.value >= clip.start_ms && playheadMs.value < clip.end_ms)?.content ?? '',
)

/** 视频块色相（无缩略图时占位渐变） */
function clipHue(clip: TimelineClipItem): number {
  const seed = Number(clip.shot_no ?? clip.source_id ?? clip.id) || 0
  return (seed * 47) % 360
}

onBeforeUnmount(pause)

/* ==================== 质检面板 ==================== */

/** 音画同步偏差条目 */
interface SyncPair {
  /** 标签（镜号 ↔ 配音序号） */
  label: string
  /** 偏差毫秒 */
  diffMs: number
}

/**
 * 读取片段 content 中携带的镜头 ID。
 * 后端装配 video / audio 轨时会把 shot_id 写入 content，用于前后端同口径的音画配对。
 * @param clip 时间线片段
 * @returns 镜头 ID；缺失或非法时返回 null
 */
function clipShotId(clip: TimelineClipItem): number | null {
  const raw = clip.content as unknown
  if (!raw || typeof raw !== 'object') return null
  const shotId = Number((raw as Record<string, unknown>).shot_id)
  return Number.isFinite(shotId) && shotId > 0 ? shotId : null
}

/** 音画同步逐对偏差（video/audio 按 shot_id 精确配对，与后端 70001 校验口径一致） */
const syncPairs = computed<SyncPair[]>(() => {
  const videos = videoClips.value
  const audios = audioClips.value
  const videoByShot = new Map<number, TimelineClipItem>()
  for (const video of videos) {
    const shotId = clipShotId(video)
    if (shotId !== null) videoByShot.set(shotId, video)
  }
  const list: SyncPair[] = []
  if (videoByShot.size > 0) {
    for (const audio of audios) {
      const shotId = clipShotId(audio)
      const video = shotId === null ? undefined : videoByShot.get(shotId)
      if (!video) continue
      list.push({
        label: `S${video.shot_no ?? video.source_id ?? video.id} ↔ ${audio.label ?? `配音 ${audio.order_index ?? audio.id}`}`,
        diffMs: Math.abs(audio.start_ms - video.start_ms),
      })
    }
    return list
  }
  // 兼容旧数据：两轨均无 shot_id 元数据时按序配对
  const count = Math.min(videos.length, audios.length)
  for (let index = 0; index < count; index += 1) {
    list.push({
      label: `S${videos[index].shot_no ?? videos[index].source_id ?? index + 1} ↔ 配音 ${index + 1}`,
      diffMs: Math.abs(audios[index].start_ms - videos[index].start_ms),
    })
  }
  return list
})

/** 音画同步是否全部达标（≤100ms 且至少一对） */
const syncOk = computed(() => syncPairs.value.length > 0 && syncPairs.value.every((pair) => pair.diffMs <= 100))

/** 字幕完整性：字幕片段数 vs 视频片段数 */
const subtitleOk = computed(() => videoClips.value.length > 0 && subtitleClips.value.length >= videoClips.value.length)

/* ==================== 结算与时间线信息 ==================== */

/** 渲染阶段实际消耗积分合计 */
const renderCostCredits = ref(0)
/** 结算加载中 */
const costLoading = ref(false)

/**
 * 聚合渲染任务实际消耗（分页拉取 Σ actual_cost_credits）。
 * @returns 无
 */
async function loadRenderCost(): Promise<void> {
  costLoading.value = true
  try {
    let sum = 0
    let page = 1
    let total = Infinity
    while (page <= 10 && (page - 1) * 100 < total) {
      const result = await listRenderTasks(projectId.value, { page, page_size: 100 })
      total = result.total
      sum += result.items.reduce((acc, task) => acc + (task.actual_cost_credits ?? 0), 0)
      if (result.items.length < 100) break
      page += 1
    }
    renderCostCredits.value = sum
  } catch {
    renderCostCredits.value = 0
  } finally {
    costLoading.value = false
  }
}

/* ==================== 审批决策 ==================== */

/** 决策请求中 */
const deciding = ref(false)
/** 通过二次确认弹窗 */
const approveConfirmOpen = ref(false)

/**
 * 提交通过（HITL-4 approve：结算推进，项目流转至待分发）。
 * @returns 无
 */
async function handleApprove(): Promise<void> {
  if (!approval.value || deciding.value) return
  deciding.value = true
  try {
    await decideApproval(approval.value.id, { action: 'approve' })
    approveConfirmOpen.value = false
    toast.success('成片已确认通过，项目进入分发准备')
    pause()
    await router.push(`/projects/${projectId.value}/distribute`)
  } catch {
    // 拦截器已 toast（40001 已处理 / 已过期等）
  } finally {
    deciding.value = false
  }
}

/**
 * 提交驳回（feedback ≥10 字由 ApprovalBar 校验；回到后期编辑器修改）。
 * @param feedback 驳回原因
 * @returns 无
 */
async function handleReject(feedback: string): Promise<void> {
  if (!approval.value || deciding.value) return
  deciding.value = true
  try {
    await decideApproval(approval.value.id, { action: 'reject', feedback })
    toast.success('已驳回成片，请回到后期编辑器调整时间线')
    pause()
    await router.push(`/projects/${projectId.value}/post`)
  } catch {
    // 拦截器已 toast
  } finally {
    deciding.value = false
  }
}

/* ==================== 退回反馈弹窗（F6-02-s1：时间码范围 + 处理动作 chips） ==================== */

/** 退回弹窗显示状态 */
const rejectModalOpen = ref(false)
/** 退回范围：起始时间码（mm:ss，可编辑） */
const rejectStartTc = ref('00:00')
/** 退回范围：结束时间码（mm:ss，可编辑） */
const rejectEndTc = ref('00:00')
/** 已选处理动作（value 集合，可多选） */
const rejectActions = ref<string[]>([])
/** 反馈说明 */
const rejectNote = ref('')
/** 反馈说明校验错误 */
const rejectNoteError = ref('')

/** 处理动作选项（chips；value 对齐后期处理动作语义） */
const REJECT_ACTION_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 're_tts', label: 're_tts · 重新配音' },
  { value: 're_bgm', label: 're_bgm · 换曲' },
  { value: 're_mix', label: 're_mix · 重混音' },
  { value: 're_subtitle', label: 're_subtitle · 改字幕' },
]

/** 反馈说明是否有效（≥10 字，对齐 ApprovalBar 原校验规则） */
const rejectNoteValid = computed(() => rejectNote.value.trim().length >= 10)

/**
 * 打开退回弹窗：时间码预填当前播放头 ±17s（与设计稿示意一致），清空动作与说明。
 * @returns 无
 */
function openRejectModal(): void {
  const startMs = totalMs.value > 0 ? Math.min(playheadMs.value, Math.max(0, totalMs.value - 1000)) : 0
  const endMs = totalMs.value > 0 ? Math.min(totalMs.value, startMs + 17_000) : Math.min(17_000, startMs + 17_000)
  rejectStartTc.value = timecode(startMs)
  rejectEndTc.value = timecode(endMs)
  rejectActions.value = []
  rejectNote.value = ''
  rejectNoteError.value = ''
  rejectModalOpen.value = true
}

/**
 * 切换处理动作 chip 选中态。
 * @param value 动作值
 * @returns 无
 */
function toggleRejectAction(value: string): void {
  const index = rejectActions.value.indexOf(value)
  if (index >= 0) {
    rejectActions.value.splice(index, 1)
  } else {
    rejectActions.value.push(value)
  }
}

/**
 * 确认退回：校验 ≥10 字说明后，按「时间码 + 处理动作 + 说明」组合 feedback 提交。
 * @returns 无
 */
async function confirmReject(): Promise<void> {
  if (!rejectNoteValid.value) {
    rejectNoteError.value = '请填写至少 10 个字的反馈说明，便于创作侧修改'
    return
  }
  rejectNoteError.value = ''
  const actionLabels = REJECT_ACTION_OPTIONS.filter((opt) => rejectActions.value.includes(opt.value)).map((opt) => opt.label)
  const feedback = `时间码 ${rejectStartTc.value}–${rejectEndTc.value}${actionLabels.length > 0 ? `；处理动作：${actionLabels.join('、')}` : ''}；${rejectNote.value.trim()}`
  await handleReject(feedback)
  rejectModalOpen.value = false
}

/* ==================== 生命周期 ==================== */

// 挂载：加载审批单 → 有单则加载时间线 + 结算聚合
onMounted(async () => {
  await loadApproval()
  if (approval.value) {
    void loadTimeline()
    void loadRenderCost()
  }
})
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <!-- ============ 加载 / 失败 / 无单空态 ============ -->
    <div v-if="approvalLoading" class="flex flex-1 items-center justify-center p-8">
      <div class="w-full max-w-lg"><LoadingSkeleton variant="card" /></div>
    </div>
    <div v-else-if="approvalError" class="flex flex-1 items-center justify-center p-8">
      <div class="w-full max-w-md"><ErrorRetryBlock message="审批单加载失败" :description="approvalError" @retry="loadApproval" /></div>
    </div>
    <div v-else-if="!approval" class="flex flex-1 items-center justify-center p-8">
      <EmptyState
        icon="✅"
        title="未合成或已确认"
        description="当前没有待确认的成片审批单：可能尚未发起合成，或成片已通过 / 已驳回。可回到后期编辑器发起合成或查看状态。"
      >
        <template #action>
          <AppButton variant="primary" size="lg" @click="router.push(`/projects/${projectId}/post`)">去后期编辑器 →</AppButton>
        </template>
      </EmptyState>
    </div>

    <!-- ============ 有单主体 ============ -->
    <template v-else>
      <!-- 过期横幅 -->
      <div v-if="isExpired" class="flex h-10 shrink-0 items-center justify-center gap-2 bg-danger-bg text-xs font-medium text-danger" role="alert">
        ⛔ 审批单已过期（{{ formatShortDate(approval.expires_at) }}），操作已禁用——请回到后期编辑器重新发起合成生成新审批单
      </div>

      <!-- ===== 刊头：关口四 · 成片确认（F6-02） ===== -->
      <header class="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-b border-hairline px-5 py-3">
        <div class="flex items-center gap-3">
          <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
          <h1 class="gd-masthead text-display-lg">关口四 · 成片确认</h1>
        </div>
        <span class="gd-num text-xs text-neutral-400">审批单 #{{ approval.id }} · 发起于 {{ formatRelativeTime(approval.requested_at) }}</span>
        <span class="gd-num text-xs" :class="isExpired ? 'text-danger' : 'text-gd-pending'">
          审批倒计时 {{ countdownLabel }} · 请于 {{ formatShortDate(approval.expires_at) }} 前处理
        </span>
        <div class="flex-1" />
        <AppButton variant="ghost" size="sm" @click="router.push(`/projects/${projectId}/post`)">← 返回后期编辑器</AppButton>
      </header>

      <!-- ===== 主体：预览 60% + 面板 40% ===== -->
      <div class="flex min-h-0 flex-1">
        <!-- 左：带水印预览 -->
        <section class="flex min-w-0 flex-[3] flex-col bg-player-bg">
          <div class="flex min-h-0 flex-1 items-center justify-center p-5">
            <!-- 播放器底：暗场里 neutral-900 已被令牌桥接成「最亮正文色」，
                 写 bg-neutral-900 会得到一块白板。媒体底一律用 surface-sunken。 -->
            <div class="relative h-full max-w-[380px] overflow-hidden rounded-xl bg-surface-sunken shadow-gd-4" style="aspect-ratio: 9 / 16">
              <!-- 当前镜头画面 -->
              <img
                v-if="currentVideoClip?.thumb_url"
                :src="currentVideoClip.thumb_url"
                :alt="`镜头 ${currentVideoClip.shot_no ?? ''} 画面`"
                class="h-full w-full object-cover"
              />
              <div
                v-else
                class="flex h-full w-full flex-col items-center justify-center gap-2"
                :style="currentVideoClip ? { background: `linear-gradient(160deg, hsl(${clipHue(currentVideoClip)} 42% 30%), hsl(${(clipHue(currentVideoClip) + 40) % 360} 42% 18%))` } : { background: PLAYER.panel }"
              >
                <span class="text-4xl opacity-80" aria-hidden="true">🎬</span>
                <span class="gd-num text-sm text-neutral-400">{{ currentVideoClip ? `S${currentVideoClip.shot_no ?? currentVideoClip.source_id ?? ''}` : '等待播放' }}</span>
              </div>

              <!-- 水印角标 + 演示横幅 -->
              <span class="absolute right-3 top-3 rounded-full bg-scrim-soft px-2.5 py-1 text-caption text-neutral-200">预览 · 成片</span>
              <div class="absolute inset-x-0 bottom-12 bg-scrim-faint py-1.5 text-center text-xs tracking-[0.3em] text-white/80">演 示 预 览</div>

              <!-- 字幕叠加 -->
              <div class="absolute inset-x-0 bottom-0 bg-gradient-to-t from-scrim-strong to-transparent px-5 pb-4 pt-8">
                <p
                  class="line-clamp-2 text-center text-sm font-medium leading-relaxed text-white"
                  style="text-shadow: var(--gd-scrim-strong) 1px 0 0, var(--gd-scrim-strong) -1px 0 0, var(--gd-scrim-strong) 0 1px 0, var(--gd-scrim-strong) 0 -1px 0, var(--gd-scrim-strong) 1px 1px 0, var(--gd-scrim-strong) -1px -1px 0"
                >
                  {{ currentSubtitle || '（无字幕）' }}
                </p>
              </div>
            </div>
          </div>
          <!-- 播放控制（P14 播放器简化版） -->
          <div class="flex h-14 shrink-0 items-center gap-4 border-t border-white/10 px-5">
            <button
              type="button"
              class="flex h-9 w-9 items-center justify-center rounded-full gd-fill-brand shadow-gd-1 transition-colors hover:bg-primary-600"
              :aria-label="playing ? '暂停' : '播放'"
              @click="playing ? pause() : play()"
            >
              <svg v-if="!playing" class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <path d="M8 5.14v13.72c0 .8.87 1.3 1.56.9l11-6.86a1.05 1.05 0 000-1.8l-11-6.86A1.05 1.05 0 008 5.14z" />
              </svg>
              <svg v-else class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                <path d="M7 5h4v14H7zM13 5h4v14h-4z" />
              </svg>
            </button>
            <span class="gd-num text-xs text-neutral-400">{{ timecode(playheadMs) }} / {{ timecode(totalMs) }}</span>
            <p v-if="timelineError" class="truncate text-xs text-danger">{{ timelineError }}（预览不可用，不影响审批）</p>
            <p v-else class="truncate text-caption text-neutral-500">按时间线视频轨顺序播放（演示预览，非合成母版）</p>
          </div>

          <!-- 合规强制横幅：片尾 AI 生成内容标识（不可关闭） -->
          <div class="mx-5 mb-4 flex items-start gap-2.5 rounded-lg border border-warning-border bg-warning-bg px-4 py-3" role="note">
            <svg class="mt-0.5 h-4 w-4 shrink-0 text-gd-pending" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M7 11V7a5 5 0 0110 0v4M5 11h14v9H5z" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <p class="text-xs leading-relaxed text-gd-pending">
              <span class="font-semibold">合规强制 · 片尾 AI 生成内容标识：</span>母版片尾将固定展示「本内容由 AI 辅助生成」标识，并随分发元数据向平台声明。
            </p>
            <span class="ml-auto inline-flex shrink-0 items-center gap-1 self-center text-caption text-gd-pending">不可关闭</span>
          </div>
        </section>

        <!-- 双域雾带交界（09 号文档 2.4：左暗场预览 / 右暖纸审核面板） -->
        <div class="gd-fog-junction-v shrink-0" aria-hidden="true" />

        <!-- 右：后期质检摘要 / 成本汇总 / 时间线信息 -->
        <aside class="flex min-w-0 flex-[2] flex-col overflow-y-auto bg-surface-raised">
          <!-- 后期质检摘要 -->
          <section class="border-b border-hairline-soft p-4">
            <div class="flex items-center justify-between">
              <h3 class="gd-kicker">后期质检摘要</h3>
              <StatusTag
                :status="syncOk ? 'completed' : 'failed'"
                :label="syncOk ? 'passed · 通过' : '未通过'"
                size="sm"
              />
            </div>

            <!-- 音画同步 -->
            <div class="mt-3">
              <p class="text-caption font-medium text-neutral-600">
                音画同步（≤100ms 达标）
                <span v-if="syncPairs.length > 0" class="ml-1 font-normal" :class="syncOk ? 'text-success' : 'text-danger'">
                  {{ syncOk ? '全部达标' : `${syncPairs.filter((p) => p.diffMs > 100).length} 处超限` }}
                </span>
              </p>
              <ul v-if="syncPairs.length > 0" class="mt-1.5 max-h-32 space-y-1 overflow-y-auto">
                <li
                  v-for="pair in syncPairs"
                  :key="pair.label"
                  class="flex items-center justify-between rounded-md px-2.5 py-1.5 text-caption"
                  :class="pair.diffMs <= 100 ? 'bg-success-bg text-success' : 'bg-danger-bg text-danger'"
                >
                  <span class="text-neutral-600">{{ pair.label }}</span>
                  <span class="gd-num font-medium">偏差 {{ pair.diffMs }}ms {{ pair.diffMs <= 100 ? '✓' : '✕' }}</span>
                </li>
              </ul>
              <p v-else class="mt-1.5 text-caption text-neutral-400">时间线无配音片段，跳过音画校验</p>
            </div>

            <!-- 字幕完整性 -->
            <div class="mt-3">
              <p class="text-caption font-medium text-neutral-600">字幕完整性</p>
              <p class="gd-num mt-1 text-caption" :class="subtitleOk ? 'text-success' : 'text-gd-pending'">
                {{ subtitleOk ? '✓' : '⚠' }} 字幕片段 {{ subtitleClips.length }} / 视频片段 {{ videoClips.length }}
              </p>
            </div>

            <!-- 合规（moderation 占位） -->
            <div class="mt-3">
              <p class="text-caption font-medium text-neutral-600">内容合规</p>
              <p class="mt-1 text-caption text-neutral-400">moderation 检查在合成阶段由后端执行（演示环境占位）</p>
            </div>
          </section>

          <!-- 成本汇总 -->
          <section class="border-b border-hairline-soft p-4">
            <h3 class="gd-kicker">成本汇总</h3>
            <div class="mt-2 flex items-baseline gap-1.5">
              <span class="text-caption text-neutral-400">本项目累计消耗</span>
              <CreditAmountText :amount="renderCostCredits" class="text-lg font-semibold" />
              <span class="text-caption text-neutral-400">积分</span>
            </div>
            <p class="mt-1 text-caption text-neutral-400">聚合渲染任务实际结算积分；后期合成费用以最终结算为准{{ costLoading ? ' · 统计中…' : '' }}</p>
          </section>

          <!-- 时间线信息 -->
          <section class="p-4">
            <h3 class="text-xs font-semibold text-neutral-500">时间线信息</h3>
            <dl class="mt-2 space-y-1.5 text-caption text-neutral-500">
              <div class="flex justify-between"><dt>总时长</dt><dd class="gd-num">{{ timecode(totalMs) }}</dd></div>
              <div class="flex justify-between"><dt>镜头数</dt><dd class="gd-num">{{ videoClips.length }}</dd></div>
              <div class="flex justify-between"><dt>轨数</dt><dd class="gd-num">{{ trackCount }}（视频/配音/字幕/BGM）</dd></div>
              <div class="flex justify-between"><dt>片段总数</dt><dd class="gd-num">{{ clips.length }}</dd></div>
              <div class="flex justify-between"><dt>版本</dt><dd class="gd-num">{{ timelineData?.timeline?.cut_type ?? '—' }} · v{{ timelineData?.timeline?.version_no ?? '—' }}</dd></div>
            </dl>
          </section>
        </aside>
      </div>

      <!-- ===== 底部操作栏（吸底 72px：编辑时间线 / 退回修改 / 通过并生成母版） ===== -->
      <footer class="flex h-footer-bar shrink-0 items-center gap-3 border-t border-hairline bg-surface-raised px-5">
        <p class="mr-4 hidden text-xs text-neutral-400 lg:block">通过后生成母版（草稿），进入导出与分发；退回请填写反馈，将按时间码定位问题段落重做</p>
        <div class="flex-1" />
        <AppButton variant="secondary" size="md" :disabled="deciding" @click="router.push(`/projects/${projectId}/post`)">编辑时间线</AppButton>
        <AppButton variant="danger" size="md" :disabled="isExpired || deciding" @click="openRejectModal">
          <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M3 12a9 9 0 109-9m9 9c0 .34-.02.67-.05 1M3 12c0 .34.02.67.05 1M12 3c-.34 0-.67.02-1 .05M21 12l-3-3m3 3l-3 3M3 12l3-3m-3 3l3 3" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          退回修改
        </AppButton>
        <AppButton variant="primary" size="md" :disabled="isExpired || deciding" @click="approveConfirmOpen = true">
          <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          通过并生成母版
        </AppButton>
      </footer>
    </template>

    <!-- ============ 退回反馈弹窗（F6-02-s1：时间码范围 + 处理动作 chips + 反馈说明） ============ -->
    <Modal v-model="rejectModalOpen" title="退回修改 · 请填写反馈" :max-width="520">
      <p class="text-xs text-neutral-500">反馈将按时间码定位到对应轨道，仅重做问题段落。</p>

      <!-- 退回范围：时间码起止 -->
      <div class="mt-4">
        <span class="mb-1.5 block text-xs font-medium text-neutral-500">退回范围</span>
        <div class="flex items-center gap-2 rounded-lg border border-hairline bg-surface-raised px-3.5 py-2.5">
          <span class="text-xs text-neutral-500">时间码</span>
          <input v-model="rejectStartTc" type="text" class="gd-num w-16 bg-transparent text-sm font-semibold text-neutral-900 outline-none" aria-label="起始时间码" />
          <span class="text-neutral-300" aria-hidden="true">–</span>
          <input v-model="rejectEndTc" type="text" class="gd-num w-16 bg-transparent text-sm font-semibold text-neutral-900 outline-none" aria-label="结束时间码" />
          <span class="ml-auto hidden text-caption text-neutral-400 sm:block">按视频轨时间码定位</span>
        </div>
      </div>

      <!-- 处理动作 chips -->
      <div class="mt-4">
        <span class="mb-1.5 block text-xs font-medium text-neutral-500">处理动作（可多选）</span>
        <div class="flex flex-wrap gap-2">
          <button
            v-for="option in REJECT_ACTION_OPTIONS"
            :key="option.value"
            type="button"
            class="inline-flex h-7 items-center rounded-full border px-3 text-caption transition-colors duration-gd-fast"
            :class="
              rejectActions.includes(option.value)
                ? 'border-primary-500 bg-primary-50 font-medium text-primary-700'
                : 'border-hairline bg-surface-raised text-neutral-500 hover:border-primary-300'
            "
            :aria-pressed="rejectActions.includes(option.value)"
            @click="toggleRejectAction(option.value)"
          >
            {{ option.label }}
          </button>
        </div>
      </div>

      <!-- 反馈说明 -->
      <div class="mt-4">
        <span class="mb-1.5 block text-xs font-medium text-neutral-500">反馈说明 <span class="text-danger" aria-hidden="true">*</span></span>
        <textarea
          v-model="rejectNote"
          rows="3"
          maxlength="200"
          class="gd-input resize-none"
          placeholder="描述问题与期望的处理方式，例如：这句台词情绪太平了，希望换成隐忍带颤音的感觉，参考 S12 的处理。"
        />
        <div class="mt-1 flex items-center justify-between text-caption">
          <span :class="rejectNoteError ? 'text-danger' : 'text-neutral-400'">{{ rejectNoteError || '空反馈将被阻止提交' }}</span>
          <span class="gd-num text-neutral-400">{{ rejectNote.trim().length }}/200</span>
        </div>
      </div>

      <template #footer>
        <div class="flex justify-end gap-2.5">
          <AppButton variant="secondary" size="md" :disabled="deciding" @click="rejectModalOpen = false">取消</AppButton>
          <AppButton variant="danger" size="md" :loading="deciding" @click="confirmReject">确认退回</AppButton>
        </div>
      </template>
    </Modal>

    <!-- ============ 通过二次确认 ============ -->
    <ConfirmDialog
      v-model="approveConfirmOpen"
      title="确认通过成片？"
      content="渲染费用已按任务实际用量在渲染阶段结算；母版合成与导出不消耗积分。通过后推进项目至分发准备（P18），该决策记录为成片确认（HITL-4）的最终通过。"
      confirm-label="确认通过"
      :loading="deciding"
      @confirm="handleApprove"
      @cancel="approveConfirmOpen = false"
    />
  </div>
</template>

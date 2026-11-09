<!--
  文件级注释：P16 · 成片预览与时间线（路由 /projects/:projectId/post，Task 15）。
  v9 版面梳理（只调整布局与信息层级，功能与数据流不变）：
  ① 刊头：标题 + 一行上下文（镜头 / 时长 / 轨数 / 成片状态）+ cut_type 切换；右侧只保留
    字幕样式与「合成成片」主 CTA。30s 样音、LUFS 交付口径归位到声音工作台，BGM 入口
    收敛到「编导 BGM」页签与时间线 BGM 行，刊头控件由 9 个降为 3 个；
  ② 主区两栏（lg:grid-cols-12，左 5 右 7）：成片监看 + 声音工作台。9:16 舞台改为纵向
    两端对齐（水印贴顶、字幕贴底），安全区只保留虚线框，去掉多余角标；
  ③ gd-fog-junction 雾带上叠加贯穿播放头（与 Konva 播放头同色 --gd-playhead）；
  ④ 多轨时间线卡（轨头列 + 标尺行 + 四轨）：轨道头只保留轨名 / 计数 / 规格，移除无数据源的
    禁用 M/S 结构件；DOM 标尺与 Konva 轨道体同宽同滚，拖拽/吸附/保存逻辑零改动；
  ⑤ 底部 HITL 关口四(H4) 终审卡：四个质检药丸合并为一行质检状态（音画同步为真实测量值，
    黑帧/爆音标注「待合成回填」）+ 退回 / 批准双按钮（路由至 P17 成片确认）。
  既有功能全部保留：三态加载、cut_type 切换、缩放、保存、拖拽吸附、音画警示、样音试听、
  全剧配音、字幕样式应用、BGM 推荐应用、合成轮询。数据：GET/PUT post/timeline、tts-preview/tts、
  subtitles、bgm-recommend/bgm、compose、subtitle-styles（后端 post 域未就绪时三态兜底）。
  空态：无时间线 / clips 为空 → 引导去 P15 生成任务中心。
-->
<script setup lang="ts">
/**
 * PostEditorView 后期编辑器（四轨时间线 · 按 stitch_prd/p6_clean_pro 对齐布局）。
 * props：无。emits：无。
 * 依赖 post API（timeline/tts/subtitles/bgm/compose）、Konva 时间线、useToast、
 * ConfirmDialog（合成确认）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/http'
import {
  applySubtitleStyle,
  composeFinal,
  getPostPreviewUrl,
  getProjectTimeline,
  getVoiceCoverage,
  listSubtitleStyles,
  previewTts,
  recommendBgm,
  saveProjectTimeline,
  startFullTts,
  TIMELINE_STATUS_LABELS,
  TRACK_TYPE_LABELS,
  updateBgm,
  type BgmTrackItem,
  type SubtitleStyleItem,
  type TimelineClipItem,
  type TimelineData,
  type VoiceCoverageData,
  type TimelineTrackType,
} from '@/api/post'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import { useToast } from '@/composables/useToast'
import { BRAND, EDITOR, PLAYER, TIMELINE } from '@/constants/canvasTokens'

const route = useRoute()
const router = useRouter()
const toast = useToast()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ==================== 时间线数据 ==================== */

/** 版本类型（preview 预览 / master 母版） */
const cutType = ref<'preview' | 'master'>('preview')
/** 时间线聚合数据 */
const timelineData = ref<TimelineData | null>(null)
/** 片段列表（拖拽时响应式更新 start_ms/end_ms） */
const clips = ref<TimelineClipItem[]>([])
/** 加载中 */
const loading = ref(true)
/** 加载失败文案 */
const loadError = ref('')
/** 是否为「尚未生成时间线」（HTTP 404：应先回生成任务中心，而非反复重试） */
const timelineMissing = ref(false)
/** 保存中 */
const saving = ref(false)
/** 成片预览地址（后端优先返回真实 MP4，其次可播放占位 MP4） */
const previewUrl = ref('')
/** 成片监看 video 元素 */
const monitorVideo = ref<HTMLVideoElement | null>(null)

/** 预览地址是否为可播放 MP4 */
const previewPlayable = computed(() => /\.mp4(\?|#|$)/i.test(previewUrl.value))

/**
 * 拉取成片预览地址；失败时静默降级为关键帧监看。
 * @returns 无
 */
async function loadPreviewUrl(): Promise<void> {
  try {
    const data = await getPostPreviewUrl(projectId.value)
    previewUrl.value = data.url ?? ''
  } catch {
    previewUrl.value = ''
  }
}

/**
 * 成片监看视频加载失败：清空地址并回退关键帧/渐变。
 * @returns 无
 */
function onPreviewVideoError(): void {
  if (!previewUrl.value) return
  previewUrl.value = ''
  toast.warning('成片预览视频加载失败，已回退关键帧监看')
}

/**
 * 加载时间线聚合（clips 空时渲染空态引导；未生成时间线时给出「去生成任务中心」的下一步入口）。
 * @returns 无
 */
async function loadTimeline(): Promise<void> {
  loading.value = true
  loadError.value = ''
  timelineMissing.value = false
  try {
    const data = await getProjectTimeline(projectId.value, cutType.value)
    timelineData.value = data
    clips.value = Array.isArray(data.clips) ? [...data.clips] : []
    ensureSpeakerParams()
    await loadPreviewUrl()
  } catch (error) {
    timelineData.value = null
    clips.value = []
    if (error instanceof ApiError && (error.code === 404 || error.code === 10002)) {
      timelineMissing.value = true
      // 修复（E2E 报告 P-07）：母版页签的时间线由「合成成片」生成，
      // 与镜头视频是否生成无关——此前统一提示「请先完成镜头视频生成」，
      // 在镜头全部渲染成功后仍出现，文案与事实矛盾。
      loadError.value = cutType.value === 'master'
        ? '母版尚未合成：镜头视频均已就绪，请切回「预览版」点击「合成成片 · 母版导出」发起母版合成'
        : '尚未生成时间线：请先完成镜头视频生成，系统会自动编排时间线后再进入后期合成'
    } else {
      loadError.value = error instanceof Error ? error.message : '时间线加载失败'
    }
  } finally {
    loading.value = false
  }
}

/**
 * 切回预览版并重新加载时间线（母版空态「去发起合成」入口）。
 * @returns 无
 */
function backToPreview(): void {
  cutType.value = 'preview'
  void loadTimeline()
}

/** 时间线容器信息 */
const timelineInfo = computed(() => timelineData.value?.timeline ?? null)

/** 合成状态中文标签 */
const timelineStatusLabel = computed(() =>
  timelineInfo.value ? (TIMELINE_STATUS_LABELS[timelineInfo.value.status] ?? timelineInfo.value.status) : '',
)

/** 总时长（毫秒：后端 duration_ms 优先，兜底 clips 最大终点） */
const totalMs = computed(() => {
  if (timelineInfo.value && timelineInfo.value.duration_ms > 0) return timelineInfo.value.duration_ms
  return clips.value.reduce((max, clip) => Math.max(max, clip.end_ms), 0)
})

/**
 * 片段时长（毫秒）。
 * @param clip 片段
 * @returns 时长
 */
function clipDuration(clip: TimelineClipItem): number {
  return Math.max(1, clip.end_ms - clip.start_ms)
}

/**
 * 按轨道类型过滤并按起点排序。
 * @param type 轨道类型
 * @returns 排序后的片段
 */
function clipsOf(type: TimelineTrackType): TimelineClipItem[] {
  return clips.value.filter((clip) => clip.track_type === type).sort((a, b) => a.start_ms - b.start_ms)
}

/* ==================== Konva 时间线布局 ==================== */

/** 标尺行高度（px，DOM 标尺与轨头控制格同高） */
const RULER_H = 24
/** 轨道配置：键 / 原型轨号 / 轨头标签 / 行高 */
const TRACKS: Array<{ key: TimelineTrackType; code: string; label: string; height: number }> = [
  { key: 'video', code: 'V1', label: '视频剪辑轨', height: 72 },
  { key: 'audio', code: 'A1', label: '对白 / 配音轨', height: 56 },
  { key: 'bgm', code: 'A2', label: 'BGM 音乐轨', height: 48 },
  { key: 'subtitle', code: 'T1', label: '双语字幕轨', height: 56 },
]

/** 轨道头内联图标（stroke path，与既有视图内联 SVG 做法同源） */
const TRACK_ICONS: Record<TimelineTrackType, string> = {
  video: 'M4 6h11a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2zm13 4.5 4-2.5v8l-4-2.5',
  audio: 'M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3zM5 11a7 7 0 0 0 14 0M12 18v3',
  bgm: 'M9 18V5l10-2v12M6.5 20.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5zm10-3a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z',
  subtitle: 'M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1zm3 9h4m4 0h3M7 10h10',
}

/** 轨道头图标强调色（对齐 timeline 令牌语义：紫/薄荷/暖金/极青） */
const TRACK_ICON_CLASS: Record<TimelineTrackType, string> = {
  video: 'text-primary-500',
  audio: 'text-success',
  bgm: 'text-warning',
  subtitle: 'text-info',
}

/** 轨道 y 偏移表（track → 距 stage 顶部像素；标尺在 DOM 层，stage 自轨 1 顶起） */
const TRACK_TOP: Record<TimelineTrackType, number> = (() => {
  const map = {} as Record<TimelineTrackType, number>
  let cursor = 0
  for (const track of TRACKS) {
    map[track.key] = cursor
    cursor += track.height
  }
  return map
})()

/** stage 总高度（四轨行高之和） */
const STAGE_H = TRACKS.reduce((sum, track) => sum + track.height, 0)

/** 像素/秒（20~100 可缩放） */
const pps = ref(50)
/** 缩放步长 */
const PPS_STEP = 10
/** 缩放边界 */
const PPS_MIN = 20
const PPS_MAX = 100

/** 时间线滚动容器引用 */
const timelineScroller = ref<HTMLDivElement | null>(null)
/** 容器宽度（响应式测量，stage 至少铺满可视区） */
const containerWidth = ref(960)

/** 时间线像素总宽（内容宽与可视宽取大） */
const stageW = computed(() => Math.max(containerWidth.value, Math.ceil((totalMs.value / 1000) * pps.value) + 80))

/**
 * 毫秒 → 像素 x。
 * @param ms 毫秒
 * @returns 像素坐标
 */
function toPx(ms: number): number {
  return Math.round((ms / 1000) * pps.value)
}

/**
 * 像素 x → 毫秒。
 * @param px 像素坐标
 * @returns 毫秒
 */
function toMs(px: number): number {
  return Math.round((px / pps.value) * 1000)
}

/**
 * 放大 / 缩小时间线（步长 10，边界 20~100）。
 * @param delta 步进（正放大负缩小）
 * @returns 无
 */
function zoomBy(delta: number): void {
  pps.value = Math.min(PPS_MAX, Math.max(PPS_MIN, pps.value + delta))
}

/** 轨道填充 / 文字配色（轨道类型 → 视觉映射；方向 B：全部对齐 canvasTokens，与设计令牌同源） */
const TRACK_COLORS: Record<TimelineTrackType, { fill: string; text: string; accent: string }> = {
  video: TIMELINE.video,
  audio: TIMELINE.audio,
  bgm: TIMELINE.bgm,
  subtitle: TIMELINE.subtitle,
}

/**
 * 片段宽度（像素，最窄 6px 保证可点）。
 * @param clip 片段
 * @returns 宽度
 */
function clipW(clip: TimelineClipItem): number {
  return Math.max(6, toPx(clipDuration(clip)))
}

/**
 * 片段高度（轨道行高减内边距）。
 * @param clip 片段
 * @returns 高度
 */
function clipH(clip: TimelineClipItem): number {
  const track = TRACKS.find((t) => t.key === clip.track_type)
  return (track?.height ?? 56) - 8
}

/**
 * 片段展示文本：视频显示 #镜号，字幕显示内容截断，其余显示 label 或曲目名。
 * @param clip 片段
 * @returns 文本
 */
function clipText(clip: TimelineClipItem): string {
  if (clip.track_type === 'video') {
    return `#${clip.shot_no ?? clip.source_id ?? clip.id}`
  }
  const text = clip.content || clip.label || (clip.track_type === 'bgm' ? 'BGM' : TRACK_TYPE_LABELS[clip.track_type])
  const maxChars = Math.max(4, Math.floor(clipW(clip) / 13))
  return text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text
}

/**
 * 片段波形示意折线（装饰性：由镜号/源 ID 哈希生成确定性起伏，非真实音频采样，
 * 与原型 SVG 波形同性质）。
 * @param clip 片段
 * @param width 波形宽（px）
 * @param height 波形高（px）
 * @returns Konva line points（相对 clip 原点的 x0 y0 x1 y1…序列）
 */
function wavePoints(clip: TimelineClipItem, width: number, height: number): number[] {
  const seed = Number(clip.shot_no ?? clip.source_id ?? clip.id) || 1
  const points: number[] = [0, height / 2]
  const step = 8
  for (let x = step; x <= width; x += step) {
    const amp = height * (0.18 + 0.3 * Math.abs(Math.sin(x * 0.11 + seed)))
    points.push(x, height / 2 + Math.sin(x * 0.42 + seed * 1.7) * amp)
  }
  return points
}

/** 时间刻度序列（15s 主刻度 + 终点刻度，对齐原型标尺节奏） */
const rulerTicks = computed(() => {
  const ticks: Array<{ second: number; x: number; final: boolean }> = []
  const totalSeconds = Math.floor(totalMs.value / 1000)
  for (let second = 0; second <= totalSeconds; second += 15) {
    ticks.push({ second, x: toPx(second * 1000), final: false })
  }
  if (totalMs.value > 0) {
    const endSecond = Math.ceil(totalMs.value / 1000)
    if (endSecond % 15 !== 0) {
      ticks.push({ second: endSecond, x: toPx(totalMs.value), final: true })
    }
  }
  return ticks
})

/** 选中片段 ID */
const selectedClipId = ref<string | number | null>(null)

/** 选中片段对象 */
const selectedClip = computed(() =>
  selectedClipId.value === null ? null : clips.value.find((clip) => String(clip.id) === String(selectedClipId.value)) ?? null,
)

/**
 * 是否可拖拽（仅字幕 / BGM 片段；视频 / 配音由后端编排锁定）。
 * @param clip 片段
 * @returns 是否可拖拽
 */
function isDraggable(clip: TimelineClipItem): boolean {
  return clip.track_type === 'subtitle' || clip.track_type === 'bgm'
}

/**
 * 选中片段（点击 clip）。
 * @param clip 片段
 * @returns 无
 */
function selectClip(clip: TimelineClipItem): void {
  selectedClipId.value = clip.id
}

/**
 * 点击 stage 空白（背景 Rect）取消选中。
 * @returns 无
 */
function onStageBackgroundClick(): void {
  selectedClipId.value = null
}

/* ==================== 拖拽与吸附 ==================== */

/** 吸附阈值（px） */
const SNAP_PX = 12
/** 吸附竖线 x（null 隐藏） */
const snapLineX = ref<number | null>(null)

/**
 * 计算吸附 x：邻近 clip 边缘与整秒刻度，阈值 12px。
 * @param x 当前像素位置
 * @param dragId 拖拽中片段 ID（排除自身）
 * @returns 吸附后像素位置（无吸附候选为 null）
 */
function findSnapX(x: number, dragId: string | number): number | null {
  let best: number | null = null
  let bestDist = SNAP_PX
  for (const clip of clips.value) {
    if (String(clip.id) === String(dragId)) continue
    for (const edge of [clip.start_ms, clip.end_ms]) {
      const candidate = toPx(edge)
      const dist = Math.abs(x - candidate)
      if (dist < bestDist) {
        bestDist = dist
        best = candidate
      }
    }
  }
  const second = Math.round(x / pps.value)
  for (const offset of [-1, 0, 1]) {
    const candidate = (second + offset) * pps.value
    if (candidate < 0) continue
    const dist = Math.abs(x - candidate)
    if (dist < bestDist) {
      bestDist = dist
      best = candidate
    }
  }
  return best
}

/** Konva 拖拽事件（target 节点提供 x/y 坐标读写方法） */
type KonvaDragEvent = { target: { x: (value?: number) => number; y: (value?: number) => number } }

/**
 * 生成片段拖拽中处理器（Konva config 事件绑定用，避免模板内联参数失去类型）。
 * @param clip 拖拽片段
 * @returns 拖拽中事件处理器
 */
function clipDragMoveHandler(clip: TimelineClipItem): (event: KonvaDragEvent) => void {
  return (event) => onClipDragMove(clip, event)
}

/**
 * 拖拽中：锁定 y、应用吸附并响应式平移片段（config 与节点保持一致防回弹）。
 * @param clip 拖拽片段
 * @param event Konva 拖拽事件（target 提供 getter/setter 坐标方法）
 * @returns 无
 */
function onClipDragMove(clip: TimelineClipItem, event: KonvaDragEvent): void {
  const node = event.target
  node.y(TRACK_TOP[clip.track_type])
  let x = Math.max(0, node.x())
  const snapped = findSnapX(x, clip.id)
  snapLineX.value = snapped
  if (snapped !== null) x = snapped
  node.x(x)
  // 平移量写入本地状态（end 随起点平移，时长不变）
  const duration = clipDuration(clip)
  clip.start_ms = Math.max(0, toMs(x))
  clip.end_ms = clip.start_ms + duration
}

/**
 * 拖拽结束：隐藏吸附线并 PUT 全量提交（失败自动回滚重载；位置已在 dragmove 写入本地状态）。
 * @returns 无
 */
function onClipDragEnd(): void {
  snapLineX.value = null
  void persistTimeline()
}

/**
 * 保存时间线（clips 全量提交；失败重载回滚）。
 * @returns 无
 */
async function persistTimeline(): Promise<void> {
  saving.value = true
  try {
    await saveProjectTimeline(projectId.value, {
      clips: clips.value.map((clip) => ({ ...clip })),
    })
    toast.success('时间线已保存')
  } catch {
    // 拦截器已 toast；重新加载回滚到服务端版本
    await loadTimeline()
  } finally {
    saving.value = false
  }
}

/* ==================== 音画同步提示 ==================== */

/** 音画偏差条目 */
interface SyncWarning {
  /** 视频片段标签 */
  videoLabel: string
  /** 音频片段标签 */
  audioLabel: string
  /** 起点偏差（毫秒） */
  diffMs: number
}

/**
 * 读取片段 content 中携带的镜头 ID。
 * 后端装配 video / audio 轨时会把 shot_id 写入 content，这里作为前后端同口径的配对键；
 * content 为字幕纯文本或缺少 shot_id 时返回 null。
 * @param clip 时间线片段
 * @returns 镜头 ID；缺失或非法时返回 null
 */
function clipShotId(clip: TimelineClipItem): number | null {
  const raw = clip.content as unknown
  if (!raw || typeof raw !== 'object') return null
  const value = (raw as Record<string, unknown>).shot_id
  const shotId = Number(value)
  return Number.isFinite(shotId) && shotId > 0 ? shotId : null
}

/** 音画同步容差（毫秒，与后端 70001 QC 的 AV_SYNC_TOLERANCE_MS 同值） */
const SYNC_TOLERANCE_MS = 100

/**
 * audio 轨上的配音片段（source_type=voice_clip，旧数据兜底 tts）。
 * audio 轨还承载 SFX 音效（source_type=media_file）等片段，配音句计数与
 * 音画同步配对都应以配音片段为准——否则 SFX 会被误计为配音句（实测
 * 26 = 15 配音 + 11 SFX），且 SFX 触发点不在镜头起点，混入配对会产生
 * 虚假音画偏差告警。
 */
const voiceClips = computed<TimelineClipItem[]>(() =>
  clipsOf('audio').filter((clip) => {
    const sourceType = clip.source_type ?? ''
    return sourceType === 'voice_clip' || sourceType === 'tts'
  }),
)

/** 音画同步配对（video 与配音 audio 按 shot_id 一一对应，与后端 70001 校验口径一致） */
interface SyncedPair {
  /** 视频片段 */
  video: TimelineClipItem
  /** 音频片段 */
  audio: TimelineClipItem
  /** 起点偏差（毫秒，窗口口径：0 表示落在镜头窗口内） */
  diffMs: number
}

/**
 * 音画偏差计算（与后端 70001 QC 同口径的窗口判定）。
 * 同一镜头可能承载多句配音，第二句起点在镜头内顺延（不等于镜头起点），
 * 绝对差口径会产生 3536ms 之类的虚假告警而后端 QC 通过。这里改为：
 * 音频起点落在所属镜头窗口 [video.start_ms - 100, video.end_ms + 100]
 * 内视为对齐（偏差 0）；窗口外取到窗口最近边界的距离。
 * @param video 视频片段（定义镜头窗口）
 * @param audio 配音片段
 * @returns 偏差毫秒数（0 表示窗口内对齐）
 */
function syncDiffMs(video: TimelineClipItem, audio: TimelineClipItem): number {
  if (
    audio.start_ms >= video.start_ms - SYNC_TOLERANCE_MS &&
    audio.start_ms <= video.end_ms + SYNC_TOLERANCE_MS
  ) {
    return 0
  }
  return audio.start_ms < video.start_ms
    ? video.start_ms - audio.start_ms
    : audio.start_ms - video.end_ms
}

/**
 * 音画同步配对列表。
 * 音频侧只取配音片段（voiceClips，排除 SFX 音效）。优先按 content.shot_id
 * 精确配对：镜头无对白时音频片段缺失，纯顺序配对会整体错位，从而产生
 * 17000ms / 22000ms 之类的虚假偏差。两轨均无 shot_id 元数据时回退顺序配对
 * （兼容旧数据）。偏差按 syncDiffMs 窗口口径计算，与后端 QC 一致。
 */
const syncPairs = computed<SyncedPair[]>(() => {
  const videos = clipsOf('video')
  const audios = voiceClips.value
  const videoByShot = new Map<number, TimelineClipItem>()
  for (const video of videos) {
    const shotId = clipShotId(video)
    if (shotId !== null) videoByShot.set(shotId, video)
  }
  const pairs: SyncedPair[] = []
  if (videoByShot.size > 0) {
    for (const audio of audios) {
      const shotId = clipShotId(audio)
      const video = shotId === null ? undefined : videoByShot.get(shotId)
      if (!video) continue
      pairs.push({ video, audio, diffMs: syncDiffMs(video, audio) })
    }
    return pairs
  }
  const count = Math.min(videos.length, audios.length)
  for (let index = 0; index < count; index += 1) {
    pairs.push({
      video: videos[index],
      audio: audios[index],
      diffMs: syncDiffMs(videos[index], audios[index]),
    })
  }
  return pairs
})

/** 音画偏差列表（偏差 >100ms 的配对） */
const syncWarnings = computed<SyncWarning[]>(() =>
  syncPairs.value
    .filter((pair) => pair.diffMs > 100)
    .map((pair) => ({
      videoLabel: `#${pair.video.shot_no ?? pair.video.source_id ?? pair.video.id}`,
      audioLabel: pair.audio.label ?? `配音 ${pair.audio.order_index ?? pair.audio.id}`,
      diffMs: pair.diffMs,
    })),
)

/** 音画同步最大偏差（毫秒，全部配对实测值；HITL 关口四质检药丸数据源） */
const maxSyncDiffMs = computed(() => syncPairs.value.reduce((max, pair) => Math.max(max, pair.diffMs), 0))

/** 存在偏差的音频片段 ID 集合（时间线上黄描边） */
const warnedAudioIds = computed(() => {
  const ids = new Set<string>()
  for (const pair of syncPairs.value) {
    if (pair.diffMs > 100) ids.add(String(pair.audio.id))
  }
  return ids
})

/* ==================== 播放引擎 ==================== */

/** 是否播放中 */
const playing = ref(false)
/** 播放指针（毫秒） */
const playheadMs = ref(0)
/** 播放定时器 */
let playTimer: ReturnType<typeof setInterval> | null = null

/** A-B 循环入点（null 未打点） */
const loopA = ref<number | null>(null)
/** A-B 循环出点（null 未打点） */
const loopB = ref<number | null>(null)

/** A-B 循环按钮提示文案 */
const abLoopTitle = computed(() => {
  if (loopA.value === null) return 'A-B 循环：点击打点 A'
  if (loopB.value === null) return `A-B 循环：A ${timecode(loopA.value)}，再点击打点 B`
  return `A-B 循环：${timecode(loopA.value)} ↔ ${timecode(loopB.value)}（再点击清除）`
})

/**
 * 切换 A-B 循环（三态：打点 A → 打点 B 并循环 → 清除）。
 * @returns 无
 */
function toggleAbLoop(): void {
  if (loopA.value === null) {
    loopA.value = playheadMs.value
    return
  }
  if (loopB.value === null) {
    loopB.value = Math.max(loopA.value + 500, playheadMs.value)
    toast.success(`A-B 循环已设定：${timecode(loopA.value)} ↔ ${timecode(loopB.value)}`)
    return
  }
  loopA.value = null
  loopB.value = null
}

/**
 * 播放 tick：有视频时以视频时间驱动指针，否则按 100ms 步进。
 * @returns 无
 */
function tick(): void {
  const video = monitorVideo.value
  if (video && !video.paused) {
    playheadMs.value = Math.round(video.currentTime * 1000)
    if (video.ended || playheadMs.value >= totalMs.value) {
      playheadMs.value = totalMs.value
      pause()
    }
    return
  }
  if (loopA.value !== null && loopB.value !== null && playheadMs.value + 100 >= loopB.value) {
    playheadMs.value = loopA.value
    return
  }
  if (playheadMs.value + 100 >= totalMs.value) {
    playheadMs.value = totalMs.value
    pause()
    return
  }
  playheadMs.value += 100
}

/**
 * 开始播放：视频监看可播时同步驱动 video，失败给出提示。
 * @returns 无
 */
function play(): void {
  if (playing.value || totalMs.value <= 0) return
  if (playheadMs.value >= totalMs.value) playheadMs.value = 0
  playing.value = true
  const video = monitorVideo.value
  if (video) {
    video.currentTime = playheadMs.value / 1000
    void video.play().catch(() => {
      toast.warning('成片预览播放失败，请检查预览地址')
    })
  }
  playTimer = setInterval(tick, 100)
}

/**
 * 暂停播放（同时暂停成片监看视频）。
 * @returns 无
 */
function pause(): void {
  playing.value = false
  monitorVideo.value?.pause()
  if (playTimer) {
    clearInterval(playTimer)
    playTimer = null
  }
}

/**
 * 逐帧步进（帧长按时间线 fps，缺省 24fps）。
 * @param delta 帧数（正进负退）
 * @returns 无
 */
function stepFrame(delta: number): void {
  const frameMs = 1000 / Math.max(1, timelineInfo.value?.fps ?? 24)
  playheadMs.value = Math.min(totalMs.value, Math.max(0, playheadMs.value + delta * frameMs))
}

/* ==================== 标尺定位（点击 / 拖拽进度条） ==================== */

/** 是否正在标尺上拖拽定位播放头 */
const scrubbing = ref(false)

/**
 * 定位播放头：夹取到 [0, 总时长] 并同步成片监看视频的播放位置。
 * @param ms 目标位置（毫秒）
 * @returns 无
 */
function seekTo(ms: number): void {
  const clamped = Math.min(totalMs.value, Math.max(0, Math.round(ms)))
  playheadMs.value = clamped
  const video = monitorVideo.value
  if (video && Number.isFinite(video.duration)) {
    try {
      video.currentTime = clamped / 1000
    } catch {
      // 视频元数据尚未就绪时忽略，开始播放时会再次同步
    }
  }
}

/**
 * 按指针横坐标换算播放位置（以标尺元素自身为坐标原点，自动兼容横向滚动）。
 * @param event 指针事件
 * @returns 无
 */
function seekFromPointerEvent(event: PointerEvent): void {
  const track = event.currentTarget as HTMLElement
  const rect = track.getBoundingClientRect()
  seekTo(toMs(event.clientX - rect.left))
}

/**
 * 标尺按下：捕获指针并立即定位（单击即跳转）。
 * @param event 指针事件（button 非 0 时不处理）
 * @returns 无
 */
function onRulerPointerDown(event: PointerEvent): void {
  if (event.button !== 0) return
  const track = event.currentTarget as HTMLElement
  scrubbing.value = true
  track.setPointerCapture?.(event.pointerId)
  seekFromPointerEvent(event)
}

/**
 * 标尺拖拽中：指针移动时持续定位（支持拖出标尺区域）。
 * @param event 指针事件
 * @returns 无
 */
function onRulerPointerMove(event: PointerEvent): void {
  if (!scrubbing.value) return
  seekFromPointerEvent(event)
}

/**
 * 标尺松开 / 指针取消：结束拖拽并释放指针捕获。
 * @param event 指针事件
 * @returns 无
 */
function onRulerPointerUp(event: PointerEvent): void {
  scrubbing.value = false
  const track = event.currentTarget as HTMLElement
  if (track.hasPointerCapture?.(event.pointerId)) track.releasePointerCapture(event.pointerId)
}

/**
 * 标尺键盘定位：←/→ 逐帧，Shift + ←/→ 步进 10 帧。
 * @param event 键盘事件
 * @returns 无
 */
function onRulerKeydown(event: KeyboardEvent): void {
  if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
  const frameMs = 1000 / Math.max(1, timelineInfo.value?.fps ?? 24)
  const step = (event.shiftKey ? 10 : 1) * frameMs
  seekTo(playheadMs.value + (event.key === 'ArrowRight' ? step : -step))
  event.preventDefault()
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

/** 当前指针所在视频片段（预览窗画面） */
const currentVideoClip = computed(() => clipsOf('video').find((clip) => playheadMs.value >= clip.start_ms && playheadMs.value < clip.end_ms) ?? null)

/** 当前指针所在字幕片段（预览窗字幕） */
const currentSubtitle = computed(() => clipsOf('subtitle').find((clip) => playheadMs.value >= clip.start_ms && playheadMs.value < clip.end_ms)?.content ?? '')

/** 当前指针所在配音片段（预览窗角色条，label 为角色名） */
const currentAudioClip = computed(() => clipsOf('audio').find((clip) => playheadMs.value >= clip.start_ms && playheadMs.value < clip.end_ms) ?? null)

/** 视频块色相（无缩略图时按镜号哈希生成占位渐变） */
function clipHue(clip: TimelineClipItem): number {
  const seed = Number(clip.shot_no ?? clip.source_id ?? clip.id) || 0
  return (seed * 47) % 360
}

/** 监听音量（0~100，仅本地监听增益 UI 态） */
const previewVolume = ref(50)

/** 监听音量 dB 读数（20·log10 增益换算，50% = -6dB） */
const previewDb = computed(() => (20 * Math.log10(Math.max(previewVolume.value, 1) / 100)).toFixed(1))

/** 预览舞台引用（全屏监看宿主） */
const previewStage = ref<HTMLDivElement | null>(null)

/**
 * 切换预览舞台全屏监看。
 * @returns 无
 */
async function toggleFullscreen(): Promise<void> {
  const el = previewStage.value
  if (!el) return
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen()
    } else {
      await el.requestFullscreen()
    }
  } catch {
    // 浏览器拒绝全屏（非用户手势等）时静默
  }
}

// 卸载暂停播放
onBeforeUnmount(pause)

/* ==================== TTS 卡 ==================== */

/** 样音试听请求中 */
const ttsPreviewing = ref(false)
/** 样音数据 */
const ttsPreview = ref<{ audio_url: string; duration_ms: number } | null>(null)
/** 样音 audio 元素 */
const ttsAudio = ref<HTMLAudioElement | null>(null)
/** 全剧配音请求中 */
const ttsStarting = ref(false)
/** 当前触发试听的角色名（行级 loading 定位） */
const activeSpeaker = ref('')

/**
 * 生成 30s 样音并自动试听（样音接口为项目级，角色行卡「试听该角色」复用此能力）。
 * @returns 无
 */
async function handleTtsPreview(): Promise<void> {
  ttsPreviewing.value = true
  try {
    const data = await previewTts(projectId.value)
    ttsPreview.value = { audio_url: data.audio_url, duration_ms: data.duration_ms }
    // 下一帧挂载 audio 后自动播放
    requestAnimationFrame(() => {
      void ttsAudio.value?.play().catch(() => undefined)
    })
  } catch {
    // 拦截器已 toast
  } finally {
    ttsPreviewing.value = false
  }
}

/**
 * 角色行卡触发样音试听（记录触发行用于行级 loading）。
 * @param name 角色名
 * @returns 无
 */
async function handleSpeakerPreview(name: string): Promise<void> {
  activeSpeaker.value = name
  try {
    await handleTtsPreview()
  } finally {
    activeSpeaker.value = ''
  }
}

/**
 * 发起全剧配音（202 受理）。
 * @returns 无
 */
async function handleFullTts(): Promise<void> {
  ttsStarting.value = true
  try {
    await startFullTts(projectId.value)
    toast.success('全剧配音任务已受理，完成后字幕轨将同步更新')
  } catch {
    // 拦截器已 toast
  } finally {
    ttsStarting.value = false
  }
}

/* ==================== 字幕样式卡 ==================== */

/** 字幕样式列表 */
const subtitleStyles = ref<SubtitleStyleItem[]>([])
/** 样式加载中 */
const stylesLoading = ref(false)
/** 样式加载失败（后端未就绪时下拉禁用） */
const stylesError = ref(false)
/** 选中样式 ID */
const selectedStyleId = ref<number | string>('')
/** 应用中 */
const applyingStyle = ref(false)
/** 刊头「字幕样式（九宫格）」下拉是否展开 */
const stylesMenuOpen = ref(false)

/** 当前选中样式名（字幕轨头徽标数据源） */
const selectedStyleName = computed(
  () => subtitleStyles.value.find((style) => String(style.id) === String(selectedStyleId.value))?.name ?? '',
)

/**
 * 加载字幕样式库。
 * @returns 无
 */
async function loadStyles(): Promise<void> {
  stylesLoading.value = true
  stylesError.value = false
  try {
    subtitleStyles.value = await listSubtitleStyles()
    const defaultStyle = subtitleStyles.value.find((style) => style.is_default)
    selectedStyleId.value = defaultStyle ? String(defaultStyle.id) : subtitleStyles.value[0] ? String(subtitleStyles.value[0].id) : ''
  } catch {
    subtitleStyles.value = []
    stylesError.value = true
  } finally {
    stylesLoading.value = false
  }
}

/**
 * 应用字幕样式（后端重排字幕轨）。
 * @returns 无
 */
async function handleApplyStyle(): Promise<void> {
  applyingStyle.value = true
  try {
    await applySubtitleStyle(projectId.value, selectedStyleId.value || undefined)
    toast.success('字幕样式已应用')
    stylesMenuOpen.value = false
    await loadTimeline()
  } catch {
    // 拦截器已 toast
  } finally {
    applyingStyle.value = false
  }
}

/* ==================== BGM 卡 ==================== */

/** BGM 情绪选项（推荐查询参数） */
const BGM_EMOTION_OPTIONS: Array<{ value: string; label: string }> = [
  { value: 'warm', label: '温馨' },
  { value: 'tense', label: '紧张' },
  { value: 'joyful', label: '欢快' },
  { value: 'sad', label: '悲伤' },
  { value: 'epic', label: '恢弘' },
  { value: 'suspense', label: '悬疑' },
]

/** 选中情绪 */
const bgmEmotion = ref('warm')
/** 推荐列表 */
const bgmList = ref<BgmTrackItem[]>([])
/** 推荐加载中 */
const bgmLoading = ref(false)
/** 已选曲目 ID */
const bgmTrackId = ref<number | string>('')
/** 音量（dB，-30~0，对白基准 -6） */
const bgmVolume = ref(-6)
/** 侧链压限 */
const bgmSidechain = ref(true)
/** 应用中 */
const bgmApplying = ref(false)

/** 声音工作台页签（1 逐角色 TTS / 2 BGM 情绪卡点） */
const soundTab = ref<1 | 2>(1)

/** 当前应用 BGM（timeline 聚合附带；空则回落 BGM 片段 label） */
const currentBgm = computed(() => timelineData.value?.bgm ?? null)

/** 底部 BGM 行标题（当前应用曲目 → BGM 片段 label → 未设置） */
const bgmTitle = computed(() => {
  if (currentBgm.value) return currentBgm.value.title
  const bgmClip = clipsOf('bgm')[0]
  return bgmClip?.label || bgmClip?.content || '未设置 BGM'
})

/** BGM 授权说明（license_type → 中文；无数据时明示待确认） */
const bgmLicenseLabel = computed(() => {
  const license = currentBgm.value?.license_type
  if (license === 'self') return '自有版权商用授权'
  if (license === 'third_party') return '第三方正版商用授权'
  return '授权信息待确认'
})

/**
 * 拉取情绪推荐 BGM 列表。
 * @returns 无
 */
async function fetchBgmRecommend(): Promise<void> {
  bgmLoading.value = true
  bgmList.value = []
  try {
    bgmList.value = await recommendBgm(projectId.value, bgmEmotion.value)
  } catch {
    // 拦截器已 toast（后端未就绪时空列表兜底）
  } finally {
    bgmLoading.value = false
  }
}

/**
 * 应用 BGM（曲目 + 音量 + 侧链）。
 * @returns 无
 */
async function handleApplyBgm(): Promise<void> {
  if (!bgmTrackId.value) return
  bgmApplying.value = true
  try {
    await updateBgm(projectId.value, {
      bgm_track_id: bgmTrackId.value,
      volume_db: bgmVolume.value,
      sidechain: bgmSidechain.value,
    })
    toast.success('BGM 已应用到时间线')
    await loadTimeline()
  } catch {
    // 拦截器已 toast
  } finally {
    bgmApplying.value = false
  }
}

/* ==================== 逐角色行卡（聚合自配音片段） ==================== */

/** 角色行卡（由配音片段 label 聚合，label 即角色名） */
interface SpeakerRow {
  /** 角色名 */
  name: string
  /** 该角色配音句数 */
  lineCount: number
  /** 头像字徽（名称首字） */
  initial: string
  /** 语义强调档（按序轮转 紫/红/绿/青/金，对齐原型角色独立强调色） */
  tone: SpeakerTone
}

/** 角色微调参数（本地 UI 态：TTS 参数暂无下发端点，仅作行内结构件） */
interface SpeakerParam {
  /** 语速（80~150 → 0.80x~1.50x） */
  rate: number
  /** 音调（-6~6 Semitone） */
  pitch: number
  /** 情绪强度（0~100） */
  emotion: number
}

/** 角色强调档（全部取自语义令牌色，不引入新色值） */
type SpeakerTone = 'brand' | 'rose' | 'mint' | 'cyan' | 'gold'

/** 强调档 → 类名映射（头像字徽 / 角色徽标 / 强调文字 / 卡片 hover 描边 / 实底按钮） */
const TONE_CLASS: Record<SpeakerTone, { avatar: string; badge: string; accent: string; hover: string; button: string }> = {
  brand: {
    avatar: 'border-primary-200 bg-primary-100 text-primary-700',
    badge: 'gd-fill-brand',
    accent: 'text-primary-700',
    hover: 'hover:border-primary-200',
    button: 'bg-primary-100 text-primary-700 hover:bg-primary-200',
  },
  rose: {
    avatar: 'border-danger-border bg-danger-bg text-danger-strong',
    badge: 'gd-fill-rose',
    accent: 'text-danger-strong',
    hover: 'hover:border-danger-border',
    button: 'bg-danger-bg text-danger-strong hover:bg-danger-border',
  },
  mint: {
    avatar: 'border-success-border bg-success-bg text-success-strong',
    badge: 'gd-fill-mint',
    accent: 'text-success-strong',
    hover: 'hover:border-success-border',
    button: 'bg-success-bg text-success-strong hover:bg-success-border',
  },
  cyan: {
    avatar: 'border-info-border bg-info-bg text-info-strong',
    badge: 'gd-fill-cyan',
    accent: 'text-info-strong',
    hover: 'hover:border-info-border',
    button: 'bg-info-bg text-info-strong hover:bg-info-border',
  },
  gold: {
    avatar: 'border-warning-border bg-warning-bg text-warning-strong',
    badge: 'gd-fill-amber',
    accent: 'text-warning-strong',
    hover: 'hover:border-warning-border',
    button: 'bg-warning-bg text-warning-strong hover:bg-warning-border',
  },
}

/** 角色行卡（配音片段按 label 聚合，最多展示 6 位，超出以句数计入首位可后续分页） */
const speakerRows = computed<SpeakerRow[]>(() => {
  const order: string[] = []
  const counts = new Map<string, number>()
  for (const clip of clipsOf('audio')) {
    const name = (clip.label ?? '').trim() || '未标注角色'
    if (!counts.has(name)) {
      counts.set(name, 0)
      order.push(name)
    }
    counts.set(name, (counts.get(name) ?? 0) + 1)
  }
  const tones: Array<SpeakerTone> = ['brand', 'rose', 'mint', 'cyan', 'gold']
  return order.slice(0, 6).map((name, index) => ({
    name,
    lineCount: counts.get(name) ?? 0,
    initial: name.slice(0, 1),
    tone: tones[index % tones.length],
  }))
})

/** 角色微调参数表（key = 角色名） */
const speakerParams = ref<Record<string, SpeakerParam>>({})

/**
 * 确保每位角色都有微调参数（loadTimeline 成功后调用；缺省 1.00x / 0 / 75%）。
 * @returns 无
 */
function ensureSpeakerParams(): void {
  for (const row of speakerRows.value) {
    if (!speakerParams.value[row.name]) {
      speakerParams.value[row.name] = { rate: 100, pitch: 0, emotion: 75 }
    }
  }
}

/**
 * 语速值 → 展示文案（80~150 → 0.80x~1.50x）。
 * @param value 语速档位
 * @returns 倍速文案
 */
function formatRate(value: number): string {
  return `${(value / 100).toFixed(2)}x`
}

/**
 * 音调值 → 展示文案（-6~6 → ±N Semitone）。
 * @param value 音调半音
 * @returns 半音文案
 */
function formatPitch(value: number): string {
  return `${value > 0 ? '+' : ''}${value} Semitone`
}

/* ==================== 结构锚点 / 汇总 ==================== */

/** 黄金卡点爆点（毫秒）：原型以结尾悬念截断为锚（结尾前 4s），待真实卡点数据接入后替换 */
const hookMs = computed(() => Math.max(0, totalMs.value - 4000))

/** 雾带贯穿播放头横向位置（百分比） */
const playheadPct = computed(() => (totalMs.value > 0 ? `${Math.min(100, (playheadMs.value / totalMs.value) * 100).toFixed(2)}%` : '0%'))

/** 字幕轨内容就绪条数（✓ 角标数据源：有 content 即视为文本就绪） */
const subtitleReadyCount = computed(() => clipsOf('subtitle').filter((clip) => Boolean(clip.content)).length)

/* ==================== 合成 ==================== */

/**
 * 打开合成确认弹窗前先校验配音覆盖，避免用户把“只有字幕”的无声成片提交合成。
 *
 * 有台词但缺少真实配音（未生成 / 失败 / 沙箱占位）时直接 toast 拦截；
 * 覆盖查询本身失败时仍放行后续后端 70005 兜底，不因网络问题阻断正常流程。
 *
 * @returns 无
 */
async function handleOpenCompose(): Promise<void> {
  if (composeChecking.value || clips.value.length === 0) return
  composeChecking.value = true
  try {
    const coverage = await getVoiceCoverage(projectId.value)
    voiceCoverage.value = coverage
    const missing = coverage.missing_shots || []
    const placeholders = coverage.placeholder_lines || []
    if (missing.length > 0 || placeholders.length > 0) {
      const shotNos = missing
        .map((item) => item.shot_no || item.shot_id)
        .filter(Boolean)
        .join('、')
      const reason = placeholders.length > 0 ? '存在沙箱占位配音（非真实人声）' : '有台词但缺少有效配音'
      toast.error(`${reason}：镜头 ${shotNos || '未知'}。请先在配音轨生成/重配后再合成。`)
      return
    }
    composeDialogOpen.value = true
  } catch {
    // 覆盖查询失败时不阻断：真正的合成门禁在后端（70005）
    voiceCoverage.value = null
    composeDialogOpen.value = true
  } finally {
    composeChecking.value = false
  }
}

/** 合成确认弹窗 */
const composeDialogOpen = ref(false)
/** 合成前配音覆盖检查中 */
const composeChecking = ref(false)
/** 最近一次配音覆盖统计（弹窗内展示） */
const voiceCoverage = ref<VoiceCoverageData | null>(null)
/** 合成请求中 */
const composing = ref(false)
/** 合成轮询定时器 */
let composePollTimer: ReturnType<typeof setInterval> | null = null
/** 合成轮询剩余次数 */
let composePollLeft = 0

/** 合成确认弹窗正文（按最近一次配音覆盖结果动态展示） */
const composeDialogContent = computed(() => {
  const coverage = voiceCoverage.value
  const voiceLine = coverage && coverage.dialogue_shots > 0
    ? `配音覆盖：${coverage.voiced_shots}/${coverage.dialogue_shots} 个台词镜头已生成真实配音。`
    : ''
  // 配音超镜头原时长 1.5 倍：镜头会被定格补帧自动拉长，这里只做提示不阻断。
  const overlong = coverage?.overlong_shots ?? []
  const overlongLine = overlong.length > 0
    ? `注意：镜头 ${overlong.map((item) => item.shot_no || item.shot_id).join('、')} 的配音超过原时长 1.5 倍，合成时会自动拉长镜头（定格补帧）。`
    : ''
  return `${voiceLine}${overlongLine}合成将按当前时间线渲染母版视频（含转场 / 混音 / 字幕烧录），完成后进入成片确认（HITL-4）。有台词但无有效配音时后端会拒绝合成（70005）。`
})

/**
 * 发起合成（POST compose 202 → 轮询 timeline 状态至 pending_review）。
 * @returns 无
 */
async function handleCompose(): Promise<void> {
  composing.value = true
  try {
    await composeFinal(projectId.value)
    composeDialogOpen.value = false
    toast.success('合成任务已受理，正在后台合成…')
    composePollLeft = 60
    if (!composePollTimer) {
      composePollTimer = setInterval(() => {
        composePollLeft -= 1
        void getProjectTimeline(projectId.value, cutType.value)
          .then((data) => {
            timelineData.value = data
            const status = data.timeline?.status
            if ((status === 'pending_review' || status === 'approved' || status === 'failed' || composePollLeft <= 0) && composePollTimer) {
              clearInterval(composePollTimer)
              composePollTimer = null
              if (status === 'pending_review') {
                toast.success('合成完成，正在前往成片确认（HITL-4）')
                void router.push(`/projects/${projectId.value}/post/review`)
              } else if (status === 'approved') {
                toast.success('合成完成，该成片此前已确认通过')
              } else if (status === 'failed') {
                toast.error('合成失败，请检查时间线后重试')
              }
            }
          })
          .catch(() => undefined)
      }, 3_000)
    }
  } catch {
    // 拦截器已 toast（40001 状态不允许 / 70001 音画同步拦截等）
  } finally {
    composing.value = false
  }
}

/* ==================== 生命周期 ==================== */

/**
 * 测量时间线容器宽度（stage 至少铺满可视区）。
 * @returns 无
 */
function measureContainer(): void {
  if (timelineScroller.value) {
    containerWidth.value = timelineScroller.value.clientWidth
  }
}

// 挂载：加载时间线 + 样式库 + BGM 推荐 + 容器测量
onMounted(() => {
  measureContainer()
  window.addEventListener('resize', measureContainer)
  void loadTimeline()
  void loadStyles()
  void fetchBgmRecommend()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', measureContainer)
  if (composePollTimer) {
    clearInterval(composePollTimer)
    composePollTimer = null
  }
})
</script>

<template>
  <div class="flex h-full min-h-0 flex-col overflow-hidden">
    <!-- ============ 刊头行（原型 §1：集名 meta + 右侧状态/动作带） ============ -->
    <header class="shrink-0 border-b border-hairline bg-surface-raised px-5 py-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <!-- 左：标题 + 一行上下文 + 版本切换（版本决定监看与导出，跟随标题） -->
        <div class="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
          <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true" />
          <div class="min-w-0">
            <h1 class="gd-masthead truncate text-display-lg">后期合成与多轨时间线</h1>
            <p class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-caption text-ink-mute">
              <span class="gd-num">{{ clipsOf('video').length }} 镜头</span>
              <span aria-hidden="true">·</span>
              <span class="gd-num">{{ timecode(totalMs) }}</span>
              <span aria-hidden="true">·</span>
              <span class="gd-num">{{ TRACKS.length }} 轨混流</span>
              <span aria-hidden="true">·</span>
              <span :class="timelineInfo?.status === 'pending_review' ? 'font-medium text-primary-700' : ''">{{ timelineInfo ? timelineStatusLabel : '成片试看与终审' }}</span>
            </p>
          </div>
          <!-- cut_type 切换（既有功能，并入标题组） -->
          <div class="flex overflow-hidden rounded-md border border-hairline" role="group" aria-label="版本切换">
            <button
              type="button"
              class="px-2.5 py-1 text-caption transition-colors"
              :class="cutType === 'preview' ? 'gd-fill-brand' : 'bg-surface-raised text-ink-mute hover:bg-state-hover'"
              @click="cutType = 'preview'; loadTimeline()"
            >
              预览版
            </button>
            <button
              type="button"
              class="px-2.5 py-1 text-caption transition-colors"
              :class="cutType === 'master' ? 'gd-fill-brand' : 'bg-surface-raised text-ink-mute hover:bg-state-hover'"
              @click="cutType = 'master'; loadTimeline()"
            >
              母版
            </button>
          </div>
        </div>
        <!-- 右：字幕样式 + 合成主 CTA（样音 / 响度 / BGM 已归位到声音工作台） -->
        <div class="flex flex-wrap items-center gap-2">
          <!-- 字幕样式（九宫格）下拉：样式选择/应用为既有功能，九宫格为结构示意 -->
          <div class="relative">
            <button
              type="button"
              class="flex items-center gap-1 rounded-lg border border-hairline bg-surface-canvas px-3 py-1.5 text-[13px] font-medium text-ink transition-colors hover:bg-state-hover"
              :aria-expanded="stylesMenuOpen"
              @click="stylesMenuOpen = !stylesMenuOpen"
            >
              <svg class="h-4 w-4 text-info" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                <path d="M4 4h16v16H4zM4 9.3h16M4 14.6h16M9.3 4v16M14.6 4v16" />
              </svg>
              <span>字幕样式</span>
            </button>
            <div v-if="stylesMenuOpen" class="fixed inset-0 z-drawer" aria-hidden="true" @click="stylesMenuOpen = false" />
            <div v-if="stylesMenuOpen" class="absolute right-0 top-full z-drawer mt-1.5 flex w-64 flex-col gap-2 rounded-xl border border-hairline bg-surface-raised p-3 shadow-gd-4">
              <span class="text-kicker uppercase tracking-wider text-ink-mute">九宫格字幕安全锚点</span>
              <div class="grid aspect-video w-full grid-cols-3 gap-1.5 rounded-lg border border-hairline bg-surface-inset p-2">
                <div
                  v-for="cell in ['TL', 'TC', 'TR', 'ML', 'MC', 'MR', 'BL', 'BC', 'BR']"
                  :key="cell"
                  class="flex items-center justify-center rounded border text-[10px]"
                  :class="cell === 'BC' ? 'border-2 border-primary-200 bg-primary-100 font-semibold text-primary-700' : 'border-hairline text-ink-mute'"
                >
                  {{ cell === 'BC' ? 'BC 推荐' : cell }}
                </div>
              </div>
              <p class="text-[11px] text-ink-mute">已锁定下中居中（Bottom-Center）· 描边加粗</p>
              <div class="flex gap-2">
                <select
                  v-model="selectedStyleId"
                  class="gd-input gd-field-sm min-w-0 flex-1"
                  :disabled="stylesLoading || stylesError || subtitleStyles.length === 0"
                  aria-label="字幕样式"
                >
                  <option v-if="stylesError" value="">样式库暂不可用</option>
                  <option v-else-if="subtitleStyles.length === 0" value="">暂无样式</option>
                  <option v-for="style in subtitleStyles" :key="String(style.id)" :value="String(style.id)">
                    {{ style.name }}{{ style.is_default ? '（默认）' : '' }}
                  </option>
                </select>
                <AppButton variant="secondary" size="sm" :loading="applyingStyle" :disabled="stylesError || !selectedStyleId" @click="handleApplyStyle">应用</AppButton>
              </div>
              <p v-if="stylesError" class="text-[11px] text-ink-mute">样式库接口暂不可用（后端未就绪），可稍后重试</p>
            </div>
          </div>
          <AppButton
            variant="primary"
            size="sm"
            :disabled="clips.length === 0"
            :loading="composing || composeChecking"
            :title="clips.length === 0 ? '时间线为空：请先在生成任务中心完成镜头视频生成' : '按当前时间线合成成片并进入 HITL-4 成片确认'"
            @click="handleOpenCompose"
          >
            🎬 合成成片 · 母版导出
          </AppButton>
        </div>
      </div>
    </header>

    <!-- ============ HITL-4 待确认提示条（合成完成等待审批） ============ -->
    <div
      v-if="timelineInfo?.status === 'pending_review'"
      class="flex h-9 shrink-0 items-center justify-center gap-2 bg-warning-bg text-xs text-gd-pending"
      role="status"
    >
      <span>成片已合成完毕，等待你的确认（HITL-4）——通过后即可进入导出与分发</span>
      <AppButton variant="ghost" size="sm" @click="router.push(`/projects/${projectId}/post/review`)">去成片确认 →</AppButton>
    </div>

    <!-- ============ 加载 / 错误 / 空态 ============ -->
    <div v-if="loading" class="flex flex-1 items-center justify-center p-8">
      <div class="w-full max-w-lg"><LoadingSkeleton variant="card" /></div>
    </div>
    <div v-else-if="loadError" class="flex flex-1 flex-col items-center justify-center gap-3 p-8">
      <div class="w-full max-w-md">
        <ErrorRetryBlock
          :message="timelineMissing ? (cutType === 'master' ? '母版尚未合成' : '还没有可后期的时间线') : '时间线加载失败'"
          :description="loadError"
          @retry="loadTimeline"
        />
      </div>
      <AppButton v-if="timelineMissing" variant="primary" size="md" @click="cutType === 'master' ? backToPreview() : router.push(`/projects/${projectId}/render-tasks`)">
        {{ cutType === 'master' ? '切回预览版发起合成 →' : '去生成任务中心 →' }}
      </AppButton>
    </div>
    <div v-else-if="clips.length === 0" class="flex flex-1 items-center justify-center p-8">
      <EmptyState
        icon="🎬"
        :title="cutType === 'master' ? '母版尚未合成' : '还没有可后期的时间线'"
        :description="cutType === 'master'
          ? '母版时间线由「合成成片」生成——请切回预览版点击「合成成片 · 母版导出」，合成完成后此处将展示母版时间线。'
          : '时间线由已渲染（rendered）镜头自动编排——请先在生成任务中心完成镜头视频生成。'"
      >
        <template #action>
          <AppButton v-if="cutType === 'master'" variant="primary" size="lg" @click="backToPreview()">切回预览版发起合成 →</AppButton>
          <AppButton v-else variant="primary" size="lg" @click="router.push(`/projects/${projectId}/render-tasks`)">去生成任务中心 →</AppButton>
        </template>
      </EmptyState>
    </div>

    <!-- ============ 主体（原型 §2~§4：暗场工作区 → 雾带 → 多轨时间线 → HITL 关口四） ============ -->
    <template v-else>
      <div class="flex min-h-0 flex-1 flex-col overflow-y-auto">
        <!-- ===== 上：暗场工作区（预览监视器 5/12 + 声音工作台 7/12） ===== -->
        <section class="shrink-0 bg-surface-inset px-5 py-4">
          <div class="grid grid-cols-1 gap-4 lg:grid-cols-12">
            <!-- ===== 左 5/12：预览监视器（原型 PREVIEW_MONITOR · P6） ===== -->
            <div class="gd-night-window flex min-w-0 flex-col p-4 lg:col-span-5">
              <!-- 监视器头部徽标行 -->
              <div class="flex w-full items-center justify-between gap-2 border-b border-hairline pb-2.5">
                <div class="flex min-w-0 items-center gap-2">
                  <span class="relative flex h-2 w-2 shrink-0" aria-hidden="true">
                    <span class="absolute inline-flex h-full w-full animate-ping rounded-full bg-danger opacity-60" />
                    <span class="relative inline-flex h-2 w-2 rounded-full bg-danger" />
                  </span>
                  <span class="gd-num truncate text-[12px] font-semibold tracking-wider text-ink">成片监看</span>
                  <span class="gd-num shrink-0 rounded border border-primary-200 bg-primary-100 px-2 py-0.5 text-[11px] text-primary-700">
                    {{ timelineInfo?.resolution ?? '分辨率待定' }}
                  </span>
                </div>
                <div class="flex shrink-0 items-center gap-2">
                  <span class="gd-num rounded border border-hairline bg-state-hover px-2 py-0.5 text-[11px] font-semibold text-aurora-halo">
                    {{ timelineInfo?.fps ?? '—' }} FPS
                  </span>
                  <button
                    type="button"
                    class="rounded p-0.5 text-ink-mute transition-colors hover:text-ink"
                    title="全屏监看"
                    aria-label="全屏监看"
                    @click="toggleFullscreen"
                  >
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                      <path d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z" />
                    </svg>
                  </button>
                </div>
              </div>
              <!-- 9:16 竖屏舞台 -->
              <div class="flex w-full justify-center py-2.5">
                <div
                  ref="previewStage"
                  class="relative flex aspect-[9/16] h-[clamp(180px,40vh,360px)] w-auto flex-col justify-between overflow-hidden rounded-lg bg-player-panel shadow-gd-4 ring-1 ring-hairline-strong"
                >
                  <!-- 当前镜头画面：成片视频 → 缩略图 → 镜号色块 -->
                  <video
                    v-if="previewPlayable"
                    ref="monitorVideo"
                    :src="previewUrl"
                    class="h-full w-full object-contain"
                    playsinline
                    preload="metadata"
                    @error="onPreviewVideoError"
                    @ended="pause()"
                  />
                  <img
                    v-else-if="currentVideoClip?.thumb_url"
                    :src="currentVideoClip.thumb_url"
                    :alt="`镜头 ${currentVideoClip.shot_no ?? ''} 画面`"
                    class="h-full w-full object-cover"
                  />
                  <div
                    v-else
                    class="flex h-full w-full flex-col items-center justify-center gap-2"
                    :style="currentVideoClip ? { background: `linear-gradient(160deg, hsl(${clipHue(currentVideoClip)} 42% 30%), hsl(${(clipHue(currentVideoClip) + 40) % 360} 42% 18%))` } : { background: PLAYER.panel }"
                  >
                    <span class="text-3xl opacity-80" aria-hidden="true">🎞️</span>
                    <span class="gd-num text-sm text-neutral-400">{{ currentVideoClip ? `#${currentVideoClip.shot_no ?? currentVideoClip.source_id ?? ''}` : '播放头未落在镜头上' }}</span>
                  </div>
                  <!-- 上下渐隐遮罩 -->
                  <div class="pointer-events-none absolute inset-0 bg-gradient-to-t from-scrim-strong via-transparent to-scrim-faint" aria-hidden="true" />
                  <!-- 安全区参考：只保留虚线框（9:16 比例已由画幅本身表达，避免叠加过多角标） -->
                  <div class="pointer-events-none absolute inset-x-3 inset-y-4 rounded border border-dashed border-primary-200/70" aria-hidden="true" />
                  <!-- 顶部：水印角标 + SHOT 角标 -->
                  <div class="relative z-10 flex items-center justify-between px-3 pt-2.5">
                    <span class="gd-num rounded border border-white/10 bg-scrim-soft px-2 py-0.5 text-[10px] uppercase tracking-wider text-ink-soft backdrop-blur-sm">
                      {{ cutType === 'preview' ? '试看版 · 带水印' : '母版 · 无水印' }}
                    </span>
                    <span v-if="currentVideoClip" class="gd-fill-brand gd-num rounded px-2 py-0.5 text-[11px] font-semibold shadow-gd-1">
                      SHOT #{{ currentVideoClip.shot_no ?? currentVideoClip.source_id ?? '—' }}
                    </span>
                  </div>
                  <!-- 底部：配音角色条 + 字幕条（安全区内） -->
                  <div class="relative z-10 flex flex-col items-center gap-1.5 px-3 pb-4 text-center">
                    <span
                      v-if="currentAudioClip"
                      class="gd-num rounded border border-white/10 bg-scrim-soft px-2 py-0.5 text-[11px] tracking-wider text-primary-800 backdrop-blur-sm"
                    >
                      {{ currentAudioClip.label ?? '配音' }} · TTS 配音
                    </span>
                    <div class="w-full rounded-md border border-white/10 bg-scrim-strong px-3 py-1.5 shadow-gd-3 backdrop-blur-sm">
                      <p
                        class="line-clamp-2 text-[13px] font-medium leading-relaxed text-white"
                        style="text-shadow: var(--gd-scrim-strong) 1px 0 0, var(--gd-scrim-strong) -1px 0 0, var(--gd-scrim-strong) 0 1px 0, var(--gd-scrim-strong) 0 -1px 0, var(--gd-scrim-strong) 1px 1px 0, var(--gd-scrim-strong) -1px -1px 0"
                      >
                        {{ currentSubtitle || '（无字幕）' }}
                      </p>
                    </div>
                  </div>
                </div>
              </div>
              <!-- 底部传输条（原型：逐帧 / 播放 / A-B / 1.0x / 时码 / 监听音量） -->
              <div class="flex w-full flex-wrap items-center justify-between gap-x-3 gap-y-2 rounded-lg border border-hairline bg-surface-canvas px-3 py-2">
                <div class="flex items-center gap-1">
                  <button
                    type="button"
                    class="rounded p-1 text-ink-mute transition-colors hover:bg-state-hover hover:text-ink"
                    title="上一帧"
                    aria-label="上一帧"
                    @click="stepFrame(-1)"
                  >
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                      <path d="M7 6h2v12H7zm3.5 6 8.5 6V6z" />
                    </svg>
                  </button>
                  <button
                    type="button"
                    class="gd-fill-brand flex h-7 w-7 items-center justify-center rounded-full shadow-gd-1 transition-transform active:scale-95"
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
                  <button
                    type="button"
                    class="rounded p-1 text-ink-mute transition-colors hover:bg-state-hover hover:text-ink"
                    title="下一帧"
                    aria-label="下一帧"
                    @click="stepFrame(1)"
                  >
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                      <path d="M15 6h2v12h-2zM5 18l8.5-6L5 6z" />
                    </svg>
                  </button>
                  <div class="ml-1 flex items-center gap-1">
                    <button
                      type="button"
                      class="gd-num rounded border border-transparent px-1.5 py-0.5 text-[10px] font-semibold transition-colors"
                      :class="loopB !== null ? 'gd-fill-brand' : 'bg-state-hover text-ink-mute hover:bg-state-active hover:text-ink'"
                      :title="abLoopTitle"
                      @click="toggleAbLoop"
                    >
                      A-B 循环
                    </button>
                    <span class="gd-num rounded bg-state-hover px-1.5 py-0.5 text-[10px] font-semibold text-ink-mute" title="预览播放固定 1.0x">1.0x</span>
                  </div>
                </div>
                <div class="gd-num flex items-baseline gap-1.5 text-[13px] tracking-wider">
                  <span class="font-semibold text-ink">{{ timecode(playheadMs) }}</span>
                  <span class="text-[11px] text-ink-mute">/</span>
                  <span class="text-[11px] text-primary-700">{{ timecode(totalMs) }}</span>
                </div>
                <div class="flex items-center gap-1.5">
                  <svg class="h-3.5 w-3.5 text-ink-mute" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                    <path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4z" />
                  </svg>
                  <input v-model.number="previewVolume" type="range" min="0" max="100" step="1" class="h-1 w-14 cursor-pointer accent-primary-500" aria-label="监听音量" />
                  <span class="gd-num text-[11px] font-semibold text-info">{{ previewDb }}dB</span>
                </div>
              </div>
            </div>

            <!-- ===== 右 7/12：声音工作台（逐角色 TTS 微调 / BGM 情绪卡点 双页签） ===== -->
            <div class="gd-night-window flex min-w-0 flex-col p-4 lg:col-span-7">
              <!-- 页签头 + 响度/TTS 徽标 -->
              <div class="mb-2.5 flex flex-wrap items-center justify-between gap-2 border-b border-hairline pb-2.5">
                <div class="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-semibold transition-all"
                    :class="soundTab === 1 ? 'gd-fill-brand shadow-gd-1' : 'text-ink-mute hover:bg-state-hover hover:text-ink'"
                    @click="soundTab = 1"
                  >
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path :d="TRACK_ICONS.audio" />
                    </svg>
                    <span>逐角色 TTS 独立微调</span>
                    <span class="gd-num rounded bg-black/25 px-1.5 py-0.5 text-[10px]">{{ speakerRows.length }}位角色</span>
                  </button>
                  <button
                    type="button"
                    class="flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[13px] font-semibold transition-all"
                    :class="soundTab === 2 ? 'gd-fill-brand shadow-gd-1' : 'text-ink-mute hover:bg-state-hover hover:text-ink'"
                    @click="soundTab = 2"
                  >
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true">
                      <path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4" />
                    </svg>
                    <span>BGM 情绪卡点 &amp; 侧链闪避</span>
                    <span class="gd-num rounded px-1.5 py-0.5 text-[10px]" :class="bgmSidechain ? 'gd-fill-mint' : 'bg-state-hover text-ink-mute'">
                      Ducking {{ bgmSidechain ? 'ON' : 'OFF' }}
                    </span>
                  </button>
                </div>
                <div class="flex flex-wrap items-center gap-2">
                  <!-- 项目级 30s 样音：从刊头归位到声音工作台，紧邻角色音色微调 -->
                  <button
                    type="button"
                    class="flex items-center gap-1.5 rounded-md border border-hairline bg-surface-canvas px-2.5 py-1 text-[11px] font-medium transition-colors hover:bg-state-hover disabled:opacity-50"
                    :disabled="ttsPreviewing"
                    title="生成项目级 30s 样音，试听整体音色与响度"
                    @click="handleTtsPreview"
                  >
                    <span class="h-1.5 w-1.5 rounded-full" :class="ttsPreview ? 'bg-success' : 'bg-primary-500'" aria-hidden="true" />
                    <span :class="ttsPreview ? 'text-success-strong' : 'text-primary-700'">
                      {{ ttsPreviewing ? '样音生成中…' : ttsPreview ? '30s 样音已生成' : '生成 30s 样音' }}
                    </span>
                  </button>
                  <span class="gd-fill-mint gd-num flex items-center gap-1 rounded px-2 py-0.5 text-[11px]">
                    <span>-14 LUFS · 真峰值 ≤ -1 dBTP ✓</span>
                  </span>
                  <span class="gd-num flex items-center gap-1 text-[11px] text-info">
                    <svg class="h-3.5 w-3.5 text-success" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                      <path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1.2 14.5-4-4 1.4-1.4 2.6 2.6 5.6-5.6 1.4 1.4-7 7z" />
                    </svg>
                    <span>Neural TTS</span>
                  </span>
                </div>
              </div>

              <!-- 页签 1：逐角色 TTS 独立微调 -->
              <div v-show="soundTab === 1" class="flex min-h-0 flex-1 flex-col gap-2.5 overflow-y-auto pr-1">
                <!-- 30s 样音回放（生成后出现） -->
                <div v-if="ttsPreview" class="flex items-center gap-3 rounded-lg border border-primary-200 bg-primary-50 p-2.5">
                  <audio ref="ttsAudio" :src="ttsPreview.audio_url" controls class="h-8 min-w-0 flex-1" preload="none" />
                  <span class="gd-num shrink-0 text-caption text-primary-700">30s 样音 {{ (ttsPreview.duration_ms / 1000).toFixed(1) }}s</span>
                </div>
                <!-- 角色行卡（聚合自配音片段 label） -->
                <article
                  v-for="row in speakerRows"
                  :key="row.name"
                  class="flex flex-col gap-2 rounded-lg border border-hairline bg-surface-canvas p-3 transition-colors"
                  :class="TONE_CLASS[row.tone].hover"
                >
                  <div class="flex flex-wrap items-center justify-between gap-2">
                    <div class="flex min-w-0 items-center gap-2.5">
                      <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-[12px] font-semibold" :class="TONE_CLASS[row.tone].avatar">
                        {{ row.initial }}
                      </span>
                      <div class="flex min-w-0 flex-wrap items-center gap-2">
                        <span class="truncate text-caption font-semibold text-ink">{{ row.name }}</span>
                        <span class="rounded px-1.5 py-0.5 text-[10px] font-medium" :class="TONE_CLASS[row.tone].badge">配音 ({{ row.lineCount }}句)</span>
                      </div>
                    </div>
                    <div class="flex shrink-0 items-center gap-2">
                      <button
                        type="button"
                        class="flex items-center gap-1 rounded bg-state-hover px-2.5 py-1 text-[11px] font-medium text-ink transition-colors hover:bg-state-active disabled:opacity-50"
                        :disabled="ttsPreviewing"
                        title="样音接口为项目级：生成 30s 样音试听音色效果"
                        @click="handleSpeakerPreview(row.name)"
                      >
                        <svg class="h-3 w-3" :class="TONE_CLASS[row.tone].accent" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                          <path d="M8 5.14v13.72c0 .8.87 1.3 1.56.9l11-6.86a1.05 1.05 0 000-1.8l-11-6.86A1.05 1.05 0 008 5.14z" />
                        </svg>
                        <span>{{ activeSpeaker === row.name && ttsPreviewing ? '生成中…' : '试听该角色' }}</span>
                      </button>
                      <button
                        type="button"
                        class="rounded px-2.5 py-1 text-[11px] font-medium transition-opacity hover:opacity-90 disabled:opacity-50"
                        :class="TONE_CLASS[row.tone].button"
                        :disabled="ttsStarting"
                        @click="handleFullTts"
                      >
                        全剧同步应用 ({{ row.lineCount }} 句)
                      </button>
                    </div>
                  </div>
                  <!-- 三滑杆（语速 / 音调 / 情绪强度；本地 UI 态） -->
                  <div v-if="speakerParams[row.name]" class="grid grid-cols-1 gap-2.5 pt-0.5 md:grid-cols-3">
                    <div class="flex flex-col gap-1">
                      <div class="flex justify-between text-[11px] text-ink-mute">
                        <span>语速 (Rate)</span>
                        <span class="gd-num font-semibold" :class="TONE_CLASS[row.tone].accent">{{ formatRate(speakerParams[row.name].rate) }}</span>
                      </div>
                      <input v-model.number="speakerParams[row.name].rate" type="range" min="80" max="150" step="1" class="h-1 w-full cursor-pointer accent-primary-500" :aria-label="`${row.name} 语速`" />
                    </div>
                    <div class="flex flex-col gap-1">
                      <div class="flex justify-between text-[11px] text-ink-mute">
                        <span>音调 (Pitch)</span>
                        <span class="gd-num font-semibold text-info">{{ formatPitch(speakerParams[row.name].pitch) }}</span>
                      </div>
                      <input v-model.number="speakerParams[row.name].pitch" type="range" min="-6" max="6" step="1" class="h-1 w-full cursor-pointer accent-info" :aria-label="`${row.name} 音调`" />
                    </div>
                    <div class="flex flex-col gap-1">
                      <div class="flex justify-between text-[11px] text-ink-mute">
                        <span>情绪强度 (Emotion)</span>
                        <span class="gd-num font-semibold text-aurora-magenta">{{ speakerParams[row.name].emotion }}%</span>
                      </div>
                      <input v-model.number="speakerParams[row.name].emotion" type="range" min="0" max="100" step="1" class="h-1 w-full cursor-pointer accent-aurora-magenta" :aria-label="`${row.name} 情绪强度`" />
                    </div>
                  </div>
                </article>
                <!-- 无配音片段兜底 -->
                <div v-if="speakerRows.length === 0" class="flex flex-1 items-center justify-center rounded-lg border border-dashed border-hairline p-4">
                  <p class="text-caption text-ink-mute">时间线暂无配音片段——请先在刊头发起 30s 样音 / 全剧配音，或回 P15 生成台词音频。</p>
                </div>
              </div>

              <!-- 页签 2：BGM 情绪卡点 & 侧链闪避（原设置面板 BGM 卡并入） -->
              <div v-show="soundTab === 2" class="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1">
                <div class="flex flex-wrap gap-2">
                  <select v-model="bgmEmotion" class="gd-input gd-field-sm min-w-0 flex-1" aria-label="情绪" @change="fetchBgmRecommend">
                    <option v-for="opt in BGM_EMOTION_OPTIONS" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                  </select>
                  <AppButton variant="secondary" size="sm" :loading="bgmLoading" @click="fetchBgmRecommend">按情绪推荐</AppButton>
                </div>
                <ul v-if="bgmList.length > 0" class="flex flex-col gap-1.5">
                  <li v-for="track in bgmList" :key="String(track.id)">
                    <button
                      type="button"
                      class="flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left text-xs transition-colors"
                      :class="String(bgmTrackId) === String(track.id) ? 'border-primary-200 bg-primary-100' : 'border-hairline hover:bg-state-hover'"
                      @click="bgmTrackId = track.id"
                    >
                      <span class="min-w-0 flex-1">
                        <span class="block truncate font-medium text-ink">{{ track.title }}</span>
                        <span class="gd-num block truncate text-caption text-ink-mute">
                          {{ track.artist || '未知艺术家' }}<template v-if="track.bpm"> · {{ track.bpm }} BPM</template><template v-if="track.duration_ms"> · {{ Math.round(track.duration_ms / 1000) }}s</template>
                        </span>
                      </span>
                      <span v-if="String(bgmTrackId) === String(track.id)" class="text-primary-700" aria-hidden="true">✓</span>
                    </button>
                  </li>
                </ul>
                <p v-else class="text-caption text-ink-mute">暂无推荐曲目{{ bgmLoading ? '（加载中…）' : '' }}</p>
                <!-- 音量 + 侧链 + 应用 -->
                <div v-if="bgmTrackId" class="flex flex-col gap-2 rounded-lg bg-surface-sunken p-3">
                  <label class="flex items-center gap-2 text-caption text-ink-mute" for="bgm-volume">
                    <span>音量</span>
                    <span class="gd-num w-12 text-ink-soft">{{ bgmVolume }} dB</span>
                  </label>
                  <input id="bgm-volume" v-model.number="bgmVolume" type="range" min="-30" max="0" step="1" class="w-full accent-primary-500" />
                  <label class="flex cursor-pointer items-center gap-1.5 text-caption text-ink-mute">
                    <input v-model="bgmSidechain" type="checkbox" class="h-3.5 w-3.5 accent-primary-500" />
                    侧链闪避（对白出现时 BGM 自动下潜，人声优先）
                  </label>
                  <AppButton variant="primary" size="sm" block :loading="bgmApplying" @click="handleApplyBgm">应用 BGM</AppButton>
                </div>
              </div>

              <!-- 底部 BGM 行（当前应用曲目 + 波形示意 + 调音台入口） -->
              <div class="mt-2 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-hairline bg-surface-canvas p-2">
                <div class="flex min-w-0 items-center gap-2.5">
                  <span class="flex h-7 w-7 shrink-0 items-center justify-center rounded bg-warning-bg text-warning">
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path :d="TRACK_ICONS.bgm" />
                    </svg>
                  </span>
                  <div class="min-w-0">
                    <div class="flex items-center gap-2">
                      <span class="gd-num truncate text-[12px] font-semibold text-ink">{{ bgmTitle }}</span>
                      <span v-if="currentBgm?.bpm" class="gd-num rounded bg-state-hover px-1.5 py-0.5 text-[10px] text-aurora-halo">{{ currentBgm.bpm }} BPM</span>
                    </div>
                    <span class="block truncate text-[11px] text-ink-mute">
                      {{ bgmLicenseLabel }} · 侧链闪避{{ bgmSidechain ? '已联动（对白时自动下潜）' : '未启用' }}
                    </span>
                  </div>
                </div>
                <div class="flex shrink-0 items-center gap-3">
                  <svg class="h-4 w-24 text-warning" viewBox="0 0 100 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                    <path d="M0 8 Q10 2, 20 8 T40 8 C50 6, 60 6, 70 8 Q80 1, 90 15 T100 8" />
                  </svg>
                  <button
                    type="button"
                    class="rounded bg-state-hover px-2.5 py-1 text-[11px] font-medium text-ink transition-colors hover:bg-state-active"
                    @click="soundTab = 2"
                  >
                    调音台
                  </button>
                </div>
              </div>
            </div>
          </div>
        </section>

        <!-- ===== 雾带过渡 + 贯穿播放头（与 Konva 播放头同色 --gd-playhead） ===== -->
        <div class="gd-fog-junction relative z-10 shrink-0" aria-hidden="true">
          <div class="absolute inset-y-0 z-10 w-0.5 -translate-x-1/2 bg-timeline-playhead" :style="{ left: playheadPct }">
            <span class="absolute left-1/2 top-1/2 h-3.5 w-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-timeline-playhead ring-2 ring-white/50">
              <span class="absolute left-1/2 top-1/2 h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white" />
            </span>
          </div>
        </div>

        <!-- ===== 下：多轨时间线（轨头列 + 标尺行 + 四轨；原型 §3） ===== -->
        <section class="relative flex min-h-[320px] flex-1 flex-col px-5 pb-3 pt-4">
          <!-- 音画同步警示条 -->
          <div v-if="syncWarnings.length > 0" class="mb-2 flex shrink-0 items-center gap-2 rounded-lg border border-warning-border bg-warning-bg px-3 py-1.5 text-xs text-gd-pending" role="alert">
            <span>⚠ 音画同步：{{ syncWarnings.length }} 处偏差超 100ms（{{ syncWarnings.slice(0, 2).map((w) => `${w.videoLabel} ↔ ${w.audioLabel} 偏差 ${w.diffMs}ms`).join('；') }}{{ syncWarnings.length > 2 ? '…' : '' }}）· 合成时后端将拦截（70001）</span>
          </div>

          <!-- 选中片段属性小卡 -->
          <div
            v-if="selectedClip"
            class="absolute right-6 top-6 z-20 w-64 rounded-lg border border-hairline bg-[rgba(22,32,58,0.97)] p-3 shadow-gd-3 backdrop-blur-[12px]"
          >
            <div class="flex items-center justify-between">
              <span class="text-xs font-semibold text-ink">
                {{ TRACK_TYPE_LABELS[selectedClip.track_type] }}片段
                <span class="gd-num ml-1 font-normal text-ink-mute">#{{ selectedClip.id }}</span>
              </span>
              <button type="button" class="rounded p-0.5 text-ink-mute hover:bg-state-hover" aria-label="关闭属性卡" @click="selectedClipId = null">✕</button>
            </div>
            <dl class="mt-2 space-y-1 text-caption text-ink-mute">
              <div class="flex justify-between"><dt>起点</dt><dd class="gd-num">{{ timecode(selectedClip.start_ms) }}.{{ String(selectedClip.start_ms % 1000).padStart(3, '0') }}s</dd></div>
              <div class="flex justify-between"><dt>时长</dt><dd class="gd-num">{{ (clipDuration(selectedClip) / 1000).toFixed(2) }}s</dd></div>
              <div class="flex justify-between"><dt>可拖拽</dt><dd>{{ isDraggable(selectedClip) ? '是（水平移动）' : '否（自动编排）' }}</dd></div>
            </dl>
            <p v-if="selectedClip.content" class="mt-1.5 line-clamp-2 rounded bg-surface-sunken px-2 py-1 text-caption text-ink-soft">{{ selectedClip.content }}</p>
          </div>

          <!-- 时间线卡 -->
          <div class="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-hairline bg-surface-canvas shadow-gd-1">
            <div class="flex min-h-0 flex-1">
              <!-- 轨头列（原型 2/12 栅格头） -->
              <div class="gd-panel-rail flex w-44 shrink-0 flex-col border-r border-hairline">
                <!-- 控制头格（多轨时间线 + 工具；与 DOM 标尺同高） -->
                <div class="flex shrink-0 items-center justify-between gap-1 border-b border-hairline-soft px-2" :style="{ height: `${RULER_H}px` }">
                  <span class="flex min-w-0 items-center gap-1">
                    <svg class="h-3.5 w-3.5 shrink-0 text-primary-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                      <path d="M3 5h18v14H3zM7 9h6m-6 6h10" />
                    </svg>
                    <span class="truncate text-kicker text-ink-soft" title="多轨时间线 (4 Tracks)">多轨时间线</span>
                  </span>
                  <span class="flex shrink-0 items-center gap-0.5">
                    <button type="button" class="rounded bg-state-hover px-1 text-caption leading-4 text-ink-mute transition-colors hover:bg-state-active hover:text-ink" title="缩小时间线" aria-label="缩小时间线" @click="zoomBy(-PPS_STEP)">−</button>
                    <button type="button" class="rounded bg-state-hover px-1 text-caption leading-4 text-ink-mute transition-colors hover:bg-state-active hover:text-ink" title="放大时间线" aria-label="放大时间线" @click="zoomBy(PPS_STEP)">＋</button>
                    <button type="button" class="rounded bg-state-hover px-1 text-caption leading-4 text-ink-mute transition-colors hover:bg-state-active hover:text-ink disabled:opacity-50" title="保存时间线（拖拽结束自动保存）" aria-label="保存时间线" :disabled="saving" @click="persistTimeline">存</button>
                  </span>
                </div>
                <!-- 四轨轨头（图标 + 轨号徽标 + 规格；M/S 为结构件） -->
                <div
                  v-for="track in TRACKS"
                  :key="`head-${track.key}`"
                  class="flex flex-col justify-between border-b border-hairline-soft p-2"
                  :style="{ height: `${track.height}px` }"
                >
                  <div class="flex items-center justify-between gap-1">
                    <span class="flex min-w-0 items-center gap-1.5">
                      <svg class="h-4 w-4 shrink-0" :class="TRACK_ICON_CLASS[track.key]" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                        <path :d="TRACK_ICONS[track.key]" />
                      </svg>
                      <span class="truncate text-caption font-semibold text-ink">{{ track.code }} {{ track.label }}</span>
                    </span>
                    <span v-if="track.key === 'video'" class="gd-num shrink-0 text-[10px] text-ink-mute">{{ clipsOf('video').length }} 镜头</span>
                    <span v-else-if="track.key === 'audio'" class="gd-num shrink-0 text-[10px] font-medium text-success">{{ voiceClips.length }}句对齐</span>
                    <span v-else-if="track.key === 'bgm'" class="gd-num shrink-0 text-[10px] font-semibold text-warning">Ducking {{ bgmSidechain ? '-8dB' : 'OFF' }}</span>
                    <span v-else class="gd-num shrink-0 text-[10px] text-ink-mute">{{ clipsOf('subtitle').length }} 条目</span>
                  </div>
                  <div class="gd-num flex min-w-0 items-center text-[10px] text-ink-mute">
                    <span v-if="track.key === 'video'" class="min-w-0 truncate">{{ timelineInfo?.resolution ?? '—' }} · {{ timelineInfo?.fps ?? '—' }}FPS</span>
                    <span v-else-if="track.key === 'audio'" class="min-w-0 truncate">TTS 自动编排</span>
                    <span v-else-if="track.key === 'bgm'" class="min-w-0 truncate" :title="bgmTitle">{{ bgmTitle }}</span>
                    <span v-else class="min-w-0 truncate" :title="selectedStyleName ? `字幕样式：${selectedStyleName}` : '字幕样式：默认（九宫格底部居中）'">
                      ✓ 就绪 {{ subtitleReadyCount }}/{{ clipsOf('subtitle').length }}
                    </span>
                  </div>
                </div>
              </div>
              <!-- 滚动容器：DOM 标尺 + Konva 轨道体（同宽同滚，保证对齐） -->
              <div ref="timelineScroller" class="min-w-0 flex-1 overflow-x-auto overflow-y-hidden">
                <div class="relative" :style="{ width: `${stageW}px` }">
                  <!-- 标尺行（15s 主刻度 + 终点刻度 + 黄金卡点徽标 + 播放头针）
                       ★ 交互：点击 / 拖拽标尺即定位播放头（等价于视频进度条的 seek） -->
                  <div
                    class="relative shrink-0 cursor-ew-resize touch-none select-none border-b border-hairline-soft"
                    :style="{ height: `${RULER_H}px` }"
                    role="slider"
                    tabindex="0"
                    aria-label="时间线进度：点击或拖拽定位播放头，方向键逐帧微调"
                    aria-valuemin="0"
                    :aria-valuemax="totalMs"
                    :aria-valuenow="playheadMs"
                    :aria-valuetext="`${timecode(playheadMs)} / ${timecode(totalMs)}`"
                    @pointerdown="onRulerPointerDown"
                    @pointermove="onRulerPointerMove"
                    @pointerup="onRulerPointerUp"
                    @pointercancel="onRulerPointerUp"
                    @keydown="onRulerKeydown"
                  >
                    <span
                      v-for="tick in rulerTicks"
                      :key="`tick-${tick.second}`"
                      class="absolute bottom-0 flex flex-col"
                      :class="tick.x === 0 ? 'left-0 items-start' : '-translate-x-1/2 items-center'"
                      :style="{ left: `${tick.x}px` }"
                    >
                      <span class="w-px" :class="tick.final ? 'h-2.5 bg-danger' : 'h-1.5 bg-hairline-strong'" />
                      <span class="gd-num mt-0.5 text-[9px] leading-none" :class="tick.final ? 'font-semibold text-danger' : 'text-ink-mute'">{{ timecode(tick.second * 1000) }}</span>
                    </span>
                    <!-- 黄金卡点爆点（结构锚点：结尾前 4s 悬念截断位，待真实卡点数据接入后替换） -->
                    <span
                      v-if="totalMs > 0"
                      class="absolute top-0 z-10 flex -translate-x-1/2 items-center gap-0.5 whitespace-nowrap rounded border border-warning-border bg-warning-bg px-1.5 py-0.5 text-[10px] font-semibold text-warning-strong shadow-gd-1"
                      :style="{ left: `${toPx(hookMs)}px` }"
                    >
                      <span aria-hidden="true">⚡</span>
                      <span class="gd-num">{{ timecode(hookMs) }}</span>
                      <span>黄金卡点爆点</span>
                    </span>
                    <!-- 播放头针（与 Konva 播放头同 x 同色） -->
                    <span class="absolute inset-y-0 z-20 w-0.5 -translate-x-1/2 bg-timeline-playhead" :style="{ left: `${toPx(playheadMs)}px` }">
                      <span class="gd-num absolute left-1/2 top-0 -translate-x-1/2 whitespace-nowrap rounded bg-timeline-playhead px-1 py-0.5 text-[10px] font-semibold text-white shadow-gd-1">
                        {{ timecode(playheadMs) }}
                      </span>
                    </span>
                  </div>
                  <!-- Konva 轨道体（拖拽 / 吸附 / 保存逻辑不变） -->
                  <v-stage :config="{ width: stageW, height: STAGE_H }">
                    <!-- L0 背景 + 轨道分隔线 -->
                    <v-layer>
                      <v-rect :config="{ x: 0, y: 0, width: stageW, height: STAGE_H, fill: EDITOR.stage }" @click="onStageBackgroundClick" />
                      <v-line
                        v-for="track in TRACKS"
                        :key="`sep-${track.key}`"
                        :config="{ points: [0, TRACK_TOP[track.key], stageW, TRACK_TOP[track.key]], stroke: EDITOR.tick, strokeWidth: 1 }"
                      />
                    </v-layer>
                    <!-- L1 clips -->
                    <v-layer>
                      <!-- Konva 事件经 config 的 onXxx 传入：v-group 根节点为 fragment，
                           模板 @click 会触发 Vue「Extraneous non-emits event listeners」警告 -->
                      <v-group
                        v-for="clip in clips"
                        :key="String(clip.id)"
                        :config="{
                          x: toPx(clip.start_ms),
                          y: TRACK_TOP[clip.track_type],
                          draggable: isDraggable(clip),
                          onClick: () => selectClip(clip),
                          onDragmove: clipDragMoveHandler(clip),
                          onDragend: () => onClipDragEnd(),
                        }"
                      >
                        <v-rect
                          :config="{
                            x: 0,
                            y: 4,
                            width: clipW(clip),
                            height: clipH(clip),
                            cornerRadius: 4,
                            fill: TRACK_COLORS[clip.track_type].fill,
                            stroke: selectedClipId !== null && String(selectedClipId) === String(clip.id) ? TIMELINE.clipSelected : warnedAudioIds.has(String(clip.id)) ? TIMELINE.bgm.accent : EDITOR.clipStroke,
                            strokeWidth: selectedClipId !== null && String(selectedClipId) === String(clip.id) ? 2 : 1,
                          }"
                        />
                        <v-text
                          :config="{
                            x: 6,
                            y: 8,
                            width: clip.track_type === 'subtitle' && clip.content && clipW(clip) > 40 ? Math.max(0, clipW(clip) - 26) : Math.max(0, clipW(clip) - 12),
                            text: clipText(clip),
                            fontSize: clip.track_type === 'video' ? 12 : 10,
                            fontStyle: clip.track_type === 'video' ? 'bold' : 'normal',
                            fill: TRACK_COLORS[clip.track_type].text,
                            wrap: 'none',
                          }"
                        />
                        <!-- 视频片段：起止时码（原型 clip 含时码） -->
                        <v-text
                          v-if="clip.track_type === 'video' && clipW(clip) > 64"
                          :config="{
                            x: 6,
                            y: 26,
                            width: Math.max(0, clipW(clip) - 12),
                            text: `${timecode(clip.start_ms)}-${timecode(clip.end_ms)}`,
                            fontSize: 9,
                            fill: TRACK_COLORS[clip.track_type].text,
                            wrap: 'none',
                          }"
                        />
                        <!-- 配音 / BGM 片段：波形示意 -->
                        <v-line
                          v-if="(clip.track_type === 'audio' || clip.track_type === 'bgm') && clipW(clip) > 44"
                          :config="{
                            x: 6,
                            y: 4 + clipH(clip) - 13,
                            points: wavePoints(clip, Math.min(clipW(clip) - 12, 600), 10),
                            stroke: TRACK_COLORS[clip.track_type].accent,
                            strokeWidth: 1,
                            tension: 0.4,
                          }"
                        />
                        <!-- 字幕片段：内容就绪 ✓ 角标 -->
                        <v-text
                          v-if="clip.track_type === 'subtitle' && clip.content && clipW(clip) > 40"
                          :config="{ x: clipW(clip) - 16, y: 8, text: '✓', fontSize: 10, fill: TIMELINE.audio.text }"
                        />
                        <!-- 可拖拽角标 -->
                        <v-text
                          v-if="isDraggable(clip) && clipW(clip) > 44"
                          :config="{ x: clipW(clip) - 18, y: 6, text: '⠿', fontSize: 10, fill: EDITOR.handle }"
                        />
                      </v-group>
                    </v-layer>
                    <!-- L2 吸附线 + 播放指针 -->
                    <v-layer :config="{ listening: false }">
                      <v-line v-if="snapLineX !== null" :config="{ points: [snapLineX, 0, snapLineX, STAGE_H], stroke: BRAND.p500, strokeWidth: 1, dash: [4, 4] }" />
                      <v-line :config="{ points: [toPx(playheadMs), 0, toPx(playheadMs), STAGE_H], stroke: TIMELINE.playhead, strokeWidth: 1.5 }" />
                    </v-layer>
                  </v-stage>
                </div>
              </div>
            </div>
            <p class="shrink-0 border-t border-hairline-soft px-3 py-1 text-caption text-ink-mute">
              字幕 / BGM 可水平拖拽（12px 磁吸附，松手自动保存）；视频 / 配音轨由后端自动编排
            </p>
          </div>
        </section>
      </div>

      <!-- ===== 底部：HITL 关口四(H4) 成片终审卡（质检一行汇总 + 决策动作） ===== -->
      <section class="shrink-0 border-t border-hairline bg-surface-raised px-5 py-3">
        <div class="flex flex-col gap-3 rounded-xl border border-hairline bg-surface-canvas px-4 py-3 shadow-gd-1 lg:flex-row lg:items-center lg:justify-between">
          <!-- 左：质检状态单行（同步误差为硬门槛，其余指标待合成回填） -->
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-caption text-ink-soft">
            <span class="flex items-center gap-1.5">
              <span class="h-1.5 w-1.5 rounded-full" :class="maxSyncDiffMs > 100 ? 'bg-danger' : 'bg-success'" aria-hidden="true" />
              <span>音画同步</span>
              <strong class="gd-num" :class="maxSyncDiffMs > 100 ? 'text-danger-strong' : 'text-success-strong'">{{ maxSyncDiffMs }}ms</strong>
              <span class="text-ink-mute">{{ maxSyncDiffMs > 100 ? '超出 100ms，合成将被拦截（70001）' : '≤ 100ms 达标' }}</span>
            </span>
            <span class="gd-junction-v h-3" aria-hidden="true" />
            <span class="text-ink-mute">黑帧 / 爆音 · 待合成质检回填</span>
            <span class="gd-junction-v h-3" aria-hidden="true" />
            <span class="text-success-strong">响度 -14 LUFS · 真峰值 ≤ -1 dBTP ✓</span>
          </div>
          <!-- 右：终审决策（决策动作在 P17 成片确认页执行） -->
          <div class="flex shrink-0 flex-wrap items-center gap-3">
            <AppButton variant="secondary" size="sm" title="打回意见在成片确认页（HITL-4）填写提交" @click="router.push(`/projects/${projectId}/post/review`)">
              退回后期微调（输入打回意见）
            </AppButton>
            <AppButton variant="primary" size="sm" title="前往成片确认（HITL-4），通过后进入 P7 母版分发" @click="router.push(`/projects/${projectId}/post/review`)">
              ✓ 批准成片并生成母版（进入 P7 母版分发）
            </AppButton>
          </div>
        </div>
      </section>
    </template>

    <!-- ============ 合成确认弹窗 ============ -->
    <ConfirmDialog
      v-model="composeDialogOpen"
      title="发起成片合成？"
      :content="composeDialogContent"
      confirm-label="开始合成"
      :loading="composing"
      @confirm="handleCompose"
    />
  </div>
</template>

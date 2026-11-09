/**
 * 文件级注释：后期域 API（P16 成片预览与时间线 / P17 成片确认，Task 15）。
 * 端点契约（对齐 00 契约后期端点清单；backend/app/api/post.py 尚未聚合，
 * 字段形状按 backend/app/models/post.py 的 timelines / timeline_clips /
 * subtitle_styles / bgm_tracks 四表定义）：
 * - GET  /projects/{project_id}/post/timeline?cut_type=   时间线聚合（timeline + clips + 样式 + BGM）；
 * - PUT  /projects/{project_id}/post/timeline             保存时间线（clips 全量提交）；
 * - POST /projects/{project_id}/post/tts-preview          TTS 样音试听（30s）；
 * - POST /projects/{project_id}/post/tts                  全剧配音（202 受理）；
 * - POST /projects/{project_id}/post/subtitles            应用字幕样式（style_id 可空=默认）；
 * - GET  /projects/{project_id}/post/bgm-recommend?emotion=  按情绪推荐 BGM；
 * - PUT  /projects/{project_id}/post/bgm                  应用 BGM（音量 / 侧链）；
 * - POST /projects/{project_id}/post/compose              发起合成（202 受理，异步 → pending_review）；
 * - GET  /projects/{project_id}/post/preview-url          合成产物预览地址；
 * - GET  /subtitle-styles                                 字幕样式库。
 * 说明：成片确认（P17）的审批决策走已对齐后端的
 * POST /orchestration/approvals/{id}/decide（见 approvals.ts），本模块不含 decide 端点。
 */
import { http } from './http'

/** 时间线轨道类型（timeline_clips.track_type 4 值） */
export type TimelineTrackType = 'video' | 'audio' | 'subtitle' | 'bgm'

/** 时间线片段（timeline_clips 形状 + 后端聚合可能附带的展示字段） */
export interface TimelineClipItem {
  /** 片段 ID */
  id: number | string
  /** 所属时间线 ID */
  timeline_id?: number | string | null
  /** 轨道类型 */
  track_type: TimelineTrackType
  /** 源类型（shot_version/media_file/bgm/tts） */
  source_type?: string | null
  /** 源 ID（多态） */
  source_id?: number | string | null
  /** 轨内序号 */
  order_index?: number
  /** 时间线起点（毫秒） */
  start_ms: number
  /** 时间线终点（毫秒，> start_ms） */
  end_ms: number
  /** 源入点（毫秒） */
  in_point_ms?: number | null
  /** 源出点（毫秒） */
  out_point_ms?: number | null
  /** 音量（dB） */
  volume_db?: number | string | null
  /** 字幕样式 ID */
  subtitle_style_id?: number | string | null
  /** 字幕文本（字幕轨） */
  content?: string | null
  /** 镜号（后端聚合附带时可展示） */
  shot_no?: number | null
  /** 缩略 / 关键帧地址（后端聚合附带时可预览） */
  thumb_url?: string | null
  /** 展示名（后端聚合附带） */
  label?: string | null
}

/** 时间线容器（timelines 形状） */
export interface TimelineInfo {
  /** 时间线 ID */
  id: number | string
  /** 关联剧集 ID */
  episode_id?: number | string | null
  /** 版本类型 master/preview */
  cut_type: string
  /** 版本号 */
  version_no?: number
  /** 状态 draft/composing/qc_review/pending_review/approved/failed */
  status: string
  /** 总时长（毫秒，成片时长唯一权威） */
  duration_ms: number
  /** 帧率 */
  fps?: number
  /** 分辨率 720p/1080p/4k */
  resolution?: string
  /** 合成产物媒体文件 ID */
  rendered_media_file_id?: number | string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 字幕样式（subtitle_styles 形状） */
export interface SubtitleStyleItem {
  /** 样式 ID */
  id: number | string
  /** 样式名 */
  name: string
  /** 字体 */
  font_family?: string | null
  /** 字号 */
  font_size?: number | null
  /** 颜色（十六进制） */
  color?: string | null
  /** 位置 top/middle/bottom */
  position?: string | null
  /** 是否默认样式 */
  is_default?: boolean
}

/** BGM 曲目（bgm_tracks 形状） */
export interface BgmTrackItem {
  /** 曲目 ID */
  id: number | string
  /** 曲名 */
  title: string
  /** 艺术家 */
  artist?: string | null
  /** 时长（毫秒） */
  duration_ms?: number
  /** 节拍 */
  bpm?: number | null
  /** 授权类型 self/third_party */
  license_type?: string | null
  /** 情绪标签数组 */
  mood_tags?: string[] | null
  /** 推荐理由（推荐接口附带） */
  reason?: string | null
}

/** 时间线聚合响应 */
export interface TimelineData {
  /** 时间线容器信息 */
  timeline: TimelineInfo
  /** 片段列表（四轨混合） */
  clips: TimelineClipItem[]
  /** 可选字幕样式（样式库聚合时附带） */
  subtitle_styles?: SubtitleStyleItem[]
  /** 当前应用 BGM 曲目（可空） */
  bgm?: BgmTrackItem | null
}

/** TTS 样音试听响应 */
export interface TtsPreviewData {
  /** 试听音频地址 */
  audio_url: string
  /** 音频时长（毫秒） */
  duration_ms: number
  /** 参与试听的台词行 */
  lines?: string[]
}

/** 缺失配音的镜头 */
export interface VoiceMissingShot {
  /** 镜头 ID */
  shot_id: number
  /** 镜头号 */
  shot_no: number
  /** 场号 */
  scene_no: number
  /** 缺失原因：未生成 / 生成失败 / 沙箱占位 */
  reason: 'not_generated' | 'failed' | 'placeholder'
}

/** 配音超镜头时长的告警项（仍自动拉长镜头，仅提示） */
export interface VoiceOverlongShot {
  /** 镜头数据库 ID */
  shot_id: number
  /** 镜号 */
  shot_no: number
  /** 镜头原时长（毫秒） */
  base_ms: number
  /** 该镜配音总时长（含句间间隙，毫秒） */
  voice_ms: number
  /** 配音时长 / 镜头原时长 */
  ratio: number
}

/** 配音覆盖统计（合成前拦截用） */
export interface VoiceCoverageData {
  /** 有台词的镜头数 */
  dialogue_shots: number
  /** 已完成真实配音的镜头数 */
  voiced_shots: number
  /** 期望配音句数 */
  expected_line_count: number
  /** 已完成真实配音句数 */
  voiced_line_count: number
  /** 缺失配音的镜头清单 */
  missing_shots: VoiceMissingShot[]
  /** 生成失败的台词 */
  failed_lines: Array<Record<string, unknown>>
  /** 沙箱占位配音（非真实人声） */
  placeholder_lines: Array<Record<string, unknown>>
  /** 配音超过镜头原时长 1.5 倍的镜头（自动拉长，仅提示） */
  overlong_shots?: VoiceOverlongShot[]
  /** 未匹配到任何分镜的台词数 */
  unmatched: number
}

/** 保存时间线请求体 */
export interface SaveTimelinePayload {
  /** 片段全量列表（拖拽提交后按新 start_ms/end_ms 组装） */
  clips: TimelineClipItem[]
}

/** 应用 BGM 请求体 */
export interface UpdateBgmPayload {
  /** BGM 曲目 ID */
  bgm_track_id: number | string
  /** 音量（dB，对白基准 -6） */
  volume_db: number
  /** 是否侧链压限（配音时自动压低 BGM） */
  sidechain?: boolean
}

/** 时间线状态 → 中文标签映射 */
export const TIMELINE_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  composing: '合成中',
  qc_review: '质检审核中',
  pending_review: '待人工确认',
  approved: '已通过',
  failed: '合成失败',
}

/** 轨道类型 → 中文标签映射 */
export const TRACK_TYPE_LABELS: Record<TimelineTrackType, string> = {
  video: '视频轨',
  audio: '配音轨',
  subtitle: '字幕轨',
  bgm: 'BGM 轨',
}

/**
 * 归一化列表响应：兼容后端返回数组或 { items } 两种形状。
 * @param raw 原始响应
 * @returns 归一后的数组
 */
function normalizeList<T>(raw: T[] | { items: T[] } | null | undefined): T[] {
  if (Array.isArray(raw)) return raw
  if (raw && typeof raw === 'object' && Array.isArray((raw as { items?: unknown }).items)) {
    return (raw as { items: T[] }).items
  }
  return []
}

/**
 * 获取项目时间线聚合（默认预览版）。
 * @param projectId 项目 ID
 * @param cutType 版本类型 preview/master，默认 preview
 * @returns 时间线 + 片段 + 样式 + BGM
 * @throws ApiError 404 后端未就绪 / 无时间线时由拦截器统一提示
 */
export function getProjectTimeline(projectId: string, cutType: 'preview' | 'master' = 'preview'): Promise<TimelineData> {
  return http.get<TimelineData>(`/projects/${projectId}/post/timeline`, { params: { cut_type: cutType } })
}

/**
 * 保存时间线（clips 全量提交；字幕 / BGM 片段拖拽结束后调用）。
 * @param projectId 项目 ID
 * @param payload 片段全量列表
 * @returns 保存后的时间线聚合（结构宽松）
 */
export function saveProjectTimeline(projectId: string, payload: SaveTimelinePayload): Promise<TimelineData> {
  return http.put<TimelineData>(`/projects/${projectId}/post/timeline`, payload)
}

/**
 * 生成 TTS 样音试听（默认取前 30s 台词）。
 * @param projectId 项目 ID
 * @returns 音频地址与时长
 */
export function previewTts(projectId: string): Promise<TtsPreviewData> {
  return http.post<TtsPreviewData>(`/projects/${projectId}/post/tts-preview`)
}

/**
 * 发起全剧配音（202 受理，后台批量 TTS）。
 * @param projectId 项目 ID
 * @returns 受理结果（结构宽松）
 */
export function startFullTts(projectId: string): Promise<Record<string, unknown>> {
  return http.post<Record<string, unknown>>(`/projects/${projectId}/post/tts`)
}

/**
 * 应用字幕样式（style_id 缺省 = 默认样式；后端重新排布字幕轨片段）。
 * @param projectId 项目 ID
 * @param styleId 样式 ID（可空）
 * @returns 应用结果（结构宽松）
 */
export function applySubtitleStyle(projectId: string, styleId?: number | string): Promise<Record<string, unknown>> {
  return http.post<Record<string, unknown>>(`/projects/${projectId}/post/subtitles`, {
    style_id: styleId ?? null,
  })
}

/**
 * 按情绪推荐 BGM 列表。
 * @param projectId 项目 ID
 * @param emotion 情绪键（如 warm/tense/joyful）
 * @returns 推荐曲目列表（含推荐理由）
 */
export function recommendBgm(projectId: string, emotion: string): Promise<BgmTrackItem[]> {
  return http.get<BgmTrackItem[] | { items: BgmTrackItem[] }>(`/projects/${projectId}/post/bgm-recommend`, {
    params: { emotion },
  }).then(normalizeList)
}

/**
 * 应用 BGM（曲目 + 音量 + 侧链）。
 * @param projectId 项目 ID
 * @param payload 曲目 / 音量 / 侧链参数
 * @returns 应用结果（结构宽松）
 */
export function updateBgm(projectId: string, payload: UpdateBgmPayload): Promise<Record<string, unknown>> {
  return http.put<Record<string, unknown>>(`/projects/${projectId}/post/bgm`, payload)
}

/**
 * 发起成片合成（202 受理；异步合成后时间线状态流转至 pending_review）。
 * @param projectId 项目 ID
 * @returns 受理结果（结构宽松）
 */
export function composeFinal(projectId: string): Promise<Record<string, unknown>> {
  return http.post<Record<string, unknown>>(`/projects/${projectId}/post/compose`)
}

/**
 * 查询有台词镜头的配音覆盖情况（只读，用于合成前拦截）。
 * @param projectId 项目 ID
 * @param episodeNo 集号（为空表示全部集）
 * @returns 配音覆盖统计
 */
export function getVoiceCoverage(projectId: string, episodeNo?: number): Promise<VoiceCoverageData> {
  return http.get<VoiceCoverageData>(`/projects/${projectId}/post/voice-coverage`, {
    params: episodeNo ? { episode_no: episodeNo } : undefined,
  })
}

/**
 * 获取合成产物预览地址（未合成时 url 为空）。
 * @param projectId 项目 ID
 * @returns 预览地址（结构宽松）
 */
export function getPostPreviewUrl(projectId: string): Promise<{ url?: string | null; expires_in?: number }> {
  return http.get<{ url?: string | null; expires_in?: number }>(`/projects/${projectId}/post/preview-url`)
}

/**
 * 查询字幕样式库（全局 + 项目级）。
 * @returns 样式列表
 */
export function listSubtitleStyles(): Promise<SubtitleStyleItem[]> {
  return http.get<SubtitleStyleItem[] | { items: SubtitleStyleItem[] }>('/subtitle-styles').then(normalizeList)
}

/**
 * 文件级注释：V6.4 增量 API 客户端（对齐 backend `app/api/*` V6 路由）。
 *
 * 端点分组：
 * - F2.12 / F4.13：台词清单、编辑、拆分、时长预估、镜头绑定、音频驱动时长重算；
 * - F9：角色音色参数、样音、整集配音、轨道气口、单句重配、漂移处理、关口 V；
 * - F10：编排图同步、双视图、节点/边、结构校验、编译与 plan_hash、主路径回写；
 * - F11：生效参数、保存、版本、回滚、恢复默认、审计、重评估与采纳；
 * - 5A / F2.13：Story Bible、连续性事实、L1+L2 校验、报告、逻辑规则、豁免、反馈与反例；
 * - F3.14：照片上传四道闸、批次详情、身份档、M1 合成与确认落资产。
 *
 * 约定：http 拦截器已解包统一信封并统一 toast 错误，本模块只负责类型与路径。
 */
import { http } from './http'

/* ============================== 通用 ============================== */

/** 台词清单条目 */
export interface ScriptLineItem {
  /** 台词 ID */
  id: number
  /** 稳定行键（ln_0000000001 形式） */
  line_key: string | null
  /** 集号 */
  episode_no: number
  /** 场号 */
  scene_no: number
  /** 行号 */
  line_no: number
  /** 行类型 dialogue/narration/action/transition */
  line_type: string
  /** 说话人 */
  speaker_name: string | null
  /** 角色资产 ID */
  character_asset_id: number | null
  /** 文本 */
  content: string
  /** 情绪标签 */
  emotion_tag: string | null
  /** 预估时长（毫秒） */
  est_ms: number
  /** 实测时长（毫秒） */
  actual_ms: number | null
  /** 配音状态 */
  voice_status: string
  /** 是否已删除 */
  is_deleted: boolean
}

/** 镜头↔台词绑定 */
export interface ShotLineBinding {
  /** 绑定 ID */
  id?: number
  /** 台词 ID */
  line_id: number
  /** 台词文本 */
  content?: string
  /** 说话人 */
  speaker_name?: string | null
  /** 镜头内开始偏移（毫秒） */
  offset_ms: number
  /** 镜头内顺序 */
  order_index: number
}

/** 镜头绑定详情 */
export interface ShotBindingsDetail {
  /** 绑定项 */
  bindings: ShotLineBinding[]
  /** 候选台词 */
  candidates?: ScriptLineItem[]
  /** 镜头时长（毫秒） */
  duration_ms: number
  /** 台词覆盖率（%） */
  line_coverage_pct: number
  /** 是否超长句 */
  long_line?: boolean
}

/** 配音片段 */
export interface VoiceClipItem {
  /** 片段 ID */
  id: number
  /** 台词 ID */
  line_id: number
  /** 行键 */
  line_key: string | null
  /** 台词文本（后端字段名 text） */
  text: string
  /** 说话人 */
  speaker_name: string | null
  /** 音频地址 */
  audio_url: string | null
  /** 时长（毫秒） */
  duration_ms: number
  /** 词级时间戳（后端字段名 w） */
  words: Array<{ w: string; start_ms: number; end_ms: number }>
  /** 状态 */
  status: string
  /** 实际配音情绪（P1 修复：由后端持久化并返回，旁白同样返回剧本情绪） */
  emotion?: string | null
  /** 对齐降级（沙箱/对齐失败时为 true） */
  align_degraded: boolean
}

/** 配音轨详情 */
export interface VoiceTrackDetail {
  /** 集号（后端字段名 ep_no） */
  ep_no: number
  /** 轨道状态 */
  status: string
  /** 气口（毫秒） */
  gap_ms: number
  /** 片段列表 */
  clips: VoiceClipItem[]
  /** 总时长（毫秒，后端字段名 total_ms） */
  total_ms: number
  /** 轨道汇总（后端 data.track，实际接口返回的时长/统计都在这里） */
  track?: {
    /** 总时长（毫秒） */
    total_ms?: number
    /** 语音时长（毫秒） */
    speech_ms?: number
    /** 静默时长（毫秒） */
    silence_ms?: number
    /** 气口（毫秒） */
    gap_ms?: number
  }
}

/** 配音报告（对齐后端 /voice/report 实际出参） */
export interface VoiceReport {
  /** 集号 */
  episode: number
  /** 台词条数 */
  line_count: number
  /** 已配音条数 */
  voiced_count: number
  /** 生成失败条数 */
  failed_count: number
  /** 已配音语音总时长（毫秒） */
  speech_ms: number
  /** 句间气口（毫秒） */
  gap_ms: number
  /** 含气口的轨道总时长（毫秒） */
  total_ms: number
  /** 纯预估总时长（未生成音频时的兜底，毫秒） */
  estimated_total_ms: number
  /** 分场统计：场 ID → 台词数 / 时长 / 已配音数 */
  scene_stats: Record<string, { line_count: number; duration_ms: number; voiced_count: number }>
  /** 尚未绑定音色的角色 */
  missing_voice_characters: Array<{ id: number; name: string }>
  /** 是否存在对齐降级的片段 */
  align_degraded: boolean
}

/* ============================== F2.12 / F4.13 ============================== */

/**
 * 查询某集台词清单。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param sceneNo 可选场号过滤
 * @returns 台词条目与总数
 * @throws ApiError 业务或网络错误（由 http 拦截器统一提示）
 */
export function listLines(
  projectId: string | number,
  epNo: number,
  sceneNo?: number | null,
): Promise<{ items: ScriptLineItem[]; total: number }> {
  return http.get(`/projects/${projectId}/episodes/${epNo}/lines`, {
    params: sceneNo != null ? { scene_no: sceneNo } : undefined,
  })
}

/**
 * 编辑台词（文本变化会重算预估时长）。
 * @param projectId 项目 ID
 * @param lineId 台词 ID
 * @param payload 待更新字段
 * @returns 更新后的台词
 * @throws ApiError 业务或网络错误
 */
export function updateLine(
  projectId: string | number,
  lineId: number,
  payload: Partial<Pick<ScriptLineItem, 'content' | 'speaker_name' | 'character_asset_id' | 'emotion_tag'>>,
): Promise<ScriptLineItem> {
  return http.patch(`/projects/${projectId}/lines/${lineId}`, payload)
}

/**
 * 拆分超长台词。
 * @param projectId 项目 ID
 * @param lineId 台词 ID
 * @param payload 拆分位置或自定义后半段
 * @returns 原行与新建行
 * @throws ApiError 业务或网络错误
 */
export function splitLine(
  projectId: string | number,
  lineId: number,
  payload: { position?: number; content?: string },
): Promise<{ source: ScriptLineItem; created: ScriptLineItem }> {
  return http.post(`/projects/${projectId}/lines/${lineId}/split`, payload)
}

/**
 * 预估台词朗读时长。
 * @param projectId 项目 ID
 * @param lineId 台词 ID
 * @param payload 试算文本与语速
 * @returns 预估与实测时长
 * @throws ApiError 业务或网络错误
 */
export function estimateLine(
  projectId: string | number,
  lineId: number,
  payload: { content?: string; rate?: number },
): Promise<{ line_id: number; est_ms: number; actual_ms: number | null }> {
  return http.post(`/projects/${projectId}/lines/${lineId}/estimate`, payload)
}

/**
 * 查询镜头台词绑定。
 * @param projectId 项目 ID
 * @param shotId 镜头 ID
 * @returns 绑定详情
 * @throws ApiError 业务或网络错误
 */
export function getShotLineBindings(
  projectId: string | number,
  shotId: number,
): Promise<ShotBindingsDetail> {
  return http.get(`/projects/${projectId}/shots/${shotId}/line-bindings`)
}

/**
 * 全量重建镜头台词绑定。
 * @param projectId 项目 ID
 * @param shotId 镜头 ID
 * @param bindings 绑定项数组（顺序即镜头内顺序）
 * @returns 更新后的绑定详情
 * @throws ApiError 业务或网络错误
 */
export function replaceShotLineBindings(
  projectId: string | number,
  shotId: number,
  bindings: Array<Pick<ShotLineBinding, 'line_id' | 'offset_ms' | 'order_index'>>,
): Promise<ShotBindingsDetail> {
  return http.put(`/projects/${projectId}/shots/${shotId}/line-bindings`, { bindings })
}

/**
 * 按台词音频重算单镜头时长。
 * @param projectId 项目 ID
 * @param shotId 镜头 ID
 * @param forceManual 是否覆盖手工锁定时长
 * @returns 绑定详情与是否变化
 * @throws ApiError 业务或网络错误
 */
export function recalcShotDuration(
  projectId: string | number,
  shotId: number,
  forceManual = false,
): Promise<ShotBindingsDetail & { changed: boolean }> {
  return http.post(`/projects/${projectId}/shots/${shotId}/recalculate-duration`, {
    force_manual: forceManual,
    max_shot_ms: 15000,
  })
}

/**
 * 批量重算某集镜头时长。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param forceManual 是否覆盖手工锁定时长
 * @returns 重算统计
 * @throws ApiError 业务或网络错误
 */
export function recalcEpisodeDuration(
  projectId: string | number,
  epNo: number,
  forceManual = false,
): Promise<{ changed_shot_ids: number[]; report: Record<string, unknown> }> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/recalculate-duration`, {
    force_manual: forceManual,
    max_shot_ms: 15000,
  })
}

/**
 * 查询集级时长报告。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @returns 时长、覆盖率与未绑定台词
 * @throws ApiError 业务或网络错误
 */
export function getDurationReport(
  projectId: string | number,
  epNo: number,
): Promise<Record<string, unknown>> {
  return http.get(`/projects/${projectId}/episodes/${epNo}/duration-report`)
}

/* ============================== F9 配音轨 ============================== */

/**
 * 查询角色配音参数。
 * @param projectId 项目 ID
 * @param assetId 角色资产 ID
 * @returns 参数或 null
 * @throws ApiError 业务或网络错误
 */
export function getVoiceParams(
  projectId: string | number,
  assetId: number,
): Promise<Record<string, unknown> | null> {
  return http.get(`/projects/${projectId}/characters/${assetId}/voice-params`)
}

/**
 * 确认角色配音参数（新版本生效）。
 * @param projectId 项目 ID
 * @param assetId 角色资产 ID
 * @param payload 音色、语速、音调与默认情绪
 * @returns 新参数版本
 * @throws ApiError 业务或网络错误
 */
export function confirmVoiceParams(
  projectId: string | number,
  assetId: number,
  payload: { voice_id: number; rate: number; pitch: number; emotion_default?: string | null },
): Promise<Record<string, unknown>> {
  return http.put(`/projects/${projectId}/characters/${assetId}/voice-params`, payload)
}

/**
 * 生成角色样音。
 * @param projectId 项目 ID
 * @param assetId 角色资产 ID
 * @param text 试听文本
 * @param rate 语速
 * @returns 样音地址与时长
 * @throws ApiError 业务或网络错误
 */
export function generateVoiceSample(
  projectId: string | number,
  assetId: number,
  text: string,
  rate = 1,
): Promise<{ audio_url: string; duration_ms: number }> {
  return http.post(`/projects/${projectId}/characters/${assetId}/voice/sample`, {
    character_asset_id: assetId,
    text,
    rate,
    pitch: 0,
  })
}

/**
 * 查询整集配音轨。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @returns 轨道详情
 * @throws ApiError 业务或网络错误
 */
export function getVoiceTrack(projectId: string | number, epNo: number): Promise<VoiceTrackDetail> {
  return http.get(`/projects/${projectId}/episodes/${epNo}/voice/track`)
}

/**
 * 逐句生成整集配音。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param lineIds 指定台词子集（空表示整集）
 * @param force 是否强制重配
 * @returns 生成统计与轨道
 * @throws ApiError 业务或网络错误
 */
export function generateVoiceTrack(
  projectId: string | number,
  epNo: number,
  lineIds: number[] = [],
  force = false,
): Promise<{ generated: number; skipped: number; failed: number; track: VoiceTrackDetail }> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/voice/generate`, {
    line_ids: lineIds,
    force,
  })
}

/**
 * 调整配音轨气口。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param gapMs 气口毫秒
 * @returns 更新后的轨道
 * @throws ApiError 业务或网络错误
 */
export function updateVoiceTrack(
  projectId: string | number,
  epNo: number,
  gapMs: number,
): Promise<VoiceTrackDetail> {
  return http.patch(`/projects/${projectId}/episodes/${epNo}/voice/track`, { gap_ms: gapMs })
}

/**
 * 单句重配。
 * @param projectId 项目 ID
 * @param clipId 片段 ID
 * @param payload 语速、音调与原因
 * @returns 片段与漂移记录
 * @throws ApiError 业务或网络错误
 */
export function regenerateVoiceClip(
  projectId: string | number,
  clipId: number,
  payload: { rate?: number; pitch?: number; reason?: string },
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/voice/clips/${clipId}/regenerate`, payload)
}

/**
 * 处理音频时长漂移。
 * @param projectId 项目 ID
 * @param driftId 漂移记录 ID
 * @param action apply/dismiss
 * @param level absorb/recalc/regenerate（apply 时生效）
 * @returns 处理结果
 * @throws ApiError 业务或网络错误
 */
export function decideVoiceDrift(
  projectId: string | number,
  driftId: number,
  action: 'apply' | 'dismiss',
  level?: 'absorb' | 'recalc' | 'regenerate',
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/voice/drifts/${driftId}/decide`, {
    action,
    level,
  })
}

/**
 * 关口 V：审批整集配音。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param decision approve/reject
 * @param comment 备注
 * @returns 审批后轨道状态
 * @throws ApiError 业务或网络错误
 */
export function approveVoiceTrack(
  projectId: string | number,
  epNo: number,
  decision: 'approve' | 'reject',
  comment?: string,
): Promise<{ track: VoiceTrackDetail }> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/voice/approve`, { decision, comment })
}

/**
 * 查询配音报告。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @returns 完成度与降级统计
 * @throws ApiError 业务或网络错误
 */
export function getVoiceReport(projectId: string | number, epNo: number): Promise<VoiceReport> {
  return http.get(`/projects/${projectId}/episodes/${epNo}/voice/report`)
}

/* ============================== F10 合成编排 ============================== */

/** 编排节点 */
export interface CompositionNode {
  /** 节点 ID */
  id: number
  /** 节点键（shot_{shotId} 等，语义源唯一标识） */
  node_key: string
  /** 节点类型 */
  node_type: string
  /** 引用对象 ID（字符串，如镜头 ID） */
  ref_id: string | null
  /** 引用对象种类（shot 等） */
  ref_kind: string | null
  /** 参数（in_ms/out_ms/duration_ms 等） */
  params: Record<string, unknown>
  /** 展示标签 */
  label: string | null
  /** 父节点 ID */
  parent_id: number | null
  /** 画布坐标 */
  ui_pos: { x?: number; y?: number } | null
  /** 排序提示（主路径顺序） */
  order_hint: number
  /** 是否结构过期 */
  is_stale: boolean
  /** 节点状态（normal/stale/disabled） */
  status: string
}

/** 编排边 */
export interface CompositionEdge {
  /** 边 ID */
  id: number
  /** 起点节点 ID */
  from_node_id: number
  /** 终点节点 ID */
  to_node_id: number
  /** 起点节点键 */
  from_key: string | null
  /** 终点节点键 */
  to_key: string | null
  /** 转场类型（cut/dissolve 等） */
  transition: string
  /** 转场时长（毫秒） */
  duration_ms: number
  /** 排序提示 */
  order_hint: number
  /** 是否主路径边 */
  is_main: boolean
}

/** 编排图详情 */
export interface CompositionGraph {
  /** 集号 */
  episode_no: number
  /** 图版本 */
  version: number
  /** 视图 */
  view: 'track' | 'node'
  /** 节点 */
  nodes: CompositionNode[]
  /** 边 */
  edges: CompositionEdge[]
  /** 校验结果 */
  validation: { valid: boolean; issues: Array<Record<string, unknown>> }
  /** 总时长（毫秒） */
  total_duration_ms: number
}

/**
 * 从分镜同步编排图。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param force 是否强制重建
 * @returns 图摘要
 * @throws ApiError 业务或网络错误
 */
export function syncComposition(
  projectId: string | number,
  epNo: number,
  force = false,
): Promise<CompositionGraph> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/composition/sync`, undefined, {
    params: { force },
  })
}

/**
 * 获取编排图。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param view track/node
 * @returns 图详情
 * @throws ApiError 业务或网络错误
 */
export function getCompositionGraph(
  projectId: string | number,
  epNo: number,
  view: 'track' | 'node' = 'track',
): Promise<CompositionGraph> {
  return http.get(`/projects/${projectId}/episodes/${epNo}/composition`, { params: { view } })
}

/**
 * 切换编排视图。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param toView 目标视图
 * @returns 图详情与无损标记
 * @throws ApiError 业务或网络错误
 */
export function switchCompositionView(
  projectId: string | number,
  epNo: number,
  toView: 'track' | 'node',
): Promise<CompositionGraph & { lossless: boolean }> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/composition/view`, { to_view: toView })
}

/**
 * 新增编排节点。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param payload 节点字段
 * @returns 新节点
 * @throws ApiError 业务或网络错误
 */
export function createCompositionNode(
  projectId: string | number,
  epNo: number,
  payload: { node_type: string; label?: string | null; params?: Record<string, unknown>; ref_id?: number | null },
): Promise<CompositionNode> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/composition/nodes`, payload)
}

/**
 * 更新编排节点。
 * @param projectId 项目 ID
 * @param nodeId 节点 ID
 * @param payload 待更新字段
 * @returns 更新后的节点
 * @throws ApiError 业务或网络错误
 */
export function updateCompositionNode(
  projectId: string | number,
  nodeId: number,
  payload: Record<string, unknown>,
): Promise<CompositionNode> {
  return http.patch(`/projects/${projectId}/composition/nodes/${nodeId}`, payload)
}

/**
 * 删除编排节点。
 * @param projectId 项目 ID
 * @param nodeId 节点 ID
 * @returns 删除结果
 * @throws ApiError 业务或网络错误
 */
export function deleteCompositionNode(
  projectId: string | number,
  nodeId: number,
): Promise<{ deleted_node: number; deleted_edges: number }> {
  return http.delete(`/projects/${projectId}/composition/nodes/${nodeId}`)
}

/**
 * 新增编排边。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param payload 边字段
 * @returns 新边
 * @throws ApiError 业务或网络错误
 */
export function createCompositionEdge(
  projectId: string | number,
  epNo: number,
  payload: { from_node_id: number; to_node_id: number; transition?: string; duration_ms?: number },
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/composition/edges`, payload)
}

/**
 * 删除编排边。
 * @param projectId 项目 ID
 * @param edgeId 边 ID
 * @returns 删除结果
 * @throws ApiError 业务或网络错误
 */
export function deleteCompositionEdge(
  projectId: string | number,
  edgeId: number,
): Promise<{ deleted: boolean; edge_id: number }> {
  return http.delete(`/projects/${projectId}/composition/edges/${edgeId}`)
}

/**
 * 编译编排图为渲染计划。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param preview 是否试渲
 * @param force 是否强制重编译
 * @returns 计划与 plan_hash
 * @throws ApiError 业务或网络错误
 */
export function compileComposition(
  projectId: string | number,
  epNo: number,
  preview = false,
  force = false,
): Promise<{ plan: Record<string, unknown>; plan_hash: string; reused: boolean }> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/composition/compile`, {
    preview,
    force,
  })
}

/**
 * 主路径顺序回写分镜。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param confirmImpacts 是否确认接受影响
 * @returns 影响清单或回写结果
 * @throws ApiError 业务或网络错误
 */
export function applyCompositionOrder(
  projectId: string | number,
  epNo: number,
  confirmImpacts: boolean,
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/composition/apply-order`, {
    confirm_impacts: confirmImpacts,
  })
}

/**
 * 查询历史渲染计划。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @returns 计划列表
 * @throws ApiError 业务或网络错误
 */
export function listCompositionPlans(
  projectId: string | number,
  epNo: number,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/episodes/${epNo}/composition/plans`)
}

/* ============================== F3.13 多维锚点包 ============================== */

/** 锚点项 */
export interface AnchorItem {
  /** 锚点类型 face/outfit/scene/prop/explicit */
  ref_type: string
  /** 引用 ID */
  ref_id: number
  /** 引用形态 embedding/image/text/state/asset_version */
  ref_kind: string
  /** 资产 ID */
  asset_id?: number
  /** 来源表 */
  source: string
  /** 展示名 */
  label: string
  /** 文本锚点内容（服装/场景） */
  text?: string
  /** 场景关键点位 */
  key_points?: Array<Record<string, unknown>>
  /** 光线基线 */
  lighting_base?: string | null
  /** 道具状态 */
  state?: string | null
  /** 媒体文件 ID */
  media_file_id?: number
}

/** 镜头锚点包 */
export interface AnchorPack {
  /** 镜头 ID */
  shot_id: number
  /** 集号 */
  ep_no: number | null
  /** 场号 */
  scene_no: number
  /** 锚点项 */
  items: AnchorItem[]
  /** 缺失项 */
  missing: Array<{ ref_type: string; reason: string; asset_id?: number }>
  /** 降级说明 */
  degradation: { level: string; missing_types: string[]; note: string }
  /** 引用摘要哈希 */
  pack_hash: string
}

/**
 * 查询单镜头锚点包。
 * @param projectId 项目 ID
 * @param shotId 镜头 ID
 * @returns 锚点包
 * @throws ApiError 业务或网络错误
 */
export function getShotAnchorPack(
  projectId: string | number,
  shotId: number,
): Promise<AnchorPack> {
  return http.get(`/projects/${projectId}/shots/${shotId}/anchor-pack`)
}

/**
 * 查询整集镜头锚点包。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @returns 锚点包列表
 * @throws ApiError 业务或网络错误
 */
export function listAnchorPacks(
  projectId: string | number,
  epNo: number,
): Promise<{ items: AnchorPack[]; total: number }> {
  return http.get(`/projects/${projectId}/episodes/${epNo}/anchor-packs`)
}

/* ============================== F11 参数中心 ============================== */

/** 参数定义分组 */
export interface ParamGroup {
  /** 组标识 A~H */
  group: string
  /** 组内定义 */
  items: ParamDefinition[]
}

/** 参数定义条目 */
export interface ParamDefinition {
  /** 参数键 */
  param_key: string
  /** 中文名 */
  label: string
  /** 值类型 */
  value_type: string
  /** 默认值 */
  default_value: unknown
  /** 硬下界 */
  hard_min: unknown
  /** 硬上界 */
  hard_max: unknown
  /** 推荐下界 */
  suggested_min: unknown
  /** 推荐上界 */
  suggested_max: unknown
  /** 单位 */
  unit: string | null
  /** 生效范围 */
  effect_scope: string
  /** 是否危险参数 */
  is_dangerous: boolean
  /** 是否合规锁定 */
  is_locked: boolean
  /** 是否进阶参数 */
  is_advanced: boolean
  /** 是否首屏常用 */
  is_common: boolean
  /** 说明 */
  description: string | null
  /** 调高影响 */
  high_effect: string | null
  /** 调低影响 */
  low_effect: string | null
}

/** 生效参数响应 */
export interface EffectiveParams {
  /** 项目 ID */
  project_id: number
  /** 集号 */
  ep_no: number | null
  /** 配置版本 */
  version: number
  /** 生效值 */
  values: Record<string, unknown>
  /** 参数定义分组 */
  groups?: ParamGroup[]
}

/**
 * 查询生效参数。
 * @param projectId 项目 ID
 * @param epNo 可选集号
 * @returns 生效参数与定义
 * @throws ApiError 业务或网络错误
 */
export function getEffectiveParams(
  projectId: string | number,
  epNo?: number | null,
): Promise<EffectiveParams> {
  return http.get(`/projects/${projectId}/params`, {
    params: { include_defs: true, ...(epNo != null ? { ep_no: epNo } : {}) },
  })
}

/**
 * 保存参数变更。
 * @param projectId 项目 ID
 * @param changes 变更键值
 * @param reason 变更理由（危险参数必填，≥10 字）
 * @param scope project/episode
 * @param scopeId 作用域 ID
 * @returns 变更结果与新版本
 * @throws ApiError 业务或网络错误（合规锁定返回 20003）
 */
export function saveParams(
  projectId: string | number,
  changes: Record<string, unknown>,
  reason: string,
  scope: 'project' | 'episode' = 'project',
  scopeId = '0',
): Promise<{ version: number; changed: Record<string, unknown> }> {
  return http.put(`/projects/${projectId}/params`, { changes, reason, scope, scope_id: scopeId })
}

/**
 * 查询参数版本列表。
 * @param projectId 项目 ID
 * @returns 版本列表
 * @throws ApiError 业务或网络错误
 */
export function listParamVersions(
  projectId: string | number,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/params/versions`)
}

/**
 * 回滚到历史版本。
 * @param projectId 项目 ID
 * @param versionNo 目标版本号
 * @param reason 理由
 * @returns 新版本信息
 * @throws ApiError 业务或网络错误
 */
export function rollbackParams(
  projectId: string | number,
  versionNo: number,
  reason: string,
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/params/rollback`, { version_no: versionNo, reason })
}

/**
 * 恢复默认参数。
 * @param projectId 项目 ID
 * @param paramKeys 参数键（空表示全部）
 * @param reason 理由
 * @returns 恢复结果
 * @throws ApiError 业务或网络错误
 */
export function restoreParams(
  projectId: string | number,
  paramKeys: string[],
  reason: string,
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/params/restore`, { param_keys: paramKeys, reason })
}

/**
 * 查询参数审计。
 * @param projectId 项目 ID
 * @returns 审计列表
 * @throws ApiError 业务或网络错误
 */
export function listParamAudits(
  projectId: string | number,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/params/audits`)
}

/**
 * 按当前参数重评估历史校验裁决。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param target 对象类型
 * @returns 对照结果
 * @throws ApiError 业务或网络错误
 */
export function reassessParams(
  projectId: string | number,
  epNo: number,
  target = 'shot',
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/params/reassess`, { ep_no: epNo, target })
}

/**
 * 采纳重评估结论。
 * @param projectId 项目 ID
 * @param checkIds 校验结果 ID 列表
 * @returns 采纳结果
 * @throws ApiError 业务或网络错误
 */
export function adoptReassess(
  projectId: string | number,
  checkIds: number[],
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/params/reassess/adopt`, { check_ids: checkIds })
}

/* ============================== 5A / F2.13 ============================== */

/**
 * 查询 Story Bible。
 * @param projectId 项目 ID
 * @param epNo 可选集号
 * @returns 角色/场景/道具与完整度
 * @throws ApiError 业务或网络错误
 */
export function getStoryBible(
  projectId: string | number,
  epNo?: number | null,
): Promise<{
  characters: Array<Record<string, unknown>>
  scenes: Array<Record<string, unknown>>
  props: Array<Record<string, unknown>>
  completeness: Record<string, unknown>
  warning: string | null
}> {
  return http.get(`/projects/${projectId}/story-bible`, {
    params: epNo != null ? { ep_no: epNo } : undefined,
  })
}

/**
 * 写入 Story Bible 角色。
 * @param projectId 项目 ID
 * @param payload 角色字段（char_key 幂等）
 * @returns 角色与完整度
 * @throws ApiError 业务或网络错误
 */
export function upsertBibleCharacter(
  projectId: string | number,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return http.put(`/projects/${projectId}/story-bible/characters`, payload)
}

/**
 * 写入 Story Bible 场景。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param sceneNo 场号
 * @param payload 场景字段
 * @returns 场景与完整度
 * @throws ApiError 业务或网络错误
 */
export function upsertBibleScene(
  projectId: string | number,
  epNo: number,
  sceneNo: number,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return http.put(`/projects/${projectId}/episodes/${epNo}/scenes/${sceneNo}/story-bible`, payload)
}

/**
 * 写入 Story Bible 道具。
 * @param projectId 项目 ID
 * @param payload 道具字段（prop_key 幂等）
 * @returns 道具与完整度
 * @throws ApiError 业务或网络错误
 */
export function upsertBibleProp(
  projectId: string | number,
  payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  return http.put(`/projects/${projectId}/story-bible/props`, payload)
}

/**
 * 重建连续性事实。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param overwrite 是否覆盖人工编辑
 * @returns 重建统计
 * @throws ApiError 业务或网络错误
 */
export function rebuildFacts(
  projectId: string | number,
  epNo: number,
  overwrite = false,
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/continuity/facts/rebuild`, {
    ep_no: epNo,
    overwrite,
  })
}

/**
 * 查询连续性事实。
 * @param projectId 项目 ID
 * @param epNo 可选集号
 * @returns 事实列表
 * @throws ApiError 业务或网络错误
 */
export function listFacts(
  projectId: string | number,
  epNo?: number | null,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/continuity/facts`, {
    params: { latest_only: true, ...(epNo != null ? { ep_no: epNo } : {}) },
  })
}

/**
 * 执行连续性校验（L1 + L2）。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param shotIds 镜头子集（空表示整集）
 * @returns 校验汇总与降级标记
 * @throws ApiError 业务或网络错误
 */
export function runContinuityChecks(
  projectId: string | number,
  epNo: number,
  shotIds: number[] = [],
): Promise<{
  summary: Record<string, number>
  checks: Array<Record<string, unknown>>
  degraded: boolean
  config_version: number
}> {
  return http.post(`/projects/${projectId}/continuity/checks`, {
    ep_no: epNo,
    shot_ids: shotIds,
    use_bible: true,
  })
}

/**
 * 查询连续性报告。
 * @param projectId 项目 ID
 * @param epNo 可选集号
 * @returns 汇总与最近校验
 * @throws ApiError 业务或网络错误
 */
export function getContinuityReport(
  projectId: string | number,
  epNo?: number | null,
): Promise<Record<string, unknown>> {
  return http.get(`/projects/${projectId}/continuity/report`, {
    params: epNo != null ? { ep_no: epNo } : undefined,
  })
}

/**
 * 执行 F2.13 逻辑规则检查。
 * @param projectId 项目 ID
 * @param epNo 集号
 * @param ruleCodes 规则编号（空表示全部）
 * @returns 问题清单与计数
 * @throws ApiError 业务或网络错误
 */
export function runLogicCheck(
  projectId: string | number,
  epNo: number,
  ruleCodes: string[] = [],
): Promise<{ issues: Array<Record<string, unknown>>; counts: Record<string, number>; waived: number }> {
  return http.post(`/projects/${projectId}/episodes/${epNo}/logic-check`, {
    ep_no: epNo,
    rule_codes: ruleCodes,
  })
}

/**
 * 创建逻辑规则豁免。
 * @param projectId 项目 ID
 * @param ruleCode 规则编号
 * @param targetRef 定位引用
 * @param reason 理由（≥10 字）
 * @returns 豁免记录
 * @throws ApiError 业务或网络错误
 */
export function createLogicWaiver(
  projectId: string | number,
  ruleCode: string,
  targetRef: string,
  reason: string,
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/logic-waivers`, {
    rule_code: ruleCode,
    target_ref: targetRef,
    reason,
  })
}

/**
 * 列出逻辑规则豁免。
 * @param projectId 项目 ID
 * @returns 豁免列表
 * @throws ApiError 业务或网络错误
 */
export function listLogicWaivers(
  projectId: string | number,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/logic-waivers`)
}

/**
 * 写入结构化反馈。
 * @param projectId 项目 ID
 * @param payload 反馈字段
 * @returns 反馈记录与路由结果
 * @throws ApiError 业务或网络错误
 */
export function createFeedback(
  projectId: string | number,
  payload: {
    target_type: string
    target_id: number
    dimensions: string[]
    expectation: string
    expectation_text?: string
    keep_items?: string[]
    text: string
    source_gate?: string
  },
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/feedback`, payload)
}

/**
 * 查询结构化反馈。
 * @param projectId 项目 ID
 * @param targetType 可选对象类型
 * @param targetId 可选对象 ID
 * @returns 反馈列表
 * @throws ApiError 业务或网络错误
 */
export function listFeedback(
  projectId: string | number,
  targetType?: string,
  targetId?: number,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/feedback`, {
    params: {
      ...(targetType ? { target_type: targetType } : {}),
      ...(targetId != null ? { target_id: targetId } : {}),
    },
  })
}

/**
 * 查询反例库。
 * @param projectId 项目 ID
 * @param dimension 可选维度
 * @returns 反例列表
 * @throws ApiError 业务或网络错误
 */
export function listNegativeExamples(
  projectId: string | number,
  dimension?: string,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/negative-examples`, {
    params: dimension ? { dimension } : undefined,
  })
}

/* ============================== F3.14 照片合成角色 ============================== */

/** 照片上传结果 */
export interface PhotoUploadResult {
  /** 照片记录 */
  photo: Record<string, unknown>
  /** 批次状态 */
  batch: Record<string, unknown>
}

/**
 * 上传登记照片（四道闸）。
 * @param projectId 项目 ID
 * @param payload 照片元数据与用途声明
 * @returns 照片与批次状态
 * @throws ApiError 业务或网络错误（50004/50002/50003 对应闸门拒绝）
 */
export function uploadPhoto(
  projectId: string | number,
  payload: {
    batch_key?: string
    file_name: string
    mime: string
    size_bytes: number
    width?: number
    height?: number
    sha256?: string
    declaration?: string
    authorization_confirmed?: boolean
    analysis?: Record<string, unknown>
  },
): Promise<PhotoUploadResult> {
  return http.post(`/projects/${projectId}/photo-batches/uploads`, payload)
}

/**
 * 查询照片批次详情。
 * @param projectId 项目 ID
 * @param batchKey 批次键
 * @returns 批次统计与照片明细
 * @throws ApiError 业务或网络错误
 */
export function getPhotoBatch(
  projectId: string | number,
  batchKey: string,
): Promise<Record<string, unknown> & { photos: Array<Record<string, unknown>> }> {
  return http.get(`/projects/${projectId}/photo-batches/${batchKey}`)
}

/**
 * 列出项目照片。
 * @param projectId 项目 ID
 * @param batchKey 可选批次键
 * @returns 照片列表与批次数
 * @throws ApiError 业务或网络错误
 */
export function listPhotoUploads(
  projectId: string | number,
  batchKey?: string,
): Promise<{ items: Array<Record<string, unknown>>; total: number; batches: string[] }> {
  return http.get(`/projects/${projectId}/photo-batches`, {
    params: batchKey ? { batch_key: batchKey } : undefined,
  })
}

/**
 * 创建身份档。
 * @param projectId 项目 ID
 * @param payload 批次、名称与照片选择
 * @returns 身份档
 * @throws ApiError 业务或网络错误
 */
export function createIdentityProfile(
  projectId: string | number,
  payload: { batch_key: string; name: string; photo_ids?: number[]; main_photo_id?: number | null },
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/identity-profiles`, payload)
}

/**
 * 列出身份档。
 * @param projectId 项目 ID
 * @returns 身份档列表
 * @throws ApiError 业务或网络错误
 */
export function listIdentityProfiles(
  projectId: string | number,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/identity-profiles`)
}

/**
 * 发起 M1 照片合成。
 * @param projectId 项目 ID
 * @param payload 身份档、风格预设与风格化强度
 * @returns 合成任务（含预览与自检）
 * @throws ApiError 业务或网络错误
 */
export function createPhotoComposition(
  projectId: string | number,
  payload: { profile_id: number; style_preset_id?: number | null; stylization?: number },
): Promise<Record<string, unknown>> {
  return http.post(`/projects/${projectId}/character-compositions`, payload)
}

/**
 * 列出合成任务。
 * @param projectId 项目 ID
 * @returns 合成任务列表
 * @throws ApiError 业务或网络错误
 */
export function listPhotoCompositions(
  projectId: string | number,
): Promise<{ items: Array<Record<string, unknown>>; total: number }> {
  return http.get(`/projects/${projectId}/character-compositions`)
}

/**
 * 确认合成结果并落角色资产。
 * @param projectId 项目 ID
 * @param compositionId 合成任务 ID
 * @param name 角色资产名称
 * @returns 合成任务与资产 ID
 * @throws ApiError 业务或网络错误
 */
export function confirmPhotoComposition(
  projectId: string | number,
  compositionId: number,
  name?: string,
): Promise<{ composition: Record<string, unknown>; asset_id: string }> {
  return http.post(`/projects/${projectId}/character-compositions/${compositionId}/confirm`, undefined, {
    params: name ? { name } : undefined,
  })
}

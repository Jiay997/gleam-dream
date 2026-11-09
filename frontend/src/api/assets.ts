/**
 * 文件级注释：资产域 API（与 backend/app/api/assets.py 签名对齐，Task 13）。
 * 端点清单：
 * - POST /projects/{project_id}/assets/plan          资产规划（202 后台 LLM 提取，事件 assets.planned）；
 * - GET  /projects/{project_id}/assets?asset_type=&status=&page=&page_size=  资产分页列表；
 * - GET  /assets/{asset_id}                          资产详情（含 versions/qc_reports/voice/shot_refs）；
 * - POST /assets/{asset_id}/generate                 发起生成（202 后台 stub 文生图 + QC，事件 asset.generated）；
 * - POST /assets/{asset_id}/regenerate               重新生成（同 generate，新版本旧版保留）；
 * - POST /assets/{asset_id}/approve                  审核通过（pending_review → approved）；
 * - POST /assets/{asset_id}/reject                   驳回（feedback ≥10 字 → rejected）；
 * - POST /assets/{asset_id}/lock                     锁定锚点（版本齐全且 QC 通过，否则 60003）；
 * - GET  /assets/{asset_id}/versions                 版本列表（版本号倒序）；
 * - PUT  /assets/{asset_id}/voice                    绑定 TTS 音色；
 * - GET  /tts-voices                                 音色库分页（默认 available）；
 * - POST /assets/{asset_id}/face-authorization       真人照片授权留痕（IP/UA/协议版本存证）；
 * - GET  /assets/{asset_id}/anchor-impact            锚点影响镜头数统计。
 * 资产状态机：planning → generating → qc_review → pending_review → approved → locked；
 * 异常态 qc_failed / rejected。media_url 为相对路径 /files/{object_key}（Vite 已代理）。
 */
import { http } from './http'

/** 资产版本类型（后端 CHECK 约束 7 值；角色主用前 5 种） */
export type AssetVersionType =
  | 'portrait'
  | 'turnaround_front'
  | 'turnaround_side'
  | 'turnaround_back'
  | 'expression_set'
  | 'scene_ref'
  | 'prop_ref'

/** 资产类型（后端 CHECK 约束 4 值） */
export type AssetType = 'character' | 'scene' | 'prop' | 'voice'

/** 版本类型 → 中文标签映射（画廊 / 生成按钮文案共用） */
export const ASSET_VERSION_TYPE_LABELS: Record<AssetVersionType, string> = {
  portrait: '主形象',
  turnaround_front: '三视图 · 正面',
  turnaround_side: '三视图 · 侧面',
  turnaround_back: '三视图 · 背面',
  expression_set: '表情集',
  scene_ref: '场景参考图',
  prop_ref: '道具参考图',
}

/** 资产类型 → 中文标签映射（Tab / 徽章共用） */
export const ASSET_TYPE_LABELS: Record<AssetType, string> = {
  character: '角色',
  scene: '场景',
  prop: '道具',
  voice: '音色',
}

/** 资产（后端 asset_out 序列化形状，列表 / 审核返回共用） */
export interface AssetItem {
  /** 资产 ID */
  id: number
  /** 所属项目 ID */
  project_id: number | null
  /** 资产类型（character/scene/prop/voice） */
  asset_type: AssetType
  /** 资产名称 */
  name: string
  /** 描述（角色为外形描述） */
  description: string | null
  /** 资产状态（资产 9 态） */
  status: string
  /** 来源（ai_generated/manual） */
  source: string | null
  /** 当前版本 ID（可空） */
  current_version_id: number | null
  /** 绑定音色 ID（可空） */
  voice_id: number | null
  /** 标签数组（JSONB） */
  tags: string[] | null
  /** 扩展元数据（appearance/age_range/weight/last_error 等） */
  meta: Record<string, unknown> | null
  /** 审核通过时间 */
  approved_at: string | null
  /** 锁定时间 */
  locked_at: string | null
  /** 创建时间 */
  created_at: string | null
  /** 更新时间 */
  updated_at: string | null
  /** 引用镜头数（仅详情接口携带） */
  shot_refs?: number
}

/** 资产版本（后端 asset_version_out 形状） */
export interface AssetVersionItem {
  /** 版本 ID */
  id: number
  /** 所属资产 ID */
  asset_id: number
  /** 版本类型 */
  version_type: AssetVersionType
  /** 同类型版本序号（从 1 递增） */
  version_no: number
  /** 媒体文件 ID（可空） */
  media_file_id: number | null
  /** 产物访问 URL（/files/... 相对路径，可空） */
  media_url: string | null
  /** 生成 prompt */
  prompt: string | null
  /** 随机种子 */
  seed: number | null
  /** 模型标识 */
  model_key: string | null
  /** 关联 QC 报告 ID（可空） */
  qc_report_id: number | null
  /** 是否当前版本 */
  is_current: boolean
  /** 创建时间 */
  created_at: string | null
}

/** 资产质检报告（后端 qc_report_out 形状） */
export interface AssetQcReport {
  /** 报告 ID */
  id: number
  /** 目标类型（asset） */
  target_type: string
  /** 目标 ID */
  target_id: number
  /** 关联版本 ID（可空） */
  target_version_id: number | null
  /** 结果（passed/warning/failed） */
  result: string
  /** 一致性得分 0~1（可空） */
  consistency_score: number | null
  /** 明细（version_type/thresholds 等） */
  detail: Record<string, unknown> | null
  /** 轮次 */
  round_no: number | null
  /** 创建时间 */
  created_at: string | null
}

/** TTS 音色（后端 voice_out 形状） */
export interface TtsVoice {
  /** 音色 ID（配音模块按此 ID 引用） */
  id: number
  /** 音色键名 */
  voice_key: string
  /** 音色名称 */
  name: string
  /** 性别（male/female/neutral） */
  gender: string
  /** 语言（BCP-47） */
  language: string
  /** 风格标签（后端 JSONB 数组；兼容历史的字符串形态） */
  style_tags: string[] | string | null
  /** 提供方 */
  provider: string | null
  /** 状态（available/unavailable） */
  status: string
}

/** 音色性别 → 中文标签（音色列表 / 详情共用） */
export const VOICE_GENDER_LABELS: Record<string, string> = {
  male: '男声',
  female: '女声',
  neutral: '中性',
}

/** 音色风格标签 → 中文标签（英文原始标签的展示映射，未收录时原样展示） */
export const VOICE_STYLE_LABELS: Record<string, string> = {
  warm: '温暖',
  gentle: '温柔',
  sweet: '甜美',
  young: '年轻',
  calm: '沉稳',
  professional: '专业',
  lively: '活泼',
  cheerful: '明快',
  romance: '浪漫',
  soft: '柔和',
  deep: '低沉',
  serious: '严肃',
  narration: '旁白',
  energetic: '活力',
  magnetic: '磁性',
  tension: '张力',
  documentary: '纪录片',
}

/** 绑定该音色的角色摘要（音色详情页展示） */
export interface TtsVoiceBoundCharacter {
  /** 角色资产 ID */
  id: number
  /** 角色名称 */
  name: string
}

/** TTS 音色详情（后端 tts_voice_service.get_voice_detail 形状） */
export interface TtsVoiceDetail extends TtsVoice {
  /** 实际调用的引擎音色名（如 Cherry） */
  engine_voice: string
  /** 默认试听示例句 */
  sample_text: string
  /** 已持久化样音地址（相对路径 /files/...，未生成过为 null） */
  sample_audio_url: string | null
  /** 已持久化样音时长（毫秒） */
  sample_duration_ms: number | null
  /** 绑定该音色的角色数量 */
  bound_character_count: number
  /** 最近绑定的角色列表（≤6） */
  bound_characters: TtsVoiceBoundCharacter[]
  /** 下架时间（可空） */
  offline_at: string | null
  /** 授权到期时间（可空） */
  license_expires_at: string | null
  /** 更新时间 */
  updated_at: string | null
  /** 允许的语速区间 */
  rate_range: { min: number; max: number }
  /** 允许的音调区间（半音） */
  pitch_range: { min: number; max: number }
  /** 试听文本长度上限 */
  max_preview_text_length: number
}

/** 音色试听 / 重新生成样音请求体 */
export interface VoicePreviewPayload {
  /** 试听文本（空则使用音色默认示例句，≤80 字） */
  text?: string | null
  /** 语速倍率（0.5–2.0） */
  rate?: number
  /** 音调偏移（-12–12 半音） */
  pitch?: number
}

/** 音色试听结果（后端 synthesize_preview 形状） */
export interface VoicePreviewResult {
  /** 音色 ID */
  voice_id: number
  /** 音色键名 */
  voice_key: string
  /** 音色名称 */
  name: string
  /** 引擎音色名 */
  engine_voice: string
  /** 实际合成的文本 */
  text: string
  /** 语速倍率 */
  rate: number
  /** 音调偏移 */
  pitch: number
  /** 音频对象键 */
  audio_key: string
  /** 音频访问地址（/files/... 相对路径） */
  audio_url: string
  /** 音频时长（毫秒） */
  duration_ms: number
  /** MIME 类型 */
  mime_type: string
  /** 是否为沙盒占位音频 */
  sandbox: boolean
  /** 生成时间（ISO 字符串） */
  generated_at: string
  /** 是否已持久化为音色样音 */
  persisted: boolean
  /** 持久化后的媒体文件 ID（persist=true 时返回） */
  sample_media_id?: number
}

/**
 * 归一化音色风格标签（后端 JSONB 数组 / 历史字符串 → 字符串数组）。
 * @param tags 原始标签字段
 * @returns 去重后的标签数组
 */
export function voiceStyleTagList(tags: string[] | string | null | undefined): string[] {
  if (!tags) return []
  const list = Array.isArray(tags) ? tags : tags.split(/[,，、\s]+/)
  return Array.from(new Set(list.map((tag) => tag.trim()).filter(Boolean)))
}

/**
 * 音色风格标签的中文展示文案。
 * @param tag 原始标签
 * @returns 中文标签（未收录时原样返回）
 */
export function voiceStyleLabel(tag: string): string {
  return VOICE_STYLE_LABELS[tag] ?? tag
}

/**
 * 音色展示用短编号（VO_003；与资产卡 ASSET_ID 徽标同族，便于列表与详情互认）。
 * @param voiceId 音色 ID
 * @returns 形如 VO_003 的编号
 */
export function voiceIdTag(voiceId: number): string {
  return `VO_${String(voiceId).padStart(3, '0')}`
}

/** 资产详情（后端 asset_detail_out 形状：资产 + 版本 + QC + 音色 + 引用镜头数） */
export interface AssetDetail extends AssetItem {
  /** 全部版本（版本号倒序） */
  versions: AssetVersionItem[]
  /** 全部 QC 报告 */
  qc_reports: AssetQcReport[]
  /** 已绑定音色（未绑定为 null） */
  voice: TtsVoice | null
}

/** 分页包装（后端 {items, page, page_size, total}） */
export interface AssetPage<T> {
  /** 当前页数据 */
  items: T[]
  /** 页码 */
  page: number
  /** 页大小 */
  page_size: number
  /** 总条数 */
  total: number
}

/** 202 受理回执（generate / regenerate / plan） */
export interface AssetAcceptedReceipt {
  /** 资产 ID（plan 时为项目 ID） */
  asset_id?: number
  /** 项目 ID（plan 返回） */
  project_id?: number
  /** 版本类型（generate / regenerate 返回） */
  version_type?: AssetVersionType
  /** 受理状态（accepted） */
  status: string
  /** 提示消息 */
  message: string
}

/** 资产列表查询参数 */
export interface ListAssetsParams {
  /** 类型筛选（character/scene/prop/voice） */
  asset_type?: AssetType
  /** 状态筛选（资产 9 态之一） */
  status?: string
  /** 页码（≥1） */
  page?: number
  /** 每页条数（1~100） */
  page_size?: number
}

/**
 * 发起资产规划（202 后台 LLM 从剧本提取角色/场景/道具清单；完成后事件 assets.planned）。
 * @param projectId 项目 ID
 * @returns 受理回执
 * @throws ApiError 10002 项目尚无剧本
 */
export function planAssets(projectId: string | number): Promise<AssetAcceptedReceipt> {
  return http.post<AssetAcceptedReceipt>(`/projects/${projectId}/assets/plan`)
}

/**
 * 分页查询项目资产列表。
 * @param projectId 项目 ID
 * @param params 类型 / 状态筛选与分页参数
 * @returns 分页资产列表
 */
export function listAssets(projectId: string | number, params?: ListAssetsParams): Promise<AssetPage<AssetItem>> {
  return http.get<AssetPage<AssetItem>>(`/projects/${projectId}/assets`, { params })
}

/**
 * 查询资产详情（含版本 / QC 报告 / 音色 / 引用镜头数）。
 * @param assetId 资产 ID
 * @returns 资产详情
 * @throws ApiError 10002 资产不存在；20003 无权限
 */
export function getAsset(assetId: string | number): Promise<AssetDetail> {
  return http.get<AssetDetail>(`/assets/${assetId}`)
}

/**
 * 发起资产生成（202 后台 stub 文生图 + 伪向量 QC；状态 planning/pending_review/approved/rejected/qc_failed 可发起）。
 * @param assetId 资产 ID
 * @param payload 版本类型与可选调整要求
 * @returns 受理回执
 * @throws ApiError 10001 版本类型不匹配；40001 生成中 / 已锁定；20003 无权限
 */
export function generateAsset(
  assetId: string | number,
  payload: { version_type: AssetVersionType; feedback?: string | null },
): Promise<AssetAcceptedReceipt> {
  return http.post<AssetAcceptedReceipt>(`/assets/${assetId}/generate`, payload)
}

/**
 * 重新生成资产（同 generate，产生新版本且旧版本保留）。
 * @param assetId 资产 ID
 * @param payload 版本类型与可选反馈（拼入生成 prompt）
 * @returns 受理回执
 * @throws ApiError 同 generate
 */
export function regenerateAsset(
  assetId: string | number,
  payload: { version_type: AssetVersionType; feedback?: string | null },
): Promise<AssetAcceptedReceipt> {
  return http.post<AssetAcceptedReceipt>(`/assets/${assetId}/regenerate`, payload)
}

/**
 * 审核通过资产（pending_review → approved）。
 * @param assetId 资产 ID
 * @returns 更新后的资产
 * @throws ApiError 40001 状态不允许（仅 pending_review）
 */
export function approveAsset(assetId: string | number): Promise<AssetItem> {
  return http.post<AssetItem>(`/assets/${assetId}/approve`)
}

/**
 * 驳回资产（pending_review → rejected，反馈存 meta.reject_feedback）。
 * @param assetId 资产 ID
 * @param feedback 驳回原因（≥10 字）
 * @returns 更新后的资产
 * @throws ApiError 10001 反馈不足 10 字；40001 状态不允许
 */
export function rejectAsset(assetId: string | number, feedback: string): Promise<AssetItem> {
  return http.post<AssetItem>(`/assets/${assetId}/reject`, { feedback })
}

/**
 * 锁定资产作为生成锚点（角色须 portrait + 三视图齐全且 QC 通过）。
 * @param assetId 资产 ID
 * @returns 锁定后的资产
 * @throws ApiError 60003 缺版本 / QC 未通过（data.missing 与 data.qc_failed 均为版本类型列表，一次锁定汇总全部缺失项）；40001 已锁定
 */
export function lockAsset(assetId: string | number): Promise<AssetItem> {
  return http.post<AssetItem>(`/assets/${assetId}/lock`)
}

/**
 * 查询资产版本列表（版本号倒序，含媒体 URL）。
 * @param assetId 资产 ID
 * @returns 版本列表与总数
 */
export function listAssetVersions(assetId: string | number): Promise<{ items: AssetVersionItem[]; total: number }> {
  return http.get<{ items: AssetVersionItem[]; total: number }>(`/assets/${assetId}/versions`)
}

/**
 * 为角色资产绑定 TTS 音色（音色须存在且 available）。
 * @param assetId 资产 ID
 * @param voiceId 音色 ID
 * @returns 更新后的资产
 * @throws ApiError 10001 非角色 / 音色不可用；10002 音色不存在
 */
export function bindAssetVoice(assetId: string | number, voiceId: number): Promise<AssetItem> {
  return http.put<AssetItem>(`/assets/${assetId}/voice`, { voice_id: voiceId })
}

/**
 * 分页查询音色库（默认仅可用音色，且排除自动化测试音色）。
 * @param params 状态/性别筛选（传空串查全部）、是否排除测试音色与分页参数
 * @returns 分页音色列表
 */
export function listTtsVoices(params?: {
  status?: string
  gender?: string
  exclude_test?: boolean
  page?: number
  page_size?: number
}): Promise<AssetPage<TtsVoice>> {
  return http.get<AssetPage<TtsVoice>>('/tts-voices', { params })
}

/**
 * 查询单个音色详情（含持久化样音、绑定角色与引擎音色映射）。
 * @param voiceId 音色 ID（tts_voices.id）
 * @returns 音色详情
 * @throws ApiError 资源不存在（40004）/ 登录失效（20001/20002）
 */
export function getTtsVoice(voiceId: string | number): Promise<TtsVoiceDetail> {
  return http.get<TtsVoiceDetail>(`/tts-voices/${voiceId}`)
}

/**
 * 实时合成音色试听样音（不写入配音时间线）。
 * @param voiceId 音色 ID
 * @param payload 试听文本、语速与音调（均可省略）
 * @returns 试听结果（含 audio_url / duration_ms / sandbox）
 * @throws ApiError 音色不存在（40004）；音色下架 / 文本非法（10001）；模型不可用（50000 级）
 */
export function previewTtsVoice(
  voiceId: string | number,
  payload: VoicePreviewPayload = {},
): Promise<VoicePreviewResult> {
  return http.post<VoicePreviewResult>(`/tts-voices/${voiceId}/preview`, payload)
}

/**
 * 重新生成音色样音并持久化到音色库（刷新后仍可播放）。
 * @param voiceId 音色 ID
 * @param payload 试听文本、语速与音调（均可省略）
 * @returns 新样音结果（persisted=true）
 * @throws ApiError 音色不存在（40004）；音色下架 / 文本非法（10001）；模型不可用（50000 级）
 */
export function regenerateTtsVoiceSample(
  voiceId: string | number,
  payload: VoicePreviewPayload = {},
): Promise<VoicePreviewResult> {
  return http.post<VoicePreviewResult>(`/tts-voices/${voiceId}/regenerate`, payload)
}

/** 真人照片授权记录（后端 face_authorization_out 形状） */
export interface FaceAuthorizationRecord {
  /** 授权记录 ID */
  id: number
  /** 关联资产 ID */
  asset_id: number
  /** 所属项目 ID */
  project_id: number | null
  /** 授权类型（self/authorized_other） */
  authorization_type: string
  /** 协议版本 */
  protocol_version: string
  /** 客户端 IP（非法 IP 为 null） */
  ip: string | null
  /** 授权时间 */
  authorized_at: string | null
}

/**
 * 写入真人照片授权留痕（IP / UA / 协议版本 / 时间戳由后端存证）。
 * @param assetId 资产 ID
 * @param payload 授权类型与协议版本
 * @returns 授权记录
 * @throws ApiError 10001 授权类型非法；10002 资产不存在
 */
export function createFaceAuthorization(
  assetId: string | number,
  payload: { authorization_type: 'self' | 'authorized_other'; agreement_version: string },
): Promise<FaceAuthorizationRecord> {
  return http.post<FaceAuthorizationRecord>(`/assets/${assetId}/face-authorization`, payload)
}

/**
 * 统计资产作为锚点出现在多少镜头。
 * @param assetId 资产 ID
 * @returns 影响镜头数
 */
export function getAssetAnchorImpact(assetId: string | number): Promise<{ asset_id: number; affected_shots: number }> {
  return http.get<{ asset_id: number; affected_shots: number }>(`/assets/${assetId}/anchor-impact`)
}

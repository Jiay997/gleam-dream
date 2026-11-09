/**
 * 文件级注释：剧本域 API（P05-P08 剧本链路，对齐 backend/app/api/scripts.py 契约）。
 * 说明：scripts.py 由 Task 7a 并行实现中，本文件按既定契约先行编写，联调若不一致以后端为准微调。
 * 端点清单：
 * - GET  /projects/{id}/scripts/current          当前剧本全树（script/episodes/scenes/lines）；
 * - GET  /projects/{id}/script-outline           分集大纲（集-场树 + 时长 + 钩子）；
 * - PATCH /script-lines/{line_id}                行内编辑（content/speaker/emotion）；
 * - POST /projects/{id}/scripts/rewrite          局部改写（episode_no/scene_no/feedback）；
 * - GET  /scripts/{id}/diff?against=             版本差异（三色行渲染）；
 * - POST /scripts/{id}/submit-review             提交剧本审核；
 * - POST /projects/{id}/scripts/upload           上传 txt/md 解析为剧本（multipart）；
 * - GET  /documents/{id}                         解析文档状态与整体置信度；
 * - GET  /documents/{id}/confidence-items        逐行解析置信度明细；
 * - POST /orchestration/projects/{id}/script-generate  发起剧本生成（202 异步任务，进 orchestration 域）。
 */
import { http } from './http'

/* ---------- 剧本树类型 ---------- */

/** 剧本行节点（dialogue/narration/action/stage_direction 四类） */
export interface ScriptLineNode {
  /** 行 ID */
  id: string | number
  /** 行类型（dialogue/narration/action/stage_direction；兼容 type 字段） */
  line_type?: string
  /** 行类型（兼容字段名 type） */
  type?: string
  /** 行序号 */
  line_no?: number
  /** 行序号（兼容字段名 index） */
  index?: number
  /** 说话角色（兼容 character 字段） */
  speaker?: string | null
  /** 说话角色（兼容字段名 character） */
  character?: string | null
  /** 行内容 */
  content?: string
  /** 情绪标注 */
  emotion?: string | null
  /** 解析 / 生成置信度（0~1，<0.75 视为低置信） */
  confidence?: number | null
}

/** 场次节点 */
export interface SceneNode {
  /** 场次 ID */
  id: string | number
  /** 场次序号（兼容 index） */
  scene_no?: number
  /** 场次序号（兼容字段名 index） */
  index?: number
  /** 场头（如「内景 · 咖啡馆 - 日」） */
  heading?: string | null
  /** 地点 */
  location?: string | null
  /** 内外景（内景/外景） */
  int_ext?: string | null
  /** 时段（日/夜/黄昏/清晨） */
  time_of_day?: string | null
  /** 场景摘要 */
  summary?: string | null
  /** 场次下剧本行 */
  lines?: ScriptLineNode[]
}

/** 剧集节点 */
export interface EpisodeNode {
  /** 剧集 ID */
  id: string | number
  /** 集数序号（兼容 index） */
  episode_no?: number
  /** 集数序号（兼容字段名 index） */
  index?: number
  /** 集标题 */
  title?: string | null
  /** 一句话梗概 */
  logline?: string | null
  /** 结尾钩子 */
  hook_text?: string | null
  /** 目标时长（秒） */
  target_duration_seconds?: number | null
  /** 场次列表 */
  scenes?: SceneNode[]
}

/** 当前剧本全树（GET /projects/{id}/scripts/current） */
export interface ScriptTree {
  /** 剧本元信息 */
  script: {
    /** 剧本 ID */
    id: string | number
    /** 版本号（兼容 version） */
    version_no?: number
    /** 版本号（兼容字段名 version） */
    version?: number
    /** 剧本标题 */
    title?: string | null
    /** 一句话故事 */
    logline?: string | null
    /** 剧本状态 */
    status?: string
    /** 总字数 */
    word_count?: number
    /** 更新时间 */
    updated_at?: string | null
  }
  /** 剧集列表 */
  episodes: EpisodeNode[]
}

/* ---------- 分集大纲类型 ---------- */

/** 大纲场次行 */
export interface OutlineScene {
  /** 场次序号（兼容 index） */
  scene_no?: number
  /** 场次序号（兼容字段名 index） */
  index?: number
  /** 场头 */
  heading?: string | null
  /** 地点 */
  location?: string | null
  /** 内外景 */
  int_ext?: string | null
  /** 时段 */
  time_of_day?: string | null
  /** 场景摘要 */
  summary?: string | null
  /** 剧本行数 */
  line_count?: number
  /** 镜头数（预留） */
  shot_count?: number
}

/** 大纲剧集 */
export interface OutlineEpisode {
  /** 集数序号（兼容 index） */
  episode_no?: number
  /** 集数序号（兼容字段名 index） */
  index?: number
  /** 集标题 */
  title?: string | null
  /** 一句话梗概 */
  logline?: string | null
  /** 集摘要 */
  summary?: string | null
  /** 结尾钩子 */
  hook_text?: string | null
  /** 目标时长（秒） */
  target_duration_seconds?: number | null
  /** 目标时长（秒，兼容字段名 target_duration_sec） */
  target_duration_sec?: number | null
  /** 场次列表 */
  scenes?: OutlineScene[]
}

/** 分集大纲响应（GET /projects/{id}/script-outline） */
export interface ScriptOutlineData {
  /** 剧集列表 */
  episodes: OutlineEpisode[]
}

/* ---------- 文档解析类型 ---------- */

/** 上传解析文档（GET /documents/{id}） */
export interface ScriptDocument {
  /** 文档 ID */
  id: string | number
  /** 解析状态（pending/parsing/parsed/failed） */
  parse_status?: string
  /** 整体置信度（0~1） */
  confidence?: number | null
  /** 原始文件名 */
  file_name?: string | null
  /** 解析出的剧本 ID（解析成功后回填） */
  script_id?: string | number | null
  /** 创建时间 */
  created_at?: string | null
}

/** 逐行置信度明细（GET /documents/{id}/confidence-items） */
export interface ConfidenceItem {
  /** 明细 ID */
  id?: string | number
  /** 关联剧本行 ID */
  line_id?: string | number
  /** 置信度（0~1） */
  confidence?: number
  /** 行内容 */
  content?: string
  /** 集数 */
  episode_no?: number
  /** 场次号 */
  scene_no?: number
}

/* ---------- 改写 / 差异类型 ---------- */

/** 局部改写响应（完成后需重新拉树） */
export interface RewriteResult {
  /** 受理标记 */
  accepted?: boolean
  /** 关联剧本 ID */
  script_id?: string | number
  /** 附加信息 */
  message?: string
  [key: string]: unknown
}

/** 差异行（type/kind 归一化前兼容两种字段名） */
export interface DiffRow {
  /** 行类别（added/removed/changed/context） */
  type?: string
  /** 行类别（兼容字段名 kind） */
  kind?: string
  /** 新内容 */
  content?: string
  /** 旧内容 */
  old_content?: string
  /** 新内容（兼容字段名 new_content） */
  new_content?: string
  /** 行号 */
  line_no?: number
  /** 场次号（可选定位） */
  scene_no?: number
}

/* ---------- 生成受理类型 ---------- */

/** 剧本生成受理结果（POST /orchestration/projects/{id}/script-generate，HTTP 202） */
export interface ScriptGenerateAccepted {
  /** 项目 ID */
  project_id: number | string
  /** 受理后项目状态（scripting） */
  status?: string
  /** 预估消耗（积分） */
  estimated_credits?: number
  /** 受理的创意原文 */
  idea?: string | null
  /** 受理提示 */
  message?: string
  [key: string]: unknown
}

/* ---------- API 函数 ---------- */

/**
 * 获取项目当前剧本全树（script/episodes/scenes/lines）。
 * @param projectId 项目 ID
 * @returns 剧本树（项目尚无剧本时后端可能返回 404/空，由调用方降级处理）
 * @throws ApiError 40004 无剧本时由拦截器统一提示
 */
export function getProjectCurrentScript(projectId: string | number): Promise<ScriptTree> {
  return http.get<ScriptTree>(`/projects/${projectId}/scripts/current`)
}

/**
 * 获取分集大纲（集-场树 + 时长 + 钩子）。
 * @param projectId 项目 ID
 * @returns 大纲数据
 */
export function getScriptOutline(projectId: string | number): Promise<ScriptOutlineData> {
  return http.get<ScriptOutlineData>(`/projects/${projectId}/script-outline`)
}

/**
 * 行内编辑：更新剧本行内容 / 说话人 / 情绪。
 * @param lineId 行 ID
 * @param payload 更新字段（PATCH 语义，仅传字段生效）
 * @returns 更新后的行
 */
export function updateScriptLine(
  lineId: string | number,
  payload: { content?: string; speaker?: string; emotion?: string },
): Promise<ScriptLineNode> {
  return http.patch<ScriptLineNode>(`/script-lines/${lineId}`, payload)
}

/**
 * 局部改写：按集 / 场（可选行）携带反馈触发 AI 重写。
 * @param projectId 项目 ID
 * @param payload 集号 / 场次号（可选）/ 反馈（≥5 字）
 * @returns 受理结果（完成后需重新拉取剧本树）
 */
export function rewriteScript(
  projectId: string | number,
  payload: { episode_no: number; scene_no?: number; feedback: string },
): Promise<RewriteResult> {
  return http.post<RewriteResult>(`/projects/${projectId}/scripts/rewrite`, payload)
}

/**
 * 获取剧本版本差异。
 * @param scriptId 剧本 ID
 * @param against 对比基准（版本号 / 版本 ID，由后端约定）
 * @returns 差异行列表
 */
export function getScriptDiff(scriptId: string | number, against: string | number): Promise<DiffRow[]> {
  return http.get<DiffRow[]>(`/scripts/${scriptId}/diff`, { params: { against } })
}

/**
 * 提交剧本进入审核流（HITL-1 前置动作）。
 * 后端同步执行 LLM 合规审核（flag 时还会逐场弱化改写并二审），
 * 全剧本审核可达 1-3 分钟，故使用 5 分钟请求级超时覆盖全局 15s 默认值。
 * @param scriptId 剧本 ID
 * @returns 提交结果
 */
export function submitScriptReview(scriptId: string | number): Promise<Record<string, unknown>> {
  return http.post<Record<string, unknown>>(`/scripts/${scriptId}/submit-review`, undefined, {
    timeout: 300000,
  })
}

/**
 * 上传 txt/md 文档解析为剧本（multipart/form-data）。
 * @param projectId 项目 ID
 * @param file 用户选择的文件（txt/md）
 * @returns 解析文档（含 parse_status，需轮询直至 parsed/failed）
 */
export function uploadScriptDocument(projectId: string | number, file: File): Promise<ScriptDocument> {
  const form = new FormData()
  form.append('file', file)
  return http.post<ScriptDocument>(`/projects/${projectId}/scripts/upload`, form)
}

/**
 * 查询解析文档状态与整体置信度（上传后轮询直至 parse_status=parsed/failed）。
 * @param documentId 文档 ID
 * @returns 文档解析信息
 */
export function getScriptDocument(documentId: string | number): Promise<ScriptDocument> {
  return http.get<ScriptDocument>(`/documents/${documentId}`)
}

/**
 * 查询解析文档的逐行置信度明细（低置信行高亮数据源）。
 * @param documentId 文档 ID
 * @returns 置信度明细列表
 */
export function getDocumentConfidenceItems(documentId: string | number): Promise<ConfidenceItem[]> {
  return http.get<ConfidenceItem[]>(`/documents/${documentId}/confidence-items`)
}

/**
 * 发起剧本生成（202 异步任务；进度经项目 SSE ``progress`` 事件推送）。
 * 端点位于编排域：POST /orchestration/projects/{id}/script-generate（body 仅 idea，extra=forbid）。
 * @param projectId 项目 ID
 * @param idea 一句话创意（缺省时后端取项目 synopsis）
 * @returns 受理信息（含 estimated_credits）
 * @throws ApiError 40001 已有执行中流程 / 状态不允许；30001 积分不足
 */
export function generateScript(projectId: string | number, idea?: string): Promise<ScriptGenerateAccepted> {
  return http.post<ScriptGenerateAccepted>(`/orchestration/projects/${projectId}/script-generate`, { idea: idea || undefined })
}

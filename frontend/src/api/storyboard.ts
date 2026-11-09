/**
 * 文件级注释：分镜域 API（对齐 backend/app/api/storyboards.py + schemas/storyboard.py 真实契约）。
 * 端点清单：
 * - POST /projects/{project_id}/storyboards/generate  发起分镜生成（202 异步；同集幂等 200）；
 * - GET  /projects/{project_id}/storyboards           项目分镜板列表（按集升序，含镜头数/状态/时长）；
 * - GET  /storyboards/{storyboard_id}                 分镜板详情（含 shots 与首帧 media_url）；
 * - PATCH /shots/{shot_id}                            镜头局部编辑（image_prompt/shot_size/camera_move/duration_ms/emotion_tag）；
 * - POST /storyboards/{storyboard_id}/shots/reorder   重排镜头（order_index 全量重算 + 钩子位移提示）；
 * - POST /shots/{shot_id}/anchors                     绑定镜头锚点（资产须 locked）→ 返回组装后 final_prompt；
 * - POST /storyboards/{storyboard_id}/lock            锁定分镜（缺锚点 60005 附清单）→ 产生 HITL-3 审批单；
 * - POST /storyboards/{storyboard_id}/decide          分镜审批决策（approve → 项目 rendering，202）；
 * - POST /storyboards/{storyboard_id}/layout          画布布局保存（按用户维度 upsert；后端暂无读取端点）。
 */
import { http } from './http'

/* ==================== 类型定义（与后端 shot_out / storyboard_out 序列化对齐） ==================== */

/** 分镜板摘要（列表接口输出，episode_no 由列表接口携带） */
export interface StoryboardSummary {
  /** 分镜板 ID */
  id: number | string
  /** 项目 ID */
  project_id: number | string
  /** 关联剧集 ID */
  episode_id: number | string
  /** 分镜板版本号 */
  version_no?: number
  /** 分镜板状态（pending_review / locked 等） */
  status: string
  /** 镜头总数 */
  shot_count: number
  /** 预估总时长（毫秒） */
  estimated_duration_ms: number
  /** 锁定时间（ISO，未锁定为 null） */
  locked_at?: string | null
  /** 创建时间 */
  created_at?: string | null
  /** 集号（列表接口携带） */
  episode_no?: number
}

/** 镜头（GET /storyboards/{id} 的 shots 数组元素；media_url 为当前首帧 URL 或 null） */
export interface ShotItem {
  /** 镜头 ID */
  id: number | string
  /** 所属分镜板 ID */
  storyboard_id: number | string
  /** 镜号（分镜板内唯一，≥1） */
  shot_no: number
  /** 展示序（≥0，重排后重算） */
  order_index: number
  /** 景别（extreme_wide/wide/medium/close_up/extreme_close_up/over_shoulder） */
  shot_size?: string | null
  /** 运镜（static/pan/tilt/dolly_in/dolly_out/tracking/orbit/handheld/crane） */
  camera_move?: string | null
  /** 情绪标签（calm/joy/anger/sadness/fear/surprise/tension/romance） */
  emotion_tag?: string | null
  /** 时长（毫秒，1000~15000） */
  duration_ms: number
  /** 对白 */
  dialogue?: string | null
  /** 动作/画面描述 */
  action_desc?: string | null
  /** 关键帧 prompt（可编辑） */
  image_prompt?: string | null
  /** 最终组装 prompt（绑定锚点后由后端生成） */
  final_prompt?: string | null
  /** 锚点资产版本 ID 列表 */
  asset_anchor_ids?: Array<number | string>
  /** 出场角色资产 ID 列表 */
  character_asset_ids?: Array<number | string>
  /** 场景资产 ID（可空） */
  scene_asset_id?: number | string | null
  /** 镜头状态（pending_review / locked 等） */
  status: string
  /** 锁定时间（ISO） */
  locked_at?: string | null
  /** 创建时间 */
  created_at?: string | null
  /** 当前首帧媒体 URL（详情接口携带，无首帧为 null） */
  media_url?: string | null
  /**
   * 最近一轮质检结论（详情接口携带，未渲染过的镜头可能没有）。
   * mismatch=true 表示画面与剧本锚点对不上（如动作/道具未进入提示词），
   * 分镜页据此标红提示人工复核。
   */
  anchor_qc?: {
    /** 质检结论 passed/warning/failed */
    result: string
    /** 质检轮次 */
    round_no: number
    /** 是否命中剧本锚点失配 */
    mismatch: boolean
    /** 失配原因（可直接展示给用户） */
    reasons: string[]
    /** 生成期规则告警（如场景资产未匹配 scene_unbound） */
    warnings?: string[]
    /** 多模态视觉比对结果（开启时存在） */
    vision?: { score?: number; description?: string; missing?: string[] } | null
  } | null
}

/** 分镜板详情（storyboard_out + shots 数组） */
export interface StoryboardDetail extends Omit<StoryboardSummary, 'episode_no'> {
  /** 镜头列表（按 order_index 升序） */
  shots: ShotItem[]
  /** 集号（列表接口携带，详情接口无） */
  episode_no?: number
}

/** 分镜生成响应（新发起 202 / 幂等命中 200） */
export interface GenerateStoryboardResult {
  /** 是否幂等命中（true 表示该集已有分镜板） */
  idempotent: boolean
  /** 幂等命中时返回已有分镜板 */
  storyboard?: StoryboardSummary
  /** 受理 / 幂等提示文案 */
  message?: string
  /** 受理状态（accepted） */
  status?: string
  /** 请求的集号 */
  episode_no?: number
  /** 资产就绪度（受理回执，用于提示未锁定资产数量） */
  asset_readiness?: {
    /** 资产总数 */
    total: number
    /** 已锁定数 */
    locked: number
    /** 未锁定数 */
    unlocked: number
    /** 是否全部锁定 */
    ready: boolean
  }
}

/** 重排响应：order 重算结果 + 钩子位移提示 */
export interface ReorderResult {
  /** 重排后本集总时长（毫秒） */
  episode_duration_ms: number
  /** 结尾钩子镜头是否因重排发生位移 */
  hook_position_changed: boolean
  /** 重排后的 {shot_id, order_index} 列表 */
  shots: Array<{ shot_id: number | string; order_index: number }>
}

/** 锚点绑定响应 */
export interface AnchorBindResult {
  /** 镜头 ID */
  shot_id: number | string
  /** 绑定后的角色资产 ID 列表 */
  character_asset_ids: Array<number | string>
  /** 绑定后的场景资产 ID */
  scene_asset_id: number | string | null
  /** 锚点资产版本 ID 列表 */
  asset_anchor_ids: Array<number | string>
  /** 组装后的 final_prompt */
  final_prompt: string
}

/** 锁定分镜响应 */
export interface LockStoryboardResult {
  /** 锁定后的分镜板 */
  storyboard: StoryboardSummary
  /** 创建的审批单 ID（HITL-3） */
  approval_id: number | string
  /** 提示文案 */
  message: string
}

/** 分镜审批决策响应（202） */
export interface StoryboardDecisionResult {
  /** 审批单 ID */
  approval_id: number | string
  /** 决策动作 */
  action: 'approve' | 'reject'
  /** 分镜板 ID */
  storyboard_id: number | string
  /** 决策后项目状态（approve → rendering） */
  project_status?: string | null
}

/** 画布布局节点（保存请求体元素） */
export interface LayoutNodePayload {
  /** 镜头 ID */
  shot_id: number | string
  /** X 坐标 */
  x: number
  /** Y 坐标 */
  y: number
  /** 节点宽度 */
  w: number
  /** 节点高度 */
  h: number
}

/* ==================== API 方法 ==================== */

/** 分镜生成的可选控制参数（P1-7：镜头数 / 总时长 / 单镜上限由用户决定） */
export interface GenerateStoryboardOptions {
  /** 节奏模式（fast/balanced/slow） */
  pacing_mode?: 'fast' | 'balanced' | 'slow'
  /** 目标镜头数（3~60） */
  target_shot_count?: number
  /** 目标单集时长（毫秒，10s~10min） */
  target_duration_ms?: number
  /** 单镜头时长上限（毫秒，1s~15s） */
  max_shot_duration_ms?: number
}

/**
 * 发起分镜生成（202 后台 LLM 拆镜；同集已有分镜板时幂等返回 200）。
 * @param projectId 项目 ID
 * @param episodeNo 集号（须为当前剧本已存在的分集）
 * @param options 可选控制参数（节奏 / 目标镜头数 / 目标总时长 / 单镜上限）
 * @returns 受理 / 幂等结果
 * @throws ApiError 10002 项目尚无剧本或集号不存在
 */
export function generateStoryboard(
  projectId: string | number,
  episodeNo: number,
  options: GenerateStoryboardOptions = {},
): Promise<GenerateStoryboardResult> {
  return http.post<GenerateStoryboardResult>(`/projects/${projectId}/storyboards/generate`, {
    episode_no: episodeNo,
    ...options,
  })
}

/**
 * 查询项目分镜板列表（按集号升序，含镜头数/状态/时长合计）。
 * @param projectId 项目 ID
 * @returns 分镜板摘要列表
 */
export async function listStoryboards(projectId: string | number): Promise<StoryboardSummary[]> {
  const data = await http.get<{ items: StoryboardSummary[] }>(`/projects/${projectId}/storyboards`)
  return data.items ?? []
}

/**
 * 查询分镜板详情（含 shots 数组与首帧 media_url）。
 * @param storyboardId 分镜板 ID
 * @returns 分镜板详情
 * @throws ApiError 10002 分镜板不存在
 */
export function getStoryboardDetail(storyboardId: number | string): Promise<StoryboardDetail> {
  return http.get<StoryboardDetail>(`/storyboards/${storyboardId}`)
}

/**
 * 局部编辑镜头（PATCH 语义，仅显式传入字段生效；duration_ms 1000~15000）。
 * @param shotId 镜头 ID
 * @param payload 更新字段（shot_size/camera_move/emotion_tag/duration_ms/image_prompt）
 * @returns 更新后的镜头
 * @throws ApiError 10001 参数非法；40001 镜头已锁定
 */
export function patchShot(
  shotId: number | string,
  payload: Partial<Pick<ShotItem, 'image_prompt' | 'shot_size' | 'camera_move' | 'duration_ms' | 'emotion_tag'>>,
): Promise<ShotItem> {
  return http.patch<ShotItem>(`/shots/${shotId}`, payload)
}

/**
 * 重排镜头（order_index 全量重算）并返回钩子位移提示。
 * @param storyboardId 分镜板 ID
 * @param shotId 要移动的镜头 ID
 * @param anchor 插入锚点：{ insert_before_shot_id } 或 { insert_after_shot_id } 二选一
 * @returns 重排结果
 * @throws ApiError 40001 分镜已锁定
 */
export function reorderShots(
  storyboardId: number | string,
  shotId: number | string,
  anchor: { insert_before_shot_id?: number | string; insert_after_shot_id?: number | string },
): Promise<ReorderResult> {
  return http.post<ReorderResult>(`/storyboards/${storyboardId}/shots/reorder`, { shot_id: shotId, ...anchor })
}

/**
 * 绑定镜头锚点（角色资产全部须 locked；场景可空）→ 返回组装后 final_prompt。
 * @param shotId 镜头 ID
 * @param characterAssetIds 出场角色资产 ID 列表
 * @param sceneAssetId 场景资产 ID（可空）
 * @returns 绑定结果
 * @throws ApiError 50001 资产未锁定；40001 镜头已锁定
 */
export function bindShotAnchors(
  shotId: number | string,
  characterAssetIds: Array<number | string>,
  sceneAssetId?: number | string | null,
): Promise<AnchorBindResult> {
  return http.post<AnchorBindResult>(`/shots/${shotId}/anchors`, {
    character_asset_ids: characterAssetIds,
    scene_asset_id: sceneAssetId ?? null,
  })
}

/**
 * 锁定分镜（全部镜头须至少 1 个锚点，缺则 60005 附缺锚点清单）→ 产生 HITL-3 审批单。
 * @param storyboardId 分镜板 ID
 * @returns 锁定结果（含审批单 ID）
 * @throws ApiError 60005 存在缺锚点镜头（data.missing_shots 为清单）
 */
export function lockStoryboard(storyboardId: number | string): Promise<LockStoryboardResult> {
  return http.post<LockStoryboardResult>(`/storyboards/${storyboardId}/lock`)
}

/**
 * 分镜审批决策（approve → 项目流转 rendering；reject 需 feedback ≥10 字）。
 * @param storyboardId 分镜板 ID
 * @param action 决策动作
 * @param feedback 反馈原文（reject 必填）
 * @returns 决策回执（202）
 */
export function decideStoryboard(
  storyboardId: number | string,
  action: 'approve' | 'reject',
  feedback?: string,
): Promise<StoryboardDecisionResult> {
  return http.post<StoryboardDecisionResult>(`/storyboards/${storyboardId}/decide`, { action, feedback })
}

/**
 * 保存分镜画布布局（按用户维度 upsert；x/y 为画布世界坐标）。
 * @param storyboardId 分镜板 ID
 * @param nodes 布局节点列表
 * @param viewport 视口状态 {x, y, zoom}（可空）
 * @returns 保存回执
 */
export function saveStoryboardLayout(
  storyboardId: number | string,
  nodes: LayoutNodePayload[],
  viewport?: { x: number; y: number; zoom: number },
): Promise<{ node_count: number; saved: boolean }> {
  return http.post(`/storyboards/${storyboardId}/layout`, { nodes, viewport })
}

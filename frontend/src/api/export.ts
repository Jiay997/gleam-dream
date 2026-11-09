/**
 * 文件级注释：导出与分发域 API（与《域 J · 导出与分发》契约对齐）。
 * - GET    /projects/{id}/exports          导出任务列表；
 * - POST   /projects/{id}/exports          创建导出任务（70003 权益不足由拦截器统一提示）；
 * - GET    /exports/{id}                   导出任务详情（含下载 url）；
 * - POST   /projects/{id}/exports/batch    批量导出（全部剧集打包 zip）；
 * - POST   /projects/{id}/exports/metadata AI 生成发行元数据（标题/简介/话题标签/封面建议）；
 * - GET    /platform-accounts              平台授权账号列表；
 * - POST   /platform-accounts              平台授权（沙箱 mock_authorize=true 直接成功）；
 * - DELETE /platform-accounts/{id}         解除平台授权；
 * - POST   /projects/{id}/distribute       一键分发到平台；
 * - GET    /projects/{id}/distributions    分发记录列表。
 */
import { http } from './http'

/** 导出任务（export_jobs 输出形状） */
export interface ExportJobItem {
  /** 任务 ID */
  id: number | string
  /** 项目 ID */
  project_id: number | string
  /** 时间线 ID（可空） */
  timeline_id?: number | string | null
  /** 分辨率（720p/1080p/4k） */
  resolution: string
  /** 是否带水印 */
  has_watermark: boolean
  /** 导出格式（mp4/mov/zip） */
  format: string
  /** 编码参数 */
  params?: Record<string, unknown> | null
  /** 状态（pending/running/succeeded/failed） */
  status: string
  /** 进度 0~100 */
  progress: number
  /** 产物大小（字节） */
  output_size_bytes?: number | null
  /** 失败原因 */
  error_reason?: string | null
  /** 开始时间 */
  started_at?: string | null
  /** 完成时间 */
  finished_at?: string | null
  /** 创建时间 */
  created_at?: string | null
  /** 下载地址（succeeded 时返回，GET /exports/{id} 详情含 url） */
  download_url?: string | null
  /** 下载地址（兼容字段名 url） */
  url?: string | null
  /** 产物信息（列表与详情接口均返回，含可播放/下载地址） */
  output?: {
    /** 媒体文件 ID */
    media_file_id: number
    /** 存储对象键 */
    object_key: string
    /** 媒体类型 */
    media_type: string
    /** 文件大小（字节） */
    size_bytes: number
    /** 下载/播放地址 */
    download_url: string
  } | null
}

/** 创建导出任务请求体 */
export interface ExportCreatePayload {
  /** 分辨率（720p/1080p/4k） */
  resolution: string
  /** 是否带水印（缺省由服务端按权益决定） */
  with_watermark?: boolean
  /** 导出集数（缺省导出全部） */
  episode_no?: number
}

/** 批量导出请求体（全部剧集打包 zip） */
export interface ExportBatchPayload {
  /** 分辨率（720p/1080p/4k） */
  resolution: string
  /** 是否带水印 */
  with_watermark: boolean
}

/** AI 生成的发行元数据 */
export interface ExportMetadataResult {
  /** 建议标题 */
  title: string
  /** 建议简介 */
  description: string
  /** 建议话题标签列表 */
  tags: string[]
  /** 封面建议（帧描述列表，取一作为分发封面） */
  cover_suggestions: string[]
}

/** 平台授权账号（platform_accounts 输出形状） */
export interface PlatformAccountItem {
  /** 账号 ID */
  id: number | string
  /** 平台标识（douyin/kuaishou/wechat_channels/jianying） */
  platform: string
  /** 平台用户标识（沙箱为 mock 账号名） */
  platform_user_id?: string | null
  /** 授权范围 */
  scope?: string | null
  /** 状态（active/expired/revoked） */
  status: string
  /** 授权时间 */
  authorized_at?: string | null
  /** token 到期时间 */
  token_expires_at?: string | null
}

/** 平台授权请求体 */
export interface PlatformAccountCreatePayload {
  /** 平台标识 */
  platform: string
  /** 沙箱模拟授权（固定 true） */
  mock_authorize?: boolean
}

/** 分发记录（distribution_records 输出形状） */
export interface DistributionRecordItem {
  /** 记录 ID */
  id: number | string
  /** 项目 ID */
  project_id: number | string
  /** 关联导出任务 ID */
  export_job_id?: number | string | null
  /** 平台标识 */
  platform: string
  /** 平台视频 ID */
  external_video_id?: string | null
  /** 发布链接 */
  publish_url?: string | null
  /** 发布标题 */
  title?: string | null
  /** 状态（pending/uploading/succeeded/failed） */
  status: string
  /** 失败原因 */
  error_reason?: string | null
  /** 分发时间 */
  distributed_at?: string | null
  /** 创建时间 */
  created_at?: string | null
}

/** 一键分发请求体 */
export interface DistributePayload {
  /** 目标平台标识 */
  platform: string
  /** 发行元数据（title 必填） */
  metadata: { title: string; description?: string; tags?: string[] }
  /** 关联导出任务 ID（可选，未传由服务端取最近成功导出） */
  export_job_id?: number | string
}

/**
 * 获取项目导出任务列表。
 * @param projectId 项目 ID
 * @param params 分页参数
 * @returns 导出任务列表（{ items, total } 或分页形状，兼容消费）
 */
export function listExports(projectId: string | number, params?: { page?: number; page_size?: number }): Promise<{ items: ExportJobItem[]; total: number }> {
  return http.get<{ items: ExportJobItem[]; total: number }>(`/projects/${projectId}/exports`, { params })
}

/**
 * 创建导出任务。
 * @param projectId 项目 ID
 * @param payload 分辨率 / 水印 / 集数参数
 * @returns 新导出任务
 * @throws ApiError 70003 套餐权益不足 / 40001 状态不允许
 */
export function createExport(projectId: string | number, payload: ExportCreatePayload): Promise<ExportJobItem> {
  return http.post<ExportJobItem>(`/projects/${projectId}/exports`, payload)
}

/**
 * 获取导出任务详情（含下载 url）。
 * @param exportId 导出任务 ID
 * @returns 导出任务详情
 */
export function getExport(exportId: string | number): Promise<ExportJobItem> {
  return http.get<ExportJobItem>(`/exports/${exportId}`)
}

/**
 * 批量导出（全部剧集打包为 zip）。
 * @param projectId 项目 ID
 * @param payload 分辨率 / 水印参数
 * @returns 新导出任务（format=zip）
 */
export function batchExport(projectId: string | number, payload: ExportBatchPayload): Promise<ExportJobItem> {
  return http.post<ExportJobItem>(`/projects/${projectId}/exports/batch`, payload)
}

/**
 * AI 生成发行元数据（标题 / 简介 / 话题标签 / 封面建议）。
 * @param projectId 项目 ID
 * @returns 生成的元数据
 */
export function generateExportMetadata(projectId: string | number): Promise<ExportMetadataResult> {
  return http.post<ExportMetadataResult>(`/projects/${projectId}/exports/metadata`)
}

/**
 * 获取当前用户平台授权账号列表。
 *
 * 注意：后端 `GET /platform-accounts` 的 data 形状为 `{items, total}`（分页信封，
 * 见 backend/app/api/export.py::list_platform_accounts），不是裸数组。
 * 本函数负责解包，向调用方保持「返回数组」的旧签名，避免改动消费侧。
 *
 * @returns 授权账号数组
 */
export function listPlatformAccounts(): Promise<PlatformAccountItem[]> {
  return http
    .get<{ items: PlatformAccountItem[]; total: number }>('/platform-accounts')
    .then((page) => page.items)
}

/**
 * 授权平台账号（沙箱 mock_authorize=true 直接授权成功）。
 * @param payload 平台标识与模拟授权标记
 * @returns 新授权账号
 */
export function createPlatformAccount(payload: PlatformAccountCreatePayload): Promise<PlatformAccountItem> {
  return http.post<PlatformAccountItem>('/platform-accounts', payload)
}

/**
 * 解除平台授权。
 * @param accountId 授权账号 ID
 * @returns { id, revoked }
 */
export function deletePlatformAccount(accountId: string | number): Promise<{ id: number | string; revoked: boolean }> {
  return http.delete<{ id: number | string; revoked: boolean }>(`/platform-accounts/${accountId}`)
}

/**
 * 一键分发成片到平台。
 * @param projectId 项目 ID
 * @param payload 平台 / 元数据 / 导出任务
 * @returns 分发记录
 * @throws ApiError 40001 元数据不完整 / 未授权时由拦截器统一提示
 */
export function createDistribution(projectId: string | number, payload: DistributePayload): Promise<DistributionRecordItem> {
  return http.post<DistributionRecordItem>(`/projects/${projectId}/distribute`, payload)
}

/**
 * 获取项目分发记录列表。
 * @param projectId 项目 ID
 * @param params 分页参数
 * @returns 分发记录列表
 */
export function listDistributions(projectId: string | number, params?: { page?: number; page_size?: number }): Promise<{ items: DistributionRecordItem[]; total: number }> {
  return http.get<{ items: DistributionRecordItem[]; total: number }>(`/projects/${projectId}/distributions`, { params })
}

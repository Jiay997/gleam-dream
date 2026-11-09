/**
 * 文件级注释：后端 API DTO 类型定义（snake_case，与 00 契约术语对齐）。
 * 全部属性尽量宽松可选，后端未就绪不影响前端编译；类型仅做消费侧约束。
 */

/** 统一响应信封：{ code, message, data, request_id }，code === 0 表示成功 */
export interface ApiEnvelope<T = unknown> {
  /** 业务码，0 为成功 */
  code: number
  /** 提示消息 */
  message: string
  /** 业务数据 */
  data: T
  /** 链路追踪 ID */
  request_id?: string
}

/** 分页包装 */
export interface Paginated<T> {
  /** 当前页数据 */
  items: T[]
  /** 总条数 */
  total: number
  /** 当前页码 */
  page?: number
  /** 页大小 */
  page_size?: number
}

/** 登录 / 注册结果（POST /auth/login、POST /auth/register） */
export interface LoginResult {
  /** 访问令牌 */
  access_token: string
  /** 刷新令牌 */
  refresh_token?: string
  /** 令牌类型（bearer） */
  token_type?: string
  /** 访问令牌有效期（秒） */
  expires_in?: number
  /** 用户信息 */
  user: User
}

/** 当前用户详情（GET /users/me：用户 + 积分账户摘要） */
export interface UserProfile {
  /** 用户信息（含 preferences） */
  user: User
  /** 积分账户摘要（无账户时为 null） */
  credit: CreditAccount | null
}

/** 用户（后端 _user_brief / users.me 输出） */
export interface User {
  /** 用户 ID */
  id: number | string
  /** 手机号 */
  phone?: string | null
  /** 昵称 */
  nickname?: string | null
  /** 头像 URL */
  avatar_url?: string | null
  /** 账号状态 */
  status?: string
  /** 注册来源（phone/wechat） */
  register_source?: string
  /** 角色（owner/admin/member 等，预留） */
  role?: string
  /** 用户偏好设置（JSONB 浅合并） */
  preferences?: Record<string, unknown> | null
  /** 创建时间 */
  created_at?: string | null
}

/** 项目（后端 project_out 序列化形状） */
export interface Project {
  /** 项目 ID */
  id: number | string
  /** 创建者用户 ID */
  owner_user_id?: number | string
  /** 归属团队 ID */
  team_id?: number | string | null
  /** 项目标题 */
  title: string
  /** 一句话简介 */
  synopsis?: string | null
  /** 题材 */
  genre?: string | null
  /** 画风预设键（live_action/anime/chinese_style/three_d） */
  style_preset_key?: string
  /** 画幅比例（9:16/16:9/1:1） */
  aspect_ratio?: string
  /** 主语言（BCP-47） */
  language?: string
  /** 项目状态（见 statusTone 项目 14 态） */
  status: string
  /** 目标集数（1~30） */
  target_episode_count?: number
  /** 预算上限（积分，0 不限） */
  budget_credits?: number
  /** 已消耗预算（积分） */
  spent_credits?: number
  /** 封面媒体 ID */
  cover_media_id?: number | string | null
  /** 当前剧本 ID */
  current_script_id?: number | string | null
  /** 当前分镜板 ID */
  current_storyboard_id?: number | string | null
  /** 项目级配置 */
  config?: Record<string, unknown> | null
  /** 乐观锁版本号 */
  lock_version?: number
  /** 归档时间 */
  archived_at?: string | null
  /** 创建时间 */
  created_at?: string | null
  /** 更新时间 */
  updated_at?: string | null
}

/** 项目快照（后端 snapshot_out 形状，P05 快照面板 / 回滚消费） */
export interface ProjectSnapshot {
  /** 快照 ID（后端为自增 int） */
  id: string | number
  /** 项目 ID */
  project_id: string
  /** 项目内快照序号（单调递增） */
  sequence_no?: number
  /** 快照名称 */
  label?: string | null
  /** 快照名称（兼容旧字段名 name） */
  name?: string
  /** 触发方式（manual/auto/rollback） */
  trigger?: string
  /** 触发阶段（兼容旧字段名 stage） */
  stage?: string
  /** 关联 LangGraph checkpoint ID */
  checkpoint_id?: string | null
  /** 创建人用户 ID */
  created_by?: number | string | null
  /** 快照体积（字节） */
  size_bytes?: number | null
  /** 完整状态快照（state 摘要模式时缺省） */
  state?: Record<string, unknown> | null
  /** 创建时间 */
  created_at?: string
}

/** 项目时间线事件 */
export interface TimelineEvent {
  /** 事件 ID */
  id: string
  /** 项目 ID */
  project_id: string
  /** 事件类型 */
  event_type?: string
  /** 事件描述 */
  summary?: string
  /** 事件负载 */
  payload?: Record<string, unknown>
  /** 发生时间 */
  created_at?: string
}

/** 剧本 */
export interface Script {
  /** 剧本 ID */
  id: string
  /** 项目 ID */
  project_id: string
  /** 版本号 */
  version?: number
  /** 标题 */
  title?: string
  /** 一句话故事 */
  logline?: string
  /** 剧本状态 */
  status?: string
  /** 剧本正文 */
  content?: string
  /** 创建时间 */
  created_at?: string
  /** 更新时间 */
  updated_at?: string
}

/** 剧集 */
export interface Episode {
  /** 剧集 ID */
  id: string
  /** 所属剧本 ID */
  script_id: string
  /** 集数序号 */
  index?: number
  /** 集标题 */
  title?: string
  /** 本集梗概 */
  synopsis?: string
  /** 目标时长（秒） */
  duration_target?: number
  /** 状态 */
  status?: string
}

/** 场次 */
export interface Scene {
  /** 场次 ID */
  id: string
  /** 所属剧集 ID */
  episode_id: string
  /** 场次序号 */
  index?: number
  /** 场景标题（场头） */
  heading?: string
  /** 地点 */
  location?: string
  /** 时间（日 / 夜） */
  time_of_day?: string
  /** 场景摘要 */
  summary?: string
}

/** 剧本台词 / 动作行 */
export interface ScriptLine {
  /** 行 ID */
  id: string
  /** 所属场次 ID */
  scene_id: string
  /** 行序号 */
  index?: number
  /** 行类型（dialogue/action/parenthetical） */
  type?: string
  /** 角色名 */
  character?: string
  /** 行内容 */
  content?: string
  /** 情绪标注 */
  emotion?: string
}

/** 数字资产（角色 / 场景 / 道具 / 声音） */
export interface Asset {
  /** 资产 ID */
  id: string
  /** 项目 ID */
  project_id: string
  /** 资产类型（character/scene/prop/voice） */
  asset_type?: string
  /** 资产名称 */
  name: string
  /** 资产状态（见 statusTone 资产态） */
  status: string
  /** 预览图 URL */
  preview_url?: string
  /** 资产描述 */
  description?: string
  /** 最新版本 ID */
  latest_version_id?: string
  /** 创建时间 */
  created_at?: string
  /** 更新时间 */
  updated_at?: string
}

/** 资产版本 */
export interface AssetVersion {
  /** 版本 ID */
  id: string
  /** 所属资产 ID */
  asset_id: string
  /** 版本号 */
  version?: number
  /** 成品文件 URL */
  file_url?: string
  /** 缩略图 URL */
  thumbnail_url?: string
  /** 版本状态 */
  status?: string
  /** 质检报告 */
  qc_report?: Record<string, unknown>
  /** 创建时间 */
  created_at?: string
}

/** 形象 / 声音授权记录 */
export interface AssetAuthorization {
  /** 授权 ID */
  id: string
  /** 项目 ID */
  project_id: string
  /** 关联资产 ID */
  asset_id?: string
  /** 授权对象姓名 */
  subject_name?: string
  /** 授权类型（face/voice） */
  authorization_type?: string
  /** 授权书文件 URL */
  consent_file_url?: string
  /** 授权状态 */
  status?: string
  /** 创建时间 */
  created_at?: string
}

/** 分镜板 */
export interface Storyboard {
  /** 分镜板 ID */
  id: string
  /** 项目 ID */
  project_id: string
  /** 关联剧集 ID */
  episode_id?: string
  /** 分镜板状态 */
  status?: string
  /** 镜头总数 */
  shot_count?: number
  /** 创建时间 */
  created_at?: string
  /** 更新时间 */
  updated_at?: string
}

/** 镜头 */
export interface Shot {
  /** 镜头 ID */
  id: string
  /** 项目 ID */
  project_id?: string
  /** 所属场次 ID */
  scene_id?: string
  /** 镜头序号 */
  index?: number
  /** 景别（远景/全景/中景/近景/特写） */
  shot_type?: string
  /** 运镜方式 */
  camera_move?: string
  /** 时长（秒） */
  duration?: number
  /** 画面描述 */
  description?: string
  /** 镜头状态 */
  status?: string
  /** 关联资产 ID 列表 */
  asset_ids?: string[]
  /** 分镜草图 / 成片片段 URL */
  preview_url?: string
  /** 版本号 */
  version?: number
}

/** 渲染 / 生成任务 */
export interface RenderTask {
  /** 任务 ID */
  id: string
  /** 项目 ID */
  project_id: string
  /** 关联镜头 ID */
  shot_id?: string
  /** 任务类型（shot_image/shot_video/compose 等） */
  task_type?: string
  /** 任务状态（见 statusTone 任务态） */
  status: string
  /** 进度百分比 0-100 */
  progress?: number
  /** 消耗积分 */
  credit_cost?: number
  /** 失败原因 */
  error_message?: string
  /** 结果文件 URL */
  result_url?: string
  /** 创建时间 */
  created_at?: string
  /** 开始时间 */
  started_at?: string
  /** 结束时间 */
  finished_at?: string
}

/** 后期制作详情 */
export interface PostDetail {
  /** 项目 ID */
  project_id: string
  /** 时间线工程文件 URL */
  timeline_url?: string
  /** 字幕样式配置 */
  subtitle_style?: Record<string, unknown>
  /** 背景音乐列表 */
  bgm_list?: Array<{ id: string; name?: string; url?: string }>
  /** 后期状态 */
  status?: string
}

/** 积分账户（后端 _account_summary 形状，available = balance - frozen） */
export interface CreditAccount {
  /** 账户 ID */
  account_id?: number | string
  /** 归属类型（user/team） */
  owner_type?: string
  /** 余额（积分） */
  balance_credits: number
  /** 冻结金额（进行中任务预扣） */
  frozen_credits?: number
  /** 可用余额（余额 - 冻结） */
  available_credits?: number
  /** 累计发放（积分） */
  total_granted_credits?: number
  /** 累计消耗（积分） */
  total_consumed_credits?: number
}

/** 积分流水 */
export interface CreditTransaction {
  /** 流水 ID */
  id: string
  /** 账户 ID */
  account_id?: string
  /** 类型（recharge/consume/freeze/unfreeze/refund） */
  type?: string
  /** 变动金额（正收入 / 负支出） */
  amount: number
  /** 变动后余额 */
  balance_after?: number
  /** 关联业务类型 */
  ref_type?: string
  /** 关联业务 ID */
  ref_id?: string
  /** 备注 */
  remark?: string
  /** 创建时间 */
  created_at?: string
}

/** 套餐 */
export interface Plan {
  /** 套餐 ID */
  id: string
  /** 套餐名 */
  name: string
  /** 价格（元） */
  price?: number
  /** 计费周期（month/year） */
  period?: string
  /** 赠送积分 */
  credits?: number
  /** 权益特性列表 */
  features?: string[]
  /** 套餐等级 */
  level?: string
}

/** 审批请求 */
export interface ApprovalRequest {
  /** 审批 ID */
  id: string
  /** 项目 ID */
  project_id?: string
  /** 审批目标类型（script/asset/shot/final 等） */
  target_type?: string
  /** 审批目标 ID */
  target_id?: string
  /** 请求动作 */
  action?: string
  /** 审批状态 */
  status?: string
  /** 发起原因 / 说明 */
  reason?: string
  /** 审批意见 */
  reviewer_comment?: string
  /** 创建时间 */
  created_at?: string
  /** 处理时间 */
  decided_at?: string
}

/** 站内通知 */
export interface Notification {
  /** 通知 ID */
  id: string
  /** 通知类型（task/asset/system 等） */
  type?: string
  /** 标题 */
  title: string
  /** 内容 */
  content?: string
  /** 是否已读 */
  is_read?: boolean
  /** 跳转链接 */
  link?: string
  /** 创建时间 */
  created_at?: string
}

/** 成片导出记录 */
export interface ExportRecord {
  /** 导出 ID */
  id: string
  /** 项目 ID */
  project_id: string
  /** 导出格式（mp4/mov） */
  format?: string
  /** 分辨率 */
  resolution?: string
  /** 文件 URL */
  file_url?: string
  /** 导出状态 */
  status?: string
  /** 创建时间 */
  created_at?: string
}

/** 团队成员 */
export interface TeamMember {
  /** 成员 ID */
  id: string
  /** 昵称 */
  nickname?: string
  /** 手机号 */
  phone?: string
  /** 角色（owner/admin/editor/viewer） */
  role?: string
  /** 状态 */
  status?: string
  /** 加入时间 */
  joined_at?: string
}

/** 平台授权账号 */
export interface PlatformAccount {
  /** 账号 ID */
  id: string
  /** 平台标识（douyin/kuaishou/bilibili 等） */
  platform: string
  /** 平台账号名 */
  account_name?: string
  /** 授权状态 */
  status?: string
  /** 授权时间 */
  authorized_at?: string
}

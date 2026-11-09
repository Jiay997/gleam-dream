/**
 * 文件级注释：业务选项常量（P02 引导 / P03 工作台 / P04 创建向导共用）。
 * 单一来源：题材 / 画风预设 / 画幅比例 / 单集时长 / 项目 14 态 / 预估成本单价。
 */

/** 单个可选项（key + 中文标签） */
export interface OptionItem {
  /** 选项键（提交后端） */
  key: string
  /** 中文标签（展示） */
  label: string
  /** 可选展示图标（emoji） */
  emoji?: string
}

/** 题材选项（与 P02 引导 / 项目创建保持一致） */
export const GENRE_OPTIONS: OptionItem[] = [
  { key: '都市情感', label: '都市情感', emoji: '🌹' },
  { key: '悬疑逆袭', label: '悬疑逆袭', emoji: '🔍' },
  { key: '古装宫斗', label: '古装宫斗', emoji: '🏯' },
  { key: '科幻末世', label: '科幻末世', emoji: '🚀' },
  { key: '甜宠日常', label: '甜宠日常', emoji: '🍬' },
  { key: '复仇逆袭', label: '复仇逆袭', emoji: '⚔️' },
  { key: '重生穿越', label: '重生穿越', emoji: '⏳' },
]

/**
 * 题材英文枚举兜底映射（修复测试报告 #4：AI 生成/后端写入的英文枚举
 * 如 urban_romance 在选项表中无 key，展示为原始英文）。
 */
export const GENRE_LABEL_FALLBACK: Record<string, string> = {
  urban_romance: '都市情感',
  urban: '都市情感',
  romance: '都市情感',
  suspense: '悬疑逆袭',
  mystery: '悬疑逆袭',
  suspense_reversal: '悬疑逆袭',
  palace: '古装宫斗',
  ancient: '古装宫斗',
  ancient_costume: '古装宫斗',
  sci_fi: '科幻末世',
  sci_fi_apocalypse: '科幻末世',
  apocalypse: '科幻末世',
  sweet: '甜宠日常',
  sweet_daily: '甜宠日常',
  sweet_pet: '甜宠日常',
  revenge: '复仇逆袭',
  rebirth: '重生穿越',
  time_travel: '重生穿越',
  rebirth_time_travel: '重生穿越',
}

/**
 * 题材中文标签（共享实现：选项表匹配 → 英文枚举兜底 → 未设）。
 * @param key 题材 key（中文或英文枚举）
 * @returns 中文标签；未设置返回 null（调用方决定回退文案）
 */
export function genreLabelOf(key?: string | null): string | null {
  if (!key) return null
  return GENRE_OPTIONS.find((g) => g.key === key)?.label ?? GENRE_LABEL_FALLBACK[key] ?? key
}

/** 画风预设选项（key 与后端 projects.style_preset_key CHECK 约束一致） */
export const STYLE_OPTIONS: OptionItem[] = [
  { key: 'live_action', label: '真人感', emoji: '🎬' },
  { key: 'anime', label: '二次元', emoji: '🌸' },
  { key: 'chinese_style', label: '国风', emoji: '🏮' },
  { key: 'three_d', label: '3D', emoji: '🧊' },
]

/** 画幅比例选项（key 与后端 projects.aspect_ratio CHECK 约束一致） */
export const ASPECT_RATIO_OPTIONS: OptionItem[] = [
  { key: '9:16', label: '9:16 竖屏', emoji: '📱' },
  { key: '16:9', label: '16:9 横屏', emoji: '🖥️' },
  { key: '1:1', label: '1:1 方形', emoji: '🔲' },
]

/** 单集时长选项（分钟） */
export const DURATION_OPTIONS: number[] = [1, 2, 3]

/** 单集每分钟预估成本（积分/分钟，P02/P04 粗略公式共用） */
export const CREDITS_PER_MINUTE = 160

/** 项目 14 态 key（与 constants/statusTone.ts 项目态一致，供下拉筛选） */
export const PROJECT_STATUS_KEYS: string[] = [
  'draft',
  'scripting',
  'script_review',
  'asset_preparing',
  'asset_review',
  'storyboarding',
  'storyboard_review',
  'rendering',
  'post_producing',
  'final_review',
  'completed',
  'archived',
  'paused',
  'failed',
]

/** 项目列表排序选项（sort 参数值） */
export const PROJECT_SORT_OPTIONS: Array<{ key: string; label: string }> = [
  { key: '-updated_at', label: '最近更新' },
  { key: '-created_at', label: '创建时间' },
  { key: '-spent_credits', label: '预算消耗' },
]

/* ==================== 项目六阶段（P05 Stepper / 环节跳转） ==================== */

/** 项目流程阶段定义（P05 Stepper 六阶段，与 ProjectSidebar 七 Tab 中的创作环节对应） */
export interface ProjectStageOption {
  /** 阶段键 */
  key: string
  /** 中文标签 */
  label: string
  /** 该阶段主页面路径模板（{id} 替换为项目 ID） */
  pathTemplate: string
}

/** 项目六阶段：剧本 / 资产设计 / 分镜 / 生成 / 后期 / 分发（资产设计 = 规划与生成，区别于全局资产库的归档查阅） */
export const PROJECT_STAGES: ProjectStageOption[] = [
  { key: 'script', label: '剧本', pathTemplate: '/projects/{id}/script' },
  { key: 'asset', label: '资产设计', pathTemplate: '/projects/{id}/assets' },
  { key: 'storyboard', label: '分镜', pathTemplate: '/projects/{id}/storyboard' },
  { key: 'render', label: '生成', pathTemplate: '/projects/{id}/render-tasks' },
  { key: 'post', label: '后期', pathTemplate: '/projects/{id}/post' },
  { key: 'distribute', label: '分发', pathTemplate: '/projects/{id}/distribute' },
]

/** 项目状态 → 阶段位置信息 */
export interface StatusStageInfo {
  /** 当前活跃阶段序号（1~6；7 表示全部完成） */
  index: number
  /** 当前阶段是否处于失败态（Stepper 红色 ✕ 标记） */
  failed: boolean
}

/** 项目 14 态 → 六阶段位置映射（draft 停在剧本阶段；completed 越界表示全部完成） */
const STATUS_STAGE_INDEX: Record<string, number> = {
  draft: 1,
  scripting: 1,
  script_review: 1,
  asset_preparing: 2,
  asset_review: 2,
  storyboarding: 3,
  storyboard_review: 3,
  rendering: 4,
  post_producing: 5,
  final_review: 5,
  completed: 7,
  archived: 7,
  rendering_failed: 4,
}

/**
 * 解析项目状态对应的流程阶段位置（P05 Stepper 与「进入当前环节」按钮共用）。
 * paused 状态优先从项目 config._paused_before.status 还原原阶段；
 * failed 状态按剧本阶段（最常见失败点）标红；未知状态停在剧本阶段。
 * @param status 项目状态字符串
 * @param config 项目 config（用于还原暂停前状态，可空）
 * @returns 阶段位置信息 { index, failed }
 */
export function stageIndexOf(status: string, config?: Record<string, unknown> | null): StatusStageInfo {
  if (status === 'paused') {
    const before = config?._paused_before
    const beforeStatus = typeof before === 'object' && before !== null ? String((before as Record<string, unknown>).status ?? '') : ''
    return { index: STATUS_STAGE_INDEX[beforeStatus] ?? 1, failed: false }
  }
  if (status === 'failed') {
    return { index: 1, failed: true }
  }
  return { index: STATUS_STAGE_INDEX[status] ?? 1, failed: false }
}

/* ==================== HITL 四关口（P05 关口面板） ==================== */

/** HITL 人工关口定义（审批单类型与确认页路由映射） */
export interface HitlGateOption {
  /** 关口键 */
  key: 'script' | 'asset' | 'storyboard' | 'final_cut'
  /** 展示名称 */
  label: string
  /** 对应审批单 approval_type */
  approvalType: 'script' | 'asset' | 'storyboard' | 'final_cut'
  /** 去确认的目标路径模板（{id} 替换为项目 ID） */
  routeTemplate: string
  /** 关口所在阶段序号（1~6，用于从项目状态推断「已通过 / 未开始」） */
  stageIndex: number
}

/** HITL 四关口：剧本 / 资产 / 分镜 / 成片 */
export const HITL_GATES: HitlGateOption[] = [
  { key: 'script', label: 'HITL-1 剧本确认', approvalType: 'script', routeTemplate: '/projects/{id}/script/review', stageIndex: 1 },
  { key: 'asset', label: 'HITL-2 资产确认', approvalType: 'asset', routeTemplate: '/projects/{id}/assets', stageIndex: 2 },
  { key: 'storyboard', label: 'HITL-3 分镜确认', approvalType: 'storyboard', routeTemplate: '/projects/{id}/storyboard', stageIndex: 3 },
  { key: 'final_cut', label: 'HITL-4 成片确认', approvalType: 'final_cut', routeTemplate: '/projects/{id}/post/review', stageIndex: 5 },
]

/* ==================== 通知中心（P22） ==================== */

/** 通知类型选项（type 参数值 + 中文标签 + 列表图标，全部 Tab 用空串表示） */
export const NOTIFICATION_TYPE_OPTIONS: Array<{ key: string; label: string; icon: string }> = [
  { key: '', label: '全部', icon: '🔔' },
  { key: 'approval', label: '审批待办', icon: '📝' },
  { key: 'task', label: '生成完成', icon: '🎬' },
  { key: 'system', label: '系统', icon: '📢' },
  { key: 'credit', label: '积分', icon: '💰' },
]

/* ==================== 导出分发（P18/P19） ==================== */

/** 分发平台选项（platform 参数值与 platform_accounts CHECK 约束一致） */
export const PLATFORM_OPTIONS: Array<{ key: string; label: string; icon: string }> = [
  { key: 'douyin', label: '抖音', icon: '🎵' },
  { key: 'kuaishou', label: '快手', icon: '⚡' },
  { key: 'wechat_channels', label: '视频号', icon: '📺' },
  // 剪映：后端 platform_accounts / distributions 的 CHECK 约束里有 jianying，
  // 缺这一项时列表会把 jianying 这个 key 原样当平台名显示出来
  { key: 'jianying', label: '剪映', icon: '🎬' },
]

/**
 * 按平台键查询展示信息。
 * @param platform 平台键
 * @returns 平台选项（未知平台回退通用图标 + 原样键名）
 */
export function platformOption(platform: string): { key: string; label: string; icon: string } {
  return PLATFORM_OPTIONS.find((item) => item.key === platform) ?? { key: platform, label: platform, icon: '🔗' }
}

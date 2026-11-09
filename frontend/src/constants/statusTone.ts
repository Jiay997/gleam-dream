/**
 * 文件级注释：业务状态 → 视觉五态映射的单一来源（Single Source of Truth）。
 * 覆盖三类状态：项目 14 态、生成任务 9 态、数字资产 9 态。
 * 每个状态归一化为五种 tone（running 生成中 / pending 待确认 / locked 已锁定 /
 * failed 失败 / blocked 审核拦截），另设 neutral 中性兜底 tone。
 * STATUS_TONE 为组件消费的最终映射；toneOf / projectStatusLabel 提供归一化查询。
 */
import { STATUS_ICON_PATHS } from './statusIcons'

/** 视觉五态（+ 中性兜底） */
export type StatusToneKey = 'running' | 'pending' | 'locked' | 'failed' | 'blocked' | 'neutral'

/** tone 对应的前景色 / 浅底色 / 描边色 */
export interface TonePalette {
  /** 前景色（文字 / 圆点） */
  color: string
  /** 浅底色 */
  bg: string
  /** 描边色 */
  border: string
}

/** 单个状态的完整视觉信息 */
export interface StatusToneInfo {
  /** 中文标签 */
  label: string
  /** 前景色 */
  color: string
  /** 浅底色 */
  bg: string
  /** 描边色 */
  border: string
  /** 图标 SVG path（24x24 stroke 风格） */
  icon: string
}

/** 五态 + 中性 tone 的调色板（取自 tokens.css 对应变量） */
const TONE_PALETTES: Record<StatusToneKey, TonePalette> = {
  running: { color: 'var(--gd-running)', bg: 'var(--gd-running-bg)', border: 'var(--gd-running-border)' },
  pending: { color: 'var(--gd-pending)', bg: 'var(--gd-pending-bg)', border: 'var(--gd-pending-border)' },
  locked: { color: 'var(--gd-locked)', bg: 'var(--gd-locked-bg)', border: 'var(--gd-locked-border)' },
  failed: { color: 'var(--gd-failed)', bg: 'var(--gd-failed-bg)', border: 'var(--gd-failed-border)' },
  blocked: { color: 'var(--gd-blocked)', bg: 'var(--gd-blocked-bg)', border: 'var(--gd-blocked-border)' },
  neutral: { color: 'var(--gd-neutral-500)', bg: 'var(--gd-neutral-100)', border: 'var(--gd-neutral-200)' },
}

/** 状态定义（内部结构：标签 + tone + 图标） */
interface StatusDefinition {
  label: string
  tone: StatusToneKey
  icon: keyof typeof STATUS_ICON_PATHS
}

/** 全部业务状态定义表 */
const STATUS_DEFINITIONS: Record<string, StatusDefinition> = {
  /* ---------- 项目 14 态 ---------- */
  draft: { label: '草稿', tone: 'neutral', icon: 'pencil' },
  scripting: { label: '剧本生成中', tone: 'running', icon: 'document' },
  script_review: { label: '剧本待审核', tone: 'pending', icon: 'eye' },
  asset_preparing: { label: '资产准备中', tone: 'running', icon: 'cube' },
  asset_review: { label: '资产待审核', tone: 'pending', icon: 'eye' },
  storyboarding: { label: '分镜生成中', tone: 'running', icon: 'grid' },
  storyboard_review: { label: '分镜待审核', tone: 'pending', icon: 'eye' },
  rendering: { label: '视频生成中', tone: 'running', icon: 'refresh' },
  post_producing: { label: '后期制作中', tone: 'running', icon: 'film' },
  final_review: { label: '成片待审核', tone: 'pending', icon: 'eye' },
  completed: { label: '已完成', tone: 'locked', icon: 'check' },
  archived: { label: '已归档', tone: 'neutral', icon: 'archive' },
  paused: { label: '已暂停', tone: 'neutral', icon: 'pause' },
  failed: { label: '已失败', tone: 'failed', icon: 'close' },

  /* ---------- 生成任务态 ---------- */
  pending: { label: '待处理', tone: 'pending', icon: 'clock' },
  queued: { label: '排队中', tone: 'pending', icon: 'clock' },
  running: { label: '生成中', tone: 'running', icon: 'refresh' },
  qc_review: { label: '质检审核中', tone: 'pending', icon: 'eye' },
  succeeded: { label: '已成功', tone: 'locked', icon: 'check' },
  cancelled: { label: '已取消', tone: 'neutral', icon: 'close' },
  retrying: { label: '重试中', tone: 'running', icon: 'refresh' },
  manual_required: { label: '需人工介入', tone: 'blocked', icon: 'alert' },

  /* ---------- 数字资产态 ---------- */
  planning: { label: '规划中', tone: 'running', icon: 'document' },
  generating: { label: '生成中', tone: 'running', icon: 'refresh' },
  pending_review: { label: '待确认', tone: 'pending', icon: 'eye' },
  approved: { label: '已通过', tone: 'locked', icon: 'check' },
  locked: { label: '已锁定', tone: 'locked', icon: 'lock' },
  qc_failed: { label: '质检失败', tone: 'failed', icon: 'close' },
  rejected: { label: '已驳回', tone: 'failed', icon: 'close' },
  // Q-015：配音轨/voice_clips 专有状态，避免页面显示“未知状态”。
  // 注：pending 已在上方任务态定义为「待处理」（键必须唯一），配音轨上下文复用该条目。
  voiced: { label: '已配音', tone: 'locked', icon: 'check' },
  voice_locked: { label: '配音已锁定', tone: 'locked', icon: 'lock' },
  skipped: { label: '已跳过', tone: 'neutral', icon: 'pencil' },
  deprecated: { label: '已废弃', tone: 'neutral', icon: 'trash' },
}

/**
 * 状态 → 视觉信息映射（供 StatusTag 等组件直接消费）。
 * 键为后端状态字符串，值为 { label, color, bg, border, icon }。
 */
export const STATUS_TONE: Record<string, StatusToneInfo> = Object.fromEntries(
  Object.entries(STATUS_DEFINITIONS).map(([status, def]) => {
    const palette = TONE_PALETTES[def.tone]
    return [
      status,
      { label: def.label, color: palette.color, bg: palette.bg, border: palette.border, icon: STATUS_ICON_PATHS[def.icon] },
    ]
  }),
)

/**
 * 归一化查询：任意状态字符串 → 所属五态 tone（未知状态归为 neutral）。
 * @param status 后端状态字符串
 * @returns 五态 tone 键（running/pending/locked/failed/blocked/neutral）
 */
export function toneOf(status: string): StatusToneKey {
  return STATUS_DEFINITIONS[status]?.tone ?? 'neutral'
}

/**
 * 查询任意状态的完整视觉信息（未知状态回退为中性"未知状态"）。
 * @param status 后端状态字符串
 * @returns 含 label / color / bg / border / icon 的视觉信息
 */
export function statusInfo(status: string): StatusToneInfo {
  return (
    STATUS_TONE[status] ?? {
      label: status,
      ...TONE_PALETTES.neutral,
      icon: STATUS_ICON_PATHS.alert,
    }
  )
}

/**
 * 查询项目状态中文标签（未知状态原样返回，便于排查）。
 * @param status 项目状态字符串
 * @returns 中文标签
 */
export function projectStatusLabel(status: string): string {
  return STATUS_DEFINITIONS[status]?.label ?? status
}

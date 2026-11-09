/**
 * 文件级注释：日期时间展示工具（相对时间与短日期格式化）。
 * 供项目卡片「相对时间更新」等场景消费，全部为纯函数。
 */

/**
 * 将 ISO 时间字符串格式化为中文相对时间。
 * @param iso ISO8601 时间字符串（可空）
 * @returns 相对时间文案（如「刚刚」「5 分钟前」「3 天前」；超过 30 天回退为日期）
 */
export function formatRelativeTime(iso?: string | null): string {
  if (!iso) return '—'
  const time = new Date(iso).getTime()
  if (Number.isNaN(time)) return '—'
  const diffMs = Date.now() - time
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (diffMs < minute) return '刚刚'
  if (diffMs < hour) return `${Math.floor(diffMs / minute)} 分钟前`
  if (diffMs < day) return `${Math.floor(diffMs / hour)} 小时前`
  if (diffMs < 30 * day) return `${Math.floor(diffMs / day)} 天前`
  return formatShortDate(iso)
}

/**
 * 将 ISO 时间字符串格式化为短日期（YYYY-MM-DD）。
 * @param iso ISO8601 时间字符串（可空）
 * @returns 短日期文案（无效输入返回 —）
 */
export function formatShortDate(iso?: string | null): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

/**
 * 提取展示用首字符（中文取首字，英文取首字母并大写）。
 * @param text 原始文本
 * @returns 首字符（空文本回退「梦」）
 */
export function firstChar(text: string): string {
  const trimmed = text.trim()
  return trimmed ? trimmed[0].toUpperCase() : '梦'
}

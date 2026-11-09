/**
 * 文件级注释：通用展示格式化工具（纯函数）。
 * 手机号掩码、文件体积 humanize 等跨页面复用的展示格式化。
 */

/**
 * 手机号掩码：保留前 3 位与后 4 位，中间以 **** 代替（如 138****8888）。
 * 非 11 位标准手机号时仅展示末 4 位，避免泄露完整号码。
 * @param phone 原始手机号（可空）
 * @returns 掩码后的手机号（空输入返回 '—'）
 */
export function maskPhone(phone?: string | null): string {
  if (!phone) return '—'
  const trimmed = phone.trim()
  if (trimmed.length === 11 && /^\d+$/.test(trimmed)) {
    return `${trimmed.slice(0, 3)}****${trimmed.slice(-4)}`
  }
  return trimmed.length > 4 ? `****${trimmed.slice(-4)}` : '****'
}

/**
 * 字节体积 humanize（B/KB/MB/GB 自适应，保留 1 位小数）。
 * @param bytes 字节数（可空）
 * @returns 体积文案（空输入返回 '—'）
 */
export function formatBytes(bytes?: number | null): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`
}

/**
 * 分转元显示（人民币分 → 「¥49.00」样式，等宽数字场景配合 gd-num 使用）。
 * @param cents 金额（分，可空）
 * @returns 价格文案（空输入返回 '—'）
 */
export function formatCents(cents?: number | null): string {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return '—'
  return `¥${(cents / 100).toFixed(2).replace(/\.00$/, '')}`
}

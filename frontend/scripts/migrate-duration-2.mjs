/**
 * 一次性迁移脚本（方向 B · 第二批机械替换）。
 *
 * 目标：把散落在各页面的 Tailwind 默认时长类收敛到设计令档次，
 * 消除「同一动效三种写法」的不一致。
 *
 *    duration-150  ->  duration-gd-fast    (150ms)
 *    duration-200  ->  duration-gd-normal  (200ms)
 *    duration-300  ->  duration-gd-slow    (300ms)
 *
 * 说明：数值等价，属纯重命名，不改变视觉表现，仅统一可维护入口。
 * 同时把 `transition-all` 收敛为显式的属性过渡（如果无法确定属性则保留，
 * 避免误伤 —— 本脚本只处理 duration）。
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

/** 项目源码根目录（使用 fileURLToPath 避免路径含空格被 URL 编码） */
const SRC = fileURLToPath(new URL('../src', import.meta.url))

/** 替换规则：键为原类名（含边界），值为目标类名 */
const RULES = [
  { from: /\bduration-150\b/g, to: 'duration-gd-fast' },
  { from: /\bduration-200\b/g, to: 'duration-gd-normal' },
  { from: /\bduration-300\b/g, to: 'duration-gd-slow' },
]

/** 已跳过/未发生变动的文件数 */
let scanned = 0
/** 命中的文件数 */
let touched = 0
/** 累计替换处数 */
let replaced = 0
/** 逐文件命中明细 */
const details = []

/**
 * 递归收集目录下所有 .vue / .ts 文件。
 * @param dir 起始目录
 * @param out 结果累加容器
 * @returns 文件绝对路径数组
 */
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    const st = statSync(full)
    if (st.isDirectory()) walk(full, out)
    else if (/\.(vue|ts)$/.test(name)) out.push(full)
  }
  return out
}

for (const file of walk(SRC)) {
  scanned += 1
  const before = readFileSync(file, 'utf8')
  let after = before
  for (const rule of RULES) after = after.replace(rule.from, rule.to)
  if (after !== before) {
    // 统计该文件命中次数（按行号采集，便于人工复核）
    const hits = []
    before.split('\n').forEach((line, idx) => {
      if (/duration-(150|200|300)\b/.test(line)) hits.push(idx + 1)
    })
    writeFileSync(file, after, 'utf8')
    touched += 1
    replaced += hits.length
    details.push(`${file.replace(SRC, 'src')}  行 ${hits.join(', ')}`)
  }
}

console.log(`[migrate-duration-2] 扫描 ${scanned} 个文件，修改 ${touched} 个，替换 ${replaced} 处。`)
for (const line of details) console.log('  · ' + line)

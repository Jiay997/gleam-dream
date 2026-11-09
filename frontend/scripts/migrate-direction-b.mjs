/**
 * 一次性迁移脚本（方向 B 视觉重构）：
 * 1) text-neutral-300 → text-neutral-400 ：300 在新色阶下仅 1.5:1，禁止用于文字；
 *    经逐行核对，全站 text-neutral-300 均为文字用途（计数器/空态提示/时间戳/占位符）。
 * 2) text-[10px] → text-[11px] ：消除低于 11px 的字号（信息密集页仍可读）。
 * 用法：node scripts/migrate-direction-b.mjs
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('../src', import.meta.url))

const RULES = [
  { from: 'text-neutral-300', to: 'text-neutral-400' },
  { from: 'text-[10px]', to: 'text-[11px]' },
]

/** 递归收集 .vue 文件 */
function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) return walk(full)
    return full.endsWith('.vue') ? [full] : []
  })
}

let totalFiles = 0
let totalHits = 0
for (const file of walk(ROOT)) {
  let content = readFileSync(file, 'utf8')
  const before = content
  const hits = []
  for (const rule of RULES) {
    const count = content.split(rule.from).length - 1
    if (count > 0) {
      content = content.split(rule.from).join(rule.to)
      hits.push(`${rule.from} -> ${rule.to} ×${count}`)
    }
  }
  if (content !== before) {
    writeFileSync(file, content, 'utf8')
    totalFiles += 1
    totalHits += hits.reduce((sum, h) => sum + Number(h.split('×')[1]), 0)
    console.log(`${file.replace(ROOT, 'src')}: ${hits.join(', ')}`)
  }
}
console.log(`\n完成：${totalFiles} 个文件，${totalHits} 处替换。`)

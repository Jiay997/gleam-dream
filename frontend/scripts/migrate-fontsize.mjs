/**
 * 一次性迁移脚本（方向 B · 第三批：字号语义化）。
 *
 * 背景：全站仍有 41 处 `text-[11px]` 与 3 处 `text-[13px]` 直接写字面量字号，
 * 绕过语义字阶（micro/caption/title/display）。这批把字面量收敛到语义类。
 *
 * 映射规则：
 *   text-[11px] → text-micro   仅限「数字/序号角标」——这些位置本就该 11px + 600 字重
 *   text-[11px] → text-caption 其余一律（12px），把「最小可读字号」真正落到 12px
 *   text-[13px] → text-sm      正文档（14px）
 *
 * 角标白名单按「文件 + 行号」精确指定，避免误判——行号以迁移当刻为准。
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/** 项目源码根目录 */
const SRC = fileURLToPath(new URL('../src', import.meta.url))

/**
 * 应迁移为 text-micro 的角标位置（文件相对路径 → 行号集合）。
 * 判据：内容是「纯数字 / 序号 / emoji 字形」，且容器为固定尺寸的小圆/小方角标。
 */
const MICRO_ALLOWLIST = {
  'components/layout/TopBar.vue': [142],        // gd-num 通知计数角标
  'components/storyboard/ShotDetailDrawer.vue': [389, 398], // h-5 w-5 圆形内的 emoji 字形
  'views/DraftPreviewView.vue': [436],          // 占位缩略图中心的镜号
  'views/OnboardingView.vue': [283],            // h-5 w-5 步骤序号角标
  'views/ScriptEditorView.vue': [939, 1172, 1191], // 集号角标 + 状态指示圆点
}

/** 统计 */
const stats = { micro: 0, caption: 0, sm: 0 }

/**
 * 处理单个文件。
 * @param rel 相对 src 的路径
 * @param microLines 该文件中应转 micro 的行号集合
 * @returns 是否有改动
 */
function migrate(rel, microLines) {
  const full = `${SRC}/${rel}`.replace(/\\/g, '/')
  const lines = readFileSync(full, 'utf8').split('\n')
  let changed = false

  const out = lines.map((line, index) => {
    const lineNo = index + 1
    if (line.includes('text-[11px]')) {
      if (microLines.has(lineNo)) {
        stats.micro += 1
        changed = true
        return line.replaceAll('text-[11px]', 'text-micro')
      }
      stats.caption += 1
      changed = true
      return line.replaceAll('text-[11px]', 'text-caption')
    }
    if (line.includes('text-[13px]')) {
      stats.sm += 1
      changed = true
      return line.replaceAll('text-[13px]', 'text-sm')
    }
    return line
  })

  if (changed) writeFileSync(full, out.join('\n'), 'utf8')
  return changed
}

/** 需要处理的文件清单 = 白名单文件 ∪ 实际含字面量字号的 .vue 文件 */
const targets = new Map()
for (const [rel, arr] of Object.entries(MICRO_ALLOWLIST)) targets.set(rel, new Set(arr))

// 扫描所有 .vue，补全未被白名单覆盖的文件
const { readdirSync, statSync } = await import('node:fs')
const { join } = await import('node:path')

/**
 * 递归收集 .vue 文件（相对 src 的路径）。
 * @param dir 目录绝对路径
 * @param out 结果容器
 * @returns 相对路径数组
 */
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (name.endsWith('.vue')) out.push(full.replace(SRC, '').replace(/\\/g, '/').replace(/^\//, ''))
  }
  return out
}

let touched = 0
for (const rel of walk(SRC)) {
  if (!targets.has(rel)) targets.set(rel, new Set())
}

for (const [rel, microLines] of targets) {
  if (migrate(rel, microLines)) {
    touched += 1
    console.log(`  · ${rel}`)
  }
}

console.log(
  `[migrate-fontsize] 修改 ${touched} 个文件：micro ${stats.micro} 处 / caption ${stats.caption} 处 / sm ${stats.sm} 处`,
)

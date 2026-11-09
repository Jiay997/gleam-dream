/**
 * 截设计系统预览页（本地 HTML）作为视觉基线，和实现页做对照。
 * 用法：node scripts/ref-shot.mjs [width]
 */
import { chromium } from 'file:///C:/Users/10486/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { pathToFileURL } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const OUT = join(HERE, '..', 'e2e-verify', 'aurora')
const REF = resolve(
  HERE,
  '../../项目开发文档/视觉设计方案/aurora_system/02-设计系统预览.html'
)
const width = Number(process.argv[2] || 1440)

const browser = await chromium.launch({ channel: 'msedge', headless: true })
try {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, deviceScaleFactor: 2 })
  await page.goto(pathToFileURL(REF).href, { waitUntil: 'load' })
  await page.waitForTimeout(1200)
  await page.screenshot({ path: join(OUT, `ref-ds-${width}.png`), fullPage: true })
  const h = await page.evaluate(() => document.body.scrollHeight)
  console.log(`saved ref-ds-${width}.png (page height ${h})`)
} catch (e) {
  console.error('REF SHOT FAILED:', e.message)
  process.exitCode = 1
} finally {
  await browser.close()
}

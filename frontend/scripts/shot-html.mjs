/**
 * 静态 HTML 截图（设计稿审阅用，不依赖 dev server）。
 * 用法：node scripts/shot-html.mjs <abs-html-path> <out-name> [width] [fullPage]
 */
import { chromium } from 'file:///C:/Users/10486/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { pathToFileURL } from 'node:url'

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'e2e-verify', 'design')
const [file, name = 'shot', width = '1440', full = '1'] = process.argv.slice(2)

const browser = await chromium.launch({ channel: 'msedge' })
const page = await browser.newPage({
  viewport: { width: Number(width), height: 1000 },
  deviceScaleFactor: 2,
})
await page.goto(pathToFileURL(file).href, { waitUntil: 'load' })
await page.evaluate(() => document.fonts?.ready)
await page.waitForTimeout(500)
await page.screenshot({ path: join(OUT, `${name}.png`), fullPage: full === '1' })
console.log('saved', join(OUT, `${name}.png`))
await browser.close()

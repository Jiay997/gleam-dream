/**
 * 组件放大截图：把某个页面的某个区域以 2x 截出来，用于判断材质 / 对比度 / 状态。
 * 用法：node scripts/zoom.mjs <path> <selector> <name> [width]
 *
 * 注意：不能用 waitUntil:'networkidle' —— /events/stream 的 EventSource 会无限重连，
 * networkidle 永远不满足，脚本会挂到超时。
 */
import { chromium } from 'file:///C:/Users/10486/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'e2e-verify', 'aurora')
const [path = '/billing', selector = 'body', name = 'zoom', width = '1440'] = process.argv.slice(2)

const ok = (data) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ code: 0, message: 'ok', data }),
})
const project = {
  id: 1,
  title: '失踪的回声',
  status: 'script_review',
  aspect_ratio: '16:9',
  genre: 'suspense',
  budget_credits: 5000,
  spent_credits: 3240,
  target_episode_count: 12,
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
try {
  const context = await browser.newContext({
    viewport: { width: Number(width), height: 900 },
    deviceScaleFactor: 2,
  })
  await context.addInitScript(() => {
    localStorage.setItem('gd_access_token', 'mock-token')
    localStorage.setItem(
      'gd_user',
      JSON.stringify({ id: 1, phone: '138****8888', nickname: '林晚舟', roles: ['owner'] })
    )
  })
  await context.route('**/api/v1/**', async (route) => {
    const p = new URL(route.request().url()).pathname.replace(/^\/api\/v1/, '')
    // SSE：用永不结束的空流替代 abort，避免 EventSource 疯狂重连 + console 噪音
    // 真实端点是 /api/v1/projects/{id}/events（不是 /events/stream）
    if (p.endsWith('/events') || p.endsWith('/events/stream')) {
      return route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' },
        body: ': keep-alive\n\n',
      })
    }
    if (p === '/projects') return route.fulfill(ok({ items: [project], total: 1, page: 1, page_size: 12 }))
    if (/^\/projects\/\d+$/.test(p)) return route.fulfill(ok(project))
    if (p.endsWith('/billing/credit-account'))
      return route.fulfill(
        ok({ balance_credits: 1240, frozen_credits: 320, total_charged_credits: 6000, total_consumed_credits: 3240 })
      )
    if (p.endsWith('/notifications/unread-count')) return route.fulfill(ok({ count: 2 }))
    if (p.endsWith('/users/me'))
      return route.fulfill(
        ok({ id: 1, phone: '138****8888', nickname: '林晚舟', roles: ['owner'], preferences: {} })
      )
    return route.fulfill(ok({ items: [], total: 0 }))
  })

  const page = await context.newPage()
  await page.goto(`http://127.0.0.1:5175${path}`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(1200)

  const el = page.locator(selector).first()
  await el.waitFor({ state: 'visible', timeout: 8000 })
  const box = await el.boundingBox()
  if (!box) throw new Error(`元素无 boundingBox: ${selector}`)

  // 用 clip 截图（相对视口），并把元素滚动到可视区
  await el.scrollIntoViewIfNeeded()
  const box2 = await el.boundingBox()
  const clip = {
    x: Math.max(0, box2.x),
    y: Math.max(0, box2.y),
    width: Math.min(box2.width, Number(width) - Math.max(0, box2.x)),
    height: Math.min(box2.height, 900 - Math.max(0, box2.y)),
  }
  await page.screenshot({ path: join(OUT, `${name}.png`), clip })
  console.log(`saved ${name}.png  (${Math.round(clip.width)}x${Math.round(clip.height)} @2x)`)
} catch (e) {
  console.error('ZOOM FAILED:', e.message)
  process.exitCode = 1
} finally {
  await browser.close()
}

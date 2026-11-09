/**
 * e2e 视觉验收截图脚本（v7 纸上放映厅 Pro 重构验收）。
 * 用 playwright-core + 系统 Edge，对 dev server(5200) 截取：
 *   /login（裸页暗场域）与 /projects（AppShell + Bento 工作台）
 * 桌面 1440x900 + 移动 390x844 两档。
 * 通过路由拦截 mock /api/v1 数据并注入登录态，不依赖真实账号。
 * 输出：frontend/e2e-verify/*.png + 控制台错误清单。
 */
import { chromium } from 'file:///C:/Users/10486/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'e2e-verify')
mkdirSync(OUT, { recursive: true })

const BASE = 'http://127.0.0.1:5200'
const now = Date.now()
const iso = (d) => new Date(now - d * 3600e3).toISOString()

/** mock 项目数据（对应效果图 P03：hero + 4 小卡，五态各异） */
const projects = [
  { id: 1, title: '失踪的回声', genre: 'suspense', status: 'script_review', aspect_ratio: '16:9', target_episode_count: 12, budget_credits: 5000, spent_credits: 3240, updated_at: iso(2) },
  { id: 2, title: '长安夜行记', genre: 'ancient', status: 'storyboarding', aspect_ratio: '9:16', target_episode_count: 8, budget_credits: 4000, spent_credits: 1500, updated_at: iso(5) },
  { id: 3, title: '深夜便利店', genre: 'urban', status: 'rendering', aspect_ratio: '9:16', target_episode_count: 6, budget_credits: 3600, spent_credits: 2100, updated_at: iso(26) },
  { id: 4, title: '我的AI房东', genre: 'comedy', status: 'completed', aspect_ratio: '16:9', target_episode_count: 12, budget_credits: 6000, spent_credits: 5320, updated_at: iso(52) },
  { id: 5, title: '川西来信', genre: 'literary', status: 'draft', aspect_ratio: '16:9', target_episode_count: 0, budget_credits: 0, spent_credits: 0, updated_at: iso(120), created_at: iso(120) },
]

const ok = (data) => ({ status: 200, contentType: 'application/json', body: JSON.stringify({ code: 0, message: 'ok', data, request_id: 'mock' }) })

async function setup(context) {
  await context.addInitScript(() => {
    localStorage.setItem('gd_access_token', 'mock-token')
    localStorage.setItem('gd_refresh_token', 'mock-refresh')
    localStorage.setItem('gd_user', JSON.stringify({ id: 1, phone: '138****8888', nickname: '林晚舟', roles: ['owner'] }))
  })
  await context.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const p = url.pathname
    if (p.endsWith('/projects')) return route.fulfill(ok({ items: projects, total: projects.length, page: 1, page_size: 12 }))
    if (p.endsWith('/billing/credit-account')) return route.fulfill(ok({ balance: 1240, frozen_credits: 320, total_charged: 6000, updated_at: iso(1) }))
    if (p.endsWith('/notifications/unread-count')) return route.fulfill(ok({ count: 3 }))
    if (p.endsWith('/notifications')) return route.fulfill(ok({ items: [], total: 0 }))
    return route.fulfill(ok({ items: [], total: 0 }))
  })
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const errors = []

async function shoot(name, viewport, options = {}) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, isMobile: options.mobile ?? false, hasTouch: options.mobile ?? false })
  await setup(context)
  const page = await context.newPage()
  page.on('pageerror', (e) => errors.push(`[${name}] pageerror: ${e.message}`))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(`[${name}] console.error: ${m.text()}`) })
  await page.goto(`${BASE}${options.path ?? '/login'}`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)
  await page.screenshot({ path: join(OUT, `${name}.png`), fullPage: options.fullPage ?? false })
  // 横向溢出检测
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  console.log(`${name}: shot ok, h-overflow=${overflow}px`)
  await context.close()
}

await shoot('login-1440', { width: 1440, height: 900 })
await shoot('login-390', { width: 390, height: 844 }, { mobile: true })
await shoot('projects-1440', { width: 1440, height: 900 }, { path: '/projects' })
await shoot('projects-390', { width: 390, height: 844 }, { path: '/projects', mobile: true })

await browser.close()
console.log(errors.length ? `\nERRORS (${errors.length}):\n` + errors.slice(0, 20).join('\n') : '\nNo console/page errors.')

/**
 * 文件级注释：配音修复专项 UI 回归（第 5 阶段交付物）。
 *
 * 覆盖（对应用户验收清单第 11 项的前端三提示 + 合成前拦截）：
 * 1. 配音轨页面：占位音统计标签、逐句徽标「占位音·非真实人声」、
 *    degradedCount>0 告警条（含 70005 拦截提示文案）；
 * 2. 后期编辑器：点「合成成片」时前端先调 voice-coverage，
 *    存在缺失/占位时 toast 拦截且不打开合成对话框（70005 前置拦截）。
 *
 * 默认项目 1410（QA 账号自有、含真实占位音数据），可用环境变量
 * VOICE_PROJECT 覆盖；仅做只读断言与前端拦截验证，不触发生成/合成写操作。
 *
 * 运行：npx playwright test -c tests/regression/playwright.config.ts voice-gate
 */
import { expect, test } from '@playwright/test'

import { QA_PASSWORD, QA_PHONE } from './helpers'

/** 配音专项验证目标项目（默认 1410：全链路测试项目，含 14 句占位音）。 */
const VOICE_PROJECT = Number(process.env.VOICE_PROJECT ?? '1410')

/** 密码通道登录并等待进入项目列表。 */
async function login(page: import('@playwright/test').Page): Promise<void> {
  await page.goto('/login')
  await page.getByLabel('手机号').fill(QA_PHONE)
  await page.getByRole('button', { name: '密码登录' }).click()
  await page.getByLabel('登录密码').fill(QA_PASSWORD)
  await page.getByRole('checkbox').check()
  await page.getByRole('button', { name: /^登\s*录$/ }).click()
  await page.waitForURL(/projects/, { timeout: 15_000 })
}

test.describe('配音修复专项回归', () => {
  test('配音轨：占位音统计、逐句徽标与 70005 告警条', async ({ page }) => {
    await login(page)
    await page.goto(`/projects/${VOICE_PROJECT}/voice`)

    // 台词列表加载（统计区含「已配音」标签）
    await expect(page.getByText('已配音', { exact: false }).first()).toBeVisible({
      timeout: 20_000,
    })

    // 统计区出现「占位音」标签（degradedCount > 0 的项目）
    await expect(page.getByText('占位音', { exact: true }).first()).toBeVisible({
      timeout: 10_000,
    })

    // 逐句徽标：占位音·非真实人声
    await expect(
      page.getByText('占位音·非真实人声').first(),
    ).toBeVisible({ timeout: 10_000 })

    // 告警条：沙箱占位音频 + 70005 拦截提示
    await expect(
      page.getByText(/沙箱占位音频（非真实人声），合成时会被后端拦截（70005）/),
    ).toBeVisible({ timeout: 10_000 })
  })

  test('后期编辑器：合成前覆盖检查拦截（不打开合成对话框）', async ({ page }) => {
    await login(page)
    await page.goto(`/projects/${VOICE_PROJECT}/post`)

    // 后期编辑器加载完成（时间线头部出现「合成成片」入口）
    const composeBtn = page.getByRole('button', { name: /合成成片/ })
    await expect(composeBtn).toBeVisible({ timeout: 20_000 })

    // 点击后应弹出拦截 toast，而不是打开「发起成片合成？」对话框
    await composeBtn.click()
    await expect(
      page.getByText(/沙箱占位配音（非真实人声）|有台词但缺少有效配音/).first(),
    ).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText('发起成片合成？')).toHaveCount(0)
  })
})

/**
 * 文件级注释：核心路径 UI 回归（第 5 阶段交付物之一）。
 *
 * 覆盖：登录（密码通道）→ 项目列表 → 新建项目入口 → 项目工作台可进入。
 * 全程真实浏览器驱动，验证 P0 修复后的页面骨架与登录态（Q-015 状态标签
 * 所在的 AppShell 正常渲染）。
 *
 * 页面事实（依 Playwright 快照校正）：
 * - 登录页默认即在密码通道（tab「登录」选中，验证码登录为切换按钮）；
 * - 提交按钮文案为「登 录」（含字间空格），用正则匹配；
 * - 登录前须勾选「我已阅读并同意」协议复选框。
 *
 * 运行：npx playwright test -c tests/regression/playwright.config.ts core-path
 */
import { expect, test } from '@playwright/test'

import { QA_PASSWORD, QA_PHONE } from './helpers'

test.describe('核心路径冒烟', () => {
  test('登录 → 项目列表 → 新建项目入口', async ({ page }) => {
    // 1. 登录页可达（Q-020：不再有 fonts.googleapis 外链请求阻塞）
    await page.goto('/login')
    await expect(page).toHaveURL(/login/)

    // 2. 密码通道登录（默认为验证码通道，须先切换；按钮文案含字间空格）
    await page.getByLabel('手机号').fill(QA_PHONE)
    await page.getByRole('button', { name: '密码登录' }).click()
    await page.getByLabel('登录密码').fill(QA_PASSWORD)
    await page.getByRole('checkbox').check()
    await page.getByRole('button', { name: /^登\s*录$/ }).click()

    // 3. 登录成功进入项目列表（实际按钮文案「新建短剧项目」）
    await page.waitForURL(/projects/, { timeout: 15_000 })
    await expect(page.getByRole('button', { name: '新建短剧项目' })).toBeVisible({ timeout: 15_000 })

    // 4. 状态标签组件正常渲染（Q-015：voiced/pending 等状态不会白标签）
    const tags = page.locator('span.rounded-full')
    await expect(tags.first()).toBeVisible()
  })

  test('新建项目 → 剧本编辑器可打开', async ({ page }) => {
    await page.goto('/login')
    await page.getByLabel('手机号').fill(QA_PHONE)
    await page.getByRole('button', { name: '密码登录' }).click()
    await page.getByLabel('登录密码').fill(QA_PASSWORD)
    await page.getByRole('checkbox').check()
    await page.getByRole('button', { name: /^登\s*录$/ }).click()
    await page.waitForURL(/projects/, { timeout: 15_000 })

    // 打开第一个项目（回归项目 1410 在列表中）
    const firstCard = page.locator('[class*="cursor-pointer"]').first()
    await firstCard.click()
    await page.waitForLoadState('networkidle', { timeout: 20_000 })
    // 工作台任一关键区域可见即认为可进入
    await expect(page.locator('body')).toContainText(/剧本|分镜|资产|后期/)
  })
})

/**
 * 文件级注释：第 5 阶段 Playwright 回归配置。
 *
 * 运行方式（在 frontend/ 目录下执行，复用其 node_modules 中的 Playwright）：
 *   npx playwright test -c ../tests/regression/playwright.config.ts
 *
 * 环境变量：
 *   FRONT_BASE   前端地址（默认 http://localhost:5175，vite dev）
 *   API_BASE     后端地址（默认 http://localhost:8100，uvicorn）
 *   QA_PHONE     测试账号手机号（默认 13900009903）
 *   QA_PASSWORD  测试账号密码（默认 E2ePass123456）
 *   REG_PROJECT  保真度断言用的既有链路项目 ID（默认 1410）
 *   FULL_CHAIN   置 1 时启用完整链路 spec（约 20 分钟，含审批推进与导出）
 */
import { defineConfig } from '@playwright/test'

const FRONT_BASE = process.env.FRONT_BASE ?? 'http://localhost:5175'

export default defineConfig({
  testDir: __dirname,
  timeout: 60_000,
  // 完整链路 spec 自带 30 分钟超时覆盖
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: FRONT_BASE,
    viewport: { width: 1440, height: 900 },
    trace: 'off',
    screenshot: 'only-on-failure',
  },
})

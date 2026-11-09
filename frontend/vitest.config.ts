/**
 * 文件级注释：Vitest 单元测试配置。
 * - 复用 Vue SFC 插件与 `@` 别名，保证测试内引入与业务代码一致；
 * - 使用 jsdom 环境模拟浏览器 DOM；
 * - 统一加载测试环境补丁（PointerEvent / 媒体元素能力）。
 */
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  /** 插件：仅需 Vue SFC 编译能力 */
  plugins: [vue()],
  /** 模块解析：`@` 别名指向 src */
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  /** 测试运行配置 */
  test: {
    /** 浏览器 DOM 模拟环境 */
    environment: 'jsdom',
    /** 测试环境补丁（指针事件与媒体元素桩） */
    setupFiles: ['./src/test/setup.ts'],
    /** 仅收集 src 下的 *.spec.ts，避免误跑其它目录 */
    include: ['src/**/*.spec.ts'],
    /** 每个用例结束后恢复被 spy 的原始实现 */
    restoreMocks: true,
  },
})

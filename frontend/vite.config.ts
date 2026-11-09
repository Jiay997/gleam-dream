/**
 * 文件级注释：Vite 构建配置。
 * - 注册 Vue 3 单文件组件插件；
 * - 配置 `@` 别名指向 src 目录；
 * - 开发服务器端口 5175（本机 5173/5174 被其他项目占用），并将 /api、/files 请求
 *   代理到本地后端（127.0.0.1:8100，本机 8000 端口被其他项目占用）。
 */
import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  /** 插件列表：Vue SFC 支持 */
  plugins: [vue()],
  /** 模块解析配置：`@` 别名映射到 src 目录 */
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  /** 开发服务器配置：固定端口、双栈监听与后端代理 */
  server: {
    /** 固定端口，被占用时直接报错而不是顺延，保证运维脚本可预测 */
    port: 5175,
    strictPort: true,
    /** 监听全部地址（IPv4 127.0.0.1 + IPv6 ::1）——Vite 默认仅 ::1 会导致 127.0.0.1 访问被拒 */
    host: true,
    proxy: {
      '/api': 'http://127.0.0.1:8100',
      '/files': 'http://127.0.0.1:8100',
    },
  },
})


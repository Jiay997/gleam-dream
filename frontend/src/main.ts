/**
 * 文件级注释：应用入口。
 * - 创建 Vue 应用实例并挂载 #app；
 * - 依次注册 Pinia（状态管理）、Vue Router（路由）、VueKonva（分镜画布渲染）；
 * - 引入全局样式（Tailwind 入口 + 设计令牌 + 全局基础样式）。
 */
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import VueKonva from 'vue-konva'

import App from './App.vue'
import router from './router'

import '@/assets/styles/index.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
app.use(VueKonva)

app.mount('#app')

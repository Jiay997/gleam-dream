<!--
  文件级注释：应用根组件。
  - 裸路由（登录 / 注册 / 引导）直接渲染 router-view；
  - 其余路由统一渲染 AppShell（全局导航 + 顶栏 + 项目导航 + 内容区 + 状态条）；
  - 全局 Toast 浮层挂载于根层级，供任意模块调用 useToast 弹出提示；
  - .gd-grain 是 aurora 的可选颗粒层（2.8% 噪点）：极暗渐变在 8bit 面板上会出色带，
    一层噪点把色带打散，同时给暗场一点胶片质感。fixed + pointer-events:none，
    不参与布局、不拦点击，删掉也不影响任何功能。
-->
<script setup lang="ts">
import { useRoute } from 'vue-router'

import AppShell from '@/components/layout/AppShell.vue'
import ToastHost from '@/components/common/ToastHost.vue'

const route = useRoute()
</script>

<template>
  <div class="gd-grain" aria-hidden="true" />
  <!-- 路由解析完成前不挂壳：否则首屏会误挂 AppShell 并触发未登录请求 -->
  <AppShell v-if="route.matched.length > 0 && !route.meta.bare" />
  <router-view v-else />
  <ToastHost />
</template>

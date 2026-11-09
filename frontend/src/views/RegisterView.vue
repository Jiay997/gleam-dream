<!--
  文件级注释：P01 注册页（裸路由，不渲染 AppShell）。
  与登录页复用同一 AuthPanel（由 route.name === 'register' 驱动注册 Tab 与方向感知切换过渡），
  本视图仅承担路由挂载。
-->
<script setup lang="ts">
/**
 * RegisterView 注册页。
 * 无 props / emit；表单逻辑（昵称/密码选填注册、强度提示、协议校验）全部由 AuthPanel 承担。
 */
import AuthPanel from '@/components/auth/AuthPanel.vue'
</script>

<template>
  <AuthPanel />
</template>

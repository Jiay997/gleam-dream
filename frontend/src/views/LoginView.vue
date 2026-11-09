<!--
  文件级注释：P01 登录页（裸路由，不渲染 AppShell）。
  页面本体为 AuthPanel（登录 / 注册共享面板，v4「首映夜场」暗场域：全屏暗场舞台 +
  暗玻璃表单卡 + 极光光卡 + 三条光影条 + 胶片颗粒），由 route.name 驱动 Tab 状态，
  本视图仅承担路由挂载，不注入任何额外包装层。
-->
<script setup lang="ts">
/**
 * LoginView 登录页。
 * 无 props / emit；表单逻辑（验证码/密码双通道、倒计时、校验）全部由 AuthPanel 承担。
 */
import AuthPanel from '@/components/auth/AuthPanel.vue'
</script>

<template>
  <AuthPanel />
</template>

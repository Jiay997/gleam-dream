<!--
  文件级注释：镜头详情路由兼容页（/projects/:projectId/storyboard/shots/:shotId）。
  v7 纸上放映厅 Pro · 对照 F4-03（P13 镜头详情抽屉）：P13 抽屉本体为分镜画布内
  DrawerPanel（components/storyboard/ShotDetailDrawer.vue，视觉已对照 F4-03 重构）；
  本页仅做深链兼容：携带 ?shot={shotId} 重定向回分镜画布，由画布在全部分镜板中
  定位镜头并自动打开抽屉。
-->
<script setup lang="ts">
/**
 * ShotDrawerView 镜头详情深链兼容页。
 * props：无。emits：无。
 * 挂载即重定向：/projects/{projectId}/storyboard?shot={shotId}。
 */
import { onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

// 挂载即重定向（保留镜头 ID 作为画布深链查询参数）
onMounted(() => {
  const projectId = String(route.params.projectId ?? '')
  const shotId = String(route.params.shotId ?? '')
  void router.replace({
    path: `/projects/${projectId}/storyboard`,
    query: shotId ? { shot: shotId } : {},
  })
})
</script>

<template>
  <div class="flex h-full items-center justify-center bg-surface-canvas">
    <p class="gd-num text-sm text-neutral-400">正在跳转到分镜画布定位镜头…</p>
  </div>
</template>

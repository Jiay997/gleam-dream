<!--
  文件级注释：P27 · V4.0 能力中心（路由 /projects/:projectId/v4）。

  背景：PRD V4.0 并入 MVP 的 6 项 P0 细节功能与 26 条新 US / 21 个新端点，
  能力分散在剧本、资产、分镜、生成、后期、导出六个域。本页把这些增量能力
  **集中承载**，避免在既有高信息密度页面中叠床架屋：

  - 剧本：版本列表/恢复/命名、导出 md/docx/txt、续写、生成终止、模板套用；
  - 资产：标签与搜索、重抽成本透明、批量重抽、场景变体；
  - 分镜：节奏模式、批量改镜头、复制、操作历史回退、静帧重出、分镜模板；
  - 生成：六态仪表盘与 ETA、人工标记合格、放弃、批量/延迟重试、插队、按集锁定；
  - 后期：逐角色试听调参、字幕逐条编辑与批量样式、BGM 替换、混音与响度；
  - 导出：导出历史与参数复用、平台规格自检、AI 内容标识预览、导出预设、
    分发重试与撤回；
  - 授权：真人授权记录与撤回。

  所有面板共用当前项目上下文（project store / 路由参数），任一操作完成后
  通过 `reload` 事件刷新本页数据。
-->
<script setup lang="ts">
/**
 * V4CapabilityView V4 能力中心。
 *
 * 结构：顶部分段导航（按 F 域分组）+ 对应面板；面板自带数据加载与错误提示
 * （http 拦截器统一 toast），本页只负责上下文与分组切换。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import { listAssets } from '@/api/assets'
import { listExportHistory } from '@/api/v4'
import type { DistributionRecordItem } from '@/api/export'
import { listDistributions } from '@/api/export'
import { listStoryboards } from '@/api/storyboard'
import AssetV4Panel from '@/components/v4/AssetV4Panel.vue'
import ExportV4Panel from '@/components/v4/ExportV4Panel.vue'
import FaceAuthV4Panel from '@/components/v4/FaceAuthV4Panel.vue'
import PostV4Panel from '@/components/v4/PostV4Panel.vue'
import RenderDashboardPanel from '@/components/v4/RenderDashboardPanel.vue'
import ScriptV4Panel from '@/components/v4/ScriptV4Panel.vue'
import StoryboardV4Panel from '@/components/v4/StoryboardV4Panel.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import SegmentedControl from '@/components/common/SegmentedControl.vue'

const route = useRoute()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/** 当前分组 */
const tab = ref<'script' | 'asset' | 'storyboard' | 'render' | 'post' | 'export' | 'auth'>('script')

/** 分组定义 */
const TABS = [
  { value: 'script', label: '剧本（F2）' },
  { value: 'asset', label: '资产（F3）' },
  { value: 'storyboard', label: '分镜（F4）' },
  { value: 'render', label: '生成（F5）' },
  { value: 'post', label: '后期（F6）' },
  { value: 'export', label: '导出（F7）' },
  { value: 'auth', label: '真人授权（F3-12）' },
] as const

/** 资产列表（角色/场景） */
const assets = ref<Array<Record<string, unknown>>>([])
/** 角色资产（后期面板用） */
const characters = computed(() => assets.value.filter((asset) => asset.asset_type === 'character'))
/** 当前选中资产 */
const selectedAssetId = ref<number | null>(null)
/** 分镜板 ID */
const storyboardId = ref<string | number>('')
/** 分镜镜头 ID 列表 */
const shotIds = ref<number[]>([])
/** 首集 ID */
const firstEpisodeId = ref<number | null>(null)
/** 总集数 */
const episodeCount = ref(0)
/** 渲染任务（仪表盘用） */
const renderTasks = ref<Array<Record<string, unknown>>>([])
/** 分发记录（导出面板用） */
const distributions = ref<DistributionRecordItem[]>([])
/** 导出条数（仅用于首屏提示） */
const exportTotal = ref(0)

/**
 * 加载页面所需的上下文数据（资产 / 分镜 / 集信息 / 渲染任务 / 分发记录）。
 * @returns 无
 */
async function loadContext(): Promise<void> {
  if (!projectId.value) return
  const [assetPage, boards, exportPage, distributionPage] = await Promise.all([
    listAssets(projectId.value, { page: 1, page_size: 100 }).catch(() => null),
    listStoryboards(projectId.value).catch(() => null),
    listExportHistory(projectId.value).catch(() => null),
    listDistributions(projectId.value).catch(() => null),
  ])
  assets.value = (assetPage?.items ?? []) as unknown as Array<Record<string, unknown>>
  const firstBoard = boards?.[0]
  if (firstBoard) {
    storyboardId.value = firstBoard.id
    shotIds.value = []
  }
  exportTotal.value = exportPage?.total ?? 0
  distributions.value = distributionPage?.items ?? []
  const firstEpisode = (firstBoard as { episode_id?: number } | undefined)?.episode_id
  firstEpisodeId.value = firstEpisode ?? null
}

/**
 * 刷新可见数据（面板操作完成后的统一入口）。
 * @returns 无
 */
async function refreshAll(): Promise<void> {
  await loadContext()
}

onMounted(loadContext)
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <header class="shrink-0 px-6 pb-4 pt-5 lg:px-8">
      <div class="flex flex-wrap items-end justify-between gap-3">
        <div class="flex min-w-0 items-center gap-3">
          <span class="h-7 w-1 shrink-0 rounded-full bg-grad-primary shadow-gd-bloom" aria-hidden="true"></span>
          <h1 class="gd-masthead truncate text-display-lg">V4 能力中心</h1>
        </div>
        <SegmentedControl v-model="tab" :options="[...TABS]" size="sm" aria-label="V4 能力分组" />
      </div>
    </header>

    <main class="min-h-0 flex-1 overflow-y-auto px-6 pb-10 lg:px-8">
      <div class="mx-auto flex max-w-[1200px] flex-col gap-4">
        <ScriptV4Panel
          v-if="tab === 'script'"
          :project-id="projectId"
          :episode-count="episodeCount"
          @reload="refreshAll"
        />

        <AssetV4Panel
          v-else-if="tab === 'asset'"
          :assets="assets"
          :selected-asset-id="selectedAssetId"
          @refresh="refreshAll"
        />

        <StoryboardV4Panel
          v-else-if="tab === 'storyboard' && storyboardId"
          :project-id="projectId"
          :storyboard-id="storyboardId"
          :shot-ids="shotIds"
          @reload="refreshAll"
        />
        <EmptyState
          v-else-if="tab === 'storyboard'"
          title="该集尚无分镜板"
          description="先在「分镜」页发起拆解，完成后即可在此调整节奏模式与回退画布操作。"
          icon-path="M3.75 6A2.25 2.25 0 016 3.75h3v16.5H6A2.25 2.25 0 013.75 18V6z"
        />

        <RenderDashboardPanel
          v-else-if="tab === 'render'"
          :project-id="projectId"
          :tasks="renderTasks"
          :episode-id="firstEpisodeId"
          @refresh="refreshAll"
        />

        <PostV4Panel
          v-else-if="tab === 'post'"
          :project-id="projectId"
          :characters="characters"
          @reload="refreshAll"
        />

        <ExportV4Panel
          v-else-if="tab === 'export'"
          :project-id="projectId"
          :distributions="distributions as unknown as Array<Record<string, unknown>>"
          @reload="refreshAll"
        />

        <FaceAuthV4Panel v-else :project-id="projectId" @reload="refreshAll" />
      </div>
    </main>
  </div>
</template>

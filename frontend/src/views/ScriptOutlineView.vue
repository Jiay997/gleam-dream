<!--
  文件级注释：P07 · 分集大纲（路由 /projects/:projectId/script/outline，AppShell 布局）。
  v7 纸上放映厅 Pro · 对照 F2-04（P07 分集大纲）：
  - 左集列表改集卡形态：ep_no + 标题 + hook_suggestion 行（hook_text 前缀展示），
    选中卡紫描边（border-primary-500 + ring），超时长卡附「△ est_duration_sec NNNs > 180s
    · 建议拆分」琥珀警示条（warning-bg）；
  - 右侧集大纲卡：衬线「集大纲 · 第 N 集」标题 + 目标时长徽标（越限转金雾底金字「超限」）
    + hook_suggestion 块 + 场次列表行卡（场号 chip + 地点·内外景·时段 + 右侧行数）；
  - 节奏总览条形图：紫条常态、越限转金条 + 红线 180s 标记（绝对定位竖线），图例注明；
  - 空态 / 骨架 / 重试三态保留，点击场次跳编辑器锚点逻辑保留。
  数据：GET /projects/{id}/script-outline；点击场次 → 跳编辑器锚点（query 传 episode/scene）。
-->
<script setup lang="ts">
/**
 * ScriptOutlineView 分集大纲。
 * props：无。emits：无。
 * 依赖 project store（当前项目 ID / 标题）与 useToast。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { getScriptOutline, type OutlineEpisode, type OutlineScene } from '@/api/script'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useToast } from '@/composables/useToast'

/** 路由实例（跳编辑器锚点） */
const route = useRoute()
const router = useRouter()
/** Toast */
const toast = useToast()

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/** 单集目标时长上限（秒），超过提示裁剪 */
const DURATION_LIMIT = 180

/* ---------- 数据状态 ---------- */
/** 大纲剧集列表 */
const episodes = ref<OutlineEpisode[]>([])
/** 加载中 */
const loading = ref(true)
/** 加载失败信息 */
const errorMsg = ref('')
/** 当前选中集号（默认第一集） */
const activeEpisodeNo = ref(1)

/* ---------- 归一化工具 ---------- */

/**
 * 归一化集号（episode_no / index / 列表序号兜底）。
 * @param episode 大纲剧集
 * @param index 列表序号
 * @returns 集号
 */
function episodeNoOf(episode: OutlineEpisode, index: number): number {
  return Number(episode.episode_no ?? episode.index ?? index + 1)
}

/**
 * 归一化集目标时长（秒，兼容 target_duration_seconds / target_duration_sec）。
 * @param episode 大纲剧集
 * @returns 时长秒数（缺失返回 0）
 */
function durationOf(episode: OutlineEpisode): number {
  return Number(episode.target_duration_seconds ?? episode.target_duration_sec ?? 0)
}

/**
 * 归一化场次号。
 * @param scene 大纲场次
 * @param index 列表序号
 * @returns 场次号
 */
function sceneNoOf(scene: OutlineScene, index: number): number {
  return Number(scene.scene_no ?? scene.index ?? index + 1)
}

/**
 * 秒数格式化为 mm:ss（不足 1 分钟显示秒）。
 * @param seconds 秒数
 * @returns 格式化文案
 */
function formatDuration(seconds: number): string {
  if (seconds <= 0) return '—'
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  return `${m}:${String(s).padStart(2, '0')}`
}

/* ---------- 派生数据 ---------- */

/** 全部集最大时长（条形图归一化基准） */
const maxDuration = computed(() => Math.max(1, ...episodes.value.map((ep) => durationOf(ep))))

/** 当前选中集（找不到回退第一集） */
const activeEpisode = computed<OutlineEpisode | null>(() => {
  const found = episodes.value.find((ep, index) => episodeNoOf(ep, index) === activeEpisodeNo.value)
  return found ?? episodes.value[0] ?? null
})

/** 当前选中集的场次列表 */
const activeScenes = computed<OutlineScene[]>(() => {
  const scenes = activeEpisode.value?.scenes
  return Array.isArray(scenes) ? scenes : []
})

/**
 * 时长条形宽度百分比（按全集最大值归一化）。
 * @param seconds 集时长（秒）
 * @returns 0~100 整数
 */
function barWidth(seconds: number): number {
  if (seconds <= 0) return 2
  return Math.max(4, Math.round((seconds / maxDuration.value) * 100))
}

/** 红线 180s 在条形图中的横向位置（%，超出归一化范围时钳制在 4~100） */
const redLinePercent = computed(() => Math.min(100, Math.max(4, Math.round((DURATION_LIMIT / maxDuration.value) * 100))))

/* ---------- 数据加载 ---------- */

/**
 * 拉取分集大纲（GET /projects/{id}/script-outline）。
 * @returns 无
 */
async function fetchOutline(): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  try {
    const data = await getScriptOutline(projectId.value)
    episodes.value = Array.isArray(data?.episodes) ? data.episodes : []
    // 默认选中第一集
    if (episodes.value.length > 0) {
      activeEpisodeNo.value = episodeNoOf(episodes.value[0], 0)
    }
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '大纲加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 选择集。
 * @param no 集号
 * @returns 无
 */
function selectEpisode(no: number): void {
  activeEpisodeNo.value = no
}

/**
 * 点击场次：跳编辑器对应锚点（query 传 episode/scene）。
 * @param sceneNo 场次号
 * @returns 无
 */
function goScene(sceneNo: number): void {
  const ep = activeEpisodeNo.value
  toast.info(`跳转至第 ${ep} 集 · 第 ${sceneNo} 场`)
  void router.push({ path: `/projects/${projectId.value}/script`, query: { episode: String(ep), scene: String(sceneNo) } })
}

/**
 * 跳转剧本编辑器（空态引导）。
 * @returns 无
 */
function goEditor(): void {
  void router.push(`/projects/${projectId.value}/script`)
}

// 挂载：拉取大纲
onMounted(() => {
  void fetchOutline()
})
</script>

<template>
  <div class="p-6 lg:p-8">
    <!-- ============ 页头 ============ -->
    <PageHeader title="分集大纲" />

    <!-- ============ 加载骨架 ============ -->
    <div v-if="loading" class="grid grid-cols-1 gap-4 lg:grid-cols-12">
      <LoadingSkeleton class="lg:col-span-3" variant="card" />
      <LoadingSkeleton class="lg:col-span-9" variant="text" :rows="8" />
    </div>

    <!-- ============ 加载失败 ============ -->
    <ErrorRetryBlock v-else-if="errorMsg" message="分集大纲加载失败" :description="errorMsg" @retry="fetchOutline" />

    <!-- ============ 空态：无大纲 ============ -->
    <EmptyState
      v-else-if="episodes.length === 0"
      icon="📑"
      title="还没有分集大纲"
      description="先生成或导入剧本后，这里将展示分集节奏、钩子与场次结构"
      action-text="前往剧本编辑器"
      @action="goEditor"
    />

    <!-- ============ 数据区 ============ -->
    <template v-else>
      <!-- ===== 节奏总览：集时长分布条形图（紫条常态 / 越限金条 + 红线 180s，对照 F2-04 底部图） ===== -->
      <section class="gd-card mb-5 p-6">
        <div class="flex items-center justify-between">
          <h2 class="font-serif text-title text-neutral-900">集时长分布</h2>
          <p class="text-caption text-neutral-400">单集时长上限 180s · 超限金色警示「建议拆分」</p>
        </div>
        <ul class="relative mt-4 space-y-2.5">
          <!-- 红线 180s 标记（跨全部条行；轨道起点 = 集号列 4rem + 间距 0.75rem，轨道宽 = 100% - 8.5rem） -->
          <span
            class="absolute bottom-0 top-0 w-px bg-danger"
            :style="{ left: `calc(4.75rem + (100% - 8.5rem) * ${redLinePercent / 100})` }"
            aria-hidden="true"
          />
          <li v-for="(ep, index) in episodes" :key="index" class="relative flex items-center gap-3">
            <button
              type="button"
              class="gd-num w-16 shrink-0 text-right text-xs transition-colors duration-gd-fast"
              :class="episodeNoOf(ep, index) === activeEpisodeNo ? 'font-semibold text-primary-700' : 'text-neutral-500 hover:text-neutral-700'"
              @click="selectEpisode(episodeNoOf(ep, index))"
            >
              EP{{ String(episodeNoOf(ep, index)).padStart(2, '0') }}
            </button>
            <div class="h-4 min-w-0 flex-1 overflow-hidden rounded bg-surface-sunken">
              <div
                class="flex h-full items-center rounded transition-[width] duration-gd-slow"
                :class="durationOf(ep) > DURATION_LIMIT ? 'bg-gold' : 'bg-primary-400'"
                :style="{ width: `${barWidth(durationOf(ep))}%` }"
              />
            </div>
            <span class="gd-num w-12 shrink-0 text-xs" :class="durationOf(ep) > DURATION_LIMIT ? 'font-semibold text-gold-deep' : 'text-neutral-500'">
              {{ durationOf(ep) > 0 ? `${durationOf(ep)}s` : '—' }}
            </span>
          </li>
        </ul>
      </section>

      <!-- ===== 主体：左集卡列表 + 右集详情 ===== -->
      <div class="grid grid-cols-1 gap-5 lg:grid-cols-12">
        <!-- 左：集卡列表（对照 F2-04：ep_no + 标题 + hook_suggestion + 超限警示条） -->
        <nav class="lg:col-span-3" aria-label="分集列表">
          <ul class="space-y-3">
            <li v-for="(ep, index) in episodes" :key="index">
              <button
                type="button"
                class="relative w-full rounded-lg border p-4 text-left transition-[border-color,box-shadow,background-color] duration-gd-fast"
                :class="episodeNoOf(ep, index) === activeEpisodeNo
                  ? 'border-primary-500 bg-surface-raised shadow-gd-1 ring-1 ring-primary-500'
                  : 'border-hairline bg-surface-raised hover:border-primary-300'"
                @click="selectEpisode(episodeNoOf(ep, index))"
              >
                <div class="flex items-center justify-between gap-2">
                  <span class="gd-num text-xs font-semibold" :class="episodeNoOf(ep, index) === activeEpisodeNo ? 'text-primary-700' : 'text-neutral-400'">
                    ep_{{ String(episodeNoOf(ep, index)).padStart(2, '0') }}
                  </span>
                  <span class="gd-num shrink-0 text-xs" :class="durationOf(ep) > DURATION_LIMIT ? 'font-semibold text-gold-deep' : 'text-neutral-400'">
                    {{ formatDuration(durationOf(ep)) }}
                  </span>
                </div>
                <p class="mt-1.5 truncate text-sm font-medium text-neutral-900" :title="ep.title || ''">{{ ep.title || `未命名分集` }}</p>
                <!-- hook_suggestion 行 -->
                <p class="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-500" :title="ep.hook_text || ''">
                  <template v-if="ep.hook_text">hook_suggestion：{{ ep.hook_text }}</template>
                  <template v-else><span class="text-neutral-300">hook_suggestion：—</span></template>
                </p>
                <!-- 超时长琥珀警示条（对照 F2-04「△ est_duration_sec … > 180s · 建议拆分」） -->
                <p
                  v-if="durationOf(ep) > DURATION_LIMIT"
                  class="mt-2 flex items-center gap-1.5 rounded-md border border-warning-border bg-warning-bg px-2 py-1 text-micro font-medium leading-4 text-warning"
                  role="alert"
                >
                  <svg class="h-3 w-3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                    <path d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                  est_duration_sec {{ durationOf(ep) }}s &gt; {{ DURATION_LIMIT }}s · 建议拆分
                </p>
              </button>
            </li>
          </ul>
        </nav>

        <!-- 右：集大纲卡 + 场次行卡 -->
        <section class="lg:col-span-9">
          <template v-if="activeEpisode">
            <!-- 集大纲卡（对照 F2-04「集大纲 · EP03」） -->
            <article class="gd-card p-6">
              <div class="flex items-start justify-between gap-4">
                <h2 class="font-serif text-title text-neutral-900">集大纲 · 第 {{ activeEpisodeNo }} 集 · {{ activeEpisode.title || '未命名分集' }}</h2>
                <!-- 目标时长徽标：越限转金雾底金字（对照 F2-04「204 s · 超限」） -->
                <span
                  class="gd-num shrink-0 rounded-full px-3 py-1 text-xs font-semibold"
                  :class="durationOf(activeEpisode) > DURATION_LIMIT ? 'bg-gold-fog text-gold-deep' : 'bg-primary-50 text-primary-700'"
                >
                  target {{ formatDuration(durationOf(activeEpisode)) }}<template v-if="durationOf(activeEpisode) > DURATION_LIMIT"> · 超限</template>
                </span>
              </div>
              <p v-if="activeEpisode.logline || activeEpisode.summary" class="mt-3 text-sm leading-relaxed text-neutral-600">
                {{ activeEpisode.logline || activeEpisode.summary }}
              </p>
              <!-- hook_suggestion 块（对照 F2-04 钩子字段） -->
              <div v-if="activeEpisode.hook_text" class="mt-4 flex items-start gap-2.5 rounded-lg border border-warning-border bg-warning-bg p-4">
                <svg class="mt-0.5 h-4 w-4 shrink-0 text-gold-deep" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                  <path d="M13 10V3L4 14h7v7l9-11h-7z" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
                <div>
                  <p class="gd-num text-xs font-semibold text-gold-deep">hook_suggestion（≤ 50 字）</p>
                  <p class="mt-0.5 text-sm leading-relaxed text-neutral-700">{{ activeEpisode.hook_text }}</p>
                </div>
              </div>
              <p v-else class="mt-4 rounded-lg border border-dashed border-hairline p-4 text-center text-xs text-neutral-400">
                本集暂无结尾钩子——短剧留存关键，建议在编辑器中补充
              </p>
            </article>

            <!-- 场次行卡列表（对照 F2-04「场次 · 场次列表」） -->
            <div class="mb-3 mt-5 flex items-center justify-between">
              <h3 class="font-serif text-title text-neutral-900">场次 · 场次列表</h3>
              <p class="text-caption text-neutral-400">点击场次跳转编辑器对应位置</p>
            </div>
            <ul v-if="activeScenes.length > 0" class="gd-card-flat divide-y divide-hairline-soft overflow-hidden">
              <li v-for="(scene, scIndex) in activeScenes" :key="scIndex">
                <button
                  type="button"
                  class="group flex w-full items-start gap-4 p-4 text-left transition-colors duration-gd-fast hover:bg-surface-canvas"
                  @click="goScene(sceneNoOf(scene, scIndex))"
                >
                  <!-- 场号 chip（对照 F2-04「场 1」紫字小标） -->
                  <span class="gd-num shrink-0 rounded bg-primary-50 px-2 py-1 text-xs font-semibold text-primary-700 transition-colors duration-gd-normal group-hover:bg-primary-100">
                    场 {{ sceneNoOf(scene, scIndex) }}
                  </span>
                  <!-- 场信息 -->
                  <div class="min-w-0 flex-1">
                    <div class="flex flex-wrap items-center gap-2">
                      <span class="text-sm font-medium text-neutral-900">{{ scene.location || scene.heading || '未命名场景' }}</span>
                      <span v-if="scene.int_ext" class="rounded border border-hairline bg-surface-canvas px-1.5 py-0.5 text-caption text-neutral-500">{{ scene.int_ext }}</span>
                      <span v-if="scene.time_of_day" class="rounded border border-hairline bg-surface-canvas px-1.5 py-0.5 text-caption text-neutral-500">{{ scene.time_of_day }}</span>
                    </div>
                    <p class="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-500">{{ scene.summary || '暂无场景摘要' }}</p>
                  </div>
                  <!-- 行数统计 + 跳转 -->
                  <div class="flex shrink-0 flex-col items-end gap-1">
                    <span class="gd-num text-xs text-neutral-400">{{ scene.line_count ?? 0 }} 行</span>
                    <span class="text-xs text-primary-500 opacity-0 transition-opacity duration-gd-normal group-hover:opacity-100">去编辑 →</span>
                  </div>
                </button>
              </li>
            </ul>
            <div v-else class="rounded-lg border border-dashed border-hairline bg-surface-raised py-10 text-center text-xs text-neutral-400">
              本集暂无场次信息
            </div>
          </template>
        </section>
      </div>
    </template>
  </div>
</template>

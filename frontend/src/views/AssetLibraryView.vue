<!--
  文件级注释：资产库 · 项目管理库（路由 /assets，AppShell 布局）。
  定位（与链路 STAGE 03「资产设计」严格分离）：
  - 本页 = 档案室：先选项目，再进入该项目只读资产档案，查阅已生成 / 已通过 / 已锁定（定稿）资产；
  - 链路 /projects/:projectId/assets = 设计台：负责资产规划、生成、质检、审核与锁定；
  - 页面首屏用「本页 / 链路」双卡明确定位，避免两个资产入口混淆。
  视觉语言沿用「极夜 · 极光」；项目以「档案封套」呈现：左侧窄封面书脊（coverTone 确定性取色）
  + 右侧项目信息与角色 / 场景 / 道具三格计数 + 定稿印章。
-->
<script setup lang="ts">
/**
 * AssetLibraryView 资产库首页（项目管理库）。
 * props：无。emits：无。
 * 依赖 projects / assets API；点击项目卡进入 /assets/:projectId 只读资产档案。
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { listAssets, type AssetItem } from '@/api/assets'
import { listProjects } from '@/api/projects'
import type { Project } from '@/api/types'
import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { coverTone } from '@/utils/coverTone'
import { firstChar, formatRelativeTime } from '@/utils/datetime'

/** 路由实例（进入项目资产档案 / 项目工作台） */
const router = useRouter()

/** 项目资产统计（仅角色 / 场景 / 道具三实体，不含音色库） */
interface ProjectAssetSummary {
  /** 角色资产数 */
  character: number
  /** 场景资产数 */
  scene: number
  /** 道具资产数 */
  prop: number
  /** 已有产出文件的资产数（current_version_id 非空） */
  generated: number
  /** 定稿资产数（approved + locked） */
  finalized: number
  /** 待人工确认资产数 */
  pending: number
}

/** 项目列表 */
const projects = ref<Project[]>([])
/** 项目 ID → 资产统计（加载失败的项目不出现在此映射中） */
const summaries = ref<Record<string, ProjectAssetSummary>>({})
/** 首屏加载中 */
const loading = ref(true)
/** 加载失败信息（空串表示正常） */
const errorMsg = ref('')
/** 项目关键词（标题 / 题材 / 简介） */
const keyword = ref('')
/** 档案筛选：all 全部项目 / finalized 已有定稿 / empty 空档案 */
const archiveFilter = ref<'all' | 'finalized' | 'empty'>('all')

/**
 * 聚合单项目资产统计（语音资产不计入实体档案）。
 * @param items 该项目的资产列表
 * @returns 三实体计数与生成 / 定稿 / 待确认计数
 */
function summarize(items: AssetItem[]): ProjectAssetSummary {
  const summary: ProjectAssetSummary = {
    character: 0,
    scene: 0,
    prop: 0,
    generated: 0,
    finalized: 0,
    pending: 0,
  }
  for (const asset of items) {
    if (asset.asset_type === 'voice') continue
    summary[asset.asset_type] += 1
    if (asset.current_version_id) summary.generated += 1
    if (asset.status === 'approved' || asset.status === 'locked') summary.finalized += 1
    if (asset.status === 'pending_review') summary.pending += 1
  }
  return summary
}

/**
 * 拉取项目列表与各项目资产统计（并行请求；单个项目统计失败不影响其它项目）。
 * @returns 无
 * @throws 不抛出（错误写入 errorMsg 由 ErrorRetryBlock 承接）
 */
async function loadLibrary(): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  try {
    const page = await listProjects({ page: 1, page_size: 100, sort: '-updated_at' })
    projects.value = page.items
    const entries = await Promise.all(
      page.items.map(async (project) => {
        try {
          const assetPage = await listAssets(project.id, { page: 1, page_size: 100 })
          return [String(project.id), summarize(assetPage.items)] as const
        } catch {
          return [String(project.id), null] as const
        }
      }),
    )
    summaries.value = Object.fromEntries(
      entries.filter((entry): entry is readonly [string, ProjectAssetSummary] => entry[1] !== null),
    )
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '资产库加载失败'
  } finally {
    loading.value = false
  }
}

/** 全部项目按当前关键词与档案状态过滤 */
const filteredProjects = computed<Project[]>(() => {
  const text = keyword.value.trim().toLowerCase()
  return projects.value.filter((project) => {
    const summary = summaries.value[String(project.id)]
    if (archiveFilter.value === 'finalized' && !(summary && summary.finalized > 0)) return false
    if (archiveFilter.value === 'empty' && summary) {
      const entityTotal = summary.character + summary.scene + summary.prop
      if (entityTotal > 0) return false
    }
    if (!text) return true
    return [project.title, project.genre ?? '', project.synopsis ?? ''].join(' ').toLowerCase().includes(text)
  })
})

/** 全库已生成实体资产总数 */
const totalGenerated = computed(() =>
  Object.values(summaries.value).reduce((sum, item) => sum + item.generated, 0),
)

/** 全库定稿实体资产总数 */
const totalFinalized = computed(() =>
  Object.values(summaries.value).reduce((sum, item) => sum + item.finalized, 0),
)

/** 全库待确认实体资产总数 */
const totalPending = computed(() =>
  Object.values(summaries.value).reduce((sum, item) => sum + item.pending, 0),
)

/** 档案筛选选项 */
const ARCHIVE_FILTERS: Array<{ key: 'all' | 'finalized' | 'empty'; label: string }> = [
  { key: 'all', label: '全部项目' },
  { key: 'finalized', label: '已有定稿' },
  { key: 'empty', label: '空档案' },
]

/** 三实体计数展示配置（标签 + 语义色） */
const ENTITY_METRICS: Array<{ key: 'character' | 'scene' | 'prop'; label: string; tone: string }> = [
  { key: 'character', label: '角色', tone: 'text-ink-brand' },
  { key: 'scene', label: '场景', tone: 'text-info' },
  { key: 'prop', label: '道具', tone: 'text-gold' },
]

/**
 * 项目封面编号（档案封套书脊上的 No. 编号，按 ID 补零）。
 * @param project 项目对象
 * @returns 三位编号文案
 */
function archiveNo(project: Project): string {
  return String(project.id).padStart(3, '0')
}

/**
 * 项目资产统计的安全读取（未加载完成时返回 null）。
 * @param project 项目对象
 * @returns 资产统计或 null
 */
function summaryOf(project: Project): ProjectAssetSummary | null {
  return summaries.value[String(project.id)] ?? null
}

/**
 * 读取某一实体类型的资产计数。
 * @param summary 项目资产统计（可为 null）
 * @param key 实体类型键
 * @returns 计数；统计未加载时返回 null
 */
function entityCount(summary: ProjectAssetSummary | null, key: 'character' | 'scene' | 'prop'): number | null {
  return summary ? summary[key] : null
}

/**
 * 进入指定项目的资产档案页。
 * @param project 项目对象
 * @returns 无
 */
function openProject(project: Project): void {
  void router.push(`/assets/${project.id}`)
}

onMounted(() => {
  void loadLibrary()
})
</script>

<template>
  <div class="mx-auto w-full max-w-content px-4 py-7 sm:px-6 lg:px-8">
    <PageHeader title="资产档案库">
      <template #actions>
        <AppButton variant="subtle" size="sm" @click="loadLibrary">刷新档案</AppButton>
        <AppButton variant="primary" size="sm" @click="router.push('/projects')">进入项目工作台</AppButton>
      </template>
    </PageHeader>

    <!-- 定位说明：本页（归档查阅）↔ 链路 STAGE 03（设计生成），入口与职责一眼可分 -->
    <section class="mb-6 grid gap-3 lg:grid-cols-2" aria-label="资产库与资产设计的定位区别">
      <div
        class="rounded-[18px] border border-[var(--gd-line-brand)] bg-[linear-gradient(135deg,rgba(139,124,255,0.14),rgba(17,26,44,0.55)_62%)] p-4"
      >
        <div class="flex items-center gap-2">
          <span class="rounded-full bg-[var(--gd-violet-wash)] px-2 py-0.5 font-mono text-[10px] tracking-[0.14em] text-primary-300">
            本页
          </span>
          <p class="text-sm font-semibold text-ink">资产库 · 归档查阅</p>
        </div>
        <p class="mt-2 text-caption leading-relaxed text-ink-soft">
          先展示项目管理库：选择项目后进入该项目的资产档案，按角色 / 场景 / 道具查看已生成、已通过设计审核定稿的资产。
        </p>
      </div>
      <div class="rounded-[18px] border border-hairline bg-[rgba(255,255,255,0.025)] p-4">
        <div class="flex items-center gap-2">
          <span class="rounded-full bg-[rgba(255,255,255,0.06)] px-2 py-0.5 font-mono text-[10px] tracking-[0.14em] text-ink-mute">
            STAGE 03
          </span>
          <p class="text-sm font-semibold text-ink">链路 · 资产设计</p>
        </div>
        <p class="mt-2 text-caption leading-relaxed text-ink-soft">
          规划、生成、质检、审核与锁定锚点，都在项目链路的「资产设计」中完成；完成后资产自动沉淀到本资产库。
        </p>
        <RouterLink
          to="/projects"
          class="mt-2 inline-flex items-center gap-1 text-caption font-medium text-ink-brand transition-colors duration-200 hover:text-ink"
        >
          从项目工作台进入 STAGE 03
          <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
            <path d="M4.5 12h15m0 0l-6-6m6 6l-6 6" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </RouterLink>
      </div>
    </section>

    <!-- 全库统计条 -->
    <section
      class="mb-6 grid grid-cols-3 divide-x divide-[var(--gd-line-soft)] overflow-hidden rounded-[18px] border border-hairline bg-[var(--gd-surface-glass)]"
      aria-label="资产库统计"
    >
      <div class="px-4 py-3.5 sm:px-5">
        <p class="gd-section-label">PROJECTS</p>
        <p class="mt-1 font-num text-[22px] leading-none text-ink">{{ projects.length }}</p>
        <p class="mt-1 text-[11px] text-ink-mute">个项目档案</p>
      </div>
      <div class="px-4 py-3.5 sm:px-5">
        <p class="gd-section-label">GENERATED</p>
        <p class="mt-1 font-num text-[22px] leading-none text-ink">{{ totalGenerated }}</p>
        <p class="mt-1 text-[11px] text-ink-mute">件已生成实体资产</p>
      </div>
      <div class="px-4 py-3.5 sm:px-5">
        <p class="gd-section-label">FINALIZED</p>
        <div class="mt-1 flex items-baseline gap-2">
          <p class="font-num text-[22px] leading-none text-success">{{ totalFinalized }}</p>
          <span v-if="totalPending > 0" class="font-num text-[11px] text-gd-pending">+{{ totalPending }} 待确认</span>
        </div>
        <p class="mt-1 text-[11px] text-ink-mute">件定稿资产</p>
      </div>
    </section>

    <!-- 筛选行：搜索 + 档案状态 chips -->
    <div class="mb-5 flex flex-wrap items-center gap-3">
      <label class="gd-field-group flex h-9 w-full max-w-[280px] items-center gap-2 px-3">
        <svg class="h-3.5 w-3.5 shrink-0 text-ink-mute" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <path d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <input
          v-model="keyword"
          type="search"
          class="w-full bg-transparent text-[12.5px] text-ink outline-none placeholder:text-ink-mute"
          placeholder="搜索项目名 / 题材 / 简介…"
          aria-label="搜索项目档案"
        />
      </label>
      <div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="档案状态筛选">
        <button
          v-for="option in ARCHIVE_FILTERS"
          :key="option.key"
          type="button"
          class="rounded-full border px-3 py-1.5 text-[12px] transition-[background-color,border-color,color] duration-200"
          :class="
            archiveFilter === option.key
              ? 'gd-chip-active border-transparent'
              : 'border-hairline bg-[rgba(255,255,255,0.025)] text-ink-mute hover:border-[var(--gd-line-brand)] hover:text-ink'
          "
          @click="archiveFilter = option.key"
        >
          {{ option.label }}
        </button>
      </div>
      <p class="ml-auto hidden text-caption text-ink-mute sm:block">共 {{ filteredProjects.length }} 个项目</p>
    </div>

    <div v-if="loading" class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3" aria-busy="true">
      <LoadingSkeleton v-for="index in 6" :key="`archive-skeleton-${index}`" variant="card" />
    </div>

    <ErrorRetryBlock v-else-if="errorMsg" message="资产库加载失败" :description="errorMsg" @retry="loadLibrary" />

    <EmptyState
      v-else-if="filteredProjects.length === 0"
      icon-path="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M2.25 18.75h16.5a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5H3.75A1.5 1.5 0 002.25 6v11.25a1.5 1.5 0 001.5 1.5z"
      title="没有匹配的项目档案"
      :description="projects.length === 0 ? '还没有项目，先创建项目并在链路中生成资产。' : '试试更换关键词或档案筛选条件。'"
      action-text="进入项目工作台"
      @action="router.push('/projects')"
    />

    <!-- 项目档案封套网格 -->
    <div v-else class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      <button
        v-for="project in filteredProjects"
        :key="project.id"
        type="button"
        class="group relative flex w-full overflow-hidden rounded-[18px] border border-hairline bg-[var(--gd-surface-glass)] text-left shadow-card transition-[transform,border-color,box-shadow] duration-[320ms] hover:-translate-y-0.5 hover:border-[var(--gd-line-brand)] hover:shadow-cardHover"
        @click="openProject(project)"
      >
        <!-- 书脊：确定性渐变封面 + 档案编号 -->
        <div class="relative w-[84px] shrink-0 sm:w-[96px]">
          <span class="absolute inset-0" :class="['gd-cv-cover', coverTone(project.id)]" aria-hidden="true" />
          <span class="absolute inset-y-4 left-3 w-px bg-[rgba(255,255,255,0.18)]" aria-hidden="true" />
          <span class="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 font-serif text-[34px] leading-none text-[#fffaf0]/90">
            {{ firstChar(project.title) }}
          </span>
          <span class="absolute inset-x-2 bottom-3 rounded-[6px] bg-[rgba(4,7,15,0.45)] py-1 text-center font-mono text-[9px] tracking-[0.14em] text-white/70">
            No.{{ archiveNo(project) }}
          </span>
        </div>

        <div class="min-w-0 flex-1 p-4">
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <p class="truncate font-serif text-[17px] font-semibold text-ink-heading">{{ project.title }}</p>
              <p class="mt-1 truncate text-[11px] text-ink-mute">
                {{ project.genre || '未设题材' }} · 更新于 {{ formatRelativeTime(project.updated_at) }}
              </p>
            </div>
            <StatusTag :status="project.status" size="sm" variant="outline" />
          </div>

          <p class="mt-2 line-clamp-2 text-caption leading-relaxed text-ink-soft">
            {{ project.synopsis || '暂无简介 · 档案仍在建立中' }}
          </p>

          <!-- 三实体计数 -->
          <div class="mt-3 grid grid-cols-3 gap-2">
            <div
              v-for="metric in ENTITY_METRICS"
              :key="metric.key"
              class="rounded-[10px] border border-[var(--gd-line-soft)] bg-[rgba(255,255,255,0.02)] px-2 py-1.5"
            >
              <p class="text-[10px] text-ink-mute">{{ metric.label }}</p>
              <p class="font-num text-[15px] leading-tight" :class="metric.tone">
                {{ entityCount(summaryOf(project), metric.key) ?? '—' }}
              </p>
            </div>
          </div>

          <div class="mt-3 flex items-center justify-between gap-2 border-t border-[var(--gd-line-soft)] pt-2.5">
            <span class="truncate text-[11px] text-ink-mute">
              <template v-if="summaryOf(project)">
                已生成 {{ summaryOf(project)!.generated }} · 定稿
                <span class="font-num text-success">{{ summaryOf(project)!.finalized }}</span>
              </template>
              <template v-else>资产统计暂不可用</template>
            </span>
            <span class="shrink-0 text-[12px] font-medium text-ink-brand transition-colors duration-200 group-hover:text-ink">
              打开档案 →
            </span>
          </div>
        </div>
      </button>
    </div>
  </div>
</template>

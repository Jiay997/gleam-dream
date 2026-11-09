<!--
  文件级注释：项目资产档案（路由 /assets/:projectId，AppShell 布局）。
  定位（与链路 STAGE 03「资产设计」严格分离）：
  - 本页 = 只读档案：只查阅已生成、已通过设计审核 / 已锁定（定稿）的角色 / 场景 / 道具；
  - 链路 /projects/:projectId/assets = 设计台：负责规划、生成、质检、审核与锁定；
  - 顶部定位条 + 「进入资产设计」按钮把两条路径显式分开。
  视觉语言沿用「极夜 · 极光」：档案卡 = 渐变封面 + 衬线资产名 + 档案编号，
  抽屉内展示真实版本图（media_url）与版本档案；无媒体时回退确定性渐变封面。
-->
<script setup lang="ts">
/**
 * ProjectAssetLibraryView 项目资产档案（只读）。
 * props：无。emits：无。
 * 依赖 project / assets API；点击档案卡打开右侧只读抽屉（版本图 + 元信息）。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  ASSET_TYPE_LABELS,
  ASSET_VERSION_TYPE_LABELS,
  getAsset,
  listAssets,
  type AssetDetail,
  type AssetItem,
  type AssetType,
  type AssetVersionItem,
} from '@/api/assets'
import { getProject } from '@/api/projects'
import type { Project } from '@/api/types'
import AppButton from '@/components/common/AppButton.vue'
import DrawerPanel from '@/components/common/DrawerPanel.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { coverTone } from '@/utils/coverTone'
import { formatShortDate } from '@/utils/datetime'

/** 路由实例（返回资产库 / 进入资产设计） */
const route = useRoute()
const router = useRouter()

/** 实体资产类型（档案不含音色库） */
type EntityType = Exclude<AssetType, 'voice'>

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/** 项目详情（档案刊头） */
const project = ref<Project | null>(null)
/** 项目实体资产（已排除音色） */
const assets = ref<AssetItem[]>([])
/** 页面加载中 */
const loading = ref(true)
/** 页面加载失败信息 */
const errorMsg = ref('')

/** 当前模块 Tab（空串 = 全部实体资产） */
const activeType = ref<'' | EntityType>('')
/** 档案口径：finalized 定稿 / approved 已通过 / locked 已锁定 / all 全部已生成 */
const statusScope = ref<'finalized' | 'approved' | 'locked' | 'all'>('finalized')
/** 关键词（名称 / 描述 / 标签 / 编号） */
const keyword = ref('')

/** 抽屉显示状态 */
const detailOpen = ref(false)
/** 当前抽屉资产 */
const activeAsset = ref<AssetItem | null>(null)
/** 抽屉详情 */
const detail = ref<AssetDetail | null>(null)
/** 抽屉加载中 */
const detailLoading = ref(false)
/** 抽屉加载失败信息 */
const detailError = ref('')

/** 模块 Tab 定义 */
const MODULE_TABS: Array<{ key: '' | EntityType; label: string }> = [
  { key: '', label: '全部资产' },
  { key: 'character', label: '角色' },
  { key: 'scene', label: '场景' },
  { key: 'prop', label: '道具' },
]

/** 档案口径 chips */
const SCOPE_OPTIONS: Array<{ key: 'finalized' | 'approved' | 'locked' | 'all'; label: string }> = [
  { key: 'finalized', label: '定稿资产' },
  { key: 'approved', label: '已通过' },
  { key: 'locked', label: '已锁定' },
  { key: 'all', label: '全部已生成' },
]

/** 类型展示元信息（徽标前缀 + 封水墨字） */
const TYPE_META: Record<EntityType, { prefix: string; glyph: string }> = {
  character: { prefix: 'CH', glyph: '角' },
  scene: { prefix: 'SC', glyph: '景' },
  prop: { prefix: 'PR', glyph: '道' },
}

/**
 * 判断资产是否已定稿（已通过设计审核或已锁定锚点）。
 * @param asset 资产对象
 * @returns 是否定稿
 */
function isFinalized(asset: AssetItem): boolean {
  return asset.status === 'approved' || asset.status === 'locked'
}

/** 是否具备已生成产物 */
function isGenerated(asset: AssetItem): boolean {
  return Boolean(asset.current_version_id) || isFinalized(asset)
}

/** 符合当前档案口径的资产（先按状态收敛，模块 Tab 计数基于此） */
const scopeAssets = computed<AssetItem[]>(() => {
  if (statusScope.value === 'all') return assets.value.filter(isGenerated)
  if (statusScope.value === 'approved') return assets.value.filter((asset) => asset.status === 'approved')
  if (statusScope.value === 'locked') return assets.value.filter((asset) => asset.status === 'locked')
  return assets.value.filter(isFinalized)
})

/**
 * 模块 Tab 计数。
 * @param key 模块键
 * @returns 当前口径下的资产数
 */
function moduleCount(key: '' | EntityType): number {
  if (key === '') return scopeAssets.value.length
  return scopeAssets.value.filter((asset) => asset.asset_type === key).length
}

/** 模块 + 关键词过滤后的档案卡数据 */
const filteredAssets = computed<AssetItem[]>(() => {
  const text = keyword.value.trim().toLowerCase()
  return scopeAssets.value.filter((asset) => {
    if (activeType.value !== '' && asset.asset_type !== activeType.value) return false
    if (!text) return true
    return [asset.name, asset.description ?? '', ...(asset.tags ?? []), String(asset.id)]
      .join(' ')
      .toLowerCase()
      .includes(text)
  })
})

/** 实体资产总数（不含音色） */
const entityTotal = computed(() => assets.value.length)
/** 已生成资产数 */
const generatedTotal = computed(() => assets.value.filter(isGenerated).length)
/** 定稿资产数 */
const finalizedTotal = computed(() => assets.value.filter(isFinalized).length)
/** 待确认资产数（尚未定稿） */
const pendingTotal = computed(() => assets.value.filter((asset) => asset.status === 'pending_review').length)

/** 资产设计路径（链路 STAGE 03） */
const designPath = computed(() => `/projects/${projectId.value}/assets`)

/**
 * 拉取项目信息与全部实体资产（一次性取 100 条，前端做模块 / 口径 / 关键词过滤）。
 * @returns 无
 * @throws 不抛出（错误写入 errorMsg 由 ErrorRetryBlock 承接）
 */
async function loadArchive(): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  try {
    const [projectData, assetPage] = await Promise.all([
      getProject(projectId.value),
      listAssets(projectId.value, { page: 1, page_size: 100 }),
    ])
    project.value = projectData
    assets.value = assetPage.items.filter((asset) => asset.asset_type !== 'voice')
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '项目资产档案加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 打开资产档案抽屉并拉取详情（版本 / QC / 音色）。
 * @param asset 被点击的资产
 * @returns 无
 * @throws 不抛出（错误写入 detailError，可在抽屉内重试）
 */
async function openDetail(asset: AssetItem): Promise<void> {
  activeAsset.value = asset
  detailOpen.value = true
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  try {
    detail.value = await getAsset(asset.id)
  } catch (error) {
    detailError.value = error instanceof Error ? error.message : '资产详情加载失败'
  } finally {
    detailLoading.value = false
  }
}

/**
 * 重试加载当前抽屉资产详情。
 * @returns 无
 */
async function retryDetail(): Promise<void> {
  if (activeAsset.value) await openDetail(activeAsset.value)
}

/**
 * 进入项目链路的资产设计（STAGE 03）。
 * @returns 无
 */
function goDesign(): void {
  void router.push(designPath.value)
}

/**
 * 返回全局资产库（项目管理库）。
 * @returns 无
 */
function goLibrary(): void {
  void router.push('/assets')
}

/**
 * 资产编号徽标（CH_001 / SC_002 / PR_003）。
 * @param asset 资产对象
 * @returns 编号文案
 */
function assetNo(asset: AssetItem): string {
  if (asset.asset_type === 'voice') return `VO_${String(asset.id).padStart(3, '0')}`
  return `${TYPE_META[asset.asset_type].prefix}_${String(asset.id).padStart(3, '0')}`
}

/**
 * 资产类型封水墨字（角 / 景 / 道）。
 * @param type 资产类型
 * @returns 单字墨印
 */
function glyphOf(type: AssetType): string {
  return type === 'voice' ? '声' : TYPE_META[type].glyph
}

/**
 * 资产类型中文标签。
 * @param type 资产类型
 * @returns 中文标签
 */
function typeLabel(type: AssetType): string {
  return ASSET_TYPE_LABELS[type] ?? type
}

/**
 * 版本类型中文标签（列表 / 抽屉共用）。
 * @param type 版本类型字符串
 * @returns 中文标签（未收录时原样返回）
 */
function versionTypeLabel(type: string): string {
  return ASSET_VERSION_TYPE_LABELS[type as keyof typeof ASSET_VERSION_TYPE_LABELS] ?? type
}

/**
 * 定稿印章文案（锁定优先）。
 * @param asset 资产对象
 * @returns 印章文案
 */
function sealLabel(asset: AssetItem): string {
  if (asset.status === 'locked') return '锚点已锁定'
  if (asset.status === 'approved') return '设计已定稿'
  return '待定稿'
}

/**
 * 档案卡底部时间文案（定稿时间优先，回退更新时间）。
 * @param asset 资产对象
 * @returns 日期文案
 */
function archiveDate(asset: AssetItem): string {
  const iso = asset.locked_at ?? asset.approved_at ?? asset.updated_at
  return formatShortDate(iso)
}

/**
 * 资产来源文案。
 * @param asset 资产对象
 * @returns 来源文案
 */
function sourceLabel(asset: AssetItem): string {
  if (asset.source === 'ai_generated') return 'AI 生成'
  if (asset.source === 'manual') return '人工建档'
  return '系统资产'
}

/**
 * 抽屉当前版本（is_current 优先，回退版本号最大者）。
 * @param versions 版本列表
 * @returns 当前版本或 null
 */
function currentVersionOf(versions: AssetVersionItem[]): AssetVersionItem | null {
  const current = versions.find((version) => version.is_current)
  if (current) return current
  return versions.reduce<AssetVersionItem | null>(
    (latest, version) => (!latest || version.version_no > latest.version_no ? version : latest),
    null,
  )
}

/** 抽屉当前版本 */
const drawerCurrentVersion = computed<AssetVersionItem | null>(() =>
  detail.value ? currentVersionOf(detail.value.versions) : null,
)

onMounted(() => {
  void loadArchive()
})
</script>

<template>
  <div class="mx-auto w-full max-w-content px-4 py-7 sm:px-6 lg:px-8">
    <button
      type="button"
      class="mb-4 inline-flex items-center gap-1.5 text-caption font-medium text-ink-mute transition-colors duration-200 hover:text-ink"
      @click="goLibrary"
    >
      <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
        <path d="M19.5 12h-15m0 0l6-6m-6 6l6 6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      返回资产库 · 项目管理库
    </button>

    <PageHeader :title="project?.title || '项目资产档案'">
      <template #actions>
        <AppButton variant="subtle" size="sm" @click="goLibrary">返回资产库</AppButton>
        <AppButton variant="primary" size="sm" @click="goDesign">进入资产设计</AppButton>
      </template>
    </PageHeader>

    <LoadingSkeleton v-if="loading" variant="table" :rows="5" />

    <ErrorRetryBlock v-else-if="errorMsg" message="项目资产档案加载失败" :description="errorMsg" @retry="loadArchive" />

    <template v-else>
      <!-- 统计条 -->
      <section
        class="mb-5 grid grid-cols-2 divide-x divide-y divide-[var(--gd-line-soft)] overflow-hidden rounded-[18px] border border-hairline bg-[var(--gd-surface-glass)] sm:grid-cols-4 sm:divide-y-0"
        aria-label="项目资产统计"
      >
        <div class="px-4 py-3.5 sm:px-5">
          <p class="gd-section-label">ASSETS</p>
          <p class="mt-1 font-num text-[22px] leading-none text-ink">{{ entityTotal }}</p>
          <p class="mt-1 text-[11px] text-ink-mute">件实体资产</p>
        </div>
        <div class="px-4 py-3.5 sm:px-5">
          <p class="gd-section-label">GENERATED</p>
          <p class="mt-1 font-num text-[22px] leading-none text-info">{{ generatedTotal }}</p>
          <p class="mt-1 text-[11px] text-ink-mute">件已生成</p>
        </div>
        <div class="px-4 py-3.5 sm:px-5">
          <p class="gd-section-label">FINALIZED</p>
          <p class="mt-1 font-num text-[22px] leading-none text-success">{{ finalizedTotal }}</p>
          <p class="mt-1 text-[11px] text-ink-mute">件已定稿</p>
        </div>
        <div class="px-4 py-3.5 sm:px-5">
          <p class="gd-section-label">PENDING</p>
          <p class="mt-1 font-num text-[22px] leading-none" :class="pendingTotal > 0 ? 'text-gd-pending' : 'text-ink-mute'">
            {{ pendingTotal }}
          </p>
          <p class="mt-1 text-[11px] text-ink-mute">件待确认</p>
        </div>
      </section>

      <!-- 模块 Tab + 档案口径 + 搜索 -->
      <div class="mb-5 space-y-3">
        <div class="flex flex-wrap items-center gap-2" role="tablist" aria-label="资产模块">
          <button
            v-for="tab in MODULE_TABS"
            :key="tab.key"
            type="button"
            role="tab"
            :aria-selected="activeType === tab.key"
            class="flex items-center gap-2 rounded-full border px-3.5 py-1.5 text-[12.5px] transition-[background-color,border-color,color] duration-200"
            :class="
              activeType === tab.key
                ? 'gd-chip-active border-transparent'
                : 'border-hairline bg-[rgba(255,255,255,0.025)] text-ink-mute hover:border-[var(--gd-line-brand)] hover:text-ink'
            "
            @click="activeType = tab.key"
          >
            {{ tab.label }}
            <span class="font-num text-[11px] opacity-70">{{ moduleCount(tab.key) }}</span>
          </button>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="档案口径">
            <span class="mr-1 text-[11px] text-ink-mute">档案口径</span>
            <button
              v-for="option in SCOPE_OPTIONS"
              :key="option.key"
              type="button"
              class="rounded-full border px-2.5 py-1 text-[11.5px] transition-[background-color,border-color,color] duration-200"
              :class="
                statusScope === option.key
                  ? 'border-[var(--gd-line-brand)] bg-[var(--gd-violet-wash)] text-ink'
                  : 'border-[var(--gd-line-soft)] text-ink-mute hover:border-[var(--gd-line-brand)] hover:text-ink'
              "
              @click="statusScope = option.key"
            >
              {{ option.label }}
            </button>
          </div>

          <label class="gd-field-group ml-auto flex h-8 w-full max-w-[260px] items-center gap-2 px-3">
            <svg class="h-3.5 w-3.5 shrink-0 text-ink-mute" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
              <path d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <input
              v-model="keyword"
              type="search"
              class="w-full bg-transparent text-[12px] text-ink outline-none placeholder:text-ink-mute"
              placeholder="搜索资产名 / 标签…"
              aria-label="搜索项目资产"
            />
          </label>
        </div>
      </div>

      <EmptyState
        v-if="filteredAssets.length === 0"
        icon-path="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909M2.25 18.75h16.5a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5H3.75A1.5 1.5 0 002.25 6v11.25a1.5 1.5 0 001.5 1.5z"
        :title="entityTotal === 0 ? '该项目还没有沉淀资产' : '当前档案口径下暂无资产'"
        :description="
          entityTotal === 0
            ? '资产需要在项目链路的 STAGE 03「资产设计」中规划并生成，定稿后会自动出现在这里。'
            : '试试切换「全部已生成」或更换模块 / 关键词。'
        "
        :action-text="entityTotal === 0 ? '前往资产设计' : '查看全部已生成'"
        @action="entityTotal === 0 ? goDesign() : (statusScope = 'all')"
      />

      <!-- 档案卡网格 -->
      <div v-else class="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <button
          v-for="asset in filteredAssets"
          :key="asset.id"
          type="button"
          class="group relative overflow-hidden rounded-[18px] border border-hairline bg-[var(--gd-surface-glass)] text-left shadow-card transition-[transform,border-color,box-shadow] duration-[320ms] hover:-translate-y-0.5 hover:border-[var(--gd-line-brand)] hover:shadow-cardHover"
          :aria-label="`查看资产档案：${asset.name}`"
          @click="openDetail(asset)"
        >
          <!-- 档案封面：确定性渐变 + 类型墨字 + 编号 / 状态 -->
          <div class="relative aspect-[4/3] overflow-hidden">
            <span class="absolute inset-0" :class="['gd-cv-cover', coverTone(asset.id)]" aria-hidden="true" />
            <span
              class="pointer-events-none absolute -bottom-8 -right-1 select-none font-serif text-[112px] leading-none text-[rgba(255,250,240,0.13)]"
              aria-hidden="true"
            >
              {{ glyphOf(asset.asset_type) }}
            </span>
            <span
              class="absolute left-3 top-3 rounded-[6px] bg-[rgba(4,7,15,0.42)] px-1.5 py-0.5 font-mono text-[10px] tracking-[0.12em] text-white/70"
            >
              {{ assetNo(asset) }}
            </span>
            <StatusTag class="absolute right-3 top-3" :status="asset.status" size="sm" variant="dark" />
            <span
              v-if="isFinalized(asset)"
              class="absolute left-3 top-9 rounded-[6px] border border-[rgba(62,232,184,0.42)] bg-[rgba(4,7,15,0.46)] px-1.5 py-0.5 font-mono text-[9px] tracking-[0.1em] text-success-bright"
            >
              {{ sealLabel(asset) }}
            </span>

            <div class="absolute inset-x-3 bottom-3">
              <p class="truncate font-serif text-[18px] font-semibold text-[#fffaf0]">{{ asset.name }}</p>
              <p class="mt-0.5 text-[11px] text-white/60">
                {{ typeLabel(asset.asset_type) }} · 当前版本 {{ asset.current_version_id ? `#${asset.current_version_id}` : '—' }}
              </p>
            </div>
          </div>

          <div class="p-4">
            <p class="line-clamp-2 min-h-[34px] text-caption leading-relaxed text-ink-soft">
              {{ asset.description || '暂无描述 · 档案仅保留生成记录' }}
            </p>

            <div v-if="asset.tags && asset.tags.length > 0" class="mt-2.5 flex flex-wrap gap-1">
              <span
                v-for="tag in asset.tags.slice(0, 4)"
                :key="tag"
                class="rounded-full border border-[var(--gd-line-soft)] px-1.5 py-0.5 text-[10px] text-ink-mute"
              >
                {{ tag }}
              </span>
            </div>

            <div class="mt-3 flex items-center justify-between gap-2 border-t border-[var(--gd-line-soft)] pt-2.5">
              <span class="truncate font-mono text-[10.5px] tracking-[0.06em] text-ink-mute">
                {{ isFinalized(asset) ? `定稿 ${archiveDate(asset)}` : `更新 ${archiveDate(asset)}` }}
              </span>
              <span class="shrink-0 text-[12px] font-medium text-ink-brand transition-colors duration-200 group-hover:text-ink">
                查阅档案 →
              </span>
            </div>
          </div>
        </button>
      </div>
    </template>

    <!-- 只读档案抽屉 -->
    <DrawerPanel v-model="detailOpen" :width="640" :title="detail?.name || activeAsset?.name || '资产档案'">
      <div v-if="detailLoading" class="space-y-3" aria-busy="true">
        <div class="gd-skeleton aspect-video w-full" />
        <div class="gd-skeleton h-4 w-2/3" />
        <div class="gd-skeleton h-4 w-1/2" />
      </div>

      <ErrorRetryBlock v-else-if="detailError" message="资产详情加载失败" :description="detailError" @retry="retryDetail" />

      <div v-else-if="detail" class="space-y-5">
        <!-- 当前版本主视图 -->
        <div class="gd-night-window relative aspect-video overflow-hidden">
          <img
            v-if="drawerCurrentVersion?.media_url"
            :src="drawerCurrentVersion.media_url"
            :alt="`${detail.name} 当前版本`"
            class="h-full w-full object-cover"
          />
          <div v-else class="flex h-full items-center justify-center" :class="['gd-cv-cover', coverTone(detail.id)]">
            <span class="font-serif text-[64px] leading-none text-[rgba(255,250,240,0.5)]">{{ glyphOf(detail.asset_type) }}</span>
          </div>
          <div class="absolute inset-x-0 bottom-0 bg-[linear-gradient(180deg,transparent,rgba(4,7,15,0.86))] px-4 pb-3 pt-10">
            <p class="font-serif text-[20px] font-semibold text-[#fffaf0]">{{ detail.name }}</p>
            <p class="mt-0.5 font-mono text-[10.5px] tracking-[0.1em] text-white/65">
              {{ assetNo(detail) }} · {{ typeLabel(detail.asset_type) }} · {{ sourceLabel(detail) }}
            </p>
          </div>
        </div>

        <!-- 档案元信息 -->
        <dl class="grid grid-cols-2 gap-3 rounded-[14px] border border-hairline bg-[rgba(255,255,255,0.02)] p-4">
          <div>
            <dt class="text-[10.5px] text-ink-mute">档案状态</dt>
            <dd class="mt-1"><StatusTag :status="detail.status" size="sm" /></dd>
          </div>
          <div>
            <dt class="text-[10.5px] text-ink-mute">当前版本</dt>
            <dd class="mt-1 text-caption text-ink">
              {{ drawerCurrentVersion ? `v${drawerCurrentVersion.version_no} · ${versionTypeLabel(drawerCurrentVersion.version_type)}` : '—' }}
            </dd>
          </div>
          <div>
            <dt class="text-[10.5px] text-ink-mute">定稿时间</dt>
            <dd class="mt-1 font-num text-caption text-ink">
              {{ formatShortDate(detail.locked_at ?? detail.approved_at) }}
            </dd>
          </div>
          <div>
            <dt class="text-[10.5px] text-ink-mute">版本总数</dt>
            <dd class="mt-1 font-num text-caption text-ink">{{ detail.versions.length }} 个</dd>
          </div>
        </dl>

        <div v-if="detail.description">
          <p class="gd-section-label">DESCRIPTION</p>
          <p class="mt-1.5 text-sm leading-[1.75] text-ink-soft">{{ detail.description }}</p>
        </div>

        <div v-if="detail.tags && detail.tags.length > 0">
          <p class="gd-section-label">TAGS</p>
          <div class="mt-1.5 flex flex-wrap gap-1.5">
            <span
              v-for="tag in detail.tags"
              :key="tag"
              class="rounded-full border border-[var(--gd-line-soft)] bg-[rgba(255,255,255,0.02)] px-2 py-0.5 text-[11px] text-ink-soft"
            >
              {{ tag }}
            </span>
          </div>
        </div>

        <!-- 版本档案 -->
        <div>
          <div class="mb-2 flex items-center justify-between">
            <p class="gd-section-label">VERSIONS · 版本档案</p>
            <span class="font-num text-[11px] text-ink-mute">{{ detail.versions.length }}</span>
          </div>
          <div v-if="detail.versions.length > 0" class="grid grid-cols-2 gap-3">
            <figure v-for="version in detail.versions" :key="version.id" class="min-w-0">
              <div class="relative aspect-video overflow-hidden rounded-[12px] border border-hairline bg-surface-sunken">
                <img
                  v-if="version.media_url"
                  :src="version.media_url"
                  :alt="`版本 v${version.version_no}`"
                  class="h-full w-full object-cover"
                  loading="lazy"
                />
                <div v-else class="flex h-full items-center justify-center" :class="['gd-cv-cover', coverTone(version.id)]">
                  <span class="font-serif text-[30px] text-[rgba(255,250,240,0.42)]">{{ glyphOf(detail.asset_type) }}</span>
                </div>
                <span
                  v-if="version.is_current"
                  class="absolute left-2 top-2 rounded-[5px] bg-[rgba(4,7,15,0.55)] px-1.5 py-0.5 font-mono text-[9px] tracking-[0.1em] text-success-bright"
                >
                  当前
                </span>
              </div>
              <figcaption class="mt-1.5 truncate text-[11px] text-ink-soft">
                v{{ version.version_no }} · {{ versionTypeLabel(version.version_type) }}
              </figcaption>
            </figure>
          </div>
          <p v-else class="rounded-[12px] border border-dashed border-hairline px-3 py-6 text-center text-caption text-ink-mute">
            暂无版本产物
          </p>
        </div>
      </div>

      <template #footer>
        <div class="flex items-center justify-end gap-2">
          <AppButton variant="subtle" size="sm" @click="detailOpen = false">关闭</AppButton>
          <AppButton variant="primary" size="sm" @click="goDesign">在资产设计中打开</AppButton>
        </div>
      </template>
    </DrawerPanel>
  </div>
</template>

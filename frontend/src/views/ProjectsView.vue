<!--
  文件级注释：P03 · 创作工作台（路由 /projects，AppShell 布局）· v8 · 按 stitch_prd/p1 对齐功能布局。
  Redesign-Preserve：布局结构 1:1 对齐 原型 code.html（项目开发文档/stitch_prd/p1），
  主题风格沿用现有「极夜 · 极光」令牌（gd-* 类 / surface-* / primary-* / hairline / shadow-gd / font-serif 等），
  不引入新色值 / 新字体 / 新依赖。区块顺序与栅格：
  - 刊头行：竖紫条 + 衬线大标题「浮光造梦 · 创作空间」+ STUDIO V3.2 徽标 + 价值主张副文案；
    右侧同行：快捷状态页签（所有项目 / 进行中 / 已交付，带本页计数）+「从模板库导入」
    +「新建短剧项目」主按钮 + 网格 / 列表切换；
  - 次级筛选行：搜索 / 14 态 / 题材 / 排序 / 范围（既有细粒度筛选全部保留）；
  - 主区 12 栏 Bento（gap-5）：焦点项目大卡 xl:col-span-8（左 5/12 夜窗竖版封面 + 右 7/12 信息区：
    标题 + ID 徽标 + ⋮ 菜单 + 简介 + 存盘版本 meta + HITL 四关口 H1-H4 检查卡组 + 积分预算 gauge
    + 核心质检一致性 + 操作行「项目快照历史 / 项目设置 / 进入智能分镜工作台」）；
    QUICK GENERATOR 抽屉卡 xl:col-span-4（徽标 + 可收起 + ⊕ 大按钮 + 灵感输入 + 闪电提交 + 热门题材直达矩阵）；
  - 下半区两卡并列（md:col-span-6 / xl:col-span-4）：
    左「近 7 日算力与产出流水」夜窗卡（LIVE TELEMETRY + 三大数 + 7 日柱状图，数据聚合自
    GET /billing/transactions settle 流水与积分账户）；右「渲染队列监控」卡（队列 / 并发 / 今日已交付
    三格 + 当前任务卡 + 管理入口，数据取自焦点项目 GET /projects/{id}/render-tasks）；
  - 底部：近期剧作矩阵（默认最近 3 卡 +「浏览所有历史项目 (total)」展开链接），
    展开后为完整卡片墙 / 列表表格 + 虚线新建卡 + 分页条（既有分页、重命名、归档全保留）。
  数据走 GET /projects（scope/status/genre/sort 后端过滤排序，关键词与快捷页签前端过滤当前页）。
  三态完整：LoadingSkeleton 骨架（按新栅格组合）/ ErrorRetryBlock 重试 / EmptyState 引导。
  卡片菜单：重命名（PATCH，乐观锁）/ 归档（POST /projects/{id}/archive，二次确认）。
-->
<script setup lang="ts">
/**
 * ProjectsView 创作工作台（按 stitch_prd/p1 对齐布局）。
 * props：无。emits：无。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { getCreditAccount, listTransactions } from '@/api/billing'
import type { TransactionItem } from '@/api/billing'
import { archiveProject, listProjects, resumeProject, unarchiveProject, updateProject } from '@/api/projects'
import { listRenderTasks, RENDER_TASK_TYPE_LABELS } from '@/api/render'
import type { RenderTaskListItem } from '@/api/render'
import type { CreditAccount, Project } from '@/api/types'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import CreditAmountText from '@/components/common/CreditAmountText.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import Modal from '@/components/common/Modal.vue'
import SegmentedControl from '@/components/common/SegmentedControl.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import {
  GENRE_OPTIONS,
  HITL_GATES,
  PROJECT_SORT_OPTIONS,
  PROJECT_STATUS_KEYS,
  STYLE_OPTIONS,
  genreLabelOf,
  stageIndexOf,
} from '@/constants/options'
import { projectStatusLabel, toneOf } from '@/constants/statusTone'
import type { StatusToneKey } from '@/constants/statusTone'
import { coverTone } from '@/utils/coverTone'
import { formatRelativeTime } from '@/utils/datetime'

/** 路由实例（卡片跳转） */
const router = useRouter()
/** 当前路由（接收顶栏全局搜索带来的 ?q= 关键词） */
const route = useRoute()
/** Toast */
const toast = useToast()

/** 每页条数（近期矩阵展开后的卡片墙分页） */
const PAGE_SIZE = 12

/* ---------- 列表查询状态 ---------- */
/** 项目列表（当前页） */
const projects = ref<Project[]>([])
/** 总条数（后端分页） */
const total = ref(0)
/** 加载中标记 */
const loading = ref(true)
/** 加载失败信息（空串表示正常） */
const errorMsg = ref('')

/* ---------- 筛选 / 展示状态 ---------- */
/** 关键词（前端过滤当前页标题；初始值取自顶栏全局搜索 `?q=`） */
const keyword = ref(String(route.query.q ?? ''))
/** 范围：created 我创建 / joined 我参与 */
const scope = ref<'created' | 'joined'>('created')
/** 状态筛选（空串=全部） */
const statusFilter = ref('')
/** 题材筛选（空串=全部） */
const genreFilter = ref('')
/** 排序字段 */
const sort = ref('-updated_at')
/** 视图形态：grid 卡片墙 / list 列表 */
const viewMode = ref<'grid' | 'list'>('grid')
/** 当前页码（1 起） */
const page = ref(1)

/** 快捷状态页签（对应原型刊头「所有项目 / 进行中 / 已交付」分段筛选，前端过滤当前页） */
type QuickTab = 'all' | 'active' | 'done'
/** 当前快捷页签 */
const quickTab = ref<QuickTab>('all')

/** 总页数 */
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

/** 页码序列（≤7 全显，否则两侧窗口 + 省略号） */
const pageItems = computed<(number | '…')[]>(() => {
  const last = totalPages.value
  const cur = page.value
  if (last <= 7) return Array.from({ length: last }, (_, i) => i + 1)
  const items: (number | '…')[] = [1]
  const start = Math.max(2, cur - 1)
  const end = Math.min(last - 1, cur + 1)
  if (start > 2) items.push('…')
  for (let p = start; p <= end; p += 1) items.push(p)
  if (end < last - 1) items.push('…')
  items.push(last)
  return items
})

/** 当前页内「进行中」项目数（running / pending 两 tone 视为在途） */
const activeCount = computed(
  () => projects.value.filter((p) => toneOf(p.status) === 'running' || toneOf(p.status) === 'pending').length,
)

/** 当前页内「已交付」项目数（completed / archived 视为交付终态） */
const doneCount = computed(
  () => projects.value.filter((p) => toneOf(p.status) === 'locked' || p.status === 'archived').length,
)

/** 快捷状态页签选项（带实时计数，SegmentedControl 消费） */
const QUICK_TAB_OPTIONS = computed(() => [
  // P1-2 修复：「所有项目」改用全库 total 口径（原为当前页条数，与
  // 底部「浏览所有历史项目 (total)」计数矛盾、易被误读为丢项目）
  // 测试报告问题 4：首屏数据未返回前用「—」占位，不再闪现 0。
  { value: 'all' as const, label: `所有项目 (${loading.value ? '—' : total.value})` },
  { value: 'active' as const, label: `进行中 (${loading.value ? '—' : activeCount.value})` },
  { value: 'done' as const, label: `已交付 (${loading.value ? '—' : doneCount.value})` },
])

/** 关键词 + 快捷页签过滤后的当前页项目 */
const visibleProjects = computed(() => {
  let list = projects.value
  const kw = keyword.value.trim().toLowerCase()
  if (kw) list = list.filter((p) => p.title.toLowerCase().includes(kw))
  if (quickTab.value === 'active') {
    list = list.filter((p) => toneOf(p.status) === 'running' || toneOf(p.status) === 'pending')
  } else if (quickTab.value === 'done') {
    list = list.filter((p) => toneOf(p.status) === 'locked' || p.status === 'archived')
  }
  return list
})

/** 当前筛选下是否全空（区分「无项目」与「无匹配结果」） */
const isFiltered = computed(
  () => keyword.value.trim() !== '' || statusFilter.value !== '' || genreFilter.value !== '' || quickTab.value !== 'all',
)

/** 项目范围分段选项（统一走 SegmentedControl 选项格式） */
const SCOPE_OPTIONS: { value: 'created' | 'joined'; label: string }[] = [
  { value: 'created', label: '我创建' },
  { value: 'joined', label: '我参与' },
]

/* ---------- 焦点项目（hero 大卡，对应原型 8 栏焦点卡） ---------- */
/** 焦点项目（列表首项 = 最近更新） */
const focusProject = computed<Project | null>(() => projects.value[0] ?? null)

/**
 * 封面底色：项目墙的封面色相统一走 utils/coverTone（确定性哈希 + 装饰池不含薄荷）。
 * 保留一层同名封装让模板里 `coverClass(project.id)` 的写法不变。
 * @param id 项目 ID
 * @returns cv 渐变类名
 */
function coverClass(id: string | number): string {
  return coverTone(id)
}

/**
 * 题材中文标签。
 * @param key 题材 key
 * @returns 中文标签（未设回退）
 */
function genreLabel(key?: string | null): string {
  // 修复（测试报告 #4）：AI 写入的英文枚举（urban_romance 等）经共享兜底映射转中文
  return genreLabelOf(key) ?? '未设题材'
}

/**
 * 画风中文标签（未设返回空串，模板隐藏该 pill）。
 * @param key 画风预设键
 * @returns 中文标签
 */
function styleLabel(key?: string): string {
  return STYLE_OPTIONS.find((s) => s.key === key)?.label ?? ''
}

/* ---------- HITL 四关口（H1-H4 检查卡组，事实源 stageIndexOf） ---------- */
/** 关口序号（H1~H4 角标） */
const GATE_NO: Record<string, number> = { script: 1, asset: 2, storyboard: 3, final_cut: 4 }

/** 关口短题（对应原型四卡标题：关口一 剧本 / 关口二 资产 / 关口三 分镜 / 关口四 成片） */
const GATE_TITLE: Record<string, string> = { script: '剧本', asset: '资产', storyboard: '分镜', final_cut: '成片' }

/** 关口卡视觉状态 */
type GateState = 'done' | 'active' | 'upcoming' | 'failed'

/** 关口卡数据形状 */
interface GateCard {
  /** 关口键 */
  key: string
  /** 关口序号 Hn */
  no: number
  /** 短题 */
  title: string
  /** 视觉状态 */
  state: GateState
  /** 状态短语 */
  hint: string
  /** 进行中是否旋转图标（running tone 时转圈） */
  spin: boolean
}

/**
 * 由项目状态推导 HITL 四关口卡状态（stageIndexOf 为唯一阶段事实源，不虚构进度）。
 * @param project 项目
 * @returns 四关口卡数组
 */
function gateCardsOf(project: Project): GateCard[] {
  const { index, failed } = stageIndexOf(project.status, project.config)
  const pending = toneOf(project.status) === 'pending'
  return HITL_GATES.map((gate) => {
    const no = GATE_NO[gate.key] ?? 0
    let state: GateState
    if (index >= 7) state = 'done'
    else if (failed && gate.stageIndex === index) state = 'failed'
    else if (gate.stageIndex < index) state = 'done'
    else if (gate.stageIndex === index) state = 'active'
    else state = 'upcoming'
    const hint =
      state === 'done' ? '已通过' : state === 'failed' ? '已失败' : state === 'active' ? (pending ? '待核准' : '进行中') : '未就绪'
    return { key: gate.key, no, title: GATE_TITLE[gate.key] ?? gate.key, state, hint, spin: state === 'active' && !pending }
  })
}

/** 焦点项目四关口卡（无焦点项目时空数组） */
const focusGates = computed<GateCard[]>(() => (focusProject.value ? gateCardsOf(focusProject.value) : []))

/** 关口面板右上角提示（真实项目状态标签） */
const focusGateHint = computed(() => {
  const project = focusProject.value
  if (!project) return ''
  const { index, failed } = stageIndexOf(project.status, project.config)
  if (index >= 7) return `全部关口已通过 · ${projectStatusLabel(project.status)}`
  if (failed) return `${projectStatusLabel(project.status)} · 需导演处理`
  const gate = HITL_GATES.find((g) => g.stageIndex === index)
  return gate ? `关口${GATE_NO[gate.key] ?? '—'} · ${projectStatusLabel(project.status)}` : projectStatusLabel(project.status)
})

/** 关口卡样式（按状态映射现有令牌类：mint 通过 / 品牌实底进行中 / rose 失败 / 中性未就绪） */
const GATE_CARD_CLASS: Record<GateState, string> = {
  done: 'gd-fill-mint',
  active: 'gd-fill-brand',
  failed: 'gd-fill-rose',
  upcoming: 'border border-hairline bg-surface-raised text-ink-mute opacity-60',
}

/**
 * 关口卡状态图标（24x24 stroke path）。
 * @param state 关口状态
 * @returns 图标 path
 */
function gateIcon(state: GateState): string {
  if (state === 'done') return ICON_CHECK
  if (state === 'failed') return ICON_CLOSE
  if (state === 'active') return ICON_REFRESH
  return ICON_CIRCLE
}

/* ---------- hero 行动区（快照历史 / 项目设置 / 主 CTA） ---------- */
/** 等待导演确认状态 → 对应确认页路由（既有确认页，含资产关口） */
const REVIEW_ROUTES: Record<string, (id: string | number) => string> = {
  script_review: (id) => `/projects/${id}/script/review`,
  asset_review: (id) => `/projects/${id}/assets`,
  storyboard_review: (id) => `/projects/${id}/storyboard`,
  final_review: (id) => `/projects/${id}/post/review`,
}

/**
 * hero 主 CTA 文案：等待确认时引导去关口，否则进入智能分镜工作台。
 * @param project 项目
 * @returns 文案
 */
function heroCtaLabel(project: Project): string {
  return REVIEW_ROUTES[project.status] ? '去确认关口' : '进入智能分镜工作台'
}

/**
 * hero 主 CTA 目标路由。
 * @param project 项目
 * @returns 路由串
 */
function heroCtaTarget(project: Project): string {
  const review = REVIEW_ROUTES[project.status]
  return review ? review(project.id) : `/projects/${project.id}/storyboard`
}

/** hero 主 CTA 跳转（焦点项目守卫） */
function goHeroCta(): void {
  const project = focusProject.value
  if (project) void router.push(heroCtaTarget(project))
}

/** 项目快照历史 → 审批台（审批台面板承载快照区） */
function goSnapshots(): void {
  const project = focusProject.value
  if (project) void router.push(`/projects/${project.id}/overview`)
}

/** 项目设置 → 复用既有重命名弹窗（当前唯一的项目基础信息编辑能力） */
function openFocusRename(): void {
  const project = focusProject.value
  if (project) openRename(project)
}

/* ---------- 焦点项目预算 / 质检读数 ---------- */
/**
 * 预算消耗百分比（budget=0 不限时返回 0）。
 * @param project 项目
 * @returns 0~100 整数
 */
function budgetPercent(project: Project): number {
  const budget = project.budget_credits ?? 0
  if (budget <= 0) return 0
  return Math.min(100, Math.round(((project.spent_credits ?? 0) / budget) * 100))
}

/** 焦点项目预算消耗百分比 */
const focusBudgetPercent = computed(() => (focusProject.value ? budgetPercent(focusProject.value) : 0))

/** 预算读数文案（已耗 / 上限（百分比），未设上限标注「不限」） */
const focusBudgetText = computed(() => {
  const project = focusProject.value
  if (!project) return '—'
  const budget = project.budget_credits ?? 0
  return budget > 0 ? `${project.spent_credits ?? 0} / ${budget} (${focusBudgetPercent.value}%)` : `${project.spent_credits ?? 0} / 不限`
})

/** 预算卡脚注（剩余额度 / 未设上限） */
const focusBudgetFoot = computed(() => {
  const project = focusProject.value
  if (!project) return ''
  const budget = project.budget_credits ?? 0
  return budget > 0 ? `剩余 ${Math.max(0, budget - (project.spent_credits ?? 0))} 积分` : '未设预算上限'
})

/** 五态 tone → 状态点颜色（质检卡用，取令牌基色） */
const TONE_DOT_COLOR: Record<StatusToneKey, string> = {
  running: 'var(--gd-running)',
  pending: 'var(--gd-pending)',
  locked: 'var(--gd-locked)',
  failed: 'var(--gd-failed)',
  blocked: 'var(--gd-blocked)',
  neutral: 'var(--gd-neutral-400)',
}

/** 焦点项目质检卡提示（按五态 tone 归纳，不虚构一致性分数） */
const focusQualityHint = computed(() => {
  const project = focusProject.value
  if (!project) return ''
  const label = projectStatusLabel(project.status)
  switch (toneOf(project.status)) {
    case 'pending':
      return `当前待审：${label}，需要导演确认`
    case 'running':
      return `${label}，流水线推进中`
    case 'locked':
      return '成片已交付，质检链路通过'
    case 'failed':
      return `${label}，请检查失败原因后重试`
    default:
      return label
  }
})

/** 焦点项目质检卡状态点颜色 */
const focusQualityDot = computed(() =>
  focusProject.value ? TONE_DOT_COLOR[toneOf(focusProject.value.status)] : TONE_DOT_COLOR.neutral,
)

/* ---------- QUICK GENERATOR 抽屉卡 ---------- */
/** 灵感输入框内容（提交后作为 query 种子带入创建向导） */
const generatorSeed = ref('')
/** QUICK GENERATOR 卡收起状态（收起后仅保留徽标行） */
const generatorCollapsed = ref(false)
/** 热门题材直达矩阵（取题材选项前 4，对应原型 4 枚 tag） */
const GENERATOR_GENRES = GENRE_OPTIONS.slice(0, 4)

/**
 * 跳转创建项目向导（可携带灵感 / 题材 / 来源种子，向导侧可选择性消费）。
 * @param seed 创建种子（可空 = 直接新建）
 * @returns 无
 */
function goCreate(seed?: { inspiration?: string; genre?: string; source?: string }): void {
  const query: Record<string, string> = {}
  if (seed?.inspiration) query.inspiration = seed.inspiration
  if (seed?.genre) query.genre = seed.genre
  if (seed?.source) query.source = seed.source
  void router.push(Object.keys(query).length > 0 ? { path: '/projects/new', query } : '/projects/new')
}

/** 提交灵感输入（空值退化为直接新建） */
function submitInspiration(): void {
  const inspiration = generatorSeed.value.trim()
  goCreate(inspiration ? { inspiration } : undefined)
}

/** 题材 chip 直达：带题材种子新建项目 */
function startFromGenre(genre: string): void {
  goCreate({ genre })
}

/* ---------- 近 7 日算力与产出流水（listTransactions settle 聚合 + 积分账户） ---------- */
/** 统计窗口天数 */
const TELEMETRY_DAYS = 7
/** 近 7 日结算流水（page_size 200 近似覆盖一周，best-effort） */
const weekTransactions = ref<TransactionItem[]>([])
/** 当前积分账户 */
const creditAccount = ref<CreditAccount | null>(null)
/** 遥测加载失败（两源都失败才置位，卡片展示占位读数「—」） */
const telemetryError = ref(false)

/** 单日聚合形状 */
interface TelemetryDay {
  /** 本地日期键 YYYY-MM-DD */
  key: string
  /** 柱下标签（MM/DD / 今天） */
  label: string
  /** 当日消耗积分（settle 绝对值合计） */
  credits: number
  /** 当日结算笔数 */
  count: number
  /** 是否今天（高亮柱） */
  isToday: boolean
}

/**
 * 本地日期键（YYYY-MM-DD，避免 UTC 偏移导致跨日错桶）。
 * @param date 日期
 * @returns 键串
 */
function localDateKey(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${month}-${day}`
}

/**
 * 拉取遥测数据（流水 + 账户，双源 allSettled，失败降级为占位读数）。
 * @returns 无
 */
async function fetchTelemetry(): Promise<void> {
  telemetryError.value = false
  const [tx, account] = await Promise.allSettled([
    listTransactions({ page: 1, page_size: 100, sort: '-created_at', transaction_type: 'settle' }),
    getCreditAccount(),
  ])
  weekTransactions.value = tx.status === 'fulfilled' ? tx.value.items : []
  creditAccount.value = account.status === 'fulfilled' ? account.value : null
  telemetryError.value = tx.status === 'rejected' && account.status === 'rejected'
}

/** 近 7 日流水聚合（柱状图 + 大数读数共用） */
const telemetryWeek = computed<TelemetryDay[]>(() => {
  const now = new Date()
  const days: TelemetryDay[] = []
  for (let offset = TELEMETRY_DAYS - 1; offset >= 0; offset -= 1) {
    const date = new Date(now.getFullYear(), now.getMonth(), now.getDate() - offset)
    days.push({
      key: localDateKey(date),
      label: offset === 0 ? '今天' : `${String(date.getMonth() + 1).padStart(2, '0')}/${String(date.getDate()).padStart(2, '0')}`,
      credits: 0,
      count: 0,
      isToday: offset === 0,
    })
  }
  const index = new Map(days.map((day, i) => [day.key, i]))
  for (const tx of weekTransactions.value) {
    if (!tx.created_at) continue
    const i = index.get(localDateKey(new Date(tx.created_at)))
    if (i === undefined) continue
    days[i].credits += Math.abs(tx.amount_credits)
    days[i].count += 1
  }
  return days
})

/** 柱高归一化峰值（至少 1 防除零） */
const telemetryPeak = computed(() => Math.max(1, ...telemetryWeek.value.map((day) => day.credits)))

/** 7 日消耗合计（积分） */
const weekConsumed = computed(() => telemetryWeek.value.reduce((sum, day) => sum + day.credits, 0))

/** 7 日结算笔数 */
const weekSettleCount = computed(() => telemetryWeek.value.reduce((sum, day) => sum + day.count, 0))

/** 可用积分读数（账户缺失时为 null，展示「—」） */
const availableCredits = computed<number | null>(() => {
  if (!creditAccount.value) return null
  return creditAccount.value.available_credits ?? creditAccount.value.balance_credits
})

/**
 * 柱高百分比（0 值给 3% 基线桩，非零至少 8% 可见）。
 * @param day 单日聚合
 * @returns 高度百分比
 */
function barHeight(day: TelemetryDay): number {
  if (day.credits <= 0) return 3
  return Math.max(8, Math.round((day.credits / telemetryPeak.value) * 100))
}

/* ---------- 渲染队列监控（焦点项目最近任务聚合） ---------- */
/** 队列活跃态（排队 / 生成 / 重试） */
const QUEUE_ACTIVE_STATUSES = ['pending', 'queued', 'running', 'retrying']

/** 焦点项目最近渲染任务（近 50 条，best-effort） */
const renderTasks = ref<RenderTaskListItem[]>([])
/** 队列加载失败标记（卡内降级为空态提示） */
const queueError = ref(false)

/**
 * 拉取焦点项目渲染任务列表（静默失败：监控卡是辅助件，不打断主列表）。
 * @returns 无
 */
async function fetchQueue(): Promise<void> {
  const project = focusProject.value
  if (!project) {
    renderTasks.value = []
    return
  }
  queueError.value = false
  try {
    const result = await listRenderTasks(String(project.id), { page: 1, page_size: 50, sort: '-created_at' })
    renderTasks.value = result.items
  } catch {
    queueError.value = true
  }
}

// 焦点项目变化（首次加载 / 切页 / 重命名刷新）时重取队列
watch(
  () => focusProject.value?.id,
  () => {
    void fetchQueue()
  },
  { immediate: true },
)

/** 排队任务数（pending / queued，对应「Celery 队列」格） */
const queuePendingCount = computed(() => renderTasks.value.filter((t) => t.status === 'pending' || t.status === 'queued').length)

/** 生成中任务数（running / retrying，对应「渲染并发」格） */
const queueRunningCount = computed(() => renderTasks.value.filter((t) => t.status === 'running' || t.status === 'retrying').length)

/** 失败 / 需人工任务数（脚注提示） */
const queueFailedCount = computed(
  () => renderTasks.value.filter((t) => t.status === 'failed' || t.status === 'manual_required').length,
)

/** 今日已交付（succeeded 且创建于今天，列表接口无完成时间，以创建日近似） */
const queueDeliveredToday = computed(() => {
  const today = localDateKey(new Date())
  return renderTasks.value.filter((t) => t.status === 'succeeded' && t.created_at && localDateKey(new Date(t.created_at)) === today)
    .length
})

/** 当前进行中的任务（running / retrying 优先，其次排队头部） */
const activeQueueTask = computed<RenderTaskListItem | null>(() => {
  const running = renderTasks.value.find((t) => t.status === 'running' || t.status === 'retrying')
  if (running) return running
  return renderTasks.value.find((t) => QUEUE_ACTIVE_STATUSES.includes(t.status)) ?? null
})

/**
 * 任务类型中文标签（未知类型原样展示）。
 * @param task 渲染任务
 * @returns 类型标签
 */
function taskTypeLabel(task: RenderTaskListItem): string {
  return RENDER_TASK_TYPE_LABELS[task.task_type] ?? task.task_type
}

/** 管理完整队列 → 焦点项目渲染任务中心 */
function goRenderCenter(): void {
  const project = focusProject.value
  if (project) void router.push(`/projects/${project.id}/render-tasks`)
}

/** 查看流水报表 → 计费中心 */
function goBilling(): void {
  void router.push('/billing')
}

/* ---------- 近期剧作矩阵（底部 3 卡 + 历史展开） ---------- */
/** 历史项目区展开（默认仅显示最近 3 卡，对应原型底部矩阵） */
const libraryExpanded = ref(false)

/** 矩阵渲染集合：默认前 3 张；展开或列表视图时为当前页全量 */
const matrixProjects = computed(() =>
  libraryExpanded.value || viewMode.value === 'list' ? visibleProjects.value : visibleProjects.value.slice(0, 3),
)

/* ---------- 空态矢量图标（24x24 线性，统一全站图标语言） ---------- */
/** 场记板：无项目引导 */
const ICON_CLAPPER =
  'M3.75 6.75h16.5v12H3.75zM3.75 6.75L5.5 3.75h13l1.75 3M8.25 6.75l1.5-3m3 3l1.5-3M6 12.75l1.5 1.5 3-3'
/** 放大镜：无匹配结果 */
const ICON_SEARCH =
  'M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z'
/** 时钟：存盘时间 / 更新时间 */
const ICON_CLOCK = 'M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z'
/** 闪电：灵感快捷提交 */
const ICON_BOLT = 'M13 10V3L4 14h7v7l9-11h-7z'
/** 星火：从模板库导入 */
const ICON_SPARKLES =
  'M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456z'
/** 右箭头：主 CTA / 卡片行尾 */
const ICON_ARROW_RIGHT = 'M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3'
/** 对勾：关口已通过 */
const ICON_CHECK = 'M4.5 12.75l6 6 9-13.5'
/** 环形箭头：关口进行中 */
const ICON_REFRESH =
  'M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99'
/** 空心圆：关口未就绪 */
const ICON_CIRCLE = 'M21 12a9 9 0 11-18 0 9 9 0 0118 0z'
/** 叉：关口失败 */
const ICON_CLOSE = 'M6 18L18 6M6 6l12 12'
/** 盾牌对勾：自动容错重试 */
const ICON_SHIELD =
  'M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z'

/* ---------- 卡片 ··· 菜单 / 重命名 / 归档 ---------- */
/** 当前展开菜单的项目 ID（空串表示全部收起） */
const openMenuId = ref<string>('')
/** 重命名目标项目（null 表示弹窗关闭） */
const renameTarget = ref<Project | null>(null)
/** 重命名输入框内容 */
const renameTitle = ref('')
/** 重命名提交中 */
const renameSaving = ref(false)
/** 重命名弹窗显示状态（computed 双向代理，供 Modal v-model） */
const renameDialogOpen = computed({
  get: () => renameTarget.value !== null,
  set: (value: boolean) => {
    if (!value) renameTarget.value = null
  },
})
/** 归档目标项目（null 表示弹窗关闭） */
const archiveTarget = ref<Project | null>(null)
/** 归档弹窗显示状态（computed 双向代理，供 ConfirmDialog v-model） */
const archiveDialogOpen = computed({
  get: () => archiveTarget.value !== null,
  set: (value: boolean) => {
    if (!value) archiveTarget.value = null
  },
})
/** 归档提交中 */
const archiveSaving = ref(false)

/**
 * 拉取项目列表（当前筛选 / 页码）。
 * @returns 无
 */
async function fetchProjects(): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  try {
    const result = await listProjects({
      scope: scope.value,
      status: statusFilter.value || undefined,
      genre: genreFilter.value || undefined,
      sort: sort.value,
      page: page.value,
      page_size: PAGE_SIZE,
    })
    projects.value = result.items
    total.value = result.total
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 筛选条件变化：回到第 1 页并重新拉取。
 * @returns 无
 */
watch([scope, statusFilter, genreFilter, sort], () => {
  page.value = 1
  void fetchProjects()
})

/**
 * 翻页并滚回列表顶部。
 * @param target 目标页码
 * @returns 无
 */
function goPage(target: number): void {
  if (target < 1 || target > totalPages.value || target === page.value) return
  page.value = target
  void fetchProjects()
}

/**
 * 跳转审批台（继续创作）。
 * @param project 目标项目
 * @returns 无
 */
function continueCreate(project: Project): void {
  // 修复（测试报告 #2）：已归档项目为只读终态，不再放行「继续创作」，
  // 引导用户先通过卡片菜单「恢复归档」（仅 Owner）。
  if (project.status === 'archived') {
    toast.warning('该项目已归档，请先通过卡片菜单「恢复归档」后再继续创作')
    return
  }
  void router.push(`/projects/${project.id}/overview`)
}

/**
 * 清空全部筛选条件（保留当前页数据不重新请求，由用户继续浏览）。
 * @returns 无
 */
function clearFilters(): void {
  keyword.value = ''
  statusFilter.value = ''
  genreFilter.value = ''
  quickTab.value = 'all'
}

// 顶栏全局搜索进入本页时同步关键词（含浏览器前进/后退带来的 query 变化）
watch(
  () => route.query.q,
  (value) => {
    keyword.value = String(value ?? '')
  },
)

/**
 * 点击空白处收起 ··· 菜单。
 * @param event 文档点击事件
 * @returns 无
 */
function onDocClick(event: MouseEvent): void {
  if (!(event.target as HTMLElement).closest('[data-project-menu]')) {
    openMenuId.value = ''
  }
}

/**
 * 打开重命名弹窗。
 * @param project 目标项目
 * @returns 无
 */
function openRename(project: Project): void {
  openMenuId.value = ''
  renameTarget.value = project
  renameTitle.value = project.title
}

/**
 * 提交重命名（PATCH，携带乐观锁版本）。
 * @returns 无
 */
async function confirmRename(): Promise<void> {
  const target = renameTarget.value
  if (!target) return
  const title = renameTitle.value.trim()
  if (!title) {
    toast.warning('项目名不能为空')
    return
  }
  if (title.length > 30) {
    toast.warning('项目名不能超过 30 字')
    return
  }
  renameSaving.value = true
  try {
    const updated = await updateProject(target.id, { title, lock_version: target.lock_version })
    // 就地更新列表项，避免整页刷新
    const index = projects.value.findIndex((p) => p.id === target.id)
    if (index >= 0) projects.value[index] = updated
    toast.success('项目已重命名')
    renameTarget.value = null
    // 修复（E2E 报告问题 10）：就地替换后卡片仍显示旧名（焦点大卡与列表
    // 由不同数据源渲染），补一次列表拉取，保证重命名即时可见。
    await fetchProjects()
  } catch {
    // 拦截器已 toast（10006 乐观锁冲突等）
  } finally {
    renameSaving.value = false
  }
}

/**
 * 打开归档二次确认弹窗。
 * @param project 目标项目
 * @returns 无
 */
function openArchive(project: Project): void {
  openMenuId.value = ''
  archiveTarget.value = project
}

/**
 * 确认归档（POST /projects/{id}/archive）。
 * @returns 无
 */
async function confirmArchive(): Promise<void> {
  const target = archiveTarget.value
  if (!target) return
  archiveSaving.value = true
  try {
    await archiveProject(target.id)
    toast.success(`「${target.title}」已归档`)
    archiveTarget.value = null
    await fetchProjects()
  } catch {
    // 拦截器已 toast（40004 重复归档等）
  } finally {
    archiveSaving.value = false
  }
}

/** 恢复归档中的项目 ID（用于 loading 与防重复点击） */
const restoringId = ref<string | number | null>(null)

/**
 * 恢复归档项目（POST /projects/{id}/unarchive）：归档卡片菜单的唯一回退出口。
 * @param project 目标项目
 * @returns 无
 */
async function restoreArchived(project: Project): Promise<void> {
  if (restoringId.value !== null) return
  openMenuId.value = ''
  restoringId.value = project.id
  try {
    const restored = await unarchiveProject(project.id)
    toast.success(`「${restored.title}」已恢复，可继续创作`)
    await fetchProjects()
  } catch {
    // 拦截器已 toast（20003 非 Owner / 40001 未归档）
  } finally {
    restoringId.value = null
  }
}

/** 恢复失败项目进行中（用于 loading 与防重复点击） */
const resumingId = ref<string | number | null>(null)

/**
 * 恢复失败项目（POST /projects/{id}/resume）：failed 状态的唯一回退出口，
 * 还原 config._failed_before 记录的失败前状态，回到对应环节继续操作。
 * @param project 目标项目
 * @returns 无
 */
async function resumeFailed(project: Project): Promise<void> {
  if (resumingId.value !== null) return
  openMenuId.value = ''
  resumingId.value = project.id
  try {
    const resumed = await resumeProject(project.id)
    toast.success(`「${resumed.title}」已恢复到「${projectStatusLabel(resumed.status)}」状态，可继续创作`)
    await fetchProjects()
  } catch {
    // 拦截器已 toast（40001 未处于失败状态等）
  } finally {
    resumingId.value = null
  }
}

// 挂载：首次拉取 + 遥测 + 注册菜单收起监听
onMounted(() => {
  void fetchProjects()
  void fetchTelemetry()
  document.addEventListener('click', onDocClick)
})

// 卸载：移除监听
onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
})
</script>

<template>
  <div class="mx-auto max-w-content p-6 lg:p-8">
    <!-- ============ 1. 刊头行（原型 Masthead：竖紫条 + 衬线大标题 + 徽标 + 副文案 | 动作工具条） ============ -->
    <header class="mb-6 flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
      <div class="flex items-start gap-3">
        <span class="mt-1.5 h-9 w-1 shrink-0 rounded-full bg-primary-500 shadow-brand" aria-hidden="true" />
        <div class="min-w-0 space-y-1.5">
          <div class="flex flex-wrap items-center gap-2.5">
            <h1 class="gd-masthead text-masthead">浮光造梦 · 创作空间</h1>
            <span class="gd-badge gd-badge--violet">STUDIO V3.2</span>
          </div>
          <p class="gd-lede">从一句话灵感到爆款短剧成片，端到端 AI 多智能体创作阵列与导演决策台。</p>
        </div>
      </div>

      <!-- 动作工具条：快捷状态页签 + 模板导入 + 新建 + 视图切换（原型同位） -->
      <div class="flex flex-wrap items-center gap-2.5 lg:shrink-0" role="toolbar" aria-label="项目快捷筛选与操作">
        <SegmentedControl v-model="quickTab" :options="QUICK_TAB_OPTIONS" aria-label="项目状态快捷筛选" />
        <span class="hidden h-5 w-px bg-hairline sm:block" aria-hidden="true" />
        <AppButton variant="ghost" size="md" @click="goCreate({ source: 'template' })">
          <span class="inline-flex items-center gap-1.5">
            <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path :d="ICON_SPARKLES" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            从模板库导入
          </span>
        </AppButton>
        <AppButton variant="primary" size="md" @click="goCreate()">
          <span class="inline-flex items-center gap-1.5">
            <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M12 4.5v15m7.5-7.5h-15" stroke-linecap="round" />
            </svg>
            新建短剧项目
          </span>
        </AppButton>
        <!-- 视图切换（网格 / 列表，既有件原位保留） -->
        <div class="flex items-center gap-0.5 rounded-full border border-hairline bg-surface-inset p-0.5" role="group" aria-label="视图切换">
          <button
            type="button"
            class="flex h-8 w-8 items-center justify-center rounded-full transition-[background-color,color,box-shadow] duration-gd-fast ease-gd-standard"
            :class="viewMode === 'grid' ? 'bg-surface-raised text-neutral-800 shadow-gd-1' : 'text-neutral-500 hover:text-neutral-700'"
            :aria-pressed="viewMode === 'grid'"
            @click="viewMode = 'grid'"
          >
            <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
          <button
            type="button"
            class="flex h-8 w-8 items-center justify-center rounded-full transition-[background-color,color,box-shadow] duration-gd-fast ease-gd-standard"
            :class="viewMode === 'list' ? 'bg-surface-raised text-neutral-800 shadow-gd-1' : 'text-neutral-500 hover:text-neutral-700'"
            :aria-pressed="viewMode === 'list'"
            @click="viewMode = 'list'"
          >
            <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </div>
      </div>
    </header>

    <!-- ============ 1.1 次级筛选行（既有细粒度筛选收纳：搜索 / 状态 / 题材 / 排序 / 范围） ============ -->
    <div class="mb-6 flex flex-wrap items-center gap-2.5" role="toolbar" aria-label="项目筛选">
      <!-- 搜索胶囊 -->
      <div class="gd-field-group flex h-10 w-full items-center gap-2 px-4 sm:w-64" role="search">
        <svg class="h-4 w-4 shrink-0 text-neutral-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <input
          v-model="keyword"
          type="search"
          placeholder="搜索项目名..."
          aria-label="搜索项目名"
          class="w-full bg-transparent text-sm text-neutral-800 outline-none placeholder:text-neutral-400"
        />
      </div>

      <!-- 状态筛选（14 态） -->
      <select v-model="statusFilter" class="gd-input w-auto" aria-label="按状态筛选">
        <option value="">全部状态</option>
        <option v-for="key in PROJECT_STATUS_KEYS" :key="key" :value="key">{{ projectStatusLabel(key) }}</option>
      </select>

      <!-- 题材筛选 -->
      <select v-model="genreFilter" class="gd-input w-auto" aria-label="按题材筛选">
        <option value="">全部题材</option>
        <option v-for="genre in GENRE_OPTIONS" :key="genre.key" :value="genre.key">{{ genre.label }}</option>
      </select>

      <!-- 排序 -->
      <select v-model="sort" class="gd-input w-auto" aria-label="排序方式">
        <option v-for="option in PROJECT_SORT_OPTIONS" :key="option.key" :value="option.key">{{ option.label }}</option>
      </select>

      <!-- 范围切换 -->
      <SegmentedControl v-model="scope" :options="SCOPE_OPTIONS" aria-label="项目范围" />
    </div>

    <!-- ============ 加载骨架（按新栅格组合：焦点卡 + 生成器 + 双监控卡 + 矩阵） ============ -->
    <div v-if="loading" class="grid grid-cols-12 gap-5">
      <LoadingSkeleton variant="card" class="col-span-12 min-h-[420px] xl:col-span-8" />
      <LoadingSkeleton variant="card" class="col-span-12 min-h-[420px] xl:col-span-4" />
      <LoadingSkeleton variant="chart" class="col-span-12 md:col-span-6 xl:col-span-4" />
      <LoadingSkeleton variant="card" class="col-span-12 md:col-span-6 xl:col-span-4" />
      <LoadingSkeleton variant="card" class="col-span-12 md:col-span-6 xl:col-span-4" />
      <LoadingSkeleton variant="card" class="col-span-12 md:col-span-6 lg:col-span-4" />
      <LoadingSkeleton variant="card" class="col-span-12 md:col-span-6 lg:col-span-4" />
      <LoadingSkeleton variant="card" class="col-span-12 md:col-span-6 lg:col-span-4" />
    </div>

    <!-- ============ 加载失败 ============ -->
    <ErrorRetryBlock
      v-else-if="errorMsg"
      message="项目列表加载失败"
      :description="errorMsg"
      @retry="fetchProjects"
    />

    <!-- ============ 空态 ============ -->
    <EmptyState
      v-else-if="visibleProjects.length === 0 && !isFiltered"
      :icon-path="ICON_CLAPPER"
      title="还没有项目"
      description="还没有项目——从一句话创意开始你的第一部短剧"
      action-text="+ 新建项目"
      @action="goCreate()"
    />
    <EmptyState
      v-else-if="visibleProjects.length === 0"
      :icon-path="ICON_SEARCH"
      title="没有匹配的项目"
      description="关键词仅过滤当前页；可翻页继续查找，或清空筛选后按状态 / 题材浏览"
      action-text="清空筛选"
      @action="clearFilters"
    />

    <!-- ============ 数据区（12 栏 Bento：焦点卡 + 生成器 + 双监控卡 + 近期矩阵） ============ -->
    <template v-else>
      <div class="grid grid-cols-12 gap-5">
        <!-- ======== 2. 焦点项目大卡（xl:col-span-8，左夜窗封面 5/12 + 右信息区 7/12） ======== -->
        <section v-if="focusProject" class="col-span-12 gd-card-field p-5 xl:col-span-8">
          <div class="flex flex-col gap-5 md:flex-row">
            <!-- 左：夜窗竖版封面（EP 集数徽标 + 状态 pill + 题材/画风 pills + 画幅/语言 meta） -->
            <div class="flex w-full shrink-0 flex-col md:w-5/12">
              <div class="gd-night-window gd-night-window-hero relative flex min-h-[320px] w-full flex-col justify-between p-4 md:h-full">
                <!-- 封面色场（cv 渐变族 + 暗底 scrim），垫在内容层之下 -->
                <div class="absolute inset-0" aria-hidden="true">
                  <div class="h-full w-full gd-cv-cover" :class="coverClass(focusProject.id)" />
                </div>

                <!-- 顶部：集数徽标（琥珀脉冲点）+ 进行中状态 pill -->
                <div class="relative z-10 flex items-center justify-between gap-2">
                  <span
                    class="inline-flex items-center gap-1.5 rounded-full border border-[rgba(255,255,255,0.24)] bg-[rgba(4,7,15,0.55)] px-2.5 py-1 text-[11px] text-[color:var(--gd-window-text)]"
                  >
                    <span class="h-2 w-2 animate-pulse rounded-full bg-warning" aria-hidden="true" />
                    共 <span class="font-num tabular-nums">{{ focusProject.target_episode_count ?? '—' }}</span> 集
                  </span>
                  <StatusTag :status="focusProject.status" size="sm" variant="dark" />
                </div>

                <!-- 底部：题材 / 画风 pills + 画幅 / 语言 meta 条 -->
                <div class="relative z-10 mt-auto space-y-2 pt-6">
                  <div class="flex flex-wrap gap-1.5">
                    <span
                      class="rounded-full border border-[rgba(255,255,255,0.2)] bg-[rgba(255,255,255,0.08)] px-2 py-0.5 text-[10px] text-[color:var(--gd-window-text)]"
                    >
                      {{ genreLabel(focusProject.genre) }}
                    </span>
                    <span
                      v-if="styleLabel(focusProject.style_preset_key)"
                      class="rounded-full border border-[rgba(255,255,255,0.2)] bg-[rgba(255,255,255,0.08)] px-2 py-0.5 text-[10px] text-[color:var(--gd-window-text)]"
                    >
                      {{ styleLabel(focusProject.style_preset_key) }}
                    </span>
                  </div>
                  <div class="flex items-center justify-between gap-2 text-[11px] text-[color:var(--gd-window-hint)]">
                    <span class="inline-flex items-center gap-1.5">
                      <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                        <path d="M3.75 5.25h16.5v13.5H3.75z" stroke-linecap="round" stroke-linejoin="round" />
                      </svg>
                      {{ focusProject.aspect_ratio ?? '—' }} 画幅
                    </span>
                    <span class="font-num tabular-nums">{{ focusProject.language ?? 'zh-CN' }}</span>
                  </div>
                </div>
              </div>
            </div>

            <!-- 右：项目核心信息台（标题 + ID + ⋮ 菜单 + 简介 + 存盘版本 + HITL 四关口 + 双指标 + CTA） -->
            <div class="flex min-w-0 flex-1 flex-col justify-between gap-4">
              <div class="space-y-3">
                <!-- 头部：标题 + ID 徽标 + ⋮ 菜单（既有重命名 / 归档） -->
                <div class="flex items-start justify-between gap-3">
                  <div class="min-w-0">
                    <div class="flex flex-wrap items-center gap-2">
                      <h2 class="font-serif text-[22px] leading-tight">{{ focusProject.title }}</h2>
                      <span class="gd-badge gd-badge--violet">ID: {{ focusProject.id }}</span>
                    </div>
                    <p class="mt-1 line-clamp-2 text-[13px] text-ink-soft">
                      {{ focusProject.synopsis || '暂无简介' }}
                    </p>
                  </div>
                  <div class="relative shrink-0" data-project-menu>
                    <button
                      type="button"
                      class="flex h-8 w-8 items-center justify-center rounded-lg border border-hairline text-neutral-400 transition-colors duration-gd-fast hover:bg-state-hover hover:text-neutral-200"
                      aria-label="更多操作"
                      aria-haspopup="menu"
                      :aria-expanded="openMenuId === String(focusProject.id)"
                      @click.stop="openMenuId = openMenuId === String(focusProject.id) ? '' : String(focusProject.id)"
                    >
                      <svg class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                        <circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" />
                      </svg>
                    </button>
                    <div
                      v-if="openMenuId === String(focusProject.id)"
                      class="absolute right-0 top-10 z-20 w-28 overflow-hidden rounded-lg border border-hairline bg-surface-raised py-1 shadow-gd-3"
                      role="menu"
                    >
                      <button
                        type="button"
                        class="block w-full px-3 py-1.5 text-left text-xs text-neutral-700 transition-colors duration-gd-fast hover:bg-surface-canvas"
                        role="menuitem"
                        @click.stop="openRename(focusProject)"
                      >
                        重命名
                      </button>
                      <button
                        v-if="focusProject.status === 'failed'"
                        type="button"
                        class="block w-full px-3 py-1.5 text-left text-xs text-amber-700 transition-colors duration-gd-fast hover:bg-surface-canvas"
                        role="menuitem"
                        :disabled="resumingId === focusProject.id"
                        @click.stop="resumeFailed(focusProject)"
                      >
                        {{ resumingId === focusProject.id ? '恢复中…' : '恢复项目' }}
                      </button>
                      <button
                        v-if="focusProject.status !== 'archived'"
                        type="button"
                        class="block w-full px-3 py-1.5 text-left text-xs text-neutral-700 transition-colors duration-gd-fast hover:bg-surface-canvas"
                        role="menuitem"
                        @click.stop="openArchive(focusProject)"
                      >
                        归档
                      </button>
                      <button
                        v-else
                        type="button"
                        class="block w-full px-3 py-1.5 text-left text-xs text-primary-700 transition-colors duration-gd-fast hover:bg-surface-canvas disabled:cursor-not-allowed disabled:opacity-50"
                        role="menuitem"
                        :disabled="restoringId === focusProject.id"
                        @click.stop="restoreArchived(focusProject)"
                      >
                        {{ restoringId === focusProject.id ? '恢复中…' : '恢复归档' }}
                      </button>
                    </div>
                  </div>
                </div>

                <!-- 存盘 meta：更新时间（自动存盘）• 乐观锁版本 -->
                <div class="flex flex-wrap items-center gap-2 gd-timecode">
                  <span class="inline-flex items-center gap-1">
                    <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                      <path :d="ICON_CLOCK" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                    {{ formatRelativeTime(focusProject.updated_at) }} 自动存盘
                  </span>
                  <span aria-hidden="true">•</span>
                  <span class="font-medium text-ink-soft">版本 v{{ focusProject.lock_version ?? '—' }}</span>
                </div>

                <!-- HITL 智导四关口（Human-in-the-Loop）检查卡组 -->
                <div class="rounded-lg border border-hairline bg-surface-sunken p-3">
                  <div class="mb-2 flex items-center justify-between gap-2">
                    <span class="gd-section-label">HITL 智导四关口 (HUMAN-IN-THE-LOOP)</span>
                    <span class="truncate text-caption font-medium text-primary-300">{{ focusGateHint }}</span>
                  </div>
                  <div class="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
                    <div
                      v-for="gate in focusGates"
                      :key="gate.key"
                      class="flex items-center gap-1.5 rounded-md p-2"
                      :class="GATE_CARD_CLASS[gate.state]"
                    >
                      <svg
                        class="h-4 w-4 shrink-0"
                        :class="{ 'animate-spin': gate.spin }"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="2"
                        aria-hidden="true"
                      >
                        <path :d="gateIcon(gate.state)" stroke-linecap="round" stroke-linejoin="round" />
                      </svg>
                      <div class="min-w-0">
                        <div class="truncate text-micro">关口{{ gate.no }}: {{ gate.title }}</div>
                        <div class="font-num text-[10px] leading-tight tabular-nums">{{ gate.hint }} H{{ gate.no }}</div>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- 积分预算 gauge + 核心质检一致性 -->
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <!-- 项目积分算力预算 -->
                  <div class="rounded-lg border border-hairline bg-surface-sunken p-3">
                    <div class="mb-1.5 flex items-center justify-between gap-2 text-caption">
                      <span class="text-ink-mute">项目积分算力预算</span>
                      <span class="gd-num text-caption">{{ focusBudgetText }}</span>
                    </div>
                    <div
                      class="h-2 w-full overflow-hidden rounded-full bg-surface-inset"
                      role="progressbar"
                      :aria-valuenow="focusBudgetPercent"
                      aria-valuemin="0"
                      aria-valuemax="100"
                      :aria-label="`预算消耗 ${focusBudgetPercent}%`"
                    >
                      <div
                        class="h-full rounded-full bg-primary-500 transition-[width] duration-gd-slow"
                        :style="{ width: `${focusBudgetPercent}%` }"
                      />
                    </div>
                    <div class="mt-1 flex items-center justify-between gap-2 font-num text-[10px] tabular-nums text-ink-mute">
                      <span>已耗 {{ focusProject.spent_credits ?? 0 }} 积分</span>
                      <span>{{ focusBudgetFoot }}</span>
                    </div>
                  </div>
                  <!-- 核心质检一致性（真实状态归纳，不虚构分数） -->
                  <div class="flex flex-col justify-between gap-2 rounded-lg border border-hairline bg-surface-sunken p-3">
                    <div class="flex items-center justify-between gap-2">
                      <span class="text-caption text-ink-mute">核心质检一致性</span>
                      <StatusTag :status="focusProject.status" size="sm" />
                    </div>
                    <div class="flex items-center gap-1.5 text-[11px] text-ink-soft">
                      <span class="h-2 w-2 shrink-0 rounded-full" :style="{ backgroundColor: focusQualityDot }" aria-hidden="true" />
                      <span>{{ focusQualityHint }}</span>
                    </div>
                  </div>
                </div>
              </div>

              <!-- CTA 行：快照历史 / 项目设置 | 主 CTA -->
              <div class="flex flex-wrap items-center justify-between gap-2 pt-1">
                <div class="flex flex-wrap items-center gap-2">
                  <AppButton variant="subtle" size="sm" :disabled="!focusProject" @click="goSnapshots">
                    <span class="inline-flex items-center gap-1.5">
                      <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                        <path :d="ICON_CLOCK" stroke-linecap="round" stroke-linejoin="round" />
                      </svg>
                      项目快照历史
                    </span>
                  </AppButton>
                  <AppButton variant="subtle" size="sm" :disabled="!focusProject" @click="openFocusRename">项目设置</AppButton>
                </div>
                <AppButton variant="primary" size="md" :disabled="!focusProject" @click="goHeroCta">
                  <span class="inline-flex items-center gap-1.5">
                    {{ focusProject ? heroCtaLabel(focusProject) : '进入智能分镜工作台' }}
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                      <path :d="ICON_ARROW_RIGHT" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </span>
                </AppButton>
              </div>
            </div>
          </div>
        </section>

        <!-- ======== 3. QUICK GENERATOR 抽屉卡（xl:col-span-4，可收起） ======== -->
        <section class="col-span-12 flex flex-col gd-card-field p-5 xl:col-span-4">
          <div class="flex items-center justify-between">
            <span class="gd-badge gd-badge--violet">QUICK GENERATOR</span>
            <button
              type="button"
              class="flex h-8 w-8 items-center justify-center rounded-lg text-neutral-400 transition-[color,transform] duration-gd-fast hover:bg-state-hover hover:text-primary-300"
              :aria-expanded="!generatorCollapsed"
              :aria-label="generatorCollapsed ? '展开快捷生成器' : '收起快捷生成器'"
              @click="generatorCollapsed = !generatorCollapsed"
            >
              <svg
                class="h-4 w-4 transition-transform duration-gd-fast"
                :class="generatorCollapsed ? 'rotate-180' : ''"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                aria-hidden="true"
              >
                <path d="M15.75 19.5L8.25 12l7.5-7.5" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </button>
          </div>

          <template v-if="!generatorCollapsed">
            <div class="flex flex-1 flex-col items-center justify-center gap-2 py-6 text-center">
              <!-- 大 ⊕ 按钮：新建项目 -->
              <button
                type="button"
                class="flex h-14 w-14 items-center justify-center rounded-full border border-hairline bg-surface-sunken text-primary-300 transition-[background-color,color,border-color,box-shadow] duration-gd-normal ease-gd-standard hover:border-primary-400 hover:bg-state-hover hover:text-primary-200 hover:shadow-brand"
                aria-label="新建短剧项目"
                @click="goCreate()"
              >
                <svg class="h-7 w-7" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
                  <path d="M12 4.5v15m7.5-7.5h-15" stroke-linecap="round" />
                </svg>
              </button>
              <h3 class="mt-2 font-serif text-lg">开启全新故事旅程</h3>
              <p class="max-w-xs text-[13px] leading-relaxed text-ink-soft">
                支持输入一句话灵感、导入网络长篇小说大纲或专业影视剧本，秒级构筑 AI 导演分镜矩阵。
              </p>
              <!-- 灵感输入 + 闪电提交（提交后携种子进入创建向导） -->
              <div class="relative mt-2 w-full">
                <input
                  v-model="generatorSeed"
                  type="text"
                  class="gd-input h-10 w-full pr-11"
                  placeholder="输入灵感，如：民国特工潜伏于古董当铺..."
                  aria-label="一句话灵感"
                  @keyup.enter="submitInspiration"
                />
                <button
                  type="button"
                  class="gd-fill-brand absolute right-1.5 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-md transition-transform duration-gd-fast hover:scale-105 active:scale-95"
                  aria-label="以灵感新建项目"
                  @click="submitInspiration"
                >
                  <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                    <path :d="ICON_BOLT" stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
              </div>
            </div>

            <!-- 热门题材直达矩阵（题材 chip 直达创建向导） -->
            <div>
              <div class="gd-section-label mb-2">热门题材直达矩阵</div>
              <div class="flex flex-wrap gap-2">
                <button
                  v-for="genre in GENERATOR_GENRES"
                  :key="genre.key"
                  type="button"
                  class="gd-tag transition-colors duration-gd-fast hover:bg-state-hover hover:text-ink"
                  @click="startFromGenre(genre.key)"
                >
                  {{ genre.label }}
                </button>
              </div>
            </div>
          </template>
        </section>

        <!-- ======== 4. 近 7 日算力与产出流水（夜窗卡，md:6 / xl:4） ======== -->
        <section class="col-span-12 flex flex-col justify-between gap-4 gd-night-window gd-night-window-panel p-5 md:col-span-6 xl:col-span-4">
          <div>
            <div class="mb-4 flex items-center justify-between gap-2">
              <div class="flex items-center gap-2">
                <span class="h-2 w-2 rounded-full bg-primary-400 shadow-brand" aria-hidden="true" />
                <span class="gd-section-label">近 7 日算力与产出流水</span>
              </div>
              <span class="font-num text-[10px] tracking-wider text-[color:var(--gd-window-hint)]">LIVE TELEMETRY</span>
            </div>

            <!-- 三大数：7 日消耗 / 结算笔数 / 可用积分 -->
            <div class="grid grid-cols-3 gap-2">
              <div>
                <div class="gd-num gd-t-brand text-xl">{{ Math.round(weekConsumed) }}</div>
                <div class="mt-0.5 text-[10px] text-[color:var(--gd-window-hint)]">7 日消耗积分</div>
              </div>
              <div>
                <div class="gd-num gd-t-ink text-xl">{{ weekSettleCount }}</div>
                <div class="mt-0.5 text-[10px] text-[color:var(--gd-window-hint)]">结算笔数 (Tasks)</div>
              </div>
              <div>
                <div class="gd-num gd-t-mint text-xl">{{ availableCredits === null ? '—' : Math.round(availableCredits) }}</div>
                <div class="mt-0.5 text-[10px] text-[color:var(--gd-window-hint)]">可用积分 (GPU)</div>
              </div>
            </div>

            <!-- 7 日柱状图（settle 流水按日聚合，今天高亮） -->
            <div class="mt-4 flex h-20 items-end justify-between gap-2 px-1">
              <div
                v-for="day in telemetryWeek"
                :key="day.key"
                class="flex h-full w-full flex-col items-center justify-end gap-1.5"
                :title="`${day.label}：${Math.round(day.credits)} 积分 / ${day.count} 笔`"
              >
                <div
                  class="w-full rounded-t transition-[height,background-color] duration-gd-normal"
                  :class="day.isToday ? 'bg-primary-500 shadow-brand' : 'bg-[rgba(139,124,255,0.22)]'"
                  :style="{ height: `${barHeight(day)}%` }"
                />
                <span class="font-num text-[10px] tabular-nums" :class="day.isToday ? 'text-primary-300' : 'text-[color:var(--gd-window-hint)]'">
                  {{ day.label }}
                </span>
              </div>
            </div>
            <p v-if="telemetryError" class="mt-2 text-[10px] text-[color:var(--gd-window-hint)]">流水加载失败，读数暂不可用</p>
          </div>

          <!-- 汇总行：账户余额 + 流水报表入口 -->
          <div class="flex items-center justify-between border-t border-hairline-soft pt-3 text-caption text-[color:var(--gd-window-hint)]">
            <span>
              当前余额:
              <span class="gd-num gd-t-ink">{{ creditAccount ? Math.round(creditAccount.balance_credits) : '—' }}</span>
            </span>
            <button
              type="button"
              class="inline-flex items-center gap-0.5 font-medium text-primary-300 transition-colors duration-gd-fast hover:text-primary-200"
              @click="goBilling"
            >
              查看流水报表
              <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path :d="ICON_ARROW_RIGHT" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </button>
          </div>
        </section>

        <!-- ======== 5. 渲染队列监控（md:6 / xl:4） ======== -->
        <section class="col-span-12 flex flex-col justify-between gap-4 gd-card-field p-5 md:col-span-6 xl:col-span-4">
          <div>
            <div class="mb-4 flex items-center justify-between gap-2">
              <div class="flex items-center gap-2">
                <span class="relative flex h-2.5 w-2.5" aria-hidden="true">
                  <span v-if="queueRunningCount > 0" class="absolute inline-flex h-full w-full animate-ping rounded-full bg-warning opacity-75" />
                  <span class="relative inline-flex h-2.5 w-2.5 rounded-full" :class="queueRunningCount > 0 ? 'bg-warning' : 'bg-neutral-500'" />
                </span>
                <h3 class="font-serif text-base">渲染队列监控</h3>
              </div>
              <span v-if="queueError" class="gd-badge gd-badge--rose">队列加载失败</span>
              <span v-else-if="queueRunningCount > 0" class="gd-badge gd-badge--amber"><span class="gd-badge__dot" aria-hidden="true" />生成处理中</span>
              <span v-else class="gd-badge">队列空闲</span>
            </div>

            <!-- 队列三格：Celery 队列 / 渲染并发 / 今日已交付 -->
            <div class="mb-4 flex flex-wrap items-center gap-2">
              <span class="gd-badge">
                Celery 队列: <strong class="font-num tabular-nums">{{ queuePendingCount }}</strong>
              </span>
              <span class="gd-badge gd-badge--amber">
                渲染并发: <strong class="font-num tabular-nums">{{ queueRunningCount }}</strong>
              </span>
              <span class="gd-badge gd-badge--mint">
                今日已交付: <strong class="font-num tabular-nums">{{ queueDeliveredToday }}</strong>
              </span>
            </div>

            <!-- 当前任务卡（列表接口无进度字段，用 live 扫光条表达「进行中」） -->
            <div v-if="activeQueueTask" class="space-y-2 rounded-lg border border-hairline bg-surface-sunken p-3">
              <div class="flex items-center justify-between gap-2">
                <span class="truncate text-caption font-semibold text-ink">
                  #{{ activeQueueTask.id }} · {{ taskTypeLabel(activeQueueTask) }}
                  <template v-if="activeQueueTask.shot"> · 第 {{ activeQueueTask.shot.shot_no }} 镜</template>
                </span>
                <span class="shrink-0 font-num text-caption font-semibold tabular-nums text-primary-300">
                  {{ projectStatusLabel(activeQueueTask.status) }}
                </span>
              </div>
              <p class="truncate text-[11px] text-ink-mute">
                预估 {{ activeQueueTask.estimated_cost_credits }} 积分 · 已重试 {{ activeQueueTask.retry_count }} 次
              </p>
              <div class="gd-progress gd-progress--live">
                <div class="gd-progress__bar w-2/5" />
              </div>
              <div class="flex items-center justify-between gap-2 pt-1 font-num text-[10px] tabular-nums text-ink-mute">
                <span>创建于 {{ formatRelativeTime(activeQueueTask.created_at) }}</span>
                <span>优先级 {{ activeQueueTask.priority }}</span>
              </div>
            </div>
            <div v-else class="rounded-lg border border-dashed border-hairline p-4 text-center text-caption text-ink-mute">
              {{ queueError ? '渲染队列加载失败，可进入任务中心查看' : '当前没有进行中的渲染任务' }}
            </div>
          </div>

          <!-- 底部：容错说明 + 管理完整队列入口 -->
          <div class="flex items-center justify-between gap-2 text-caption text-ink-soft">
            <span class="inline-flex items-center gap-1 text-ink-mute">
              <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path :d="ICON_SHIELD" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              自动容错重试: 开启<template v-if="queueFailedCount > 0"> · 失败 {{ queueFailedCount }}</template>
            </span>
            <button
              type="button"
              class="inline-flex items-center gap-0.5 font-semibold text-primary-300 transition-colors duration-gd-fast hover:text-primary-200"
              @click="goRenderCenter"
            >
              管理完整队列
              <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M8.25 4.5l7.5 7.5-7.5 7.5" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </button>
          </div>
        </section>

        <!-- ======== 6. 近期剧作矩阵（默认最近 3 卡 + 历史展开链接） ======== -->
        <div class="col-span-12 space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-baseline gap-2">
              <h2 class="font-serif text-lg">近期剧作矩阵</h2>
              <span class="font-num text-caption tabular-nums text-ink-mute">( {{ matrixProjects.length }} 个在库活跃项目 )</span>
            </div>
            <button
              type="button"
              class="inline-flex items-center gap-0.5 text-caption font-semibold text-primary-300 transition-colors duration-gd-fast hover:text-primary-200"
              :aria-expanded="libraryExpanded"
              @click="libraryExpanded = !libraryExpanded"
            >
              {{ libraryExpanded ? '收起历史项目' : `浏览所有历史项目 (${loading ? '—' : total})` }}
              <svg
                class="h-3.5 w-3.5 transition-transform duration-gd-fast"
                :class="libraryExpanded ? 'rotate-90' : ''"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                stroke-width="2"
                aria-hidden="true"
              >
                <path d="M8.25 4.5l7.5 7.5-7.5 7.5" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </button>
          </div>

          <!-- 网格视图分支（卡片墙 + 虚线新建卡）；template 保证 v-else 与本分支正确配对 -->
          <template v-if="viewMode === 'grid'">
          <div class="grid grid-cols-1 gap-5 md:grid-cols-2 lg:grid-cols-3">
            <article
              v-for="project in matrixProjects"
              :key="project.id"
              class="group relative flex flex-col overflow-hidden gd-card-field gd-card-interactive"
            >
              <!-- 整卡可点（键盘可达）；···菜单为兄弟浮层，避免按钮嵌套 -->
              <button
                type="button"
                class="relative flex h-full w-full flex-col text-left"
                :aria-label="`打开项目 ${project.title}`"
                @click="continueCreate(project)"
              >
                <!-- 微型夜窗封面：左上关口状态徽标 + 底部统计条 -->
                <div class="gd-night-window gd-night-window-sm relative h-44 shrink-0">
                  <div class="absolute inset-0" aria-hidden="true">
                    <div class="h-full w-full gd-cv-cover" :class="coverClass(project.id)" />
                  </div>
                  <StatusTag :status="project.status" size="sm" variant="dark" class="absolute left-2.5 top-2.5 z-10" />
                  <div
                    class="absolute bottom-2.5 left-2.5 right-2.5 z-10 flex items-center justify-between gap-2 text-[10px] text-[color:var(--gd-window-hint)]"
                  >
                    <span class="font-num tabular-nums">共 {{ project.target_episode_count ?? '—' }} 集</span>
                    <span v-if="project.aspect_ratio" class="font-num tabular-nums">{{ project.aspect_ratio }}</span>
                  </div>
                </div>

                <!-- 信息区：标题 + 题材 tag + 简介 -->
                <div class="flex flex-1 flex-col gap-1.5 p-4">
                  <div class="flex items-start justify-between gap-2">
                    <h3 class="font-serif text-base leading-snug">{{ project.title }}</h3>
                    <span class="gd-tag shrink-0">{{ genreLabel(project.genre) }}</span>
                  </div>
                  <p class="line-clamp-2 text-[13px] text-ink-soft">{{ project.synopsis || '暂无简介' }}</p>
                </div>

                <!-- 页脚 meta：更新时间 + 行尾箭头（整卡即按钮，箭头为视觉元素） -->
                <div class="mt-auto flex items-center justify-between border-t border-hairline-soft px-4 py-2.5 text-[11px] text-ink-mute">
                  <span class="inline-flex items-center gap-1 font-num tabular-nums">
                    <svg class="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                      <path :d="ICON_CLOCK" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                    {{ formatRelativeTime(project.updated_at) }}
                  </span>
                  <span class="text-primary-300 transition-transform duration-gd-fast group-hover:translate-x-0.5" aria-hidden="true">
                    <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path :d="ICON_ARROW_RIGHT" stroke-linecap="round" stroke-linejoin="round" />
                    </svg>
                  </span>
                </div>
              </button>

              <!-- ··· 菜单（兄弟浮层，避免按钮嵌套） -->
              <div class="absolute right-3 top-3 z-10" data-project-menu>
                <button
                  type="button"
                  class="flex h-8 w-8 items-center justify-center rounded-full border border-[rgba(255,255,255,0.24)] bg-[rgba(4,7,15,0.42)] text-[#cdd7ea] backdrop-blur-[8px] transition-colors duration-gd-fast hover:border-[rgba(255,255,255,0.46)] hover:bg-[rgba(4,7,15,0.68)] hover:text-[#eef3fc]"
                  aria-label="更多操作"
                  aria-haspopup="menu"
                  :aria-expanded="openMenuId === String(project.id)"
                  @click.stop="openMenuId = openMenuId === String(project.id) ? '' : String(project.id)"
                >
                  <svg class="h-4 w-4" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                    <circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" />
                  </svg>
                </button>
                <!-- 下拉菜单 -->
                <div
                  v-if="openMenuId === String(project.id)"
                  class="absolute right-0 top-10 z-20 w-28 overflow-hidden rounded-lg border border-hairline bg-surface-raised py-1 shadow-gd-3"
                  role="menu"
                >
                  <button
                    type="button"
                    class="block w-full px-3 py-1.5 text-left text-xs text-neutral-700 transition-colors duration-gd-fast hover:bg-surface-canvas"
                    role="menuitem"
                    @click.stop="openRename(project)"
                  >
                    重命名
                  </button>
                  <button
                    v-if="project.status === 'failed'"
                    type="button"
                    class="block w-full px-3 py-1.5 text-left text-xs text-amber-700 transition-colors duration-gd-fast hover:bg-surface-canvas"
                    role="menuitem"
                    :disabled="resumingId === project.id"
                    @click.stop="resumeFailed(project)"
                  >
                    {{ resumingId === project.id ? '恢复中…' : '恢复项目' }}
                  </button>
                  <button
                    v-if="project.status !== 'archived'"
                    type="button"
                    class="block w-full px-3 py-1.5 text-left text-xs text-neutral-700 transition-colors duration-gd-fast hover:bg-surface-canvas"
                    role="menuitem"
                    @click.stop="openArchive(project)"
                  >
                    归档
                  </button>
                  <button
                    v-else
                    type="button"
                    class="block w-full px-3 py-1.5 text-left text-xs text-primary-700 transition-colors duration-gd-fast hover:bg-surface-canvas disabled:cursor-not-allowed disabled:opacity-50"
                    role="menuitem"
                    :disabled="restoringId === project.id"
                    @click.stop="restoreArchived(project)"
                  >
                    {{ restoringId === project.id ? '恢复中…' : '恢复归档' }}
                  </button>
                </div>
              </div>
            </article>
          </div>

          <!-- 尾部虚线新建卡（历史展开时出现，保留既有引导件） -->
          <button
            v-if="libraryExpanded"
            type="button"
            class="flex w-full flex-col items-center justify-center gap-3 rounded-[var(--gd-radius-lg)] border border-dashed border-hairline-strong px-6 py-10 text-center transition-[border-color,background-color] duration-gd-fast ease-gd-standard hover:border-primary-300 hover:bg-primary-50/40"
            aria-label="新建短剧项目"
            @click="goCreate()"
          >
            <span class="flex h-10 w-10 items-center justify-center rounded-full bg-primary-50 text-primary-500" aria-hidden="true">
              <svg class="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
                <path d="M12 4.5v15m7.5-7.5h-15" stroke-linecap="round" />
              </svg>
            </span>
            <span class="font-serif text-lg font-semibold text-neutral-900">新建短剧项目</span>
            <span class="max-w-md text-sm text-neutral-500">从一句话灵感开始，或上传现成剧本 —— 下一部爆款就在这张卡上开镜。</span>
            <span class="mt-1 inline-flex h-9 items-center rounded-full border border-hairline bg-surface-raised px-5 text-sm text-neutral-800 shadow-gd-1">
              从模板开始
            </span>
          </button>
          </template>

          <!-- 列表视图（容器可横向滚动，窄屏不再裁切 7 列） -->
          <div v-else class="overflow-x-auto rounded-lg border border-hairline bg-surface-raised">
            <table class="w-full min-w-[820px] text-left text-sm">
              <thead>
                <tr class="border-b border-hairline bg-surface-sunken text-caption text-neutral-500">
                  <th class="px-4 py-3 font-medium">标题</th>
                  <th class="px-4 py-3 font-medium">状态</th>
                  <th class="px-4 py-3 font-medium">题材</th>
                  <th class="px-4 py-3 font-medium">集数</th>
                  <th class="px-4 py-3 font-medium">预算（消耗 / 上限）</th>
                  <th class="px-4 py-3 font-medium">更新时间</th>
                  <th class="px-4 py-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="project in visibleProjects"
                  :key="project.id"
                  class="border-b border-hairline-soft transition-colors duration-gd-fast last:border-b-0 hover:bg-surface-canvas"
                >
                  <td class="max-w-[240px] px-4 py-3">
                    <button type="button" class="truncate font-medium text-neutral-900 hover:text-primary-600" @click="continueCreate(project)">
                      {{ project.title }}
                    </button>
                  </td>
                  <td class="px-4 py-3"><StatusTag :status="project.status" size="sm" /></td>
                  <td class="px-4 py-3 text-neutral-600">{{ project.genre || '—' }}</td>
                  <td class="gd-num px-4 py-3 text-neutral-600">{{ project.target_episode_count ?? '—' }}</td>
                  <td class="gd-num px-4 py-3 text-neutral-600">
                    <CreditAmountText :amount="project.spent_credits ?? 0" :colored="false" class="text-sm" />
                    /
                    <CreditAmountText :amount="project.budget_credits ?? 0" :colored="false" class="text-sm" />
                  </td>
                  <td class="px-4 py-3 text-xs text-neutral-400">{{ formatRelativeTime(project.updated_at) }}</td>
                  <td class="px-4 py-3 text-right">
                    <AppButton variant="text" size="sm" @click="continueCreate(project)">继续创作</AppButton>
                    <AppButton variant="text" size="sm" @click="openRename(project)">重命名</AppButton>
                    <AppButton v-if="project.status !== 'archived'" variant="text" size="sm" @click="openArchive(project)">归档</AppButton>
                    <AppButton
                      v-else
                      variant="text"
                      size="sm"
                      :disabled="restoringId === project.id"
                      :title="'恢复归档后可继续创作'"
                      @click="restoreArchived(project)"
                    >
                      {{ restoringId === project.id ? '恢复中…' : '恢复归档' }}
                    </AppButton>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- ============ 分页条（历史展开或列表视图时出现） ============ -->
          <nav v-if="(libraryExpanded || viewMode === 'list') && totalPages > 1" class="flex items-center justify-center gap-1.5" aria-label="分页">
            <AppButton variant="secondary" size="sm" :disabled="page <= 1" @click="goPage(page - 1)">上一页</AppButton>
            <template v-for="(item, index) in pageItems" :key="`${item}-${index}`">
              <span v-if="item === '…'" class="px-1 text-xs text-neutral-400">…</span>
              <button
                v-else
                type="button"
                class="gd-num h-7 min-w-7 rounded-md border px-2 text-xs transition-[background-color,border-color,color] duration-gd-fast ease-gd-standard"
                :class="item === page ? 'border-primary-500 gd-fill-brand' : 'border-hairline bg-surface-raised text-neutral-600 hover:bg-surface-canvas'"
                :aria-current="item === page ? 'page' : undefined"
                @click="goPage(item)"
              >
                {{ item }}
              </button>
            </template>
            <AppButton variant="secondary" size="sm" :disabled="page >= totalPages" @click="goPage(page + 1)">下一页</AppButton>
            <span class="gd-num ml-3 text-xs text-neutral-400">共 {{ loading ? '—' : total }} 个项目</span>
          </nav>
        </div>
      </div>
    </template>

    <!-- ============ 重命名弹窗 ============ -->
    <Modal v-model="renameDialogOpen" title="重命名项目" :max-width="420">
      <label for="rename-input" class="mb-2 block text-caption text-neutral-600">项目名（≤30 字）</label>
      <input
        id="rename-input"
        v-model="renameTitle"
        type="text"
        maxlength="30"
        class="gd-input"
        @keyup.enter="confirmRename"
      />
      <template #footer>
        <AppButton variant="secondary" size="md" :disabled="renameSaving" @click="renameTarget = null">取消</AppButton>
        <AppButton variant="primary" size="md" :loading="renameSaving" @click="confirmRename">保存</AppButton>
      </template>
    </Modal>

    <!-- ============ 归档二次确认 ============ -->
    <ConfirmDialog
      v-model="archiveDialogOpen"
      title="确认归档该项目？"
      :content="`归档后「${archiveTarget?.title ?? ''}」将进入终态，不可再继续创作。`"
      confirm-label="确认归档"
      :loading="archiveSaving"
      @confirm="confirmArchive"
      @cancel="archiveTarget = null"
    />
  </div>
</template>

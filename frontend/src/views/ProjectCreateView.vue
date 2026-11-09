<!--
  文件级注释：P04 · 创建项目四步向导（路由 /projects/new，AppShell 布局）。
  v7 纸上放映厅 Pro · 对照 F2-01（P04 创建项目向导）：
  - 页面容器 mx-auto max-w-content p-6 lg:p-8，刊头改用 PageHeader（衬线大标题 + lede + 发丝线左端 24px 紫刻度）；
  - 顶部步骤条改为 26px 圆形 step-dot：完成 = 金雾底金字（gold-fog / gold-deep / gold-light 描边）、
    当前 = 紫底白字（shadow-gd-1）、待处理 = sunken 灰底发丝描边；连接线 hairline、完成段 bg-gold；
  - 每步配衬线节标题（font-serif）；
  - 题材 / 画风选择升级为「模板卡」形态：暗底渐变封面缩略（.gd-cv-cover + .gd-cv-N）+ 已选紫描边
    + 题材紫字 meta + 描述文案，交互仍是单选 button（aria-pressed 保留）；
  - 第 4 步「预估总成本」数据着色改暖金（gold-deep），预算超余额警示条保留；
  - 功能逻辑（校验 / 提交 / 幂等键 / 余额拉取 / 预填偏好）一律未动。
  效果图中未落地的结构（剧本来源 SegmentedControl / creative_text 大文本域 / 解析预检卡）
  依赖新的后端能力（upload / precheck API），见重构报告。
-->
<script setup lang="ts">
/**
 * ProjectCreateView 创建项目向导。
 * props：无。emits：无。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { getCreditAccount } from '@/api/billing'
import { createProject } from '@/api/projects'
import type { ProjectCreatePayload } from '@/api/projects'
import type { CreditAccount } from '@/api/types'
import AppButton from '@/components/common/AppButton.vue'
import CreditAmountText from '@/components/common/CreditAmountText.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useToast } from '@/composables/useToast'
import { ASPECT_RATIO_OPTIONS, CREDITS_PER_MINUTE, GENRE_OPTIONS, STYLE_OPTIONS } from '@/constants/options'
import { useAuthStore } from '@/stores/auth'

/** 路由实例（取消 / 提交后跳转） */
const router = useRouter()
/** 当前路由（接收工作台灵感 / 题材快捷创建的预填参数） */
const route = useRoute()
/** Toast */
const toast = useToast()
/** 认证 store（读取引导偏好做预填） */
const authStore = useAuthStore()

/** 步骤定义 */
const STEPS = ['基本信息', '题材', '画风', '篇幅与预算'] as const

/** 当前步骤（1 起） */
const currentStep = ref(1)
/** 提交中标记 */
const submitting = ref(false)

/* ---------- 模板卡视觉配置（对照 F2-01 六宫格：暗底渐变封面 + 紫字 meta + 描述） ---------- */

/** 题材模板卡封面（gd-cv-1~6 循环取用） */
function genreCover(index: number): string {
  return `gd-cv-${(index % 6) + 1}`
}

/** 画风模板卡封面（固定映射，保证同一画风永远同一款渐变） */
const STYLE_COVERS: Record<string, string> = {
  live_action: 'gd-cv-1',
  anime: 'gd-cv-2',
  chinese_style: 'gd-cv-6',
  three_d: 'gd-cv-5',
}

/** 题材卡描述文案（仅展示层） */
const GENRE_DESCRIPTIONS: Record<string, string> = {
  都市情感: '适合都市题材、情感冲突，节奏快、反转密，单集 800 字左右',
  悬疑逆袭: '悬念开局，层层剥笋，适合身份反转与复仇线',
  古装宫斗: '适合宫廷 / 江湖题材，权谋对峙，旁白占比高',
  科幻末世: '设定驱动，高概念钩子，适合末日求生与文明抉择',
  甜宠日常: '轻松治愈，糖点密集，适合日常向与CP互动',
  复仇逆袭: '憋屈开局、打脸收尾，适合扮猪吃虎流',
  重生穿越: '信息差碾压，金手指爽点前置，适合穿越重生流',
}

/** 画风卡描述文案（仅展示层） */
const STYLE_DESCRIPTIONS: Record<string, string> = {
  live_action: '电影质感，写实光影',
  anime: '日式动漫画风，色块明快',
  chinese_style: '水墨丹青，东方美学',
  three_d: '立体渲染，空间感强',
}

/* ---------- 工作台快捷创建种子（?inspiration= / ?genre=） ---------- */
/** 灵感原文（工作台「一句话灵感」输入） */
const inspirationSeed = typeof route.query.inspiration === 'string' ? route.query.inspiration.trim() : ''
/** 题材种子（工作台题材卡片跳转携带，校验后才采用） */
const genreSeed = typeof route.query.genre === 'string' ? route.query.genre.trim() : ''

/* ---------- 表单状态 ---------- */
/** 项目名（必填 ≤30 字；有灵感时预填前 30 字，避免用户再次输入） */
const title = ref(inspirationSeed ? inspirationSeed.slice(0, 30) : '')
/** 一句话简介（选填；有灵感时直接落为简介，后续用于生成剧本） */
const synopsis = ref(inspirationSeed)
/** 题材（单选；仅接受合法题材键） */
const genre = ref(GENRE_OPTIONS.some((item) => item.key === genreSeed) ? genreSeed : '')
/** 目标集数（1~30，滑块） */
const episodeCount = ref<number>(readSavedNumber('default_episode_count', 10))
/** 画风（单选 4 卡） */
const styleKey = ref<string>(readSavedString('default_style', ''))
/** 画幅（三选） */
const aspectRatio = ref<string>(readSavedString('default_aspect_ratio', '9:16'))
/** 项目预算（积分，0=不限） */
const budget = ref<number>(0)
/** 预算输入的原始文本（允许清空） */
const budgetInput = ref('0')
/** 可用余额账户（未加载为 null） */
const creditAccount = ref<CreditAccount | null>(null)
/** 余额加载中 */
const balanceLoading = ref(false)

/** 引导单集时长偏好（缺省 2 分钟，用于成本预估） */
const preferredDuration = computed(() => readSavedNumber('default_duration_minutes', 2))

/** 实时预估总成本（集数 × 单集时长 × 160 积分/分钟，粗略公式） */
const estimatedCost = computed(() => episodeCount.value * preferredDuration.value * CREDITS_PER_MINUTE)

/** 可用余额（积分） */
const availableCredits = computed(() => creditAccount.value?.available_credits ?? creditAccount.value?.balance_credits ?? 0)

/** 预算是否超出可用余额 */
const budgetExceedsBalance = computed(() => budget.value > availableCredits.value)

/** 当前步骤是否满足进入下一步 / 提交的前置条件（按钮禁用而非点击后报错） */
const canProceed = computed(() => {
  if (currentStep.value === 1) {
    const name = title.value.trim()
    return name.length > 0 && name.length <= 30
  }
  if (currentStep.value === 2) return genre.value !== ''
  if (currentStep.value === 3) return styleKey.value !== ''
  // 第 4 步：预算不得超出可用余额（0 表示不限预算）
  return !budgetExceedsBalance.value
})

/** 当前步骤不满足前置条件时的原因文案（按钮 title + 按钮旁提示） */
const proceedHint = computed(() => {
  if (currentStep.value === 1) {
    const name = title.value.trim()
    if (!name) return '请先填写项目名（1~30 字）'
    if (name.length > 30) return '项目名不能超过 30 字'
  }
  if (currentStep.value === 2 && !genre.value) return '请选择一个题材'
  if (currentStep.value === 3 && !styleKey.value) return '请选择一个画风'
  if (currentStep.value === STEPS.length && budgetExceedsBalance.value) {
    return '项目预算超出可用余额：请先充值或调低预算'
  }
  return ''
})

/**
 * 从用户偏好读取字符串键。
 * @param key 偏好键名
 * @param fallback 缺省值
 * @returns 偏好值
 */
function readSavedString(key: string, fallback: string): string {
  const value = authStore.user?.preferences?.[key]
  return typeof value === 'string' ? value : fallback
}

/**
 * 从用户偏好读取数值键（非法值回退缺省并夹取范围）。
 * @param key 偏好键名
 * @param fallback 缺省值
 * @returns 偏好值
 */
function readSavedNumber(key: string, fallback: number): number {
  const value = authStore.user?.preferences?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

/**
 * 拉取可用余额（GET /billing/credit-account）。
 * @returns 无
 */
async function fetchBalance(): Promise<void> {
  balanceLoading.value = true
  try {
    creditAccount.value = await getCreditAccount()
  } catch {
    // 拦截器已 toast；余额展示回退 0，不阻塞向导
  } finally {
    balanceLoading.value = false
  }
}

/**
 * 各步骤校验：通过则进入下一步；最后一步提交。
 * @returns 无
 */
function handleNext(): void {
  if (currentStep.value === 1) {
    const name = title.value.trim()
    if (!name) {
      toast.warning('请输入项目名')
      return
    }
    if (name.length > 30) {
      toast.warning('项目名不能超过 30 字')
      return
    }
  }
  if (currentStep.value === 2 && !genre.value) {
    toast.warning('请选择一个题材')
    return
  }
  if (currentStep.value === 3 && !styleKey.value) {
    toast.warning('请选择一个画风')
    return
  }
  if (currentStep.value === STEPS.length) {
    void handleSubmit()
    return
  }
  currentStep.value += 1
}

/**
 * 上一步。
 * @returns 无
 */
function handlePrev(): void {
  if (currentStep.value > 1) currentStep.value -= 1
}

/**
 * 取消向导返回项目列表。
 * @returns 无
 */
function handleCancel(): void {
  void router.push('/projects')
}

/**
 * 跳转充值（计费中心）。
 * @returns 无
 */
function goBilling(): void {
  void router.push('/billing')
}

/**
 * 预算输入变化：夹取非负整数并同步展示值。
 * @returns 无
 */
function onBudgetInput(): void {
  const parsed = Math.max(0, Math.floor(Number(budgetInput.value) || 0))
  budget.value = Math.min(parsed, 99_999_999)
  budgetInput.value = String(budget.value)
}

/**
 * 提交创建（POST /projects，幂等键由 http 拦截器自动注入）。
 * @returns 无
 */
async function handleSubmit(): Promise<void> {
  submitting.value = true
  try {
    const payload: ProjectCreatePayload = {
      title: title.value.trim(),
      genre: genre.value,
      style_preset_key: (styleKey.value || 'live_action') as ProjectCreatePayload['style_preset_key'],
      aspect_ratio: (aspectRatio.value || '9:16') as ProjectCreatePayload['aspect_ratio'],
      target_episode_count: episodeCount.value,
      budget_credits: budget.value,
    }
    if (synopsis.value.trim()) payload.synopsis = synopsis.value.trim()
    const project = await createProject(payload)
    toast.success(`项目「${project.title}」创建成功`)
    await router.replace(`/projects/${project.id}/overview`)
  } catch {
    // 拦截器已 toast（30002 预算超限 / 40005 集数超限等）
  } finally {
    submitting.value = false
  }
}

// 挂载：拉取余额供第 4 步对比
onMounted(() => {
  void fetchBalance()
})

/** 步骤点状态类（对照效果图 step-dot：完成金雾 / 当前紫 / 待处理 sunken 灰） */
function stepDotClass(index: number): string {
  if (index + 1 < currentStep.value) return 'bg-gold-fog text-gold-deep border border-gold-light'
  if (index + 1 === currentStep.value) return 'gd-fill-brand border border-primary-500 shadow-gd-1'
  return 'bg-surface-sunken text-neutral-500 border border-hairline'
}

/** 步骤连接线状态类（完成段转金） */
function stepLineClass(index: number): string {
  return index + 1 < currentStep.value ? 'bg-gold' : 'bg-hairline'
}

/** 各步衬线节标题（对照 F2-01 节标题排印） */
const STEP_HEADINGS: Record<number, string> = {
  1: '起个名字，一句话讲清你的故事',
  2: '选择题材模板，定下故事底盘',
  3: '选择画风，定下成片质感',
  4: '篇幅与预算，预估创作成本',
}
</script>

<template>
  <div class="mx-auto max-w-content p-6 lg:p-8">
    <!-- ============ 刊头（统一光条 + 28px 衬线标题 | 取消按钮） ============ -->
    <PageHeader title="新建项目">
      <template #actions>
        <AppButton variant="secondary" size="md" :disabled="submitting" @click="handleCancel">取消</AppButton>
      </template>
    </PageHeader>

    <!-- ============ 步骤条：26px 圆形 step-dot + 发丝连接线（完成段转金） ============ -->
    <ol class="mb-8 flex items-center gap-4" aria-label="创建步骤">
      <li v-for="(stepTitle, index) in STEPS" :key="stepTitle" class="flex flex-1 items-center gap-3 last:flex-none">
        <div class="flex items-center gap-2.5">
          <span
            class="gd-num flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-full text-xs transition-colors duration-gd-normal"
            :class="stepDotClass(index)"
            aria-hidden="true"
          >
            <svg v-if="index + 1 < currentStep" class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <template v-else>{{ index + 1 }}</template>
          </span>
          <span
            class="hidden text-sm sm:inline"
            :class="index + 1 === currentStep ? 'font-semibold text-neutral-900' : index + 1 < currentStep ? 'text-gold-deep' : 'text-neutral-500'"
          >
            {{ stepTitle }}
          </span>
        </div>
        <div v-if="index < STEPS.length - 1" class="h-px flex-1 transition-colors duration-gd-normal" :class="stepLineClass(index)" aria-hidden="true" />
      </li>
    </ol>

    <!-- ============ 衬线节标题 ============ -->
    <h2 class="mb-5 font-serif text-display-lg text-neutral-900">{{ STEP_HEADINGS[currentStep] }}</h2>

    <!-- ============ 步骤 1：基本信息 ============ -->
    <section v-if="currentStep === 1" class="gd-card p-6 lg:p-8">
      <label for="create-title" class="block text-caption font-medium text-neutral-600">
        项目名<span class="text-danger">*</span>
      </label>
      <input
        id="create-title"
        v-model="title"
        type="text"
        maxlength="30"
        placeholder="给项目起个名字，如「重生之都市之光」"
        class="mt-2 gd-input"
      />
      <p class="gd-num mt-1.5 text-xs" :class="title.length > 30 ? 'text-danger' : 'text-neutral-400'">
        {{ title.length }}/30 字
      </p>

      <label for="create-synopsis" class="mt-6 block text-caption font-medium text-neutral-600">
        一句话简介<span class="text-neutral-400">（选填）</span>
      </label>
      <textarea
        id="create-synopsis"
        v-model="synopsis"
        rows="3"
        maxlength="200"
        placeholder="用一句话描述你的故事创意，AI 将据此生成剧本"
        class="mt-2 gd-input"
      />
      <p class="gd-num mt-1.5 text-xs text-neutral-400">{{ synopsis.length }}/200 字</p>
    </section>

    <!-- ============ 步骤 2：题材（模板卡形态，对照 F2-01 六宫格） ============ -->
    <section v-else-if="currentStep === 2" class="gd-card p-6 lg:p-8">
      <h3 class="gd-section-label">题材<span class="text-danger">*</span><span class="font-normal text-neutral-400">（单选）</span></h3>
      <div class="mt-4 grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-3">
        <button
          v-for="(item, index) in GENRE_OPTIONS"
          :key="item.key"
          type="button"
          class="gd-card-interactive rounded-lg border p-4 text-left transition-[border-color,box-shadow,transform] duration-gd-normal ease-gd-standard"
          :class="genre === item.key ? 'border-primary-500 shadow-gd-1 ring-1 ring-primary-500' : 'border-hairline bg-surface-raised hover:border-primary-300'"
          :aria-pressed="genre === item.key"
          @click="genre = item.key"
        >
          <!-- 暗底渐变封面缩略 -->
          <span class="gd-cv-cover block h-20 w-full overflow-hidden rounded-md" :class="genreCover(index)" aria-hidden="true" />
          <span class="mt-3 block font-medium text-neutral-900">{{ item.label }}</span>
          <span class="mt-1 block text-xs font-medium text-primary-600">
            {{ item.emoji }} {{ item.label }} · 短剧模板
          </span>
          <span class="mt-1.5 block text-xs leading-relaxed text-neutral-500">
            {{ GENRE_DESCRIPTIONS[item.key] || '适合该题材的通用节奏模板' }}
          </span>
        </button>
      </div>

      <h3 class="gd-section-label mt-8">目标集数</h3>
      <div class="mt-3 flex items-center gap-4">
        <input
          v-model.number="episodeCount"
          type="range"
          min="1"
          max="30"
          step="1"
          class="h-1.5 flex-1 cursor-pointer appearance-none rounded-full bg-surface-inset accent-[color:var(--gd-primary-500)]"
          aria-label="目标集数滑块"
        />
        <span class="gd-num w-16 shrink-0 rounded-md bg-primary-50 py-1 text-center text-sm font-semibold text-primary-700">
          {{ episodeCount }} 集
        </span>
      </div>
      <p class="mt-1.5 text-xs text-neutral-400">范围 1~30 集，创建后可在项目设置中调整</p>
    </section>

    <!-- ============ 步骤 3：画风与画幅（画风模板卡 + 画幅胶囊） ============ -->
    <section v-else-if="currentStep === 3" class="gd-card p-6 lg:p-8">
      <h3 class="gd-section-label">画风<span class="text-danger">*</span></h3>
      <div class="mt-4 grid grid-cols-1 gap-5 sm:grid-cols-2 lg:grid-cols-4">
        <button
          v-for="item in STYLE_OPTIONS"
          :key="item.key"
          type="button"
          class="gd-card-interactive rounded-lg border p-4 text-left transition-[border-color,box-shadow,transform] duration-gd-normal ease-gd-standard"
          :class="styleKey === item.key ? 'border-primary-500 shadow-gd-1 ring-1 ring-primary-500' : 'border-hairline bg-surface-raised hover:border-primary-300'"
          :aria-pressed="styleKey === item.key"
          @click="styleKey = item.key"
        >
          <span class="gd-cv-cover block h-20 w-full overflow-hidden rounded-md" :class="STYLE_COVERS[item.key] || 'gd-cv-1'" aria-hidden="true" />
          <span class="mt-3 block font-medium text-neutral-900">{{ item.label }}</span>
          <span class="mt-1 block text-xs font-medium text-primary-600">{{ item.emoji }} {{ item.label }}质感</span>
          <span class="mt-1.5 block text-xs leading-relaxed text-neutral-500">{{ STYLE_DESCRIPTIONS[item.key] }}</span>
        </button>
      </div>

      <h3 class="gd-section-label mt-8">画幅</h3>
      <div class="mt-3 flex flex-wrap gap-3">
        <button
          v-for="item in ASPECT_RATIO_OPTIONS"
          :key="item.key"
          type="button"
          class="inline-flex items-center gap-2 rounded-full border px-5 py-2.5 text-sm transition-[background-color,border-color,color] duration-gd-normal ease-gd-standard"
          :class="
            aspectRatio === item.key
              ? 'border-primary-500 bg-primary-50 font-medium text-primary-700'
              : 'border-hairline bg-surface-raised text-neutral-600 hover:border-primary-300'
          "
          :aria-pressed="aspectRatio === item.key"
          @click="aspectRatio = item.key"
        >
          <span aria-hidden="true">{{ item.emoji }}</span>
          {{ item.label }}
        </button>
      </div>
    </section>

    <!-- ============ 步骤 4：篇幅与预算（预估数据暖金着色，对照 F2-01 底部预估积分） ============ -->
    <section v-else class="gd-card p-6 lg:p-8">
      <!-- 预算输入 -->
      <h3 class="gd-section-label">项目预算（积分）</h3>
      <div class="mt-3 flex items-center gap-3">
        <div class="relative w-48">
          <input
            v-model="budgetInput"
            type="number"
            min="0"
            class="gd-input gd-num pr-12"
            :class="budgetExceedsBalance ? 'gd-field-invalid' : ''"
            aria-label="项目预算（积分）"
            @input="onBudgetInput"
          />
          <span class="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-neutral-400">积分</span>
        </div>
        <span class="text-sm text-neutral-500">
          当前输入：
          <CreditAmountText :amount="budget" :colored="false" />
          <template v-if="budget === 0">（0 表示不限预算）</template>
        </span>
      </div>

      <!-- 余额 vs 预算 对比行 -->
      <div class="mt-6 rounded-lg border border-hairline bg-surface-canvas p-5">
        <div class="flex items-center justify-between py-2">
          <span class="text-sm text-neutral-500">可用余额</span>
          <span class="flex items-baseline gap-1">
            <CreditAmountText :amount="availableCredits" :colored="false" />
            <span class="text-xs text-neutral-400">积分</span>
            <span v-if="balanceLoading" class="ml-1 text-xs text-neutral-400">加载中…</span>
          </span>
        </div>
        <div class="flex items-center justify-between border-t border-hairline py-2">
          <span class="text-sm text-neutral-500">项目预算</span>
          <span class="flex items-baseline gap-1">
            <CreditAmountText :amount="budget" :colored="budgetExceedsBalance" />
            <span class="text-xs text-neutral-400">积分</span>
          </span>
        </div>
        <div class="flex items-center justify-between border-t border-hairline py-2">
          <span class="text-sm text-neutral-500">预估总成本</span>
          <span class="flex items-baseline gap-1">
            <!-- 预估数据暖金着色（对照 F2-01「预估积分」金字） -->
            <span class="gd-num text-base font-semibold text-gold-deep">{{ estimatedCost.toLocaleString('zh-CN') }}</span>
            <span class="text-xs text-gold-deep">积分</span>
          </span>
        </div>
        <p class="mt-1 text-xs text-neutral-400">
          按 {{ episodeCount }} 集 × {{ preferredDuration }} 分钟 × {{ CREDITS_PER_MINUTE }} 积分/分钟粗略估算；建议预算 ≥ 预估成本，草稿模式实际消耗更低。
        </p>

        <!-- 预算超余额内嵌警示 -->
        <div v-if="budgetExceedsBalance" class="mt-4 flex items-start gap-3 rounded-lg border border-warning-border bg-warning-bg p-4" role="alert">
          <svg class="mt-0.5 h-5 w-5 shrink-0 text-warning" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path
              d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium text-neutral-800">预算超出可用余额</p>
            <p class="mt-0.5 text-xs leading-relaxed text-neutral-500">
              预算 {{ budget.toLocaleString('zh-CN') }} 积分，当前可用 {{ availableCredits.toLocaleString('zh-CN') }} 积分。
              服务端将拒绝创建预算超出可用余额的项目，请先充值或调低预算。
            </p>
            <AppButton variant="primary" size="sm" class="mt-3" @click="goBilling">去充值</AppButton>
          </div>
        </div>
      </div>
    </section>

    <!-- ============ 底部导航（sticky：窄窗口/小屏下始终可见，避免需滚动才能点） ============ -->
    <div class="sticky bottom-0 z-20 mt-8 flex items-center justify-between gap-4 border-t border-hairline bg-surface-canvas/95 py-4 backdrop-blur">
      <AppButton
        variant="secondary"
        size="md"
        :disabled="currentStep === 1 || submitting"
        :title="currentStep === 1 ? '已在第一步' : '返回上一步修改设定'"
        @click="handlePrev"
      >
        上一步
      </AppButton>
      <div class="flex min-w-0 flex-1 items-center justify-end gap-3">
        <!-- 前置条件未满足时给出可读原因，按钮同时禁用，避免点击后才报错 -->
        <span v-if="proceedHint" class="min-w-0 truncate text-caption text-neutral-500" role="status">
          {{ proceedHint }}
        </span>
        <AppButton
          variant="primary"
          size="lg"
          :loading="submitting"
          :disabled="!canProceed || submitting"
          :title="proceedHint || (currentStep === STEPS.length ? '按当前设定创建项目' : '进入下一步')"
          @click="handleNext"
        >
          {{ currentStep === STEPS.length ? '创建项目 →' : '下一步' }}
        </AppButton>
      </div>
    </div>
  </div>
</template>

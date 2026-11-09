<!--
  文件级注释：P02 · 新手引导三步向导（路由 /onboarding，bare 布局）。
  v7 纸上放映厅 Pro · 无专属效果图，按同一设计语言轻量对齐：
  衬线刊头（font-serif masthead-lg）+ 白纸卡步骤（gd-card）+ 紫行动色（primary 主按钮）；
  步骤点对齐设计系统 step-dot 三态：当前紫实底 / 已完成金雾底（gold-fog）/ 未到灰描边。
  步骤：1 题材偏好（多选 ≥1）→ 2 默认画风（单选 4 卡）→ 3 默认篇幅
  （画幅三选 + 集数步进器 1~30 + 单集时长 1/2/3 分钟 + 实时预估成本）。
  完成或跳过均调 PATCH /users/me 写入 preferences（服务端 JSONB 浅合并）后跳 /projects。
  顶部步骤条 + 进度百分比 + 右上跳过；每步底部上一步/下一步，原生控件保证键盘可用。
-->
<script setup lang="ts">
/**
 * OnboardingView 新手引导。
 * props：无。emits：无。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import { updateMe } from '@/api/auth'
import AppButton from '@/components/common/AppButton.vue'
import CreditAmountText from '@/components/common/CreditAmountText.vue'
import { useToast } from '@/composables/useToast'
import { ASPECT_RATIO_OPTIONS, CREDITS_PER_MINUTE, DURATION_OPTIONS, GENRE_OPTIONS, STYLE_OPTIONS } from '@/constants/options'
import { useAuthStore } from '@/stores/auth'

/** 路由实例（完成后跳转） */
const router = useRouter()
/** Toast */
const toast = useToast()
/** 认证 store（读取已保存偏好做预填） */
const authStore = useAuthStore()

/** 步骤定义（1 题材偏好 / 2 默认画风 / 3 默认篇幅） */
const STEPS = [
  { title: '题材偏好', hint: '选出你感兴趣的题材，帮助我们为你定制创作建议' },
  { title: '默认画风', hint: '选择一个默认画风，创建项目时将自动带入' },
  { title: '默认篇幅', hint: '设置常用画幅与篇幅，作为新项目的默认配置' },
] as const

/** 当前步骤（1 起） */
const currentStep = ref(1)
/** 已选题材（多选，≥1 才能下一步） */
const favoriteGenres = ref<string[]>(readSavedGenres())
/** 已选画风（单选） */
const defaultStyle = ref<string>(readSaved('default_style', ''))
/** 已选画幅（单选） */
const defaultAspectRatio = ref<string>(readSaved('default_aspect_ratio', '9:16'))
/** 目标集数（1~30） */
const defaultEpisodeCount = ref<number>(readSaved('default_episode_count', 10))
/** 单集时长（分钟，1/2/3） */
const defaultDurationMinutes = ref<number>(readSaved('default_duration_minutes', 2))
/** 提交中标记 */
const submitting = ref(false)

/** 进度百分比（完成度，第 3 步提交前为 2/3） */
const progressPercent = computed(() => Math.round(((currentStep.value - 1) / STEPS.length) * 100))

/** 实时预估总成本（粗略公式：集数 × 单集时长 × 160 积分/分钟） */
const estimatedCost = computed(() => defaultEpisodeCount.value * defaultDurationMinutes.value * CREDITS_PER_MINUTE)

/** 第 1 步是否可进入下一步（至少选 1 个题材） */
const canNextFromStep1 = computed(() => favoriteGenres.value.length >= 1)

/**
 * 从已保存的用户偏好读取指定键（登录后重新进入引导时预填）。
 * @param key 偏好键名
 * @param fallback 缺省值
 * @returns 偏好值
 */
function readSaved<T extends string | number>(key: string, fallback: T): T {
  const prefs = authStore.user?.preferences
  const value = prefs?.[key]
  return (typeof value === typeof fallback ? (value as T) : fallback)
}

/**
 * 从已保存偏好读取题材数组。
 * @returns 题材字符串数组（非法输入回退空数组）
 */
function readSavedGenres(): string[] {
  const saved = authStore.user?.preferences?.favorite_genres
  return Array.isArray(saved) ? (saved as string[]) : []
}

/**
 * 切换题材选中态（多选）。
 * @param genre 题材名
 * @returns 无
 */
function toggleGenre(genre: string): void {
  const index = favoriteGenres.value.indexOf(genre)
  if (index >= 0) {
    favoriteGenres.value.splice(index, 1)
  } else {
    favoriteGenres.value.push(genre)
  }
}

/**
 * 集数步进：加 / 减并夹取 1~30。
 * @param delta 步进增量（+1 / -1）
 * @returns 无
 */
function stepEpisode(delta: number): void {
  defaultEpisodeCount.value = Math.min(30, Math.max(1, defaultEpisodeCount.value + delta))
}

/**
 * 跳过引导：仍写入 onboarding_done 避免下次登录重复进入，然后跳项目工作台。
 * @returns 无
 */
async function handleSkip(): Promise<void> {
  if (submitting.value) return
  submitting.value = true
  try {
    await updateMe({ preferences: { onboarding_done: true } })
    toast.info('已跳过新手引导，可随时在个人设置中补充偏好')
    await router.replace('/projects')
  } catch {
    // 拦截器已 toast
  } finally {
    submitting.value = false
  }
}

/**
 * 下一步：第 1 步校验题材 ≥1；第 3 步直接提交完成。
 * @returns 无
 */
function handleNext(): void {
  if (currentStep.value === 1 && !canNextFromStep1.value) {
    toast.warning('请至少选择 1 个感兴趣的题材')
    return
  }
  if (currentStep.value === STEPS.length) {
    void handleFinish()
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
 * 完成引导：PATCH /users/me 写入全部偏好后跳项目工作台。
 * @returns 无
 */
async function handleFinish(): Promise<void> {
  submitting.value = true
  try {
    await updateMe({
      preferences: {
        onboarding_done: true,
        favorite_genres: favoriteGenres.value,
        default_style: defaultStyle.value,
        default_aspect_ratio: defaultAspectRatio.value,
        default_episode_count: defaultEpisodeCount.value,
        default_duration_minutes: defaultDurationMinutes.value,
      },
    })
    // 同步本地用户偏好（服务端已浅合并，本地立即生效避免再拉取）
    if (authStore.user) {
      authStore.user.preferences = {
        ...(authStore.user.preferences ?? {}),
        onboarding_done: true,
        favorite_genres: favoriteGenres.value,
        default_style: defaultStyle.value,
        default_aspect_ratio: defaultAspectRatio.value,
        default_episode_count: defaultEpisodeCount.value,
        default_duration_minutes: defaultDurationMinutes.value,
      }
    }
    toast.success('偏好已保存，开始你的第一部短剧吧')
    await router.replace('/projects')
  } catch {
    // 拦截器已 toast
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="flex min-h-screen flex-col bg-surface-canvas">
    <!-- ============ 顶部：步骤条 + 进度 + 跳过 ============ -->
    <header class="border-b border-hairline bg-surface-raised">
      <div class="mx-auto flex max-w-content-sm items-center justify-between gap-4 px-6 py-4">
        <ol class="flex flex-1 items-center gap-3 sm:gap-5">
          <li v-for="(step, index) in STEPS" :key="step.title" class="flex items-center gap-2">
            <!-- 步骤点三态（step-dot：当前紫实底 / 已完成金雾 / 未到灰描边） -->
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full border-2 transition-colors duration-gd-normal"
              :class="
                index + 1 < currentStep
                  ? 'border-gold-light bg-gold-fog text-gold-deep'
                  : index + 1 === currentStep
                    ? 'border-primary-500 gd-fill-brand'
                    : 'border-hairline-strong'
              "
              aria-hidden="true"
            >
              <svg v-if="index + 1 < currentStep" class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
            <span
              class="hidden text-sm sm:inline"
              :class="index + 1 === currentStep ? 'font-semibold text-neutral-900' : 'text-neutral-500'"
            >
              {{ step.title }}
            </span>
            <span v-if="index < STEPS.length - 1" class="h-px w-6 sm:w-10" :class="index + 1 < currentStep ? 'bg-gold' : 'bg-hairline'" aria-hidden="true" />
          </li>
        </ol>
        <div class="flex shrink-0 items-center gap-4">
          <span class="gd-num text-xs text-neutral-400" aria-live="polite">已完成 {{ progressPercent }}%</span>
          <AppButton variant="ghost" size="sm" :disabled="submitting" @click="handleSkip">跳过</AppButton>
        </div>
      </div>
      <!-- 进度条 -->
      <div class="h-0.5 w-full bg-surface-sunken">
        <div class="h-full bg-primary-500 transition-[width] duration-gd-slow" :style="{ width: `${progressPercent}%` }" />
      </div>
    </header>

    <!-- ============ 内容区 ============ -->
    <main class="mx-auto flex w-full max-w-content-sm flex-1 flex-col px-6 py-10">
      <!-- 刊眉（v7 设计语言对齐） -->
      <p class="gd-kicker mb-3 text-gold-deep">浮光造梦 · 新手引导</p>
      <!-- 第 1 步：题材偏好（多选） -->
      <section v-if="currentStep === 1">
        <h1 class="font-serif text-masthead-lg text-neutral-900">你喜欢看什么题材？</h1>
        <p class="gd-lede mt-3">{{ STEPS[0].hint }}</p>
        <div class="gd-card mt-8 p-6">
          <div class="flex flex-wrap gap-3">
            <button
              v-for="genre in GENRE_OPTIONS"
              :key="genre.key"
              type="button"
              class="inline-flex items-center gap-2 rounded-full border px-5 py-2.5 text-sm transition-[background-color,border-color,color] duration-gd-normal ease-gd-standard"
              :class="
                favoriteGenres.includes(genre.key)
                  ? 'border-primary-500 bg-primary-50 font-medium text-primary-700'
                  : 'border-hairline bg-surface-raised text-neutral-600 hover:border-primary-300 hover:text-primary-600'
              "
              :aria-pressed="favoriteGenres.includes(genre.key)"
              @click="toggleGenre(genre.key)"
            >
              <span v-if="genre.emoji" aria-hidden="true">{{ genre.emoji }}</span>
              {{ genre.label }}
            </button>
          </div>
          <p class="mt-4 text-xs text-neutral-400">已选 {{ favoriteGenres.length }} / 至少 1 项，可多选</p>
        </div>
      </section>

      <!-- 第 2 步：默认画风（单选 4 卡） -->
      <section v-else-if="currentStep === 2">
        <h1 class="font-serif text-masthead-lg text-neutral-900">选择你的默认画风</h1>
        <p class="gd-lede mt-3">{{ STEPS[1].hint }}</p>
        <div class="gd-card mt-8 p-6">
          <div class="grid gap-5 sm:grid-cols-2">
            <button
              v-for="style in STYLE_OPTIONS"
              :key="style.key"
              type="button"
              class="flex items-center gap-4 rounded-lg border p-4 text-left transition-[background-color,border-color,color] duration-gd-normal ease-gd-standard"
              :class="defaultStyle === style.key ? 'border-primary-500 bg-primary-50' : 'border-hairline bg-surface-raised hover:border-primary-300'"
              :aria-pressed="defaultStyle === style.key"
              @click="defaultStyle = style.key"
            >
              <span
                class="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl text-2xl transition-colors duration-gd-normal"
                :class="defaultStyle === style.key ? 'bg-primary-100' : 'bg-surface-sunken'"
                aria-hidden="true"
              >
                {{ style.emoji }}
              </span>
              <span class="min-w-0">
                <span class="block font-medium text-neutral-900">{{ style.label }}</span>
                <span class="mt-0.5 block text-xs text-neutral-400">
                  {{ { live_action: '电影质感，写实光影', anime: '日式动漫画风', chinese_style: '水墨丹青，东方美学', three_d: '立体渲染，空间感强' }[style.key] }}
                </span>
              </span>
              <span
                v-if="defaultStyle === style.key"
                class="ml-auto flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-[var(--gd-violet)] text-micro text-[#0b0a1e]"
                aria-hidden="true"
              >
                ✓
              </span>
            </button>
          </div>
        </div>
      </section>

      <!-- 第 3 步：默认篇幅 -->
      <section v-else>
        <h1 class="font-serif text-masthead-lg text-neutral-900">设置默认篇幅</h1>
        <p class="gd-lede mt-3">{{ STEPS[2].hint }}</p>

        <div class="gd-card mt-8 p-6">
          <!-- 画幅三选 -->
          <div>
            <h2 class="gd-section-label">画幅比例</h2>
            <div class="mt-3 flex flex-wrap gap-3">
              <button
                v-for="ratio in ASPECT_RATIO_OPTIONS"
                :key="ratio.key"
                type="button"
                class="inline-flex items-center gap-2 rounded-md border px-4 py-2.5 text-sm transition-[background-color,border-color,color] duration-gd-normal ease-gd-standard"
                :class="
                  defaultAspectRatio === ratio.key
                    ? 'border-primary-500 bg-primary-50 font-medium text-primary-700'
                    : 'border-hairline bg-surface-raised text-neutral-600 hover:border-primary-300'
                "
                :aria-pressed="defaultAspectRatio === ratio.key"
                @click="defaultAspectRatio = ratio.key"
              >
                <span aria-hidden="true">{{ ratio.emoji }}</span>
                {{ ratio.label }}
              </button>
            </div>
          </div>

          <!-- 集数步进器 + 单集时长 -->
          <div class="mt-8 grid gap-5 sm:grid-cols-2">
            <div>
              <h2 class="gd-section-label">目标集数</h2>
              <div class="mt-3 inline-flex items-center rounded-md border border-hairline bg-surface-raised">
                <button
                  type="button"
                  class="flex h-9 w-9 items-center justify-center text-lg text-neutral-500 transition-colors duration-gd-fast hover:bg-surface-canvas disabled:cursor-not-allowed disabled:opacity-40"
                  :disabled="defaultEpisodeCount <= 1"
                  aria-label="减少集数"
                  @click="stepEpisode(-1)"
                >
                  −
                </button>
                <input
                  v-model.number="defaultEpisodeCount"
                  type="number"
                  min="1"
                  max="30"
                  class="gd-num h-9 w-16 border-x border-hairline text-center text-sm text-neutral-800 outline-none [appearance:textfield] focus:border-primary-500 [&::-webkit-inner-spin-button]:appearance-none"
                  aria-label="目标集数"
                  @change="defaultEpisodeCount = Math.min(30, Math.max(1, Math.round(defaultEpisodeCount) || 1))"
                />
                <button
                  type="button"
                  class="flex h-9 w-9 items-center justify-center text-lg text-neutral-500 transition-colors duration-gd-fast hover:bg-surface-canvas disabled:cursor-not-allowed disabled:opacity-40"
                  :disabled="defaultEpisodeCount >= 30"
                  aria-label="增加集数"
                  @click="stepEpisode(1)"
                >
                  +
                </button>
              </div>
              <p class="mt-2 text-xs text-neutral-400">范围 1~30 集</p>
            </div>

            <div>
              <h2 class="gd-section-label">单集时长</h2>
              <div class="mt-3 flex gap-3">
                <button
                  v-for="minutes in DURATION_OPTIONS"
                  :key="minutes"
                  type="button"
                  class="rounded-md border px-4 py-2.5 text-sm transition-[background-color,border-color,color] duration-gd-normal ease-gd-standard"
                  :class="
                    defaultDurationMinutes === minutes
                      ? 'border-primary-500 bg-primary-50 font-medium text-primary-700'
                      : 'border-hairline bg-surface-raised text-neutral-600 hover:border-primary-300'
                  "
                  :aria-pressed="defaultDurationMinutes === minutes"
                  @click="defaultDurationMinutes = minutes"
                >
                  {{ minutes }} 分钟
                </button>
              </div>
            </div>
          </div>
        </div>

        <!-- 实时预估成本 -->
        <div class="mt-8 flex items-center justify-between rounded-lg border border-primary-200 bg-primary-50 p-6">
          <div>
            <p class="text-sm font-medium text-neutral-800">预估总成本</p>
            <p class="mt-0.5 text-xs text-neutral-500">
              {{ defaultEpisodeCount }} 集 × {{ defaultDurationMinutes }} 分钟 × {{ CREDITS_PER_MINUTE }} 积分/分钟（粗略估算，草稿模式实际消耗更低）
            </p>
          </div>
          <div class="flex items-baseline gap-1">
            <CreditAmountText :amount="estimatedCost" :colored="false" class="text-lg" />
            <span class="text-xs text-neutral-500">积分</span>
          </div>
        </div>
      </section>

      <!-- ============ 底部导航 ============ -->
      <div class="mt-8 flex items-center justify-between border-t border-hairline pt-6">
        <AppButton variant="secondary" size="md" :disabled="currentStep === 1 || submitting" @click="handlePrev">
          上一步
        </AppButton>
        <AppButton
          variant="primary"
          size="md"
          :loading="submitting && currentStep === STEPS.length"
          :disabled="currentStep === 1 && !canNextFromStep1"
          @click="handleNext"
        >
          {{ currentStep === STEPS.length ? '完成设置' : '下一步' }}
        </AppButton>
      </div>
    </main>
  </div>
</template>

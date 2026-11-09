<!--
  文件级注释：P22b · 个人设置（路由 /settings/profile，AppShell 布局）· v7 纸上放映厅 Pro · 对照 F1-04。
  左设置 Tab 200px（个人资料 / 创作偏好 / 通知偏好 / 安全设置 / 关于 + 快捷入口通知中心）
  + 右分区白纸卡。个人资料：头像展示（更换入口即将上线）+ 昵称 → PATCH /users/me
  + 手机号只读。创作偏好：回显 preferences（题材多选 / 画风 / 画幅 / 集数，与 P02 引导同构）
  → PATCH。通知偏好（对照 F1-04 开关行）：五条偏好行 × 紫色胶囊开关（自研 role=switch），
  开关即时保存至 preferences.notification_prefs（JSONB 浅合并），失败回滚。安全：修改密码
  表单（后端无接口 → 禁用态 +「即将上线」提示，禁止伪装可提交）+ 登录设备占位 +
  底部危险区（注销账号按钮禁用 +「即将上线」标识）。关于：版本号 / Swagger 外链 / 致谢。
  三态（LoadingSkeleton / ErrorRetryBlock / 内容）与表单控件 .gd-input 保留。
-->
<script setup lang="ts">
/**
 * ProfileSettingsView 个人设置。
 * props：无。emits：无。
 * 依赖 auth API（updateMe）、auth store（用户与偏好回显）、useToast、options 常量。
 */
import { computed, onMounted, ref, type Component } from 'vue'

import { updateMe } from '@/api/auth'
import AppButton from '@/components/common/AppButton.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useToast } from '@/composables/useToast'
import { ASPECT_RATIO_OPTIONS, GENRE_OPTIONS, STYLE_OPTIONS } from '@/constants/options'
import { useAuthStore } from '@/stores/auth'
import {
  PhBell,
  PhDeviceMobile,
  PhInfo,
  PhLockKey,
  PhMonitor,
  PhPalette,
  PhTray,
  PhUser,
} from '@phosphor-icons/vue'

/** Toast 提示 */
const toast = useToast()
/** 认证 store（当前用户 / 偏好回显） */
const authStore = useAuthStore()

/** 设置 Tab 定义（icon 为 Phosphor 矢量图标组件，替代 emoji） */
const TABS: Array<{ key: 'profile' | 'preference' | 'notify' | 'security' | 'about'; label: string; icon: Component }> = [
  { key: 'profile', label: '个人资料', icon: PhUser },
  { key: 'preference', label: '创作偏好', icon: PhPalette },
  { key: 'notify', label: '通知偏好', icon: PhBell },
  { key: 'security', label: '安全设置', icon: PhLockKey },
  { key: 'about', label: '关于', icon: PhInfo },
]

/** 当前激活 Tab */
const activeTab = ref<'profile' | 'preference' | 'notify' | 'security' | 'about'>('profile')

/* ==================== 个人资料 ==================== */

/** 昵称输入 */
const nickname = ref('')
/** 资料保存中 */
const savingProfile = ref(false)

/** 展示头像（账号已有头像，缺省回退首字符占位） */
const avatarDisplay = computed(() => authStore.user?.avatar_url || '')

/**
 * 保存个人资料（昵称 → PATCH /users/me）。
 * @returns 无
 */
async function saveProfile(): Promise<void> {
  const name = nickname.value.trim()
  if (!name) {
    toast.warning('昵称不能为空')
    return
  }
  if (name.length > 30) {
    toast.warning('昵称不能超过 30 字')
    return
  }
  savingProfile.value = true
  try {
    const { user } = await updateMe({ nickname: name })
    authStore.user = user
    toast.success('个人资料已保存')
  } catch {
    // 拦截器已 toast
  } finally {
    savingProfile.value = false
  }
}

/* ==================== 创作偏好（与 P02 引导同构） ==================== */

/** 选中的题材多选 */
const favoriteGenres = ref<string[]>([])
/** 默认画风 */
const defaultStyle = ref('live_action')
/** 默认画幅 */
const defaultAspectRatio = ref('9:16')
/** 默认集数 */
const defaultEpisodeCount = ref(10)
/** 偏好保存中 */
const savingPrefs = ref(false)

/**
 * 从用户 preferences 回显创作偏好表单。
 * @returns 无
 */
function hydratePrefs(): void {
  const prefs = authStore.user?.preferences ?? {}
  const genres = prefs.favorite_genres
  favoriteGenres.value = Array.isArray(genres) ? (genres as string[]) : []
  defaultStyle.value = typeof prefs.default_style === 'string' ? prefs.default_style : 'live_action'
  defaultAspectRatio.value = typeof prefs.default_aspect_ratio === 'string' ? prefs.default_aspect_ratio : '9:16'
  defaultEpisodeCount.value = typeof prefs.default_episode_count === 'number' ? prefs.default_episode_count : 10
}

/**
 * 切换题材选中态（多选）。
 * @param genre 题材名
 * @returns 无
 */
function toggleGenre(genre: string): void {
  favoriteGenres.value = favoriteGenres.value.includes(genre)
    ? favoriteGenres.value.filter((item) => item !== genre)
    : [...favoriteGenres.value, genre]
}

/**
 * 保存创作偏好（PATCH /users/me preferences 浅合并）。
 * @returns 无
 */
async function savePreferences(): Promise<void> {
  if (favoriteGenres.value.length === 0) {
    toast.warning('请至少选择一个题材偏好')
    return
  }
  savingPrefs.value = true
  try {
    const { user } = await updateMe({
      preferences: {
        favorite_genres: favoriteGenres.value,
        default_style: defaultStyle.value,
        default_aspect_ratio: defaultAspectRatio.value,
        default_episode_count: defaultEpisodeCount.value,
      },
    })
    authStore.user = user
    toast.success('创作偏好已保存')
  } catch {
    // 拦截器已 toast
  } finally {
    savingPrefs.value = false
  }
}

/* ==================== 通知偏好（对照 F1-04 开关行，存 preferences.notification_prefs） ==================== */

/** 通知偏好行定义（对照 F1-04 五条开关行） */
const NOTIFY_OPTIONS: Array<{ key: string; label: string; desc: string }> = [
  { key: 'task_done', label: '任务完成通知', desc: '渲染任务、分镜批次完成后提醒我' },
  { key: 'approval', label: '关口审批提醒', desc: '关口关卡等待确认时提醒我' },
  { key: 'credit_low', label: '积分余额预警', desc: '可用余额低于阈值时提醒充值' },
  { key: 'team_activity', label: '团队动态', desc: '成员加入、移除与角色变更' },
  { key: 'marketing', label: '营销与活动', desc: '新功能上线、积分活动推送' },
]

/** 通知偏好开关态（key → 是否开启；缺省前四开、营销关，对齐效果图默认态） */
const notifyPrefs = ref<Record<string, boolean>>({})

/** 切换保存进行中的 key（避免并发 PUT） */
const notifyingKey = ref('')

/**
 * 从用户 preferences 回显通知偏好开关。
 * @returns 无
 */
function hydrateNotifyPrefs(): void {
  const stored = authStore.user?.preferences?.notification_prefs
  const base: Record<string, boolean> = {}
  for (const option of NOTIFY_OPTIONS) {
    base[option.key] =
      typeof stored === 'object' && stored !== null && !Array.isArray(stored)
        ? (stored as Record<string, unknown>)[option.key] === true
        : option.key !== 'marketing'
  }
  notifyPrefs.value = base
}

/**
 * 切换通知偏好开关：乐观更新 + 即时保存（PATCH /users/me preferences 浅合并），失败回滚。
 * @param key 偏好键
 * @returns 无
 */
async function toggleNotify(key: string): Promise<void> {
  if (notifyingKey.value) return
  notifyingKey.value = key
  const prev = notifyPrefs.value
  const next = { ...prev, [key]: !prev[key] }
  notifyPrefs.value = next
  try {
    const { user } = await updateMe({ preferences: { notification_prefs: next } })
    authStore.user = user
    toast.success('通知偏好已保存')
  } catch {
    notifyPrefs.value = prev
    // 拦截器已 toast
  } finally {
    notifyingKey.value = ''
  }
}

/* ==================== 安全（后端未开放，降级为只读 + 即将上线） ==================== */

/** 旧密码 */
const oldPassword = ref('')
/** 新密码 */
const newPassword = ref('')
/** 确认新密码 */
const confirmPassword = ref('')

/* 修改密码：后端接口未开放（users/auth 域仅有 PATCH /users/me），
   表单以禁用态 + “即将上线”标识降级，不再提供可点击但无实际作用的提交按钮。 */

/** 登录设备占位数据（icon 为 Phosphor 矢量图标组件） */
const DEVICES: Array<{ name: string; meta: string; current: boolean; icon: Component }> = [
  { name: 'Chrome · Windows', meta: '本机 · 最近活跃', current: true, icon: PhMonitor },
  { name: '移动端 App', meta: '3 天前登录', current: false, icon: PhDeviceMobile },
]

/* ==================== 危险区：注销账号（对照 F1-04 底部粉底块） ====================
 * 后端未提供账号注销接口：按钮以禁用态 + “即将上线”标识降级，
 * 不再弹出二次确认后仅给一句“操作未执行”的空反馈。 */

/* ==================== 关于 ==================== */

/** 前端版本号（取自 package.json version 常量维护） */
const APP_VERSION = '0.1.0'
/** 后端 Swagger 文档地址 */
const DOCS_URL = 'http://127.0.0.1:8100/docs'

/* ==================== 首屏加载三态 ==================== */

/**
 * 首屏加载中。
 *
 * 本地优先原则：`authStore.user` 由登录时写入 localStorage（`gd_user`），
 * 路由守卫已保证进入本页时必然已登录，因此**有缓存就不该让用户看骨架**。
 * 仅当缓存缺失（如直接粘贴 URL 冷启动）时才需要真实拉取。
 */
const loading = ref(!authStore.user)
/** 加载失败信息（空串表示正常）；仅在「无缓存 + 拉取失败」时置值，用于整页重试 */
const errorMsg = ref('')

/**
 * 用当前 store 中的用户信息回显表单。
 * @returns 无
 */
function hydrateFromStore(): void {
  nickname.value = authStore.user?.nickname ?? ''
  hydratePrefs()
  hydrateNotifyPrefs()
}

/**
 * 拉取最新用户信息并回显表单（骨架 / 失败重试 / 内容三态）。
 *
 * 分支策略：
 * - 本地已有用户 → 立即用缓存渲染（保持重构前的即时渲染行为），再后台静默刷新；
 * - 本地无缓存 → 骨架 + 拉取，失败才整页拦截（ErrorRetryBlock）。
 *
 * @returns 无
 */
async function loadProfile(): Promise<void> {
  // 快路径：有缓存 → 先渲染，后刷新
  if (authStore.user) {
    hydrateFromStore()
    loading.value = false
    void refreshProfile()
    return
  }

  // 慢路径：无缓存 → 骨架兜底
  loading.value = true
  errorMsg.value = ''
  try {
    await authStore.fetchMe()
    hydrateFromStore()
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 后台静默刷新用户信息。
 *
 * 失败不阻断使用：保留本地缓存内容渲染，仅以 toast 轻提示。
 * @returns 无
 */
async function refreshProfile(): Promise<void> {
  try {
    await authStore.fetchMe()
    hydrateFromStore()
  } catch {
    toast.warning('资料刷新失败，当前展示本地缓存内容')
  }
}

// 挂载：拉取用户信息并回显昵称与偏好
onMounted(() => {
  void loadProfile()
})
</script>

<template>
  <div class="mx-auto max-w-content-lg p-6 lg:p-8">
    <!-- ============ 刊头（masthead） ============ -->
    <PageHeader title="个人设置" />

    <!-- ============ 首屏加载骨架 ============ -->
    <div v-if="loading" class="flex flex-col gap-5 lg:flex-row">
      <div class="lg:w-[200px] lg:shrink-0">
        <LoadingSkeleton variant="text" :rows="4" />
      </div>
      <div class="min-w-0 flex-1">
        <div class="gd-card w-full max-w-content-xs p-6">
          <LoadingSkeleton variant="text" :rows="7" />
        </div>
      </div>
    </div>

    <!-- ============ 加载失败 ============ -->
    <ErrorRetryBlock v-else-if="errorMsg" message="个人设置加载失败" :description="errorMsg" @retry="loadProfile" />

    <!-- ============ 设置内容 ============ -->
    <div v-else class="flex flex-col gap-6 lg:flex-row">
      <!-- ==================== 左设置 Tab（桌面 200px；移动端横向滑动条） ==================== -->
      <nav class="lg:w-[200px] lg:shrink-0" aria-label="设置分类">
        <ul class="flex gap-1 overflow-x-auto pb-2 lg:flex-col lg:gap-0 lg:space-y-1 lg:overflow-x-visible lg:pb-0">
          <li v-for="tab in TABS" :key="tab.key" class="shrink-0 lg:shrink">
            <button
              type="button"
              class="flex w-full items-center gap-2.5 whitespace-nowrap rounded-lg px-3 py-2 text-sm transition-colors duration-gd-normal"
              :class="activeTab === tab.key ? 'bg-primary-50 font-medium text-primary-700' : 'text-neutral-600 hover:bg-surface-sunken'"
              :aria-current="activeTab === tab.key ? 'page' : undefined"
              @click="activeTab = tab.key"
            >
              <component
                :is="tab.icon"
                class="h-4 w-4 shrink-0"
                :weight="activeTab === tab.key ? 'fill' : 'regular'"
                aria-hidden="true"
              />
              {{ tab.label }}
            </button>
          </li>
        </ul>

        <!-- 快捷入口（对照 F1-04 左栏「快捷入口」组） -->
        <p class="gd-section-label mt-6 hidden px-3 lg:block">快捷入口</p>
        <ul class="hidden gap-0 space-y-1 lg:mt-2 lg:block">
          <li>
            <RouterLink
              to="/notifications"
              class="flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-neutral-600 transition-colors duration-gd-normal hover:bg-surface-sunken"
            >
              <PhTray class="h-4 w-4 shrink-0" aria-hidden="true" />
              通知中心
            </RouterLink>
          </li>
        </ul>
      </nav>

      <!-- ==================== 右侧分区白纸卡 ==================== -->
      <div class="min-w-0 flex-1">
        <div class="gd-card w-full p-6 sm:p-7">
          <!-- ============ 个人资料 ============ -->
          <section v-if="activeTab === 'profile'" aria-label="个人资料">
            <h2 class="text-title text-neutral-900">个人资料</h2>

            <!-- 头像（后端无上传接口：禁用更换入口并标注即将上线，避免“预览成功但保存丢失”） -->
            <div class="mt-5 flex items-center gap-4 border-b border-hairline-soft pb-6">
              <span
                v-if="avatarDisplay"
                class="flex h-16 w-16 shrink-0 items-center justify-center overflow-hidden rounded-full border border-hairline bg-surface-canvas"
              >
                <img :src="avatarDisplay" alt="当前头像" class="h-full w-full object-cover" />
              </span>
              <span
                v-else
                class="flex h-16 w-16 shrink-0 items-center justify-center rounded-full bg-[image:var(--gd-grad-gold)] text-xl font-semibold text-white"
                aria-hidden="true"
              >
                {{ (authStore.user?.nickname ?? '梦')[0] }}
              </span>
              <div class="min-w-0 flex-1">
                <span
                  class="inline-flex h-9 cursor-not-allowed items-center rounded-full border border-hairline bg-surface-raised px-4 text-sm text-neutral-400"
                  title="头像更换即将上线：后端暂无头像上传接口"
                >
                  更换头像（即将上线）
                </span>
                <p class="mt-1.5 text-caption text-neutral-400">头像更换即将上线：当前仅展示账号已有头像。</p>
              </div>
            </div>

            <!-- 昵称（失焦自动保存的语义保留为手动保存，提交按钮在下方） -->
            <div class="mt-6 max-w-[420px]">
              <label class="block">
                <span class="mb-1.5 block text-caption text-neutral-500">昵称（失焦自动保存）</span>
                <input
                  v-model="nickname"
                  type="text"
                  maxlength="30"
                  placeholder="输入昵称（≤30 字）"
                  class="gd-input"
                />
              </label>
              <!-- 手机号只读 -->
              <label class="mt-4 block">
                <span class="mb-1.5 block text-caption text-neutral-500">手机号（只读）</span>
                <input
                  type="text"
                  :value="authStore.user?.phone ?? '—'"
                  disabled
                  class="gd-input gd-num"
                />
                <span class="mt-1 block text-caption text-neutral-400">手机号为登录凭证，暂不支持修改</span>
              </label>
            </div>

            <div class="mt-7 flex justify-end border-t border-hairline-soft pt-5">
              <AppButton variant="primary" size="md" :loading="savingProfile" @click="saveProfile">保存资料</AppButton>
            </div>
          </section>

          <!-- ============ 创作偏好 ============ -->
          <section v-else-if="activeTab === 'preference'" aria-label="创作偏好">
            <h2 class="text-title text-neutral-900">创作偏好</h2>
            <p class="mt-1 text-caption text-neutral-500">作为新建项目的默认值，可随时调整</p>

            <!-- 题材多选 -->
            <div class="mt-5">
              <p class="gd-section-label mb-2">题材偏好（多选）</p>
              <div class="flex flex-wrap gap-2">
                <button
                  v-for="genre in GENRE_OPTIONS"
                  :key="genre.key"
                  type="button"
                  class="rounded-full border px-3.5 py-1.5 text-sm transition-colors duration-gd-normal"
                  :class="favoriteGenres.includes(genre.key) ? 'border-primary-500 bg-primary-50 text-primary-700' : 'border-hairline text-neutral-600 hover:border-hairline-strong hover:bg-surface-sunken'"
                  :aria-pressed="favoriteGenres.includes(genre.key)"
                  @click="toggleGenre(genre.key)"
                >
                  {{ genre.label }}
                </button>
              </div>
            </div>

            <!-- 画风单选 -->
            <div class="mt-5">
              <p class="gd-section-label mb-2">默认画风</p>
              <div class="flex flex-wrap gap-2">
                <button
                  v-for="style in STYLE_OPTIONS"
                  :key="style.key"
                  type="button"
                  class="rounded-lg border px-3.5 py-1.5 text-sm transition-colors duration-gd-normal"
                  :class="defaultStyle === style.key ? 'border-primary-500 bg-primary-50 text-primary-700' : 'border-hairline text-neutral-600 hover:border-hairline-strong hover:bg-surface-sunken'"
                  :aria-pressed="defaultStyle === style.key"
                  @click="defaultStyle = style.key"
                >
                  {{ style.label }}
                </button>
              </div>
            </div>

            <!-- 画幅单选 -->
            <div class="mt-5">
              <p class="gd-section-label mb-2">默认画幅</p>
              <div class="flex flex-wrap gap-2">
                <button
                  v-for="aspect in ASPECT_RATIO_OPTIONS"
                  :key="aspect.key"
                  type="button"
                  class="rounded-lg border px-3.5 py-1.5 text-sm transition-colors duration-gd-normal"
                  :class="defaultAspectRatio === aspect.key ? 'border-primary-500 bg-primary-50 text-primary-700' : 'border-hairline text-neutral-600 hover:border-hairline-strong hover:bg-surface-sunken'"
                  :aria-pressed="defaultAspectRatio === aspect.key"
                  @click="defaultAspectRatio = aspect.key"
                >
                  {{ aspect.label }}
                </button>
              </div>
            </div>

            <!-- 默认集数步进器 -->
            <div class="mt-5">
              <p class="gd-section-label mb-2">默认集数</p>
              <div class="flex items-center gap-3">
                <button
                  type="button"
                  class="flex h-9 w-9 items-center justify-center rounded-full border border-hairline text-neutral-600 transition-colors duration-gd-fast hover:bg-surface-canvas disabled:opacity-40 disabled:cursor-not-allowed"
                  :disabled="defaultEpisodeCount <= 1"
                  aria-label="减少集数"
                  @click="defaultEpisodeCount = Math.max(1, defaultEpisodeCount - 1)"
                >
                  −
                </button>
                <span class="gd-num w-10 text-center text-base font-semibold text-neutral-900">{{ defaultEpisodeCount }}</span>
                <button
                  type="button"
                  class="flex h-9 w-9 items-center justify-center rounded-full border border-hairline text-neutral-600 transition-colors duration-gd-fast hover:bg-surface-canvas disabled:opacity-40 disabled:cursor-not-allowed"
                  :disabled="defaultEpisodeCount >= 30"
                  aria-label="增加集数"
                  @click="defaultEpisodeCount = Math.min(30, defaultEpisodeCount + 1)"
                >
                  +
                </button>
                <span class="text-caption text-neutral-400">1~30 集</span>
              </div>
            </div>

            <div class="mt-7 flex justify-end border-t border-hairline-soft pt-5">
              <AppButton variant="primary" size="md" :loading="savingPrefs" @click="savePreferences">保存偏好</AppButton>
            </div>
          </section>

          <!-- ============ 通知偏好（对照 F1-04 开关行） ============ -->
          <section v-else-if="activeTab === 'notify'" aria-label="通知偏好">
            <div class="flex items-baseline justify-between gap-3">
              <h2 class="text-title text-neutral-900">通知偏好</h2>
              <span class="text-caption text-neutral-400">开关即时保存</span>
            </div>

            <ul class="mt-2 divide-y divide-hairline-soft">
              <li v-for="option in NOTIFY_OPTIONS" :key="option.key" class="flex items-center justify-between gap-6 py-4">
                <div class="min-w-0">
                  <p class="text-sm font-medium text-neutral-800">{{ option.label }}</p>
                  <p class="mt-0.5 text-caption text-neutral-400">{{ option.desc }}</p>
                </div>
                <!-- 紫色胶囊开关（自研 role=switch） -->
                <button
                  type="button"
                  role="switch"
                  class="relative h-5 w-9 shrink-0 rounded-full transition-colors duration-gd-fast disabled:cursor-not-allowed disabled:opacity-60"
                  :class="notifyPrefs[option.key] ? 'bg-primary-500' : 'bg-neutral-200'"
                  :aria-checked="notifyPrefs[option.key]"
                  :aria-label="option.label"
                  :disabled="notifyingKey !== ''"
                  @click="toggleNotify(option.key)"
                >
                  <span
                    class="absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-[var(--gd-ink)] shadow-gd-1 transition-transform duration-gd-fast"
                    :class="notifyPrefs[option.key] ? 'translate-x-4' : ''"
                    aria-hidden="true"
                  ></span>
                </button>
              </li>
            </ul>
          </section>

          <!-- ============ 安全设置（含底部危险区） ============ -->
          <section v-else-if="activeTab === 'security'" aria-label="安全设置">
            <h2 class="text-title text-neutral-900">安全设置</h2>
            <p class="mt-1 text-caption text-neutral-500">修改登录密码与管理登录设备</p>

            <!-- 能力降级提示：后端尚未提供修改密码接口，表单禁用而非伪装可提交 -->
            <div class="mt-4 rounded-lg border border-warning-border bg-warning-bg px-4 py-2.5" role="note">
              <p class="text-caption text-gd-pending">修改密码即将上线：后端接口尚未开放，当前表单为只读预览，暂不可提交。</p>
            </div>

            <!-- 修改密码表单（禁用态 + 原因提示） -->
            <form class="mt-5 max-w-[420px] space-y-4" @submit.prevent>
              <label class="block">
                <span class="mb-1.5 block text-caption text-neutral-500">旧密码</span>
                <input
                  v-model="oldPassword"
                  type="password"
                  autocomplete="current-password"
                  class="gd-input"
                  disabled
                  title="修改密码即将上线：后端接口未开放"
                />
              </label>
              <label class="block">
                <span class="mb-1.5 block text-caption text-neutral-500">新密码（≥6 位）</span>
                <input
                  v-model="newPassword"
                  type="password"
                  autocomplete="new-password"
                  class="gd-input"
                  disabled
                  title="修改密码即将上线：后端接口未开放"
                />
              </label>
              <label class="block">
                <span class="mb-1.5 block text-caption text-neutral-500">确认新密码</span>
                <input
                  v-model="confirmPassword"
                  type="password"
                  autocomplete="new-password"
                  class="gd-input"
                  disabled
                  title="修改密码即将上线：后端接口未开放"
                />
              </label>
              <div class="flex items-center justify-end gap-3">
                <span class="text-caption text-neutral-400">即将上线</span>
                <AppButton variant="primary" size="md" type="submit" disabled title="修改密码即将上线：后端接口未开放">
                  修改密码
                </AppButton>
              </div>
            </form>

            <!-- 登录设备占位 -->
            <div class="mt-8 border-t border-hairline-soft pt-5">
              <h3 class="text-sm font-semibold text-neutral-900">最近登录</h3>
              <ul class="mt-3 space-y-2.5">
                <li v-for="device in DEVICES" :key="device.name" class="gd-card-flat flex items-center gap-3 px-4 py-3">
                  <span
                    class="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-hairline bg-surface-sunken text-neutral-400"
                    aria-hidden="true"
                  >
                    <component :is="device.icon" class="h-[18px] w-[18px]" weight="duotone" />
                  </span>
                  <div class="min-w-0 flex-1">
                    <p class="text-sm text-neutral-800">{{ device.name }}</p>
                    <p class="text-caption text-neutral-400">{{ device.meta }}</p>
                  </div>
                  <span v-if="device.current" class="rounded-full bg-success-bg px-2 py-0.5 text-caption text-success-strong">当前设备</span>
                  <AppButton v-else variant="text" size="sm" disabled>下线（占位）</AppButton>
                </li>
              </ul>
            </div>

            <!-- 危险区（粉底圆角块 + 红描边胶囊，对照 F1-04 底部） -->
            <div class="mt-8 rounded-xl border border-danger/25 bg-danger-bg p-5">
              <div class="flex flex-wrap items-center justify-between gap-4">
                <div class="min-w-0">
                  <h3 class="text-sm font-semibold text-danger">注销账号</h3>
                  <p class="mt-1 text-caption leading-relaxed text-danger/80">注销后所有项目、资产与剩余积分将被清空，且无法恢复。</p>
                </div>
                <AppButton
                  variant="secondary"
                  size="md"
                  class="!border-danger !bg-transparent !text-danger"
                  disabled
                  title="注销账号即将上线：后端接口未开放，当前不可执行"
                >
                  注销账号（即将上线）
                </AppButton>
              </div>
            </div>
          </section>

          <!-- ============ 关于 ============ -->
          <section v-else aria-label="关于浮光造梦">
            <h2 class="text-title text-neutral-900">关于浮光造梦</h2>
            <p class="mt-1 text-caption text-neutral-500">GleamDream Studio · AI 短剧创作工具</p>

            <dl class="mt-5 space-y-3.5 text-sm">
              <div class="flex items-center justify-between border-b border-hairline-soft pb-3.5">
                <dt class="text-neutral-500">版本号</dt>
                <dd class="gd-num font-medium text-neutral-800">v{{ APP_VERSION }}</dd>
              </div>
              <div class="flex items-center justify-between border-b border-hairline-soft pb-3.5">
                <dt class="text-neutral-500">接口文档</dt>
                <dd>
                  <a :href="DOCS_URL" target="_blank" rel="noopener" class="font-medium text-primary-600 hover:text-primary-700">
                    后端 Swagger（FastAPI /docs）↗
                  </a>
                </dd>
              </div>
              <div class="flex items-center justify-between border-b border-hairline-soft pb-3.5">
                <dt class="text-neutral-500">开源致谢</dt>
                <dd class="text-right text-caption leading-relaxed text-neutral-500">
                  Vue 3 · Vite · Tailwind CSS · Pinia · Konva<br />
                  FastAPI · SQLAlchemy · LangGraph
                </dd>
              </div>
              <div class="flex items-center justify-between">
                <dt class="text-neutral-500">反馈渠道</dt>
                <dd class="text-caption text-neutral-400">演示环境暂未开通</dd>
              </div>
            </dl>
          </section>
        </div>
      </div>
    </div>

  </div>
</template>

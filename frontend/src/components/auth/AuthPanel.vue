<!--
  文件级注释：P01 登录 / 注册共享面板（/login 与 /register 两路由复用同一组件）· v5 按参考设计稿 1:1 还原。
  构图（对齐参考图实测）：全屏暗场舞台 + 左下斜向光束 + 特大暖奶油衬线刊头（柔和暖白辉光）+
  宽字距英文题注 + 中央暗玻璃卡（发丝网格 Tab、域内标签式输入、高主按钮）+
  两侧真实极光摄影光卡（视高 61% 处，aurora-left/right.jpg，白紫光晕）+
  三条白色光影条（右上带黑段截断 / 左中带黑段截断 / 右下短弱无截断）+
  收敛的紫色辉光 + 胶片颗粒 + 暗角。
  功能：验证码/密码双通道登录、注册（昵称/密码选填 + 确认密码）、60s 验证码倒计时
  （dev 模式自动回填 dev_code）、字段级内联校验（必填 / 手机号格式 / 密码长度 /
  两次一致性 + 强度提示）、协议勾选校验、登录/注册成功后按 onboarding_done 决定跳转。
  交互：登录/注册表单切换带方向感知的滑动过渡。
  无障碍：视觉上标签收入域内（与设计稿一致），输入框以 aria-label 提供可访问名称；
  校验错误仍以 role=alert 内联呈现。
  后端契约：账号标识为手机号（无邮箱字段），身份核验由短信验证码承担。
-->
<script setup lang="ts">
/**
 * AuthPanel 认证面板。
 * props：无（通过 route.name 区分登录 / 注册 Tab）。
 * emits：无。
 */
import { computed, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { sendSmsCode } from '@/api/auth'
import type { RegisterPayload } from '@/api/auth'
import AppButton from '@/components/common/AppButton.vue'
import { useToast } from '@/composables/useToast'
import { useAuthStore } from '@/stores/auth'

/** 路由实例（Tab 切换与登录后跳转） */
const router = useRouter()
/** 当前路由（判断激活 Tab / redirect 参数） */
const route = useRoute()
/** 全局 Toast */
const toast = useToast()
/** 认证 store */
const authStore = useAuthStore()

/** 当前是否为注册 Tab（由 route.name 驱动，保证 URL 与视图一致） */
const isRegisterTab = computed(() => route.name === 'register')

/** 表单切换过渡方向：登录 → 注册为 fwd（右滑入），反之为 back */
const swapDirection = ref<'fwd' | 'back'>('fwd')

/** 登录通道：sms 验证码 / password 密码（密码登录为折叠切换项） */
const loginMode = ref<'sms' | 'password'>('sms')

/* ---------- 共享表单状态（Tab 间保留输入） ---------- */
/** 手机号（登录 / 注册共用） */
const phone = ref('')
/** 短信验证码 */
const smsCode = ref('')
/** 登录密码（password 通道） */
const password = ref('')
/** 注册昵称（选填） */
const nickname = ref('')
/** 注册密码（选填，设置后支持密码登录） */
const registerPassword = ref('')
/** 确认密码（与注册密码配对校验） */
const registerConfirm = ref('')
/** 协议勾选 */
const agreed = ref(false)
/** 提交中标记 */
const submitting = ref(false)
/** 验证码发送中标记 */
const smsSending = ref(false)
/** 验证码倒计时（秒） */
const countdown = ref(0)
/** 倒计时定时器句柄 */
let countdownTimer: ReturnType<typeof setInterval> | null = null

/** 密码可见性（登录密码 / 注册密码 / 确认密码各自独立） */
const showLoginPassword = ref(false)
const showRegPassword = ref(false)
const showRegConfirm = ref(false)

/** 字段级校验错误：key 见 FieldErrorKey，空对象表示全部通过 */
type FieldErrorKey = 'phone' | 'smsCode' | 'password' | 'regPassword' | 'regConfirm' | 'agreed'
const fieldErrors = ref<Partial<Record<FieldErrorKey, string>>>({})

/** 手机号格式是否合法（与后端校验规则一致：1 开头第二位 3-9） */
const isPhoneValid = computed(() => /^1[3-9]\d{9}$/.test(phone.value.trim()))

/** 注册密码是否合法（留空视为不设置；填写时须 ≥6 位） */
const isRegisterPasswordValid = computed(() => registerPassword.value === '' || registerPassword.value.length >= 6)

/** 注册密码强度：0 未填写 / 1 弱 / 2 中 / 3 强（长度 + 字符组合） */
const regStrength = computed(() => {
  const value = registerPassword.value
  if (!value) return 0
  let score = 0
  if (value.length >= 6) score += 1
  if (/[a-zA-Z]/.test(value) && /\d/.test(value)) score += 1
  if (value.length >= 10 || /[^a-zA-Z0-9]/.test(value)) score += 1
  return score
})

/** 强度条分段色（1/2/3 档分别对应危险 / 琥珀 / 成功） */
const regStrengthColor = computed(() => {
  if (regStrength.value <= 1) return 'bg-danger'
  if (regStrength.value === 2) return 'bg-warning'
  return 'bg-success'
})

/** 强度文案与文字色 */
const regStrengthLabel = computed(() => {
  if (regStrength.value <= 1) return { text: '弱', cls: 'text-danger-bright' }
  if (regStrength.value === 2) return { text: '中', cls: 'text-warning-strong' }
  return { text: '强', cls: 'text-success-strong' }
})

/** 验证码按钮禁用条件：倒计时中 / 发送中 / 手机号不合法 */
const sendCodeDisabled = computed(() => countdown.value > 0 || smsSending.value || !isPhoneValid.value)

/** 验证码按钮文案 */
const sendCodeLabel = computed(() => {
  if (smsSending.value) return '发送中…'
  return countdown.value > 0 ? `${countdown.value}s 后重发` : '获取验证码'
})

/**
 * 读取字段错误态对应的输入框类串（暗场基类 + 错误时叠加 .gd-field-invalid）。
 * @param key 字段键
 * @returns 输入框类串
 */
function fieldCls(key: FieldErrorKey): string {
  return fieldErrors.value[key] ? 'gd-input gd-field-dark gd-field-invalid' : 'gd-input gd-field-dark'
}

/**
 * 写入 / 清除字段错误。
 * @param key 字段键
 * @param message 错误文案；传空串表示清除
 * @returns 无
 */
function setFieldError(key: FieldErrorKey, message: string): void {
  if (message) fieldErrors.value[key] = message
  else delete fieldErrors.value[key]
}

/**
 * 校验手机号并回写错误态。
 * @returns 是否通过
 */
function validatePhone(): boolean {
  if (!phone.value.trim()) {
    setFieldError('phone', '请输入手机号')
    return false
  }
  if (!isPhoneValid.value) {
    setFieldError('phone', '手机号格式不正确（11 位，1 开头）')
    return false
  }
  setFieldError('phone', '')
  return true
}

/**
 * 校验协议勾选并回写错误态。
 * @returns 是否通过
 */
function validateAgreed(): boolean {
  if (!agreed.value) {
    setFieldError('agreed', '请先阅读并同意用户协议与隐私政策')
    return false
  }
  setFieldError('agreed', '')
  return true
}

/**
 * 卸载时清理倒计时定时器，避免内存泄漏。
 * @returns 无
 */
onUnmounted(() => {
  if (countdownTimer) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
})

/**
 * 启动 60s 验证码倒计时。
 * @returns 无
 */
function startCountdown(): void {
  countdown.value = 60
  if (countdownTimer) clearInterval(countdownTimer)
  countdownTimer = setInterval(() => {
    countdown.value -= 1
    if (countdown.value <= 0 && countdownTimer) {
      clearInterval(countdownTimer)
      countdownTimer = null
    }
  }, 1000)
}

/**
 * Tab 切换：以路由驱动（同一页面组件复用，URL 同步变化），
 * 并记录方向供过渡动画使用。
 * @param target 目标 Tab（login / register）
 * @returns 无
 */
function switchTab(target: 'login' | 'register'): void {
  if (route.name === target) return
  swapDirection.value = target === 'register' ? 'fwd' : 'back'
  void router.replace({ name: target })
}

/**
 * 发送短信验证码：dev 模式响应含 dev_code 时自动填入输入框。
 * @returns 无
 */
async function handleSendCode(): Promise<void> {
  if (!isPhoneValid.value) {
    setFieldError('phone', '请先输入正确的 11 位手机号')
    return
  }
  if (sendCodeDisabled.value) return
  smsSending.value = true
  try {
    const result = await sendSmsCode(phone.value.trim())
    startCountdown()
    if (result.dev_code) {
      smsCode.value = result.dev_code
      toast.success('验证码已发送(开发模式)')
    } else {
      toast.success('验证码已发送，请查收短信')
    }
  } catch {
    // 错误提示已由 http 拦截器统一 toast，这里仅终止流程
  } finally {
    smsSending.value = false
  }
}

/**
 * 登录 / 注册成功后的跳转：未完成新手引导先去 /onboarding，否则回 redirect 或 /projects。
 * @returns 无
 */
async function navigateAfterAuth(): Promise<void> {
  // 登录/注册响应不含 preferences，拉取一次 /users/me 拿到最新偏好再判断
  let onboardingDone = false
  try {
    const user = await authStore.fetchMe()
    onboardingDone = user.preferences?.onboarding_done === true
  } catch {
    // 拉取失败按未完成引导处理（引导页可再次提交）
  }
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : ''
  if (!onboardingDone) {
    await router.replace('/onboarding')
  } else {
    await router.replace(redirect || '/projects')
  }
}

/**
 * 提交登录（sms / password 双通道）：字段级校验全部通过才发起请求。
 * @returns 无
 */
async function handleLogin(): Promise<void> {
  const phoneOk = validatePhone()
  let secretOk = true
  if (loginMode.value === 'sms' && !smsCode.value.trim()) {
    setFieldError('smsCode', '请输入验证码')
    secretOk = false
  } else {
    setFieldError('smsCode', '')
  }
  if (loginMode.value === 'password' && !password.value) {
    setFieldError('password', '请输入登录密码')
    secretOk = false
  } else {
    setFieldError('password', '')
  }
  if (!phoneOk || !secretOk || !validateAgreed()) return
  submitting.value = true
  try {
    const user = await authStore.login(
      loginMode.value === 'password'
        ? { login_type: 'password', phone: phone.value.trim(), password: password.value }
        : { login_type: 'sms', phone: phone.value.trim(), sms_code: smsCode.value.trim() },
    )
    toast.success(`欢迎回来，${user.nickname ?? '创作者'}`)
    await navigateAfterAuth()
  } catch {
    // 拦截器已 toast
  } finally {
    submitting.value = false
  }
}

/**
 * 提交注册（昵称 / 密码可选；密码填写时校验长度、强度提示与两次一致性）。
 * @returns 无
 */
async function handleRegister(): Promise<void> {
  const phoneOk = validatePhone()
  let codeOk = true
  if (!smsCode.value.trim()) {
    setFieldError('smsCode', '请输入验证码')
    codeOk = false
  } else {
    setFieldError('smsCode', '')
  }
  let passwordOk = true
  if (!isRegisterPasswordValid.value) {
    setFieldError('regPassword', '密码至少 6 位，或留空不设置')
    passwordOk = false
  } else {
    setFieldError('regPassword', '')
  }
  if (registerPassword.value && registerConfirm.value !== registerPassword.value) {
    setFieldError('regConfirm', '两次输入的密码不一致')
    passwordOk = false
  } else if (!registerPassword.value && registerConfirm.value) {
    setFieldError('regConfirm', '请先输入上方密码，或清空此处')
    passwordOk = false
  } else {
    setFieldError('regConfirm', '')
  }
  if (!phoneOk || !codeOk || !passwordOk || !validateAgreed()) return
  submitting.value = true
  try {
    const payload: RegisterPayload = { phone: phone.value.trim(), sms_code: smsCode.value.trim() }
    if (nickname.value.trim()) payload.nickname = nickname.value.trim()
    if (registerPassword.value) payload.password = registerPassword.value
    const user = await authStore.register(payload)
    toast.success(`欢迎加入，${user.nickname ?? '创作者'}`)
    await navigateAfterAuth()
  } catch {
    // 拦截器已 toast
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="auth-root relative flex min-h-[100dvh] flex-col items-center justify-center overflow-hidden px-4 py-10">
    <!-- ============ 装饰层（纯展示，不响应指针；reduced-motion 下全部静止） ============ -->
    <div class="auth-glow" aria-hidden="true"></div>
    <div class="auth-glow-core" aria-hidden="true"></div>
    <div class="auth-grain" aria-hidden="true"></div>
    <div class="auth-vignette" aria-hidden="true"></div>

    <!-- 极光光卡（<1280px 隐藏）：容器整体再做 1px 柔焦，与背景融为一体 -->
    <div class="auth-deco pointer-events-none absolute inset-0 hidden xl:block" aria-hidden="true">
      <div class="auth-chip auth-chip-left">
        <span class="auth-chip-glow auth-chip-glow--left"></span>
        <span class="auth-chip-veil auth-chip-veil--left"></span>
      </div>
      <div class="auth-chip auth-chip-right">
        <span class="auth-chip-glow auth-chip-glow--right"></span>
        <span class="auth-chip-veil auth-chip-veil--right"></span>
      </div>
    </div>

    <!-- 斜向光束（独立层：自带 2px 柔焦，不进带 filter 的容器，避免嵌套滤镜逐帧重排影响滑动流畅度） -->
    <div class="pointer-events-none absolute inset-0 hidden xl:block" aria-hidden="true">
      <div class="auth-beam"></div>
    </div>

    <!-- 光影条（独立层：不做容器柔焦，保持清晰边缘；<1280px 隐藏） -->
    <div class="auth-streaks pointer-events-none absolute inset-0 hidden xl:block" aria-hidden="true">
      <div class="auth-streak auth-streak-a"></div>
      <div class="auth-streak auth-streak-b"></div>
      <div class="auth-streak auth-streak-e"></div>
    </div>

    <!-- 薄雾层：轻雾化背景装饰，营造薄雾氛围；刊头与表单绘制在其上方，保持清晰可读 -->
    <div class="auth-fog pointer-events-none absolute inset-0" aria-hidden="true"></div>

    <!-- ============ 刊头 ============ -->
    <header class="relative mb-[clamp(36px,6.5vh,72px)] flex flex-col items-center text-center">
      <h1 class="auth-title font-serif">浮光造梦</h1>
      <p class="auth-caption mt-2">GLEAMDREAM STUDIO</p>
    </header>

    <!-- ============ 暗玻璃卡 ============ -->
    <main class="auth-glass relative w-full max-w-[420px] rounded-[20px] p-6 sm:p-10">
      <!-- 发丝网格式 Tab（对齐设计稿：等分格 + 竖分隔线 + 底部发丝线） -->
      <div class="auth-tabs" role="tablist" aria-label="登录或注册">
        <button
          id="auth-tab-login"
          type="button"
          role="tab"
          :aria-selected="!isRegisterTab"
          class="auth-tab"
          :class="{ 'auth-tab-active': !isRegisterTab }"
          @click="switchTab('login')"
        >
          登录
        </button>
        <button
          id="auth-tab-register"
          type="button"
          role="tab"
          :aria-selected="isRegisterTab"
          class="auth-tab"
          :class="{ 'auth-tab-active': isRegisterTab }"
          @click="switchTab('register')"
        >
          注册
        </button>
      </div>

      <!-- 登录 / 注册表单：方向感知滑动过渡（输入状态存于 ref，切换不丢失） -->
      <Transition :name="`gd-auth-${swapDirection}`" mode="out-in">
        <!-- 登录表单 -->
        <form
          v-if="!isRegisterTab"
          key="login"
          novalidate
          role="tabpanel"
          aria-labelledby="auth-tab-login"
          @submit.prevent="handleLogin"
        >
          <div>
            <input
              v-model="phone"
              type="tel"
              maxlength="11"
              autocomplete="tel"
              placeholder="手机号"
              aria-label="手机号"
              :class="fieldCls('phone')"
              :aria-invalid="!!fieldErrors.phone || undefined"
              :aria-describedby="fieldErrors.phone ? 'login-phone-error' : undefined"
              @input="setFieldError('phone', '')"
            />
            <p v-if="fieldErrors.phone" id="login-phone-error" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.phone }}</p>
          </div>

          <!-- 验证码通道：验证码与「获取验证码」同域（对齐设计稿） -->
          <div v-if="loginMode === 'sms'" class="mt-7">
            <div :class="fieldErrors.smsCode ? 'auth-code gd-field-invalid' : 'auth-code'">
              <input
                v-model="smsCode"
                type="text"
                inputmode="numeric"
                maxlength="8"
                autocomplete="one-time-code"
                placeholder="验证码"
                aria-label="验证码"
                :aria-invalid="!!fieldErrors.smsCode || undefined"
                :aria-describedby="fieldErrors.smsCode ? 'login-code-error' : undefined"
                @input="setFieldError('smsCode', '')"
              />
              <button type="button" class="auth-code-send" :disabled="sendCodeDisabled" @click="handleSendCode">
                {{ sendCodeLabel }}
              </button>
            </div>
            <p v-if="fieldErrors.smsCode" id="login-code-error" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.smsCode }}</p>
          </div>

          <!-- 密码通道（折叠切换） -->
          <div v-else class="relative mt-6">
            <input
              v-model="password"
              :type="showLoginPassword ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="密码"
              aria-label="登录密码"
              :class="fieldCls('password') + ' pr-12'"
              :aria-invalid="!!fieldErrors.password || undefined"
              :aria-describedby="fieldErrors.password ? 'login-password-error' : undefined"
              @input="setFieldError('password', '')"
            />
            <button
              type="button"
              class="absolute inset-y-0 right-0 flex items-center px-3 text-player-hint transition-colors duration-gd-fast hover:text-player-text"
              :aria-label="showLoginPassword ? '隐藏密码' : '显示密码'"
              @click="showLoginPassword = !showLoginPassword"
            >
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" stroke-linecap="round" stroke-linejoin="round" />
                <circle cx="12" cy="12" r="3" />
                <path v-if="!showLoginPassword" d="M4 20 20 4" stroke-linecap="round" />
              </svg>
            </button>
            <p v-if="fieldErrors.password" id="login-password-error" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.password }}</p>
          </div>

          <!-- 密码 / 验证码登录折叠切换 -->
          <div class="mt-3 text-right">
            <button
              type="button"
              class="text-xs text-primary-300 transition-colors duration-gd-fast hover:text-primary-200"
              @click="loginMode = loginMode === 'sms' ? 'password' : 'sms'"
            >
              {{ loginMode === 'sms' ? '密码登录' : '验证码登录' }}
            </button>
          </div>

          <!-- 协议勾选 -->
          <div class="mt-4">
            <label class="flex cursor-pointer items-start gap-2 text-xs leading-relaxed text-player-hint">
              <input v-model="agreed" type="checkbox" class="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[color:var(--gd-primary-400)]" @change="setFieldError('agreed', '')" />
              <span>
                我已阅读并同意
                <span class="text-primary-300" title="协议文本即将上线，当前无法查看">《用户协议》（即将上线）</span>
                与
                <span class="text-primary-300" title="隐私政策文本即将上线，当前无法查看">《隐私政策》（即将上线）</span>
              </span>
            </label>
            <p v-if="fieldErrors.agreed" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.agreed }}</p>
          </div>

          <span class="auth-submit mt-9 block">
            <AppButton type="submit" block size="lg" :loading="submitting">登 录</AppButton>
          </span>

          <p class="mt-5 text-center text-xs text-player-hint">
            还没有账号？
            <button type="button" class="text-primary-300 hover:underline" @click="switchTab('register')">立即注册</button>
          </p>
        </form>

        <!-- 注册表单 -->
        <form
          v-else
          key="register"
          novalidate
          role="tabpanel"
          aria-labelledby="auth-tab-register"
          @submit.prevent="handleRegister"
        >
          <div>
            <input
              v-model="nickname"
              type="text"
              maxlength="64"
              autocomplete="nickname"
              placeholder="昵称（选填）"
              aria-label="昵称（选填）"
              class="gd-input gd-field-dark"
            />
          </div>

          <div class="mt-5">
            <input
              v-model="phone"
              type="tel"
              maxlength="11"
              autocomplete="tel"
              placeholder="手机号"
              aria-label="手机号"
              :class="fieldCls('phone')"
              :aria-invalid="!!fieldErrors.phone || undefined"
              :aria-describedby="fieldErrors.phone ? 'reg-phone-error' : undefined"
              @input="setFieldError('phone', '')"
            />
            <p v-if="fieldErrors.phone" id="reg-phone-error" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.phone }}</p>
          </div>

          <div class="mt-5">
            <div :class="fieldErrors.smsCode ? 'auth-code gd-field-invalid' : 'auth-code'">
              <input
                v-model="smsCode"
                type="text"
                inputmode="numeric"
                maxlength="8"
                autocomplete="one-time-code"
                placeholder="验证码"
                aria-label="验证码"
                :aria-invalid="!!fieldErrors.smsCode || undefined"
                :aria-describedby="fieldErrors.smsCode ? 'reg-code-error' : undefined"
                @input="setFieldError('smsCode', '')"
              />
              <button type="button" class="auth-code-send" :disabled="sendCodeDisabled" @click="handleSendCode">
                {{ sendCodeLabel }}
              </button>
            </div>
            <p v-if="fieldErrors.smsCode" id="reg-code-error" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.smsCode }}</p>
          </div>

          <div class="relative mt-6">
            <input
              v-model="registerPassword"
              :type="showRegPassword ? 'text' : 'password'"
              autocomplete="new-password"
              placeholder="设置密码（选填，6 位以上）"
              aria-label="设置密码（选填，6 位以上）"
              :class="fieldCls('regPassword') + ' pr-12'"
              :aria-invalid="!!fieldErrors.regPassword || undefined"
              :aria-describedby="fieldErrors.regPassword ? 'reg-password-error' : undefined"
              @input="setFieldError('regPassword', ''); setFieldError('regConfirm', '')"
            />
            <button
              type="button"
              class="absolute inset-y-0 right-0 flex items-center px-3 text-player-hint transition-colors duration-gd-fast hover:text-player-text"
              :aria-label="showRegPassword ? '隐藏密码' : '显示密码'"
              @click="showRegPassword = !showRegPassword"
            >
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" stroke-linecap="round" stroke-linejoin="round" />
                <circle cx="12" cy="12" r="3" />
                <path v-if="!showRegPassword" d="M4 20 20 4" stroke-linecap="round" />
              </svg>
            </button>
            <!-- 密码强度提示（填写时出现） -->
            <div v-if="registerPassword" class="mt-2 flex items-center gap-2">
              <div class="flex flex-1 gap-1" aria-hidden="true">
                <span v-for="i in 3" :key="i" class="h-1 flex-1 rounded-full transition-colors duration-gd-normal" :class="i <= regStrength ? regStrengthColor : 'bg-white/10'" />
              </div>
              <span class="text-caption" :class="regStrengthLabel.cls">强度{{ regStrengthLabel.text }}</span>
            </div>
            <p v-if="fieldErrors.regPassword" id="reg-password-error" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.regPassword }}</p>
          </div>

          <div class="relative mt-6">
            <input
              v-model="registerConfirm"
              :type="showRegConfirm ? 'text' : 'password'"
              autocomplete="new-password"
              placeholder="确认密码（与上方密码一致）"
              aria-label="确认密码（与上方密码一致）"
              :class="fieldCls('regConfirm') + ' pr-12'"
              :aria-invalid="!!fieldErrors.regConfirm || undefined"
              :aria-describedby="fieldErrors.regConfirm ? 'reg-confirm-error' : undefined"
              @input="setFieldError('regConfirm', '')"
            />
            <button
              type="button"
              class="absolute inset-y-0 right-0 flex items-center px-3 text-player-hint transition-colors duration-gd-fast hover:text-player-text"
              :aria-label="showRegConfirm ? '隐藏密码' : '显示密码'"
              @click="showRegConfirm = !showRegConfirm"
            >
              <svg class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
                <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" stroke-linecap="round" stroke-linejoin="round" />
                <circle cx="12" cy="12" r="3" />
                <path v-if="!showRegConfirm" d="M4 20 20 4" stroke-linecap="round" />
              </svg>
            </button>
            <p v-if="fieldErrors.regConfirm" id="reg-confirm-error" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.regConfirm }}</p>
          </div>

          <!-- 协议勾选 -->
          <div class="mt-4">
            <label class="flex cursor-pointer items-start gap-2 text-xs leading-relaxed text-player-hint">
              <input v-model="agreed" type="checkbox" class="mt-0.5 h-3.5 w-3.5 shrink-0 accent-[color:var(--gd-primary-400)]" @change="setFieldError('agreed', '')" />
              <span>
                我已阅读并同意
                <span class="text-primary-300" title="协议文本即将上线，当前无法查看">《用户协议》（即将上线）</span>
                与
                <span class="text-primary-300" title="隐私政策文本即将上线，当前无法查看">《隐私政策》（即将上线）</span>
              </span>
            </label>
            <p v-if="fieldErrors.agreed" class="mt-1.5 text-xs text-danger-bright" role="alert">{{ fieldErrors.agreed }}</p>
          </div>

          <span class="auth-submit mt-9 block">
            <AppButton type="submit" block size="lg" :loading="submitting">注 册</AppButton>
          </span>

          <p class="mt-5 text-center text-xs text-player-hint">
            已有账号？
            <button type="button" class="text-primary-300 hover:underline" @click="switchTab('login')">直接登录</button>
          </p>
        </form>
      </Transition>
    </main>
  </div>
</template>

<style scoped>
/* ========== 暗玻璃卡（web 玻璃拟态近似；无 backdrop-filter / 减少透明度时回退实底） ========== */
.auth-root {
  /* 认证舞台底色：比播放器域更深的近黑（对齐终稿 #0a0a12） */
  background-color: var(--gd-auth-stage);
}

/* ========== 暗玻璃卡（终稿：更低不透明度 + 更强 backdrop-blur + 极淡细边） ========== */
.auth-glass {
  background: var(--gd-auth-glass-bg);
  border: 1px solid var(--gd-auth-glass-border);
  box-shadow:
    var(--gd-shadow-4),
    inset 0 1px 0 rgba(255, 255, 255, 0.10),
    0 0 110px rgba(124, 92, 255, 0.18);
  backdrop-filter: blur(32px) saturate(140%);
  -webkit-backdrop-filter: blur(32px) saturate(140%);
  animation: auth-rise 0.75s var(--gd-ease-out) 0.15s backwards;
}

@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px))) {
  .auth-glass {
    background: rgba(23, 25, 31, 0.92);
  }
}

@media (prefers-reduced-transparency: reduce) {
  .auth-glass {
    background: #17191f;
    backdrop-filter: none;
    -webkit-backdrop-filter: none;
  }
}

/* 卡内输入域（终稿：全圆角 pill、深底 #16161f、不发光——焦点仅边框微亮保持可见性） */
.auth-glass .gd-input {
  height: 44px;
  padding: 0 20px;
  font-size: 15px;
  border-radius: 9999px;
}

.auth-glass .gd-input::placeholder {
  color: var(--gd-auth-placeholder);
}

.auth-glass .gd-input:hover:not(:disabled) {
  border-color: rgba(255, 255, 255, 0.16);
}

.auth-glass .gd-input:focus:not(:disabled) {
  border-color: rgba(255, 255, 255, 0.28);
  box-shadow: none;
}

/* 追光 ①：中心光池（参考图卡面较平：仅保留极淡的中央紫雾提亮） */
.auth-glass::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  background: radial-gradient(
    120% 82% at 50% 38%,
    rgba(139, 124, 255, 0.10),
    rgba(17, 26, 44, 0.14) 55%,
    transparent 82%
  );
  pointer-events: none;
}

/* 追光 ②：扫光束——一道斜向光带周期性扫过卡面（只动 transform，reduced-motion 下静止于画外） */
.auth-glass::after {
  content: '';
  position: absolute;
  top: -30%;
  bottom: -30%;
  left: 0;
  width: 45%;
  background: linear-gradient(
    100deg,
    transparent,
    rgba(255, 255, 255, 0.06) 42%,
    rgba(143, 126, 242, 0.12) 55%,
    transparent
  );
  transform: translateX(-180%) skewX(-12deg);
  animation: auth-sweep 8s var(--gd-ease-in-out) infinite;
  pointer-events: none;
}

@keyframes auth-sweep {
  0%,
  55% {
    transform: translateX(-180%) skewX(-12deg);
  }

  92%,
  100% {
    transform: translateX(360%) skewX(-12deg);
  }
}

/* ========== 分段式 Tab（二轮：文字整体降为暗灰，分割线几乎隐入卡片） ========== */
.auth-tabs {
  display: flex;
  margin: -6px -10px 34px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

@media (min-width: 640px) {
  .auth-tabs {
    margin: -12px -14px 36px;
  }
}

.auth-tab {
  flex: 1;
  height: 64px;
  font-size: 14px;
  font-weight: 400;
  color: #77778a;
  border-left: 1px solid rgba(255, 255, 255, 0.05);
  transition: color var(--gd-duration-fast) var(--gd-ease-standard);
}

.auth-tab:first-child {
  border-left: 0;
}

.auth-tab:hover {
  color: #a0a0b0;
}

.auth-tab-active {
  color: #8f8f9e;
}

/* ========== 组合验证码域（终稿：pill 形深底、发送改纯文字弱化存在感） ========== */
.auth-code {
  display: flex;
  align-items: center;
  height: 44px;
  padding: 0 10px 0 20px;
  background-color: var(--gd-auth-field-bg);
  border: 1px solid var(--gd-auth-field-border);
  border-radius: 9999px;
  outline: none;
  transition: border-color var(--gd-duration-fast) var(--gd-ease-standard);
}

.auth-code:hover {
  border-color: rgba(255, 255, 255, 0.16);
}

.auth-code:focus-within {
  border-color: rgba(255, 255, 255, 0.28);
}

.auth-code.gd-field-invalid,
.auth-code.gd-field-invalid:hover,
.auth-code.gd-field-invalid:focus-within {
  border-color: var(--gd-danger);
}

.auth-code input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: none;
  background: transparent;
  font-size: 15px;
  color: var(--gd-player-text);
}

.auth-code input::placeholder {
  color: var(--gd-auth-placeholder);
}

/* 发送验证码：纯文字 #55555f，弱化存在感 */
.auth-code-send {
  padding: 6px 8px;
  font-size: 13px;
  color: #55555f;
  white-space: nowrap;
  transition: color var(--gd-duration-fast) var(--gd-ease-standard);
}

.auth-code-send:hover:enabled {
  color: var(--gd-player-hint);
}

.auth-code-send:disabled {
  opacity: 0.55;
}

/* ========== 主按钮（二轮：外发光 +20%，全图唯一高亮光源） ========== */
.auth-submit button {
  height: 56px;
  border-radius: 16px;
  font-size: 16px;
  background: linear-gradient(135deg, #8b5cf6, #a78bfa);
  box-shadow:
    0 0 62px rgba(139, 92, 246, 0.58),
    0 0 130px rgba(124, 92, 255, 0.28),
    0 12px 32px -10px rgba(0, 0, 0, 0.60);
}

.auth-submit button:hover:enabled {
  background: linear-gradient(135deg, #9d74f8, #b49ffb);
  box-shadow:
    0 0 76px rgba(139, 92, 246, 0.66),
    0 0 150px rgba(124, 92, 255, 0.32),
    0 12px 32px -10px rgba(0, 0, 0, 0.60);
}

.auth-submit button:active:enabled {
  background: linear-gradient(135deg, #7c4dea, #9a86f8);
}

/* ========== 刊头（参考图还原：暖奶油衬线 + 柔和暖白辉光，字距放宽） ========== */
.auth-title {
  font-size: clamp(34px, 4.8vw, 70px);
  line-height: 1.08;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-indent: 0.08em;
  color: #f0ead8;
  text-shadow:
    0 0 10px rgba(242, 236, 217, 0.42),
    0 0 26px rgba(240, 234, 216, 0.28),
    0 0 70px rgba(167, 139, 250, 0.30);
}

/* 副标题（与中文标题同色同辉光，保留小字号与宽字距） */
.auth-caption {
  font-size: clamp(11px, 1.2vw, 17px);
  letter-spacing: 0.5em;
  text-indent: 0.5em;
  color: #f0ead8;
  text-shadow:
    0 0 10px rgba(242, 236, 217, 0.42),
    0 0 26px rgba(240, 234, 216, 0.28),
    0 0 70px rgba(167, 139, 250, 0.30);
}

.auth-title,
.auth-caption {
  animation: auth-drop 0.65s var(--gd-ease-out) backwards;
}

.auth-caption {
  animation-delay: 0.08s;
}

/* ========== 装饰层：双层辉光（中心 46% 视高，位于刊头与卡之间） + 颗粒 + 暗角 ========== */
/* ========== 装饰层容器：整体 1px 轻柔焦 + 降饱和（保留极光线条细节） ========== */
.auth-deco {
  filter: blur(1px) saturate(0.92);
}

/* ========== 薄雾层：轻微 backdrop 雾化（2px），仅柔化其下方的背景与装饰；刊头/表单在其上层不受影响 ========== */
.auth-fog {
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
  background: rgba(196, 190, 226, 0.035);
  pointer-events: none;
}

/* ========== 装饰层：紫色辉光（参考图：收敛柔雾，中心对齐表单卡 ~57% 视高） ========== */
.auth-glow {
  position: absolute;
  top: 57%;
  left: 50%;
  width: 640px;
  height: 640px;
  transform: translate(-50%, -50%);
  border-radius: 9999px;
  background: radial-gradient(
    closest-side,
    rgba(124, 92, 255, 0.20),
    rgba(124, 92, 255, 0.07) 55%,
    transparent 80%
  );
  pointer-events: none;
}

.auth-glow-core {
  position: absolute;
  top: 57%;
  left: 50%;
  width: 430px;
  height: 430px;
  transform: translate(-50%, -50%);
  border-radius: 9999px;
  background: radial-gradient(
    closest-side,
    rgba(167, 139, 250, 0.22),
    rgba(124, 92, 255, 0.10) 58%,
    transparent
  );
  pointer-events: none;
  animation: auth-breathe 6s var(--gd-ease-in-out) infinite;
}

@keyframes auth-breathe {
  0%,
  100% {
    opacity: 0.8;
  }

  50% {
    opacity: 1;
  }
}

/* 胶片颗粒（透明度 4%，落于规范 3-5% 区间） */
.auth-grain {
  position: absolute;
  inset: 0;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.5'/%3E%3C/svg%3E");
  opacity: 0.04;
  pointer-events: none;
}

/* 四角暗角加强：边缘再压 15-20%，视觉锁死中心 */
.auth-vignette {
  position: absolute;
  inset: 0;
  background: radial-gradient(120% 100% at 50% 44%, transparent 46%, rgba(0, 0, 0, 0.62) 100%);
  pointer-events: none;
}

/* ========== 光影条（参考图还原：白色光带 + 柔和暗谷，几何沿用视高标注） ==========
   暗段由多段缓坡渐变衔接（valley 过渡），不出现生硬分界线；仅 0.4px 柔化保持光带清晰。 */
.auth-streak {
  position: absolute;
  height: 10px;
  border-radius: 9999px;
  opacity: 0.9;
  pointer-events: none;
  filter: blur(0.4px);
  box-shadow: 0 0 12px rgba(255, 255, 255, 0.22);
  animation: auth-fade-in 0.8s var(--gd-ease-out) 0.55s backwards;
}

/* 右侧（标题右肩 16% 视高）：白色光带、右段柔和暗谷 */
.auth-streak-a {
  top: 16%;
  left: calc(50% + 175px);
  width: 180px;
  background: linear-gradient(
    90deg,
    rgba(255, 255, 255, 0) 0%,
    rgba(255, 255, 255, 0.55) 9%,
    rgba(255, 255, 255, 0.95) 30%,
    rgba(255, 255, 255, 0.98) 50%,
    rgba(255, 255, 255, 0.58) 57%,
    rgba(12, 12, 20, 0.88) 62%,
    rgba(12, 12, 20, 0.88) 65%,
    rgba(255, 255, 255, 0.52) 70%,
    rgba(255, 255, 255, 0.86) 80%,
    rgba(255, 255, 255, 0.30) 100%
  );
}

/* 左侧（26% 视高）：白色光带、中段柔和暗谷 */
.auth-streak-b {
  top: 26%;
  right: calc(50% + 194px);
  width: 148px;
  background: linear-gradient(
    90deg,
    rgba(255, 255, 255, 0) 0%,
    rgba(255, 255, 255, 0.85) 16%,
    rgba(255, 255, 255, 0.92) 26%,
    rgba(255, 255, 255, 0.5) 31%,
    rgba(12, 12, 20, 0.85) 35%,
    rgba(12, 12, 20, 0.85) 37.5%,
    rgba(255, 255, 255, 0.45) 42.5%,
    rgba(255, 255, 255, 0.9) 55%,
    rgba(255, 255, 255, 0.6) 100%
  );
}

/* 右下（79% 视高）：短弱白带、无截断、两端缓入缓出 */
.auth-streak-e {
  top: 79%;
  left: calc(50% + 215px);
  width: 90px;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(255, 255, 255, 0.5) 20%,
    rgba(255, 255, 255, 0.72) 50%,
    rgba(255, 255, 255, 0.5) 80%,
    transparent
  );
  opacity: 0.75;
}

/* 左下斜向光束：参考图左侧自上而下的微亮斜面（边界约 25° 倾角，柔边过渡）。
   高度 145%：旋转后元素四边全部退出视口（底边不再斜切左下角形成可见硬缝），
   仅保留对角边界进入画面；滑动沿自身横向往复（往返缓动 = 无缝循环，仅动 transform）。 */
.auth-beam {
  position: absolute;
  top: -2%;
  left: 0;
  width: 35%;
  height: 145%;
  background: linear-gradient(
    270deg,
    rgba(238, 236, 252, 0) 0%,
    rgba(238, 236, 252, 0.045) 12%,
    rgba(238, 236, 252, 0.06) 46%,
    rgba(238, 236, 252, 0.045) 100%
  );
  transform: rotate(25deg);
  transform-origin: 100% 0%;
  filter: blur(2px);
  pointer-events: none;
  animation: auth-beam-slide 9s var(--gd-ease-in-out) infinite alternate;
  will-change: transform;
}

@keyframes auth-beam-slide {
  from {
    transform: rotate(25deg) translate3d(0, 0, 0);
    opacity: 0.86;
  }

  to {
    transform: rotate(25deg) translate3d(6%, 0, 0);
    opacity: 1;
  }
}

/* ========== 极光光卡（参考图：12px 圆角、白紫光晕把照片融进夜色） ========== */
.auth-chip {
  position: absolute;
  top: 61%;
  width: clamp(120px, 8.5vw, 156px);
  height: clamp(120px, 8.5vw, 156px);
  border-radius: 12px;
  overflow: hidden;
  transform: translateY(-50%);
  isolation: isolate;
  border: 1px solid rgba(255, 255, 255, 0.12);
  /* 近场白紫光晕 + 远场星紫扩散，还原参考图光卡周围的发光雾 */
  box-shadow:
    0 0 22px 2px rgba(236, 232, 255, 0.38),
    0 0 64px 4px rgba(160, 150, 255, 0.30),
    0 0 130px 8px rgba(124, 92, 255, 0.20),
    inset 0 1px 0 rgba(255, 255, 255, 0.12);
}

/*
 * 光卡内容：还原参考图的真实极光摄影（aurora-left / aurora-right.jpg）。
 * GIF 式循环动效：照片层做极缓慢的漂移缩放 + 色相摆动（3s 往返缓动，
 * 起止状态一致 → 无跳帧），叠一层 screen 混色的极光色雾做色彩流动，
 * 两卡相位错开 1.5s 更接近真实极光的异步律动；仅动 transform/filter/opacity。
 */
.auth-chip-glow {
  position: absolute;
  inset: 0;
  background-size: cover;
  background-position: center;
  background-repeat: no-repeat;
  filter: saturate(1.06);
  opacity: 0.95;
  animation: auth-aurora-drift 3s var(--gd-ease-in-out) infinite alternate;
  will-change: transform;
}

.auth-chip-glow--left {
  background-image: url('../../assets/covers/aurora-left.jpg');
}

.auth-chip-glow--right {
  background-image: url('../../assets/covers/aurora-right.jpg');
  animation-delay: -1.5s;
}

/* 色彩流动叠层：极光色域内的柔光雾，screen 混色让照片色相缓慢流转
   （颜色变化全部由本层承担：纯 transform/opacity 动画，避免照片层逐帧 filter 重栅格化） */
.auth-chip-veil {
  position: absolute;
  inset: -12%;
  background:
    radial-gradient(52% 44% at 30% 26%, rgba(139, 124, 255, 0.32), transparent 70%),
    radial-gradient(46% 40% at 72% 68%, rgba(56, 217, 240, 0.24), transparent 72%),
    radial-gradient(38% 34% at 60% 22%, rgba(62, 232, 184, 0.16), transparent 74%);
  mix-blend-mode: screen;
  pointer-events: none;
  animation: auth-aurora-veil 3s var(--gd-ease-in-out) infinite alternate;
  will-change: transform, opacity;
}

.auth-chip-veil--right {
  animation-delay: -1.5s;
}

@keyframes auth-aurora-drift {
  from {
    transform: scale(1.05) translate3d(-2%, 1.2%, 0);
  }

  to {
    transform: scale(1.1) translate3d(2%, -1.5%, 0);
  }
}

@keyframes auth-aurora-veil {
  from {
    transform: translate3d(-3%, 2%, 0) scale(0.96);
    opacity: 0.55;
  }

  to {
    transform: translate3d(3%, -2.5%, 0) scale(1.05);
    opacity: 1;
  }
}

.auth-chip-left {
  left: calc(50% - 330px - clamp(120px, 8.5vw, 156px));
  animation:
    auth-fade-in 0.9s var(--gd-ease-out) 0.4s backwards,
    auth-float-left 7s var(--gd-ease-in-out) infinite;
}

.auth-chip-right {
  left: calc(50% + 330px);
  animation:
    auth-fade-in 0.9s var(--gd-ease-out) 0.4s backwards,
    auth-float-right 9s var(--gd-ease-in-out) infinite;
}

/* 漂浮（水平放置后仅上下浮动，无旋转） */
@keyframes auth-float-left {
  0%,
  100% {
    transform: translateY(-50%);
  }

  50% {
    transform: translateY(calc(-50% - 12px));
  }
}

@keyframes auth-float-right {
  0%,
  100% {
    transform: translateY(-50%);
  }

  50% {
    transform: translateY(calc(-50% - 14px));
  }
}

/* ========== 首映开场编排：刊头落下 → 卡片上浮 → 光卡/光带渐显（reduced-motion 全局降级） ========== */
@keyframes auth-drop {
  from {
    opacity: 0;
    transform: translateY(-14px);
  }

  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@keyframes auth-rise {
  from {
    transform: translateY(20px);
  }

  to {
    transform: translateY(0);
  }
}

@keyframes auth-fade-in {
  from {
    opacity: 0;
  }

  to {
    opacity: 1;
  }
}

/* ========== 登录 / 注册表单切换过渡（方向感知；reduced-motion 由全局降级接管） ========== */
.gd-auth-fwd-enter-active,
.gd-auth-fwd-leave-active,
.gd-auth-back-enter-active,
.gd-auth-back-leave-active {
  transition:
    opacity var(--gd-duration-normal) var(--gd-ease-out),
    transform var(--gd-duration-normal) var(--gd-ease-out);
}

.gd-auth-fwd-enter-from {
  opacity: 0;
  transform: translateX(16px);
}

.gd-auth-fwd-leave-to {
  opacity: 0;
  transform: translateX(-16px);
}

.gd-auth-back-enter-from {
  opacity: 0;
  transform: translateX(-16px);
}

.gd-auth-back-leave-to {
  opacity: 0;
  transform: translateX(16px);
}
</style>

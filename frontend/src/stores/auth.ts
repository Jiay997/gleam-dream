/**
 * 文件级注释：认证状态 store。
 * 持有 token / refreshToken / user 并持久化到 localStorage（与 http 拦截器通过
 * constants/storage 共享键名，避免循环依赖）；提供登录 / 注册 / 拉取用户 / 登出与偏好更新。
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { fetchMe as fetchMeApi, login as loginApi, logout as logoutApi, register as registerApi, updatePreferences as updatePreferencesApi } from '@/api/auth'
import type { LoginPayload, RegisterPayload } from '@/api/auth'
import type { User } from '@/api/types'
import { STORAGE_KEYS } from '@/constants/storage'

/**
 * 从 localStorage 安全读取字符串。
 * @param key 键名
 * @returns 值（不存在返回空串）
 */
function readString(key: string): string {
  try {
    return localStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

/**
 * 从 localStorage 安全读取用户对象。
 * @returns 用户对象或 null
 */
function readUser(): User | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.user)
    return raw ? (JSON.parse(raw) as User) : null
  } catch {
    return null
  }
}

/**
 * 认证 store：登录态、当前用户与偏好。
 */
export const useAuthStore = defineStore('auth', () => {
  /** 访问令牌 */
  const token = ref(readString(STORAGE_KEYS.token))
  /** 刷新令牌 */
  const refreshToken = ref(readString(STORAGE_KEYS.refreshToken))
  /** 当前用户 */
  const user = ref<User | null>(readUser())
  /** 请求进行中标记 */
  const loading = ref(false)

  /** 是否已登录（存在令牌） */
  const isLoggedIn = computed(() => token.value !== '')

  // 修复（E2E 报告问题 2）：HTTP 层静默续期只写 localStorage 而不改 store，
  // 导致页面用 authStore.token 构造的 SSE URL 长期沿用过期令牌、反复 401。
  // 这里监听续期事件，把新令牌同步进 store（computed URL 随之重建连接）。
  if (typeof window !== 'undefined') {
    window.addEventListener('gd:token-refreshed', (event: Event) => {
      const detail = (event as CustomEvent<{ accessToken?: string; refreshToken?: string }>).detail
      if (detail?.accessToken) token.value = detail.accessToken
      if (detail?.refreshToken) refreshToken.value = detail.refreshToken
    })
  }

  /**
   * 持久化用户信息到 localStorage。
   * @param value 用户对象或 null（null 表示清除）
   * @returns 无
   */
  function persistUser(value: User | null): void {
    try {
      if (value) {
        localStorage.setItem(STORAGE_KEYS.user, JSON.stringify(value))
      } else {
        localStorage.removeItem(STORAGE_KEYS.user)
      }
    } catch {
      // 存储不可用（隐私模式等）时静默降级为内存态
    }
  }

  /**
   * 写入登录凭证与用户信息并持久化。
   * @param accessToken 访问令牌
   * @param refresh 刷新令牌（可为空串）
   * @param profile 用户信息
   * @returns 无
   */
  function setAuth(accessToken: string, refresh: string, profile: User | null): void {
    token.value = accessToken
    refreshToken.value = refresh
    user.value = profile
    try {
      localStorage.setItem(STORAGE_KEYS.token, accessToken)
      if (refresh) {
        localStorage.setItem(STORAGE_KEYS.refreshToken, refresh)
      } else {
        localStorage.removeItem(STORAGE_KEYS.refreshToken)
      }
    } catch {
      // 忽略持久化失败
    }
    persistUser(profile)
  }

  /**
   * 清空本地登录态（不调用服务端）。
   * @returns 无
   */
  function clearLocal(): void {
    token.value = ''
    refreshToken.value = ''
    user.value = null
    try {
      localStorage.removeItem(STORAGE_KEYS.token)
      localStorage.removeItem(STORAGE_KEYS.refreshToken)
      localStorage.removeItem(STORAGE_KEYS.user)
    } catch {
      // 忽略
    }
  }

  /**
   * 登录（密码 / 短信双通道）。
   * @param payload 登录请求体（login_type/phone/password?/sms_code?）
   * @returns 当前用户
   * @throws ApiError 登录失败时由拦截器统一提示
   */
  async function login(payload: LoginPayload): Promise<User> {
    loading.value = true
    try {
      const result = await loginApi(payload)
      setAuth(result.access_token, result.refresh_token ?? '', result.user)
      return result.user
    } finally {
      loading.value = false
    }
  }

  /**
   * 注册并登录（手机号 + 验证码）。
   * @param payload 注册请求体（phone/sms_code/password?/nickname?）
   * @returns 当前用户
   */
  async function register(payload: RegisterPayload): Promise<User> {
    loading.value = true
    try {
      const result = await registerApi(payload)
      setAuth(result.access_token, result.refresh_token ?? '', result.user)
      return result.user
    } finally {
      loading.value = false
    }
  }

  /**
   * 拉取并刷新当前用户信息（GET /users/me：{ user, credit }）。
   * @returns 当前用户
   */
  async function fetchMe(): Promise<User> {
    const profile = await fetchMeApi()
    user.value = profile.user
    persistUser(profile.user)
    return profile.user
  }

  /**
   * 退出登录：先调服务端吊销，无论成败都清空本地态。
   * @returns 无
   */
  async function logout(): Promise<void> {
    try {
      await logoutApi()
    } finally {
      clearLocal()
    }
  }

  /**
   * 更新用户偏好（本地合并 + 服务端同步）。
   * @param prefs 偏好键值
   * @returns 无
   */
  async function updatePreferences(prefs: Record<string, unknown>): Promise<void> {
    const merged: Record<string, unknown> = { ...(user.value?.preferences ?? {}), ...prefs }
    const updated = await updatePreferencesApi(merged)
    user.value = updated
    persistUser(updated)
  }

  return {
    token,
    refreshToken,
    user,
    loading,
    isLoggedIn,
    setAuth,
    clearLocal,
    login,
    register,
    fetchMe,
    logout,
    updatePreferences,
  }
})

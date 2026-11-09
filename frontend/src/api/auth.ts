/**
 * 文件级注释：认证与用户资料域 API。
 * 与后端签名对齐（backend/app/api/auth.py、users.py）：
 * - POST /auth/sms-code  发送验证码（沙箱返回 dev_code）；
 * - POST /auth/login     密码/短信双通道登录；
 * - POST /auth/register  手机号注册（password/nickname 可选）；
 * - GET   /users/me      当前用户详情 + 积分账户摘要；
 * - PATCH /users/me      更新昵称/头像/偏好（preferences JSONB 浅合并）；
 * - POST /auth/logout    登出（服务端把 access/refresh 令牌加入黑名单）。
 */
import { STORAGE_KEYS } from '@/constants/storage'

import { http } from './http'
import type { LoginResult, User, UserProfile } from './types'

/** 发送验证码响应数据 */
export interface SmsCodeResult {
  /** 验证码有效期（秒） */
  expires_in: number
  /** 沙箱固定验证码（仅开发模式返回，前端可自动回填） */
  dev_code?: string | null
}

/** 登录请求体（login_type 决定 password / sms_code 二选一必填） */
export interface LoginPayload {
  /** 登录方式 */
  login_type: 'password' | 'sms'
  /** 手机号 */
  phone: string
  /** 密码（login_type=password 时必填） */
  password?: string
  /** 短信验证码（login_type=sms 时必填） */
  sms_code?: string
}

/** 注册请求体（password / nickname 可选） */
export interface RegisterPayload {
  /** 注册手机号 */
  phone: string
  /** 短信验证码（4~8 位数字） */
  sms_code: string
  /** 可选登录密码（6~64 位，设置后支持密码登录） */
  password?: string
  /** 可选昵称（缺省按手机号尾号生成） */
  nickname?: string
}

/** 更新个人资料请求体（PATCH /users/me，至少一项） */
export interface UpdateMePayload {
  /** 新昵称 */
  nickname?: string
  /** 新头像 URL */
  avatar_url?: string
  /** 偏好设置键值对（服务端 JSONB 浅合并） */
  preferences?: Record<string, unknown>
}

/**
 * 发送短信验证码。
 * @param phone 手机号
 * @returns 有效期与沙箱 dev_code
 * @throws ApiError 10004 请求过于频繁时由拦截器统一提示
 */
export function sendSmsCode(phone: string): Promise<SmsCodeResult> {
  return http.post<SmsCodeResult>('/auth/sms-code', { phone })
}

/**
 * 登录（密码 / 短信双通道）。
 * @param payload 登录请求体
 * @returns 双令牌与用户信息
 * @throws ApiError 20001 账号或密码错误 / 20004 账号被封禁
 */
export function login(payload: LoginPayload): Promise<LoginResult> {
  return http.post<LoginResult>('/auth/login', payload)
}

/**
 * 手机号注册（并直接登录）。
 * @param payload 注册请求体
 * @returns 双令牌与用户信息
 * @throws ApiError 10001 验证码错误 / 手机号已注册
 */
export function register(payload: RegisterPayload): Promise<LoginResult> {
  return http.post<LoginResult>('/auth/register', payload)
}

/**
 * 获取当前登录用户详情（含积分账户摘要）。
 * @returns { user, credit }
 * @throws ApiError 未登录（20001）时由拦截器跳转登录页
 */
export function fetchMe(): Promise<UserProfile> {
  return http.get<UserProfile>('/users/me')
}

/**
 * 更新当前用户资料（昵称 / 头像 / 偏好）。
 * @param payload 更新字段（至少一项）
 * @returns 更新后的用户信息
 * @throws ApiError 10001 无可更新字段
 */
export function updateMe(payload: UpdateMePayload): Promise<{ user: User }> {
  return http.patch<{ user: User }>('/users/me', payload)
}

/**
 * 更新当前用户偏好设置（新手引导 P02 采集结果走本方法）。
 * @param prefs 偏好键值（服务端 JSONB 浅合并）
 * @returns 更新后的用户信息
 */
export async function updatePreferences(prefs: Record<string, unknown>): Promise<User> {
  const { user } = await updateMe({ preferences: prefs })
  return user
}

/**
 * 退出登录（服务端吊销令牌，无状态 JWT 仅返回成功）。
 * @returns 无
 */
export function logout(): Promise<void> {
  let refreshToken = ''
  try {
    refreshToken = localStorage.getItem(STORAGE_KEYS.refreshToken) ?? ''
  } catch {
    refreshToken = ''
  }
  // 携带 refresh_token 让服务端一并加入黑名单，避免登出后仍可续期
  return http.post<void>('/auth/logout', refreshToken ? { refresh_token: refreshToken } : undefined)
}

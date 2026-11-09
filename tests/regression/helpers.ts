/**
 * 文件级注释：共享工具——API 登录与统一包络请求。
 *
 * 回归 spec 通过后端 REST API 造数/断言（比纯 UI 稳定且快）；
 * UI 路径（core-path.spec）则驱动真实浏览器。
 */
import { APIRequestContext, request as pwRequest } from '@playwright/test'

/** 后端 API 基址 */
export const API_BASE = process.env.API_BASE ?? 'http://localhost:8100/api/v1'
/** 测试账号手机号 */
export const QA_PHONE = process.env.QA_PHONE ?? '13900009903'
/** 测试账号密码 */
export const QA_PASSWORD = process.env.QA_PASSWORD ?? 'E2ePass123456'
/** 保真度断言默认项目 */
export const REG_PROJECT = Number(process.env.REG_PROJECT ?? '1410')

/** 登录并返回 { token, request }（request 已带 Authorization 头）。 */
export async function apiLogin(base?: APIRequestContext): Promise<{
  token: string
  request: APIRequestContext
}> {
  const ctx = base ?? (await pwRequest.newContext())
  const resp = await ctx.post(`${API_BASE}/auth/login`, {
    data: { login_type: 'password', phone: QA_PHONE, password: QA_PASSWORD },
  })
  if (!resp.ok()) {
    throw new Error(`登录失败: ${resp.status()} ${await resp.text()}`)
  }
  const body = await resp.json()
  const token: string = body.data.access_token
  const request = base ?? (await pwRequest.newContext())
  return { token, request }
}

/** 发起带鉴权的 API GET，返回统一包络 data。 */
export async function apiGet<T = unknown>(
  request: APIRequestContext,
  token: string,
  path: string,
): Promise<T> {
  const resp = await request.get(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!resp.ok()) {
    throw new Error(`GET ${path} 失败: ${resp.status()} ${await resp.text()}`)
  }
  const body = await resp.json()
  return body.data as T
}

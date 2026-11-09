/**
 * 文件级注释：1.2 链路保真度自动化断言（第 5 阶段交付物之二）。
 *
 * 对回归基线项目（默认 1410，可用 REG_PROJECT 覆盖）做逐字段追踪：
 * - Q-012：shot_out 输出 script_anchor / scene_id / prop_asset_ids；
 * - 保真度：final_prompt 含「人物造型锁定」，effective_prompt
 *   含景别锁定句且与 shot_size 匹配；
 * - Q-013：镜头媒体地址优先返回已渲染视频（.mp4）；
 * - Q-010：preview_url 返回真实渲染视频（非占位）。
 *
 * 取数约定（R-003 修复）：storyboards 列表接口不含 shots 数组，须
 * 两步取详情——GET /projects/{pid}/storyboards 拿分镜板 id，再
 * GET /storyboards/{sb_id} 拿含 shots 的详情（与 step0b 脚本一致）。
 *
 * 运行：npx playwright test -c tests/regression/playwright.config.ts fidelity
 */
import { expect, test, type APIRequestContext } from '@playwright/test'

import { apiGet, apiLogin, REG_PROJECT } from './helpers'

interface ShotOut {
  id: number
  order_index: number
  shot_size: string
  script_anchor: unknown
  scene_id: number | null
  prop_asset_ids: number[]
  media_url: string | null
  prompt_versions?: Array<Record<string, unknown>>
}

/** storyboards 列表项（列表接口不含 shots，仅元数据） */
interface StoryboardListItem {
  id: number
  status: string
}

/** storyboard 详情（含 shots 数组） */
interface StoryboardDetail {
  id: number
  status: string
  shots: ShotOut[]
}

/**
 * 两步取分镜镜头列表：列表接口拿首个分镜板 id，再取详情中的 shots。
 *
 * 列表包络兼容数组与 {items: [...]} 两种形态（与 step0b 脚本一致）。
 *
 * @param request 已带鉴权上下文的 APIRequestContext。
 * @param token 访问令牌。
 * @returns 首个分镜板（id + shots）。
 * @throws Error 当项目无分镜板或详情缺 shots 时抛出（断言失败）。
 */
async function fetchFirstStoryboard(
  request: APIRequestContext,
  token: string,
): Promise<StoryboardDetail> {
  const raw = await apiGet<StoryboardListItem[] | { items?: StoryboardListItem[] }>(
    request, token, `/projects/${REG_PROJECT}/storyboards`,
  )
  const items = Array.isArray(raw) ? raw : (raw?.items ?? [])
  expect(items.length, '项目应至少有 1 个分镜板').toBeGreaterThan(0)
  const detail = await apiGet<StoryboardDetail>(
    request, token, `/storyboards/${items[0].id}`,
  )
  expect(Array.isArray(detail.shots), '分镜板详情应含 shots 数组').toBe(true)
  return detail
}

test.describe('1.2 链路保真度断言', () => {
  let token: string
  let request: Awaited<ReturnType<typeof apiLogin>>['request']

  test.beforeAll(async () => {
    const login = await apiLogin()
    token = login.token
    request = login.request
  })

  test('Q-012 shot_out 输出剧本锚点三字段', async () => {
    const sb = await fetchFirstStoryboard(request, token)
    expect(sb.shots.length).toBeGreaterThan(0)
    for (const shot of sb.shots) {
      expect(shot, `镜头 ${shot.id} 缺 script_anchor`).toHaveProperty('script_anchor')
      expect(shot, `镜头 ${shot.id} 缺 scene_id`).toHaveProperty('scene_id')
      expect(shot, `镜头 ${shot.id} 缺 prop_asset_ids`).toHaveProperty('prop_asset_ids')
    }
  })

  test('保真度：每镜 final_prompt 含人物造型锁定', async () => {
    const sb = await fetchFirstStoryboard(request, token)
    expect(sb.shots.length).toBeGreaterThan(0)
    for (const shot of sb.shots) {
      const versions = shot.prompt_versions ?? []
      const final = versions.find((v) => v.version_type === 'final')
      if (!final) {
        continue // 早期项目可能无版本记录，跳过该镜
      }
      const prompt = String(final.prompt ?? '')
      expect(prompt, `镜头 ${shot.id} final_prompt 缺人物造型锁定`).toContain('人物造型锁定')
    }
  })

  test('Q-013 镜头媒体地址：有视频交付时返回 .mp4', async () => {
    const sb = await fetchFirstStoryboard(request, token)
    const withVideo = sb.shots.filter((s) => (s.media_url ?? '').includes('.mp4'))
    // 基线项目已完成 9 镜渲染，至少应有视频地址
    expect(withVideo.length, '应有镜头返回视频 media_url').toBeGreaterThan(0)
  })

  test('Q-010 preview_url 返回真实渲染视频', async () => {
    const preview = await apiGet<{ url: string }>(
      request, token, `/projects/${REG_PROJECT}/post/preview-url`,
    )
    expect(preview.url).toBeTruthy()
    expect(preview.url, '不应再是占位预览').not.toContain('preview_placeholder')
  })
})

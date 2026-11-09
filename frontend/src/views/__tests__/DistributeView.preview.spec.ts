/**
 * 文件级注释：分发页母版成片预览单元测试。
 * 覆盖本次修复：监视器加载真实导出 MP4（含合成预览兜底）、播放/暂停、进度实时同步、
 * 进度条点击与拖拽跳转，以及成片加载失败时回退静态封面。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ExportJobItem } from '@/api/export'
import { useToast } from '@/composables/useToast'
import { firePointer } from '@/test/events'

/** 导出 / 授权 / 分发接口桩 */
const exportApi = vi.hoisted(() => ({
  listExports: vi.fn(),
  listPlatformAccounts: vi.fn(),
  listDistributions: vi.fn(),
  createExport: vi.fn(),
  batchExport: vi.fn(),
  generateExportMetadata: vi.fn(),
  createPlatformAccount: vi.fn(),
  createDistribution: vi.fn(),
  getExport: vi.fn(),
}))

/** 套餐权益接口桩 */
const billingApi = vi.hoisted(() => ({
  listPlans: vi.fn(),
}))

/** post 域接口桩（成片时长 / 合成预览兜底地址） */
const postApi = vi.hoisted(() => ({
  getProjectTimeline: vi.fn(),
  getPostPreviewUrl: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { projectId: '1410' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

vi.mock('@/api/export', () => exportApi)

vi.mock('@/api/billing', () => billingApi)

vi.mock('@/api/post', () => postApi)

vi.mock('@/composables/useSSE', () => ({
  useSSE: vi.fn(),
}))

vi.mock('@/stores/project', () => ({
  useProjectStore: () => ({
    currentProject: {
      id: 1410,
      title: '预览测试项目',
      status: 'completed',
      aspect_ratio: '9:16',
      target_episode_count: 1,
    },
  }),
}))

import DistributeView from '../DistributeView.vue'

/** 成功导出的任务夹具（产物为真实 MP4） */
const SUCCEEDED_JOB: ExportJobItem = {
  id: 233,
  project_id: 1410,
  resolution: '1080p',
  has_watermark: false,
  format: 'mp4',
  status: 'succeeded',
  progress: 100,
  output_size_bytes: 7_100_000,
  output: {
    media_file_id: 2880,
    object_key: 'exports/233/final.mp4',
    media_type: 'video',
    size_bytes: 7_100_000,
    download_url: '/files/exports/233/final.mp4',
  },
}

/** 合成预览兜底地址 */
const PREVIEW_URL = '/files/renders/final/1410/out.mp4'
/** 成片时长（秒，与 jsdom 媒体桩默认 duration 一致） */
const FILM_DURATION_SECONDS = 45.5

/**
 * 配置各域接口的默认返回值。
 * @param jobs 导出任务列表（默认一条成功任务）
 * @returns 无
 */
function setupApis(jobs: ExportJobItem[] = [SUCCEEDED_JOB]): void {
  exportApi.listExports.mockResolvedValue({ items: jobs, total: jobs.length })
  exportApi.listPlatformAccounts.mockResolvedValue([])
  exportApi.listDistributions.mockResolvedValue({ items: [], total: 0 })
  billingApi.listPlans.mockResolvedValue({
    items: [{ key: 'free', benefits: { max_resolution: '1080p' } }],
    subscription: { plan_key: 'free' },
  })
  postApi.getProjectTimeline.mockResolvedValue({
    timeline: { id: 1, cut_type: 'preview', status: 'approved', duration_ms: 45500, fps: 24, resolution: '1080p' },
    clips: [],
  })
  postApi.getPostPreviewUrl.mockResolvedValue({ url: PREVIEW_URL })
}

/**
 * 挂载分发页并等待接口与监视器地址就绪。
 * @param jobs 导出任务列表（默认一条成功任务）
 * @returns 已挂载的组件包装器
 */
async function mountView(jobs: ExportJobItem[] = [SUCCEEDED_JOB]) {
  setupApis(jobs)
  const wrapper = mount(DistributeView, {
    global: {
      plugins: [createPinia()],
      config: {
        // 测试环境不加载真实图标/弹窗副作用，静默组件解析告警
        warnHandler: () => undefined,
      },
    },
  })
  await flushPromises()
  await flushPromises()
  return wrapper
}

/**
 * 给进度条注入可控布局盒模型（jsdom 默认 width 为 0，无法换算比例）。
 * @param el 目标元素
 * @param width 宽度（像素）
 * @param left 左边距（像素）
 * @returns 无
 */
function stubRect(el: HTMLElement, width: number, left = 0): void {
  vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({
    x: left,
    y: 0,
    left,
    top: 0,
    right: left + width,
    bottom: 6,
    width,
    height: 6,
    toJSON: () => ({}),
  } as DOMRect)
}

/**
 * 获取母版监视器内的 video 元素（页面首个 video 即监视器成片）。
 * @param wrapper 组件包装器
 * @returns video 元素包装器
 */
function monitorVideoOf(wrapper: Awaited<ReturnType<typeof mountView>>) {
  const videos = wrapper.findAll('video')
  expect(videos.length).toBeGreaterThan(0)
  return videos[0]
}

describe('DistributeView · 母版成片预览', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('优先加载最近成功导出的 MP4，并隐藏静态封面', async () => {
    const wrapper = await mountView()

    const video = monitorVideoOf(wrapper)
    expect(video.attributes('src')).toBe('/files/exports/233/final.mp4')
    expect(video.attributes('preload')).toBe('metadata')
    expect(wrapper.find('.gd-cover-1').exists()).toBe(false)
  })

  it('没有成功导出时回退后端合成预览产物', async () => {
    const wrapper = await mountView([])

    expect(monitorVideoOf(wrapper).attributes('src')).toBe(PREVIEW_URL)
  })

  it('播放与暂停绑定真实 video，时码与进度条随 currentTime 同步', async () => {
    const wrapper = await mountView()
    const video = monitorVideoOf(wrapper)
    const videoEl = video.element as HTMLVideoElement

    await wrapper.get('button[aria-label="播放预览"]').trigger('click')
    expect(videoEl.paused).toBe(false)

    videoEl.currentTime = 5
    await video.trigger('timeupdate')

    const progress = wrapper.get('[aria-label="成片预览进度"]')
    expect(progress.attributes('aria-valuenow')).toBe('5000')
    expect(wrapper.text()).toContain('00:05.00')

    const fill = progress.get('div')
    expect(fill.attributes('style')).toContain(`width: ${((5 / FILM_DURATION_SECONDS) * 100).toFixed(2)}%`)

    await wrapper.get('button[aria-label="暂停预览"]').trigger('click')
    expect(videoEl.paused).toBe(true)
  })

  it('点击进度条按比例跳转成片播放位置', async () => {
    const wrapper = await mountView()
    const video = monitorVideoOf(wrapper)
    const progress = wrapper.get('[aria-label="成片预览进度"]')
    stubRect(progress.element as HTMLElement, 100)

    firePointer(progress.element, 'pointerdown', 50)
    await nextTick()

    expect(progress.attributes('aria-valuenow')).toBe('22750')
    expect((video.element as HTMLVideoElement).currentTime).toBeCloseTo(FILM_DURATION_SECONDS / 2, 5)
  })

  it('拖拽进度条连续跳转，松开后保持最后位置', async () => {
    const wrapper = await mountView()
    const video = monitorVideoOf(wrapper)
    const progress = wrapper.get('[aria-label="成片预览进度"]')
    const trackEl = progress.element as HTMLElement
    stubRect(trackEl, 100)

    firePointer(trackEl, 'pointerdown', 20)
    firePointer(trackEl, 'pointermove', 80)
    firePointer(trackEl, 'pointerup', 80)
    await nextTick()

    expect(progress.attributes('aria-valuenow')).toBe('36400')
    expect((video.element as HTMLVideoElement).currentTime).toBeCloseTo(FILM_DURATION_SECONDS * 0.8, 5)
  })

  it('成片加载失败时回退静态封面并给出提示', async () => {
    const wrapper = await mountView()
    const video = monitorVideoOf(wrapper)

    await video.trigger('error')
    await flushPromises()

    expect(wrapper.find('.gd-cover-1').exists()).toBe(true)
    expect(wrapper.findAll('video').length).toBe(1)
    expect(useToast().items.some((item) => item.message === '母版成片加载失败，已回退静态封面')).toBe(true)
  })
})

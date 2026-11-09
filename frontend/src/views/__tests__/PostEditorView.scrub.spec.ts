/**
 * 文件级注释：后期编辑器时间线标尺（进度条）定位单元测试。
 * 覆盖本次修复的三点：标尺可点击跳转、可拖拽连续定位、键盘逐帧定位，
 * 以及定位后成片监看视频的 currentTime 同步与边界夹取（不影响既有片段编辑逻辑）。
 */
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { TimelineData } from '@/api/post'
import { firePointer } from '@/test/events'

/** post 域接口桩（仅覆盖挂载时会调用的方法，其余在 mock 工厂内以空实现兜底） */
const postApi = vi.hoisted(() => ({
  getProjectTimeline: vi.fn(),
  getPostPreviewUrl: vi.fn(),
  listSubtitleStyles: vi.fn(),
  recommendBgm: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { projectId: '1410' }, query: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

vi.mock('@/api/post', async () => {
  const actual = await vi.importActual<typeof import('@/api/post')>('@/api/post')
  return {
    ...actual,
    getProjectTimeline: postApi.getProjectTimeline,
    getPostPreviewUrl: postApi.getPostPreviewUrl,
    listSubtitleStyles: postApi.listSubtitleStyles,
    recommendBgm: postApi.recommendBgm,
    saveProjectTimeline: vi.fn(),
    applySubtitleStyle: vi.fn(),
    composeFinal: vi.fn(),
    previewTts: vi.fn(),
    startFullTts: vi.fn(),
    updateBgm: vi.fn(),
  }
})

import PostEditorView from '../PostEditorView.vue'

/** 时间线夹具：45.5s / 24fps，四轨各一条片段（保证标尺与轨道体正常渲染） */
const TIMELINE: TimelineData = {
  timeline: {
    id: 1,
    cut_type: 'preview',
    status: 'approved',
    duration_ms: 45500,
    fps: 24,
    resolution: '1080p',
  },
  clips: [
    { id: 1, track_type: 'video', start_ms: 0, end_ms: 20000, thumb_url: '/files/frames/1.jpg' },
    { id: 2, track_type: 'audio', start_ms: 0, end_ms: 20000 },
    { id: 3, track_type: 'bgm', start_ms: 0, end_ms: 45500 },
    { id: 4, track_type: 'subtitle', start_ms: 0, end_ms: 5000, content: '测试字幕' },
  ],
}

/** 标尺 CSS 宽度（像素；50px = 1s，对应组件内 pps 默认值） */
const RULER_WIDTH = 2400

/**
 * 给元素注入可控布局盒模型（jsdom 默认全 0，无法换算指针位置）。
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
    bottom: 24,
    width,
    height: 24,
    toJSON: () => ({}),
  } as DOMRect)
}

/**
 * 挂载后期编辑器并等待时间线加载完成。
 * @param previewUrl 成片预览地址（空串表示无成片，此时不渲染监看视频）
 * @returns 已挂载的组件包装器
 */
async function mountEditor(previewUrl = '/files/renders/final/1410/out.mp4') {
  postApi.getProjectTimeline.mockResolvedValue(TIMELINE)
  postApi.getPostPreviewUrl.mockResolvedValue({ url: previewUrl })
  postApi.listSubtitleStyles.mockResolvedValue([])
  postApi.recommendBgm.mockResolvedValue([])
  const wrapper = mount(PostEditorView, {
    global: {
      config: {
        // 测试环境未注册 vue-konva，静默其组件解析告警
        warnHandler: () => undefined,
      },
    },
  })
  await flushPromises()
  return wrapper
}

describe('PostEditorView · 时间线标尺定位', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('标尺为可交互滑块：移除 aria-hidden 并暴露总时长与当前位置', async () => {
    const wrapper = await mountEditor()
    const ruler = wrapper.get('[role="slider"][aria-label^="时间线进度"]')

    expect(ruler.attributes('aria-hidden')).toBeUndefined()
    expect(ruler.attributes('tabindex')).toBe('0')
    expect(ruler.attributes('aria-valuemax')).toBe('45500')
    expect(ruler.attributes('aria-valuenow')).toBe('0')
  })

  it('点击标尺跳转播放头，并同步成片监看视频 currentTime', async () => {
    const wrapper = await mountEditor()
    const ruler = wrapper.get('[role="slider"][aria-label^="时间线进度"]')
    stubRect(ruler.element as HTMLElement, RULER_WIDTH)

    firePointer(ruler.element, 'pointerdown', 300)
    await nextTick()

    expect(ruler.attributes('aria-valuenow')).toBe('6000')
    const video = wrapper.get('video').element as HTMLVideoElement
    expect(video.currentTime).toBeCloseTo(6, 5)
  })

  it('按住并拖动标尺连续定位，松开后保持一致', async () => {
    const wrapper = await mountEditor()
    const ruler = wrapper.get('[role="slider"][aria-label^="时间线进度"]')
    stubRect(ruler.element as HTMLElement, RULER_WIDTH)

    firePointer(ruler.element, 'pointerdown', 300)
    firePointer(ruler.element, 'pointermove', 900)
    firePointer(ruler.element, 'pointerup', 900)
    await nextTick()

    expect(ruler.attributes('aria-valuenow')).toBe('18000')
    const video = wrapper.get('video').element as HTMLVideoElement
    expect(video.currentTime).toBeCloseTo(18, 5)
  })

  it('定位结果夹取在 [0, 总时长] 内', async () => {
    const wrapper = await mountEditor()
    const ruler = wrapper.get('[role="slider"][aria-label^="时间线进度"]')
    stubRect(ruler.element as HTMLElement, RULER_WIDTH, 100)

    firePointer(ruler.element, 'pointerdown', 9999)
    await nextTick()
    expect(ruler.attributes('aria-valuenow')).toBe('45500')

    firePointer(ruler.element, 'pointermove', 0)
    await nextTick()
    expect(ruler.attributes('aria-valuenow')).toBe('0')
  })

  it('键盘 ←/→ 逐帧定位，Shift 加速 10 帧', async () => {
    const wrapper = await mountEditor()
    const ruler = wrapper.get('[role="slider"][aria-label^="时间线进度"]')

    await ruler.trigger('keydown', { key: 'ArrowRight' })
    expect(ruler.attributes('aria-valuenow')).toBe('42')

    await ruler.trigger('keydown', { key: 'ArrowRight', shiftKey: true })
    expect(ruler.attributes('aria-valuenow')).toBe('459')
  })

  it('无可播放成片时仍可定位播放头（仅同步时间线状态，不报错）', async () => {
    const wrapper = await mountEditor('')
    const ruler = wrapper.get('[role="slider"][aria-label^="时间线进度"]')
    stubRect(ruler.element as HTMLElement, RULER_WIDTH)

    expect(wrapper.find('video').exists()).toBe(false)
    firePointer(ruler.element, 'pointerdown', 600)
    await nextTick()

    expect(ruler.attributes('aria-valuenow')).toBe('12000')
  })
})

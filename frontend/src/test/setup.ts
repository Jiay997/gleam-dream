/**
 * 文件级注释：Vitest 单元测试环境补丁。
 * jsdom 的媒体元素 play/pause/currentTime/duration 均为空实现（play 会抛 Not implemented），
 * 这里补齐最小可用能力，使成片预览的播放、跳转与进度同步逻辑可在单测中被真实驱动。
 */

/** 媒体元素在单测中使用的私有状态（WeakMap 避免污染原型上的可枚举字段） */
interface MediaMockState {
  /** 媒体时长（秒，默认 45.5 与测试时间线一致） */
  duration: number
  /** 当前播放位置（秒） */
  currentTime: number
  /** 暂停态 */
  paused: boolean
}

/** 各媒体元素实例的状态表 */
const mediaStates = new WeakMap<HTMLMediaElement, MediaMockState>()

/**
 * 读取（不存在时初始化）媒体元素的单测状态。
 * @param el 媒体元素
 * @returns 该元素的媒体状态对象
 */
function stateOf(el: HTMLMediaElement): MediaMockState {
  let state = mediaStates.get(el)
  if (!state) {
    state = { duration: 45.5, currentTime: 0, paused: true }
    mediaStates.set(el, state)
  }
  return state
}

Object.defineProperty(HTMLMediaElement.prototype, 'duration', {
  configurable: true,
  /**
   * @returns 媒体时长（秒）
   */
  get(this: HTMLMediaElement): number {
    return stateOf(this).duration
  },
})

Object.defineProperty(HTMLMediaElement.prototype, 'currentTime', {
  configurable: true,
  /**
   * @returns 当前播放位置（秒）
   */
  get(this: HTMLMediaElement): number {
    return stateOf(this).currentTime
  },
  /**
   * @param value 目标播放位置（秒）
   * @returns 无
   */
  set(this: HTMLMediaElement, value: number): void {
    stateOf(this).currentTime = value
  },
})

Object.defineProperty(HTMLMediaElement.prototype, 'paused', {
  configurable: true,
  /**
   * @returns 是否处于暂停态
   */
  get(this: HTMLMediaElement): boolean {
    return stateOf(this).paused
  },
})

/**
 * 模拟开始播放：更新暂停态并派发 play 事件（触发组件内 @play 处理）。
 * @returns 已 resolve 的 Promise，兼容组件内 `video.play().catch(...)` 写法
 */
HTMLMediaElement.prototype.play = function play(this: HTMLMediaElement): Promise<void> {
  stateOf(this).paused = false
  this.dispatchEvent(new Event('play'))
  return Promise.resolve()
}

/**
 * 模拟暂停：更新暂停态并派发 pause 事件。
 * @returns 无
 */
HTMLMediaElement.prototype.pause = function pauseMedia(this: HTMLMediaElement): void {
  stateOf(this).paused = true
  this.dispatchEvent(new Event('pause'))
}

/**
 * 空实现，避免 jsdom 抛出 "Not implemented: HTMLMediaElement.load"。
 * @returns 无
 */
HTMLMediaElement.prototype.load = function loadMedia(): void {
  // jsdom 无真实媒体解码能力，单测无需加载资源
}

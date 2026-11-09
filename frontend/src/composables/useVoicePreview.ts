/**
 * 文件级注释：音色试听播放组合式函数（音色列表 / 音色详情页共用）。
 *
 * 只维护一个共享 `<audio>` 实例，统一处理：
 * - 懒加载音频地址：调用方既可直接传 URL（已持久化的样音），也可传异步加载器
 *   （列表项点击试听时先调用 ``POST /tts-voices/{id}/preview`` 合成音频）；
 * - 播放 / 暂停 / 切换音色 / 拖动进度；
 * - 播放进度（positionMs / durationMs / progress）与加载态、错误态；
 * - 切换音色时自动停止上一条音频，避免多条试听叠音；
 * - 作用域销毁（组件卸载）时释放音频实例。
 */

import { computed, onScopeDispose, ref, type ComputedRef, type Ref } from 'vue'

/** 音频地址来源：已就绪的 URL，或返回 URL 的异步加载器（如实时合成试听） */
export type VoiceAudioSource = string | (() => Promise<string>)

/** 音色试听播放器对外 API（音色列表 / 详情页按需解构使用） */
export interface VoicePreviewApi {
  /** 当前正在播放的音色 ID（null 表示未播放） */
  playingId: Ref<number | null>
  /** 当前已加载音频所属的音色 ID（暂停后仍指向该音色，用于列表进度归属） */
  audioOwnerId: Ref<number | null>
  /** 当前正在合成 / 加载音频的音色 ID（null 表示无加载任务） */
  loadingId: Ref<number | null>
  /** 播放进度（毫秒） */
  positionMs: Ref<number>
  /** 音频总时长（毫秒；未知时为 0） */
  durationMs: Ref<number>
  /** 播放进度比例 0–1 */
  progress: ComputedRef<number>
  /** 播放错误文案（空串表示无错误） */
  error: Ref<string>
  /**
   * 播放 / 暂停指定音色。
   * @param voiceId 音色 ID
   * @param source 音频 URL 或异步加载器
   * @param durationHintMs 时长提示（毫秒，音频元数据到达前的兜底）
   * @returns 无
   */
  toggle: (voiceId: number, source: VoiceAudioSource, durationHintMs?: number) => Promise<void>
  /**
   * 强制播放指定 URL（会替换当前音频；用于「重新生成样音」后立即试听）。
   * @param voiceId 音色 ID
   * @param url 音频地址
   * @param durationHintMs 时长提示（毫秒）
   * @returns 无
   */
  playUrl: (voiceId: number, url: string, durationHintMs?: number) => Promise<void>
  /**
   * 暂停当前音频（保留进度，可再次播放）。
   * @returns 无
   */
  pause: () => void
  /**
   * 跳转到指定播放位置。
   * @param positionMs 目标位置（毫秒）
   * @returns 无
   */
  seek: (positionMs: number) => void
  /**
   * 停止播放并清空当前音频与进度（切换页面 / 关闭试听时调用）。
   * @returns 无
   */
  stop: () => void
  /**
   * 判断指定音色是否正在播放。
   * @param voiceId 音色 ID
   * @returns 是否播放中
   */
  isPlaying: (voiceId: number) => boolean
  /**
   * 判断指定音色是否正在合成 / 加载音频。
   * @param voiceId 音色 ID
   * @returns 是否加载中
   */
  isLoading: (voiceId: number) => boolean
}

/**
 * 将毫秒格式化为 `m:ss` 播放时间。
 * @param ms 毫秒数
 * @returns 形如 `0:08` 的时间文案
 */
export function formatAudioTime(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '0:00'
  const totalSeconds = Math.round(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}

/**
 * 创建音色试听播放器。
 *
 * 使用约束：在组件 `setup` 中调用；组合式函数内部通过 `onScopeDispose`
 * 自动释放音频实例，无需调用方手动清理。
 *
 * @returns 播放状态与操作方法集合
 */
export function useVoicePreview(): VoicePreviewApi {
  /** 当前播放音色 ID */
  const playingId = ref<number | null>(null)
  /** 当前已加载音频所属音色 ID */
  const audioOwnerId = ref<number | null>(null)
  /** 当前加载音色 ID */
  const loadingId = ref<number | null>(null)
  /** 当前播放进度（毫秒） */
  const positionMs = ref(0)
  /** 当前音频总时长（毫秒） */
  const durationMs = ref(0)
  /** 播放错误文案 */
  const error = ref('')

  /** 共享音频实例 */
  let audio: HTMLAudioElement | null = null
  /** 当前音频对应的音色 ID */
  let currentId: number | null = null
  /** 当前音频地址（原始字符串，用于同曲判断） */
  let currentUrl = ''
  /** 请求序号：避免快速连点导致旧请求覆盖新播放 */
  let requestSeq = 0

  /** 播放进度比例（0–1） */
  const progress = computed(() => {
    if (durationMs.value <= 0) return 0
    return Math.min(1, Math.max(0, positionMs.value / durationMs.value))
  })

  /**
   * 确保音频实例存在并绑定事件（时间进度 / 元数据 / 播放结束 / 错误）。
   * @returns 音频实例
   */
  function ensureAudio(): HTMLAudioElement {
    if (audio) return audio
    const element = new Audio()
    element.preload = 'auto'

    element.addEventListener('timeupdate', () => {
      const current = audio
      if (!current) return
      positionMs.value = Math.round(current.currentTime * 1000)
      if (Number.isFinite(current.duration) && current.duration > 0) {
        durationMs.value = Math.round(current.duration * 1000)
      }
    })

    element.addEventListener('loadedmetadata', () => {
      const current = audio
      if (!current) return
      if (Number.isFinite(current.duration) && current.duration > 0) {
        durationMs.value = Math.round(current.duration * 1000)
      }
    })

    element.addEventListener('ended', () => {
      playingId.value = null
      positionMs.value = 0
      if (audio) {
        audio.currentTime = 0
      }
    })

    element.addEventListener('play', () => {
      playingId.value = currentId
    })

    element.addEventListener('pause', () => {
      if (playingId.value === currentId) {
        playingId.value = null
      }
    })

    element.addEventListener('error', () => {
      // 停止时 removeAttribute('src') 也会触发 error，此时 currentUrl 已清空，忽略即可
      if (!currentUrl) return
      playingId.value = null
      error.value = '音频加载失败，请重新生成试听'
    })

    audio = element
    return element
  }

  /**
   * 重置播放状态与音频源。
   * @returns 无
   */
  function stop(): void {
    requestSeq += 1
    const element = audio
    currentUrl = ''
    currentId = null
    audioOwnerId.value = null
    playingId.value = null
    loadingId.value = null
    positionMs.value = 0
    durationMs.value = 0
    if (element) {
      element.pause()
      element.removeAttribute('src')
      element.load()
    }
  }

  /**
   * 播放指定地址（同音色同地址时保留当前进度续播）。
   * @param voiceId 音色 ID
   * @param url 音频地址
   * @param durationHintMs 时长提示（毫秒）
   * @returns 无
   */
  async function playUrl(voiceId: number, url: string, durationHintMs = 0): Promise<void> {
    const element = ensureAudio()
    const sameTrack = currentId === voiceId && currentUrl === url

    if (!sameTrack) {
      if (currentId !== voiceId) {
        element.pause()
      }
      element.src = url
      currentId = voiceId
      currentUrl = url
      audioOwnerId.value = voiceId
      element.currentTime = 0
      positionMs.value = 0
      durationMs.value = durationHintMs > 0 ? durationHintMs : 0
    } else if (durationHintMs > durationMs.value) {
      durationMs.value = durationHintMs
    }

    error.value = ''
    playingId.value = voiceId
    try {
      await element.play()
    } catch {
      playingId.value = null
      error.value = '音频播放失败，请重试'
    }
  }

  /**
   * 播放 / 暂停指定音色（支持异步合成音频）。
   * @param voiceId 音色 ID
   * @param source 音频 URL 或异步加载器
   * @param durationHintMs 时长提示（毫秒）
   * @returns 无
   * @throws 不抛出：加载器异常会写入 error 状态，调用方仍可继续交互
   */
  async function toggle(voiceId: number, source: VoiceAudioSource, durationHintMs = 0): Promise<void> {
    error.value = ''

    if (playingId.value === voiceId && audio) {
      audio.pause()
      return
    }

    // 同一音色且已加载同一地址：从暂停位置续播
    if (
      typeof source === 'string' &&
      currentId === voiceId &&
      currentUrl === source &&
      audio &&
      audio.paused &&
      audio.currentTime > 0
    ) {
      playingId.value = voiceId
      try {
        await audio.play()
      } catch {
        playingId.value = null
        error.value = '音频播放失败，请重试'
      }
      return
    }

    requestSeq += 1
    const seq = requestSeq
    let url: string
    if (typeof source === 'function') {
      loadingId.value = voiceId
      try {
        url = await source()
      } catch {
        if (seq === requestSeq) {
          loadingId.value = null
          error.value = '试听音频生成失败，请重试'
        }
        return
      } finally {
        if (seq === requestSeq && loadingId.value === voiceId) {
          loadingId.value = null
        }
      }
    } else {
      url = source
    }

    // 期间用户已切换到别的音色：丢弃本次结果
    if (seq !== requestSeq) return
    await playUrl(voiceId, url, durationHintMs)
  }

  /**
   * 暂停当前音频。
   * @returns 无
   */
  function pause(): void {
    audio?.pause()
  }

  /**
   * 跳转到指定播放位置。
   * @param targetMs 目标位置（毫秒）
   * @returns 无
   */
  function seek(targetMs: number): void {
    if (!audio) return
    const maxMs = durationMs.value > 0 ? durationMs.value : 0
    const nextMs = Math.min(Math.max(0, targetMs), maxMs)
    audio.currentTime = nextMs / 1000
    positionMs.value = nextMs
  }

  /**
   * 判断指定音色是否正在播放。
   * @param voiceId 音色 ID
   * @returns 是否播放中
   */
  function isPlaying(voiceId: number): boolean {
    return playingId.value === voiceId
  }

  /**
   * 判断指定音色是否正在加载音频。
   * @param voiceId 音色 ID
   * @returns 是否加载中
   */
  function isLoading(voiceId: number): boolean {
    return loadingId.value === voiceId
  }

  // 组件卸载时释放音频实例，避免后台继续播放 / 内存泄漏
  onScopeDispose(() => {
    stop()
    audio = null
  })

  return {
    playingId,
    audioOwnerId,
    loadingId,
    positionMs,
    durationMs,
    progress,
    error,
    toggle,
    playUrl,
    pause,
    seek,
    stop,
    isPlaying,
    isLoading,
  }
}

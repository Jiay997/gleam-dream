/**
 * 文件级注释：分镜域展示常量（P12 画布 / P13 镜头抽屉 / P14 草稿预览共用）。
 * 单一来源：景别 / 运镜 / 情绪枚举的中文映射与情绪视觉色（key 与后端
 * DB CHECK 约束一致：backend/app/models/storyboard.py）。
 */
import { EMOTION, EMOTION_FALLBACK, type EmotionPalette } from './canvasTokens'
import type { OptionItem } from './options'

export type { EmotionPalette }

/** 画布镜头卡宽度（px，与 05 高保真文档 5.3 一致） */
export const CARD_W = 200

/** 画布镜头卡高度（px，含封面 200×112） */
export const CARD_H = 260

/** 景别选项（shot_size 6 值枚举） */
export const SHOT_SIZE_OPTIONS: OptionItem[] = [
  { key: 'extreme_wide', label: '大远景' },
  { key: 'wide', label: '远景' },
  { key: 'medium', label: '中景' },
  { key: 'close_up', label: '近景' },
  { key: 'extreme_close_up', label: '特写' },
  { key: 'over_shoulder', label: '过肩' },
]

/** 运镜选项（camera_move 9 值枚举） */
export const CAMERA_MOVE_OPTIONS: OptionItem[] = [
  { key: 'static', label: '固定' },
  { key: 'pan', label: '横摇' },
  { key: 'tilt', label: '纵摇' },
  { key: 'dolly_in', label: '推镜' },
  { key: 'dolly_out', label: '拉镜' },
  { key: 'tracking', label: '跟踪' },
  { key: 'orbit', label: '环绕' },
  { key: 'handheld', label: '手持' },
  { key: 'crane', label: '升降' },
]

/** 情绪标签选项（emotion_tag 8 值枚举） */
export const EMOTION_TAG_OPTIONS: OptionItem[] = [
  { key: 'calm', label: '平静' },
  { key: 'joy', label: '喜悦' },
  { key: 'anger', label: '愤怒' },
  { key: 'sadness', label: '悲伤' },
  { key: 'fear', label: '恐惧' },
  { key: 'surprise', label: '惊讶' },
  { key: 'tension', label: '紧张' },
  { key: 'romance', label: '浪漫' },
]

/** 情绪视觉色（画布域唯一来源为 `constants/canvasTokens.ts` 的 `EMOTION`，本文件仅做领域转发） */
export const EMOTION_COLORS: Record<string, EmotionPalette> = EMOTION

/** 情绪视觉色兜底（中性暖灰） */
export const EMOTION_COLORS_FALLBACK: EmotionPalette = EMOTION_FALLBACK

/**
 * 按枚举 key 查询中文标签（未收录回退原样 key）。
 * @param options 选项表
 * @param key 枚举 key
 * @returns 中文标签
 */
export function optionLabel(options: OptionItem[], key?: string | null): string {
  if (!key) return '—'
  return options.find((item) => item.key === key)?.label ?? key
}

/**
 * 按情绪 key 查询视觉色（未收录回退中性色）。
 * @param key 情绪枚举 key
 * @returns 视觉色
 */
export function emotionColors(key?: string | null): EmotionPalette {
  if (!key) return EMOTION_COLORS_FALLBACK
  return EMOTION_COLORS[key] ?? EMOTION_COLORS_FALLBACK
}

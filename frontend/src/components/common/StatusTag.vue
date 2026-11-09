<!--
  文件级注释：业务状态标签组件。
  按 statusTone 单一来源渲染任意后端状态：色点 + 图标 + 中文文案三重编码，
  颜色（前景 / 浅底 / 描边）随五态 tone 自动映射。

  variant 两档：
  - filled（默认）：浅底 + 描边 + 状态色文字。用于表格、抽屉等"数据密集"场景，
    浅底能在白底表格里把状态块与相邻文本区分开；
  - outline：透明底 + 状态色描边 + 状态色文字，去掉色块。
  - dark：夜窗深底专用（09 号「梦境窗口」）—— 彩底 α + bright 档亮字，无描边。
    浅底上的 strong 深字在 #0a0a12 夜窗底上不过 AA，故换 bright 档。
  两档都保留色点（色觉冗余编码），不因形态变化牺牲无障碍。
-->
<script setup lang="ts">
/**
 * StatusTag 状态标签。
 * props：
 * - status：后端状态字符串（如 script_review）；
 * - label：自定义覆盖文案（缺省用映射表标签）；
 * - size：sm/md 尺寸，默认 md；
 * - variant：filled 浅底填充 / outline 描边不填充 / dark 夜窗深底，默认 filled。
 * 未收录的状态回退为中性灰样式并原样展示状态值。
 */
import { computed } from 'vue'

import { statusInfo } from '@/constants/statusTone'

const props = withDefaults(
  defineProps<{
    /** 后端状态字符串（可缺省：字段缺失时兜底为「未知状态」中性灰，不再触发 prop 警告） */
    status?: string
    /** 覆盖文案 */
    label?: string
    /** 尺寸 */
    size?: 'sm' | 'md'
    /** 视觉形态 */
    variant?: 'filled' | 'outline' | 'dark'
  }>(),
  { status: '', label: '', size: 'md', variant: 'filled' },
)

/** 当前状态的视觉信息 */
const info = computed(() => statusInfo(props.status))

/**
 * 五态基色 → 浅底上的"可读加重版"映射（浅底彩色文字须用 strong 档才过 AA）。
 * 色点仍用高饱和基色（色觉冗余编码需保持辨识度），文字/描边图标改用 strong。
 */
const STRONG_TEXT: Record<string, string> = {
  'var(--gd-running)': 'var(--gd-info-strong)',
  'var(--gd-pending)': 'var(--gd-warning-strong)',
  'var(--gd-locked)': 'var(--gd-success-strong)',
  'var(--gd-failed)': 'var(--gd-danger-strong)',
  'var(--gd-blocked)': 'var(--gd-blocked-strong)',
}

/** 文字 / 图标前景色（浅底可读版） */
const textColor = computed(() => STRONG_TEXT[info.value.color] ?? info.value.color)

/** 夜窗深底上的 bright 档亮字（按五态基色映射） */
const BRIGHT_TEXT: Record<string, string> = {
  'var(--gd-running)': 'var(--gd-info-bright)',
  'var(--gd-pending)': 'var(--gd-warning-bright)',
  'var(--gd-locked)': 'var(--gd-success-bright)',
  'var(--gd-failed)': 'var(--gd-danger-bright)',
  'var(--gd-blocked)': 'var(--gd-blocked-bright)',
}

/** 夜窗深底上的彩底 α 档 */
const DARK_BG: Record<string, string> = {
  'var(--gd-running)': 'var(--gd-window-tone-bg-running)',
  'var(--gd-pending)': 'var(--gd-window-tone-bg-pending)',
  'var(--gd-locked)': 'var(--gd-window-tone-bg-locked)',
  'var(--gd-failed)': 'var(--gd-window-tone-bg-failed)',
  'var(--gd-blocked)': 'var(--gd-window-tone-bg-blocked)',
}

/** 展示文案（状态为空串/未收录时兜底「未知状态」，避免空白标签） */
const displayLabel = computed(() => (props.label ? props.label : info.value.label || '未知状态'))

/** 容器样式（outline 档只取描边与前景色，底色置空；dark 档彩底 α + bright 字 + 无描边） */
const tagStyle = computed(() => {
  if (props.variant === 'dark') {
    return {
      color: BRIGHT_TEXT[info.value.color] ?? info.value.color,
      backgroundColor: DARK_BG[info.value.color] ?? 'rgba(255, 255, 255, 0.08)',
      borderColor: 'transparent',
    }
  }
  return {
    color: textColor.value,
    backgroundColor: props.variant === 'outline' ? 'transparent' : info.value.bg,
    borderColor: info.value.border,
  }
})
</script>

<template>
  <span
    class="inline-flex max-w-full items-center gap-1.5 rounded-full border font-medium"
    :class="size === 'sm' ? 'px-2 py-0.5 text-caption' : 'px-2.5 py-1 text-caption'"
    :style="tagStyle"
  >
    <!-- 色点：色觉冗余编码之一 -->
    <span class="h-1.5 w-1.5 shrink-0 rounded-full" :style="{ backgroundColor: info.color }" aria-hidden="true" />
    <!-- 图标：色觉冗余编码之二 -->
    <svg class="h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
      <path :d="info.icon" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
    <!-- 文案：色觉冗余编码之三 -->
    <span class="truncate">{{ displayLabel }}</span>
  </span>
</template>

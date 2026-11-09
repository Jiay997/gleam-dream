<!--
  文件级注释：通用按钮组件（主题「极夜 · 极光」· 材质 v2「暗心彩边」）。
  材质来源：aurora §5.1 v2 —— 中心退到夜色 #04070F，身份全部交给一圈 2px 的受光边
  （primary 为横跨五义的光谱签名款）；文字用同族亮色。
  样式全部由设计系统的 .gd-btn 承担（含直接写 class 的原生按钮），此组件只做契约封装。

  对外契约（与旧版完全一致）：
  - variant：primary（主操作，一屏只该有一个，光谱款）/ secondary（次要，极青）/
    subtle（幽灵档）/ danger（危险，玫红）/ ghost（无填充）/ text（纯文字链）；
  - size：sm / md / lg（aurora 三档高度 32 / 40 / 48）；
  - loading：走 aria-busy，转圈由设计系统的 ::before 承担，文字自动隐去但宽度不变；
  - disabled / block。
-->
<script setup lang="ts">
/**
 * AppButton 通用按钮。
 * props：
 * - variant：视觉风格，默认 primary；
 * - size：尺寸 sm/md/lg，默认 md；
 * - loading：加载中，显示 spinner 并禁点；
 * - disabled：禁用；
 * - block：占满整行。
 * slot：default 按钮内容。
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 按钮视觉风格 */
    variant?: 'primary' | 'subtle' | 'secondary' | 'danger' | 'ghost' | 'text'
    /** 按钮尺寸 */
    size?: 'sm' | 'md' | 'lg'
    /** 加载中状态 */
    loading?: boolean
    /** 是否禁用 */
    disabled?: boolean
    /** 是否占满整行 */
    block?: boolean
  }>(),
  { variant: 'primary', size: 'md', loading: false, disabled: false, block: false },
)

/**
 * 风格 → 设计系统类名。
 * - primary / secondary / danger：暗心彩边三档（光谱 / 极青 / 玫红）
 * - ghost / subtle：不发光的白描边幽灵档（subtle 语义上是「次级但需强调」，
 *   材质链里必须留一个不发光的档，否则一屏全是灯管）
 * - text：纯文字链，不占按钮体积（表格行内操作用）
 *
 * 语义 tone 扩展（设计系统侧 gd-btn--mint / violet / magenta / amber / neutral）：
 * 需要表达「通过 / 生成 / 待办」等五义语义时，在 variant 之外手动追加对应类。
 */
const VARIANT_CLASSES: Record<string, string> = {
  primary: 'gd-btn gd-btn--primary',
  secondary: 'gd-btn gd-btn--secondary',
  subtle: 'gd-btn gd-btn--ghost',
  danger: 'gd-btn gd-btn--danger',
  ghost: 'gd-btn gd-btn--ghost',
  text: 'gd-btn-text',
}

/** 尺寸 → 设计系统类名（aurora 三档：32 / 40 / 48） */
const SIZE_CLASSES: Record<string, string> = {
  sm: 'gd-btn--sm',
  md: '',
  lg: 'gd-btn--lg',
}

/** 组合类名 */
const classes = computed(() => [
  VARIANT_CLASSES[props.variant],
  SIZE_CLASSES[props.size],
  props.block ? 'gd-btn--block' : '',
])
</script>

<template>
  <button
    type="button"
    :class="classes"
    :disabled="disabled || loading"
    :aria-busy="loading ? 'true' : undefined"
  >
    <slot />
  </button>
</template>

<style scoped>
/* 纯文字链档：不占按钮体积，但仍保留 32px 触控热区与可见焦点环 */
.gd-btn-text {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-height: 32px;
  padding: 0 4px;
  border: 0;
  border-radius: var(--gd-r-1);
  background: transparent;
  color: var(--gd-ink-brand);
  font-family: var(--gd-font-body);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: color var(--gd-dur-2) var(--gd-ease);
}

.gd-btn-text:hover {
  color: var(--gd-primary-900);
}

.gd-btn-text:disabled {
  color: var(--gd-ink-3);
  cursor: not-allowed;
}
</style>

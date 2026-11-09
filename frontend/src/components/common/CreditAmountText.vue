<!--
  文件级注释：积分金额文本组件。
  等宽数字字体（tabular-nums）+ 千分位格式化；正数绿 / 负数红 / 零中性，可关闭着色。
-->
<script setup lang="ts">
/**
 * CreditAmountText 积分金额文本。
 * props：
 * - amount：金额（积分），正负均可；
 * - showSign：正数是否显示"+"号，默认 false；
 * - colored：是否按正负着色，默认 true。
 */
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 积分金额 */
    amount: number
    /** 正数是否显示 + 号 */
    showSign?: boolean
    /** 是否按正负着色 */
    colored?: boolean
  }>(),
  { showSign: false, colored: true },
)

/** 千分位格式化 + 可选符号 */
const formatted = computed(() => {
  const abs = Math.abs(props.amount).toLocaleString('zh-CN')
  const sign = props.amount < 0 ? '-' : props.showSign && props.amount > 0 ? '+' : ''
  return `${sign}${abs}`
})

/** 颜色类（正负色阶走 -strong 档，浅底上满足 AA 4.5:1） */
const colorClass = computed(() => {
  if (!props.colored) return 'text-neutral-700'
  if (props.amount > 0) return 'text-success-strong'
  if (props.amount < 0) return 'text-danger-strong'
  return 'text-neutral-500'
})
</script>

<template>
  <span class="gd-num font-medium" :class="colorClass">{{ formatted }}</span>
</template>

<!--
  文件级注释：分段控件（SegmentedControl）· 方向 B 新增基础件。
  背景：原「我创建 / 我参与」「网格 / 列表」「画布 / 列表」「预览 / 母版」等分段切换
  在 6+ 个页面各自用裸 <button> 手写，激活样式与 hover 分寸不一致。
  本组件把该形态收敛为唯一实现：轨道底（neutral-100）+ 白色滑块（shadow-gd-1）+
  发丝描边，激活项白底 + 次级文字转主文字色，切换带 200ms 过渡。

  props：
  - modelValue：当前选中值（v-model）；
  - options：选项数组 [{ value, label, icon? }]，icon 传 24x24 svg path 字符串；
  - size：sm（28px）/ md（36px），默认 md；
  - block：是否占满整行；
  - ariaLabel：控件语义标签（无障碍必填建议）。
  emit：update:modelValue / change。
-->
<script setup lang="ts" generic="T extends string | number">
/**
 * SegmentedControl 分段控件（泛型值类型，兼容字符串与数字 key）。
 * 注：选项类型在组件内部声明；调用方直接传对象字面量数组即可。
 */
interface SegmentedOption<T> {
  /** 选项值 */
  value: T
  /** 显示文案 */
  label: string
  /** 可选图标（24x24 stroke path） */
  icon?: string
}

const props = withDefaults(
  defineProps<{
    /** 当前选中值 */
    modelValue: T
    /** 选项集 */
    options: SegmentedOption<T>[]
    /** 尺寸 */
    size?: 'sm' | 'md'
    /** 是否占满整行 */
    block?: boolean
    /** 无障碍标签 */
    ariaLabel?: string
  }>(),
  { size: 'md', block: false, ariaLabel: '分段切换' },
)

const emit = defineEmits<{
  /** v-model 更新 */
  'update:modelValue': [value: T]
  /** 值变化（附带原值） */
  change: [value: T]
}>()

/**
 * 选择某项（点击已选项不重复触发）。
 * @param value 目标值
 * @returns 无
 */
function select(value: T): void {
  if (value === props.modelValue) return
  emit('update:modelValue', value)
  emit('change', value)
}
</script>

<template>
  <div
    class="inline-flex items-center rounded-md border border-hairline bg-surface-inset p-0.5"
    :class="block ? 'flex w-full' : ''"
    role="group"
    :aria-label="ariaLabel"
  >
    <button
      v-for="option in options"
      :key="String(option.value)"
      type="button"
      class="inline-flex items-center justify-center gap-1.5 rounded-sm font-medium transition-[background-color,color,box-shadow] duration-gd-fast ease-gd-standard"
      :class="[
        size === 'sm' ? 'h-7 px-2.5 text-xs' : 'h-8 px-3 text-sm',
        block ? 'flex-1' : '',
        modelValue === option.value
          ? 'bg-surface-raised text-neutral-800 shadow-gd-1'
          : 'text-neutral-500 hover:text-neutral-700',
      ]"
      :aria-pressed="modelValue === option.value"
      @click="select(option.value)"
    >
      <svg
        v-if="option.icon"
        class="h-4 w-4 shrink-0"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        aria-hidden="true"
      >
        <path :d="option.icon" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
      {{ option.label }}
    </button>
  </div>
</template>

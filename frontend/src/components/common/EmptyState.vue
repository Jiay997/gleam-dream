<!--
  文件级注释：空状态占位组件（方向 B「纸感编辑风」）。
  方向 B v2 变更点：
  - 图标底 72px、圆角 22px（rounded-2xl 令牌），由"纯灰底"改为
    "台面色底 + 发丝描边"（v2 去阴影，靠底色差与描边分区）；
  - 新增 iconPath prop：传入 24x24 线性 SVG path 时优先渲染矢量图标。
    原因：原先用 emoji（📭🎬🔍）作空态图标，与全站线性 SVG 图标语言割裂，
    且 emoji 在 Windows / macOS 渲染差异大（诊断清单 A6 / H3）。emoji 仍作兜底保留；
  - 新增 actionVariant prop，默认 primary —— 修正原先引导按钮默认灰底
    导致"首次使用引导弱于次级操作"的主次倒挂；
  - 标题提升为 16px/600（text-title）。
  对外 props 与旧版兼容（仅新增可选 iconPath / actionVariant）。
-->
<script setup lang="ts">
/**
 * EmptyState 空状态。
 * props：
 * - icon：展示图标（emoji 或字符），默认 📭；iconPath 存在时不生效；
 * - iconPath：24x24 线性 SVG path（优先于 icon）；
 * - title：主标题；
 * - description：补充描述；
 * - actionText：操作按钮文案（不传则不渲染按钮）；
 * - actionVariant：操作按钮风格，默认 primary。
 * emit：action —— 点击操作按钮时触发。
 * slot：action —— 自定义操作区（优先于 actionText 按钮）；
 *       icon —— 矢量图标插槽（优先于 iconPath 与 icon，用于渲染图标库组件）。
 */
import AppButton from './AppButton.vue'

withDefaults(
  defineProps<{
    /** 展示图标（emoji / 字符兜底） */
    icon?: string
    /** 线性 SVG 图标 path（24x24 viewBox，优先于 icon） */
    iconPath?: string
    /** 主标题 */
    title: string
    /** 补充描述 */
    description?: string
    /** 操作按钮文案 */
    actionText?: string
    /** 操作按钮风格 */
    actionVariant?: 'primary' | 'subtle' | 'secondary'
  }>(),
  { icon: '📭', iconPath: '', description: '', actionText: '', actionVariant: 'primary' },
)

const emit = defineEmits<{
  /** 点击操作按钮 */
  action: []
}>()
</script>

<template>
  <div class="flex flex-col items-center justify-center px-4 py-16 text-center">
    <!-- 极光光斑：品牌极光语言的柔和光团（v5），置于图标底之后、内容之前 -->
    <div class="relative">
      <div class="gd-empty-aurora absolute -inset-6" aria-hidden="true" />
      <!-- 图标底：台面色 + 发丝描边（v2 去阴影，靠底色差与描边分区） -->
      <div
        class="relative flex h-[72px] w-[72px] items-center justify-center rounded-2xl border border-hairline bg-surface-canvas text-3xl"
        aria-hidden="true"
      >
        <svg v-if="iconPath" class="h-8 w-8 text-neutral-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
          <path :d="iconPath" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <slot v-else-if="$slots.icon" name="icon" />
        <template v-else>{{ icon }}</template>
      </div>
    </div>
    <h3 class="mt-5 text-title text-neutral-900">{{ title }}</h3>
    <!-- text-balance + max-w：中文按字断行，46ch 宽会把「天）」这种尾巴甩成单独一行，
         视觉上像掉了一行字。自动平衡换行让每行长度接近，空态才不会被一行孤字拖低质感。 -->
    <p v-if="description" class="mt-2 max-w-[42ch] text-balance text-caption leading-relaxed text-neutral-500">{{ description }}</p>
    <div v-if="$slots.action" class="mt-6">
      <slot name="action" />
    </div>
    <AppButton v-else-if="actionText" class="mt-6" :variant="actionVariant" size="md" @click="emit('action')">
      {{ actionText }}
    </AppButton>
  </div>
</template>

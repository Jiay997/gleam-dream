<!--
  文件级注释：P12 · Konva 镜头卡组件（画布 L2 层，200×260 卡片）。
  结构：Group（draggable）→ 白底圆角 Rect + 封面区（media_url 图片 cover 裁切，
  缺图时按情绪色渐变占位；zoom<0.5 降级纯色块）+ 镜号徽章 + StatusTag（色点+文案）
  + 景别/运镜行 + 情绪徽章 + 时长 + 对白摘要（两行截断）+ 锚点提示行。
  锁定态卡片描边绿色；选中 / 悬浮描边主色。
  性能约定：拖拽由 Konva 原生移动节点，拖结束才通过 dragend 事件向父级提交位置
  （避免每帧触发 Vue 响应式更新）；封面图经 useImage 缓存。
-->
<script setup lang="ts">
/**
 * ShotKonvaCard Konva 镜头卡。
 * props：
 * - shot：镜头数据；
 * - pos：卡片世界坐标 {x, y}；
 * - selected：是否选中（主色描边）；
 * - hovered：是否悬浮（浅主色描边）；
 * - draggable：是否可拖拽（分镜板锁定 / 空格平移态为 false）；
 * - zoom：当前画布缩放（<0.5 时封面降级纯色块）。
 * emit：select（点击卡片）/ dragend（拖拽结束，携带最终坐标）/ hover（悬浮状态变化）。
 */
import { computed } from 'vue'
import { useImage } from 'vue-konva'

import type { ShotItem } from '@/api/storyboard'
import { BRAND, INK, PLAYER, STATE, SURFACE, WINDOW } from '@/constants/canvasTokens'
import {
  CAMERA_MOVE_OPTIONS,
  EMOTION_TAG_OPTIONS,
  SHOT_SIZE_OPTIONS,
  emotionColors,
  optionLabel,
} from '@/constants/storyboard'
import { CARD_H, CARD_W } from '@/constants/storyboard'
import { statusInfo } from '@/constants/statusTone'

const props = defineProps<{
  /** 镜头数据 */
  shot: ShotItem
  /** 卡片世界坐标 */
  pos: { x: number; y: number }
  /** 是否选中 */
  selected: boolean
  /** 是否悬浮 */
  hovered: boolean
  /** 是否可拖拽 */
  draggable: boolean
  /** 当前画布缩放 */
  zoom: number
}>()

const emit = defineEmits<{
  /** 点击卡片 */
  select: [shotId: number | string]
  /** 拖拽结束（携带节点最终世界坐标） */
  dragend: [payload: { id: number | string; x: number; y: number }]
  /** 拖拽中（同步位置到 Vue 状态，防止配置回写导致回弹） */
  dragmove: [payload: { id: number | string; x: number; y: number }]
  /** 悬浮状态变化 */
  hover: [shotId: number | string | null]
}>()

/** 封面图异步加载（useImage 内部缓存 HTMLImageElement；空串与 .json 占位资源不触发加载） */
const [coverImage] = useImage(() => {
  const src = props.shot.media_url
  if (!src || /\.json(\?|#|$)/i.test(src)) return ''
  return src
})

/** 镜头状态视觉信息（StatusTag 同源：色点 + 中文文案） */
const status = computed(() => statusInfo(props.shot.status))

/** 情绪视觉色 */
const emotion = computed(() => emotionColors(props.shot.emotion_tag))

/** 镜头是否锁定 */
const locked = computed(() => props.shot.status === 'locked')

/** 卡片描边色：选中主色 > 悬浮浅主色 > 锁定绿 > 默认发丝描边（取值见 canvasTokens，与令牌同源） */
const strokeColor = computed(() => {
  if (props.selected) return BRAND.p500
  if (props.hovered) return BRAND.p400
  if (locked.value) return STATE.locked
  return INK.n200
})

/** 描边宽度：选中 2px，其余 1px */
const strokeWidth = computed(() => (props.selected ? 2 : 1))

/** 封面是否显示真实图片（有图且已解码成功、缩放 ≥0.5，低缩放降级色块省解码） */
const showImage = computed(
  () => Boolean(coverImage.value) && (coverImage.value?.naturalWidth ?? 0) > 0 && props.zoom >= 0.5,
)

/**
 * 封面图 cover 裁切绘制参数（等比放大取中）。
 * @returns 绘制区域 {x, y, width, height}
 */
const coverDraw = computed(() => {
  const img = coverImage.value
  if (!img || !img.width || !img.height) return { x: 0, y: 0, width: 200, height: 112 }
  const scale = Math.max(200 / img.width, 112 / img.height)
  const width = img.width * scale
  const height = img.height * scale
  return { x: (200 - width) / 2, y: (112 - height) / 2, width, height }
})

/** 对白 / 动作摘要（两行截断：fontSize 11 × 宽 184 ≈ 16 字/行） */
const summaryText = computed(() => {
  const text = (props.shot.dialogue || props.shot.action_desc || '').trim()
  if (!text) return '（暂无内容摘要）'
  const max = 32
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
})

/** 景别中文标签 */
const shotSizeLabel = computed(() => optionLabel(SHOT_SIZE_OPTIONS, props.shot.shot_size))

/** 运镜中文标签 */
const cameraMoveLabel = computed(() => optionLabel(CAMERA_MOVE_OPTIONS, props.shot.camera_move))

/** 时长展示（秒，1 位小数） */
const durationLabel = computed(() => `${(props.shot.duration_ms / 1000).toFixed(1)}s`)

/** 情绪中文标签 */
const emotionLabel = computed(() => (props.shot.emotion_tag ? optionLabel(EMOTION_TAG_OPTIONS, props.shot.emotion_tag) : '无情绪'))

/** 锚点数量提示（QC 失配 / 生成告警优先提示并标色） */
const anchorLabel = computed(() => {
  if (props.shot.anchor_qc?.mismatch) return '⚠ 画面与剧本跑偏'
  if ((props.shot.anchor_qc?.warnings?.length ?? 0) > 0) return '⚠ 分镜告警'
  const charCount = (props.shot.character_asset_ids ?? []).length
  const sceneCount = props.shot.scene_asset_id ? 1 : 0
  if (charCount + sceneCount === 0) return '⚠ 未绑锚点'
  return `锚点 ${charCount} 角色 · ${sceneCount} 场景`
})

/** 锚点提示行颜色：QC 失配用 danger，生成告警/未绑锚点用琥珀，其余弱文本色 */
const anchorLabelColor = computed(() => {
  if (props.shot.anchor_qc?.mismatch) return STATE.failed
  if ((props.shot.anchor_qc?.warnings?.length ?? 0) > 0) return STATE.pending
  if (anchorLabel.value.startsWith('⚠')) return STATE.pending
  return INK.n400
})

/**
 * Group 节点配置（拖拽结束前坐标仅存于 Konva 节点，不回写 Vue 状态）。
 * Konva 事件通过 config 的 onXxx 传入：v-group 根节点为 fragment，
 * 在模板上用 @click 会触发 Vue「Extraneous non-emits event listeners」警告。
 */
const groupConfig = computed(() => ({
  x: props.pos.x,
  y: props.pos.y,
  width: CARD_W,
  height: CARD_H,
  draggable: props.draggable,
  onClick,
  onDragend,
  onDragmove,
  onMouseenter,
  onMouseleave,
}))

/**
 * 点击卡片（拖拽刚结束的 click 事件由父级过滤）。
 * @returns 无
 */
function onClick(): void {
  emit('select', props.shot.id)
}

/**
 * 拖拽结束：读取 Konva 节点最终坐标并提交父级。
 * @param event Konva 拖拽结束事件
 * @returns 无
 */
function onDragend(event: { target: { x: () => number; y: () => number } }): void {
  emit('dragend', { id: props.shot.id, x: event.target.x(), y: event.target.y() })
}

/**
 * 拖拽中：同步节点坐标到父级状态（保证配置回写与节点一致，避免重渲染时回弹）。
 * @param event Konva 拖拽事件
 * @returns 无
 */
function onDragmove(event: { target: { x: () => number; y: () => number } }): void {
  emit('dragmove', { id: props.shot.id, x: event.target.x(), y: event.target.y() })
}

/**
 * 悬浮进入。
 * @returns 无
 */
function onMouseenter(): void {
  emit('hover', props.shot.id)
}

/**
 * 悬浮离开。
 * @returns 无
 */
function onMouseleave(): void {
  emit('hover', null)
}
</script>

<template>
  <v-group :config="groupConfig">
    <!-- 卡片底（暗底用 --gd-surface，不再是白纸；描边随选中 / 悬浮 / 锁定态变化；圆角 14 = --gd-radius-lg，与 DOM .gd-card 同源）
         不画投影：暗域里投影既看不见，又会因为 Konva 的 shadow 实现渗进形状内部，
         把 #111A2C 的卡面压成 #0C121F（实测暗 30%）。层级交给描边与封面光承担。 -->
    <v-rect
      :config="{
        x: 0,
        y: 0,
        width: CARD_W,
        height: CARD_H,
        cornerRadius: 14,
        fill: SURFACE.card,
        stroke: strokeColor,
        strokeWidth: strokeWidth,
      }"
    />

    <!-- 封面区 200×112（裁切 + 真图 / 夜窗占位（09 号：暗黑底 + 顶部极光）/ 低缩放色块） -->
    <v-group :config="{ x: 0, y: 0, width: 200, height: 112, clipX: 0, clipY: 0, clipWidth: 200, clipHeight: 112 }">
      <v-image v-if="showImage" :config="{ image: coverImage, ...coverDraw }" />
      <template v-else>
        <!-- 夜窗底：--gd-window-bg（情绪语义由卡片下方情绪徽章承担） -->
        <v-rect
          :config="{
            x: 0,
            y: 0,
            width: 200,
            height: 112,
            fill: WINDOW.bg,
            opacity: zoom >= 0.5 ? 1 : 0.5,
          }"
        />
        <!-- 顶部极光辉光（多档渐隐防切线，与 .gd-night-window::before 同配方） -->
        <v-rect
          :config="{
            x: 0,
            y: 0,
            width: 200,
            height: 112,
            fillRadialGradientStartPoint: { x: 100, y: -18 },
            fillRadialGradientEndPoint: { x: 100, y: -18 },
            fillRadialGradientStartRadius: 0,
            fillRadialGradientEndRadius: 170,
            fillRadialGradientColorStops: [0, `rgba(${WINDOW.aurora}, 0.5)`, 0.45, `rgba(${WINDOW.aurora}, 0.13)`, 1, `rgba(${WINDOW.aurora}, 0)`],
            opacity: zoom >= 0.5 ? 1 : 0.5,
          }"
        />
      </template>
      <!-- 镜号徽章（压在封面左上） -->
      <v-rect :config="{ x: 8, y: 8, width: 40, height: 20, cornerRadius: 5, fill: PLAYER.scrim }" />
      <v-text :config="{ x: 8, y: 12, width: 40, text: `S${shot.shot_no}`, align: 'center', fontSize: 11, fontStyle: 'bold', fill: INK.n0, fontFamily: 'DIN Alternate, monospace' }" />
    </v-group>

    <!-- 状态行：色点 + 状态文案（StatusTag 的 Konva 等价小尺寸） -->
    <v-circle :config="{ x: 15, y: 128, radius: 3, fill: status.color }" />
    <v-text :config="{ x: 24, y: 121, text: status.label, fontSize: 11, fill: status.color }" />
    <v-text :config="{ x: 192, y: 121, width: 76, align: 'right', text: durationLabel, fontSize: 11, fontStyle: 'bold', fill: INK.n700, fontFamily: 'DIN Alternate, monospace' }" />

    <!-- 景别 / 运镜行 -->
    <v-text :config="{ x: 10, y: 142, width: 180, text: `${shotSizeLabel} · ${cameraMoveLabel}`, fontSize: 11, fill: INK.n500 }" />

    <!-- 情绪徽章 -->
    <v-rect :config="{ x: 10, y: 162, width: 40, height: 17, cornerRadius: 8.5, fill: emotion.bg }" />
    <v-text :config="{ x: 10, y: 165, width: 48, align: 'center', text: emotionLabel, fontSize: 10, fill: emotion.color }" />

    <!-- 对白 / 动作摘要（两行截断） -->
    <v-text :config="{ x: 10, y: 190, width: 180, height: 32, text: summaryText, fontSize: 11, lineHeight: 1.35, fill: INK.n500, wrap: 'char' }" />

    <!-- 锚点提示行 -->
    <v-text :config="{ x: 10, y: 235, text: anchorLabel, fontSize: 10, fill: anchorLabelColor }" />
  </v-group>
</template>

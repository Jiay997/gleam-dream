<!--
  文件级注释：V6 · 合成编排页（路由 /projects/:projectId/composition，覆盖 F10）。
  v2：由「表格 + 列表」形态重构为画布式编排（交互形态对齐 dify workflow）：

  页面结构：
  - 顶栏：集号选择、刷新、从分镜同步、新增标题节点、回写分镜顺序；
  - 主区 = 左画布 + 右信息栏：
    · 画布（dify 形态）：网格点背景、滚轮以指针为中心缩放（0.4x~1.6x）、
      空白处拖拽平移；节点卡片（类型徽标 / 标签 / 镜号时长 / 输入输出锚点）
      可按住拖拽，松手后自动 PATCH ui_pos 持久化；主路径边以贝塞尔曲线连线；
      单击节点选中，右侧栏展示详情；
    · 右信息栏：编排摘要、结构守卫 N1~N8、选中节点详情（参数 JSON / 删除）、
      编译区（试渲 / 编译，plan_hash 幂等）、历史计划、多维锚点包。

  数据来源：`api/v6.ts` F10 分组。渲染锁定期写操作会被后端拒绝，页面原样
  展示错误提示（http 拦截器统一 toast）。
-->
<script setup lang="ts">
/**
 * CompositionView 合成编排页面（画布式）。
 *
 * 编排以“单一语义图”为事实源：画布上的节点 / 连线与轨道视图共享同一份
 * 节点 / 边数据，编译器按拓扑序生成确定性渲染计划。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  applyCompositionOrder,
  compileComposition,
  createCompositionNode,
  deleteCompositionNode,
  getCompositionGraph,
  listAnchorPacks,
  listCompositionPlans,
  syncComposition,
  updateCompositionNode,
} from '@/api/v6'
import type { AnchorPack, CompositionGraph, CompositionNode } from '@/api/v6'
import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useToast } from '@/composables/useToast'

const route = useRoute()
const router = useRouter()
const toast = useToast()

/** 节点卡片宽度（px，连线端点计算用） */
const NODE_W = 180
/** 节点卡片高度（px，连线端点计算用） */
const NODE_H = 88
/** 缩放范围 */
const ZOOM_MIN = 0.4
const ZOOM_MAX = 1.6

/** 当前项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))
/** 当前集号 */
const epNo = ref(1)
/** 编排图 */
const graph = ref<CompositionGraph | null>(null)
/** 最近编译结果 */
const compileResult = ref<{ plan_hash: string; reused: boolean } | null>(null)
/** 历史计划 */
const plans = ref<Array<Record<string, unknown>>>([])
/** 整集镜头锚点包 */
const anchorPacks = ref<AnchorPack[]>([])
/** 新增节点标签 */
const newNodeLabel = ref('片头标题')
/** 是否加载中 */
const loading = ref(false)
/** 是否同步中 */
const syncing = ref(false)
/** 是否保存节点坐标中 */
const savingPos = ref(false)

/* ==================== 画布视口状态 ==================== */

/** 画布根元素引用（指针中心缩放取矩形用） */
const canvasEl = ref<HTMLDivElement | null>(null)
/** 视口平移（屏幕像素） */
const pan = ref({ x: 40, y: 24 })
/** 视口缩放 */
const zoom = ref(1)
/** 当前选中节点 ID（null = 未选中） */
const selectedId = ref<number | null>(null)
/** 本地节点坐标覆盖（拖拽过程中的即时位置，key = 节点 ID） */
const positions = ref<Record<number, { x: number; y: number }>>({})

/** 平移拖拽会话（非响应式临时态） */
let panSession: { startX: number; startY: number; origX: number; origY: number } | null = null
/** 节点拖拽会话（非响应式临时态） */
let nodeSession: {
  id: number
  startX: number
  startY: number
  origX: number
  origY: number
  moved: boolean
} | null = null

/** 节点 ID → 节点映射（连线端点定位用） */
const nodeById = computed(() => new Map((graph.value?.nodes ?? []).map((n) => [n.id, n])))

/**
 * 读取节点在画布上的坐标：优先本地拖拽位置，其次后端 ui_pos，
 * 缺省按主路径顺序横向排布。
 * @param node 编排节点
 * @returns 世界坐标
 */
function posOf(node: CompositionNode): { x: number; y: number } {
  const local = positions.value[node.id]
  if (local) return local
  return {
    x: node.ui_pos?.x ?? 40 + (node.order_hint || 0) * (NODE_W + 60),
    y: node.ui_pos?.y ?? 160,
  }
}

/** 连线几何列表（贝塞尔路径，主路径高亮） */
const edgePaths = computed(() => {
  const map = nodeById.value
  return (graph.value?.edges ?? [])
    .map((edge) => {
      const from = map.get(edge.from_node_id)
      const to = map.get(edge.to_node_id)
      if (!from || !to) return null
      const p1 = { x: posOf(from).x + NODE_W, y: posOf(from).y + NODE_H / 2 }
      const p2 = { x: posOf(to).x, y: posOf(to).y + NODE_H / 2 }
      const dx = Math.max(40, Math.abs(p2.x - p1.x) / 2)
      return {
        id: edge.id,
        d: `M ${p1.x} ${p1.y} C ${p1.x + dx} ${p1.y}, ${p2.x - dx} ${p2.y}, ${p2.x} ${p2.y}`,
        main: edge.is_main,
        transition: edge.transition,
      }
    })
    .filter((item): item is NonNullable<typeof item> => item !== null)
})

/** 选中节点对象（右侧详情用） */
const selectedNode = computed(() =>
  selectedId.value == null ? null : nodeById.value.get(selectedId.value) ?? null,
)

/** 节点类型徽标映射（dify 节点卡头部色带） */
const TYPE_BADGE: Record<string, { icon: string; cls: string }> = {
  clip: { icon: '🎬', cls: 'bg-primary-50 text-primary-700' },
  title: { icon: 'T', cls: 'bg-gold-fog text-gold-deep' },
  audio_overlay: { icon: '🔊', cls: 'bg-info-bg text-info' },
  compose: { icon: '⧉', cls: 'bg-danger-bg text-danger' },
}

/**
 * 读取节点类型徽标（未知类型回落为 clip 样式）。
 * @param nodeType 节点类型
 * @returns 徽标配置
 */
function badgeOf(nodeType: string): { icon: string; cls: string } {
  return TYPE_BADGE[nodeType] ?? TYPE_BADGE.clip
}

/**
 * 从节点参数读取展示时长（秒）。
 * @param node 编排节点
 * @returns 时长文本
 */
function durationText(node: CompositionNode): string {
  const ms = Number(node.params?.duration_ms ?? 0)
  return ms > 0 ? `${(ms / 1000).toFixed(1)}s` : '—'
}

/* ==================== 数据加载与写操作 ==================== */

/**
 * 加载编排图与历史计划（重置本地坐标覆盖与选中态）。
 * @returns 无
 */
async function loadData(): Promise<void> {
  if (!projectId.value) return
  loading.value = true
  try {
    graph.value = await getCompositionGraph(projectId.value, epNo.value, 'node').catch(() => null)
    const [planPage, packPage] = await Promise.all([
      listCompositionPlans(projectId.value, epNo.value).catch(() => null),
      listAnchorPacks(projectId.value, epNo.value).catch(() => null),
    ])
    plans.value = planPage?.items ?? []
    anchorPacks.value = packPage?.items ?? []
  } finally {
    loading.value = false
    positions.value = {}
    selectedId.value = null
  }
}

/**
 * 从分镜板同步生成编排图。
 * @param force 是否强制重建
 * @returns 无
 */
async function sync(force = false): Promise<void> {
  syncing.value = true
  try {
    graph.value = await syncComposition(projectId.value, epNo.value, force)
    toast.success('编排图已同步')
    await loadData()
  } catch {
    // P4-c 修复：拦截器已 toast 业务错误（如"该剧集无镜头"），
    // 此处捕获避免事件处理器 Promise 未处理拒绝（Uncaught in promise）
  } finally {
    syncing.value = false
  }
}

/**
 * 新增标题节点。
 * @returns 无
 */
async function addTitleNode(): Promise<void> {
  try {
    await createCompositionNode(projectId.value, epNo.value, {
      node_type: 'title',
      label: newNodeLabel.value,
      params: { text: newNodeLabel.value },
    })
    toast.success('标题节点已添加')
    await loadData()
  } catch {
    // 拦截器已 toast，此处仅兜底捕获避免未处理拒绝
  }
}

/**
 * 删除节点（同时级联删除关联边）。
 * @param nodeId 节点 ID
 * @returns 无
 */
async function removeNode(nodeId: number): Promise<void> {
  try {
    await deleteCompositionNode(projectId.value, nodeId)
    toast.success('节点已删除')
    await loadData()
  } catch {
    // 拦截器已 toast，此处仅兜底捕获避免未处理拒绝
  }
}

/**
 * 编译编排图为渲染计划。空态（尚未同步编排图）时给出前置条件提示。
 * @param preview 是否试渲
 * @returns 无
 */
async function compile(preview = false): Promise<void> {
  // P4-d 修复：空态点击给出明确提示，不再静默
  if (!graph.value || graph.value.nodes.length === 0) {
    toast.warning('请先点击「从分镜同步」生成编排图，再编译计划')
    return
  }
  try {
    const result = await compileComposition(projectId.value, epNo.value, preview, false)
    compileResult.value = { plan_hash: result.plan_hash, reused: result.reused }
    toast.success(result.reused ? '命中相同计划（plan_hash 复用）' : '编译完成')
    await loadData()
  } catch {
    // 拦截器已 toast，此处仅兜底捕获避免未处理拒绝
  }
}

/**
 * 主路径顺序回写分镜（先取影响清单，再确认执行）。空态时提示先同步编排图。
 * @returns 无
 */
async function applyOrder(): Promise<void> {
  // P4-d 修复：空态点击给出明确提示，不再静默
  if (!graph.value || graph.value.nodes.length === 0) {
    toast.warning('尚未生成编排图，请先点击「从分镜同步」')
    return
  }
  try {
    const first = await applyCompositionOrder(projectId.value, epNo.value, false)
    if (first.requires_confirmation) {
      const impacts = Array.isArray(first.impacts) ? first.impacts.length : 0
      const confirmed = window.confirm(`回写将调整 ${impacts} 个镜头的顺序，是否继续？`)
      if (!confirmed) return
      await applyCompositionOrder(projectId.value, epNo.value, true)
      toast.success('主路径顺序已回写分镜')
      await loadData()
      return
    }
    toast.success('分镜顺序已同步')
  } catch {
    // 拦截器已 toast，此处仅兜底捕获避免未处理拒绝
  }
}

/** 校验是否通过 */
const validationOk = computed(() => graph.value?.validation?.valid ?? false)

/**
 * 进入后期合成页。未编译计划时给出前置条件提示而非静默跳转/禁用。
 * @returns 无
 */
function goPostProduction(): void {
  // P4-d 修复：空态点击给出明确提示（原 disabled 态点击零反馈）
  if (!compileResult.value && plans.value.length === 0) {
    toast.warning('请先编译计划，再进入后期合成')
    return
  }
  void router.push(`/projects/${projectId.value}/post`)
}

/* ==================== 画布交互：缩放 / 平移 / 节点拖拽 ==================== */

/**
 * 滚轮缩放（以指针为中心）。
 * @param event 滚轮事件
 * @returns 无
 */
function onWheel(event: WheelEvent): void {
  event.preventDefault()
  const rect = canvasEl.value?.getBoundingClientRect()
  if (!rect) return
  const px = event.clientX - rect.left
  const py = event.clientY - rect.top
  const factor = event.deltaY < 0 ? 1.1 : 1 / 1.1
  const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom.value * factor))
  const ratio = next / zoom.value
  pan.value = { x: px - (px - pan.value.x) * ratio, y: py - (py - pan.value.y) * ratio }
  zoom.value = next
}

/**
 * 空白处按下：开始平移会话并取消选中。
 * @param event 指针事件
 * @returns 无
 */
function onCanvasPointerDown(event: PointerEvent): void {
  if (event.button !== 0) return
  selectedId.value = null
  panSession = { startX: event.clientX, startY: event.clientY, origX: pan.value.x, origY: pan.value.y }
  window.addEventListener('pointermove', onPanMove)
  window.addEventListener('pointerup', onPanUp)
}

/**
 * 平移中：按屏幕位移更新视口原点。
 * @param event 指针事件
 * @returns 无
 */
function onPanMove(event: PointerEvent): void {
  if (!panSession) return
  pan.value = {
    x: panSession.origX + (event.clientX - panSession.startX),
    y: panSession.origY + (event.clientY - panSession.startY),
  }
}

/**
 * 平移结束：移除监听并清理会话。
 * @returns 无
 */
function onPanUp(): void {
  panSession = null
  window.removeEventListener('pointermove', onPanMove)
  window.removeEventListener('pointerup', onPanUp)
}

/**
 * 节点卡片按下：开始拖拽会话并选中该节点。
 * @param node 编排节点
 * @param event 指针事件
 * @returns 无
 */
function onNodePointerDown(node: CompositionNode, event: PointerEvent): void {
  if (event.button !== 0) return
  event.stopPropagation()
  selectedId.value = node.id
  const pos = posOf(node)
  nodeSession = {
    id: node.id,
    startX: event.clientX,
    startY: event.clientY,
    origX: pos.x,
    origY: pos.y,
    moved: false,
  }
  window.addEventListener('pointermove', onNodeMove)
  window.addEventListener('pointerup', onNodeUp)
}

/**
 * 节点拖拽中：按缩放比把屏幕位移换算为世界坐标并即时更新。
 * @param event 指针事件
 * @returns 无
 */
function onNodeMove(event: PointerEvent): void {
  if (!nodeSession) return
  const dx = (event.clientX - nodeSession.startX) / zoom.value
  const dy = (event.clientY - nodeSession.startY) / zoom.value
  if (Math.abs(dx) + Math.abs(dy) > 2) nodeSession.moved = true
  positions.value[nodeSession.id] = {
    x: Math.round(nodeSession.origX + dx),
    y: Math.round(nodeSession.origY + dy),
  }
}

/**
 * 节点拖拽结束：位移超过阈值时把新坐标 PATCH 持久化（ui_pos）。
 * @returns 无
 */
async function onNodeUp(): Promise<void> {
  window.removeEventListener('pointermove', onNodeMove)
  window.removeEventListener('pointerup', onNodeUp)
  const session = nodeSession
  nodeSession = null
  if (!session || !session.moved || !projectId.value) return
  const pos = positions.value[session.id]
  if (!pos) return
  savingPos.value = true
  try {
    await updateCompositionNode(projectId.value, session.id, { ui_pos: { x: pos.x, y: pos.y } })
  } finally {
    savingPos.value = false
  }
}

/**
 * 重置视口（居中默认缩放）。
 * @returns 无
 */
function resetView(): void {
  pan.value = { x: 40, y: 24 }
  zoom.value = 1
}

/** 卸载时清理 window 指针监听，避免离开页面后残留会话 */
onUnmounted(() => {
  window.removeEventListener('pointermove', onPanMove)
  window.removeEventListener('pointerup', onPanUp)
  window.removeEventListener('pointermove', onNodeMove)
  window.removeEventListener('pointerup', onNodeUp)
})

watch(epNo, loadData)
onMounted(loadData)
</script>

<template>
  <div class="flex h-full flex-col overflow-hidden">
    <!-- 顶栏：刊头 + 全局操作 -->
    <header class="shrink-0 px-6 pb-3 pt-5 lg:px-8">
      <PageHeader title="合成编排">
        <template #actions>
          <label class="flex items-center gap-2 text-caption text-neutral-600">
            集号
            <input v-model.number="epNo" type="number" min="1" class="gd-input w-20" />
          </label>
          <AppButton variant="secondary" :loading="loading" @click="loadData">刷新</AppButton>
          <AppButton variant="ghost" @click="applyOrder">回写分镜顺序</AppButton>
          <AppButton :loading="syncing" @click="sync(false)">从分镜同步</AppButton>
          <!-- 编排编译完成后进入后期合成，补齐「编排 → 剪辑合成」出口，避免页面成为死胡同。
               P4-d 修复：未编译时不再禁用（禁用点击零反馈），改为点击给出前置条件提示 -->
          <AppButton
            variant="primary"
            @click="goPostProduction"
          >
            下一步 · 后期合成 →
          </AppButton>
        </template>
      </PageHeader>
    </header>

    <!-- 主区：左画布 + 右信息栏 -->
    <div class="flex min-h-0 flex-1">
      <!-- ============ 画布（dify 形态） ============ -->
      <section class="relative min-w-0 flex-1 border-t border-hairline" aria-label="编排画布">
        <!-- 空态 -->
        <div v-if="!graph && !loading" class="absolute inset-0 flex items-center justify-center">
          <EmptyState
            title="尚未生成编排图"
            description="点击「从分镜同步」把当前集的镜头序列映射为主路径节点。"
            icon-path="M3.75 6A2.25 2.25 0 016 3.75h3v16.5H6A2.25 2.25 0 013.75 18V6z"
          >
            <template #action>
              <AppButton variant="primary" :loading="syncing" @click="sync(false)">从分镜同步</AppButton>
            </template>
          </EmptyState>
        </div>

        <!-- 画布容器：滚轮缩放 / 空白拖拽平移 -->
        <div
          v-else
          ref="canvasEl"
          class="absolute inset-0 cursor-grab overflow-hidden active:cursor-grabbing"
          @wheel="onWheel"
          @pointerdown="onCanvasPointerDown"
        >
          <!-- 世界层：平移 + 缩放 -->
          <div
            class="absolute left-0 top-0 h-0 w-0 origin-top-left"
            :style="{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }"
          >
            <!-- 连线层（SVG，贝塞尔主路径） -->
            <svg
              class="pointer-events-none absolute left-0 top-0 overflow-visible"
              width="8000"
              height="5000"
              aria-hidden="true"
            >
              <defs>
                <marker id="gd-cg-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M0 0 L8 4 L0 8 z" fill="rgba(139,124,255,0.9)" />
                </marker>
                <marker id="gd-cg-arrow-sub" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M0 0 L8 4 L0 8 z" fill="rgba(140,140,150,0.55)" />
                </marker>
              </defs>
              <path
                v-for="edge in edgePaths"
                :key="edge.id"
                :d="edge.d"
                fill="none"
                :stroke="edge.main ? 'rgba(139,124,255,0.85)' : 'rgba(140,140,150,0.5)'"
                :stroke-width="edge.main ? 2 : 1.4"
                :stroke-dasharray="edge.main ? undefined : '5 4'"
                :marker-end="edge.main ? 'url(#gd-cg-arrow)' : 'url(#gd-cg-arrow-sub)'"
              />
            </svg>

            <!-- 节点卡片层 -->
            <button
              v-for="node in graph?.nodes ?? []"
              :key="node.id"
              type="button"
              class="group absolute flex cursor-grab flex-col rounded-xl border bg-surface-raised text-left shadow-gd-1 outline-none transition-[border-color,box-shadow] hover:shadow-gd-2 active:cursor-grabbing"
              :class="
                selectedId === node.id
                  ? 'border-primary-400 ring-2 ring-primary-100'
                  : node.status === 'disabled'
                    ? 'border-hairline bg-surface-inset opacity-60'
                    : 'border-hairline'
              "
              :style="{ left: `${posOf(node).x}px`, top: `${posOf(node).y}px`, width: `${NODE_W}px`, height: `${NODE_H}px` }"
              :aria-label="`编排节点 ${node.label || node.node_key}`"
              @pointerdown="onNodePointerDown(node, $event)"
            >
              <!-- 左侧类型色条（dify 节点卡识别条） -->
              <span
                class="absolute left-0 top-3 h-[calc(100%-1.5rem)] w-[3px] rounded-full"
                :class="node.node_type === 'title' ? 'bg-gold' : node.node_type === 'audio_overlay' ? 'bg-info' : 'bg-primary-400'"
                aria-hidden="true"
              />
              <!-- 输入 / 输出锚点 -->
              <span class="absolute -left-1.5 top-1/2 h-3 w-3 -translate-y-1/2 rounded-full border-2 border-primary-300 bg-surface-raised" aria-hidden="true" />
              <span class="absolute -right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 rounded-full border-2 border-primary-400 bg-primary-100" aria-hidden="true" />

              <!-- 卡头：类型徽标 + 标签 + 过期/禁用标记 -->
              <span class="flex items-center gap-2 px-3 pt-2.5">
                <span
                  class="gd-num flex h-5 w-5 shrink-0 items-center justify-center rounded text-[10px] font-semibold"
                  :class="badgeOf(node.node_type).cls"
                  aria-hidden="true"
                >
                  {{ badgeOf(node.node_type).icon }}
                </span>
                <span class="min-w-0 flex-1 truncate text-[13px] font-semibold text-neutral-800">
                  {{ node.label || node.node_key }}
                </span>
                <span v-if="node.is_stale" class="shrink-0 text-[10px] text-gold-deep" title="结构过期">⟳</span>
                <span v-if="node.status === 'disabled'" class="shrink-0 text-[10px] text-neutral-400" title="已禁用">⏸</span>
              </span>

              <!-- 卡身：镜号 / 时长 / 引用 -->
              <span class="mt-1 flex flex-1 flex-col justify-between px-3 pb-2.5">
                <span class="gd-num truncate text-caption text-neutral-500">
                  {{ node.params?.shot_no != null ? `镜 ${node.params.shot_no}` : node.node_type }}
                  · {{ durationText(node) }}
                </span>
                <span class="gd-num truncate text-caption text-neutral-400">
                  {{ node.ref_id ? `ref #${node.ref_id}` : '—' }}
                </span>
              </span>
            </button>
          </div>
        </div>

        <!-- 画布悬浮工具：缩放控件（左下） -->
        <div
          v-if="graph"
          class="absolute bottom-4 left-4 flex items-center gap-1 rounded-full border border-hairline bg-surface-raised/90 px-1.5 py-1 shadow-gd-1 backdrop-blur"
        >
          <button
            type="button"
            class="flex h-7 w-7 items-center justify-center rounded-full text-neutral-500 hover:bg-surface-inset hover:text-neutral-800"
            aria-label="缩小"
            @click="zoom = Math.max(ZOOM_MIN, zoom / 1.2)"
          >
            −
          </button>
          <span class="gd-num w-12 text-center text-caption text-neutral-600">{{ Math.round(zoom * 100) }}%</span>
          <button
            type="button"
            class="flex h-7 w-7 items-center justify-center rounded-full text-neutral-500 hover:bg-surface-inset hover:text-neutral-800"
            aria-label="放大"
            @click="zoom = Math.min(ZOOM_MAX, zoom * 1.2)"
          >
            +
          </button>
          <span class="mx-0.5 h-4 w-px bg-hairline" aria-hidden="true" />
          <button
            type="button"
            class="flex h-7 items-center justify-center rounded-full px-2 text-caption text-neutral-500 hover:bg-surface-inset hover:text-neutral-800"
            @click="resetView"
          >
            重置
          </button>
        </div>

        <!-- 画布悬浮状态（右下）：节点坐标保存提示 -->
        <span
          v-if="savingPos"
          class="gd-num absolute bottom-4 right-4 rounded-full border border-hairline bg-surface-raised/90 px-3 py-1 text-caption text-neutral-500 shadow-gd-1 backdrop-blur"
        >
          保存节点坐标…
        </span>
      </section>

      <!-- ============ 右信息栏 ============ -->
      <aside class="w-80 shrink-0 overflow-y-auto border-l border-hairline bg-surface-canvas/60 px-4 py-4" aria-label="编排信息">
        <!-- 编排摘要 -->
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h2 class="text-title-sm text-neutral-900">编排摘要</h2>
          <div class="flex gap-1.5">
            <AppButton size="sm" variant="secondary" @click="compile(true)">试渲编译</AppButton>
            <AppButton size="sm" @click="compile(false)">编译计划</AppButton>
          </div>
        </div>
        <p v-if="compileResult" class="mt-2 font-mono text-caption text-neutral-500">
          plan_hash {{ compileResult.plan_hash.slice(0, 16) }}… ·
          {{ compileResult.reused ? '复用已有计划' : '新建计划' }}
        </p>
        <dl v-if="graph" class="mt-3 grid grid-cols-3 gap-2 text-center">
          <div class="rounded-md border border-hairline bg-surface-raised px-2 py-2">
            <dt class="text-caption text-neutral-400">版本</dt>
            <dd class="gd-num text-body-sm text-neutral-800">v{{ graph.version }}</dd>
          </div>
          <div class="rounded-md border border-hairline bg-surface-raised px-2 py-2">
            <dt class="text-caption text-neutral-400">节点</dt>
            <dd class="gd-num text-body-sm text-neutral-800">{{ graph.nodes.length }}</dd>
          </div>
          <div class="rounded-md border border-hairline bg-surface-raised px-2 py-2">
            <dt class="text-caption text-neutral-400">总时长</dt>
            <dd class="gd-num text-body-sm text-neutral-800">{{ (graph.total_duration_ms / 1000).toFixed(1) }}s</dd>
          </div>
        </dl>

        <!-- 新增标题节点 -->
        <div class="mt-4 flex items-center gap-2">
          <input v-model="newNodeLabel" class="gd-input w-full" placeholder="标题文本" aria-label="标题文本" />
          <AppButton size="sm" variant="secondary" class="shrink-0" @click="addTitleNode">新增节点</AppButton>
        </div>

        <!-- 结构守卫 -->
        <section class="mt-5" aria-label="结构守卫">
          <div class="flex items-center justify-between gap-2">
            <h2 class="text-title-sm text-neutral-900">结构守卫</h2>
            <span class="text-caption" :class="validationOk ? 'text-neutral-500' : 'text-danger-600'">
              {{ validationOk ? 'N1~N8 通过' : '存在结构问题' }}
            </span>
          </div>
          <ul v-if="graph && !validationOk" class="mt-2 flex flex-col gap-2">
            <li
              v-for="(issue, index) in graph.validation.issues"
              :key="index"
              class="rounded-md border border-hairline bg-surface-inset p-2 text-body-sm text-neutral-700"
            >
              <span class="mr-2 font-mono text-caption text-danger-600">{{ issue.code || issue.rule || 'ISSUE' }}</span>
              {{ issue.message || issue.detail || '结构校验未通过' }}
            </li>
          </ul>
          <p v-else class="mt-2 text-caption text-neutral-400">
            {{ graph ? '节点 / 边结构满足 N1~N8 全部约束。' : '尚未生成编排图。' }}
          </p>
        </section>

        <!-- 选中节点详情 -->
        <section v-if="selectedNode" class="mt-5" aria-label="节点详情">
          <h2 class="text-title-sm text-neutral-900">节点详情</h2>
          <dl class="mt-2 space-y-1.5 text-body-sm">
            <div class="flex justify-between gap-3">
              <dt class="shrink-0 text-caption text-neutral-500">类型</dt>
              <dd class="truncate text-neutral-800">{{ selectedNode.node_type }}</dd>
            </div>
            <div class="flex justify-between gap-3">
              <dt class="shrink-0 text-caption text-neutral-500">标签</dt>
              <dd class="truncate text-neutral-800">{{ selectedNode.label || '—' }}</dd>
            </div>
            <div class="flex justify-between gap-3">
              <dt class="shrink-0 text-caption text-neutral-500">主路径顺序</dt>
              <dd class="gd-num text-neutral-800">{{ selectedNode.order_hint }}</dd>
            </div>
            <div class="flex justify-between gap-3">
              <dt class="shrink-0 text-caption text-neutral-500">引用</dt>
              <dd class="gd-num truncate text-neutral-800">
                {{ selectedNode.ref_id ? `${selectedNode.ref_kind ?? ''} #${selectedNode.ref_id}` : '—' }}
              </dd>
            </div>
            <div class="flex justify-between gap-3">
              <dt class="shrink-0 text-caption text-neutral-500">状态</dt>
              <dd class="text-neutral-800">{{ selectedNode.status }}{{ selectedNode.is_stale ? ' · 结构过期' : '' }}</dd>
            </div>
          </dl>
          <pre class="mt-2 max-h-32 overflow-auto rounded-md bg-surface-inset p-2 font-mono text-caption leading-relaxed text-neutral-600">{{ JSON.stringify(selectedNode.params, null, 2) }}</pre>
          <AppButton size="sm" variant="ghost" class="mt-2 w-full" @click="removeNode(selectedNode.id)">
            删除该节点
          </AppButton>
        </section>

        <!-- 多维锚点包 -->
        <details class="mt-5" :open="!anchorPacks.length">
          <summary class="cursor-pointer text-title-sm text-neutral-900">
            多维锚点包
            <span class="ml-1 text-caption font-normal text-neutral-400">（{{ anchorPacks.length }}）</span>
          </summary>
          <p v-if="!anchorPacks.length" class="mt-2 text-caption text-neutral-500">
            本集暂无镜头锚点包（先在分镜页生成镜头）。
          </p>
          <ul v-else class="mt-2 flex flex-col gap-1.5">
            <li
              v-for="pack in anchorPacks"
              :key="pack.shot_id"
              class="rounded-md border border-hairline bg-surface-inset px-2 py-1.5"
            >
              <p class="gd-num text-caption text-neutral-700">
                #{{ pack.shot_id }} · 场{{ pack.scene_no }} · {{ pack.items.length }} 项
              </p>
              <p
                class="text-caption"
                :class="pack.degradation.level === 'none' ? 'text-neutral-400' : 'text-danger-600'"
              >
                {{ pack.degradation.level === 'none' ? '锚点齐全' : pack.degradation.note }}
              </p>
            </li>
          </ul>
        </details>

        <!-- 历史计划 -->
        <details class="mt-4">
          <summary class="cursor-pointer text-title-sm text-neutral-900">
            历史计划
            <span class="ml-1 text-caption font-normal text-neutral-400">（{{ plans.length }}）</span>
          </summary>
          <p v-if="!plans.length" class="mt-2 text-caption text-neutral-500">尚无编译记录。</p>
          <ul v-else class="mt-2 flex flex-col gap-1 text-caption text-neutral-600">
            <li v-for="(plan, index) in plans" :key="index" class="font-mono">
              {{ String(plan.plan_hash || '').slice(0, 16) }}… · {{ plan.reused_count ?? 0 }} 次复用 ·
              {{ plan.created_at || '' }}
            </li>
          </ul>
        </details>
      </aside>
    </div>
  </div>
</template>

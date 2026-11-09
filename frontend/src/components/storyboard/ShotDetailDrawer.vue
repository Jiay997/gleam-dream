<!--
  文件级注释：P13 · 镜头详情抽屉（画布内 DrawerPanel，可从画布 / 列表模式复用）。
  v7 纸上放映厅 Pro · 对照 F4-03（P13 镜头详情抽屉）重构：
  - 头部：SH-XX 衬线大镜号 + StatusTag + 时长（420px 视觉规格；DrawerPanel 基础件
    档位为 480/640，本抽屉用 480 档承载——见最终报告共享层遗留项）；
  - 参数区：景别 / 运镜 / 情绪 / 时长 2×2 meta 卡（label caption + 控件），对白镜头
    >6s 黄色警示 + dialogue 金色引用块（bg-gold-fog + 金左线 + 衬线引文）；
  - 画面描述 image_prompt（可编辑保存 PATCH）；
  - 资产锚点区（角色紫点 / 场景绿点 chips + 编辑锚点弹层：拉取项目 approved+locked 资产
    多选 → POST anchors → 组装 final_prompt 只读代码块可折叠）；
  - 版本区（后端暂无 shot-versions 端点，渲染后生成占位）。
  底部：重出首帧（PATCH /shots/{id}/frame/redraw，本地合成 0 积分）/ 保存修改 / 关闭。
  锁定镜头（shot.status=locked）时全部输入禁用。
  全部功能逻辑（PATCH / 锚点绑定 / 弹层 / 校验）保留，仅视觉与交互层重构。

  v8 内容合并（原草稿预览页右栏下线后迁移至本抽屉）：
  - 顶部「本集预算感知」两列积分 + 四格统计（集时长 / 镜头 / 配音 / 字幕）；
  - 底部「镜头明细」同集镜头列表（点击切换当前镜，不重复展示参数）；
  - 底部「本集审批 HITL-3」：分镜锁定后提供驳回（反馈 ≥10 字）/ approve 通过，
    未锁定时仅提示去画布底部审批卡送审（与画布审批卡不重复）。
  所有新增区块沿用抽屉既有亮色令牌（surface-canvas / gold-deep / gd-kicker），
  与参数卡、摘要区同一视觉语言，不引入新色值。
-->
<script setup lang="ts">
/**
 * ShotDetailDrawer 镜头详情抽屉。
 * props：
 * - modelValue：显示状态（v-model）；
 * - shot：镜头数据（详情接口的 ShotItem，可为 null）；
 * - projectId：项目 ID（锚点选择器拉取项目资产用）。
 * emit：update:modelValue / updated（保存或绑定锚点后的最新镜头数据）。
 */
import { computed, reactive, ref, watch } from 'vue'

import { bindShotAnchors, decideStoryboard, patchShot, type ShotItem } from '@/api/storyboard'
import { getVoiceCoverage, type VoiceCoverageData } from '@/api/post'
import { redrawShotFrame } from '@/api/v4'
import { listAssets, type AssetItem } from '@/api/assets'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import DrawerPanel from '@/components/common/DrawerPanel.vue'
import Modal from '@/components/common/Modal.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import { CAMERA_MOVE_OPTIONS, EMOTION_TAG_OPTIONS, SHOT_SIZE_OPTIONS, optionLabel } from '@/constants/storyboard'

/** 抽屉显示状态（v-model） */
const modelValue = defineModel<boolean>({ required: true })

/** 组件 props */
const props = withDefaults(
  defineProps<{
    /** 当前镜头数据（null 表示未选中） */
    shot: ShotItem | null
    /** 项目 ID */
    projectId: string
    /** 同集镜头列表（镜头明细区展示与切换；默认空数组） */
    shots?: ShotItem[]
    /** 所属分镜板 ID（本集审批决策用；null 表示未加载） */
    storyboardId?: number | string | null
    /** 所属分镜板状态（locked 时开放驳回 / approve） */
    storyboardStatus?: string
  }>(),
  {
    shots: () => [],
    storyboardId: null,
    storyboardStatus: '',
  },
)

/** 抽屉对外事件 */
const emit = defineEmits<{
  /** 镜头数据已更新（保存 / 绑定锚点后回传最新字段） */
  updated: [shot: ShotItem]
  /** 镜头明细点击：请求切换当前镜头（父组件更新 activeShot） */
  'select-shot': [shotId: number | string]
  /** 本集审批决策已提交（父组件刷新分镜状态 / 跳转生成任务中心） */
  decided: [action: 'approve' | 'reject']
}>()

/** Toast */
const toast = useToast()

/* ==================== 参数表单状态 ==================== */

/** 表单草稿（打开抽屉时从镜头数据初始化） */
const form = reactive({
  /** 景别 */
  shot_size: '',
  /** 运镜 */
  camera_move: '',
  /** 情绪标签 */
  emotion_tag: '',
  /** 时长（毫秒） */
  duration_ms: 3000,
  /** 画面描述 image_prompt */
  image_prompt: '',
})

/** 保存中 */
const saving = ref(false)

/** 镜头是否锁定（锁定后全部输入禁用） */
const isLocked = computed(() => props.shot?.status === 'locked')

/** 镜头是否有对白 */
const hasDialogue = computed(() => Boolean(props.shot?.dialogue && props.shot.dialogue.trim()))

/**
 * 时长是否触发黄色警示（对白镜头 > 6 秒，超出后端建议的 6000ms 上限）。
 * @returns 是否警示
 */
const durationWarning = computed(() => hasDialogue.value && form.duration_ms > 6000)

/**
 * 表单相对当前镜头是否有改动。
 * @returns 是否有改动
 */
const dirty = computed(() => {
  const shot = props.shot
  if (!shot) return false
  return (
    (form.shot_size || '') !== (shot.shot_size ?? '') ||
    (form.camera_move || '') !== (shot.camera_move ?? '') ||
    (form.emotion_tag || '') !== (shot.emotion_tag ?? '') ||
    form.duration_ms !== shot.duration_ms ||
    form.image_prompt !== (shot.image_prompt ?? '')
  )
})

// 打开抽屉 / 切换镜头时：同步镜头数据到表单
watch(
  () => [modelValue.value, props.shot?.id] as const,
  ([open]) => {
    if (!open || !props.shot) return
    form.shot_size = props.shot.shot_size ?? ''
    form.camera_move = props.shot.camera_move ?? ''
    form.emotion_tag = props.shot.emotion_tag ?? ''
    form.duration_ms = props.shot.duration_ms
    form.image_prompt = props.shot.image_prompt ?? ''
  },
  { immediate: true },
)

/* ==================== 保存修改（PATCH） ==================== */

/**
 * 保存修改：仅提交有改动的字段（PATCH 语义）。
 * @returns 无
 */
async function handleSave(): Promise<void> {
  const shot = props.shot
  if (!shot || saving.value) return
  if (form.duration_ms < 1000 || form.duration_ms > 15000) {
    toast.warning('时长须在 1~15 秒（1000~15000 毫秒）之间')
    return
  }
  const payload: Record<string, unknown> = {}
  if ((form.shot_size || null) !== (shot.shot_size ?? null)) payload.shot_size = form.shot_size || null
  if ((form.camera_move || null) !== (shot.camera_move ?? null)) payload.camera_move = form.camera_move || null
  if ((form.emotion_tag || null) !== (shot.emotion_tag ?? null)) payload.emotion_tag = form.emotion_tag || null
  if (form.duration_ms !== shot.duration_ms) payload.duration_ms = form.duration_ms
  if (form.image_prompt !== (shot.image_prompt ?? '')) payload.image_prompt = form.image_prompt
  if (Object.keys(payload).length === 0) {
    toast.info('没有需要保存的修改')
    return
  }
  saving.value = true
  try {
    const updated = await patchShot(shot.id, payload)
    emit('updated', updated)
    toast.success('镜头已更新')
  } catch {
    // 拦截器已 toast（40001 已锁定 / 10001 参数非法等）
  } finally {
    saving.value = false
  }
}

/* ==================== 锚点展示 ==================== */

/** final_prompt 代码块是否展开 */
const promptOpen = ref(false)

/** 锚点资产 ID → 资产信息映射（资产列表加载后填充） */
const assetMap = ref<Map<string, AssetItem>>(new Map())

/**
 * 依 ID 查询锚点资产（资产列表未加载时返回 null 走占位展示）。
 * @param id 资产 ID
 * @returns 资产对象或 null
 */
function assetOf(id: number | string): AssetItem | null {
  return assetMap.value.get(String(id)) ?? null
}

/* ==================== 锚点选择器弹层 ==================== */

/** 锚点选择器是否打开 */
const anchorOpen = ref(false)
/** 锚点资产加载中 */
const anchorLoading = ref(false)
/** 锚点提交中 */
const anchorSubmitting = ref(false)
/** 可选角色资产（approved/locked） */
const characterAssets = ref<AssetItem[]>([])
/** 可选场景资产（approved/locked） */
const sceneAssets = ref<AssetItem[]>([])
/** 选择器中勾选的角色资产 ID 集合 */
const pickedCharacterIds = ref<Array<number | string>>([])
/** 选择器中选中的场景资产 ID（空串表示不绑定场景） */
const pickedSceneId = ref<number | string>('')

/**
 * 资产是否可绑定（仅 locked 状态可绑定锚点）。
 * @param asset 资产对象
 * @returns 是否可绑定
 */
function isBindable(asset: AssetItem): boolean {
  return asset.status === 'locked'
}

/**
 * 拉取项目角色 / 场景资产（approved+locked）并填充锚点名称映射。
 * @param initSelection 是否同时按当前镜头初始化勾选状态（打开选择器时为 true）
 * @returns 无
 */
async function ensureAssets(initSelection: boolean): Promise<void> {
  if (initSelection) {
    pickedCharacterIds.value = [...(props.shot?.character_asset_ids ?? [])]
    pickedSceneId.value = props.shot?.scene_asset_id ?? ''
  }
  anchorLoading.value = true
  try {
    const [characters, scenes] = await Promise.all([
      listAssets(props.projectId, { asset_type: 'character', page_size: 100 }),
      listAssets(props.projectId, { asset_type: 'scene', page_size: 100 }),
    ])
    const all = [...characters.items, ...scenes.items]
    assetMap.value = new Map(all.map((asset) => [String(asset.id), asset]))
    characterAssets.value = characters.items.filter((a) => a.status === 'approved' || a.status === 'locked')
    sceneAssets.value = scenes.items.filter((a) => a.status === 'approved' || a.status === 'locked')
  } catch {
    // 拦截器已 toast（失败不阻塞主表单，chips 回退展示资产 ID）
  } finally {
    anchorLoading.value = false
  }
}

/**
 * 打开锚点选择器（打开时顺带初始化勾选状态）。
 * @returns 无
 */
function openAnchorEditor(): void {
  anchorOpen.value = true
  void ensureAssets(true)
}

// 抽屉打开时静默加载资产映射（锚点 chips 展示名称用；已加载过则跳过）
watch(
  () => modelValue.value,
  (open) => {
    if (open && assetMap.value.size === 0) void ensureAssets(false)
  },
  { immediate: true },
)

/**
 * 切换角色资产勾选状态。
 * @param asset 资产对象
 * @returns 无
 */
function toggleCharacter(asset: AssetItem): void {
  if (!isBindable(asset)) return
  const idStr = String(asset.id)
  const index = pickedCharacterIds.value.findIndex((id) => String(id) === idStr)
  if (index >= 0) {
    pickedCharacterIds.value.splice(index, 1)
  } else {
    pickedCharacterIds.value.push(asset.id)
  }
}

/**
 * 提交锚点绑定（POST /shots/{id}/anchors）→ 回传组装后的 final_prompt 等字段。
 * @returns 无
 */
async function submitAnchors(): Promise<void> {
  const shot = props.shot
  if (!shot || anchorSubmitting.value) return
  anchorSubmitting.value = true
  try {
    const result = await bindShotAnchors(
      shot.id,
      pickedCharacterIds.value,
      pickedSceneId.value === '' ? null : pickedSceneId.value,
    )
    emit('updated', {
      ...shot,
      character_asset_ids: result.character_asset_ids,
      scene_asset_id: result.scene_asset_id,
      asset_anchor_ids: result.asset_anchor_ids,
      final_prompt: result.final_prompt,
    })
    promptOpen.value = true
    anchorOpen.value = false
    toast.success('锚点已绑定，final_prompt 已更新')
  } catch {
    // 拦截器已 toast（50001 资产未锁定 / 40001 镜头已锁定等）
  } finally {
    anchorSubmitting.value = false
  }
}

/* ==================== 本集预算与统计（自草稿预览页右栏合并） ==================== */

/** 本集镜头列表（按 order_index 升序；镜头明细区数据源） */
const episodeShots = computed<ShotItem[]>(() =>
  [...(props.shots ?? [])].sort((a, b) => a.order_index - b.order_index),
)

/** 本集总时长（毫秒） */
const episodeDurationMs = computed(() => episodeShots.value.reduce((sum, item) => sum + (item.duration_ms ?? 0), 0))

/** 本集总时长（秒，向上取整；积分估算基数） */
const episodeSeconds = computed(() => Math.ceil(episodeDurationMs.value / 1000))

/** 静帧草稿粗估积分（与下线前草稿预览同一口径：30 积分/秒 × 0.15） */
const draftCost = computed(() => Math.round(episodeSeconds.value * 30 * 0.15))

/** 高清生成预估积分（30 积分/秒） */
const renderCost = computed(() => Math.round(episodeSeconds.value * 30))

/** 已有字幕镜头数（对白或动作描述非空的镜头视为有字幕） */
const subtitleCount = computed(
  () => episodeShots.value.filter((item) => Boolean(item.dialogue?.trim() || item.action_desc?.trim())).length,
)

/**
 * 配音进度占位说明。
 * 分镜详情接口不返回逐镜配音状态，无法在此可靠统计；
 * 用「见配音轨」而不是伪造 0/N 进度，避免把“未生成”误读为“全部未配音”。
 */
const VOICEOVER_PLACEHOLDER = '见配音轨'

/** 项目级配音覆盖统计（只读接口，进入抽屉/切换镜头时刷新） */
const voiceCoverage = ref<VoiceCoverageData | null>(null)
/** 配音覆盖加载中 */
const voiceCoverageLoading = ref(false)

/**
 * 加载配音覆盖统计。
 *
 * 调用只读接口 ``GET /post/voice-coverage``，用于在镜头卡/详情页显示
 * “已配音 / 未配音 / 占位音”，不修改时间线。失败时保留占位文案，
 * 不打断分镜编辑。
 *
 * @returns 无
 */
async function loadVoiceCoverage(): Promise<void> {
  if (!props.projectId) return
  voiceCoverageLoading.value = true
  try {
    voiceCoverage.value = await getVoiceCoverage(props.projectId)
  } catch {
    voiceCoverage.value = null
  } finally {
    voiceCoverageLoading.value = false
  }
}

// 进入抽屉或切换镜头时刷新配音状态（重配后可即时看到变化）
watch(
  [() => props.projectId, () => props.shot?.id],
  () => {
    void loadVoiceCoverage()
  },
  { immediate: true },
)

/** 四格统计里的配音完成度（未加载时显示占位文案） */
const voiceStatText = computed(() => {
  const data = voiceCoverage.value
  if (!data) return VOICEOVER_PLACEHOLDER
  return `${data.voiced_shots}/${data.dialogue_shots}`
})

/** 当前镜头的配音状态文案（无台词时不显示） */
const currentShotVoice = computed(() => {
  if (!props.shot?.dialogue?.trim()) return ''
  const data = voiceCoverage.value
  if (!data) return '待确认'
  const shotId = Number(props.shot.id)
  const missing = (data.missing_shots || []).find((item) => Number(item.shot_id) === shotId)
  if (!missing) return '已配音'
  if (missing.reason === 'placeholder') return '占位音'
  if (missing.reason === 'failed') return '配音失败'
  return '未配音'
})

/** 当前镜头配音状态对应的徽标样式 */
const currentShotVoiceClass = computed(() => {
  if (currentShotVoice.value === '已配音') return 'bg-success-bg text-success-strong'
  if (currentShotVoice.value === '占位音' || currentShotVoice.value === '配音失败') {
    return 'bg-warning-bg text-warning-strong'
  }
  if (currentShotVoice.value === '未配音') return 'bg-danger-bg text-danger-strong'
  return 'bg-surface-canvas text-neutral-500'
})

/**
 * 毫秒 → mm:ss 集级时间码。
 * @param ms 毫秒
 * @returns mm:ss 文案
 */
function episodeTimecode(ms: number): string {
  const totalSeconds = Math.floor(Math.max(0, ms) / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/**
 * 点击镜头明细：请求父组件把抽屉切换到该镜头（不关闭抽屉）。
 * @param shotId 目标镜头 ID
 * @returns 无
 */
function selectEpisodeShot(shotId: number | string): void {
  emit('select-shot', shotId)
}

/* ==================== 本集审批（HITL-3 决策，自草稿预览页底部决策条合并） ==================== */

/** 分镜板是否已锁定（锁定后才进入审批决策态） */
const isStoryboardLocked = computed(() => props.storyboardStatus === 'locked')
/** approve 二次确认弹窗 */
const approveDialogOpen = ref(false)
/** 驳回弹窗 */
const rejectDialogOpen = ref(false)
/** 驳回原因（≥10 字） */
const rejectFeedback = ref('')
/** 决策请求中 */
const deciding = ref(false)

/**
 * 打开驳回弹窗（清空上次输入）。
 * @returns 无
 */
function openRejectDialog(): void {
  rejectFeedback.value = ''
  rejectDialogOpen.value = true
}

/**
 * 审批通过（decide approve）：通知父组件刷新并跳转生成任务中心。
 * @returns 无
 */
async function confirmApprove(): Promise<void> {
  const storyboardId = props.storyboardId
  if (storyboardId == null || deciding.value) return
  deciding.value = true
  try {
    const result = await decideStoryboard(storyboardId, 'approve')
    approveDialogOpen.value = false
    toast.success(result.project_status === 'rendering' ? '审批通过，项目已进入视频生成阶段' : '审批通过')
    emit('decided', 'approve')
  } catch {
    // 拦截器已 toast（10002 无待审审批单 / 40001 状态不允许等）
  } finally {
    deciding.value = false
  }
}

/**
 * 驳回提交（decide reject，反馈 ≥10 字）：分镜解锁，通知父组件刷新画布。
 * @returns 无
 */
async function confirmReject(): Promise<void> {
  const storyboardId = props.storyboardId
  if (storyboardId == null || deciding.value) return
  const feedback = rejectFeedback.value.trim()
  if (feedback.length < 10) {
    toast.warning('驳回原因需至少 10 个字，便于按反馈重新拆解')
    return
  }
  deciding.value = true
  try {
    await decideStoryboard(storyboardId, 'reject', feedback)
    rejectDialogOpen.value = false
    rejectFeedback.value = ''
    toast.success('已驳回，分镜已解锁，可继续调整镜头')
    emit('decided', 'reject')
  } catch {
    // 拦截器已 toast（10001 反馈不足 / 40001 状态不允许等）
  } finally {
    deciding.value = false
  }
}

/* ==================== 单镜首帧重出 ==================== */

/** 首帧重出提交中（避免重复点击） */
const redrawing = ref(false)

/**
 * 「重出首帧」按钮：调用 `PATCH /shots/{id}/frame/redraw` 本地合成整幅要素画面（0 积分）。
 *
 * 前置条件：镜头未锁定；已锁定镜头需先在抽屉内驳回分镜后重出。
 * 成功后把新 `media_url` 通过 `updated` 事件回传父组件，画布监看立即切换新画面。
 * 失败提示由请求拦截器统一给出（60004 次数超限 / 80001 模型不可用等）。
 * @returns 无
 */
async function redrawFrame(): Promise<void> {
  const shot = props.shot
  if (!shot || redrawing.value) return
  if (isLocked.value) {
    toast.warning('镜头已锁定：请先驳回本集分镜（反馈 ≥10 字）解锁后再重出首帧')
    return
  }
  redrawing.value = true
  try {
    const result = await redrawShotFrame(shot.id, undefined, 'compose')
    emit('updated', {
      ...shot,
      media_url: result.media_url ?? shot.media_url,
    })
    const degraded = result.degraded?.length ? `（降级：${result.degraded.join('、')}）` : ''
    toast.success(`镜 ${shot.shot_no} 首帧已重出${degraded}，剩余 ${result.remaining} 次`)
  } catch {
    // 请求拦截器已统一 toast；保留旧画面
  } finally {
    redrawing.value = false
  }
}
</script>

<template>
  <DrawerPanel v-model="modelValue" :title="shot ? `SH-${String(shot.shot_no).padStart(2, '0')} · 镜头详情` : '镜头详情'" :width="480">
    <!-- 空态保护 -->
    <div v-if="!shot" class="py-10 text-center text-sm text-neutral-400">未选择镜头</div>

    <div v-else class="space-y-6">
      <!-- ===== 头部：SH-XX 衬线大镜号 + 状态 + 时长（F4-03） ===== -->
      <div class="flex items-start gap-3">
        <h2 class="font-serif text-display text-neutral-900">SH-{{ String(shot.shot_no).padStart(2, '0') }}</h2>
        <StatusTag :status="shot.status" />
        <span class="gd-num ml-auto pt-1 text-xs text-neutral-400">第 {{ shot.order_index + 1 }} 镜</span>
      </div>

      <!-- ===== 版本对比占位（后端暂无 shot-versions 端点；F4-03 v1/v2 对比滑块形态预留） ===== -->
      <section>
        <div class="gd-night-window gd-night-window-sm relative flex aspect-[16/9] w-full flex-col items-center justify-center gap-2 overflow-hidden">
          <span class="text-3xl opacity-60" aria-hidden="true">🎞️</span>
          <p class="text-xs text-player-hint">v1 / v2 对比 · 渲染后生成版本</p>
          <p class="text-caption text-player-hint">首帧 / 正片对比请进入「生成」环节（P15 任务中心）</p>
        </div>
      </section>

      <!-- ===== 本集预算感知（原草稿预览页右栏顶部，合并至抽屉首位） ===== -->
      <section aria-label="本集预算感知">
        <div class="mb-2.5 flex items-baseline justify-between">
          <h4 class="gd-kicker">本集预算感知 · BUDGET</h4>
          <span class="text-caption text-neutral-400">按当前集预估</span>
        </div>
        <div class="grid grid-cols-2 gap-2.5">
          <div class="rounded-lg bg-surface-canvas p-2.5">
            <p class="gd-num text-base font-semibold text-gold-deep">{{ draftCost }}</p>
            <p class="mt-0.5 text-caption text-neutral-400">静帧草稿 · 积分</p>
          </div>
          <div class="rounded-lg bg-surface-canvas p-2.5">
            <p class="gd-num text-base font-semibold text-gold-deep">{{ renderCost }}</p>
            <p class="mt-0.5 text-caption text-neutral-400">高清生成 · 积分预估</p>
          </div>
        </div>
        <p class="mt-1.5 text-caption text-neutral-400">通过关口三后按锁定版本预估最高成本。</p>

        <!-- 四格统计：集时长 / 镜头 / 配音 / 字幕 -->
        <div class="mt-2 grid grid-cols-4 gap-1" aria-label="本集统计">
          <div class="rounded-lg bg-surface-canvas py-1.5 text-center">
            <p class="gd-num text-caption font-semibold text-neutral-800">{{ episodeTimecode(episodeDurationMs) }}</p>
            <p class="mt-0.5 text-[10px] text-neutral-400">集时长</p>
          </div>
          <div class="rounded-lg bg-surface-canvas py-1.5 text-center">
            <p class="gd-num text-caption font-semibold text-neutral-800">{{ episodeShots.length }}</p>
            <p class="mt-0.5 text-[10px] text-neutral-400">镜头</p>
          </div>
          <div class="rounded-lg bg-surface-canvas py-1.5 text-center" :title="voiceCoverageLoading ? '配音状态加载中' : '已配音镜头数 / 有台词镜头数'">
            <p class="gd-num text-caption font-semibold text-neutral-800">{{ voiceStatText }}</p>
            <p class="mt-0.5 text-[10px] text-neutral-400">配音</p>
          </div>
          <div class="rounded-lg bg-surface-canvas py-1.5 text-center">
            <p class="gd-num text-caption font-semibold text-neutral-800">{{ subtitleCount }}/{{ episodeShots.length }}</p>
            <p class="mt-0.5 text-[10px] text-neutral-400">字幕</p>
          </div>
        </div>
      </section>

      <!-- ===== 参数 meta 卡（F4-03：景别 / 运镜 / 情绪 / 时长 2×2） ===== -->
      <section>
        <h4 class="gd-kicker mb-2.5">镜头参数</h4>
        <div class="grid grid-cols-2 gap-2.5">
          <label class="block rounded-lg bg-surface-canvas p-2.5">
            <span class="mb-1 block text-caption text-neutral-400">景别 shot size</span>
            <select v-model="form.shot_size" :disabled="isLocked" class="gd-input gd-field-sm" aria-label="景别">
              <option value="">未设置</option>
              <option v-for="opt in SHOT_SIZE_OPTIONS" :key="opt.key" :value="opt.key">{{ opt.label }}</option>
            </select>
          </label>
          <label class="block rounded-lg bg-surface-canvas p-2.5">
            <span class="mb-1 block text-caption text-neutral-400">运镜 camera move</span>
            <select v-model="form.camera_move" :disabled="isLocked" class="gd-input gd-field-sm" aria-label="运镜">
              <option value="">未设置</option>
              <option v-for="opt in CAMERA_MOVE_OPTIONS" :key="opt.key" :value="opt.key">{{ opt.label }}</option>
            </select>
          </label>
          <label class="block rounded-lg bg-surface-canvas p-2.5">
            <span class="mb-1 block text-caption text-neutral-400">情绪 emotion tag</span>
            <select v-model="form.emotion_tag" :disabled="isLocked" class="gd-input gd-field-sm" aria-label="情绪">
              <option value="">未设置</option>
              <option v-for="opt in EMOTION_TAG_OPTIONS" :key="opt.key" :value="opt.key">{{ opt.label }}</option>
            </select>
          </label>
          <label class="block rounded-lg bg-surface-canvas p-2.5">
            <span class="mb-1 block text-caption text-neutral-400">时长 duration</span>
            <input
              v-model.number="form.duration_ms"
              type="number"
              min="1000"
              max="15000"
              step="100"
              :disabled="isLocked"
              class="gd-input gd-field-sm gd-num"
              aria-label="时长（毫秒）"
            />
            <span class="gd-num mt-1 block text-caption text-neutral-400">≈ {{ (form.duration_ms / 1000).toFixed(1) }}s（1000~15000ms）</span>
          </label>
        </div>
        <p v-if="durationWarning" class="mt-2 rounded-md border border-warning-border bg-warning-bg px-2.5 py-1.5 text-xs text-gd-pending">
          对白镜头时长超过 6 秒，观众停留感会明显下降，建议压缩对白或拆分镜头
        </p>
      </section>

      <!-- ===== 对白引用块（F4-03：金色引用 + 引用来源 meta） ===== -->
      <section v-if="shot.dialogue">
        <div class="rounded-lg border border-warning-border bg-gold-fog py-3 pl-4 pr-3">
          <p class="border-l-2 border-gold pl-3 font-serif text-sm leading-[1.75] text-neutral-800">
            「{{ shot.dialogue }}」
            <span
              v-if="currentShotVoice"
              class="ml-1 inline-block rounded px-1.5 py-0.5 align-middle text-[10px] font-semibold not-italic"
              :class="currentShotVoiceClass"
            >
              {{ currentShotVoice }}
            </span>
          </p>
          <p class="gd-num mt-1.5 border-t border-dashed border-gold-light pt-2 text-right text-caption text-neutral-500">
            —— {{ optionLabel(EMOTION_TAG_OPTIONS, shot.emotion_tag) || '旁白' }} · {{ (shot.duration_ms / 1000).toFixed(1) }}s ✓
          </p>
        </div>
        <p class="mt-1 text-caption text-neutral-400">对白（只读，来自剧本）</p>
      </section>

      <!-- ===== 画面描述（可编辑，保存 PATCH） ===== -->
      <section>
        <div class="mb-2.5 flex items-baseline justify-between">
          <h4 class="gd-kicker">画面描述 final_prompt</h4>
          <span class="gd-num text-caption text-neutral-400">{{ form.image_prompt.length }}/2000</span>
        </div>
        <textarea
          v-model="form.image_prompt"
          rows="4"
          maxlength="2000"
          :disabled="isLocked"
          placeholder="描述这一镜的画面内容，AI 将据此生成首帧…"
          class="gd-input"
        />
      </section>

      <!-- ===== 资产锚点区（F4-03：彩点 chips + 编辑锚点） ===== -->
      <section>
        <div class="mb-2.5 flex items-center justify-between">
          <h4 class="gd-kicker">出场角色绑定 asset_anchors</h4>
          <AppButton variant="secondary" size="sm" :disabled="isLocked" @click="openAnchorEditor">编辑锚点</AppButton>
        </div>

        <!-- 角色锚点 chips（紫点） -->
        <div class="flex flex-wrap items-center gap-1.5">
          <span
            v-for="id in shot.character_asset_ids ?? []"
            :key="`char-${id}`"
            class="inline-flex items-center gap-1.5 rounded-full border border-hairline bg-surface-raised py-1 pl-1.5 pr-2.5"
          >
            <span class="h-2.5 w-2.5 rounded-full bg-primary-500" aria-hidden="true" />
            <span class="text-xs text-neutral-700">{{ assetOf(id)?.name ?? `资产 #${id}` }}</span>
          </span>

          <!-- 场景锚点（绿点） -->
          <span
            v-if="shot.scene_asset_id"
            class="inline-flex items-center gap-1.5 rounded-full border border-hairline bg-surface-raised py-1 pl-1.5 pr-2.5"
          >
            <span class="h-2.5 w-2.5 rounded-full bg-success" aria-hidden="true" />
            <span class="text-xs text-neutral-700">{{ assetOf(shot.scene_asset_id)?.name ?? `场景 #${shot.scene_asset_id}` }}</span>
          </span>

          <!-- 添加锚点占位（打开选择器） -->
          <button
            type="button"
            class="inline-flex items-center rounded-full border border-dashed border-hairline-strong px-2.5 py-1 text-xs text-neutral-400 transition-colors hover:border-primary-400 hover:text-primary-600"
            :disabled="isLocked"
            @click="openAnchorEditor"
          >
            + 添加锚点
          </button>

          <!-- 缺锚点警示 -->
          <span
            v-if="!(shot.character_asset_ids ?? []).length && !shot.scene_asset_id"
            class="inline-flex items-center rounded-full border border-warning-border bg-warning-bg px-2.5 py-1 text-xs text-gd-pending"
          >
            ⚠ 未绑定锚点（锁定分镜前须完成绑定）
          </span>
        </div>

        <!-- 分镜生成告警（如场景资产未匹配；2026-11 新增） -->
        <div
          v-if="(shot.anchor_qc?.warnings?.length ?? 0) > 0"
          class="mt-2 rounded-md border border-warning-border bg-warning-bg px-3 py-2 text-xs leading-[1.65] text-gd-pending"
          role="alert"
        >
          <p class="font-medium">分镜生成告警</p>
          <p
            v-for="(warning, index) in shot.anchor_qc?.warnings ?? []"
            :key="`anchor-warning-${index}`"
            class="mt-0.5"
          >
            · {{ warning }}
          </p>
        </div>

        <!-- 剧本锚点失配警示（渲染后 QC 软校验命中时展示，2026-11 新增） -->
        <div
          v-if="shot.anchor_qc?.mismatch"
          class="mt-2 rounded-md border border-danger-border bg-danger-bg px-3 py-2 text-xs leading-[1.65] text-danger"
          role="alert"
        >
          <p class="font-medium">画面与剧本锚点不一致，建议人工复核或重出首帧</p>
          <p
            v-for="(reason, index) in shot.anchor_qc.reasons"
            :key="`anchor-qc-${index}`"
            class="mt-0.5"
          >
            · {{ reason }}
          </p>
        </div>

        <!-- final_prompt 只读代码块（可折叠） -->
        <div v-if="shot.final_prompt" class="mt-3">
          <button type="button" class="flex w-full items-center justify-between rounded-md bg-surface-canvas px-3 py-2 text-left text-xs text-neutral-500 transition-colors hover:bg-surface-sunken" @click="promptOpen = !promptOpen">
            <span class="font-medium">final_prompt（绑定锚点后组装，只读）</span>
            <span>{{ promptOpen ? '收起 ▲' : '展开 ▼' }}</span>
          </button>
          <pre v-show="promptOpen" class="mt-1.5 max-h-44 overflow-y-auto whitespace-pre-wrap break-all rounded-md border border-hairline bg-surface-canvas p-3 font-mono text-xs leading-[1.65] text-neutral-600">{{ shot.final_prompt }}</pre>
        </div>
      </section>

      <!-- ===== 摘要信息 ===== -->
      <section class="rounded-lg bg-surface-canvas p-4 text-xs leading-[1.65] text-neutral-500">
        <p>景别：{{ optionLabel(SHOT_SIZE_OPTIONS, shot.shot_size) }} · 运镜：{{ optionLabel(CAMERA_MOVE_OPTIONS, shot.camera_move) }} · 情绪：{{ optionLabel(EMOTION_TAG_OPTIONS, shot.emotion_tag) }}</p>
        <p v-if="shot.action_desc" class="mt-1 line-clamp-2">动作描述：{{ shot.action_desc }}</p>
      </section>

      <!-- ===== 镜头明细（原草稿预览页右栏列表，点击切换当前镜） ===== -->
      <section aria-label="镜头明细">
        <div class="mb-2.5 flex items-baseline justify-between">
          <h4 class="gd-kicker">镜头明细 · 点击跳转</h4>
          <span class="gd-num text-caption text-neutral-400">{{ episodeShots.length }} 镜</span>
        </div>
        <ul v-if="episodeShots.length > 0" class="max-h-64 space-y-1 overflow-y-auto">
          <li
            v-for="item in episodeShots"
            :key="`detail-${item.id}`"
            class="flex cursor-pointer items-center gap-2.5 rounded-lg border p-2 transition-[border-color,background-color] duration-gd-fast ease-gd-standard"
            :class="String(item.id) === String(shot.id) ? 'border-primary-400 bg-primary-50' : 'border-transparent hover:bg-surface-canvas'"
            @click="selectEpisodeShot(item.id)"
          >
            <span class="gd-num flex h-7 w-10 shrink-0 items-center justify-center rounded-md bg-surface-canvas text-micro font-semibold text-neutral-600">
              {{ String(item.shot_no).padStart(2, '0') }}
            </span>
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-1.5">
                <span class="gd-num text-xs font-semibold text-neutral-800">SH-{{ String(item.shot_no).padStart(2, '0') }}</span>
                <span class="truncate text-caption text-neutral-400">{{ optionLabel(SHOT_SIZE_OPTIONS, item.shot_size) || '—' }}</span>
                <span v-if="String(item.id) === String(shot.id)" class="shrink-0 rounded-full bg-primary-100 px-1.5 py-px text-[9px] text-primary-300">当前</span>
              </div>
              <p class="truncate text-caption text-neutral-500">{{ item.dialogue || item.action_desc || item.image_prompt || '（无描述）' }}</p>
            </div>
            <span class="gd-num shrink-0 text-caption text-neutral-400">{{ (item.duration_ms / 1000).toFixed(1) }}s</span>
          </li>
        </ul>
        <p v-else class="rounded-lg bg-surface-canvas px-3 py-2.5 text-caption text-neutral-400">本集暂无镜头明细</p>
      </section>

      <!-- ===== 本集审批（原草稿预览页底部决策条；锁定后开放驳回 / approve） ===== -->
      <section v-if="storyboardId != null" aria-label="本集审批">
        <div class="mb-2.5 flex items-baseline justify-between">
          <h4 class="gd-kicker">本集审批 · HITL-3</h4>
          <StatusTag v-if="storyboardStatus" :status="storyboardStatus" size="sm" />
        </div>

        <div v-if="isStoryboardLocked" class="rounded-lg bg-surface-canvas p-3">
          <p class="text-caption leading-relaxed text-neutral-500">
            已锁定 · 等待 HITL-3 评审。通过后项目进入视频生成；驳回需填写 ≥10 字反馈，分镜将解锁回画布。
          </p>
          <div class="mt-2.5 flex items-center gap-2">
            <AppButton variant="secondary" size="sm" :disabled="deciding" @click="openRejectDialog">驳回并反馈</AppButton>
            <AppButton variant="primary" size="sm" class="flex-1" :loading="deciding" @click="approveDialogOpen = true">
              通过本集
            </AppButton>
          </div>
        </div>
        <p v-else class="rounded-lg bg-surface-canvas px-3 py-2.5 text-caption leading-relaxed text-neutral-500">
          分镜尚未锁定：完成锚点绑定后，使用画布底部「批准分镜并送审」创建 HITL-3 审批单，锁定后此处即可驳回或通过。
        </p>
      </section>
    </div>

    <!-- ===== 底部操作（F4-03：重出首帧 / 保存 / 关闭） ===== -->
    <template #footer>
      <div class="flex items-center gap-2.5">
        <AppButton
          variant="secondary"
          size="md"
          :loading="redrawing"
          :disabled="isLocked"
          :title="isLocked ? '镜头已锁定：请先驳回本集分镜解锁后再重出首帧' : '按当前锚点与构图重出本镜整幅要素画面（本地合成，0 积分）'"
          @click="redrawFrame"
        >
          ↻ 重出首帧
        </AppButton>
        <AppButton variant="primary" size="md" :loading="saving" :disabled="isLocked || !dirty" @click="handleSave">
          {{ isLocked ? '已锁定' : dirty ? '保存修改' : '无修改' }}
        </AppButton>
        <div class="flex-1" />
        <AppButton variant="ghost" size="md" @click="modelValue = false">关闭</AppButton>
      </div>
    </template>
  </DrawerPanel>

  <!-- ===== 本集审批通过二次确认（HITL-3 approve） ===== -->
  <ConfirmDialog
    v-model="approveDialogOpen"
    title="确认审批通过本集分镜"
    content="通过后项目将进入「生成」阶段（rendering），开始正式渲染视频并消耗积分。该操作将记录为分镜审批（HITL-3）的通过决策。"
    confirm-label="确认通过"
    :loading="deciding"
    @confirm="confirmApprove"
  />

  <!-- ===== 本集审批驳回（decide reject，反馈 ≥10 字强校验） ===== -->
  <ConfirmDialog
    v-model="rejectDialogOpen"
    title="驳回本集分镜？"
    confirm-label="确认驳回"
    :loading="deciding"
    @confirm="confirmReject"
  >
    <p>请填写至少 10 个字的驳回原因，分镜将解锁回画布，导演侧按反馈重新调整镜头。</p>
    <textarea
      v-model="rejectFeedback"
      rows="3"
      class="gd-input mt-3"
      placeholder="例如：节奏太拖，前 3 镜合并；第 5 镜改为角色特写…"
    />
    <p class="mt-1.5 text-xs" :class="rejectFeedback.trim().length >= 10 ? 'text-success' : 'text-neutral-400'">
      {{ rejectFeedback.trim().length }}/10 字（至少 10 字）
    </p>
  </ConfirmDialog>

  <!-- ===== 编辑锚点弹层（Modal 基座，自动获得焦点陷阱 / 遮罩令牌 / 进出场动画） ===== -->
  <Modal v-model="anchorOpen" title="编辑镜头锚点" :max-width="520">
    <p class="leading-[1.65] text-caption text-neutral-500">
      仅「已锁定」状态的资产可绑定锚点；绑定后系统自动组装 final_prompt 注入外观一致性描述。未锁定的资产请先在资产墙完成审核并锁定。
    </p>

    <div v-if="anchorLoading" class="py-10 text-center text-sm text-neutral-400">资产加载中…</div>

    <div v-else class="mt-4 max-h-[56vh] space-y-5 overflow-y-auto pr-1">
      <!-- 角色资产多选 -->
      <div>
        <h4 class="gd-kicker mb-2">出场角色（可多选）</h4>
        <div v-if="characterAssets.length === 0" class="rounded-md border border-dashed border-hairline px-3 py-4 text-center text-xs text-neutral-400">项目中暂无角色资产，请先到资产墙生成</div>
        <div v-else class="grid grid-cols-2 gap-2">
          <button
            v-for="asset in characterAssets"
            :key="asset.id"
            type="button"
            class="flex items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left transition-[border-color,background-color] duration-gd-fast ease-gd-standard"
            :class="[
              pickedCharacterIds.some((id) => String(id) === String(asset.id)) ? 'border-primary-400 bg-primary-50' : 'border-hairline bg-surface-raised hover:border-hairline-strong',
              !isBindable(asset) ? 'cursor-not-allowed opacity-60' : '',
            ]"
            @click="toggleCharacter(asset)"
          >
            <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary-100 text-sm">👤</span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-xs font-medium text-neutral-800">{{ asset.name }}</span>
              <span class="block text-caption" :class="isBindable(asset) ? 'text-success' : 'text-gd-pending'">
                {{ isBindable(asset) ? '已锁定 · 可绑定' : '未锁定 · 不可绑定' }}
              </span>
            </span>
            <span
              v-if="pickedCharacterIds.some((id) => String(id) === String(asset.id))"
              class="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-[var(--gd-violet)] text-micro text-[#0b0a1e]"
              aria-hidden="true"
            >✓</span>
          </button>
        </div>
      </div>

      <!-- 场景资产单选 -->
      <div>
        <h4 class="gd-kicker mb-2">场景（单选，可不选）</h4>
        <div v-if="sceneAssets.length === 0" class="rounded-md border border-dashed border-hairline px-3 py-4 text-center text-xs text-neutral-400">项目中暂无场景资产，请先到资产墙生成</div>
        <div v-else class="grid grid-cols-2 gap-2">
          <button
            v-for="asset in sceneAssets"
            :key="asset.id"
            type="button"
            class="flex items-center gap-2.5 rounded-lg border px-2.5 py-2 text-left transition-[border-color,background-color] duration-gd-fast ease-gd-standard"
            :class="[
              String(pickedSceneId) === String(asset.id) ? 'border-primary-400 bg-primary-50' : 'border-hairline bg-surface-raised hover:border-hairline-strong',
              !isBindable(asset) ? 'cursor-not-allowed opacity-60' : '',
            ]"
            @click="isBindable(asset) && (pickedSceneId = pickedSceneId === asset.id ? '' : asset.id)"
          >
            <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-success-bg text-sm">🏞️</span>
            <span class="min-w-0 flex-1">
              <span class="block truncate text-xs font-medium text-neutral-800">{{ asset.name }}</span>
              <span class="block text-caption" :class="isBindable(asset) ? 'text-success' : 'text-gd-pending'">
                {{ isBindable(asset) ? '已锁定 · 可绑定' : '未锁定 · 不可绑定' }}
              </span>
            </span>
          </button>
        </div>
      </div>
    </div>

    <template #footer>
      <AppButton variant="secondary" size="md" :disabled="anchorSubmitting" @click="anchorOpen = false">取消</AppButton>
      <AppButton variant="primary" size="md" :loading="anchorSubmitting" @click="submitAnchors">确认绑定</AppButton>
    </template>
  </Modal>
</template>

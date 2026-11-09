<!--
  文件级注释：P11 · 真人照片授权（四步向导，路由 /projects/:projectId/assets/authorizations/new）。
  v7 纸上放映厅 Pro · 对照 F3-05（P11 真人授权）重构：
  - 顶部琥珀色合规风险提示横幅（红色阻断横幅样式保留，演示环境不触发）；
  - 步骤指示器：数字圆点（active 品牌紫 / done 暖金）+ 连接线；
  - 步骤 2 上传照片：拖拽上传 + 照片预览 + 模拟人脸比对块——检测中骨架（约 1.4s）→
    相似度大数字（.gd-num，演示 93.2% ≥ 80% 达标，绿）+ threshold 0.80 说明行；
    比对失败阻断样式保留（<80% 红字 + 阻断条，演示环境不触发）；
  - 步骤 3 授权声明：授权类型胶囊单选卡（self · 本人照片 / authorized_other · 已获授权他人）+
    协议文本滚动框 + 勾选确认；
  - 步骤 4 确认提交 + 成功页授权凭证（时间戳 / IP 掩码 / 协议版本）。
  功能逻辑（API / 校验 / 步骤回退 / 空态）全部保留，仅视觉与交互层重构。
-->
<script setup lang="ts">
/**
 * FaceAuthorizationView 真人照片授权向导。
 * props：无。emits：无。
 * 依赖 assets API（角色列表 / 授权留痕）、useToast。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { createFaceAuthorization, listAssets, type AssetItem, type FaceAuthorizationRecord } from '@/api/assets'
import AppButton from '@/components/common/AppButton.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import { formatShortDate } from '@/utils/datetime'

/** 路由实例 */
const route = useRoute()
const router = useRouter()
/** Toast */
const toast = useToast()

/** 项目 ID */
const projectId = computed(() => String(route.params.projectId ?? ''))

/* ==================== 步骤状态 ==================== */

/** 步骤定义 */
const STEPS = [
  { key: 'asset', label: '选择资产', icon: '🎭' },
  { key: 'photo', label: '上传照片', icon: '📷' },
  { key: 'agreement', label: '授权声明', icon: '📝' },
  { key: 'submit', label: '确认提交', icon: '✅' },
] as const

/** 当前步骤（1-4；5 表示成功页） */
const currentStep = ref(1)

/* ==================== 模拟人脸比对（F3-05 相似度大数字） ====================
 * MVP 不做真实人脸检测：照片选定后展示「检测中骨架 → 比对结果」演示链路；
 * 结果恒为达标（93.2% ≥ 80%）。失败阻断样式保留（similarityBlocked），演示环境不触发。 */
/** 演示相似度（百分比） */
const SIMILARITY_DEMO_PERCENT = 93.2
/** 达标阈值（threshold 默认 0.80） */
const SIMILARITY_THRESHOLD_PERCENT = 80
/** 比对状态：idle 未开始 / detecting 检测中 / done 完成 */
const detectState = ref<'idle' | 'detecting' | 'done'>('idle')
/** 比对失败阻断（样式保留位：命中公众人物库 / 相似度不达标时展示，演示环境不触发） */
const similarityBlocked = ref(false)
/** 检测模拟定时器 */
let detectTimer: ReturnType<typeof setTimeout> | null = null

// 卸载：清理定时器
onUnmounted(() => {
  if (detectTimer) {
    clearTimeout(detectTimer)
    detectTimer = null
  }
})

/* ==================== 步骤 1：选择资产 ==================== */

/** 角色列表 */
const characters = ref<AssetItem[]>([])
/** 列表加载中 */
const loading = ref(true)
/** 列表加载失败 */
const loadError = ref('')
/** 选中的角色资产 */
const selectedAsset = ref<AssetItem | null>(null)

/**
 * 拉取项目角色列表（asset_type=character）。
 * @returns 无
 */
async function fetchCharacters(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    const page = await listAssets(projectId.value, { asset_type: 'character', page: 1, page_size: 100 })
    characters.value = page.items
    // 支持 ?assetId= 预选（从资产详情页跳转场景）
    const preselect = route.query.assetId
    if (preselect) {
      const found = characters.value.find((a) => String(a.id) === String(preselect))
      if (found) selectedAsset.value = found
    }
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '角色列表加载失败'
  } finally {
    loading.value = false
  }
}

/* ==================== 步骤 2：上传照片 ==================== */

/** 已选照片文件 */
const photoFile = ref<File | null>(null)
/** 照片预览 URL（objectURL，卸载时回收） */
const photoPreviewUrl = ref('')
/** 拖拽悬停态 */
const dragOver = ref(false)
/** 文件输入引用（点击触发） */
const fileInputRef = ref<HTMLInputElement | null>(null)

// 照片选定后启动检测模拟；清除照片时复位
watch(photoFile, (file) => {
  if (detectTimer) {
    clearTimeout(detectTimer)
    detectTimer = null
  }
  if (!file) {
    detectState.value = 'idle'
    return
  }
  detectState.value = 'detecting'
  detectTimer = setTimeout(() => {
    detectState.value = 'done'
    detectTimer = null
  }, 1_400)
})

/**
 * 校验并设置照片文件（仅图片，≤8MB）。
 * @param file 用户选择文件
 * @returns 是否有效
 */
function setPhotoFile(file: File | null): boolean {
  if (!file) return false
  if (!file.type.startsWith('image/')) {
    toast.warning('请上传图片格式文件（JPG / PNG / WebP）')
    return false
  }
  if (file.size > 8 * 1024 * 1024) {
    toast.warning('照片不能超过 8MB')
    return false
  }
  if (photoPreviewUrl.value) {
    URL.revokeObjectURL(photoPreviewUrl.value)
  }
  photoFile.value = file
  photoPreviewUrl.value = URL.createObjectURL(file)
  return true
}

/**
 * 文件选择变化回调。
 * @param event input change 事件
 * @returns 无
 */
function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  setPhotoFile(file)
  input.value = ''
}

/**
 * 拖拽放下回调。
 * @param event drop 事件
 * @returns 无
 */
function onDrop(event: DragEvent): void {
  dragOver.value = false
  const file = event.dataTransfer?.files?.[0] ?? null
  setPhotoFile(file)
}

/**
 * 清除已选照片。
 * @returns 无
 */
function clearPhoto(): void {
  if (photoPreviewUrl.value) {
    URL.revokeObjectURL(photoPreviewUrl.value)
  }
  photoFile.value = null
  photoPreviewUrl.value = ''
}

/* ==================== 步骤 3：授权声明 ==================== */

/** 授权类型（self 本人 / authorized_other 授权他人） */
const authorizationType = ref<'self' | 'authorized_other'>('self')
/** 协议版本（提交留痕） */
const AGREEMENT_VERSION = 'v1.0'
/** 已阅读并同意协议 */
const agreed = ref(false)

/** 授权类型选项 */
const AUTH_TYPE_OPTIONS: Array<{ value: 'self' | 'authorized_other'; label: string; desc: string }> = [
  { value: 'self', label: '本人照片', desc: '照片中人物为本人，同意按协议授权使用' },
  { value: 'authorized_other', label: '已获授权他人', desc: '已获得照片中人物本人授权，代为提交' },
]

/* ==================== 步骤 4：提交与成功页 ==================== */

/** 提交中 */
const submitting = ref(false)
/** 授权成功记录 */
const authRecord = ref<FaceAuthorizationRecord | null>(null)

/**
 * 将后端返回的 IP 掩码处理（保留前两段，其余以 * 代替；IPv6 / 无值显示 —）。
 * @param ip 原始 IP
 * @returns 掩码 IP
 */
function maskIp(ip: string | null): string {
  if (!ip) return '—'
  const parts = ip.split('.')
  if (parts.length === 4) {
    return `${parts[0]}.${parts[1]}.*.*`
  }
  return ip.length > 8 ? `${ip.slice(0, 8)}****` : ip
}

/**
 * 提交授权（POST face-authorization，IP / UA / 时间戳由后端存证）。
 * @returns 无
 */
async function submitAuthorization(): Promise<void> {
  if (!selectedAsset.value) return
  submitting.value = true
  try {
    authRecord.value = await createFaceAuthorization(selectedAsset.value.id, {
      authorization_type: authorizationType.value,
      agreement_version: AGREEMENT_VERSION,
    })
    currentStep.value = 5
    toast.success('授权记录已留存')
  } catch {
    // 拦截器已 toast
  } finally {
    submitting.value = false
  }
}

/**
 * 步骤校验：不满足时 toast 提示并阻止前进。
 * @returns 是否允许前进
 */
function validateCurrentStep(): boolean {
  if (currentStep.value === 1 && !selectedAsset.value) {
    toast.warning('请先选择要授权的角色资产')
    return false
  }
  if (currentStep.value === 2 && !photoFile.value) {
    toast.warning('请先上传本人或已获授权的照片')
    return false
  }
  if (currentStep.value === 2 && similarityBlocked.value) {
    toast.warning('照片比对未达标，授权被阻断，请更换照片')
    return false
  }
  if (currentStep.value === 3 && !agreed.value) {
    toast.warning('请先阅读并勾选同意授权协议')
    return false
  }
  return true
}

/**
 * 下一步（带步骤校验）。
 * @returns 无
 */
function goNext(): void {
  if (!validateCurrentStep()) return
  currentStep.value += 1
}

/**
 * 上一步。
 * @returns 无
 */
function goPrev(): void {
  if (currentStep.value > 1) currentStep.value -= 1
}

/**
 * 成功页：继续为其他角色授权（重置向导）。
 * @returns 无
 */
function authorizeAnother(): void {
  authRecord.value = null
  selectedAsset.value = null
  clearPhoto()
  agreed.value = false
  authorizationType.value = 'self'
  currentStep.value = 1
  void fetchCharacters()
}

/**
 * 成功页：返回资产卡片墙。
 * @returns 无
 */
function backToAssets(): void {
  void router.push(`/projects/${projectId.value}/assets`)
}

// 挂载：拉角色列表
onMounted(() => {
  void fetchCharacters()
})
</script>

<template>
  <div class="h-full overflow-y-auto bg-surface-canvas gd-desk-light">
    <!-- ============ 顶部固定风险提示横幅（琥珀色，F3-05 合规提示条） ============ -->
    <div class="sticky top-0 z-sticky border-b border-warning-border bg-warning-bg px-6 py-3" role="alert">
      <div class="mx-auto flex max-w-content-md items-start gap-2.5">
        <svg class="mt-0.5 h-4 w-4 shrink-0 text-warning" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <p class="text-xs leading-[1.65] text-warning-strong">
          合规风险提示：真人照片仅限本人或已获书面授权的个体使用；命中公众人物库将被强制阻断并记录审计。上传即视为同意合规审查。
        </p>
      </div>
    </div>

    <!-- 红色阻断横幅（样式保留：命中公众人物库时展示，演示环境不触发） -->
    <div class="mx-auto max-w-content-md px-6 pt-4">
      <div class="hidden rounded-lg border border-danger-border bg-danger-bg p-4" role="alert" aria-hidden="true">
        <p class="text-xs font-medium leading-[1.65] text-danger-strong">⛔ 照片命中公众人物库，授权已被强制阻断。请更换照片或联系平台管理员申诉。</p>
      </div>
    </div>

    <div class="mx-auto max-w-content-md px-6 py-8">
      <!-- ============ 步骤指示器（成功页隐藏；active 品牌紫 / done 暖金） ============ -->
      <ol v-if="currentStep <= 4" class="mb-8 flex items-center gap-5" aria-label="授权步骤">
        <li v-for="(step, index) in STEPS" :key="step.key" class="flex items-center" :class="index < STEPS.length - 1 ? 'flex-1' : ''">
          <div class="flex flex-col items-center gap-1.5">
            <span
              class="flex h-9 w-9 items-center justify-center rounded-full border-2 text-sm font-semibold transition-colors duration-gd-normal"
              :class="currentStep > index + 1 ? 'border-gold-light bg-gold-fog text-gold-deep' : currentStep === index + 1 ? 'border-primary-500 gd-fill-brand shadow-gd-1' : 'border-hairline-strong bg-surface-raised text-neutral-400'"
              :aria-current="currentStep === index + 1 ? 'step' : undefined"
            >
              <svg v-if="currentStep > index + 1" class="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <template v-else>{{ index + 1 }}</template>
            </span>
            <span class="text-xs" :class="currentStep >= index + 1 ? 'font-medium text-neutral-800' : 'text-neutral-400'">{{ step.label }}</span>
          </div>
          <span v-if="index < STEPS.length - 1" class="mx-3 mb-5 h-px flex-1" :class="currentStep > index + 1 ? 'bg-gold' : 'bg-hairline'" aria-hidden="true" />
        </li>
      </ol>

      <!-- ============ 加载 / 错误态 ============ -->
      <div v-if="loading" class="gd-card p-6">
        <LoadingSkeleton variant="card" :rows="2" />
      </div>
      <div v-else-if="loadError">
        <ErrorRetryBlock message="角色列表加载失败" :description="loadError" @retry="fetchCharacters" />
      </div>

      <!-- ============ 成功页（步骤 5） ============ -->
      <div v-else-if="currentStep === 5 && authRecord" class="gd-card p-6">
        <div class="flex flex-col items-center text-center">
          <span class="flex h-16 w-16 items-center justify-center rounded-2xl bg-success-bg text-3xl" aria-hidden="true">✅</span>
          <h2 class="mt-4 font-serif text-display-lg text-neutral-900">授权完成</h2>
          <StatusTag status="approved" label="active · 已授权" size="sm" class="mt-2.5" />
          <p class="mt-1.5 text-sm text-neutral-500">授权凭证已生成并存证，可用于后续真人形象短剧生成</p>
        </div>

        <!-- 授权凭证 -->
        <section class="mt-6 rounded-lg border border-hairline bg-surface-canvas p-5" aria-label="授权凭证">
          <h3 class="text-xs font-semibold tracking-wide text-neutral-400">授权凭证</h3>
          <dl class="mt-3 space-y-2.5 text-sm">
            <div class="flex justify-between gap-6">
              <dt class="shrink-0 text-neutral-500">凭证编号</dt>
              <dd class="gd-num text-neutral-800">#{{ authRecord.id }}</dd>
            </div>
            <div class="flex justify-between gap-6">
              <dt class="shrink-0 text-neutral-500">授权角色</dt>
              <dd class="text-neutral-800">{{ selectedAsset?.name ?? `资产 #${authRecord.asset_id}` }}</dd>
            </div>
            <div class="flex justify-between gap-6">
              <dt class="shrink-0 text-neutral-500">授权类型</dt>
              <dd class="text-neutral-800">{{ authRecord.authorization_type === 'self' ? '本人授权' : '授权他人' }}</dd>
            </div>
            <div class="flex justify-between gap-6">
              <dt class="shrink-0 text-neutral-500">协议版本</dt>
              <dd class="gd-num text-neutral-800">{{ authRecord.protocol_version }}</dd>
            </div>
            <div class="flex justify-between gap-6">
              <dt class="shrink-0 text-neutral-500">授权时间</dt>
              <dd class="gd-num text-neutral-800">{{ authRecord.authorized_at ? formatShortDate(authRecord.authorized_at) : '—' }}</dd>
            </div>
            <div class="flex justify-between gap-6">
              <dt class="shrink-0 text-neutral-500">记录 IP（掩码）</dt>
              <dd class="gd-num text-neutral-800">{{ maskIp(authRecord.ip) }}</dd>
            </div>
          </dl>
          <p class="mt-3 text-caption leading-[1.65] text-neutral-500">凭证由平台留痕存证（时间戳 / 客户端 IP / UA），如需核查授权链路请提供凭证编号。</p>
        </section>

        <div class="mt-6 flex justify-center gap-3">
          <AppButton variant="secondary" size="md" @click="authorizeAnother">继续为其他角色授权</AppButton>
          <AppButton variant="primary" size="md" @click="backToAssets">返回资产卡片墙</AppButton>
        </div>
      </div>

      <!-- ============ 步骤 1：选择资产 ============ -->
      <div v-else-if="currentStep === 1" class="gd-card p-6">
        <h2 class="font-serif text-title text-neutral-900">选择要授权的角色资产</h2>
        <p class="mt-1 text-sm text-neutral-500">仅角色类型资产需要真人照片授权；选择后进入照片上传。</p>

        <!-- 空态 -->
        <div v-if="characters.length === 0" class="py-10 text-center">
          <p class="text-3xl" aria-hidden="true">🎭</p>
          <p class="mt-3 text-sm text-neutral-500">项目暂无角色资产，请先在资产卡片墙完成「智能规划资产」</p>
          <AppButton class="mt-4" variant="secondary" size="md" @click="backToAssets">返回资产卡片墙</AppButton>
        </div>

        <!-- 角色单选卡列表 -->
        <ul v-else class="mt-4 max-h-[360px] space-y-2.5 overflow-y-auto pr-1">
          <li v-for="asset in characters" :key="asset.id">
            <button
              type="button"
              class="flex w-full items-center gap-3 rounded-lg border p-4 text-left transition-[background-color,border-color,color] duration-gd-normal ease-gd-standard"
              :class="selectedAsset?.id === asset.id ? 'border-primary-500 bg-primary-50' : 'border-hairline bg-surface-raised hover:border-primary-300'"
              :aria-pressed="selectedAsset?.id === asset.id"
              @click="selectedAsset = asset"
            >
              <span class="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-primary-100 to-primary-50 text-xl" aria-hidden="true">🎭</span>
              <div class="min-w-0 flex-1">
                <p class="truncate text-sm font-medium text-neutral-900">{{ asset.name }}</p>
                <p class="truncate text-xs text-neutral-400">{{ asset.description || '暂无外观描述' }}</p>
              </div>
              <StatusTag :status="asset.status" size="sm" />
              <span
                class="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2"
                :class="selectedAsset?.id === asset.id ? 'border-primary-500 gd-fill-brand' : 'border-hairline-strong'"
                aria-hidden="true"
              >
                <svg v-if="selectedAsset?.id === asset.id" class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3">
                  <path d="M4.5 12.75l6 6 9-13.5" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </span>
            </button>
          </li>
        </ul>
      </div>

      <!-- ============ 步骤 2：上传照片 + 模拟比对（F3-02 活体/相似度） ============ -->
      <div v-else-if="currentStep === 2" class="gd-card p-6">
        <h2 class="font-serif text-title text-neutral-900">上传真人照片</h2>
        <p class="mt-1 text-sm text-neutral-500">为「{{ selectedAsset?.name }}」上传正面清晰照（JPG / PNG / WebP，≤ 8MB）</p>

        <!-- 拖拽上传区 -->
        <div
          v-if="!photoPreviewUrl"
          class="mt-4 flex flex-col items-center justify-center rounded-lg border border-dashed px-6 py-12 text-center transition-[border-color,background-color] duration-gd-fast"
          :class="dragOver ? 'border-primary-400 bg-primary-50' : 'border-hairline-strong bg-surface-canvas hover:border-primary-300'"
          role="button"
          tabindex="0"
          aria-label="拖拽或点击上传照片"
          @click="fileInputRef?.click()"
          @keydown.enter="fileInputRef?.click()"
          @dragover.prevent="dragOver = true"
          @dragleave.prevent="dragOver = false"
          @drop.prevent="onDrop"
        >
          <span class="text-4xl" aria-hidden="true">📤</span>
          <p class="mt-3 text-sm font-medium text-neutral-700">拖拽照片到此处，或<span class="text-primary-600">点击选择文件</span></p>
          <p class="mt-1 text-xs text-neutral-400">正面免冠、光线均匀、面部无遮挡效果最佳</p>
          <input ref="fileInputRef" type="file" accept="image/*" class="hidden" @change="onFileChange" />
        </div>

        <!-- 预览 + 比对结果 -->
        <div v-else class="mt-4">
          <div class="flex flex-wrap items-start gap-5">
            <!-- 照片预览 -->
            <div class="relative w-40 shrink-0 overflow-hidden rounded-lg border border-hairline">
              <img :src="photoPreviewUrl" alt="已上传照片预览" class="aspect-[3/4] w-full object-cover" />
              <button
                type="button"
                class="absolute right-2 top-2 flex h-7 w-7 items-center justify-center rounded-full bg-[rgba(4,7,15,0.72)] text-[color:var(--gd-ink)] backdrop-blur-[4px] transition-colors hover:bg-danger hover:text-white"
                aria-label="移除照片"
                @click="clearPhoto"
              >
                ✕
              </button>
            </div>

            <!-- 比对结果卡（F3-05 比对结果：大数字 + 达标 chips + threshold 行） -->
            <div class="min-w-0 flex-1">
              <!-- 检测中骨架 -->
              <div v-if="detectState === 'detecting'" class="rounded-lg bg-surface-canvas p-4" role="status" aria-label="人脸比对检测中">
                <div class="flex items-center gap-4">
                  <div class="gd-skeleton h-14 w-14 rounded-xl" />
                  <div class="flex-1 space-y-2">
                    <div class="gd-skeleton h-4 w-32 rounded" />
                    <div class="gd-skeleton h-3 w-48 rounded" />
                  </div>
                </div>
                <p class="mt-3 text-caption text-neutral-400">正在进行人脸质量检测与相似度比对…</p>
              </div>

              <!-- 比对结果（演示恒达标） -->
              <div v-else-if="detectState === 'done'" class="rounded-lg bg-surface-canvas p-4">
                <div class="flex flex-wrap items-center gap-4">
                  <p class="gd-num text-display-lg font-semibold leading-none" :class="similarityBlocked ? 'text-danger' : 'text-success'">
                    {{ similarityBlocked ? '41.6' : SIMILARITY_DEMO_PERCENT }}<span class="text-base">%</span>
                  </p>
                  <div class="min-w-0 flex-1">
                    <p class="text-sm font-medium" :class="similarityBlocked ? 'text-danger' : 'text-neutral-800'">
                      与角色「{{ selectedAsset?.name }}」参考比对{{ similarityBlocked ? ' · 未达阈值，已阻断' : ' · 达标' }}
                    </p>
                    <div class="mt-1.5 flex flex-wrap gap-1.5">
                      <span
                        class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-caption font-medium"
                        :class="similarityBlocked ? 'bg-danger-bg text-danger-strong' : 'bg-success-bg text-success-strong'"
                      >
                        <span class="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
                        similarity · {{ similarityBlocked ? '不达标' : '达标' }}
                      </span>
                      <span class="inline-flex items-center gap-1 rounded-full bg-success-bg px-2 py-0.5 text-caption font-medium text-success-strong">
                        <span class="h-1.5 w-1.5 rounded-full bg-success" aria-hidden="true" />活体检测通过
                      </span>
                      <span class="inline-flex items-center rounded-full bg-surface-sunken px-2 py-0.5 text-caption text-neutral-500">单张人脸</span>
                    </div>
                  </div>
                </div>
                <p class="gd-num mt-3 border-t border-hairline pt-2.5 text-right text-caption text-neutral-400">threshold 默认 {{ (SIMILARITY_THRESHOLD_PERCENT / 100).toFixed(2) }}</p>
              </div>

              <!-- 照片文件信息 -->
              <p class="gd-num mt-2 text-xs text-neutral-400">{{ photoFile?.name }}（{{ Math.ceil((photoFile?.size ?? 0) / 1024) }} KB）</p>
            </div>
          </div>
        </div>

        <!-- 比对失败阻断卡（样式保留：相似度 <80% 或命中公众人物库时展示，演示环境不触发） -->
        <div v-if="similarityBlocked" class="mt-4 flex items-start gap-2.5 rounded-lg border border-danger-border bg-danger-bg p-4" role="alert">
          <span class="mt-0.5 text-sm" aria-hidden="true">⛔</span>
          <p class="text-caption leading-[1.65] text-danger-strong">
            比对失败阻断：相似度 41.6% 低于阈值 80%，授权已被强制阻断。请更换照片或联系平台管理员申诉。
          </p>
        </div>

        <!-- 演示环境提示（MVP 不做真实人脸检测） -->
        <div class="mt-4 flex items-start gap-2.5 rounded-lg border border-info-border bg-info-bg p-4">
          <span class="mt-0.5 text-sm" aria-hidden="true">🧪</span>
          <p class="text-caption leading-[1.65] text-neutral-500">演示环境：模拟人脸质量校验通过。正式环境将在此执行人脸质量检测（清晰度 / 正脸 / 单人）与公众人物库相似度比对。</p>
        </div>
      </div>

      <!-- ============ 步骤 3：授权声明（F3-05：类型胶囊单选卡 + 协议勾选） ============ -->
      <div v-else-if="currentStep === 3" class="gd-card p-6">
        <h2 class="font-serif text-title text-neutral-900">签署授权声明</h2>

        <!-- 授权类型单选（F3-05 胶囊卡形态） -->
        <fieldset class="mt-4">
          <legend class="text-xs font-medium text-neutral-700">授权类型 <span class="text-danger">*</span></legend>
          <div class="mt-2 grid grid-cols-1 gap-2.5 sm:grid-cols-2">
            <label
              v-for="option in AUTH_TYPE_OPTIONS"
              :key="option.value"
              class="flex cursor-pointer items-center gap-2.5 rounded-full border px-4 py-3 transition-[border-color,background-color] duration-gd-fast ease-gd-standard"
              :class="authorizationType === option.value ? 'border-primary-500 bg-primary-50' : 'border-hairline bg-surface-raised hover:border-primary-300'"
            >
              <input v-model="authorizationType" type="radio" name="authorization-type" :value="option.value" class="h-4 w-4 accent-primary-500" />
              <span>
                <span class="block text-sm font-medium text-neutral-800">{{ option.value }} · {{ option.label }}</span>
                <span class="mt-0.5 block text-xs text-neutral-500">{{ option.desc }}</span>
              </span>
            </label>
          </div>
        </fieldset>

        <!-- 授权协议文本 -->
        <section class="mt-4 max-h-56 overflow-y-auto rounded-lg border border-hairline bg-surface-canvas p-4" aria-label="授权协议文本">
          <h3 class="text-sm font-semibold text-neutral-800">真人形象授权协议（{{ AGREEMENT_VERSION }}）</h3>
          <div class="mt-2 space-y-2 text-xs leading-[1.65] text-neutral-600">
            <p><span class="font-medium text-neutral-800">1. 使用范围：</span>授权方同意将所上传照片用于「浮光造梦」平台内短剧内容的 AI 形象生成，包括角色主形象、三视图、表情集及分镜画面生成。</p>
            <p><span class="font-medium text-neutral-800">2. 授权期限：</span>自签署之日起至授权方在平台内主动撤销授权或删除关联项目之日止。</p>
            <p><span class="font-medium text-neutral-800">3. 数据安全：</span>照片仅用于本项目形象锚点比对与存证，不做其他用途，不向第三方提供；平台留存签署时间戳与客户端 IP 掩码凭证。</p>
            <p><span class="font-medium text-neutral-800">4. 合规承诺：</span>授权方承诺照片为本人或已获本人书面授权；命中公众人物库的照片将被强制阻断，伪造授权的法律责任由授权方承担。</p>
            <p><span class="font-medium text-neutral-800">5. 撤销方式：</span>授权方可随时联系平台撤销授权，撤销后已生成的项目内容按平台协议处理。</p>
          </div>
        </section>

        <!-- 协议勾选（F3-05：协议 v2.3 链接 + 使用说明） -->
        <label class="mt-4 flex cursor-pointer items-start gap-2.5 rounded-lg bg-surface-canvas p-4">
          <input v-model="agreed" type="checkbox" class="mt-0.5 h-4 w-4 accent-primary-500" />
          <span class="text-xs leading-[1.65] text-neutral-600">
            我已阅读并同意<span class="font-medium text-primary-600">《真人形象授权协议（{{ AGREEMENT_VERSION }}）》</span>，授权本项目在成片中使用该形象并承担相应授权责任。
          </span>
        </label>
        <div class="mt-3 flex items-start gap-2.5 rounded-lg border border-info-border bg-info-bg p-3.5">
          <span class="mt-0.5 text-caption" aria-hidden="true">ℹ️</span>
          <p class="text-caption leading-[1.65] text-neutral-500">照片仅用于本项目角色一致性比对，检测完成后加密存储、不作其他用途；授权记录含时间与 IP 假名，可随时在「授权记录页」输入角色名找回。</p>
        </div>
      </div>

      <!-- ============ 步骤 4：确认提交 ============ -->
      <div v-else-if="currentStep === 4" class="gd-card p-6">
        <h2 class="font-serif text-title text-neutral-900">确认并提交授权</h2>
        <dl class="mt-4 space-y-2.5 text-sm">
          <div class="flex justify-between gap-6 border-b border-hairline pb-2.5">
            <dt class="shrink-0 text-neutral-500">授权角色</dt>
            <dd class="font-medium text-neutral-800">{{ selectedAsset?.name }}</dd>
          </div>
          <div class="flex justify-between gap-6 border-b border-hairline pb-2.5">
            <dt class="shrink-0 text-neutral-500">照片文件</dt>
            <dd class="max-w-[60%] truncate text-neutral-800">{{ photoFile?.name }}</dd>
          </div>
          <div class="flex justify-between gap-6 border-b border-hairline pb-2.5">
            <dt class="shrink-0 text-neutral-500">相似度比对</dt>
            <dd class="gd-num font-medium text-success">{{ SIMILARITY_DEMO_PERCENT }}%（阈值 {{ SIMILARITY_THRESHOLD_PERCENT }}%）</dd>
          </div>
          <div class="flex justify-between gap-6 border-b border-hairline pb-2.5">
            <dt class="shrink-0 text-neutral-500">授权类型</dt>
            <dd class="text-neutral-800">{{ authorizationType === 'self' ? '本人授权' : '授权他人' }}</dd>
          </div>
          <div class="flex justify-between gap-6">
            <dt class="shrink-0 text-neutral-500">协议版本</dt>
            <dd class="gd-num text-neutral-800">{{ AGREEMENT_VERSION }}</dd>
          </div>
        </dl>
        <p class="mt-4 rounded-lg bg-info-bg p-3 text-caption leading-[1.65] text-neutral-500">提交后平台将留存授权凭证（时间戳 / 客户端 IP 掩码 / UA），照片本体不在本环节上传存档。</p>
      </div>

      <!-- ============ 底部步骤导航 ============ -->
      <div v-if="currentStep <= 4 && !loading && !loadError" class="mt-8 flex items-center justify-between">
        <AppButton variant="secondary" size="md" :disabled="currentStep === 1" @click="goPrev">上一步</AppButton>
        <AppButton v-if="currentStep < 4" variant="primary" size="md" @click="goNext">下一步</AppButton>
        <AppButton v-else variant="primary" size="md" :loading="submitting" @click="submitAuthorization">提交授权</AppButton>
      </div>
    </div>
  </div>
</template>

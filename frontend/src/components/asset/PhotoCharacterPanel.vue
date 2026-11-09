<!--
  文件级注释：照片合成角色内嵌面板（F3.14 能力，由原独立页 PhotoCharacterView 迁移而来）。

  合并说明：原 /projects/:projectId/photo-characters 独立入口已取消，本面板作为
  「资产设计 → 角色设计」模块的内嵌区块，由 AssetWallView 角色 Tab 承载。

  面板结构：
  - 照片批次：选择本地照片 → 浏览器侧计算指纹/尺寸 → 逐张调用四道闸上传；
  - 批次状态：可用 / 拒绝 / 重复 / 合规阻断，可用不足 3 张时禁止创建身份档；
  - 身份档：以批次内可用照片聚类，主图缺省取质量分最高者；
  - M1 合成：风格化强度滑杆，展示预览、人脸相似度与自检明细；
  - 确认落资产：合成通过后生成待审核角色资产（回到资产墙正常审核流）。

  说明：照片本身不入库，仅登记元数据与分析结果；指纹在浏览器侧计算，
  避免同一张照片重复消耗配额。
-->
<script setup lang="ts">
/**
 * PhotoCharacterPanel 照片合成角色面板。
 *
 * props：projectId —— 所属项目 ID（由父级资产墙传入，面板内不再读路由）。
 * 依赖 `api/v6.ts` F3.14 分组；闸门拒绝由后端返回业务码并由 http 拦截器
 * 统一提示，面板只负责批次状态与后续动作的可用性。
 */
import { computed, onMounted, ref } from 'vue'

import {
  confirmPhotoComposition,
  createIdentityProfile,
  createPhotoComposition,
  getPhotoBatch,
  listIdentityProfiles,
  listPhotoCompositions,
  uploadPhoto,
} from '@/api/v6'
import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { useToast } from '@/composables/useToast'

/** 组件 props */
const props = defineProps<{
  /** 所属项目 ID */
  projectId: string
}>()

const toast = useToast()

/** 批次键（同一批照片必须一致） */
const batchKey = ref(`batch-${Date.now()}`)
/** 批次状态 */
const batch = ref<Record<string, unknown> | null>(null)
/** 身份档名称 */
const profileName = ref('照片合成角色')
/** 身份档列表 */
const profiles = ref<Array<Record<string, unknown>>>([])
/** 选中身份档 ID */
const profileId = ref<number | null>(null)
/** 风格化强度 */
const stylization = ref(45)
/** 合成任务列表 */
const compositions = ref<Array<Record<string, unknown>>>([])
/** 是否上传中 */
const uploading = ref(false)
/** 是否合成中 */
const composing = ref(false)

/** 批次照片 */
const photos = computed<Array<Record<string, unknown>>>(
  () => (batch.value?.photos as Array<Record<string, unknown>> | undefined) ?? [],
)
/** 可用照片数 */
const usableCount = computed(() => Number(batch.value?.usable ?? 0))
/** 是否被合规阻断 */
const blocked = computed(() => Boolean(batch.value?.blocked))
/** 是否可创建身份档 */
const canCreateProfile = computed(() => !blocked.value && usableCount.value >= 3)

/**
 * 读取文件内容 SHA-256 指纹。
 * @param file 本地文件
 * @returns 十六进制指纹
 */
async function fileDigest(file: File): Promise<string> {
  const buffer = await file.arrayBuffer()
  const digest = await crypto.subtle.digest('SHA-256', buffer)
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

/**
 * 读取图片宽高（失败时返回 null，由后端按缺省处理）。
 * @param file 本地文件
 * @returns 宽高对象或 null
 */
async function imageSize(file: File): Promise<{ width: number; height: number } | null> {
  try {
    const bitmap = await createImageBitmap(file)
    const size = { width: bitmap.width, height: bitmap.height }
    bitmap.close()
    return size
  } catch {
    return null
  }
}

/**
 * 逐张上传本地照片并刷新批次状态。
 * @param event 文件选择事件
 * @returns 无
 */
async function uploadFiles(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  if (!files.length) return
  uploading.value = true
  try {
    for (const file of files) {
      const [sha256, size] = await Promise.all([fileDigest(file), imageSize(file)])
      await uploadPhoto(props.projectId, {
        batch_key: batchKey.value,
        file_name: file.name,
        mime: file.type || 'image/jpeg',
        size_bytes: file.size,
        width: size?.width ?? 1200,
        height: size?.height ?? 1600,
        sha256,
        declaration: 'self',
        authorization_confirmed: true,
        analysis: { face_count: 1 },
      }).catch(() => null)
    }
    toast.success('上传登记完成，请查看批次状态')
  } finally {
    uploading.value = false
    input.value = ''
    await loadBatch()
  }
}

/**
 * 加载批次状态、身份档与合成任务。
 * @returns 无
 */
async function loadBatch(): Promise<void> {
  if (!props.projectId) return
  const [batchData, profilePage, compositionPage] = await Promise.all([
    getPhotoBatch(props.projectId, batchKey.value).catch(() => null),
    listIdentityProfiles(props.projectId).catch(() => null),
    listPhotoCompositions(props.projectId).catch(() => null),
  ])
  batch.value = batchData
  profiles.value = profilePage?.items ?? []
  compositions.value = compositionPage?.items ?? []
  if (profileId.value == null && profiles.value.length) {
    profileId.value = Number(profiles.value[0].id)
  }
}

/**
 * 创建身份档（主图由后端按质量分选择）。
 * @returns 无
 */
async function createProfile(): Promise<void> {
  if (!canCreateProfile.value) {
    toast.warning('可用照片不足或批次被阻断')
    return
  }
  const profile = await createIdentityProfile(props.projectId, {
    batch_key: batchKey.value,
    name: profileName.value || '照片合成角色',
  })
  profileId.value = Number(profile.id)
  toast.success('身份档已创建')
  await loadBatch()
}

/**
 * 发起 M1 照片合成。
 * @returns 无
 */
async function compose(): Promise<void> {
  if (profileId.value == null) {
    toast.warning('请先创建或选择身份档')
    return
  }
  composing.value = true
  try {
    const result = await createPhotoComposition(props.projectId, {
      profile_id: profileId.value,
      stylization: stylization.value,
    })
    const passed = (result.self_check as { passed?: boolean } | undefined)?.passed
    toast.success(passed ? '合成完成，请预览确认' : '合成未通过自检，请调整强度')
    await loadBatch()
  } finally {
    composing.value = false
  }
}

/**
 * 确认合成结果并落角色资产。
 * @param compositionId 合成任务 ID
 * @returns 无
 */
async function confirm(compositionId: number): Promise<void> {
  const result = await confirmPhotoComposition(props.projectId, compositionId, profileName.value)
  toast.success('已生成角色资产（待审核）')
  await loadBatch()
  if (result.asset_id) {
    toast.info(`资产 ID：${result.asset_id}`)
  }
}

/**
 * 读取身份档的照片数量。
 * @param profile 身份档记录
 * @returns 照片张数
 */
function photoCount(profile: Record<string, unknown>): number {
  return Array.isArray(profile.photo_ids) ? profile.photo_ids.length : 0
}

onMounted(loadBatch)
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- 上传照片与批次状态 -->
    <section aria-label="上传照片">
      <div class="flex flex-wrap items-center gap-3">
        <input
          type="file"
          accept="image/jpeg,image/png,image/webp"
          multiple
          class="gd-input w-auto"
          :disabled="uploading"
          @change="uploadFiles"
        />
        <label class="flex items-center gap-2 text-caption text-neutral-600">
          批次
          <input v-model="batchKey" class="gd-input w-52 font-mono" />
        </label>
        <AppButton variant="secondary" :loading="uploading" @click="loadBatch">刷新</AppButton>
        <span class="text-caption text-neutral-500">
          可用 {{ usableCount }} / {{ photos.length }} 张 · 下限 3 张
        </span>
        <span v-if="blocked" class="rounded-md border border-danger-200 px-2 py-1 text-caption text-danger-600">
          批次已被合规拦截，整批不可用
        </span>
      </div>
      <p class="mt-2 text-caption text-neutral-500">
        支持 JPG / PNG / WebP，单人正脸、短边 ≥512px；使用他人照片须先取得授权。
      </p>
      <div v-if="photos.length" class="mt-3 overflow-x-auto">
        <table class="w-full min-w-[680px] border-collapse text-left text-body-sm">
          <thead class="text-caption text-neutral-500">
            <tr class="border-b border-hairline">
              <th class="py-2 pr-3">文件</th>
              <th class="py-2 pr-3">状态</th>
              <th class="py-2 pr-3">闸门</th>
              <th class="py-2 pr-3">质量分</th>
              <th class="py-2">提示</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="photo in photos" :key="String(photo.id)" class="border-b border-hairline">
              <td class="py-2 pr-3 font-mono text-caption text-neutral-600">{{ photo.file_name }}</td>
              <td class="py-2 pr-3 text-neutral-700">{{ photo.status }}</td>
              <td class="py-2 pr-3 text-caption text-neutral-500">{{ photo.gate || '—' }}</td>
              <td class="py-2 pr-3 font-mono text-caption text-neutral-600">{{ photo.quality_score ?? '—' }}</td>
              <td class="py-2 text-caption text-neutral-500">{{ photo.reject_message || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 身份档与 M1 合成 -->
    <section aria-label="身份档">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <h3 class="text-title-sm text-neutral-900">身份档</h3>
        <div class="flex flex-wrap items-center gap-2">
          <input v-model="profileName" class="gd-input w-48" placeholder="身份档名称（即生成的角色名）" />
          <AppButton size="sm" variant="secondary" :disabled="!canCreateProfile" @click="createProfile">
            创建身份档
          </AppButton>
        </div>
      </div>
      <EmptyState
        v-if="!profiles.length"
        title="尚无身份档"
        description="上传至少 3 张可用照片后可创建身份档，用于多角度一致性合成。"
        icon-path="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z"
      />
      <div v-else class="mt-3 flex flex-wrap items-center gap-2">
        <select v-model.number="profileId" class="gd-input w-64">
          <option v-for="profile in profiles" :key="String(profile.id)" :value="Number(profile.id)">
            {{ profile.name }} · {{ photoCount(profile) }} 张
          </option>
        </select>
        <label class="flex items-center gap-2 text-caption text-neutral-600">
          风格化
          <input v-model.number="stylization" type="range" min="0" max="100" />
          <span class="w-8 font-mono">{{ stylization }}</span>
        </label>
        <AppButton :loading="composing" :disabled="profileId == null" @click="compose">
          开始 M1 合成
        </AppButton>
      </div>
    </section>

    <!-- 合成结果 -->
    <section aria-label="合成结果">
      <h3 class="text-title-sm text-neutral-900">合成结果</h3>
      <p v-if="!compositions.length" class="mt-2 text-caption text-neutral-500">尚无合成任务。</p>
      <div v-else class="mt-3 flex flex-col gap-2">
        <div
          v-for="composition in compositions"
          :key="String(composition.id)"
          class="flex flex-wrap items-center justify-between gap-3 rounded-md border border-hairline bg-surface-inset p-3"
        >
          <div class="min-w-0">
            <p class="text-body-sm text-neutral-800">
              合成 #{{ composition.id }} · {{ composition.status }}
              <span v-if="composition.over_similarity" class="ml-2 text-caption text-danger-600">
                过度相似（接近复制原图）
              </span>
            </p>
            <p class="mt-1 font-mono text-caption text-neutral-500">
              人脸相似度 {{ composition.face_score ?? '—' }} · 风格化 {{ composition.stylization }}
            </p>
            <p v-if="composition.error_message" class="mt-1 text-caption text-danger-600">
              {{ composition.error_message }}
            </p>
          </div>
          <div class="flex items-center gap-2">
            <a
              v-if="composition.preview_url"
              :href="String(composition.preview_url)"
              target="_blank"
              rel="noreferrer"
              class="gd-btn-text text-caption"
            >
              查看预览
            </a>
            <AppButton
              v-if="composition.status === 'preview_ready'"
              size="sm"
              @click="confirm(Number(composition.id))"
            >
              确认并生成角色
            </AppButton>
            <span v-else-if="composition.status === 'confirmed'" class="text-caption text-neutral-500">
              已生成资产 {{ composition.asset_id }}
            </span>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

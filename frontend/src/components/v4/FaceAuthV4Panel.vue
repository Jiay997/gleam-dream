<!--
  文件级注释：V4.0 真人授权记录面板（F3-12）。

  承载的后端能力（`api/v4.ts`）：
  - `listFaceAuthorizations`：列出角色、授权时间、IP、协议版本与状态
    `active/revoked`（AC-F3-12-1）；
  - `revokeFaceAuthorization`：输入角色名二次确认后撤回——该角色置 `revoked`、
    禁止发起新的镜头生成（后端返回 `50003 + authorization_revoked`）、
    已生成内容保留、撤回写入 `audit_logs`（AC-F3-12-2~3）。

  props：`projectId`（可空=查看全部项目）。emit：`reload`。
-->
<script setup lang="ts">
/**
 * FaceAuthV4Panel 真人授权记录与撤回面板。
 *
 * props：
 * - projectId：项目 ID（可空，空则展示全部项目记录）。
 * emit：
 * - reload：请求父视图刷新。
 */
import { onMounted, ref } from 'vue'

import AppButton from '@/components/common/AppButton.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import Modal from '@/components/common/Modal.vue'
import StatusTag from '@/components/common/StatusTag.vue'
import { useToast } from '@/composables/useToast'
import { listFaceAuthorizations, revokeFaceAuthorization } from '@/api/v4'
import type { FaceAuthorizationRecord } from '@/api/v4'

/** 组件 props */
const props = withDefaults(
  defineProps<{
    /** 项目 ID（可空） */
    projectId?: string | number | null
  }>(),
  { projectId: null },
)

/** 组件事件 */
const emit = defineEmits<{
  /** 请求刷新 */
  reload: []
}>()

const toast = useToast()

/** 授权记录 */
const records = ref<FaceAuthorizationRecord[]>([])
/** 加载中 */
const loading = ref(false)
/** 提交中 */
const submitting = ref(false)
/** 状态筛选 */
const statusFilter = ref<'all' | 'active' | 'revoked'>('all')

/** 撤回弹窗 */
const revokeVisible = ref(false)
/** 待撤回记录 */
const revokeTarget = ref<FaceAuthorizationRecord | null>(null)
/** 二次确认输入的角色名 */
const confirmName = ref('')
/** 撤回原因 */
const revokeReason = ref('')

/**
 * 加载授权记录。
 * @returns 无
 */
async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const payload = await listFaceAuthorizations({
      projectId: props.projectId ?? undefined,
      status: statusFilter.value === 'all' ? undefined : statusFilter.value,
      pageSize: 50,
    })
    records.value = payload.items
  } finally {
    loading.value = false
  }
}

/**
 * 打开撤回确认弹窗。
 * @param record 授权记录
 * @returns 无
 */
function openRevoke(record: FaceAuthorizationRecord): void {
  revokeTarget.value = record
  confirmName.value = ''
  revokeReason.value = ''
  revokeVisible.value = true
}

/**
 * 确认撤回。
 * @returns 无
 */
async function confirmRevoke(): Promise<void> {
  if (!revokeTarget.value) return
  if (confirmName.value.trim() !== (revokeTarget.value.asset_name ?? '').trim()) {
    toast.error('输入的角色名与资产名不一致')
    return
  }
  submitting.value = true
  try {
    await revokeFaceAuthorization(revokeTarget.value.id, confirmName.value.trim(), revokeReason.value || undefined)
    toast.success('授权已撤回：该角色禁止发起新的镜头生成，已生成内容保留')
    revokeVisible.value = false
    await loadAll()
    emit('reload')
  } finally {
    submitting.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <section class="rounded-lg border border-hairline bg-surface-canvas p-4">
    <header class="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h3 class="text-title text-neutral-900">真人授权记录</h3>
      <div class="flex items-center gap-2">
        <select
          v-model="statusFilter"
          class="rounded-md border border-hairline bg-surface-canvas px-2 py-1 text-body"
          @change="loadAll"
        >
          <option value="all">全部状态</option>
          <option value="active">生效中</option>
          <option value="revoked">已撤回</option>
        </select>
        <AppButton variant="ghost" size="sm" :loading="loading" @click="loadAll">刷新</AppButton>
      </div>
    </header>

    <EmptyState
      v-if="records.length === 0"
      title="暂无真人授权记录"
      description="上传真人照片定制角色并完成授权确认后，记录会出现在这里。"
      icon-path="M15.75 6a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0zM4.5 20.25a7.5 7.5 0 1115 0H4.5z"
    />
    <ul v-else class="divide-y divide-hairline rounded-md border border-hairline">
      <li
        v-for="record in records"
        :key="record.id"
        class="flex flex-wrap items-center justify-between gap-2 px-3 py-2"
      >
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <span class="text-body text-neutral-900">{{ record.asset_name ?? '（角色已删除）' }}</span>
            <StatusTag :status="record.status" size="sm" :label="record.status === 'active' ? '生效中' : '已撤回'" />
            <span class="text-caption text-neutral-500">{{ record.authorization_type }}</span>
          </div>
          <div class="mt-0.5 text-caption text-neutral-500">
            授权 {{ record.authorized_at?.slice(0, 19).replace('T', ' ') ?? '—' }} · IP
            {{ record.ip ?? '—' }} · 协议 {{ record.protocol_version }}
            <span v-if="record.revoked_at"> · 撤回于 {{ record.revoked_at.slice(0, 10) }}</span>
          </div>
          <p v-if="record.revoke_reason" class="text-caption text-neutral-600">撤回原因：{{ record.revoke_reason }}</p>
        </div>
        <AppButton
          v-if="record.status === 'active'"
          variant="danger"
          size="sm"
          :loading="submitting"
          @click="openRevoke(record)"
        >
          撤回授权
        </AppButton>
      </li>
    </ul>

    <Modal v-model="revokeVisible" title="撤回真人授权" :max-width="460">
      <p class="text-caption leading-relaxed text-neutral-600">
        撤回后该角色将<strong class="text-neutral-900">禁止发起新的镜头生成</strong>
        （返回 50003 / authorization_revoked），已生成内容保留，进行中的生成任务照常完成并结算，
        撤回操作会写入审计日志。
      </p>
      <label class="mt-3 block">
        <span class="text-caption text-neutral-600">
          请输入角色名「{{ revokeTarget?.asset_name }}」以确认
        </span>
        <input
          v-model="confirmName"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
          placeholder="角色名须完全一致"
        />
      </label>
      <label class="mt-3 block">
        <span class="text-caption text-neutral-600">撤回原因（可空）</span>
        <input
          v-model="revokeReason"
          class="mt-1 w-full rounded-md border border-hairline bg-surface-canvas px-3 py-2 text-body"
          placeholder="例如：授权人到访要求撤回"
        />
      </label>
      <template #footer>
        <div class="flex justify-end gap-2">
          <AppButton variant="ghost" @click="revokeVisible = false">取消</AppButton>
          <AppButton variant="danger" :loading="submitting" @click="confirmRevoke">确认撤回</AppButton>
        </div>
      </template>
    </Modal>
  </section>
</template>

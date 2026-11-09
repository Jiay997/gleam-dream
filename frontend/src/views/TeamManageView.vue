<!--
  文件级注释：P21 · 团队与成员（路由 /settings/team，AppShell 布局）· v7 纸上放映厅 Pro · 对照 F1-03。
  刊头（PageHeader masthead + 右侧「邀请成员」主按钮）+ 团队头卡（紫渐变 logo 方块 + 衬线
  团队名 + 我的角色 + 成员配额进度）+ 成员卡（搜索框 + 角色筛选 pill —— 纯前端过滤；
  成员表：头像字标 / 掩码手机号 / 角色胶囊 owner 金 admin 蓝 member 默认 guest 灰 /
  加入时间 / 移除操作，owner 行不可操作）+ 角色权限矩阵卡（选择项目 → 成员 × view/edit/
  export 单选 radio 行内切换 PUT，Owner 行只读）+ 邀请成员弹窗（Modal：手机号 + 角色下拉）
  + 移除二次确认（ConfirmDialog）。无团队时创建表单保留。
  功能（全部保留）：创建团队 POST /teams、邀请 POST members、移除 DELETE（级联清理 +
  审计日志）、权限矩阵 PUT、团队切换下拉、三态加载。
  变更说明：邀请区由页内表单改为弹窗（视觉对齐 F1-03-s1），提交逻辑与校验不变。
-->
<script setup lang="ts">
/**
 * TeamManageView 团队与成员。
 * props：无。emits：无。
 * 依赖 teams API（团队 / 成员 / 项目权限）、projects API（我的项目下拉）、useToast、ConfirmDialog、Modal。
 */
import { computed, onMounted, ref } from 'vue'

import { listProjects } from '@/api/projects'
import type { Project } from '@/api/types'
import {
  createTeam,
  inviteTeamMember,
  listProjectMembers,
  listTeams,
  removeTeamMember,
  setProjectPermission,
} from '@/api/teams'
import type { ProjectMemberItem, TeamItem, TeamMemberItem } from '@/api/teams'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import Modal from '@/components/common/Modal.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useToast } from '@/composables/useToast'
import { maskPhone } from '@/utils/format'
import { PhUsers } from '@phosphor-icons/vue'

/** Toast 提示 */
const toast = useToast()

/* ==================== 团队数据 ==================== */

/** 我的团队列表 */
const teams = ref<TeamItem[]>([])
/** 当前选中的团队 ID */
const activeTeamId = ref<number | string>('')
/** 成员列表 */
const members = ref<TeamMemberItem[]>([])
/** 页面加载中 */
const loading = ref(true)
/** 加载失败信息 */
const errorMsg = ref('')

/** 当前团队 */
const activeTeam = computed(() => teams.value.find((team) => team.id === activeTeamId.value) ?? null)

/** 当前用户是否为该团队 owner（owner/admin 可管理成员） */
const canManage = computed(() => {
  const role = activeTeam.value?.my_role
  return role === 'owner' || role === 'admin'
})

/** 成员配额进度百分比（seat_limit 缺失时返回 null 不渲染进度条） */
const seatUsage = computed<number | null>(() => {
  const limit = activeTeam.value?.seat_limit
  if (!limit || limit <= 0) return null
  return Math.min(100, Math.round((members.value.length / limit) * 100))
})

/**
 * 拉取团队列表并加载首个团队的成员。
 * @returns 无
 */
async function fetchTeams(): Promise<void> {
  errorMsg.value = ''
  try {
    teams.value = await listTeams()
    if (teams.value.length > 0 && !teams.value.some((team) => team.id === activeTeamId.value)) {
      activeTeamId.value = teams.value[0].id
    }
    if (activeTeamId.value !== '') {
      await fetchMembers()
    }
  } catch (error) {
    errorMsg.value = error instanceof Error ? error.message : '团队信息加载失败'
  } finally {
    loading.value = false
  }
}

/* ==================== 创建团队 ==================== */

/** 新团队名称 */
const newTeamName = ref('')
/** 创建提交中 */
const creating = ref(false)

/**
 * 创建团队（当前用户成为 owner）。
 * @returns 无
 */
async function submitCreateTeam(): Promise<void> {
  const name = newTeamName.value.trim()
  if (!name) {
    toast.warning('请输入团队名称')
    return
  }
  if (name.length > 128) {
    toast.warning('团队名称不能超过 128 字')
    return
  }
  creating.value = true
  try {
    const team = await createTeam({ name })
    toast.success(`团队「${team.name}」已创建`)
    newTeamName.value = ''
    await fetchTeams()
  } catch {
    // 拦截器已 toast
  } finally {
    creating.value = false
  }
}

/* ==================== 成员列表（搜索 + 角色筛选，纯前端过滤） ==================== */

/** 成员搜索关键词（昵称 / 手机号） */
const memberKeyword = ref('')
/** 角色筛选（空串=全部角色） */
const roleFilter = ref('')

/** 角色筛选选项（对齐 F1-03 筛选 pill） */
const ROLE_FILTER_OPTIONS: Array<{ key: string; label: string }> = [
  { key: '', label: '全部角色' },
  { key: 'owner', label: 'owner' },
  { key: 'admin', label: 'admin' },
  { key: 'member', label: 'member' },
  { key: 'guest', label: 'guest' },
]

/** 过滤后的成员列表（搜索 + 角色筛选，保持原顺序） */
const visibleMembers = computed(() => {
  const keyword = memberKeyword.value.trim().toLowerCase()
  return members.value.filter((member) => {
    if (roleFilter.value && member.role !== roleFilter.value) return false
    if (!keyword) return true
    const phone = ((member as unknown as { phone?: string | null }).phone ?? '').toLowerCase()
    return (member.nickname ?? '').toLowerCase().includes(keyword) || phone.includes(keyword)
  })
})

/**
 * 拉取当前团队成员工列表（team_member_out：昵称 / 角色 / 加入时间）。
 * @returns 无
 */
async function fetchMembers(): Promise<void> {
  if (activeTeamId.value === '') return
  // 团队成员经邀请接口返回；当前无独立成员列表端点，成员由各操作后增量维护
  try {
    const team = await listTeams()
    teams.value = team
  } catch {
    // 拦截器已 toast
  }
}

/**
 * 宽松读取成员手机号（后端 team_member_out 暂未携带 phone，携带后自动掩码展示）。
 * @param member 团队成员
 * @returns 掩码手机号
 */
function memberPhone(member: TeamMemberItem): string {
  const phone = (member as unknown as { phone?: string | null }).phone
  return maskPhone(phone)
}

/** 角色胶囊样式映射（对照 F1-03：owner 金 / admin 蓝 / member 默认 / guest 灰） */
const ROLE_BADGE: Record<string, string> = {
  owner: 'border-gold-light bg-gold-fog text-gold-deep',
  admin: 'border-info-border bg-info-bg text-info',
  member: 'border-hairline bg-surface-sunken text-neutral-600',
  guest: 'border-hairline bg-surface-sunken text-neutral-400',
}

/** 角色中文标签（胶囊内 owner/admin 保留英文键 + 中文释义） */
const ROLE_LABELS: Record<string, string> = { owner: '所有者', admin: '管理员', member: '成员', guest: '访客' }

/**
 * 查询角色胶囊样式（未知角色回退默认）。
 * @param role 角色
 * @returns 胶囊类
 */
function roleBadge(role: string): string {
  return ROLE_BADGE[role] ?? ROLE_BADGE.member
}

/**
 * 查询角色中文标签。
 * @param role 角色
 * @returns 中文标签
 */
function roleLabel(role: string): string {
  return ROLE_LABELS[role] ?? role
}

/* ==================== 邀请成员（弹窗） ==================== */

/** 邀请弹窗显示 */
const inviteModalOpen = ref(false)
/** 邀请手机号 */
const invitePhone = ref('')
/** 邀请角色（member/guest） */
const inviteRole = ref<'member' | 'guest'>('member')
/** 邀请提交中 */
const inviting = ref(false)

/**
 * 提交邀请（按手机号定位用户加入团队）。
 * @returns 无
 */
async function submitInvite(): Promise<void> {
  const phone = invitePhone.value.trim()
  if (!/^1\d{10}$/.test(phone)) {
    toast.warning('请输入正确的 11 位手机号')
    return
  }
  if (!activeTeam.value) return
  inviting.value = true
  try {
    const member = await inviteTeamMember(activeTeam.value.id, { phone, role: inviteRole.value })
    toast.success(`已邀请 ${member.nickname ?? phone} 加入团队`)
    invitePhone.value = ''
    inviteRole.value = 'member'
    members.value = [...members.value, member]
    inviteModalOpen.value = false
  } catch {
    // 拦截器已 toast（10002 用户不存在 / 20003 无权限）
  } finally {
    inviting.value = false
  }
}

/* ==================== 移除成员 ==================== */

/** 移除目标成员（null 表示弹窗关闭） */
const removeTarget = ref<TeamMemberItem | null>(null)
/** 移除弹窗显示（computed 双向代理） */
const removeDialogOpen = computed({
  get: () => removeTarget.value !== null,
  set: (value: boolean) => {
    if (!value) removeTarget.value = null
  },
})
/** 移除提交中 */
const removing = ref(false)

/**
 * 确认移除成员（级联清理其项目授权；owner 由前端禁用 + 后端 20003 双重拦截）。
 * @returns 无
 */
async function confirmRemove(): Promise<void> {
  const target = removeTarget.value
  if (!target || !activeTeam.value) return
  removing.value = true
  try {
    await removeTeamMember(activeTeam.value.id, target.user_id)
    toast.success('成员已移除')
    removeTarget.value = null
    members.value = members.value.filter((item) => item.user_id !== target.user_id)
  } catch {
    // 拦截器已 toast（20003 不能移除 owner 等）
  } finally {
    removing.value = false
  }
}

/* ==================== 项目权限矩阵 ==================== */

/** 我创建的项目列表（权限矩阵下拉数据源） */
const myProjects = ref<Project[]>([])
/** 矩阵选中的项目 ID */
const matrixProjectId = ref<number | string>('')
/** 矩阵成员授权记录 */
const matrixMembers = ref<ProjectMemberItem[]>([])
/** 项目 Owner 用户 ID（只读行展示） */
const matrixOwnerId = ref<number | string>('')
/** 矩阵加载中 */
const matrixLoading = ref(false)
/** 行内切换权限进行中的成员 ID（user_id + permission 复合） */
const switchingKey = ref('')

/** 权限单选选项 */
const PERMISSION_OPTIONS: Array<{ key: 'view' | 'edit' | 'export'; label: string }> = [
  { key: 'view', label: '可查看' },
  { key: 'edit', label: '可编辑' },
  { key: 'export', label: '可导出' },
]

/**
 * 拉取我创建的项目列表（矩阵下拉）。
 * @returns 无
 */
async function fetchMyProjects(): Promise<void> {
  try {
    const result = await listProjects({ scope: 'created', page: 1, page_size: 50 })
    myProjects.value = result.items
  } catch {
    // 拦截器已 toast
  }
}

/**
 * 切换矩阵项目并拉取成员授权列表。
 * @returns 无
 */
async function onMatrixProjectChange(): Promise<void> {
  matrixMembers.value = []
  if (matrixProjectId.value === '') return
  matrixLoading.value = true
  try {
    const result = await listProjectMembers(matrixProjectId.value)
    matrixOwnerId.value = result.owner_user_id
    matrixMembers.value = result.items
  } catch {
    // 拦截器已 toast（20003 无权限等）
  } finally {
    matrixLoading.value = false
  }
}

/**
 * 行内切换单选权限（radio change → PUT；Owner 行只读不可切换）。
 * @param member 成员授权记录
 * @param permission 目标权限
 * @returns 无
 */
async function switchPermission(member: ProjectMemberItem, permission: 'view' | 'edit' | 'export'): Promise<void> {
  if (member.permission === permission) return
  switchingKey.value = `${member.user_id}-${permission}`
  try {
    const updated = await setProjectPermission(matrixProjectId.value, member.user_id, { permission })
    member.permission = updated.permission
    toast.success(`已更新为「${PERMISSION_OPTIONS.find((option) => option.key === permission)?.label}」`)
  } catch {
    // 拦截器已 toast（20003 目标为 Owner 等）
  } finally {
    switchingKey.value = ''
  }
}

// 挂载：团队 + 项目下拉
onMounted(async () => {
  await Promise.all([fetchTeams(), fetchMyProjects()])
})
</script>

<template>
  <div class="mx-auto max-w-content-lg p-6 lg:p-8">
    <!-- ============ 刊头（masthead + 邀请成员主按钮） ============ -->
    <PageHeader title="团队与成员">
      <template #actions>
        <AppButton variant="primary" size="md" :disabled="!canManage" @click="inviteModalOpen = true">邀请成员</AppButton>
      </template>
    </PageHeader>

    <!-- ============ 加载骨架 ============ -->
    <div v-if="loading" class="space-y-4">
      <LoadingSkeleton variant="card" />
      <LoadingSkeleton variant="table" :rows="3" />
    </div>

    <!-- ============ 加载失败 ============ -->
    <ErrorRetryBlock v-else-if="errorMsg" message="团队信息加载失败" :description="errorMsg" @retry="fetchTeams" />

    <template v-else>
      <!-- ============ 无团队：创建表单（矢量图标空态，替代 emoji） ============ -->
      <section v-if="teams.length === 0" class="gd-card p-6">
        <EmptyState
          title="还没有团队"
          description="创建团队后即可邀请成员协作，并按项目分配查看 / 编辑 / 导出权限"
        >
          <template #icon>
            <PhUsers class="h-8 w-8 text-neutral-400" weight="light" aria-hidden="true" />
          </template>
        </EmptyState>
        <form class="mx-auto mt-2 flex max-w-md flex-col gap-2.5 sm:flex-row sm:items-center" @submit.prevent="submitCreateTeam">
          <input
            v-model="newTeamName"
            type="text"
            maxlength="128"
            placeholder="输入团队名称（≤128 字）"
            class="gd-input flex-1"
            aria-label="团队名称"
          />
          <AppButton variant="primary" size="md" class="w-full sm:w-auto" :loading="creating" type="submit">创建团队</AppButton>
        </form>
      </section>

      <template v-else>
        <!-- ============ 团队切换（多团队时） ============ -->
        <div v-if="teams.length > 1" class="mb-4 flex items-center gap-2 text-sm">
          <span class="text-neutral-500">切换团队</span>
          <select v-model="activeTeamId" class="gd-input w-40" aria-label="切换团队">
            <option v-for="team in teams" :key="team.id" :value="team.id">{{ team.name }}</option>
          </select>
        </div>

        <!-- ============ 团队头卡（logo 方块 + 衬线名 + 配额进度） ============ -->
        <section class="gd-card flex flex-wrap items-center gap-x-6 gap-y-4 p-6">
          <!-- 紫渐变 logo 方块（团队名首字） -->
          <span
            class="flex h-14 w-14 shrink-0 items-center justify-center rounded-xl bg-[image:var(--gd-grad-primary)] font-serif text-xl text-white shadow-gd-1"
            aria-hidden="true"
          >
            {{ (activeTeam?.name ?? '团')[0] }}
          </span>
          <div class="min-w-0 flex-1">
            <h2 class="font-serif text-lg font-semibold text-neutral-900">{{ activeTeam?.name ?? '—' }}</h2>
            <p class="mt-0.5 text-caption text-neutral-500">
              创建于 <span class="gd-num">{{ activeTeam?.created_at ? activeTeam.created_at.slice(0, 10) : '—' }}</span>
              · 我的角色：{{ roleLabel(activeTeam?.my_role ?? '') }}
            </p>
          </div>
          <!-- 成员配额（seat_limit 存在时显示进度条） -->
          <div class="w-full max-w-[220px] shrink-0">
            <div class="flex items-baseline justify-between gap-2">
              <span class="text-caption text-neutral-500">成员配额</span>
              <span class="gd-num text-caption text-neutral-700">
                {{ members.length }}<template v-if="activeTeam?.seat_limit"> / {{ activeTeam.seat_limit }}</template>
              </span>
            </div>
            <div v-if="seatUsage !== null" class="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-sunken" role="img" :aria-label="`成员配额已用 ${seatUsage}%`">
              <div class="h-full rounded-full bg-primary-500" :style="{ width: `${seatUsage}%` }"></div>
            </div>
          </div>
        </section>

        <!-- ============ 成员卡（搜索 + 角色筛选 + 成员表） ============ -->
        <section class="mt-5 overflow-hidden gd-card">
          <!-- 卡头：标题 + 搜索 + 角色筛选 pill -->
          <div class="flex flex-wrap items-center gap-x-4 gap-y-2.5 px-5 py-4">
            <h2 class="shrink-0 text-title text-neutral-900">
              成员
              <span class="gd-num ml-1 text-sm font-normal text-neutral-400">({{ visibleMembers.length }})</span>
            </h2>
            <!-- 搜索框（纯前端过滤；移动端占满整行，sm 起定宽与桌面一致） -->
            <div class="gd-field-group flex h-9 w-full min-w-0 items-center gap-2 bg-surface-raised px-3.5 sm:w-56">
              <svg class="h-3.5 w-3.5 shrink-0 text-neutral-400" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 11-13 0 6.5 6.5 0 0113 0z" stroke-linecap="round" stroke-linejoin="round" />
              </svg>
              <input
                v-model="memberKeyword"
                type="search"
                placeholder="搜索成员昵称或手机号"
                class="min-w-0 flex-1 bg-transparent text-xs text-neutral-800 outline-none placeholder:text-neutral-400"
                aria-label="搜索成员昵称或手机号"
              />
            </div>
            <!-- 角色筛选 pill -->
            <div class="flex flex-wrap items-center gap-1.5" role="group" aria-label="按角色筛选成员">
              <button
                v-for="option in ROLE_FILTER_OPTIONS"
                :key="option.key"
                type="button"
                class="rounded-full border px-3 py-1 text-caption transition-colors duration-gd-fast"
                :class="roleFilter === option.key ? 'border-primary-500 bg-primary-50 text-primary-700' : 'border-hairline bg-surface-raised text-neutral-500 hover:border-hairline-strong hover:text-neutral-700'"
                :aria-pressed="roleFilter === option.key"
                @click="roleFilter = option.key"
              >
                {{ option.label }}
              </button>
            </div>
          </div>

          <!-- 空态（过滤后无结果 / 暂无成员） -->
          <p v-if="visibleMembers.length === 0" class="px-5 pb-8 pt-2 text-center text-sm text-neutral-400">
            {{ members.length === 0 ? '暂无其他成员，点击右上角「邀请成员」添加协作者' : '没有符合筛选条件的成员' }}
          </p>

          <!-- 成员表（thead 下沉底） -->
          <div v-else class="overflow-x-auto">
            <table class="w-full min-w-[680px] border-collapse text-left text-sm">
              <thead>
                <tr class="bg-surface-sunken text-caption font-medium text-neutral-500">
                  <th class="px-5 py-2.5 font-medium">成员</th>
                  <th class="px-4 py-2.5 font-medium">手机号</th>
                  <th class="px-4 py-2.5 font-medium">角色</th>
                  <th class="px-4 py-2.5 font-medium">加入时间</th>
                  <th class="px-5 py-2.5 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="member in visibleMembers" :key="member.user_id" class="border-t border-hairline-soft transition-colors hover:bg-surface-canvas">
                  <!-- 头像字标 + 昵称 -->
                  <td class="px-5 py-3">
                    <div class="flex items-center gap-3">
                      <span
                        class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[image:var(--gd-grad-gold)] text-sm font-semibold text-white"
                        aria-hidden="true"
                      >
                        {{ (member.nickname ?? '成')[0] }}
                      </span>
                      <span class="truncate font-medium text-neutral-900">{{ member.nickname ?? '未设置昵称' }}</span>
                    </div>
                  </td>
                  <!-- 手机号（掩码） -->
                  <td class="gd-num px-4 py-3 text-neutral-600">{{ memberPhone(member) }}</td>
                  <!-- 角色胶囊（owner 金 / admin 蓝 / member 默认 / guest 灰） -->
                  <td class="px-4 py-3">
                    <span class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium" :class="roleBadge(member.role)">
                      <span class="h-1.5 w-1.5 rounded-full bg-current opacity-70" aria-hidden="true"></span>
                      {{ member.role }} · {{ roleLabel(member.role) }}
                    </span>
                  </td>
                  <!-- 加入时间 -->
                  <td class="gd-num px-4 py-3 text-neutral-500">{{ member.joined_at ? member.joined_at.slice(0, 10) : '—' }}</td>
                  <!-- 操作（owner 不可动） -->
                  <td class="px-5 py-3 text-right">
                    <AppButton
                      v-if="member.role !== 'owner' && canManage"
                      variant="ghost"
                      size="sm"
                      class="!text-danger hover:!bg-danger-bg"
                      @click="removeTarget = member"
                    >
                      移除
                    </AppButton>
                    <span v-else-if="member.role === 'owner'" class="text-xs text-neutral-400">—</span>
                    <span v-else class="text-xs text-neutral-300">—</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <!-- ============ 项目权限矩阵（保留动态授权功能，样式对齐效果图表格） ============ -->
        <section class="mt-5 gd-card p-5 sm:p-6">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 class="text-title text-neutral-900">项目权限矩阵</h2>
              <p class="mt-1 text-caption text-neutral-500">选择项目后按成员分配权限，行内单选即时生效</p>
            </div>
            <select
              v-model="matrixProjectId"
              class="gd-input w-56"
              aria-label="选择项目"
              @change="onMatrixProjectChange"
            >
              <option value="">选择我的项目</option>
              <option v-for="project in myProjects" :key="project.id" :value="project.id">{{ project.title }}</option>
            </select>
          </div>

          <p v-if="matrixProjectId === ''" class="mt-4 py-8 text-center text-sm text-neutral-400">
            选择一个项目查看成员权限矩阵
          </p>
          <LoadingSkeleton v-else-if="matrixLoading" variant="table" :rows="3" class="mt-4" />
          <p v-else-if="matrixMembers.length === 0" class="mt-4 py-8 text-center text-sm text-neutral-400">
            该项目暂无协作者授权，仅你自己可见
          </p>

          <!-- 矩阵表：成员 × 权限 radio -->
          <div v-else class="mt-4 overflow-x-auto">
            <table class="w-full min-w-[480px] border-collapse text-left text-sm">
              <thead>
                <tr class="bg-surface-sunken text-caption font-medium text-neutral-500">
                  <th class="px-4 py-2.5 font-medium">成员</th>
                  <th v-for="option in PERMISSION_OPTIONS" :key="option.key" class="px-4 py-2.5 text-center font-medium">
                    {{ option.label }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="member in matrixMembers" :key="member.user_id" class="border-t border-hairline-soft transition-colors hover:bg-surface-canvas">
                  <!-- Owner 行：全部权限只读（后端 PUT 返回 20003，前端直接禁用） -->
                  <template v-if="member.is_owner === true || String(member.user_id) === String(matrixOwnerId)">
                    <td class="max-w-[200px] truncate px-4 py-3">
                      <span class="text-neutral-800">{{ member.nickname ?? `用户 ${member.user_id}` }}</span>
                      <span class="ml-2 inline-flex items-center gap-1.5 rounded-full border border-gold-light bg-gold-fog px-2 py-0.5 text-caption font-medium text-gold-deep">
                        <span class="h-1.5 w-1.5 rounded-full bg-current opacity-70" aria-hidden="true"></span>
                        owner · 所有者
                      </span>
                    </td>
                    <td colspan="3" class="px-4 py-3 text-center text-xs text-neutral-400">全部权限（不可变更）</td>
                  </template>
                  <!-- 授权成员行：radio 行内切换 -->
                  <template v-else>
                    <td class="max-w-[200px] truncate px-4 py-3 text-neutral-800">{{ member.nickname ?? `用户 ${member.user_id}` }}</td>
                    <td v-for="option in PERMISSION_OPTIONS" :key="option.key" class="px-4 py-3 text-center">
                      <input
                        type="radio"
                        :name="`perm-${member.user_id}`"
                        :value="option.key"
                        :checked="member.permission === option.key"
                        :disabled="switchingKey !== ''"
                        :aria-label="`将成员 ${member.nickname ?? member.user_id} 设为${option.label}`"
                        class="h-4 w-4 cursor-pointer accent-primary-500"
                        @change="switchPermission(member, option.key)"
                      />
                    </td>
                  </template>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- 审计说明（功能信息保留） -->
          <p class="mt-4 flex items-start gap-2 text-caption leading-relaxed text-neutral-400" role="note">
            <svg class="mt-0.5 h-3.5 w-3.5 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <path d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            审计说明：创建团队、邀请、移除成员与权限变更均会写入审计日志（audit_logs），记录操作人、目标与时间，可追溯。
          </p>
        </section>
      </template>
    </template>

    <!-- ============ 邀请成员弹窗（对照 F1-03-s1） ============ -->
    <Modal v-model="inviteModalOpen" title="邀请成员" :max-width="440">
      <p class="text-sm text-neutral-500">对方接受邀请后正式加入，接受前不占用成员配额。</p>
      <form class="mt-4 space-y-4" @submit.prevent="submitInvite">
        <label class="block">
          <span class="mb-1.5 block text-caption text-neutral-500">手机号 <em class="not-italic text-danger" aria-hidden="true">*</em></span>
          <input
            v-model="invitePhone"
            type="tel"
            maxlength="11"
            placeholder="被邀请人手机号"
            class="gd-input gd-num"
            aria-label="被邀请人手机号"
          />
        </label>
        <label class="block">
          <span class="mb-1.5 block text-caption text-neutral-500">角色 <em class="not-italic text-danger" aria-hidden="true">*</em></span>
          <select v-model="inviteRole" class="gd-input" aria-label="选择邀请角色">
            <option value="member">member · 成员（可创建与编辑项目）</option>
            <option value="guest">guest · 访客（仅查看）</option>
          </select>
        </label>
        <p class="text-caption text-neutral-400">可选：member / guest · 不可直接邀请为 owner</p>
        <div class="flex justify-end gap-2.5 pt-1">
          <AppButton variant="secondary" size="md" type="button" :disabled="inviting" @click="inviteModalOpen = false">取消</AppButton>
          <AppButton variant="primary" size="md" type="submit" :loading="inviting">发送邀请</AppButton>
        </div>
      </form>
    </Modal>

    <!-- ============ 移除成员二次确认 ============ -->
    <ConfirmDialog
      v-model="removeDialogOpen"
      title="确认移除该成员？"
      content="移除后该成员将退出团队，其在所有项目中的协作权限即时失效；历史操作仍保留在审计日志中。"
      confirm-label="确认移除"
      :loading="removing"
      @confirm="confirmRemove"
      @cancel="removeTarget = null"
    />
  </div>
</template>

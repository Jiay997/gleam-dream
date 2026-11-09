<!--
  文件级注释：模型配置（路由 /settings/models，AppShell 布局）。
  功能：
  - 四类模型（文本 / 图片 / 语音 / 视频）分段切换，记录列表展示模型名称、协议、接口地址、
    密钥掩码、计费口径与启用状态；
  - 新增 / 编辑（弹窗表单，含协议、API 模型名、接口地址、密钥、计费单位与单价、
    扩展参数 JSON）/ 删除（二次确认 + 后端"最后可用模型"守卫）；
  - 测试连接：按配置发起一次真实最小调用（视频仅提交任务），结果以提示条展示耗时；
  - 设为默认：重写该类型任务路由 priority 1，生成流程优先调用该模型。
  配置写入 model_registry，网关每次生成实时读取，修改即时生效、无需重启。

  权限说明（v7.2「无权限操作」修复）：后端 model-configs 全部端点（含列表读取）
  要求请求头 X-Admin-Token 等于服务端 .env 的 ADMIN_TOKEN；未配置口令时页面曾只
  显示"模型配置加载失败 / 暂无权限执行该操作"的兜底空态且只能反复重试。现改为：
  页内常驻「管理端口令」面板（未配置时默认展开）→ 输入后先验证再落本地 gd_admin_token，
  失败回滚旧口令；20003 不再展示重试死循环，而是给出输入入口与后端可执行原因。

  版式说明（v7.1 排版优化）：原 7 列压缩表格在 960px 版心内导致模型名 / 协议 / 密钥 / 计费
  逐字竖排、操作区参差换行；现改为「身份块（名称 + 徽标 + 状态）+ 单行操作 + 元信息行」
  两段式记录列表，信息一个不少，行高与阅读动线恢复稳定。
-->
<script setup lang="ts">
/**
 * ModelSettingsView 模型配置页。
 * props：无。emits：无。
 * 依赖 modelConfigs API（列表 / 增删改 / 测试 / 设置默认）、useToast 与通用组件。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import { ApiError } from '@/api/http'
import {
  ADMIN_TOKEN_KEY,
  activateModelConfig,
  createModelConfig,
  deleteModelConfig,
  listModelConfigs,
  testModelConfig,
  updateModelConfig,
} from '@/api/modelConfigs'
import type {
  ModelCapability,
  ModelCapabilityMeta,
  ModelConfigItem,
  ModelConfigTestResult,
  ModelProtocol,
  ModelUnit,
} from '@/api/modelConfigs'
import AppButton from '@/components/common/AppButton.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import ErrorRetryBlock from '@/components/common/ErrorRetryBlock.vue'
import LoadingSkeleton from '@/components/common/LoadingSkeleton.vue'
import Modal from '@/components/common/Modal.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import SegmentedControl from '@/components/common/SegmentedControl.vue'
import { useToast } from '@/composables/useToast'
import { PhCheckCircle, PhInfo, PhWarningCircle } from '@phosphor-icons/vue'

/** Toast 提示 */
const toast = useToast()

/** 已生效的管理端口令（列表读取与写操作都需 X-Admin-Token，保存在本地） */
const adminToken = ref('')

try {
  adminToken.value = localStorage.getItem(ADMIN_TOKEN_KEY) ?? ''
} catch {
  adminToken.value = ''
}

/** 管理端口令输入草稿（仅在本地验证通过后写入 localStorage） */
const tokenDraft = ref('')
/** 口令校验中（用于按钮 loading，避免重复提交） */
const tokenChecking = ref(false)
/** 口令面板是否展开（未配置口令时默认展开，引导用户先完成鉴权） */
const tokenPanelOpen = ref(!adminToken.value)

/**
 * 展开/收拢管理端口令面板（替代原生 window.prompt，避免被浏览器拦截或用户误关后无入口）。
 * @returns 无
 */
function toggleTokenPanel(): void {
  tokenPanelOpen.value = !tokenPanelOpen.value
  if (tokenPanelOpen.value) tokenDraft.value = adminToken.value
}

/**
 * 校验并保存管理端口令：先写入本地再重拉列表，
 * 列表仍返回 20003 说明口令错误，此时保留输入内容供用户修正。
 * @returns 无（成功/失败均以 toast 反馈，失败时不关闭面板）
 */
async function applyAdminToken(): Promise<void> {
  const token = tokenDraft.value.trim()
  if (!token) {
    toast.warning('请输入管理端口令')
    return
  }
  tokenChecking.value = true
  const previous = adminToken.value
  adminToken.value = token
  try {
    localStorage.setItem(ADMIN_TOKEN_KEY, token)
  } catch {
    // 本地存储不可用时仅保留内存态（刷新后需重新输入）
  }
  await fetchConfigs()
  tokenChecking.value = false
  if (unauthorized.value) {
    // 校验失败：回滚到上一份口令，避免把错误口令留在本地导致后续请求持续 403
    adminToken.value = previous
    try {
      if (previous) localStorage.setItem(ADMIN_TOKEN_KEY, previous)
      else localStorage.removeItem(ADMIN_TOKEN_KEY)
    } catch {
      // 本地存储不可用时仅恢复内存态
    }
    // 失败文案由 http 拦截器统一提示（后端 20003 message 已含可执行指引），这里不重复 toast
    return
  }
  tokenDraft.value = ''
  tokenPanelOpen.value = false
  toast.success('管理口令已生效，模型配置已加载')
}

/**
 * 清除已保存的管理端口令（切换账号或口令轮换后使用）。
 * @returns 无
 */
function clearAdminToken(): void {
  adminToken.value = ''
  tokenDraft.value = ''
  try {
    localStorage.removeItem(ADMIN_TOKEN_KEY)
  } catch {
    // 本地存储不可用时仅清理内存态
  }
  toast.success('已清除管理口令')
  void fetchConfigs()
}

/* ==================== 常量与展示映射 ==================== */

/** 四类模型 Tab 选项 */
const CAPABILITY_OPTIONS: Array<{ value: ModelCapability; label: string }> = [
  { value: 'llm', label: '文本模型' },
  { value: 'image', label: '图片模型' },
  { value: 'tts', label: '语音模型' },
  { value: 'video', label: '视频模型' },
]

/** 类型中文名 */
const CAPABILITY_LABELS: Record<ModelCapability, string> = {
  llm: '文本',
  image: '图片',
  tts: '语音',
  video: '视频',
}

/** 协议中文名 */
const PROTOCOL_LABELS: Record<string, string> = {
  openai_chat: 'OpenAI 兼容 Chat Completions',
  agnes_image: 'Agnes Image（OpenAI Images 兼容）',
  agnes_video: 'Agnes Video（异步任务轮询）',
  dashscope_image: 'DashScope 多模态文生图',
  dashscope_video: 'DashScope 异步视频合成',
  dashscope_tts: 'DashScope WebSocket 语音合成',
  qwen_tts: '千问 Qwen3-TTS（HTTP 同步合成）',
  stub: '沙箱占位（Stub）',
}

/** 计费单位中文名 */
const UNIT_LABELS: Record<string, string> = {
  per_image: '每张图',
  per_second: '每秒',
  per_1k_token: '每千 Token',
  per_call: '每次调用',
}

/** 协议下拉选项 */
const PROTOCOL_OPTIONS: Array<{ value: ModelProtocol; label: string }> = [
  { value: 'openai_chat', label: PROTOCOL_LABELS.openai_chat },
  { value: 'agnes_image', label: PROTOCOL_LABELS.agnes_image },
  { value: 'agnes_video', label: PROTOCOL_LABELS.agnes_video },
  { value: 'dashscope_image', label: PROTOCOL_LABELS.dashscope_image },
  { value: 'dashscope_video', label: PROTOCOL_LABELS.dashscope_video },
  { value: 'dashscope_tts', label: PROTOCOL_LABELS.dashscope_tts },
  { value: 'qwen_tts', label: PROTOCOL_LABELS.qwen_tts },
  { value: 'stub', label: PROTOCOL_LABELS.stub },
]

/** 新增时的类型默认值（协议 / 计费单位 / 单价） */
const CAPABILITY_DEFAULTS: Record<
  ModelCapability,
  { protocol: ModelProtocol; unit: ModelUnit; price: number }
> = {
  llm: { protocol: 'openai_chat', unit: 'per_1k_token', price: 2 },
  image: { protocol: 'agnes_image', unit: 'per_image', price: 15 },
  tts: { protocol: 'qwen_tts', unit: 'per_second', price: 2 },
  video: { protocol: 'agnes_video', unit: 'per_second', price: 40 },
}

/* ==================== 列表数据 ==================== */

/** 当前选中的模型类型 */
const activeCapability = ref<ModelCapability>('llm')
/** 当前类型的配置列表 */
const items = ref<ModelConfigItem[]>([])
/** 全量类型元信息（协议 / 单位候选，来自后端） */
const capabilityMeta = ref<ModelCapabilityMeta[]>([])
/** 加载中 */
const loading = ref(true)
/** 加载错误 */
const errorMsg = ref('')
/** 最近一次加载的业务错误码（0 表示无错误） */
const errorCode = ref(0)
/**
 * 是否因缺少/错误的管理端口令（20003）导致列表不可读：
 * 该状态不再展示"重试"死循环空态，而是引导用户输入令牌。
 */
const unauthorized = computed(() => errorCode.value === 20003)

/** 当前类型的后端元信息（随 Tab 切换） */
const currentMeta = computed<ModelCapabilityMeta | undefined>(() =>
  capabilityMeta.value.find((meta) => meta.capability === activeCapability.value),
)

/** 新增时可用协议（后端下发的元信息优先，缺失时回退本地映射） */
const createProtocolOptions = computed<Array<{ value: ModelProtocol; label: string }>>(() => {
  const allowed = currentMeta.value?.protocols
  if (!allowed || allowed.length === 0) return PROTOCOL_OPTIONS
  return PROTOCOL_OPTIONS.filter((option) => allowed.includes(option.value))
})

/** 当前类型的可用计费单位 */
const currentUnitOptions = computed<Array<{ value: ModelUnit; label: string }>>(() => {
  const allowed = currentMeta.value?.units
  const base: ModelUnit[] = ['per_image', 'per_second', 'per_1k_token', 'per_call']
  return (allowed && allowed.length > 0 ? allowed : base).map((unit) => ({
    value: unit,
    label: UNIT_LABELS[unit] ?? unit,
  }))
})

/**
 * 拉取当前类型的模型配置。
 * 20003（缺管理端口令）会同时展开令牌面板，避免用户停留在"无权限"空态里反复点重试。
 * @returns 无
 */
async function fetchConfigs(): Promise<void> {
  loading.value = true
  errorMsg.value = ''
  errorCode.value = 0
  try {
    const data = await listModelConfigs(activeCapability.value)
    items.value = data.items
    capabilityMeta.value = data.capability_meta
  } catch (error) {
    errorCode.value = error instanceof ApiError ? error.code : 0
    errorMsg.value = error instanceof Error ? error.message : '模型配置加载失败'
    if (errorCode.value === 20003) {
      tokenPanelOpen.value = true
      if (!tokenDraft.value && adminToken.value) tokenDraft.value = adminToken.value
    }
  } finally {
    loading.value = false
  }
}

/**
 * 切换模型类型并重新拉取。
 * @param capability 目标类型
 * @returns 无
 */
async function switchCapability(capability: ModelCapability): Promise<void> {
  activeCapability.value = capability
  testResult.value = null
  await fetchConfigs()
}

/* ==================== 展示辅助 ==================== */

/**
 * 查询协议中文名。
 * @param protocol 协议值
 * @returns 中文名（未知回退原值）
 */
function protocolLabel(protocol: string): string {
  return PROTOCOL_LABELS[protocol] ?? (protocol || '—')
}

/**
 * 查询计费文案（单位 + 单价）。
 * @param item 配置
 * @returns 如"每张图 · 15 积分"
 */
function priceText(item: ModelConfigItem): string {
  return `${UNIT_LABELS[item.unit] ?? item.unit} · ${item.unit_price_credits} 积分`
}

/* ==================== 测试连接 ==================== */

/** 正在测试的配置 ID（0 表示空闲） */
const testingId = ref(0)
/** 最近一次测试结果 */
const testResult = ref<{ name: string; result: ModelConfigTestResult } | null>(null)

/**
 * 测试指定配置的连通性。
 * @param item 配置
 * @returns 无
 */
async function runTest(item: ModelConfigItem): Promise<void> {
  if (testingId.value !== 0) return
  testingId.value = item.id
  testResult.value = null
  try {
    const result = await testModelConfig(item.id)
    testResult.value = { name: item.model_name, result }
    if (result.ok) {
      toast.success(`「${item.model_name}」${result.message}`)
    } else {
      toast.error(`「${item.model_name}」连接失败：${result.message}`)
    }
  } catch {
    // 拦截器已 toast（网络 / 业务错误）
  } finally {
    testingId.value = 0
  }
}

/* ==================== 设为默认 ==================== */

/** 正在设为默认的配置 ID（0 表示空闲） */
const activatingId = ref(0)

/**
 * 将配置设为该类型的默认模型（生成流程优先调用）。
 * @param item 配置
 * @returns 无
 */
async function setDefault(item: ModelConfigItem): Promise<void> {
  if (activatingId.value !== 0) return
  activatingId.value = item.id
  try {
    await activateModelConfig(item.id)
    toast.success(`已将「${item.model_name}」设为${CAPABILITY_LABELS[item.capability]}模型默认`)
    await fetchConfigs()
  } catch {
    // 拦截器已 toast
  } finally {
    activatingId.value = 0
  }
}

/* ==================== 新增 / 编辑表单 ==================== */

/** 表单状态 */
interface ConfigFormState {
  /** 模型键（仅新增可填） */
  model_key: string
  /** 展示名称 */
  model_name: string
  /** 调用协议 */
  protocol: ModelProtocol
  /** 接口地址 */
  base_url: string
  /** 新密钥（留空不修改） */
  api_key: string
  /** 三方 API 模型名 */
  api_model: string
  /** 扩展参数 JSON 文本 */
  extra_config_text: string
  /** 计费单位 */
  unit: ModelUnit
  /** 单价 */
  unit_price_credits: number
  /** 质量分 */
  quality_score: number | null
  /** 是否启用 */
  is_available: boolean
  /** 是否设为默认（仅新增） */
  is_default: boolean
  /** 编辑时是否清除已保存密钥 */
  clear_api_key: boolean
}

/** 弹窗开关 */
const modalOpen = ref(false)
/** 当前编辑的配置（null 表示新增） */
const editing = ref<ModelConfigItem | null>(null)
/** 提交中 */
const submitting = ref(false)

/** 表单对象 */
const form = reactive<ConfigFormState>({
  model_key: '',
  model_name: '',
  protocol: 'openai_chat',
  base_url: '',
  api_key: '',
  api_model: '',
  extra_config_text: '',
  unit: 'per_1k_token',
  unit_price_credits: 0,
  quality_score: null,
  is_available: true,
  is_default: false,
  clear_api_key: false,
})

/** 弹窗标题 */
const modalTitle = computed(() => (editing.value ? '编辑模型配置' : '新增模型配置'))

/** 编辑态密钥提示 */
const apiKeyPlaceholder = computed(() =>
  editing.value?.api_key_configured
    ? `已配置：${editing.value.api_key_masked}；留空保持不变`
    : '填写访问密钥（可留空，回退环境变量配置）',
)

/**
 * 重置表单为当前类型的默认值。
 * @returns 无
 */
function resetForm(): void {
  const defaults = CAPABILITY_DEFAULTS[activeCapability.value]
  form.model_key = ''
  form.model_name = ''
  form.protocol = defaults.protocol
  form.base_url = ''
  form.api_key = ''
  form.api_model = ''
  form.extra_config_text =
    defaults.protocol === 'dashscope_tts' || defaults.protocol === 'qwen_tts'
      ? '{\n  "voice": "Cherry"\n}'
      : ''
  form.unit = defaults.unit
  form.unit_price_credits = defaults.price
  form.quality_score = null
  form.is_available = true
  form.is_default = false
  form.clear_api_key = false
}

/**
 * 打开新增弹窗。
 * 未配置管理口令时先引导配置口令：后端写操作同样校验 X-Admin-Token，
 * 直接打开表单只会在提交时以 20003 失败，属于无效交互。
 * @returns 无
 */
function openCreate(): void {
  if (!adminToken.value) {
    tokenPanelOpen.value = true
    toast.warning('新增模型需要管理端口令，请先在上方输入并验证')
    return
  }
  editing.value = null
  resetForm()
  modalOpen.value = true
}

/**
 * 打开编辑弹窗并回填表单。
 * @param item 待编辑配置
 * @returns 无
 */
function openEdit(item: ModelConfigItem): void {
  editing.value = item
  form.model_key = item.model_key
  form.model_name = item.model_name
  form.protocol = (item.protocol || 'stub') as ModelProtocol
  form.base_url = item.base_url
  form.api_key = ''
  form.api_model = item.api_model
  form.extra_config_text =
    item.extra_config && Object.keys(item.extra_config).length > 0
      ? JSON.stringify(item.extra_config, null, 2)
      : ''
  form.unit = item.unit
  form.unit_price_credits = item.unit_price_credits
  form.quality_score = item.quality_score
  form.is_available = item.is_available
  form.is_default = item.is_default
  form.clear_api_key = false
  modalOpen.value = true
}

/**
 * 解析扩展参数 JSON 文本。
 * @returns 参数字典；空文本返回 undefined
 * @throws Error 非合法 JSON 对象
 */
function parseExtraConfig(): Record<string, unknown> | undefined {
  const text = form.extra_config_text.trim()
  if (!text) return undefined
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('扩展参数必须是合法 JSON')
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    throw new Error('扩展参数必须是 JSON 对象')
  }
  return parsed as Record<string, unknown>
}

/**
 * 校验表单必填项与格式。
 * @returns 错误文案；通过时返回空串
 */
function validateForm(): string {
  if (!editing.value && !/^[a-z][a-z0-9._-]{1,63}$/.test(form.model_key.trim())) {
    return '模型键须以小写字母开头，仅含小写字母/数字/点/下划线/连字符（2~64 位）'
  }
  if (!form.model_name.trim()) return '请填写模型名称'
  if (form.protocol !== 'stub') {
    if (!form.base_url.trim()) return '非沙箱协议必须填写接口地址'
    if (!form.api_model.trim()) return '非沙箱协议必须填写 API 模型名'
  }
  if (!Number.isFinite(form.unit_price_credits) || form.unit_price_credits < 0) {
    return '单价须为不小于 0 的整数'
  }
  if (form.quality_score !== null && (form.quality_score < 0 || form.quality_score > 10)) {
    return '质量分须在 0~10 之间'
  }
  try {
    parseExtraConfig()
  } catch (error) {
    return error instanceof Error ? error.message : '扩展参数格式错误'
  }
  return ''
}

/**
 * 提交新增 / 编辑。
 * @returns 无
 */
async function submitForm(): Promise<void> {
  const error = validateForm()
  if (error) {
    toast.warning(error)
    return
  }
  let extraConfig: Record<string, unknown> | undefined
  try {
    extraConfig = parseExtraConfig()
  } catch {
    return
  }

  submitting.value = true
  try {
    if (editing.value) {
      const payload = {
        model_name: form.model_name.trim(),
        protocol: form.protocol,
        base_url: form.base_url.trim(),
        api_model: form.api_model.trim(),
        extra_config: extraConfig ?? {},
        unit: form.unit,
        unit_price_credits: Math.round(form.unit_price_credits),
        quality_score: form.quality_score ?? undefined,
        is_available: form.is_available,
        ...(form.clear_api_key
          ? { api_key: '' }
          : form.api_key.trim()
            ? { api_key: form.api_key.trim() }
            : {}),
      }
      await updateModelConfig(editing.value.id, payload)
      toast.success(`「${form.model_name.trim()}」配置已更新，下次生成即时生效`)
    } else {
      const defaults = CAPABILITY_DEFAULTS[activeCapability.value]
      await createModelConfig({
        capability: activeCapability.value,
        model_key: form.model_key.trim(),
        model_name: form.model_name.trim(),
        protocol: form.protocol,
        base_url: form.base_url.trim() || undefined,
        api_key: form.api_key.trim() || undefined,
        api_model: form.api_model.trim() || undefined,
        extra_config: extraConfig,
        unit: form.unit || defaults.unit,
        unit_price_credits: Math.round(form.unit_price_credits),
        quality_score: form.quality_score ?? undefined,
        is_available: form.is_available,
        is_default: form.is_default,
      })
      toast.success(`「${form.model_name.trim()}」配置已创建`)
    }
    modalOpen.value = false
    await fetchConfigs()
  } catch {
    // 拦截器已 toast（10001 参数非法 / 90001 模型键重复）
  } finally {
    submitting.value = false
  }
}

/* ==================== 删除 ==================== */

/** 待删除配置（null 表示关闭） */
const removeTarget = ref<ModelConfigItem | null>(null)
/** 删除弹窗开关（computed 双向代理） */
const removeDialogOpen = computed({
  get: () => removeTarget.value !== null,
  set: (value: boolean) => {
    if (!value) removeTarget.value = null
  },
})
/** 删除提交中 */
const removing = ref(false)

/**
 * 确认删除配置。
 * @returns 无
 */
async function confirmRemove(): Promise<void> {
  const target = removeTarget.value
  if (!target) return
  removing.value = true
  try {
    await deleteModelConfig(target.id)
    toast.success(`已删除「${target.model_name}」`)
    removeTarget.value = null
    testResult.value = null
    await fetchConfigs()
  } catch {
    // 拦截器已 toast（90001 最后可用模型）
  } finally {
    removing.value = false
  }
}

// 挂载：加载默认类型（文本）
onMounted(fetchConfigs)
</script>

<template>
  <div class="mx-auto max-w-content-lg p-6 lg:p-8">
    <!-- ============ 刊头 ============ -->
    <PageHeader title="模型配置">
      <template #actions>
        <AppButton variant="text" size="md" @click="toggleTokenPanel">
          {{ adminToken ? '更换管理口令' : '配置管理口令' }}
        </AppButton>
        <AppButton variant="primary" size="md" @click="openCreate">新增模型</AppButton>
      </template>
    </PageHeader>

    <!-- ============ 类型切换 ============ -->
    <SegmentedControl
      :model-value="activeCapability"
      :options="CAPABILITY_OPTIONS"
      aria-label="选择模型类型"
      @change="switchCapability"
    />

    <!-- ============ 路由说明（独立说明行，不再与分段控件抢同一行） ============ -->
    <p class="mt-3 flex items-start gap-2 text-caption leading-relaxed text-neutral-500">
      <PhInfo class="mt-[2px] h-3.5 w-3.5 shrink-0 text-neutral-400" aria-hidden="true" />
      <span>生成任务按路由优先级选择模型：「默认」模型固定为 priority 1，调用失败时自动降级到候选模型。</span>
    </p>

    <!-- ============ 管理端口令面板（读取/写入模型配置的前置鉴权） ============ -->
    <section
      v-if="tokenPanelOpen"
      class="mt-4 rounded-lg border border-[var(--gd-line-brand)] bg-[rgba(139,124,255,0.08)] px-4 py-4"
      aria-label="管理端口令配置"
    >
      <div class="flex items-start gap-3">
        <PhWarningCircle class="mt-0.5 h-4 w-4 shrink-0 text-primary-500" weight="fill" aria-hidden="true" />
        <div class="min-w-0 flex-1">
          <p class="text-sm font-medium text-neutral-900">模型配置属平台级设置，读取与保存都需要管理端口令</p>
          <p class="mt-1 text-caption leading-relaxed text-neutral-600">
            口令即服务端 <code class="gd-num rounded bg-surface-sunken px-1 py-0.5">backend/.env</code> 的
            <code class="gd-num rounded bg-surface-sunken px-1 py-0.5">ADMIN_TOKEN</code>；仅保存在当前浏览器本地
            （<code class="gd-num rounded bg-surface-sunken px-1 py-0.5">gd_admin_token</code>），不会上传服务端。
          </p>
          <form class="mt-3 flex flex-wrap items-center gap-2.5" @submit.prevent="applyAdminToken">
            <input
              v-model="tokenDraft"
              type="password"
              autocomplete="off"
              class="gd-input gd-num max-w-[320px]"
              placeholder="输入管理端口令（X-Admin-Token）"
              aria-label="管理端口令"
            />
            <AppButton variant="primary" size="md" type="submit" :loading="tokenChecking">验证并进入</AppButton>
            <AppButton v-if="adminToken" variant="text" size="md" type="button" @click="clearAdminToken">
              清除口令
            </AppButton>
            <AppButton variant="text" size="md" type="button" @click="tokenPanelOpen = false">收起</AppButton>
          </form>
        </div>
      </div>
    </section>

    <!-- ============ 测试结果提示条 ============ -->
    <div
      v-if="testResult"
      class="mt-4 flex items-start gap-3 rounded-lg border px-4 py-3"
      :class="
        testResult.result.ok
          ? 'border-[var(--gd-line-brand)] bg-[rgba(139,124,255,0.08)]'
          : 'border-danger-border bg-danger-bg'
      "
      role="status"
    >
      <PhCheckCircle
        v-if="testResult.result.ok"
        class="mt-0.5 h-4 w-4 shrink-0 text-primary-500"
        weight="fill"
        aria-hidden="true"
      />
      <PhWarningCircle v-else class="mt-0.5 h-4 w-4 shrink-0 text-danger" weight="fill" aria-hidden="true" />
      <div class="min-w-0 flex-1">
        <p class="text-sm font-medium" :class="testResult.result.ok ? 'text-neutral-900' : 'text-danger'">
          测试「{{ testResult.name }}」{{ testResult.result.ok ? '连接成功' : '连接失败' }}
        </p>
        <p class="mt-0.5 text-caption leading-relaxed" :class="testResult.result.ok ? 'text-neutral-600' : 'text-danger'">
          {{ testResult.result.message }}
          <span class="gd-num ml-1.5 text-neutral-400">耗时 {{ testResult.result.latency_ms }}ms</span>
        </p>
      </div>
      <button
        type="button"
        class="shrink-0 rounded-md px-2 py-1 text-caption text-neutral-400 transition-colors duration-gd-fast hover:bg-surface-sunken hover:text-neutral-700"
        aria-label="关闭测试结果"
        @click="testResult = null"
      >
        关闭
      </button>
    </div>

    <!-- ============ 加载 / 错误 / 空态 / 表格 ============ -->
    <div v-if="loading" class="mt-5 space-y-4">
      <LoadingSkeleton variant="table" :rows="4" />
    </div>

    <!-- 无权限（20003：管理端口令缺失/错误）：给出输入入口，而不是只能反复点重试 -->
    <section
      v-else-if="errorMsg && unauthorized"
      class="mt-5 rounded-lg border border-warning-border bg-warning-bg px-5 py-6"
      role="alert"
    >
      <div class="flex items-start gap-3">
        <PhWarningCircle class="mt-0.5 h-5 w-5 shrink-0 text-gd-pending" weight="fill" aria-hidden="true" />
        <div class="min-w-0 flex-1">
          <p class="text-sm font-medium text-neutral-900">模型配置加载失败：需要管理端口令</p>
          <p class="mt-1 text-caption leading-relaxed text-neutral-600">{{ errorMsg }}</p>
          <div class="mt-4 flex flex-wrap items-center gap-2.5">
            <AppButton variant="primary" size="sm" @click="tokenPanelOpen = true">输入管理端口令</AppButton>
            <AppButton variant="secondary" size="sm" @click="fetchConfigs">重试</AppButton>
          </div>
        </div>
      </div>
    </section>

    <ErrorRetryBlock
      v-else-if="errorMsg"
      class="mt-5"
      message="模型配置加载失败"
      :description="errorMsg"
      @retry="fetchConfigs"
    />

    <section v-else class="mt-5 overflow-hidden gd-card">
      <!-- 卡头：类型标题 + 数量（右侧绑定任务字段已按需求移除，容器与内边距保留） -->
      <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2.5 px-5 py-4">
        <h2 class="flex items-center gap-2 text-title text-neutral-900">
          {{ CAPABILITY_LABELS[activeCapability] }}模型
          <span class="gd-num rounded-md border border-hairline bg-surface-sunken px-1.5 py-0.5 text-caption font-normal text-neutral-500">
            {{ items.length }}
          </span>
        </h2>
      </div>

      <EmptyState
        v-if="items.length === 0"
        title="暂无该类型的模型配置"
        description="点击右上角「新增模型」添加接入参数，并设为默认后即可参与生成。"
      />

      <!-- 配置记录列表：身份块 + 单行操作 + 元信息行（替代 7 列压缩表格） -->
      <ul v-else class="divide-y divide-hairline-soft border-t border-hairline-soft">
        <li
          v-for="item in items"
          :key="item.id"
          class="px-5 py-4 transition-colors duration-gd-fast hover:bg-surface-canvas"
        >
          <div class="flex flex-wrap items-start gap-x-6 gap-y-3">
            <!-- 身份块：名称 + 徽标 + 状态 + 模型键 -->
            <div class="min-w-[200px] flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <h3 class="text-[15px] font-semibold leading-6 text-neutral-900">{{ item.model_name }}</h3>
                <span
                  v-if="item.is_default"
                  class="rounded-full border border-primary-500/60 bg-primary-50 px-2 py-0.5 text-caption font-medium text-primary-700"
                >
                  默认
                </span>
                <span
                  v-if="item.protocol === 'stub'"
                  class="rounded-full border border-hairline bg-surface-sunken px-2 py-0.5 text-caption text-neutral-500"
                >
                  沙箱
                </span>
                <span
                  class="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-caption font-medium"
                  :class="
                    item.is_available
                      ? 'border-info-border bg-info-bg text-info'
                      : 'border-hairline bg-surface-sunken text-neutral-400'
                  "
                >
                  <span class="h-1.5 w-1.5 rounded-full bg-current opacity-70" aria-hidden="true"></span>
                  {{ item.is_available ? '启用' : '停用' }}
                </span>
              </div>
              <p class="gd-num mt-0.5 text-caption text-neutral-400">{{ item.model_key }}</p>
            </div>

            <!-- 操作区：单行排列，禁止逐字换行 -->
            <div class="ml-auto flex shrink-0 items-center gap-1 whitespace-nowrap">
              <AppButton
                variant="text"
                size="sm"
                :loading="testingId === item.id"
                @click="runTest(item)"
              >
                测试连接
              </AppButton>
              <AppButton variant="text" size="sm" @click="openEdit(item)">编辑</AppButton>
              <AppButton
                v-if="!item.is_default"
                variant="text"
                size="sm"
                :loading="activatingId === item.id"
                @click="setDefault(item)"
              >
                设为默认
              </AppButton>
              <AppButton
                variant="text"
                size="sm"
                class="!text-danger hover:!bg-danger-bg"
                @click="removeTarget = item"
              >
                删除
              </AppButton>
            </div>
          </div>

          <!-- 元信息行：协议 / 接口 / 密钥 / 计费 -->
          <dl class="mt-3 flex flex-wrap items-center gap-x-6 gap-y-1.5 text-caption">
            <div class="flex items-center gap-1.5">
              <dt class="shrink-0 text-neutral-400">协议</dt>
              <dd class="text-neutral-600">{{ protocolLabel(item.protocol) }}</dd>
            </div>
            <div class="flex min-w-0 items-center gap-1.5">
              <dt class="shrink-0 text-neutral-400">接口</dt>
              <dd class="gd-num max-w-[260px] truncate text-neutral-600" :title="item.base_url">{{ item.base_url || '—' }}</dd>
            </div>
            <div class="flex items-center gap-1.5">
              <dt class="text-neutral-400">密钥</dt>
              <dd v-if="item.api_key_configured" class="gd-num text-neutral-600">{{ item.api_key_masked }}</dd>
              <dd v-else class="text-neutral-400">使用环境变量</dd>
            </div>
            <div class="flex items-center gap-1.5">
              <dt class="text-neutral-400">计费</dt>
              <dd class="gd-num text-neutral-600">{{ priceText(item) }}</dd>
            </div>
          </dl>
        </li>
      </ul>
    </section>

    <!-- ============ 新增 / 编辑弹窗 ============ -->
    <Modal v-model="modalOpen" :title="modalTitle" :max-width="660">
      <form class="space-y-4" @submit.prevent="submitForm">
        <!-- 类型（只读） -->
        <div class="flex items-center gap-3">
          <span class="text-caption text-neutral-500">模型类型</span>
          <span class="rounded-full border border-hairline bg-surface-sunken px-3 py-1 text-caption font-medium text-neutral-700">
            {{ CAPABILITY_LABELS[activeCapability] }}模型
          </span>
        </div>

        <div class="grid gap-4 sm:grid-cols-2">
          <!-- 模型键 -->
          <label class="block">
            <span class="mb-1.5 block text-caption text-neutral-500">
              模型键 <em class="not-italic text-danger" aria-hidden="true">*</em>
            </span>
            <input
              v-model="form.model_key"
              type="text"
              maxlength="64"
              class="gd-input gd-num"
              :disabled="Boolean(editing)"
              placeholder="如 qwen-image-3.0"
              aria-label="模型键"
            />
            <span v-if="editing" class="mt-1 block text-caption text-neutral-400">模型键创建后不可修改</span>
          </label>

          <!-- 模型名称 -->
          <label class="block">
            <span class="mb-1.5 block text-caption text-neutral-500">
              模型名称 <em class="not-italic text-danger" aria-hidden="true">*</em>
            </span>
            <input
              v-model="form.model_name"
              type="text"
              maxlength="128"
              class="gd-input"
              placeholder="展示名称，如 Qwen Image 3.0"
              aria-label="模型名称"
            />
          </label>

          <!-- 协议 -->
          <label class="block">
            <span class="mb-1.5 block text-caption text-neutral-500">调用协议</span>
            <select v-model="form.protocol" class="gd-input" aria-label="调用协议">
              <option v-for="option in createProtocolOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
          </label>

          <!-- API 模型名 -->
          <label class="block">
            <span class="mb-1.5 block text-caption text-neutral-500">
              API 模型名 <em class="not-italic text-danger" aria-hidden="true">*</em>
            </span>
            <input
              v-model="form.api_model"
              type="text"
              maxlength="128"
              class="gd-input gd-num"
              placeholder="三方接口使用的模型标识"
              aria-label="API 模型名"
            />
          </label>
        </div>

        <!-- 接口地址 -->
        <label class="block">
          <span class="mb-1.5 block text-caption text-neutral-500">
            接口地址 <em class="not-italic text-danger" aria-hidden="true">*</em>
          </span>
          <input
            v-model="form.base_url"
            type="text"
            maxlength="512"
            class="gd-input gd-num"
            :placeholder="
              form.protocol === 'dashscope_tts'
                ? 'wss://.../api-ws/v1/inference'
                : 'https://...（OpenAI 兼容填基础地址即可）'
            "
            aria-label="接口地址"
          />
          <span class="mt-1 block text-caption text-neutral-400">
            语音模型的地址填写 WebSocket 推理地址；沙箱协议可留空。
          </span>
        </label>

        <!-- 密钥 -->
        <label class="block">
          <span class="mb-1.5 block text-caption text-neutral-500">访问密钥</span>
          <input
            v-model="form.api_key"
            type="password"
            maxlength="1024"
            autocomplete="new-password"
            class="gd-input gd-num"
            :placeholder="apiKeyPlaceholder"
            aria-label="访问密钥"
          />
          <span v-if="editing?.api_key_configured" class="mt-1.5 flex items-center gap-2 text-caption text-neutral-500">
            <input v-model="form.clear_api_key" type="checkbox" class="h-3.5 w-3.5 accent-primary-500" />
            清除已保存密钥（改回使用环境变量配置）
          </span>
          <span v-else class="mt-1 block text-caption text-neutral-400">
            密钥仅保存在服务端，接口只返回掩码；留空则沿用环境变量配置。
          </span>
        </label>

        <div class="grid gap-4 sm:grid-cols-3">
          <!-- 计费单位 -->
          <label class="block">
            <span class="mb-1.5 block text-caption text-neutral-500">计费单位</span>
            <select v-model="form.unit" class="gd-input" aria-label="计费单位">
              <option v-for="option in currentUnitOptions" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </select>
          </label>

          <!-- 单价 -->
          <label class="block">
            <span class="mb-1.5 block text-caption text-neutral-500">单价（积分）</span>
            <input
              v-model.number="form.unit_price_credits"
              type="number"
              min="0"
              step="1"
              class="gd-input gd-num"
              aria-label="单价（积分）"
            />
          </label>

          <!-- 质量分 -->
          <label class="block">
            <span class="mb-1.5 block text-caption text-neutral-500">质量分（0~10，可空）</span>
            <input
              v-model.number="form.quality_score"
              type="number"
              min="0"
              max="10"
              step="0.1"
              class="gd-input gd-num"
              aria-label="质量分"
            />
          </label>
        </div>

        <!-- 扩展参数 -->
        <label class="block">
          <span class="mb-1.5 block text-caption text-neutral-500">扩展参数（JSON 对象，可空）</span>
          <textarea
            v-model="form.extra_config_text"
            rows="3"
            class="gd-input gd-num resize-y"
            placeholder='如 {"voice": "Cherry"}'
            aria-label="扩展参数 JSON"
          ></textarea>
        </label>

        <!-- 启用 / 默认 -->
        <div class="flex flex-wrap items-center gap-5">
          <label class="flex items-center gap-2 text-sm text-neutral-700">
            <input v-model="form.is_available" type="checkbox" class="h-4 w-4 accent-primary-500" />
            启用该模型
          </label>
          <label v-if="!editing" class="flex items-center gap-2 text-sm text-neutral-700">
            <input v-model="form.is_default" type="checkbox" class="h-4 w-4 accent-primary-500" />
            设为该类型默认模型
          </label>
          <span v-else class="text-caption text-neutral-400">
            需要调整默认模型请在列表中点击「设为默认」。
          </span>
        </div>

        <div class="flex justify-end gap-2.5 pt-1">
          <AppButton variant="secondary" size="md" type="button" :disabled="submitting" @click="modalOpen = false">
            取消
          </AppButton>
          <AppButton variant="primary" size="md" type="submit" :loading="submitting">
            {{ editing ? '保存修改' : '创建配置' }}
          </AppButton>
        </div>
      </form>
    </Modal>

    <!-- ============ 删除二次确认 ============ -->
    <ConfirmDialog
      v-model="removeDialogOpen"
      title="确认删除该模型配置？"
      :content="`删除「${removeTarget?.model_name ?? ''}」后，其路由与计费规则一并清理；若为某任务类型最后可用模型将拒绝删除。`"
      confirm-label="确认删除"
      :loading="removing"
      @confirm="confirmRemove"
      @cancel="removeTarget = null"
    />
  </div>
</template>

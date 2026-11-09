/**
 * e2e 视觉验收脚本 · 主题「极夜 · 极光」重构验收。
 *
 * 用 playwright-core + 系统 Edge，对 dev server(5175) 截取全部 24 条路由：
 *   桌面 1440×900 全量 / 移动 390×844 抽检，逐页断言「零横向溢出 + 零 console 错误」。
 * 通过路由拦截 mock /api/v1 数据并注入登录态，不依赖真实账号与后端。
 *
 * mock 的两个坑（都已处理）：
 *   · /events/stream 必须回 text/event-stream，否则 EventSource 会连报
 *     "MIME type is not text/event-stream" 并把 console.error 刷满；
 *   · 剧本 / 分镜 / 资产等接口要回**完整结构**（不是空 items），
 *     否则视图读 `script.title` 时抛 TypeError，会污染 pageerror 断言。
 *
 * 用法：
 *   node scripts/aurora-verify.mjs              # 全部页面
 *   node scripts/aurora-verify.mjs billing      # 只跑名称含 billing 的页面
 */
import { chromium } from 'file:///C:/Users/10486/.workbuddy/binaries/node/workspace/node_modules/playwright-core/index.mjs'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'e2e-verify', 'aurora')
mkdirSync(OUT, { recursive: true })

const BASE = 'http://127.0.0.1:5175'
const only = process.argv[2] ?? ''
const now = Date.now()
const iso = (h) => new Date(now - h * 3600e3).toISOString()

const project = {
  id: 1,
  title: '失踪的回声',
  synopsis: '一桩十年前的失踪案，在一场暴雨夜被重新按下播放键。',
  genre: '悬疑逆袭',
  style_preset_key: 'live_action',
  aspect_ratio: '16:9',
  language: 'zh-CN',
  status: 'script_review',
  target_episode_count: 12,
  budget_credits: 5000,
  spent_credits: 3240,
  updated_at: iso(2),
  created_at: iso(120),
}

const projects = [
  project,
  { ...project, id: 2, title: '长安夜行记', genre: '古装宫斗', status: 'storyboarding', aspect_ratio: '9:16', target_episode_count: 8, budget_credits: 4000, spent_credits: 1500, updated_at: iso(5) },
  { ...project, id: 3, title: '深夜便利店', genre: '都市情感', status: 'rendering', aspect_ratio: '9:16', target_episode_count: 6, budget_credits: 3600, spent_credits: 2100, updated_at: iso(26) },
  { ...project, id: 4, title: '我的AI房东', genre: '甜宠日常', status: 'completed', aspect_ratio: '16:9', target_episode_count: 12, budget_credits: 6000, spent_credits: 5320 },
  { ...project, id: 5, title: '川西来信', genre: '科幻末世', status: 'draft', target_episode_count: 0, budget_credits: 0, spent_credits: 0 },
]

const billing = { balance_credits: 1240, frozen_credits: 320, total_charged_credits: 6000, total_consumed_credits: 3240, updated_at: iso(1) }

const teamMembers = [
  { id: 1, user_id: 1, nickname: '林晚舟', phone: '138****8888', role: 'owner', joined_at: iso(300) },
  { id: 2, user_id: 2, nickname: '沈青野', phone: '139****2043', role: 'admin', joined_at: iso(210) },
  { id: 3, user_id: 3, nickname: '陆迟', phone: '137****5566', role: 'member', joined_at: iso(96) },
  { id: 4, user_id: 4, nickname: '苏与安', phone: '135****7781', role: 'guest', joined_at: iso(24) },
]

const notifications = [
  { id: 1, type: 'approval', title: '关口一 · 剧本确认待处理', content: '《失踪的回声》第 02 集剧本已生成，等待你的确认。', unread: true, created_at: iso(1) },
  { id: 2, type: 'render', title: '生成完成：S03 镜头组', content: '4 个镜头已生成，一致性评分 0.93。', unread: true, created_at: iso(3) },
  { id: 3, type: 'system', title: '积分余额提醒', content: '当前可用 1,240 积分，低于本集预估消耗。', unread: false, created_at: iso(28) },
]

/** 剧本全树：必须给 script 对象，视图会读 script.title / script.version */
const scriptTree = {
  script: {
    id: 11,
    version_no: 3,
    title: '失踪的回声 · v3',
    logline: '一盘旧磁带，让十年前的雨夜重新落了下来。',
    status: 'in_review',
    word_count: 8420,
    updated_at: iso(2),
  },
  episodes: [
    {
      id: 101,
      episode_no: 1,
      title: '第 01 集 · 雨夜来电',
      logline: '刑警顾行接到一通来自十年前的报警电话。',
      hook_text: '磁带里出现了第二个人的呼吸声。',
      target_duration_seconds: 180,
      scenes: [
        {
          id: 1001,
          scene_no: 1,
          heading: '内景 · 档案室 - 夜',
          location: '档案室',
          int_ext: '内景',
          time_of_day: '夜',
          summary: '顾行翻出 2014 年的旧卷宗，磁带机突然自走。',
          lines: [
            { id: 1, line_type: 'action', line_no: 1, content: '档案室的灯管闪了一下，磁带机开始自转。', confidence: 0.94 },
            { id: 2, line_type: 'dialogue', line_no: 2, speaker: '顾行', content: '这盘带子……十年没人动过。', emotion: '迟疑', confidence: 0.96 },
            { id: 3, line_type: 'dialogue', line_no: 3, speaker: '录音', content: '（电流杂音）——有人吗？我在……B3 层。', emotion: '恐惧', confidence: 0.61 },
          ],
        },
        {
          id: 1002,
          scene_no: 2,
          heading: '外景 · 旧纺织厂 - 暴雨',
          location: '旧纺织厂',
          int_ext: '外景',
          time_of_day: '夜',
          summary: '顾行驱车赶往早已废弃的厂区。',
          lines: [
            { id: 4, line_type: 'action', line_no: 1, content: '雨刷开到最大，挡风玻璃上仍是一片水幕。', confidence: 0.9 },
          ],
        },
      ],
    },
    {
      id: 102,
      episode_no: 2,
      title: '第 02 集 · B3 层',
      logline: '废弃厂区的地下三层，藏着一间不该存在的房间。',
      hook_text: '门牌上写着顾行自己的名字。',
      target_duration_seconds: 180,
      scenes: [],
    },
  ],
}

const scriptOutline = {
  project_id: 1,
  episodes: scriptTree.episodes.map((e) => ({
    id: e.id,
    episode_no: e.episode_no,
    title: e.title,
    logline: e.logline,
    hook_text: e.hook_text,
    scenes: (e.scenes ?? []).map((s) => ({ id: s.id, scene_no: s.scene_no, heading: s.heading, summary: s.summary })),
  })),
}

/** 分镜板摘要列表（GET /projects/{id}/storyboards → { items: StoryboardSummary[] }） */
const storyboards = {
  items: [
    { id: 201, project_id: 1, episode_id: 101, episode_no: 1, version_no: 2, status: 'draft', shot_count: 3, estimated_duration_ms: 12200, locked_at: null, created_at: iso(6) },
  ],
  total: 1,
}

/** 分镜详情（GET /storyboards/{id} → StoryboardDetail，shots 内嵌） */
const storyboardDetail = {
  id: 201,
  project_id: 1,
  episode_id: 101,
  episode_no: 1,
  version_no: 2,
  status: 'draft',
  locked_at: null,
  created_at: iso(6),
  shots: [
    {
      id: 3011, storyboard_id: 201, order_index: 0, shot_no: 1, scene_no: 1,
      shot_size: 'medium', camera_move: 'push_in', duration_ms: 4200,
      dialogue: '这盘带子……十年没人动过。', action_desc: '档案室推轨，磁带机特写',
      emotion_tag: 'tension', status: 'locked', thumbnail_url: null, x: 80, y: 80,
    },
    {
      id: 3012, storyboard_id: 201, order_index: 1, shot_no: 2, scene_no: 1,
      shot_size: 'close_up', camera_move: 'static', duration_ms: 2800,
      dialogue: '（电流杂音）——有人吗？', action_desc: '磁带机指示灯开始闪烁',
      emotion_tag: 'fear', status: 'generating', thumbnail_url: null, x: 360, y: 80,
    },
    {
      id: 3013, storyboard_id: 201, order_index: 2, shot_no: 3, scene_no: 2,
      shot_size: 'wide', camera_move: 'pan_right', duration_ms: 5200,
      dialogue: '', action_desc: '旧纺织厂外景，暴雨倾盆',
      emotion_tag: 'calm', status: 'pending_review', thumbnail_url: null, x: 640, y: 80,
    },
  ],
}

const assets = {
  items: [
    { id: 301, project_id: 1, asset_type: 'character', name: '顾行', status: 'approved', thumbnail_url: null, qc_score: 0.93, version: 2, created_at: iso(30) },
    { id: 302, project_id: 1, asset_type: 'character', name: '林小满', status: 'pending_review', thumbnail_url: null, qc_score: 0.71, version: 1, created_at: iso(6) },
    { id: 303, project_id: 1, asset_type: 'prop', name: '磁带机', status: 'generating', thumbnail_url: null, qc_score: null, version: 1, created_at: iso(1) },
    { id: 304, project_id: 1, asset_type: 'scene', name: '档案室', status: 'qc_failed', thumbnail_url: null, qc_score: 0.42, version: 1, created_at: iso(2) },
    { id: 305, project_id: 1, asset_type: 'character', name: '顾行', status: 'approved', thumbnail_url: null, qc_score: 0.88, version: 3, created_at: iso(48) },
    { id: 306, project_id: 1, asset_type: 'prop', name: '旧卷宗', status: 'approved', thumbnail_url: null, qc_score: 0.9, version: 1, created_at: iso(50) },
  ],
  page: 1,
  page_size: 12,
  total: 6,
}

/** 角色详情：versions / qc_reports / voice 三个数组必须齐全，视图会直接 .filter() */
const assetDetail = {
  ...assets.items[0],
  description: '刑警，四十岁上下，深灰风衣，习惯性压着嗓子说话。',
  versions: [
    { id: 9001, asset_id: 301, version_type: 'portrait', version_no: 2, media_file_id: null, media_url: '/mock/portrait-2.png', prompt: '正面半身，冷光侧打，深灰风衣', seed: 88421, model_key: 'gpt-image-1', qc_report_id: 1, is_current: true, created_at: iso(6) },
    { id: 9002, asset_id: 301, version_type: 'portrait', version_no: 1, media_file_id: null, media_url: '/mock/portrait-1.png', prompt: '正面半身，暖光', seed: 77310, model_key: 'gpt-image-1', qc_report_id: null, is_current: false, created_at: iso(30) },
    { id: 9003, asset_id: 301, version_type: 'turnaround_front', version_no: 1, media_file_id: null, media_url: '/mock/tf.png', prompt: '三视图 · 正面', seed: 55120, model_key: 'gpt-image-1', qc_report_id: null, is_current: true, created_at: iso(20) },
    { id: 9004, asset_id: 301, version_type: 'turnaround_side', version_no: 1, media_file_id: null, media_url: null, prompt: '三视图 · 侧面', seed: null, model_key: 'gpt-image-1', qc_report_id: null, is_current: false, created_at: iso(20) },
  ],
  qc_reports: [
    { id: 1, asset_id: 301, version_id: 9001, overall_score: 0.93, consistency_score: 0.95, detail: { version_type: 'portrait', face_consistency: 0.95, lighting_match: 0.9, artifact: 0.05 }, issues: [], created_at: iso(6) },
  ],
  voice: { id: 1, name: '沉稳男声 · 低音', provider: 'mock', language: 'zh-CN', preview_url: null },
}

const renderTasks = {
  items: [
    {
      id: 401, shot_id: 202, task_type: 'image_to_video', status: 'running', priority: 2, retry_count: 0,
      estimated_cost_credits: 120, actual_cost_credits: null, failure_type: null, failure_reason: null,
      created_at: iso(1), progress: 62,
      shot: { id: 202, shot_no: 2, order_index: 1, duration_ms: 2800, status: 'generating', final_prompt: '暴雨夜，档案室，冷蓝侧光，中近景' },
    },
    {
      id: 402, shot_id: 203, task_type: 'text_to_image', status: 'queued', priority: 5, retry_count: 0,
      estimated_cost_credits: 40, actual_cost_credits: null, failure_type: null, failure_reason: null,
      created_at: iso(1), progress: 0,
      shot: { id: 203, shot_no: 1, order_index: 2, duration_ms: 5200, status: 'planned', final_prompt: '旧纺织厂外景，暴雨，广角全景' },
    },
    {
      id: 403, shot_id: 201, task_type: 'image_to_video', status: 'succeeded', priority: 1, retry_count: 0,
      estimated_cost_credits: 110, actual_cost_credits: 104, failure_type: null, failure_reason: null,
      created_at: iso(3), progress: 100,
      shot: { id: 201, shot_no: 1, order_index: 0, duration_ms: 4200, status: 'done', final_prompt: '档案室推轨，磁带机特写' },
    },
    {
      id: 404, shot_id: 201, task_type: 'tts', status: 'failed', priority: 3, retry_count: 2,
      estimated_cost_credits: 12, actual_cost_credits: null, failure_type: 'timeout', failure_reason: '语音合成超时（gateway timeout）',
      created_at: iso(4), progress: 34,
      shot: { id: 201, shot_no: 1, order_index: 0, duration_ms: 4200, status: 'failed', final_prompt: '顾行台词 · 低沉男声' },
    },
  ],
  total: 4,
  page: 1,
  page_size: 20,
}

const plans = {
  items: [
    {
      id: 1, key: 'free', name: '免费版', billing_period: 'month', price_cents: 0, currency: 'CNY',
      credits_per_period: 0, concurrency_limit: 1,
      benefits: { max_resolution: '720p', watermark: true, seat_limit: 1 },
    },
    {
      id: 2, key: 'creator', name: '创作者', billing_period: 'month', price_cents: 4900, currency: 'CNY',
      credits_per_period: 6000, concurrency_limit: 3,
      benefits: { max_resolution: '1080p', watermark: false, seat_limit: 3 },
    },
    {
      id: 3, key: 'studio', name: '工作室', billing_period: 'month', price_cents: 19900, currency: 'CNY',
      credits_per_period: 30000, concurrency_limit: 8,
      benefits: { max_resolution: '4k', watermark: false, seat_limit: 20 },
    },
  ],
  subscription: {
    plan_id: 2, plan_key: 'creator', status: 'active',
    started_at: iso(720), expires_at: iso(-240), auto_renew: true,
  },
}

const transactions = {
  items: [
    { id: 501, transaction_type: 'settle', amount_credits: -120, balance_after_credits: 1240, note: '视频生成 · S03-02', created_at: iso(1) },
    { id: 502, transaction_type: 'freeze', amount_credits: -320, balance_after_credits: 1360, note: '生成任务预算冻结', created_at: iso(2) },
    { id: 503, transaction_type: 'recharge', amount_credits: 6000, balance_after_credits: 1680, note: '充值 6,000 积分', created_at: iso(96) },
    { id: 504, transaction_type: 'refund', amount_credits: 40, balance_after_credits: 1640, note: '任务失败退还', created_at: iso(98) },
  ],
  total: 4,
}

const teams = [
  { id: 1, name: '浮光造梦 · 制作组', owner_user_id: 1, seat_limit: 8, status: 'active', my_role: 'owner', created_at: iso(720) },
]

const teamMemberList = teamMembers.map((m) => ({
  team_id: 1,
  user_id: m.user_id,
  role: m.role,
  invited_by: 1,
  nickname: m.nickname,
  joined_at: m.joined_at,
}))

// 平台账号：platform 取后端 CHECK 允许值（douyin/kuaishou/wechat_channels/jianying），
// status 取 active/expired/revoked —— 自造 bilibili/xiaohongshu/connected 只会拍出一屏英文 key
const platformAccounts = [
  { id: 601, platform: 'douyin', nickname: '浮光造梦官方', status: 'active', authorized_at: iso(300), token_expires_at: iso(-1440), scope: 'video.publish' },
  { id: 602, platform: 'kuaishou', nickname: '浮光造梦', status: 'active', authorized_at: iso(280), token_expires_at: iso(-720), scope: 'video.publish' },
  { id: 603, platform: 'wechat_channels', nickname: '浮光造梦工作室', status: 'expired', authorized_at: iso(720), token_expires_at: iso(24), scope: 'video.publish' },
]

const notificationsList = { items: notifications, total: notifications.length }

const ok = (data) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ code: 0, message: 'ok', data, request_id: 'mock' }),
})

/** 按路径返回 mock 数据（路径顺序敏感：先匹配更具体的） */
function mockFor(pathname) {
  const p = pathname.replace(/^\/api\/v1/, '')
  if (p === '/projects') return { items: projects, total: projects.length, page: 1, page_size: 12 }
  if (/^\/projects\/\d+$/.test(p)) return project
  if (p.endsWith('/scripts/current')) return scriptTree
  if (p.endsWith('/script-outline') || p.endsWith('/outline')) return scriptOutline
  if (p.endsWith('/approvals')) return { items: [], total: 0 }
  if (/\/storyboards\/\d+$/.test(p)) return storyboardDetail
  if (p.endsWith('/storyboards')) return storyboards
  if (p.endsWith('/assets')) return assets
  if (/\/assets\/\d+$/.test(p)) return assetDetail
  // 批量成本估算：CostEstimateDialog 直接读 per_shot.length / shot_count，
  // 缺字段会当场抛 "Cannot read properties of undefined (reading 'length')"
  if (p.endsWith('/render/estimate')) {
    const perShot = [
      { shot_id: 201, estimated_credits: 40 },
      { shot_id: 202, estimated_credits: 40 },
      { shot_id: 203, estimated_credits: 48 },
      { shot_id: 204, estimated_credits: 48 },
    ]
    return { per_shot: perShot, total_credits: 176, shot_count: perShot.length, available_credits: 920 }
  }
  // 任务详情：抽屉直接读 detail.outputs.length / detail.qc_reports.length，
  // 少一个数组就是白屏 + "Cannot read properties of undefined (reading 'length')"
  if (/\/render-tasks\/\d+$/.test(p)) {
    const base = renderTasks.items[0]
    return {
      ...base,
      id: 401,
      model_key: 'kling-v2',
      max_retries: 3,
      frozen_credits: 120,
      input_hash: 'a1f3c9e8',
      queued_at: iso(1),
      started_at: iso(1),
      finished_at: null,
      outputs: [
        { id: 9001, output_type: 'video', url: null, duration_ms: 2800, width: 1080, height: 1920, file_size: 5242880, created_at: iso(1) },
      ],
      qc_reports: [
        { id: 5001, result: 'warning', consistency_score: 0.72, round_no: 1, detail: { note: '首轮光影跳变' }, created_at: iso(1) },
        { id: 5002, result: 'passed', consistency_score: 0.93, round_no: 2, detail: { note: '自动修正后达标' }, created_at: iso(1) },
      ],
      freeze: { frozen_credits: 120, settled_credits: 0, status: 'frozen' },
    }
  }
  if (p.endsWith('/render-tasks') || p.endsWith('/render/jobs')) return renderTasks
  if (p.endsWith('/billing/credit-account')) return billing
  if (p.endsWith('/billing/plans')) return plans
  if (p.endsWith('/billing/transactions')) return transactions
  if (p.endsWith('/notifications/unread-count')) return { count: 2 }
  if (p.endsWith('/notifications')) return notificationsList
  if (p === '/teams') return teams
  if (/^\/teams\/[\w-]+\/members$/.test(p)) return teamMemberList
  if (p.endsWith('/members')) return { owner_user_id: 1, items: teamMemberList.map((m) => ({ user_id: m.user_id, permission: 'edit', nickname: m.nickname, is_owner: m.user_id === 1 })) }
  if (p.endsWith('/team/members')) return { items: teamMembers, total: teamMembers.length }
  if (p.endsWith('/platform-accounts')) return { items: platformAccounts, total: platformAccounts.length }
  if (p.endsWith('/tts-voices')) return { items: [], page: 1, page_size: 60, total: 0 }
  if (p.endsWith('/users/me'))
    return {
      user: {
        id: 1,
        phone: '13800008888',
        nickname: '林晚舟',
        avatar_url: null,
        status: 'active',
        role: 'owner',
        preferences: { genres: ['suspense'], style_preset_key: 'live_action', aspect_ratio: '16:9', episode_count: 12, notify: { render_done: true, approval: true } },
        created_at: iso(900),
      },
      credit: { balance_credits: 1240, frozen_credits: 320 },
    }
  return { items: [], total: 0 }
}

async function setup(context, options = {}) {
  // 登录 / 注册页必须「未登录」状态打开：否则守卫会把 /login 重定向到 /projects，
  // 截出来的是工作台而不是登录表单
  if (!options.noAuth) {
    await context.addInitScript(() => {
      localStorage.setItem('gd_access_token', 'mock-token')
      localStorage.setItem('gd_refresh_token', 'mock-refresh')
      localStorage.setItem('gd_user', JSON.stringify({ id: 1, phone: '13800008888', nickname: '林晚舟', roles: ['owner'] }))
    })
  }
  // 图片桩：mock 里的 media_url 指向 /mock/*.png，若不拦截，
  // 浏览器会画出「裂图」图标 + alt 文字，把「空态设计得对不对」的复核变成噪音。
  // 这里直接回一张 SVG（content-type 决定渲染，跟扩展名无关），观感接近一张生成图。
  await context.route('**/mock/**', (route) =>
    route.fulfill({
      status: 200,
      headers: { 'content-type': 'image/svg+xml', 'cache-control': 'no-store' },
      body: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400">
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#171043"/><stop offset=".55" stop-color="#3a2c9e"/><stop offset="1" stop-color="#8b7cff"/>
          </linearGradient>
          <radialGradient id="glow" cx=".5" cy=".38" r=".5">
            <stop offset="0" stop-color="#edf3ff" stop-opacity=".28"/><stop offset="1" stop-color="#edf3ff" stop-opacity="0"/>
          </radialGradient>
        </defs>
        <rect width="400" height="400" fill="url(#g)"/>
        <ellipse cx="200" cy="170" rx="150" ry="150" fill="url(#glow)"/>
        <circle cx="200" cy="158" r="54" fill="#0f1730" opacity=".55"/>
        <path d="M112 320c0-49 39-89 88-89s88 40 88 89z" fill="#0f1730" opacity=".55"/>
      </svg>`,
    }),
  )

  await context.route('**/api/v1/**', async (route) => {
    const p = new URL(route.request().url()).pathname
    // 真实端点是 /api/v1/projects/{id}/events（EventSource 走 token 查询参数），
    // 必须回 text/event-stream，否则每页都会刷 "MIME type is not text/event-stream"
    if (p.endsWith('/events') || p.endsWith('/events/stream')) {
      return route.fulfill({
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' },
        body: ': keep-alive\n\n',
      })
    }
    await route.fulfill(ok(mockFor(p)))
  })
}

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const errors = []
const overflows = []

async function shoot(name, viewport, options = {}) {
  const context = await browser.newContext({
    viewport,
    deviceScaleFactor: 1,
    isMobile: options.mobile ?? false,
    hasTouch: options.mobile ?? false,
  })
  await setup(context, options)
  const page = await context.newPage()
  page.on('pageerror', (e) => errors.push(`[${name}] pageerror: ${e.message}`))
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`[${name}] console.error: ${m.text()}`)
  })
  await page.goto(`${BASE}${options.path ?? '/login'}`, { waitUntil: 'domcontentloaded' })
  // 1.8s：多个视图有 0.6~0.8s 的入场动画（auth-drop / auth-rise / gd-page-enter），
  // 截太早会拍到 opacity 尚为 0 的帧，误判成「内容缺失」
  await page.waitForTimeout(options.wait ?? 1800)
  // 交互态：弹窗 / 抽屉 / Toast 只有真的点开才拍得到，静态路由截图永远看不见它们
  if (options.act) {
    await options.act(page)
    await page.waitForTimeout(options.actWait ?? 700)
  }
  await page.screenshot({ path: join(OUT, `${name}.png`), fullPage: options.fullPage ?? false })
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  )
  if (overflow > 0) overflows.push(`${name}: ${overflow}px`)
  console.log(`${name}: shot ok, h-overflow=${overflow}px`)
  await context.close()
}

const DESKTOP = { width: 1440, height: 900 }
const MOBILE = { width: 390, height: 844 }

// 24 条路由全量（桌面）
const desktopRoutes = [
  ['login', '/login'],
  ['register', '/register'],
  ['onboarding', '/onboarding'],
  ['projects', '/projects'],
  ['project-create', '/projects/new'],
  ['overview', '/projects/1/overview'],
  ['script', '/projects/1/script'],
  ['script-outline', '/projects/1/script/outline'],
  ['script-review', '/projects/1/script/review'],
  ['assets', '/projects/1/assets'],
  ['character', '/projects/1/assets/characters/301'],
  ['face-auth', '/projects/1/assets/authorizations/new'],
  ['storyboard', '/projects/1/storyboard'],
  ['shot-detail', '/projects/1/storyboard/shots/3011'],
  ['draft-preview', '/projects/1/storyboard/draft-preview'],
  ['render', '/projects/1/render-tasks'],
  ['post', '/projects/1/post'],
  ['final-review', '/projects/1/post/review'],
  ['distribute', '/projects/1/distribute'],
  ['billing', '/billing'],
  ['team', '/settings/team'],
  ['platform-accounts', '/settings/platform-accounts'],
  ['notifications', '/notifications'],
  ['profile', '/settings/profile'],
]

// 移动档抽检（壳层 + 列表 + 表单 + 播放器各取一个）
const mobileRoutes = [
  ['login', '/login'],
  ['projects', '/projects'],
  ['project-create', '/projects/new'],
  ['script', '/projects/1/script'],
  ['storyboard', '/projects/1/storyboard'],
  ['billing', '/billing'],
  ['team', '/settings/team'],
]

// 交互态抽检：弹窗 / 抽屉 / Toast / 空态。这些表面静态路由截图拍不到，
// 而它们恰恰是「通用组件层」重构后最容易漏掉的 24 张面孔。
const ACTS = [
  ['act-asset-reject', '/projects/1/assets', (page) => page.locator('button', { hasText: '不通过' }).first().click()],
  ['act-asset-regenerate', '/projects/1/assets', (page) => page.locator('button', { hasText: '重抽' }).first().click()],
  ['act-asset-empty', '/projects/1/assets', async (page) => {
    // 选一个必然为空的组合：音色 Tab（mock 只造了角色 / 场景 / 道具）
    await page.locator('button', { hasText: '音色' }).first().click()
  }],
  ['act-project-menu', '/projects', (page) => page.locator('[aria-label="更多操作"]').first().click()],
  ['act-project-archive', '/projects', async (page) => {
    await page.locator('[aria-label="更多操作"]').first().click()
    await page.waitForTimeout(250)
    await page.locator('button', { hasText: '归档' }).first().click()
  }],
  ['act-render-cost', '/projects/1/render-tasks', (page) => page.locator('button', { hasText: '批量编排' }).first().click()],
  // 分发页的 CostEstimateDialog 由同一 Modal 配方渲染，渲染中心那条已覆盖视觉；
  // 这里不再重复点「开始导出」——它要 project.status=completed 且分辨率有权益才可点，
  // 造这套 mock 的收益低于维护成本。
  ['act-notice-detail', '/notifications', (page) => page.locator('ul li button').first().click()],
  ['act-render-detail', '/projects/1/render-tasks', (page) => page.locator('button', { hasText: '详情' }).first().click()],
]

// onboarding 属受保护路由（守卫会把未登录用户弹回登录页），必须以已登录态截
const NO_AUTH = new Set(['login', 'register'])

for (const [name, path] of desktopRoutes) {
  const id = `${name}-1440`
  if (only && !id.includes(only)) continue
  await shoot(id, DESKTOP, { path, noAuth: NO_AUTH.has(name) })
}
for (const [name, path] of mobileRoutes) {
  const id = `${name}-390`
  if (only && !id.includes(only)) continue
  await shoot(id, MOBILE, { path, mobile: true, noAuth: NO_AUTH.has(name) })
}
for (const [name, path, act] of ACTS) {
  if (!only || !name.includes(only)) continue
  await shoot(name, DESKTOP, { path, act })
}

await browser.close()
console.log(overflows.length ? `\nOVERFLOW:\n${overflows.join('\n')}` : '\nNo horizontal overflow.')
console.log(errors.length ? `\nERRORS (${errors.length}):\n${errors.slice(0, 30).join('\n')}` : '\nNo console/page errors.')

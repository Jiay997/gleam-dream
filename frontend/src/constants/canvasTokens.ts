/**
 * 文件级注释：Konva 画布专用颜色常量（主题「极夜 · 极光」）。
 *
 * 为什么需要这个文件：
 * Konva 在 Canvas 2D 上下文中绘制，**无法读取 CSS 变量**，因此画布域的颜色只能以字面量
 * 形式传入。本模块是画布域颜色的唯一来源，取值与 tokens.css / aurora.css 一一对应
 * （注释标注对应令牌），修改配色时请**同时**更新样式层与本文件，保持两侧同源。
 *
 * ── v5 换肤要点（极夜 · 极光）─────────────────────────────────────────
 * 1. 画布从「暖米纸」翻成「极夜底」：分镜布 / 后期台面不再是一张纸，而是同一块夜色，
 *    与 DOM 层同色（#070B16），否则 Konva 区域会像贴了一盏亮灯。
 * 2. 色的语义翻转：亮底时代「深色 = 强调」，暗底时代「亮色 = 强调」。
 *    因此 INK 阶整体反向（n900 最亮），BRAND 的 p700/p800 用来当**暗底上的亮字**。
 * 3. 低饱和的浅色块（情绪徽标底、时间线轨道填充）在暗底上必须改成「同色洗底」，
 *    否则会变成一坨发灰的脏点——这是 Canvas 里最容易翻车的一处。
 * 4. 一律以 rgba 书写半透明档：Canvas 的 fillStyle 支持 rgba，
 *    而 `transparent` 在渐变插值中会走向灰边。
 */

/** 品牌星紫阶（--gd-violet / --gd-primary-*） */
export const BRAND = {
  /** --gd-primary-50（选中底 / 徽标底，暗底上取 12% 洗色） */
  p50: 'rgba(139, 124, 255, 0.12)',
  /** --gd-primary-100 */
  p100: 'rgba(139, 124, 255, 0.18)',
  /** --gd-primary-200（描边档） */
  p200: 'rgba(139, 124, 255, 0.30)',
  /** --gd-primary-400（次级强调） */
  p400: '#a99bff',
  /** --gd-primary-500（主色） */
  p500: '#8b7cff',
  /** --gd-primary-700（暗底上的亮字，非「更深」） */
  p700: '#c6baff',
  /** --gd-primary-800 */
  p800: '#d8d1ff',
} as const

/** 中性色（冷调夜色阶，与 tokens.css 同步；阶位越大越亮） */
export const INK = {
  /** 纯白（画布高光点） */
  n0: '#ffffff',
  /** --gd-neutral-100（最亮档，用于「纸」级对比） */
  n100: '#f7f9ff',
  /** --gd-neutral-200（发丝描边） */
  n200: 'rgba(255, 255, 255, 0.09)',
  /** --gd-neutral-300（强调描边） */
  n300: 'rgba(255, 255, 255, 0.16)',
  /** --gd-neutral-400（三级文字 · 6.2:1） */
  n400: '#7d8fb3',
  /** --gd-neutral-500（三级文字） */
  n500: '#7d8fb3',
  /** --gd-neutral-600（次级文字 · 10.2:1） */
  n600: '#aab9d6',
  /** --gd-neutral-700（次级文字） */
  n700: '#aab9d6',
  /** --gd-neutral-800（正文 · 18.1:1） */
  n800: '#edf3ff',
  /** --gd-neutral-900（标题，与正文同色，层级交给字号字重） */
  n900: '#edf3ff',
} as const

/** 业务五态 / 功能色（极光五义） */
export const STATE = {
  /** --gd-running / --gd-info（极青） */
  running: '#38d9f0',
  /** --gd-pending / --gd-warning（琥珀） */
  pending: '#ffc46b',
  /** --gd-locked / --gd-success（薄荷） */
  locked: '#3ee8b8',
  /** --gd-failed / --gd-danger（玫红） */
  failed: '#ff6e86',
  /** --gd-blocked（洋红） */
  blocked: '#f07be0',
} as const

/**
 * 画布域的「面」（Konva 里所有需要铺底的地方都从这里取）。
 *
 * 亮底时代卡片一律铺 `INK.n0`（白纸）；暗底时代白纸会变成一块发光的板子，
 * 所以面与字彻底解耦：面取 SURFACE，字取 INK / STATE / BRAND。
 */
export const SURFACE = {
  /** --gd-canvas · 极夜底 */
  canvas: '#04070f',
  /** --gd-surface · 卡片基色 */
  card: '#111a2c',
  /** --gd-panel · 卡内分组 */
  panel: '#16203a',
  /** --gd-panel-raised · 抬升分组 */
  raised: '#1a2540',
  /** --gd-panel-sunken · 下沉槽位 */
  sunken: '#0d1526',
} as const

/** 分镜画布专用（极夜底：与 DOM 层同色，不另起一块亮面） */export const CANVAS = {
  /** --gd-canvas-bg */
  bg: '#070b16',
  /** --gd-canvas-grid */
  grid: 'rgba(255, 255, 255, 0.05)',
  /** --gd-shot-selected（选中镜头的衬底） */
  selected: 'rgba(139, 124, 255, 0.10)',
  /** --gd-connector（镜头连线） */
  connector: 'rgba(255, 255, 255, 0.16)',
} as const

/** 播放器 / 视频预览域 */
export const PLAYER = {
  /** --gd-player-bg */
  bg: '#04070f',
  /** --gd-player-panel */
  panel: '#0d1526',
  /** --gd-player-text */
  text: '#edf3ff',
  /** 播放器内浮层（照片/视频上的角标底） */
  scrim: 'rgba(4, 7, 15, 0.55)',
} as const

/** 夜窗（--gd-window-bg / --gd-aurora-core，Konva 占位封面用） */
export const WINDOW = {
  /** --gd-window-bg */
  bg: '#080d18',
  /** 极光核心（占位辉光起色，透明档在调用处以 rgba 书写） */
  aurora: '139, 124, 255',
} as const

/** 后期时间线轨道（暗底上用「同色洗底 + 亮字」，不做浅色块） */
export const TIMELINE = {
  /** 视频轨 · 星紫 */
  video: { fill: 'rgba(139, 124, 255, 0.22)', text: '#c6baff', accent: '#8b7cff' },
  /** 音频轨 · 薄荷 */
  audio: { fill: 'rgba(62, 232, 184, 0.18)', text: '#7defc9', accent: '#3ee8b8' },
  /** 背景音乐轨 · 琥珀 */
  bgm: { fill: 'rgba(255, 196, 107, 0.18)', text: '#ffd79a', accent: '#ffc46b' },
  /** 字幕轨 · 极青 */
  subtitle: { fill: 'rgba(56, 217, 240, 0.18)', text: '#86e9f8', accent: '#38d9f0' },
  /** 播放指针 · 玫红 */
  playhead: '#ff6e86',
  /** 剪辑块选中描边 */
  clipSelected: '#c6baff',
} as const

/** 后期编辑台（时间线 Konva 画布底与刻度） */
export const EDITOR = {
  /** --gd-editor-stage（极夜台面，与分镜布同色） */
  stage: '#070b16',
  /** --gd-editor-tick（刻度） */
  tick: 'rgba(255, 255, 255, 0.08)',
  /** 剪辑块默认描边（未选中、未告警时） */
  clipStroke: 'rgba(255, 255, 255, 0.09)',
  /** 剪辑块拖拽手柄色 */
  handle: 'rgba(255, 255, 255, 0.28)',
  /** 分镜卡片投影色（暗域里投影几乎不可见，保留极轻一档避免完全失形） */
  shadow: 'rgba(4, 7, 15, 0.55)',
} as const

/**
 * 情绪视觉色（分镜域：画布占位渐变双端色 + 情绪徽标）。
 *
 * 结构不变（from / to / color / bg 四段），但取值全部翻到暗场：
 * - `from` / `to`：占位图的渐变两端，取「同色 40% 洗底 → 夜色」，避免画布里出现亮块；
 * - `color`：徽标前景，取各族的亮档（对 `bg` 洗底 ≥ 4.5:1）；
 * - `bg`：徽标底，一律 12% 上下的同色洗色。
 *
 * 情绪色是「辅助识别」，不是主色：8 种情绪全部落在极光五义的色相区间内，
 * 不引入第六种彩色（这条纪律从亮底版本一直保留到极夜版本）。
 */
export interface EmotionPalette {
  /** 渐变起始色 */
  from: string
  /** 渐变结束色 */
  to: string
  /** 徽标前景色 */
  color: string
  /** 徽标底色（洗色） */
  bg: string
}

/**
 * 8 种情绪 → 视觉色（key 与后端 DB CHECK 约束一致：backend/app/models/storyboard.py）。
 */
export const EMOTION: Record<string, EmotionPalette> = {
  /** 平静 · 中性夜阶 */
  calm: { from: '#33405c', to: '#1c2740', color: '#aab9d6', bg: 'rgba(255, 255, 255, 0.08)' },
  /** 喜悦 · 琥珀族 */
  joy: { from: '#6b5626', to: '#3a2f16', color: '#ffd79a', bg: 'rgba(255, 196, 107, 0.13)' },
  /** 愤怒 · 玫红族 */
  anger: { from: '#6b2a33', to: '#3a1a1f', color: '#ffb3be', bg: 'rgba(255, 110, 134, 0.12)' },
  /** 悲伤 · 极青族 */
  sadness: { from: '#1d4a68', to: '#12283a', color: '#86e9f8', bg: 'rgba(56, 217, 240, 0.12)' },
  /** 恐惧 · 品牌星紫 */
  fear: { from: '#4a3d8a', to: '#2a2350', color: '#c6baff', bg: 'rgba(139, 124, 255, 0.14)' },
  /** 惊讶 · 洋红族 */
  surprise: { from: '#6b2a55', to: '#3a1730', color: '#f7a8ee', bg: 'rgba(240, 123, 224, 0.12)' },
  /** 紧张 · 琥珀深阶 */
  tension: { from: '#6b4a1f', to: '#3a2a12', color: '#ffd79a', bg: 'rgba(255, 196, 107, 0.18)' },
  /** 浪漫 · 星紫渐入洋红（与「惊讶」靠 `to` 端色相区分） */
  romance: { from: '#4a3d8a', to: '#6b2a55', color: '#f7a8ee', bg: 'rgba(139, 124, 255, 0.12)' },
}

/** 情绪视觉色兜底（中性夜阶，与 calm 同族但更暗） */
export const EMOTION_FALLBACK: EmotionPalette = {
  from: '#33405c',
  to: '#1c2740',
  color: '#aab9d6',
  bg: 'rgba(255, 255, 255, 0.08)',
}

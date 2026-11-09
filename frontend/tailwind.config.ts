/**
 * 文件级注释：Tailwind CSS 配置（主题「极夜 · 极光」Aurora v1.5）。
 * 将 src/assets/styles/tokens.css 中的设计令牌（CSS 变量）映射为 Tailwind 语义色 /
 * 字体 / 字阶 / 圆角 / 阴影 / 断点，供全局以工具类方式消费。
 *
 * v2 核心纪律（修 P1「令牌双轨」）：
 * **本文件只允许出现 `var(--gd-*)`，禁止再写裸 px / ms / cubic-bezier / 十六进制色值。**
 * 此前 borderRadius(4/6/10/14/18px)、transitionDuration(150/200/300ms)、
 * transitionTimingFunction(cubic-bezier)、maxWidth.content(1600px) 四项与 tokens.css
 * 各写一份，数值恰好相同但改一处不会同步，是最隐蔽的一致性风险。
 *
 * 本次调整：
 * - 上述四项全部改为引用令牌；
 * - 新增字阶 kicker / lede / masthead / masthead-lg（杂志刊头两档 + 刊眉 + 导语）；
 * - 新增表面语义色 surface-* / hairline-*（新代码表达"面"用它，不用 neutral-*）；
 * - 新增功能色 strong 档（浅底上的可读加重版）；
 * - 圆角对齐新令牌值（sm 6 / md 6 / lg 14 / xl 18 / 2xl 22）；
 * - 字重收敛为 3 档（400 / 500 / 600），取消 700（中文 700 在 Windows 易糊）。
 */
import type { Config } from 'tailwindcss'

export default {
  /** 扫描范围：入口 HTML 与 src 下所有组件 / TS 文件 */
  content: ['./index.html', './src/**/*.{vue,ts,tsx}'],
  theme: {
    extend: {
      colors: {
        /** 品牌浮光紫 */
        primary: {
          50: 'var(--gd-primary-50)',
          100: 'var(--gd-primary-100)',
          200: 'var(--gd-primary-200)',
          300: 'var(--gd-primary-300)',
          400: 'var(--gd-primary-400)',
          500: 'var(--gd-primary-500)',
          600: 'var(--gd-primary-600)',
          700: 'var(--gd-primary-700)',
          800: 'var(--gd-primary-800)',
          900: 'var(--gd-primary-900)',
        },
        /** 中性色（覆盖 Tailwind 默认 neutral 色阶） */
        neutral: {
          0: 'var(--gd-neutral-0)',
          50: 'var(--gd-neutral-50)',
          100: 'var(--gd-neutral-100)',
          200: 'var(--gd-neutral-200)',
          300: 'var(--gd-neutral-300)',
          400: 'var(--gd-neutral-400)',
          500: 'var(--gd-neutral-500)',
          600: 'var(--gd-neutral-600)',
          700: 'var(--gd-neutral-700)',
          800: 'var(--gd-neutral-800)',
          900: 'var(--gd-neutral-900)',
        },
        /** 表面语义（v2 新增）：新代码用 surface-* 表达"面"，neutral-* 只留文字/图标色 */
        surface: {
          canvas: 'var(--gd-surface-canvas)',
          raised: 'var(--gd-surface-raised)',
          sunken: 'var(--gd-surface-sunken)',
          inset: 'var(--gd-surface-inset)',
          invert: 'var(--gd-surface-invert)',
        },
        /** 暖金辅色家族（v7 纸上放映厅 Pro：成果与数据着色，不作行动色） */
        gold: {
          DEFAULT: 'var(--gd-gold)',
          deep: 'var(--gd-gold-deep)',
          light: 'var(--gd-gold-light)',
          fog: 'var(--gd-gold-fog)',
        },
        /** 发丝线（v2 新增；v4 补 soft 档） */
        hairline: {
          DEFAULT: 'var(--gd-hairline)',
          soft: 'var(--gd-hairline-soft)',
          strong: 'var(--gd-hairline-strong)',
        },
        /** 侧栏（参考图为近中性暖灰，与台面差一档明度） */
        sidebar: {
          DEFAULT: 'var(--gd-sidebar-bg)',
          active: 'var(--gd-sidebar-active-bg)',
        },
        /** 功能色（前景 / 浅底 / 描边 / 浅底上的可读加重版） */
        /** 暗域极光强调 + 极光五义（aurora 原生层）：只做强调，不做大面积铺色 */
        aurora: {
          core: 'var(--gd-aurora-core)',
          halo: 'var(--gd-aurora-halo)',
          mint: 'var(--gd-mint)',
          cyan: 'var(--gd-cyan)',
          violet: 'var(--gd-violet)',
          magenta: 'var(--gd-magenta)',
          amber: 'var(--gd-amber)',
          rose: 'var(--gd-rose)',
        },
        /** 文字三级 + 品牌字（新代码用它，代替 neutral-*） */
        ink: {
          DEFAULT: 'var(--gd-ink)',
          soft: 'var(--gd-ink-2)',
          mute: 'var(--gd-ink-3)',
          brand: 'var(--gd-ink-brand)',
          heading: 'var(--gd-ink-heading)',
        },
        /** 极光表面（玻璃 / 面板 / 下沉 / 抬升） */
        auroraSurface: {
          glass: 'var(--gd-surface-glass)',
          panel: 'var(--gd-panel)',
          raised: 'var(--gd-panel-raised)',
          sunken: 'var(--gd-panel-sunken)',
          canvas: 'var(--gd-canvas)',
        },
        success: {
          DEFAULT: 'var(--gd-success)',
          bg: 'var(--gd-success-bg)',
          border: 'var(--gd-success-border)',
          strong: 'var(--gd-success-strong)',
          bright: 'var(--gd-success-bright)',
        },
        warning: {
          DEFAULT: 'var(--gd-warning)',
          bg: 'var(--gd-warning-bg)',
          'bg-deep': 'var(--gd-warning-bg-deep)',
          border: 'var(--gd-warning-border)',
          strong: 'var(--gd-warning-strong)',
          bright: 'var(--gd-warning-bright)',
        },
        danger: {
          DEFAULT: 'var(--gd-danger)',
          bg: 'var(--gd-danger-bg)',
          border: 'var(--gd-danger-border)',
          strong: 'var(--gd-danger-strong)',
          bright: 'var(--gd-danger-bright)',
        },
        info: {
          DEFAULT: 'var(--gd-info)',
          bg: 'var(--gd-info-bg)',
          border: 'var(--gd-info-border)',
          strong: 'var(--gd-info-strong)',
          bright: 'var(--gd-info-bright)',
        },
        /** 业务五态：生成中 / 待确认 / 已锁定 / 失败 / 审核拦截 */
        'gd-running': {
          DEFAULT: 'var(--gd-running)',
          bg: 'var(--gd-running-bg)',
          border: 'var(--gd-running-border)',
        },
        'gd-pending': {
          DEFAULT: 'var(--gd-pending)',
          bg: 'var(--gd-pending-bg)',
          border: 'var(--gd-pending-border)',
        },
        'gd-locked': {
          DEFAULT: 'var(--gd-locked)',
          bg: 'var(--gd-locked-bg)',
          border: 'var(--gd-locked-border)',
        },
        'gd-failed': {
          DEFAULT: 'var(--gd-failed)',
          bg: 'var(--gd-failed-bg)',
          border: 'var(--gd-failed-border)',
        },
        'gd-blocked': {
          DEFAULT: 'var(--gd-blocked)',
          bg: 'var(--gd-blocked-bg)',
          border: 'var(--gd-blocked-border)',
          strong: 'var(--gd-blocked-strong)',
        },
        /** 分镜画布专用色 */
        canvas: {
          bg: 'var(--gd-canvas-bg)',
          grid: 'var(--gd-canvas-grid)',
          selected: 'var(--gd-shot-selected)',
          connector: 'var(--gd-connector)',
        },
        /** 编辑器 / 画布面板域（v2 新增） */
        panel: {
          DEFAULT: 'var(--gd-panel-bg)',
          rail: 'var(--gd-panel-rail-bg)',
          hairline: 'var(--gd-panel-hairline)',
        },
        /** 覆盖层（弹窗 / 抽屉遮罩） */
        overlay: 'var(--gd-overlay)',
        /** 媒体上的遮罩 / 磨砂层（区别于页面级 overlay） */
        scrim: {
          faint: 'var(--gd-scrim-faint)',
          soft: 'var(--gd-scrim-soft)',
          strong: 'var(--gd-scrim-strong)',
        },
        /** 压在画面上的浅色磨砂底（画布 HUD 提示条） */
        frost: 'var(--gd-frost-light)',
        /** 交互状态层（v4 精修层）：hover / active 压色，替代各视图散写的 bg-neutral-100 / bg-black/5 */
        state: {
          hover: 'var(--gd-state-hover)',
          active: 'var(--gd-state-active)',
        },
        /** 播放器 / 视频预览域 */
        player: {
          bg: 'var(--gd-player-bg)',
          panel: 'var(--gd-player-panel)',
          text: 'var(--gd-player-text)',
          hint: 'var(--gd-player-hint)',
        },
        /** 时间线轨道 */
        timeline: {
          video: 'var(--gd-timeline-video)',
          audio: 'var(--gd-timeline-audio)',
          bgm: 'var(--gd-timeline-bgm)',
          subtitle: 'var(--gd-timeline-subtitle)',
          playhead: 'var(--gd-playhead)',
        },
      },
      fontFamily: {
        sans: ['var(--gd-font-sans)'],
        /** 衬线展示层：仅页面级 masthead / masthead-lg 刊头（方向 B 杂志感），见 tokens.css */
        serif: ['var(--gd-font-serif)'],
        num: ['var(--gd-font-num)'],
        mono: ['var(--gd-font-mono)'],
      },
      /** 字阶：排版即层级（新代码一律使用语义字阶） */
      fontSize: {
        micro: ['11px', { lineHeight: '1.4', fontWeight: '600' }], // 仅数字角标
        caption: ['12px', { lineHeight: '1.5', letterSpacing: '0.01em' }], // 字段标签 / 表头
        kicker: ['12px', { lineHeight: '1.4', letterSpacing: '0.02em', fontWeight: '600' }], // 刊眉（取代裸 section-label）
        lede: ['15px', { lineHeight: '1.7' }], // 导语（刊头下的一句话）
        title: ['16px', { lineHeight: '1.45', fontWeight: '600' }], // 区块 / 弹窗标题
        display: ['24px', { lineHeight: '1.3', letterSpacing: '-0.01em', fontWeight: '600' }], // 页面标题（工具型页）
        'display-lg': ['28px', { lineHeight: '1.25', letterSpacing: '-0.015em', fontWeight: '600' }], // 引导页大标题
        masthead: ['34px', { lineHeight: '1.2', letterSpacing: '-0.02em', fontWeight: '600' }], // 项目域页面刊头
        'masthead-lg': ['40px', { lineHeight: '1.15', letterSpacing: '-0.025em', fontWeight: '600' }], // 首页刊头
      },
      /** 字重收敛为 3 档 */
      fontWeight: {
        normal: '400',
        medium: '500',
        semibold: '600',
      },
      /** 圆角：全部引用令牌（形状一致性锁，数值见 tokens.css，本处不再重复） */
      borderRadius: {
        sm: 'var(--gd-radius-sm)',
        md: 'var(--gd-radius-md)',
        lg: 'var(--gd-radius-lg)',
        xl: 'var(--gd-radius-xl)',
        '2xl': 'var(--gd-radius-2xl)',
      },
      backgroundImage: {
        /** 暗域主按钮 / 进度条渐变（v5，登录页主按钮同源） */
        'grad-primary': 'var(--gd-grad-primary)',
        /** 金色渐变（v7：头像 / 金色填充） */
        'grad-gold': 'var(--gd-grad-gold)',
      },
      boxShadow: {
        'gd-0': 'var(--gd-shadow-0)',
        'gd-1': 'var(--gd-shadow-1)',
        'gd-2': 'var(--gd-shadow-2)',
        'gd-3': 'var(--gd-shadow-3)',
        'gd-4': 'var(--gd-shadow-4)',
        /** 紫 bloom（v5）：仅暗场域使用，暖域禁用 */
        'gd-bloom': 'var(--gd-shadow-bloom)',
        /** aurora 原生档（新代码用这三档，暗域分层靠内高光 + 光晕） */
        card: 'var(--gd-elev-1)',
        cardHover: 'var(--gd-elev-2)',
        overlay: 'var(--gd-elev-overlay)',
        brand: 'var(--gd-glow-brand)',
      },
      maxWidth: {
        /** 超宽屏内容区收敛宽度 */
        content: 'var(--gd-content-max)',
        /** 版心宽度档（v4 精修层） */
        'content-xs': 'var(--gd-content-xs)',
        'content-sm': 'var(--gd-content-sm)',
        'content-md': 'var(--gd-content-md)',
        'content-lg': 'var(--gd-content-lg)',
        /** 版心列宽（正文可读行长，≈68ch） */
        measure: '68ch',
      },
      /** 面板域尺度（v2 新增：编辑器轨道宽 / 工具条高 / 底栏高） */
      width: {
        'rail-left': 'var(--gd-rail-left)',
        'rail-right': 'var(--gd-rail-right)',
        'content-sm': 'var(--gd-content-sm)',
        'sidebar': 'var(--gd-sidebar-w)',
      },
      height: {
        'toolbar': 'var(--gd-toolbar-h)',
        'footer-bar': 'var(--gd-footer-bar-h)',
        'bento-row': 'var(--gd-bento-row)',
      },
      /** 动效：全部引用令牌 */
      transitionTimingFunction: {
        'gd-out': 'var(--gd-ease-out)',
        'gd-standard': 'var(--gd-ease-standard)',
        'gd-in-out': 'var(--gd-ease-in-out)',
      },
      transitionDuration: {
        'gd-fast': 'var(--gd-duration-fast)',
        'gd-normal': 'var(--gd-duration-normal)',
        'gd-slow': 'var(--gd-duration-slow)',
      },
      /** z-index 层级（v4 精修层）：视图内散布的 z-10/z-20/z-[100] 收敛为具名层级 */
      zIndex: {
        raised: 'var(--gd-z-raised)',
        sticky: 'var(--gd-z-sticky)',
        drawer: 'var(--gd-z-drawer)',
        modal: 'var(--gd-z-modal)',
        toast: 'var(--gd-z-toast)',
      },
    },
  },
  plugins: [],
} satisfies Config

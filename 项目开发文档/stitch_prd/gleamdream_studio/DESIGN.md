---
name: GleamDream Studio
colors:
  surface: '#fdf8ff'
  surface-dim: '#ded6f3'
  surface-bright: '#fdf8ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f7f1ff'
  surface-container: '#f2ebff'
  surface-container-high: '#ece4ff'
  surface-container-highest: '#e6defc'
  on-surface: '#1d192e'
  on-surface-variant: '#474554'
  inverse-surface: '#322d44'
  inverse-on-surface: '#f4eeff'
  outline: '#787586'
  outline-variant: '#c9c4d7'
  surface-tint: '#5b46d3'
  primary: '#543ecc'
  on-primary: '#ffffff'
  primary-container: '#6d5ae6'
  on-primary-container: '#f9f4ff'
  inverse-primary: '#c7bfff'
  secondary: '#5e39e0'
  on-secondary: '#ffffff'
  secondary-container: '#7757fa'
  on-secondary-container: '#fffbff'
  tertiary: '#6044ae'
  on-tertiary: '#ffffff'
  tertiary-container: '#795ec9'
  on-tertiary-container: '#faf3ff'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#e5deff'
  primary-fixed-dim: '#c7bfff'
  on-primary-fixed: '#180065'
  on-primary-fixed-variant: '#4228ba'
  secondary-fixed: '#e6deff'
  secondary-fixed-dim: '#cabeff'
  on-secondary-fixed: '#1c0062'
  on-secondary-fixed-variant: '#4816cb'
  tertiary-fixed: '#e8ddff'
  tertiary-fixed-dim: '#cebdff'
  on-tertiary-fixed: '#21005e'
  on-tertiary-fixed-variant: '#4f319c'
  background: '#fdf8ff'
  on-background: '#1d192e'
  surface-variant: '#e6defc'
typography:
  display-lg:
    fontFamily: Noto Serif
    fontSize: 48px
    fontWeight: '600'
    lineHeight: 58px
    letterSpacing: -0.02em
  display-lg-mobile:
    fontFamily: Noto Serif
    fontSize: 32px
    fontWeight: '600'
    lineHeight: 40px
    letterSpacing: -0.01em
  headline-xl:
    fontFamily: Noto Serif
    fontSize: 36px
    fontWeight: '600'
    lineHeight: 44px
    letterSpacing: -0.015em
  headline-xl-mobile:
    fontFamily: Noto Serif
    fontSize: 26px
    fontWeight: '600'
    lineHeight: 34px
    letterSpacing: -0.01em
  headline-lg:
    fontFamily: Noto Serif
    fontSize: 28px
    fontWeight: '500'
    lineHeight: 36px
  headline-md:
    fontFamily: Noto Serif
    fontSize: 22px
    fontWeight: '500'
    lineHeight: 30px
  headline-sm:
    fontFamily: Noto Serif
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 26px
  body-xl:
    fontFamily: Plus Jakarta Sans
    fontSize: 18px
    fontWeight: '400'
    lineHeight: 28px
  body-lg:
    fontFamily: Plus Jakarta Sans
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 18px
  code-timecode:
    fontFamily: Space Grotesk
    fontSize: 13px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.06em
  label-md:
    fontFamily: Plus Jakarta Sans
    fontSize: 13px
    fontWeight: '500'
    lineHeight: 16px
  label-sm:
    fontFamily: Plus Jakarta Sans
    fontSize: 11px
    fontWeight: '600'
    lineHeight: 14px
    letterSpacing: 0.04em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1rem
  gutter-lg: 1.5rem
  margin: 1.5rem
  margin-mobile: 1rem
  margin-desktop: 2.5rem
  space-xs: 0.25rem
  space-sm: 0.375rem
  space-md: 0.75rem
  space-lg: 1.25rem
  space-xl: 2rem
---

## Brand & Style

This design system embodies the narrative paradigm of "Daylight Studio & Nighttime Dream Window" (日夜交割 · 梦境窗口). It merges the tactile, scholarly warmth of traditional cinematic scriptwriting with the deep, luminous mystery of generative artificial intelligence.

### Target Audience & Personality
Built for AI short-drama directors, narrative screenwriters, prompt engineers, and visual storytellers. The interface projects:
- **Cinematic Craftsmanship**: Serene, deliberate, and archival—grounded in organic textures reminiscent of warm manuscript parchment.
- **Hypnotic Synthesis**: Expansive, nocturnal, and radiant—evoking the infinite creative potential of high-compute diffusion and video generation models.
- **Editorial Authority**: Clean, structured, and legible under prolonged creative flow.

### Design Movement: Luminous Skeuomorphism & Optical Split-Level
The aesthetic combines two distinct physical planes:
1. **The Daylight Workbench (日间工作台)**: A tactile, high-clarity environment inspired by minimalist editorial print, warm washi paper, and archival desks.
2. **The Nocturnal Portal (夜窗容器)**: A focused, deep-space dark console housing video rendering timelines, prompt generation canvasses, and generation previews.
3. **The Luminous Fog Junction (柔光雾带)**: A physical diffuse gradient seam bridging the warm paper world and the generative abyss.

## Colors

The palette operates via a deliberate dual-domain ecosystem: the Daylight Workbench (paper tones) and the Night Window (astral depths), mediated by an iridescent violet spectrum.

### Daylight Workbench (暖纸工作区)
- **Base Canvas (`#f7f3ec`)**: Warm unbleached paper surface. Eliminates eye strain during prolonged script generation.
- **Surface Elevation (`#ffffff`)**: Pure white card stocks floating over the parchment.
- **Hairline Dividers (`rgba(0, 0, 0, 0.06)`)**: Ultra-fine spatial boundaries for cards, menus, and inspector panels.
- **Sunlit Ambient Pool**: Linear ambient gradient applied to the canvas header: `linear-gradient(180deg, rgba(254, 243, 199, 0.45) 0%, rgba(247, 243, 236, 0) 100%)`.

### Nocturnal Portal (.gd-night-window)
- **Void Core (`#0a0a12`)**: Ultra-deep space blue-black for video viewports, generation canvases, and keyframe sequencers.
- **Window Rim Glow**: Dual aura combining core violet and mist: `box-shadow: 0 0 24px rgba(124, 92, 255, 0.18), 0 0 48px rgba(196, 190, 226, 0.12)`.
- **Inset Shadow**: `inset 0 1px 1px 0 rgba(255, 255, 255, 0.08), inset 0 -1px 2px 0 rgba(0, 0, 0, 0.6)`.

### Aurora Luminescence & Chromatic Accents
- **Warm Violet Core (`#6d5ae6`)**: Solid primary color reserved for primary call-to-actions, focused states, and selected storyboard frames within daylight workspaces.
- **Generative Violet (`#7c5cff`)**: Core kinetic violet for AI active generation, processing spinners, and glowing handles.
- **Lavender Mist (`#a78bfa`)**: Secondary aura and gradient terminus.
- **Portal Vector Gradient**: `135deg, #8b5cf6 0%, #a78bfa 100%` for nocturnal generation buttons and status badges.
- **Fog Junction Band (.gd-fog-junction)**: 24px–28px linear gradient transition:
  - Horizontal: `linear-gradient(180deg, rgba(247, 243, 236, 0.95) 0%, rgba(124, 92, 255, 0.08) 50%, rgba(10, 10, 18, 1) 100%)`.
  - Vertical: `linear-gradient(90deg, rgba(247, 243, 236, 0.95) 0%, rgba(124, 92, 255, 0.08) 50%, rgba(10, 10, 18, 1) 100%)`.

## Typography

The typographic hierarchy implements a three-family triage:

1. **Title & Masthead (Noto Serif / Noto Serif SC fallback)**: Imparts literary gravity, classic script publication aesthetic, and dramatic weight to project titles, episode chapter headers, and modal proclamations.
2. **Workstation Body (Plus Jakarta Sans / PingFang SC fallback)**: Provides optical clarity, high legibility in script breakdown tables, prompt formulation textareas, and dialogue editors.
3. **Timeline & Metric Tokens (Space Grotesk / DIN alternate fallback)**: Handles frame rates (24fps/60fps), timecodes (`00:02:14:18`), seed values, generation latency, and video resolution ratios (9:16 / 16:9).

## Layout & Spacing

The studio operates on a responsive dual-canvas layout:

- **Desktop Layout (≥ 1280px)**: A split asymmetric view:
  - **Left / Daylight Wing (40% - 48%)**: Script outlining, character bible, prompt engineering sheets, and dialogue tracks laid over `#f7f3ec`.
  - **The Fog Boundary**: A fixed 28px vertical junction strip (`.gd-fog-junction`) seamlessly blending into the right zone.
  - **Right / Nocturnal Stage (52% - 60%)**: Deep `#0a0a12` console hosting video generation viewport, shot sequence preview, timeline track, and multimodal camera controls.
- **Tablet Layout (768px - 1279px)**: Vertically stacked; daylight script breakdown on top, night viewport pinned below with 24px horizontal fog divide.
- **Mobile Handheld (< 768px)**: Segmented tab switch ("Script Lab" / "Render Stage") retaining paper/night thematic colors according to active context.
- **Spacing Rhythm**: 4px baseline sub-grid. Internal component padding adheres strictly to `space-xs` (4px), `space-sm` (6px), `space-md` (12px), `space-lg` (20px), and `space-xl` (32px).

## Elevation & Depth

Spatial layering distinguishes tactile paper tools from digital generation chambers:

### Daylight Studio Elevation
- **Level 0 (Desk Ground)**: `#f7f3ec` unbleached paper surface.
- **Level 1 (Script & Prop Cards)**: Pure `#ffffff` surface, border `1px solid rgba(0, 0, 0, 0.06)`, shadow `0 2px 8px rgba(35, 23, 10, 0.03), 0 1px 2px rgba(0, 0, 0, 0.02)`.
- **Level 2 (Hover & Floating Panels)**: `#ffffff`, border `1px solid rgba(0, 0, 0, 0.08)`, shadow `0 12px 28px rgba(50, 30, 20, 0.07), 0 4px 8px rgba(0, 0, 0, 0.03)`.

### Nocturnal Window Elevation (.gd-night-window)
- **Base Sink**: Background `#0a0a12`, border `1px solid rgba(124, 92, 255, 0.22)`.
- **Volumetric Aura**: `box-shadow: 0 0 24px rgba(124, 92, 255, 0.18), 0 0 48px rgba(196, 190, 226, 0.12)`.
- **Inner Recess (Screen Depress)**: `box-shadow: inset 0 2px 8px rgba(0, 0, 0, 0.7), inset 0 0 20px rgba(10, 10, 18, 0.9)`.
- **Active AI Generation State**: Aura transitions smoothly to `box-shadow: 0 0 32px rgba(124, 92, 255, 0.35), 0 0 64px rgba(167, 139, 250, 0.2)`.

## Shapes

The geometric architecture is intentional and disciplined across the scale:

- **Interactive Controls (r6 / 6px)**: Buttons, prompt input fields, dropdown triggers, scrubber handles, timecode badges, and segmented toggles. Crisp, professional, avoiding overly playful bubbly forms.
- **Standard Cards & Storyboard Nodes (r16 / 16px)**: Script card containers, prompt templates, episode reels, and shot definition cards.
- **Dialogs, Drawers & Portals (r18 / 18px)**: AI setting modals, character loRA injectors, and generation popups.
- **Tags, Pills & Status Tokens (`rounded-full` / 9999px)**: Lens parameters (e.g., `35mm`, `Cinematic`, `Wide Shot`), genre badges, and generation status markers.

## Components

### Buttons
- **Primary Studio Button (Daylight)**: Background `#6d5ae6`, text `#ffffff`, border-radius `6px`, padding `8px 18px`. Hover: `#5b48d6`, shadow `0 2px 10px rgba(109, 90, 230, 0.25)`. Active: scale `0.98`.
- **Generative Portal Action (Night Window)**: Background `linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%)`, text `#0a0a12` (bold 600 weight), border-radius `6px`, border `1px solid rgba(255, 255, 255, 0.2)`. Hover: aura expands with `0 0 16px rgba(167, 139, 250, 0.5)`.
- **Ghost/Tertiary**: Daylight: transparent with `#1a162b` text, hover: `rgba(0, 0, 0, 0.04)`. Night: transparent with `#c4bee2` text, hover: `rgba(255, 255, 255, 0.06)`.

### Chips & Badges
- **Descriptor Pills**: Fully rounded (`border-radius: 9999px`), padding `3px 10px`.
  - Daylight: Background `#ffffff`, border `1px solid rgba(0, 0, 0, 0.08)`, text `#4b4458`.
  - Night: Background `rgba(124, 92, 255, 0.12)`, border `1px solid rgba(124, 92, 255, 0.3)`, text `#dcd6fa`.

### Storyboard Cards
- **Card Surface**: Background `#ffffff`, radius `16px`, border `1px solid rgba(0, 0, 0, 0.06)`.
- **Selected State**: Border `2px solid #6d5ae6`, shadow `0 4px 16px rgba(109, 90, 230, 0.12)`.
- **Internal Shot Header**: Serif headline pairing (`Noto Serif`, 16px) with right-aligned timecode pill (`Space Grotesk`, 11px).

### Input Fields & Textareas
- **Script & Prompt Textareas**:
  - Daylight: Background `#ffffff`, border `1px solid rgba(0, 0, 0, 0.1)`, radius `6px`, text `#1a162b`. Focus: border `1px solid #6d5ae6`, ring `2px rgba(109, 90, 230, 0.15)`.
  - Night (Prompt Console): Background `rgba(255, 255, 255, 0.04)`, border `1px solid rgba(255, 255, 255, 0.1)`, radius `6px`, text `#f7f3ec`. Focus: border `1px solid #a78bfa`, glow `0 0 12px rgba(167, 139, 250, 0.25)`.

### Selection Controls (Checkboxes & Radios)
- **Checkboxes**: Radius `4px`, size `16px`. Active: fill `#6d5ae6` with white SVG check icon.
- **Radio Buttons**: Radius `9999px`, size `16px`. Active: `#6d5ae6` outer ring with centered 6px dot.

### Custom Tool: Night Window Generation Canvas (`.gd-night-window`)
- Dedicated container with `#0a0a12` backdrop, `18px` inner container radius, dual violet outer glow, and frame-accurate timeline scrubber at base.
- Integrated `.gd-fog-junction` transition element spanning the docking edge to guarantee zero harsh UI cutoffs between desk and generative player.
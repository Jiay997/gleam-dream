/**
 * 文件级注释：剧本场头文案工具。
 *
 * 后端剧本树中的场头（`heading`）可能直接写入 `ext` / `int` / `night` 等
 * 英文枚举；中文界面需统一翻译为「外景 / 内景 / 夜」等业务可读文案。
 * 该逻辑此前散落在剧本审核页，现抽为共享工具，编辑器与审核页共用，
 * 避免同一数据在不同页面出现中英混杂（修复 E2E 测试报告问题 1）。
 */

/** 内外景英文枚举 → 中文标签 */
const INT_EXT_LABELS: Record<string, string> = {
  int: '内景',
  ext: '外景',
  int_ext: '内外景',
  'int./ext.': '内外景',
  'int/ext': '内外景',
}

/** 时段英文枚举 → 中文标签 */
const TIME_OF_DAY_LABELS: Record<string, string> = {
  day: '日',
  night: '夜',
  dawn: '晨',
  dusk: '昏',
  morning: '晨',
  evening: '夜',
  noon: '午',
}

/**
 * 将单个场头片段中的英文枚举翻译为中文。
 *
 * @param segment 场头片段（如 `ext`、`night`、`骑手站门口`）
 * @returns 中文标签；非枚举片段原样返回
 */
function translateSegment(segment: string): string {
  const raw = segment.trim()
  if (!raw) return ''
  const key = raw.toLowerCase()
  return INT_EXT_LABELS[key] ?? TIME_OF_DAY_LABELS[key] ?? raw
}

/**
 * 生成场次展示标题：优先使用后端 heading（先做英文枚举本地化），
 * 否则由「内外景 · 地点 · 时段」拼装并翻译。
 *
 * @param scene 场次节点（宽松结构，兼容 heading / int_ext / location / time_of_day）
 * @param fallback 无法拼装时的兜底文案
 * @returns 展示用场次标题
 */
export function formatSceneHeading(
  scene: {
    /** 后端已拼好的场头（可能含英文枚举） */
    heading?: string | null
    /** 内外景枚举（int / ext / int_ext） */
    int_ext?: string | null
    /** 地点名 */
    location?: string | null
    /** 时段枚举（day / night / dawn ...） */
    time_of_day?: string | null
  },
  fallback = '未命名场景',
): string {
  const heading = (scene.heading ?? '').trim()
  if (heading) {
    const localized = heading
      .split(/\s*(?:·|—|–)\s*/)
      .map(translateSegment)
      .filter(Boolean)
      .join(' · ')
    return localized || heading
  }
  const parts = [
    translateSegment(scene.int_ext ?? ''),
    (scene.location ?? '').trim(),
    translateSegment(scene.time_of_day ?? ''),
  ].filter(Boolean)
  return parts.length > 0 ? parts.join(' · ') : fallback
}

/**
 * 封面档位取色（装饰性封面族的唯一取色入口）。
 *
 * 为什么要有这个文件：.gd-cv-* 渐变封面族散落在项目墙、资产墙、模板卡上，
 * 如果每处各自 `id % 6`，同一种「哪些色相能当装饰底」的判断就会被复制四五遍，
 * 一旦要调整（例如让薄荷退出装饰池），就得全站追着改。
 *
 * 取色纪律：
 * 1. **薄荷（.gd-cv-4）不进装饰池**。aurora 的五色五义里薄荷独占「成功 / 通过 / 正向」，
 *    一旦它被随机抽成某个项目的封面底色，同屏出现「绿封面 + 绿 QC 标记」时，
 *    「通过」这个结论就退化成一块背景色，信号重量被稀释。
 * 2. **需要按实体固定**（同一项目永远同一款封面，刷新不变、不闪色）。
 *    所以用确定性哈希而不是 Math.random / index。
 * 3. **id 非法时回退第一档**，避免 `gd-cv-NaN` 这种空类名让封面变成透明块。
 */

/** 装饰池：星紫 / 极青 / 洋红 / 冷紫 / 琥珀（刻意不含薄荷 .gd-cv-4） */
export const COVER_TONES = ['gd-cv-1', 'gd-cv-2', 'gd-cv-3', 'gd-cv-5', 'gd-cv-6'] as const

/**
 * 按实体 ID 确定性取一款封面底色。
 * @param id 实体 ID（项目 / 资产 / 模板等），数字或字符串均可
 * @returns 形如 `gd-cv-3` 的类名，可安全直接绑到 :class
 */
export function coverTone(id: string | number): string {
  const key = String(id ?? '')
  if (!key) return COVER_TONES[0]

  // 31 进制滚动哈希：把 "1" / "12" / "project-7" 这类不同形态的 ID 均匀摊到池子上
  let hash = 0
  for (let i = 0; i < key.length; i += 1) {
    hash = (hash * 31 + key.charCodeAt(i)) % 997
  }
  return COVER_TONES[hash % COVER_TONES.length] ?? COVER_TONES[0]
}

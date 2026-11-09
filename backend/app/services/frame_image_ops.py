"""分镜整幅画面的本地图像算子（Pillow + NumPy，0 积分）。

背景与目标：原合成器只用 ffmpeg ``colorkey`` 抠白底，遇到影棚灰米色渐变
背景会留下白边、半透明灰块与面部花斑，人物像"纸片"贴在场景上。本模块
用可复现的纯本地算法把资产图变成可用的电影感静帧：

1. **软抠像**：按逐行估计的背景色计算颜色距离，得到软 alpha；再做开运算
   去噪、闭运算补内部孔洞、连通域过滤去孤立残块、边缘反混合去白边；
2. **置入规则**：人物按"紧致包围盒高度"缩放并接地，附接触阴影与环境色，
   避免漂浮与贴纸感；
3. **场景处理**：裁黑边、浅景深、局部规避区（场景内多余人物）虚化压暗、
   统一暖色调、暗角与细颗粒；
4. **道具镜头**：道具特写直接作为整幅画面（cover），不再叠到人物脸上。

所有函数不访问数据库、不扣积分；任何一步失败都回退到调用方提供的
ffmpeg 实现，保证链路不中断。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

logger = logging.getLogger("gleamdream.services.frame_image_ops")

#: 输出画布（9:16 竖屏）
DEFAULT_SIZE: tuple[int, int] = (720, 1280)

#: 背景带占边长的比例（取左右边缘带估计逐行背景色）
_BG_BAND = 0.055
#: 前景判定阈值（RGB 欧氏距离）：低于该值视为背景/接触阴影。
_ALPHA_THRESHOLD = 26.0
#: 羽化半径（像素）
_ALPHA_FEATHER = 1.1
#: 外部背景连通域检测的降采样边长（越大越准、越慢）
_EXTERIOR_GRID = 384

#: 暗角强度
_VIGNETTE = 0.34


def detect_panel_artifact(
    image: Image.Image,
    *,
    ignore_transparent: bool = True,
    black_ratio: float = 0.02,
    white_ratio: float = 0.15,
) -> list[str]:
    """检测整幅图/图层中的“贴图感”伪影（黑块、白底卡片）。

    规则（纯本地、0 成本）：
    - 把图像缩放到 128×128，按 8×8 分块统计均值与标准差；
    - 均值 < 25 且标准差 < 6 的块视为“均匀黑块”，占比 ≥ ``black_ratio``
      判定 ``black_block``（对应 S5 的黑块遮脸）；
    - 均值 > 240 且标准差 < 6 的块视为“均匀白块”，占比 ≥ ``white_ratio``
      判定 ``white_panel``（对应 S7 的白底人像贴图）；
    - ``ignore_transparent=True`` 时只统计 alpha>10 的像素，避免抠像后的
      透明背景被误判为黑块。

    Args:
        image: 待检测 PIL 图像（RGB 或 RGBA）。
        ignore_transparent: 是否忽略透明像素（图层检测用 True，成片用 False）。
        black_ratio: 均匀黑块占比阈值。
        white_ratio: 均匀白块占比阈值。

    Returns:
        命中的问题列表（如 ``["black_block"]``）；无问题时为空列表。

    Raises:
        无：检测异常返回空列表，不阻断合成主流程。
    """
    try:
        rgba = image.convert("RGBA").resize((128, 128))
        arr = np.asarray(rgba, dtype="float32")
        alpha = arr[:, :, 3]
        rgb = arr[:, :, :3]
        # 按 8×8 分块（16×16=256 块）
        blocks_rgb = rgb.reshape(16, 8, 16, 8, 3).transpose(0, 2, 1, 3, 4).reshape(-1, 64, 3)
        blocks_alpha = alpha.reshape(16, 8, 16, 8).transpose(0, 2, 1, 3).reshape(-1, 64)
        means = blocks_rgb.mean(axis=(1, 2))
        stds = blocks_rgb.std(axis=(1, 2))
        valid = (blocks_alpha > 10).mean(axis=1) > 0.5 if ignore_transparent else np.ones_like(means, dtype=bool)
        total = max(1, int(valid.sum()))
        black = (means < 25) & (stds < 2) & valid
        white = (means > 240) & (stds < 2) & valid
        issues: list[str] = []
        if float(black.sum()) / total >= black_ratio:
            issues.append("black_block")
        if float(white.sum()) / total >= white_ratio:
            issues.append("white_panel")
        return issues
    except Exception:  # noqa: BLE001 - 检测失败不阻断合成
        logger.debug("panel artifact 检测跳过", exc_info=True)
        return []



@dataclass
class Cutout:
    """一次人像抠像的结果。

    Attributes:
        image: RGBA 图像（已裁到 alpha 紧致包围盒）。
        coverage: 前景 alpha 覆盖率（0~1，用于降级判定）。
        bust: 素材本身在底边被画幅截断（半身/腰上定妆照）。这类素材
            合成时底部应贴屏幕下沿，而不是按安全区悬空接地，否则腰线
            处会出现一条悬空的平切硬边。
        top_gap_ratio: 原图顶部到首个不透明像素的距离占原图高度的比例。
            正常定妆照（头顶接近画幅上沿）应接近 0；若明显偏大，说明
            抠像把头部/上半身误判成背景删掉了，首帧会出现"无头手臂"。
        bbox_height_ratio: alpha 包围盒高度占原图高度的比例，用于识别
            只抠出局部（手臂/腿部）的残缺结果。
    """

    image: Image.Image
    coverage: float
    bust: bool = False
    top_gap_ratio: float = 0.0
    bbox_height_ratio: float = 1.0


def _pil_filters() -> tuple[Any, Any, Any]:
    """返回兼容不同 Pillow 版本的滤镜常量。

    Returns:
        ``(重采样, 最大值滤波, 最小值滤波)`` 三个可调用对象。

    Raises:
        无。
    """
    resample = getattr(Image, "Resampling", Image).LANCZOS
    max_filter = getattr(ImageFilter, "MaxFilter", None)
    min_filter = getattr(ImageFilter, "MinFilter", None)
    return resample, max_filter, min_filter


def _morph(image: Image.Image, *, mode: str, size: int) -> Image.Image:
    """对灰度图做形态学运算（依赖 Pillow 的 C 实现，避免 Python 逐像素）。

    Args:
        image: ``L`` 模式灰度图。
        mode: ``open``（先腐蚀后膨胀）或 ``close``（先膨胀后腐蚀）。
        size: 结构元边长（奇数，≥3）。

    Returns:
        形态学处理后的灰度图。

    Raises:
        无：Pillow 老版本缺少 MaxFilter/MinFilter 时原样返回。
    """
    _, max_filter, min_filter = _pil_filters()
    if max_filter is None or min_filter is None:
        return image
    k = max(3, int(size) | 1)
    if mode == "open":
        return image.filter(min_filter(k)).filter(max_filter(k))
    return image.filter(max_filter(k)).filter(min_filter(k))


def _largest_components(alpha: Image.Image, *, keep_ratio: float = 0.06) -> Image.Image:
    """只保留面积足够大的前景连通域（去掉背景残块与孤立阴影）。

    在 96×96 的降采样图上做四邻域标记（像素量极少，纯 Python 可接受），
    再把保留区域的掩码最近邻放大回原尺寸，与原 alpha 相乘。

    Args:
        alpha: 原尺寸的 ``L`` 模式 alpha。
        keep_ratio: 保留连通域的最小面积占比（相对整图）。

    Returns:
        过滤后的 alpha 图。

    Raises:
        无。
    """
    small = alpha.resize((96, 96), Image.Resampling.BOX)
    grid = np.asarray(small, dtype=np.float32) / 255.0
    mask = grid > 0.35
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    areas: dict[int, int] = {}
    current = 0
    for y in range(height):
        for x in range(width):
            if not mask[y, x] or labels[y, x]:
                continue
            current += 1
            stack = [(y, x)]
            labels[y, x] = current
            count = 0
            while stack:
                cy, cx = stack.pop()
                count += 1
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and labels[ny, nx] == 0:
                        labels[ny, nx] = current
                        stack.append((ny, nx))
            areas[current] = count
    if not areas:
        return alpha
    threshold = max(4, int(96 * 96 * keep_ratio))
    keep_ids = {label for label, area in areas.items() if area >= threshold}
    if not keep_ids:
        keep_ids = {max(areas, key=areas.get)}
    keep_mask = np.isin(labels, list(keep_ids)).astype(np.float32)
    keep_image = Image.fromarray((keep_mask * 255).astype(np.uint8), mode="L").resize(
        alpha.size, Image.Resampling.NEAREST
    )
    keep_image = keep_image.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    return Image.fromarray(
        (np.asarray(alpha, dtype=np.float32) * np.asarray(keep_image, dtype=np.float32) / 255.0)
        .clip(0, 255)
        .astype(np.uint8),
        mode="L",
    )


def _exterior_mask(foreground: np.ndarray, size: int = _EXTERIOR_GRID) -> np.ndarray:
    """标记可从图片边缘到达的背景区域（外部背景）。

    人像与背景颜色接近时（皮肤、白 T 恤），按颜色阈值会产生"人物内部
    透明孔洞"，合成时背景会透过人物形成斑块。正确做法是：从四边向内
    洪泛，能到达的是外部背景，到达不了且被人物包住的就是"人物内部"，
    应强制填成不透明。

    Args:
        foreground: ``(H, W)`` 二值前景（True=人物）。
        size: 降采样边长（像素数越少越快）。

    Returns:
        ``(size, size)`` 的布尔图，True 表示外部背景。
    """
    from collections import deque

    # 先开运算去掉背景纹理/阴影形成的小块"假前景"，否则洪泛无法穿过这些
    # 岛，会把墙面误判为人物内部（表现为人物周围一圈米色锯齿）。
    mask_image = Image.fromarray((foreground * 255).astype(np.uint8), mode="L")
    mask_image = _morph(mask_image, mode="open", size=9)
    small = np.asarray(
        mask_image.resize((size, size), Image.Resampling.BOX), dtype=np.float32
    )
    background = small < 127
    exterior = np.zeros_like(background, dtype=bool)
    queue: deque[tuple[int, int]] = deque()

    def _seed(y: int, x: int) -> None:
        """把可到达的背景像素加入队列。

        Args:
            y: 行号。
            x: 列号。

        Returns:
            无。
        """
        if background[y, x] and not exterior[y, x]:
            exterior[y, x] = True
            queue.append((y, x))

    for y in range(size):
        _seed(y, 0)
        _seed(y, size - 1)
    for x in range(size):
        _seed(0, x)
        _seed(size - 1, x)
    while queue:
        y, x = queue.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < size and 0 <= nx < size:
                if background[ny, nx] and not exterior[ny, nx]:
                    exterior[ny, nx] = True
                    queue.append((ny, nx))
    return exterior


def _unpremultiply(array: np.ndarray, background: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """对半透明边缘做反混合，去掉白边/灰边。

    观测颜色 ``C = a*F + (1-a)*B``，反解前景 ``F = (C - (1-a)*B) / a``。
    分母下限取 0.62：羽化边缘的 a 可能只有 0.2，若按真实 a 反解会把
    背景色放大 5 倍（表现为一圈白色锯齿），用 0.62 可保留自然边缘。

    Args:
        array: ``(H, W, 3)`` 的 RGB 浮点数组。
        background: ``(H, W, 3)`` 的逐行/逐列背景色。
        alpha: ``(H, W)`` 的 0~1 alpha。

    Returns:
        反混合后的 RGB 数组（0~255 浮点）。

    Raises:
        无。
    """
    safe_alpha = np.clip(alpha, 0.62, 1.0)[:, :, None]
    unmixed = (array - (1.0 - safe_alpha) * background) / safe_alpha
    mixed = np.where(alpha[:, :, None] > 0.06, unmixed, array)
    return np.clip(mixed, 0, 255)


def cutout_subject(path: Path) -> Cutout:
    """把影棚背景人像抠成带干净边缘的 RGBA 图。

    Args:
        path: 资产图片路径（PNG/JPEG）。

    Returns:
        抠像结果（紧致包围盒 + 覆盖率）。

    Raises:
        RuntimeError: 图片无法解码或尺寸非法。
    """
    try:
        source = Image.open(path).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 素材损坏时由调用方降级
        raise RuntimeError(f"抠像失败，无法解码 {path}: {exc}") from exc
    width, height = source.size
    if width < 32 or height < 32:
        raise RuntimeError(f"抠像失败，图片尺寸过小 {source.size}")

    array = np.asarray(source, dtype=np.float32)
    band = max(2, int(width * _BG_BAND))
    # 左右分别估计逐行背景，再按列线性插值：影棚背景左右亮度差可达 60，
    # 只取左右中值会让"左墙/右墙"都偏离模型，抠像大面积误判为前景。
    left = np.median(array[:, :band], axis=1)  # (H, 3)
    right = np.median(array[:, -band:], axis=1)  # (H, 3)
    kernel = max(3, height // 90) | 1
    pad = kernel // 2
    kernel_vec = np.ones(kernel) / kernel

    def _smooth(channel: np.ndarray) -> np.ndarray:
        """按行平滑背景色，抑制货架/门框等边缘干扰。

        Args:
            channel: ``(H,)`` 单通道背景估计。

        Returns:
            平滑后的 ``(H,)``。
        """
        padded = np.pad(channel, (pad, pad), mode="edge")
        return np.convolve(padded, kernel_vec, mode="valid")

    left = np.stack([_smooth(left[:, i]) for i in range(3)], axis=1)
    right = np.stack([_smooth(right[:, i]) for i in range(3)], axis=1)
    ramp = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :, None]
    background = left[:, None, :] * (1.0 - ramp) + right[:, None, :] * ramp  # (H, W, 3)
    distance = np.linalg.norm(array - background, axis=2)
    # 旧版软阈值会让皮肤/白 T 变成半透明（脸上斑块），闭运算填洞又会把
    # 手臂缝隙里的背景色填成实心色块；这里改用"外部背景洪泛"求人物掩码。
    # 用"外部背景洪泛"直接得到人物掩码：能从图片四边到达的背景一律透明，
    # 到不了的封闭区域（皮肤/白 T 等与背景相近的孔洞）一律不透明。
    # 这一步同时解决"背景纹理残留"和"人物内部透背景"两类穿帮。

    def _build_alpha(threshold: float) -> np.ndarray:
        """按给定色差阈值构建人物 alpha。

        2026-11-03 QA 修复（浅色衣吃掉躯干）：米白针织开衫与米白影棚
        背景色差只有 20~35，用 46 的阈值会把整件上衣连同躯干判成背景，
        首帧只剩裙子+鞋（看起来"无头无躯干"）。把它抽成函数后可按更低
        阈值重试，找回低对比度衣物。

        Args:
            threshold: 前景/背景色差阈值（越小越容易保留浅色衣物）。

        Returns:
            ``(H, W)`` 浮点 alpha（0~1，已做开运算/连通域/羽化）。
        """
        exterior_small = _exterior_mask(distance > max(6.0, threshold))
        exterior = Image.fromarray(
            (exterior_small * 255).astype(np.uint8), mode="L"
        ).resize((width, height), Image.Resampling.NEAREST)
        # 轻微膨胀外部背景，确保轮廓外 1px 的背景描边也被清掉
        exterior = exterior.filter(ImageFilter.MaxFilter(7))
        alpha = 1.0 - np.asarray(exterior, dtype=np.float32) / 255.0
        # 底部 18% 二次清除：脚边/裙摆附近的大片浅色地面会被洪泛判为"人物
        # 内部"（与人物相连，无法从边界到达），表现为人物脚下拖一块米色水渍。
        # 该区域只保留与背景色差足够大的像素（深色裤/鞋/围裙），浅色地面清掉。
        bottom_start = int(height * 0.82)
        if bottom_start < height:
            keep = np.clip((distance[bottom_start:] - 72.0) / 10.0, 0.0, 1.0)
            alpha[bottom_start:] *= keep
            # 腿部列约束：素材底部常带一大片与脚相连的浅色地面（洪泛进不来），
            # 其列宽明显大于腿/裙摆。用上方 62%~78% 的 alpha 列分布作为"人物
            # 真实占地宽度"，把底部超出该宽度 6% 的区域清掉。
            upper_start = int(height * 0.62)
            upper_end = max(upper_start + 1, int(height * 0.78))
            columns = (alpha[upper_start:upper_end].mean(axis=0) > 0.30).astype(np.uint8)
            span = max(5, int(width * 0.06) | 1)
            column_image = Image.fromarray(
                columns.reshape(1, width) * 255, mode="L"
            ).filter(ImageFilter.MaxFilter(span))
            column_mask = np.asarray(column_image, dtype=np.float32)[0] / 255.0
            alpha[bottom_start:] *= column_mask[None, :]
            # 浅色低饱和像素（米色墙/亮路面）在脚边与人物相连，仍可能被判定为
            # 人物；对底部 22% 做"亮度+饱和度"清除，只保留深色裤/鞋、红围裙等。
            light_start = int(height * 0.78)
            if light_start < height:
                region = array[light_start:]
                maximum = region.max(axis=2)
                minimum = region.min(axis=2)
                saturation = (maximum - minimum) / np.maximum(maximum, 1.0)
                bright_flat = (region.mean(axis=2) > 145.0) & (saturation < 0.22)
                alpha[light_start:][bright_flat] = 0.0
        alpha_image = Image.fromarray((alpha * 255).astype(np.uint8), mode="L")
        alpha_image = _morph(alpha_image, mode="open", size=3)
        alpha_image = _largest_components(alpha_image, keep_ratio=0.10)
        alpha_image = alpha_image.filter(ImageFilter.GaussianBlur(_ALPHA_FEATHER + 0.5))
        return np.asarray(alpha_image, dtype=np.float32) / 255.0

    def _body_gap(alpha_try: np.ndarray) -> tuple[float, float]:
        """评估一次抠像结果的"躯干空洞率"与"边缘残留率"。

        躯干空洞只取包围盒**水平中段**（40%~60%）的竖直中段（30%~55%）：
        这一带正是胸前/上衣大面积区域，头发、挎包、手臂都偏在两侧，
        整段平均会被它们稀释掉，无法反映"上衣被当成背景抠掉"。

        Args:
            alpha_try: 候选 alpha。

        Returns:
            ``(躯干空洞率, 边缘残留率)``。
        """
        solid = alpha_try > 0.5
        rows = np.flatnonzero(solid.any(axis=1))
        cols = np.flatnonzero(solid.any(axis=0))
        if rows.size == 0 or cols.size == 0:
            return 1.0, 1.0
        top, bottom = int(rows[0]), int(rows[-1]) + 1
        left, right = int(cols[0]), int(cols[-1]) + 1
        span = max(1, bottom - top)
        width_span = max(1, right - left)
        band_top = top + int(span * 0.30)
        band_bottom = top + int(span * 0.55)
        band_left = left + int(width_span * 0.40)
        band_right = left + int(width_span * 0.60)
        band = solid[band_top:max(band_top + 1, band_bottom), band_left:max(band_left + 1, band_right)]
        torso_gap = float(1.0 - band.mean()) if band.size else 1.0
        frame = max(2, int(min(height, width) * 0.02))
        # 只取左右两条边：全身定妆照的脚会触底、头顶可能贴边，上下边缘
        # 天然含人物；左右边缘若出现前景，才是背景被误保留。
        border = float(
            max(
                solid[:, :frame].mean(),
                solid[:, -frame:].mean(),
            )
        )
        return torso_gap, border

    # 自适应阈值：先用基准阈值；若躯干被整片抠空（浅色衣物与背景同色），
    # 逐级降低阈值重试，直到躯干空洞收敛或触底。边缘残留率必须保持在
    # 低位，避免为了找衣服把背景也留下来。
    chosen_alpha: np.ndarray | None = None
    best_alpha: np.ndarray | None = None
    best_gap = 1.0
    for threshold in (max(_ALPHA_THRESHOLD, 46.0), 28.0, 18.0, 12.0, 8.0):
        alpha_try = _build_alpha(threshold)
        torso_gap, border = _body_gap(alpha_try)
        logger.debug(
            "抠像阈值尝试 threshold=%s torso_gap=%.3f border=%.3f", threshold, torso_gap, border
        )
        if border <= 0.12 and torso_gap < best_gap:
            best_gap = torso_gap
            best_alpha = alpha_try
        if border <= 0.12 and torso_gap <= 0.42:
            chosen_alpha = alpha_try
            break
    # 择优：命中收敛条件直接用；否则用"空洞最小且边缘干净"的候选；
    # 全部候选都不合格时退回基准阈值结果（保持原有行为，不阻断合成）。
    if chosen_alpha is not None:
        alpha_float = chosen_alpha
    elif best_alpha is not None:
        alpha_float = best_alpha
    else:
        alpha_float = _build_alpha(max(_ALPHA_THRESHOLD, 46.0))

    rgb = _unpremultiply(array, background, alpha_float)
    rgba = np.dstack([rgb, alpha_float * 255.0]).astype(np.uint8)
    image = Image.fromarray(rgba, mode="RGBA")
    bbox = image.getbbox()
    if bbox is None:
        raise RuntimeError("抠像失败，未找到前景")
    # 素材人物触底（半身/脚贴边）时，bbox 最下方 4% 往往还残留一小块
    # 浅色地面（与脚相连、洪泛无法分离），直接裁掉；同时记录 bust，让合成
    # 层把这条平切边贴到屏幕下沿（安全区外），避免露出悬空硬边。
    x0, y0, x1, y1 = bbox
    bust = y1 >= height - 2
    if bust:
        y1 = max(y0 + 8, y1 - int(height * 0.04))
    image = image.crop((x0, y0, x1, y1))
    coverage = float((alpha_float > 0.5).mean())
    # 残缺抠像指标（2026-11 QA 新增）：首帧出现过「无头无躯干、只剩悬浮
    # 手臂」的合成畸变，根因是抠像把人物头部/上半身当成背景清掉了。
    # 这里把「顶部空洞比例」与「包围盒高度比例」一并带回，交给合成层降级。
    solid_rows = (alpha_float > 0.5).any(axis=1)
    top_gap_ratio = (
        float(np.argmax(solid_rows)) / float(height) if bool(solid_rows.any()) else 1.0
    )
    bbox_height_ratio = float(max(1, y1 - y0)) / float(height)
    return Cutout(
        image=image,
        coverage=coverage,
        bust=bust,
        top_gap_ratio=top_gap_ratio,
        bbox_height_ratio=bbox_height_ratio,
    )


def cutout_flat_background(image: Image.Image) -> Image.Image | None:
    """去除道具图的纯色/浅色平背景（白底商品图 → 透明 PNG）。

    2026-11 QA 修复：叙事道具此前以"四边羽化整图"贴入，白底道具图因此
    呈现为一块白色卡片，与实拍场景严重割裂。这里复用人物抠像的外部背景
    洪泛思路，对四角中值背景做颜色距离判定，得到干净 alpha 后裁紧致包围盒。

    Args:
        image: 道具素材（RGB/RGBA）。

    Returns:
        去背景后的 RGBA 图；背景不纯净或前景过少时返回 None（调用方回退
        到原有羽化整图）。

    Raises:
        无：图像异常按不可抠处理。
    """
    try:
        source = image.convert("RGB")
        array = np.asarray(source, dtype=np.float32)
        if array.ndim != 3 or array.shape[0] < 16 or array.shape[1] < 16:
            return None
        height, width = array.shape[:2]
        corners = np.concatenate(
            [
                array[:8, :8].reshape(-1, 3),
                array[:8, -8:].reshape(-1, 3),
                array[-8:, :8].reshape(-1, 3),
                array[-8:, -8:].reshape(-1, 3),
            ]
        )
        background = np.median(corners, axis=0)
        distance = np.linalg.norm(array - background, axis=2)
        exterior_small = _exterior_mask(distance > max(_ALPHA_THRESHOLD, 34.0))
        exterior = Image.fromarray(
            (exterior_small * 255).astype(np.uint8), mode="L"
        ).resize((width, height), Image.Resampling.NEAREST)
        exterior = exterior.filter(ImageFilter.MaxFilter(5))
        alpha = 1.0 - np.asarray(exterior, dtype=np.float32) / 255.0
        alpha_image = Image.fromarray((alpha * 255).astype(np.uint8), mode="L")
        alpha_image = _morph(alpha_image, mode="open", size=3)
        alpha_image = alpha_image.filter(ImageFilter.GaussianBlur(_ALPHA_FEATHER))
        alpha_float = np.asarray(alpha_image, dtype=np.float32) / 255.0
        if float((alpha_float > 0.5).mean()) < 0.03:
            return None
        # 主体与背景同色（白纸、白瓷等）时，硬抠会把主体一起抠空（实测
        # 离婚协议被抠成碎片）。当前景像素大量贴近背景色时放弃抠像，
        # 交给调用方回退到羽化整图，避免"道具消失"比"白底卡"更糟。
        foreground = alpha_float > 0.5
        near_background = distance[foreground] < 34.0
        if float(near_background.mean()) > 0.55:
            return None
        rgb = _unpremultiply(array, background, alpha_float)
        rgba = np.dstack([rgb, alpha_float * 255.0]).astype(np.uint8)
        result = Image.fromarray(rgba, mode="RGBA")
        bbox = result.getbbox()
        return result.crop(bbox) if bbox else None
    except (ValueError, TypeError, OSError):
        return None


def _cover(
    image: Image.Image,
    size: tuple[int, int],
    *,
    focus_x: float = 0.5,
    zoom: float = 1.0,
) -> Image.Image:
    """等比缩放并裁切到目标尺寸（cover）。

    Args:
        image: 源图。
        size: 目标 ``(宽, 高)``。
        focus_x: 裁切窗口的横向焦点（0~1，0=源图最左，1=最右）。
        zoom: 额外放大倍率（≥1；>1 时视野更窄，用于避开场景边缘人物）。

    Returns:
        cover 后的 RGB 图。

    Raises:
        无。
    """
    width, height = size
    focus = max(0.0, min(1.0, float(focus_x)))
    factor = max(1.0, float(zoom))
    scale = max(width / image.width, height / image.height) * factor
    resized = image.resize(
        (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale)))),
        Image.Resampling.LANCZOS,
    )
    left = int(round(resized.width * focus - width / 2))
    left = max(0, min(resized.width - width, left))
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height)).convert("RGB")


def _scene_avoid_crop(
    regions: Iterable[dict[str, float]],
    *,
    visible: float,
    margin: float = 0.02,
) -> tuple[float, float] | None:
    """从边缘规避区推导场景裁切窗口，把多余的边缘人物直接裁出画外。

    场景图里站在画面一侧的路人/店员，靠局部模糊/压暗仍会被观众认成
    "第二个人"（尤其 AI 生成的高清场景，模糊后轮廓依然可辨）。当规避区
    面积足够大（占源图宽度 ≥18%）且贴着一侧时，改用"收窄取景"：把可见
    窗口缩到规避区之前的区域，并让窗口中心成为新的构图焦点。

    局部虚化只作为补充手段保留给小块规避区（招牌、反光等）。

    Args:
        regions: 规避区（源图相对坐标 x0/y0/x1/y1）。
        visible: zoom=1 时 cover 裁切可见的源图宽度占比。
        margin: 规避区外额外留出的安全边距（源图宽度占比）。

    Returns:
        ``(focus_x, zoom)``；无需/无法裁切时返回 None。

    Raises:
        无：非法区域直接跳过。
    """
    best: tuple[float, float, float] | None = None  # (focus, zoom, 区域宽度)
    for region in regions:
        try:
            x0 = max(0.0, min(1.0, float(region["x0"])))
            x1 = max(0.0, min(1.0, float(region["x1"])))
        except (KeyError, TypeError, ValueError):
            continue
        region_w = x1 - x0
        if region_w < 0.18 or (best is not None and region_w <= best[2]):
            continue
        # 规避区要在当前可见窗口内才有裁切的必要
        if x1 <= 0.5 - visible / 2 or x0 >= 0.5 + visible / 2:
            continue
        if x0 >= 0.5:
            window = x0 - margin
            focus = window / 2
        elif x1 <= 0.5:
            window = 1.0 - x1 - margin
            focus = 1.0 - window / 2
        else:
            continue
        if not (0.30 <= window <= min(0.90, visible)):
            continue
        best = (focus, visible / window, region_w)
    if best is None:
        return None
    return best[0], best[1]


def _trim_dark_border(image: Image.Image, *, threshold: int = 25, limit: float = 0.32) -> Image.Image:
    """裁掉场景图顶部/底部的近黑边框（AI 场景图常见的门框黑边）。

    2026-11-04 QA 回归修复（首帧底部黑带）：Agnes 文生图输出的场景资产
    实测底部黑带高度可达 19.8%~27%（旧参数 threshold=18/limit=0.14 只能
    裁掉不足一半），残留黑带进入合成首帧后被 I2V 继承放大，渲染 QC 以
    ``black_block`` 拦截升级 manual_required（项目 1411 实测 5/23 镜）。
    与渲染 QC 的黑块判定（块均值 < 25）对齐后把 threshold 提到 25，
    limit 放宽到 0.32 覆盖实测最大黑带高度；连续行判定保证正常构图
    （底部暗区非连续贴边）不会被误裁。

    Args:
        image: 场景 RGB 图。
        threshold: 行平均亮度低于该值视为黑边（与 QC 黑块阈值对齐）。
        limit: 最多裁掉的行比例，防止误裁夜景。

    Returns:
        裁剪后的场景图；未检出黑边时原样返回。

    Raises:
        无。
    """
    array = np.asarray(image.convert("L"), dtype=np.float32)
    row_mean = array.mean(axis=1)
    max_rows = int(image.height * limit)
    top = 0
    while top < max_rows and row_mean[top] < threshold:
        top += 1
    bottom = 0
    while bottom < max_rows and row_mean[image.height - 1 - bottom] < threshold:
        bottom += 1
    if top or bottom:
        return image.crop((0, top, image.width, image.height - bottom))
    return image


def _vignette(image: Image.Image, strength: float = _VIGNETTE) -> Image.Image:
    """叠加径向暗角。

    Args:
        image: RGB 图。
        strength: 暗角强度（0~1）。

    Returns:
        叠加暗角后的 RGB 图。

    Raises:
        无。
    """
    width, height = image.size
    x = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
    y = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
    radius = np.sqrt(x * x + y * y) / math.sqrt(2.0)
    mask = np.clip(1.0 - strength * np.power(radius, 1.6), 0.0, 1.0)
    array = np.asarray(image, dtype=np.float32) * mask[:, :, None]
    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), mode="RGB")


def _grade(image: Image.Image, *, warmth: float = 0.06, contrast: float = 1.06) -> Image.Image:
    """统一暖色调与对比度（电影感的低饱和暖夜）。

    Args:
        image: RGB 图。
        warmth: 暖色偏移（0~0.2）。
        contrast: 对比度系数。

    Returns:
        调色后的 RGB 图。

    Raises:
        无。
    """
    array = np.asarray(image, dtype=np.float32)
    array[:, :, 0] *= 1.0 + warmth
    array[:, :, 2] *= 1.0 - warmth * 0.8
    mean = float(array.mean())
    array = (array - mean) * contrast + mean
    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), mode="RGB")


def _grain(image: Image.Image, amount: float = 2.2) -> Image.Image:
    """叠加细颗粒，减轻"合成图过于干净"的数码感。

    Args:
        image: RGB 图。
        amount: 噪声标准差（0~6）。

    Returns:
        加噪后的 RGB 图。

    Raises:
        无。
    """
    array = np.asarray(image, dtype=np.float32)
    rng = np.random.default_rng(seed=20261027)
    noise = rng.normal(0.0, amount, size=array.shape[:2])[:, :, None]
    return Image.fromarray(np.clip(array + noise, 0, 255).astype(np.uint8), mode="RGB")


def _blur_regions(
    image: Image.Image,
    regions: Iterable[dict[str, float]],
    *,
    blur: float = 18.0,
    darken: float = 0.26,
) -> Image.Image:
    """对场景中的规避区做局部虚化、降饱和与压暗（处理场景图自带无关人物）。

    Args:
        image: 场景 RGB 图。
        regions: 相对坐标区域列表，每项含 ``x0/y0/x1/y1``（0~1）。
        blur: 高斯模糊半径（像素）。
        darken: 亮度保留比例（0~1，越小越暗）。

    Returns:
        处理后 RGB 图。

    Raises:
        无：非法区域直接跳过。
    """
    width, height = image.size
    for region in regions:
        try:
            x0 = int(np.clip(float(region["x0"]), 0.0, 1.0) * width)
            y0 = int(np.clip(float(region["y0"]), 0.0, 1.0) * height)
            x1 = int(np.clip(float(region["x1"]), 0.0, 1.0) * width)
            y1 = int(np.clip(float(region["y1"]), 0.0, 1.0) * height)
        except (KeyError, TypeError, ValueError):
            continue
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        patch = image.crop((x0, y0, x1, y1)).filter(ImageFilter.GaussianBlur(blur))
        patch = ImageEnhance.Brightness(patch).enhance(1.0 - max(0.0, min(1.0, darken)))
        patch = ImageEnhance.Color(patch).enhance(0.70)
        # 羽化遮罩，避免出现硬边方块
        mask = Image.new("L", (x1 - x0, y1 - y0), 0)
        draw = ImageDraw.Draw(mask)
        inset = max(2, min(x1 - x0, y1 - y0) // 10)
        draw.rounded_rectangle(
            (inset, inset, x1 - x0 - inset, y1 - y0 - inset),
            radius=inset,
            fill=255,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(inset))
        image.paste(patch, (x0, y0), mask)
    return image


def _place_subject(
    canvas: Image.Image,
    cutout: Image.Image,
    *,
    height_ratio: float,
    x: float,
    anchor: str,
    safe_bottom: float,
    opacity: float = 1.0,
    shadow: bool = True,
    max_width_ratio: float = 0.92,
    bust: bool = False,
    blur: float = 0.0,
    dim: float = 0.0,
    bleed: bool = False,
) -> None:
    """把抠像人物按比例贴到画布（含接地与接触阴影）。

    Args:
        canvas: 目标 RGB 画布（原地修改）。
        cutout: RGBA 人物。
        height_ratio: 人物包围盒高度占画布比例。
        x: 人物中心横坐标（0~1）。
        anchor: ``bottom`` 接地 / ``top`` 顶部出血（特写）/ ``center`` 居中。
        safe_bottom: 底部安全区比例（仅用于 bottom 锚点）。
        opacity: 整体不透明度（0~1）。
        shadow: 是否添加接触阴影。
        max_width_ratio: 人物最大宽度占画布比例（单人特写允许 >1 以裁手臂）。
        bust: 素材本身在底边被截断（半身照），底部直接贴屏幕下沿。
        blur: 高斯模糊半径（像素，过肩前景听者虚化）。
        dim: 压暗比例（0~0.6，前景听者剪影化）。
        bleed: 是否允许人物大面积出画（前景遮挡层贴边）。

    Returns:
        无。

    Raises:
        无。
    """
    width, height = canvas.size
    target_h = max(64, int(round(height * max(0.08, min(1.6, height_ratio)))))
    scale = target_h / cutout.height
    target_w = max(1, int(round(cutout.width * scale)))
    max_w = max(1, int(round(width * max(0.2, max_width_ratio))))
    if target_w > max_w:
        scale *= max_w / target_w
        target_w = max_w
        target_h = max(1, int(round(cutout.height * scale)))
    person = cutout.resize((target_w, target_h), Image.Resampling.LANCZOS)
    if blur > 0.0:
        person = person.filter(ImageFilter.GaussianBlur(float(blur)))
    if dim > 0.0:
        alpha_channel = person.getchannel("A")
        person = ImageEnhance.Brightness(person.convert("RGB")).enhance(
            max(0.0, 1.0 - float(dim))
        ).convert("RGBA")
        person.putalpha(alpha_channel)
    if opacity < 1.0:
        alpha = person.getchannel("A").point(lambda value: int(value * max(0.0, opacity)))
        person.putalpha(alpha)
    alpha_array = np.asarray(person.getchannel("A"), dtype=np.float32)
    if not bust and float(alpha_array[-1].mean()) > 40.0 and str(anchor) == "bottom":
        # 底部 2% 直接清零（脚底/裙摆最下方常残留一小块背景色）。不做大范围
        # 渐隐：全身人像渐隐会变成"幽灵腿"，比平底裁切更难接受；阴影负责
        # 把脚底压进地面。
        alpha_array[-max(2, int(target_h * 0.02)) :, :] = 0.0
        person.putalpha(Image.fromarray(np.clip(alpha_array, 0, 255).astype(np.uint8), mode="L"))
    left = int(round(width * x - target_w / 2))
    if bleed:
        # 前景遮挡层允许大幅出画（只留肩背），常规置入则限制最多出画 35%
        left = max(-int(target_w * 0.35), min(width - int(target_w * 0.25), left))
    else:
        left = max(-int(target_w * 0.35), min(width - int(target_w * 0.65), left))
        # 2026-11 QA 修复（首帧人物贴边/压在门框上）：人物小于画幅时，
        # 保证整身落在左右各 4% 的安全边内，避免人物半边压在门框、画幅
        # 边缘或柜台立柱上（实测人物贴在门口左侧、与场景主体割裂）。
        edge_margin = int(width * 0.04)
        if target_w <= width - edge_margin * 2:
            left = max(edge_margin, min(width - target_w - edge_margin, left))
    if anchor == "top":
        # 顶部留 2% 呼吸空间：旧值 -4%（出血）会把半身素材的头顶裁掉，
        # 只剩"无头半身"贴在画面上。
        top = int(round(target_h * 0.02))
    elif anchor == "center":
        top = int(round((height - target_h) / 2))
    elif bust:
        # 半身素材的腰线/底边平切放在屏幕下沿，切边落在画外（字幕安全区
        # 之下），不会出现"人物悬空、腰下一刀齐"的穿帮。
        top = height - target_h
    else:
        top = int(round(height * (1.0 - safe_bottom) - target_h))
        top = min(top, height - int(target_h * 0.12))

    # 2026-11 QA 修复（人物与场景割裂）：人物素材是独立生成的写真，直接贴到
    # 暖光场景上会显得过亮/过冷（"抠图悬浮"）。这里做极轻微的统一调色与
    # 降亮，让肤色与场景光比接近；幅度刻意保守，避免把人物压成灰块。
    if not bleed:
        # 用当前 person 的 alpha（已包含底部清理结果），避免把刚清掉的
        # 脚底背景残块重新贴回画面（会产生黑色斑块）。
        current_alpha = person.getchannel("A")
        graded = _grade(person.convert("RGB"), warmth=0.02, contrast=1.03)
        graded = ImageEnhance.Brightness(graded).enhance(0.96)
        person = graded.convert("RGBA")
        person.putalpha(current_alpha)

    if shadow and not bust and anchor != "center":
        # 接触阴影：把人物 alpha 压扁成"脚底椭圆"，模糊后以 35% 不透明度
        # 叠在脚下，减少"悬空贴纸"感（65% 在暗背景上会形成黑色矩形）。
        shadow_alpha = person.getchannel("A").resize((target_w, max(4, target_h // 16)))
        shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(14))
        shadow_layer = Image.new("RGBA", (target_w, max(4, target_h // 16)), (0, 0, 0, 0))
        shadow_layer.putalpha(shadow_alpha.point(lambda value: int(value * 0.35)))
        shadow_y = top + target_h - max(4, target_h // 20)
        canvas.paste(
            Image.new("RGB", shadow_layer.size, (0, 0, 0)),
            (left, shadow_y),
            shadow_layer.getchannel("A"),
        )

    canvas.paste(person, (left, top), person)


def _place_prop_inset(
    canvas: Image.Image,
    image: Image.Image,
    *,
    x: float,
    y: float,
    height_ratio: float,
) -> None:
    """把非特写道具以羽化边静物图叠到右下角（不抢人物焦点）。

    道具图多为带场景的整图（锅架、碗、照片），硬抠会留碎片与空洞，加相框
    又像贴纸。这里改成无边框、四边羽化的"景深内静物"：只保留画面信息，
    边缘融进场景，大小压到人物胸口以下，叙事道具可见但不遮挡表演。

    Args:
        canvas: 目标 RGB 画布（原地修改）。
        image: 道具素材 RGB 图。
        x: 图心横坐标（0~1）。
        y: 图心纵坐标（0~1）。
        height_ratio: 图高占画布比例。

    Returns:
        无。

    Raises:
        无。
    """
    # 2026-11 QA 修复：白底道具图先做平背景抠像，避免出现"白色卡片"贴纸；
    # 抠像失败（非纯色背景/前景过少）再回退到原有羽化整图。
    cleaned = cutout_flat_background(image)
    if cleaned is not None:
        image = cleaned
    width, height = canvas.size
    photo_h = max(48, int(round(height * max(0.05, min(0.6, height_ratio)) * 0.9)))
    photo_w = max(1, int(round(photo_h * image.width / max(1, image.height))))
    max_w = int(round(width * 0.38))
    if photo_w > max_w:
        photo_w = max_w
        photo_h = max(1, int(round(photo_w * image.height / max(1, image.width))))
    left = int(round(width * x - photo_w / 2))
    top = int(round(height * y - photo_h / 2))
    left = max(8, min(width - photo_w - 8, left))
    top = max(8, min(height - photo_h - 8, top))

    if cleaned is not None:
        # 保留抠像 alpha（_cover 会转 RGB 丢透明通道），按 cover 裁切缩放。
        photo = ImageOps.fit(
            image, (photo_w, photo_h), method=Image.Resampling.LANCZOS
        )
        photo = ImageEnhance.Brightness(photo).enhance(0.94)
        base_alpha = photo.getchannel("A")
    else:
        photo = ImageEnhance.Brightness(_cover(image, (photo_w, photo_h))).enhance(0.90)
        photo = ImageEnhance.Color(photo).enhance(0.92).convert("RGBA")
        base_alpha = None
    feather = max(10, min(photo_w, photo_h) // 9)
    # 先画在带外扩留白的画布上再高斯模糊：直接模糊贴边白块时 Pillow 会
    # 把边界像素复制到画外，边缘仍是硬切（视觉上像一张贴纸）。
    pad = feather * 2
    mask_big = Image.new("L", (photo_w + pad * 2, photo_h + pad * 2), 0)
    ImageDraw.Draw(mask_big).rounded_rectangle(
        (pad, pad, pad + photo_w - 1, pad + photo_h - 1),
        radius=max(10, feather),
        fill=255,
    )
    mask_big = mask_big.filter(ImageFilter.GaussianBlur(feather * 0.6))
    mask = mask_big.crop((pad, pad, pad + photo_w, pad + photo_h))
    if base_alpha is None:
        photo.putalpha(
            Image.composite(photo.getchannel("A"), Image.new("L", mask.size, 0), mask)
        )
    else:
        # 抠像 alpha 与羽化遮罩相乘：既保留去背景后的形状，又让边缘融进场景。
        photo.putalpha(ImageChops.multiply(base_alpha, mask))

    shadow = Image.new("L", (photo_w + 40, photo_h + 40), 0)
    # 抠像道具用自身 alpha 生成贴合形状的阴影（矩形遮罩会产生"方框投影"）。
    shadow.paste(photo.getchannel("A") if cleaned is not None else mask, (20, 24))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18)).point(lambda value: int(value * 0.45))
    canvas.paste(Image.new("RGB", shadow.size, (0, 0, 0)), (left - 20, top - 20), shadow)
    canvas.paste(photo, (left, top), photo)


def _place_photo_card(
    canvas: Image.Image,
    image: Image.Image,
    *,
    x: float,
    y: float,
    height_ratio: float,
) -> None:
    """把道具/无法抠像的素材作为带相框的静物卡片贴在画布上。

    道具图多为场景化整图（锅、碗、照片），硬抠会留下碎片与空洞；整幅
    贴到人物脸上又会挡戏。因此非特写道具统一做成"拍立得"式小卡：暖白
    相框 + 圆角 + 投影，放在右下角台面位置，既让叙事道具可见，又不抢
    人物焦点。

    Args:
        canvas: 目标 RGB 画布（原地修改）。
        image: 道具或卡片素材 RGB 图。
        x: 卡片中心横坐标（0~1）。
        y: 卡片中心纵坐标（0~1）。
        height_ratio: 照片高度占画布比例。

    Returns:
        无。

    Raises:
        无。
    """
    width, height = canvas.size
    photo_h = max(48, int(round(height * max(0.05, min(0.6, height_ratio)))))
    photo_w = max(1, int(round(photo_h * image.width / max(1, image.height))))
    max_w = int(round(width * 0.42))
    if photo_w > max_w:
        photo_w = max_w
        photo_h = max(1, int(round(photo_w * image.height / max(1, image.width))))
    pad = max(6, int(round(photo_h * 0.055)))
    card_w = photo_w + pad * 2
    card_h = photo_h + pad * 2
    left = int(round(width * x - card_w / 2))
    top = int(round(height * y - card_h / 2))
    left = max(8, min(width - card_w - 8, left))
    top = max(8, min(height - card_h - 8, top))

    shadow = Image.new("RGBA", (card_w + 48, card_h + 48), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (24, 30, 24 + card_w, 30 + card_h), radius=26, fill=(0, 0, 0, 150)
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    canvas.paste(
        Image.new("RGB", shadow.size, (0, 0, 0)),
        (left - 24, top - 24),
        shadow.getchannel("A"),
    )

    card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (0, 0, card_w - 1, card_h - 1),
        radius=18,
        fill=(247, 241, 229, 242),
        outline=(255, 252, 244, 255),
        width=3,
    )
    photo = _cover(image, (photo_w, photo_h))
    mask = Image.new("L", (photo_w, photo_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, photo_w - 1, photo_h - 1), radius=max(6, 18 - pad // 2), fill=255
    )
    card.paste(photo, (pad, pad), mask)
    canvas.paste(card, (left, top), card)


def compose_scene(
    *,
    setting_path: Path | None,
    subject_layers: list[tuple[Image.Image, dict[str, Any], bool, bool]],
    prop_layer: Image.Image | None,
    spec: Any,
    prop_cards: list[tuple[Image.Image, Any]] | None = None,
    focus_x: float | None = None,
    zoom: float | None = None,
) -> Image.Image:
    """把场景、人物（可选道具）合成为整幅画面。

    Args:
        setting_path: 场景图路径（可空）。
        subject_layers: ``(RGBA 人物, 布局字段, bust, is_card)`` 列表，按远景
            到近景排序；``is_card`` 表示抠像失败/不可抠，按相卡呈现。
        prop_layer: 道具特写图（非空时整幅输出，忽略场景与人物）。
        spec: CompositionSpec（读取 canvas/safe_area/setting）。
        prop_cards: ``(道具图, 布局字段)`` 列表，作为右下角静物卡叠加。
        focus_x: 场景裁切横向焦点（0~1，空表示自动/居中）。
        zoom: 场景额外放大倍率（≥1，空表示自动/1.0）。

    Returns:
        720×1280 RGB 图像。

    Raises:
        RuntimeError: 场景与人物均不可用且无道具时。
    """
    canvas_info = getattr(spec, "canvas", {}) or {}
    size = (int(canvas_info.get("w", DEFAULT_SIZE[0])), int(canvas_info.get("h", DEFAULT_SIZE[1])))
    safe_bottom = float((getattr(spec, "safe_area", {}) or {}).get("bottom", 0.18))
    shot_size = str(getattr(spec, "shot_size", "medium"))
    setting_info = getattr(spec, "setting", None) or {}
    regions = list(setting_info.get("avoid_regions") or [])
    # 资产 meta 里人工调过的取景优先于自动推导
    if focus_x is None and isinstance(setting_info.get("focus_x"), (int, float)):
        focus_x = float(setting_info["focus_x"])
    if zoom is None and isinstance(setting_info.get("zoom"), (int, float)):
        zoom = float(setting_info["zoom"])

    if prop_layer is not None:
        canvas = _cover(
            prop_layer,
            size,
            focus_x=0.5 if focus_x is None else float(focus_x),
            zoom=1.0 if zoom is None else max(1.0, float(zoom)),
        )
        return _grain(_vignette(_grade(canvas, warmth=0.04, contrast=1.04), strength=0.28))

    if setting_path is not None and Path(setting_path).exists():
        setting = _trim_dark_border(Image.open(setting_path).convert("RGB"))
        # cover 裁切在 zoom=1 时可见的源图宽度占比（按长边铺满推导）
        base_visible = size[0] / (
            max(size[0] / setting.width, size[1] / setting.height) * setting.width
        )
        auto_crop = None
        if focus_x is None and zoom is None:
            auto_crop = _scene_avoid_crop(regions, visible=base_visible)
        if auto_crop is not None:
            crop_focus, crop_zoom = auto_crop
        else:
            crop_focus = 0.5 if focus_x is None else float(focus_x)
            crop_zoom = 1.0 if zoom is None else max(1.0, float(zoom))
        canvas = _cover(setting, size, focus_x=crop_focus, zoom=crop_zoom)
        # 收窄取景已把边缘路人裁出画外，此时再按原坐标虚化会错位；
        # 只有未裁切（小块规避区）才做局部虚化。
        if regions and auto_crop is None:
            canvas = _blur_regions(canvas, regions)
        # 浅景深：只留轻微失焦，过度模糊会让场景彻底糊成色块
        canvas = canvas.filter(ImageFilter.GaussianBlur(1.8))
    else:
        canvas = Image.new("RGB", size, (22, 24, 29))
        draw = ImageDraw.Draw(canvas)
        for y in range(size[1]):
            ratio = y / max(1, size[1] - 1)
            draw.line((0, y, size[0], y), fill=(int(20 + 10 * ratio), int(22 + 8 * ratio), int(27 + 6 * ratio)))
    canvas = _grade(canvas, warmth=0.08, contrast=1.05)

    count = len(subject_layers)
    spec_sitting = bool(getattr(spec, "sitting", False))
    single_width = {
        "extreme_close_up": 1.90,
        "close_up": 1.75,
        "over_shoulder": 1.00,
        "medium": 1.25,
        "wide": 1.08,
        "extreme_wide": 0.95,
    }
    for image, placement, bust, is_card in subject_layers:
        height_ratio = float(getattr(placement, "height_ratio", 0.5))
        place_x = float(getattr(placement, "x", 0.5))
        anchor = str(getattr(placement, "anchor", "bottom"))
        foreground = bool(getattr(placement, "foreground", False))
        if is_card:
            photo_y = 0.55 if anchor == "top" else 0.72
            _place_photo_card(
                canvas, image, x=place_x, y=photo_y, height_ratio=height_ratio
            )
            continue
        if spec_sitting or bool(getattr(placement, "sitting", False)):
            # 坐姿/蹲姿：资产只有全身站姿图，按腰线以上裁切后贴屏幕下沿，
            # 用"腰线出画 + 半身近景"的表达读出"坐在桌前"，避免站着演坐姿。
            # 裁到 62% 会保留到大腿根（观感仍是站着的全身小图），改为 52%
            # 只保留头顶到腰；同时放大占屏并强制 bottom 锚点，切边落在画外。
            image = image.crop(
                (0, 0, image.width, max(8, int(image.height * 0.52)))
            )
            bust = True
            anchor = "bottom"
            safe_bottom = 0.0
            # 上半身坐姿按近景处理：占屏 56%~62%，过小会回到"远处站着"的观感，
            # 过大则头顶顶到字幕安全区。
            height_ratio = max(0.56, min(0.62, height_ratio))
        if foreground:
            # 过肩前景听者：大比例、贴右缘出血（只露肩背/半张侧脸），
            # 叠加大半径虚化与压暗，避免观众把前景听者误读成"第三个人"。
            width_limit = 0.60
        elif count <= 1:
            # 单人不加宽度上限会把方形定妆照按屏高缩放后横向溢出；上限按
            # 景别放宽（特写裁手臂），同时不让中景被压成全身小图。
            width_limit = single_width.get(shot_size, 1.10)
        elif count == 2:
            width_limit = 0.58 if shot_size in ("wide", "extreme_wide") else 0.55
        else:
            width_limit = 0.44
        _place_subject(
            canvas,
            image,
            height_ratio=height_ratio,
            x=place_x,
            anchor=anchor,
            safe_bottom=safe_bottom,
            opacity=float(getattr(placement, "opacity", 1.0)),
            shadow=(anchor != "center") and not foreground,
            max_width_ratio=width_limit,
            bust=bust,
            blur=float(getattr(placement, "blur", 0.0) or 0.0),
            dim=float(getattr(placement, "dim", 0.0) or 0.0),
            bleed=foreground,
        )
    for image, placement in prop_cards or []:
        _place_prop_inset(
            canvas,
            image,
            x=float(getattr(placement, "x", 0.72)),
            y=float(getattr(placement, "y", 0.78)),
            height_ratio=float(getattr(placement, "height_ratio", 0.2)),
        )
    return _grain(_vignette(canvas))


def load_prop_full(path: Path) -> Image.Image | None:
    """读取道具特写图（无法解码时返回 None）。

    Args:
        path: 道具图片路径。

    Returns:
        RGB 图或 None。

    Raises:
        无：解码失败按缺失处理。
    """
    try:
        return Image.open(path).convert("RGB")
    except Exception:  # noqa: BLE001 - 素材损坏时由调用方降级
        return None


#: 含"可读文字"的道具名词表：这类道具的资产图本身常带 AI 生成的乱码
#: （转账截图、合同条款、报表），贴进首帧后会被 I2V 继续放大成穿帮。
READABLE_TEXT_PROP_MARKERS: tuple[str, ...] = (
    "手机", "屏幕", "转账", "截图", "合同", "协议", "条款", "账单", "报表",
    "账单", "便签", "字条", "文件", "档案", "资料", "名片", "海报", "菜单",
    "账本", "笔记本", "说明书", "信", "报纸", "杂志", "票据", "发票",
)


def soften_readable_text(image: Image.Image, *, radius: float = 2.6) -> Image.Image:
    """对含文字的道具图做轻微虚化与降对比，使文字不可辨认。

    背景（2026-11 QA）：验收成片出现"转账记录 -200000 重复六行、中文全部
    乱码"的屏幕浮层。单靠提示词无法稳定抑制 I2V 写字，因此在首帧参考图
    阶段就把纸张/屏幕内容处理成"有材质、不可读"，让视频模型没有清晰文字
    可继承。真正需要展示的可读信息应由后期贴片合成。

    Args:
        image: 道具 RGB 图。
        radius: 高斯模糊半径（像素，2~4 之间既有"看不清"又不糊成色块）。

    Returns:
        处理后的 RGB 图（新对象，不修改入参）。

    Raises:
        无：PIL 处理异常时返回原图。
    """
    try:
        blurred = image.filter(ImageFilter.GaussianBlur(max(0.5, float(radius))))
        out = ImageEnhance.Contrast(blurred).enhance(0.92)
        return ImageEnhance.Brightness(out).enhance(1.02)
    except Exception:  # noqa: BLE001 - 处理失败不影响合成
        return image

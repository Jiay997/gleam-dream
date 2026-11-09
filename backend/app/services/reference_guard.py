"""视频参考帧准入机制（宫格检测 / 安全版本选择 / 单格提取 / 触边检测）。

背景缺陷（2026-10-29 排查结论）：

1. ``asset_versions`` 中表情集（expression_set）为九宫格拼图，且资产服务
   每次生成新版本都无条件把它设为 ``current_version_id``；
2. 渲染执行器（executor）、首帧合成（shot_frame_service）、分镜锚点绑定
   （storyboard_service）三处取图时均只读 ``current_version_id``、不过滤
   版本类型 → 九宫格图被当作 I2V 首帧 / 合成图层直接进入视频链路，
   导致成片出现"多宫格画面"穿帮。

本模块提供四组能力，作为**所有视频参考图入口的统一准入闸门**：

- :func:`pick_safe_reference`：从资产版本列表中按优先级选出"单视角完整
  全身图 / 单表情面部特写"，显式排除宫格类版本（expression_set 等）；
- :func:`detect_grid_layout`：对图像做规则网格布局检测（行/列投影周期
  性暗线 + 分块相关性），命中说明是多宫格/分屏拼图；
- :func:`ensure_single_panel`：参考帧预检入口——命中宫格时自动提取
  左上单格落盘，返回可用图片路径与告警信息；
- :func:`check_edge_contact`：角色资产完整性检测（人物轮廓触边、
  全身图缺脚部），供资产 QC 使用。

所有函数均"检测失败不阻断主流程"：Pillow 不可用或图像异常时返回
保守结果（放行原路径 / 不告警），避免修复引入新的渲染阻塞。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("gleamdream.services.reference_guard")

# ---- 版本类型准入白/黑名单 -------------------------------------------------

#: 角色参考图安全优先级（越靠前越优先；均为"单视角"图）
CHARACTER_REFERENCE_ORDER: tuple[str, ...] = (
    "portrait",
    "turnaround_front",
    "turnaround_side",
    "turnaround_back",
)

#: 禁止作为视频参考帧的版本类型（宫格/多面板拼图类）
GRID_SUSPECT_VERSION_TYPES: frozenset[str] = frozenset({
    "expression_set",  # 表情集：九宫格拼图（仅限人设/情绪参考用途）
})

#: 场景/道具参考图允许的版本类型
SCENE_PROP_REFERENCE_TYPES: frozenset[str] = frozenset({"scene_ref", "prop_ref"})


def pick_safe_reference(
    versions: list[Any],
    *,
    asset_type: str = "character",
) -> tuple[Any | None, list[str]]:
    """从资产版本列表中选出可安全用作视频参考帧的版本。

    选择规则：
    - 角色资产：按 ``CHARACTER_REFERENCE_ORDER`` 优先级取第一个含图片的
      版本（portrait 半身/全身 → 转视图单视角全身），**显式跳过**
      ``GRID_SUSPECT_VERSION_TYPES`` 中的宫格类版本；
    - 场景/道具资产：仅接受 ``scene_ref`` / ``prop_ref`` 类型；
    - 全部不可用时返回 ``(None, warnings)``，由调用方回退其他参考图。

    Args:
        versions: ``AssetVersion`` 实例列表（须含 media_file_id /
            version_type 属性；同一版本的重复项自动去重）。
        asset_type: 资产类型（character/scene/prop）。

    Returns:
        ``(选中版本, 告警列表)`` 二元组；告警记录被跳过的宫格类版本。
    """
    warnings: list[str] = []
    seen: set[int] = set()
    candidates: list[Any] = []
    for row in versions:
        vid = getattr(row, "id", None)
        if vid is None or int(vid) in seen:
            continue
        seen.add(int(vid))
        if getattr(row, "media_file_id", None) is None:
            continue
        candidates.append(row)

    if asset_type == "character":
        allowed_order = CHARACTER_REFERENCE_ORDER
        selected: Any | None = None
        for row in candidates:
            version_type = str(getattr(row, "version_type", "") or "")
            if version_type in GRID_SUSPECT_VERSION_TYPES:
                warnings.append(
                    f"参考帧准入：版本 {vid_label(row)}（{version_type}）为宫格类素材，已排除"
                )
                continue
            if version_type not in allowed_order:
                continue
            if selected is None:
                selected = row
        return selected, warnings

    # 场景/道具：仅允许单一参考类型
    allowed_types = SCENE_PROP_REFERENCE_TYPES
    for row in candidates:
        version_type = str(getattr(row, "version_type", "") or "")
        if version_type in GRID_SUSPECT_VERSION_TYPES:
            warnings.append(
                f"参考帧准入：版本 {vid_label(row)}（{version_type}）为宫格类素材，已排除"
            )
            continue
        if version_type in allowed_types:
            return row, warnings
    return None, warnings


def vid_label(version: Any) -> str:
    """生成版本日志标签。

    Args:
        version: 资产版本实例。

    Returns:
        ``#<id>`` 形式的短标签；无 ID 时返回 ``#?``。
    """
    vid = getattr(version, "id", None)
    return f"#{vid}" if vid is not None else "#?"


# ---- 网格布局检测 -----------------------------------------------------------

#: 网格检测：投影暗线谷的最小深度（0-1，相对行/列亮度均值）
_GRID_VALLEY_DEPTH: float = 0.10
#: 网格检测：谷最小宽度占图像边比例（1px 线太窄会被噪声淹没）
_GRID_MIN_VALLEY_RATIO: float = 0.004
#: 网格检测：内部暗线数量阈值（≥2 条横/纵暗线 → 宫格嫌疑）
_GRID_MIN_LINES: int = 2
#: 网格检测：内容块均匀性上限——宫格拼图的内容块（暗谷）宽度与间距近似
#: 等距（变异系数低）；真实场景图（视频帧/环境照）的暗区宽度参差。
#: 宽度或间距的变异系数（std/mean）超过该值即判定为非规则网格。
_GRID_UNIFORMITY_MAX_CV: float = 0.45


def detect_grid_layout(image_path: str | Path) -> dict[str, Any]:
    """检测图像是否为规则网格拼图（多宫格 / 分屏 / 表情集拼版）。

    算法（纯 Pillow/NumPy 启发式，0 成本）：
    1. 灰度化并按列/行做亮度投影；
    2. 投影中出现"谷深度 ≥ 阈值、宽度 ≥ 阈值"的内部暗线视为分隔线；
    3. 内部横线或纵线数量 ≥ 2 判定为宫格布局；
    4. **均匀性检验（P0 误报修复）**：真实场景图（视频帧/环境照）的暗区
       宽窄参差（窗户、货架缝隙、阴影），而宫格拼图的内容块是规则等分——
       暗谷宽度与相邻谷间距的变异系数（std/mean）必须 ≤
       ``_GRID_UNIFORMITY_MAX_CV`` 才判定宫格。

    Args:
        image_path: 图片磁盘路径。

    Returns:
        ``{is_grid, h_lines, v_lines, score, reason}``；图像缺失、
        Pillow 不可用或解码失败时返回 ``is_grid=False``（保守放行，
        由版本类型白名单兜底）。
    """
    path = Path(image_path)
    result: dict[str, Any] = {
        "is_grid": False,
        "h_lines": 0,
        "v_lines": 0,
        "score": 0.0,
        "reason": "",
    }
    if not path.exists() or not path.is_file():
        result["reason"] = "image_missing"
        return result
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as image:
            gray = np.asarray(image.convert("L").resize((256, 256)), dtype="float32")
    except (OSError, ValueError, ImportError, Exception) as exc:  # noqa: BLE001 检测失败保守放行
        logger.debug("宫格检测跳过 %s: %s", path.name, exc)
        result["reason"] = f"detect_error:{type(exc).__name__}"
        return result

    # 列投影（纵向暗线 = 列均值低谷）；行投影同理
    col_profile = gray.mean(axis=0)
    row_profile = gray.mean(axis=1)
    v_valleys = _find_valleys(col_profile)
    h_valleys = _find_valleys(row_profile)
    v_lines = _count_regular(v_valleys)
    h_lines = _count_regular(h_valleys)
    lines = max(v_lines, h_lines)
    result.update({
        "h_lines": h_lines,
        "v_lines": v_lines,
        "score": round(min(1.0, lines / 3.0), 2),
    })
    if lines >= _GRID_MIN_LINES:
        result["is_grid"] = True
        result["reason"] = f"内部分隔线 h={h_lines} v={v_lines} ≥{_GRID_MIN_LINES}"
    return result


def _find_valleys(profile: Any) -> list[tuple[int, int]]:
    """在一维亮度投影中定位内部暗谷区间。

    仅统计图像内部（两端 5% 边距之外）的暗谷，避免把画面边缘的
    暗角/黑边误判为分隔线。

    Args:
        profile: 一维 numpy 数组（0-255 亮度均值）。

    Returns:
        暗谷 ``(start, end)`` 区间列表（end 为 exclusive）。
    """
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - 无 numpy 环境不检测
        return []
    n = int(profile.shape[0])
    if n < 16:
        return []
    margin = max(2, int(n * 0.05))
    inner = profile[margin:n - margin]
    mean_val = float(inner.mean())
    min_width = max(1, int(n * _GRID_MIN_VALLEY_RATIO))
    valleys: list[tuple[int, int]] = []
    in_valley = False
    start = margin
    for idx in range(margin, n - margin):
        val = float(profile[idx])
        is_valley = val < mean_val * (1.0 - _GRID_VALLEY_DEPTH)
        if is_valley and not in_valley:
            in_valley = True
            start = idx
        elif not is_valley and in_valley:
            in_valley = False
            if idx - start >= min_width:
                valleys.append((start, idx))
    if in_valley and (n - margin) - start >= min_width:
        valleys.append((start, n - margin))
    return valleys


def _count_regular(valleys: list[tuple[int, int]]) -> int:
    """统计"宽度与间距近似均匀"的暗谷数量（规则网格内容块判定）。

    变异系数（CV = std/mean）过滤：宫格拼图的内容块等宽等距（CV 低），
    真实场景图的暗区参差（CV 高）被排除，显著降低视频帧误报。

    Args:
        valleys: :func:`_find_valleys` 产出的暗谷区间列表。

    Returns:
        通过均匀性检验的暗谷数量；谷数 < 2 时直接返回谷数。
    """
    if len(valleys) < 2:
        return len(valleys)
    widths = [float(end - start) for start, end in valleys]
    centers = [(start + end) / 2.0 for start, end in valleys]
    gaps = [b - a for a, b in zip(centers, centers[1:])]
    width_cv = _cv(widths)
    gap_cv = _cv(gaps) if gaps else 1.0
    if width_cv > _GRID_UNIFORMITY_MAX_CV or gap_cv > _GRID_UNIFORMITY_MAX_CV:
        return 0
    return len(valleys)


def _cv(values: list[float]) -> float:
    """计算变异系数（std/mean）。

    Args:
        values: 非负数值列表。

    Returns:
        变异系数；均值为 0 或列表为空时返回 1.0（视为不均匀）。
    """
    if not values:
        return 1.0
    mean_val = sum(values) / len(values)
    if mean_val <= 1e-6:
        return 1.0
    var = sum((v - mean_val) ** 2 for v in values) / len(values)
    return (var ** 0.5) / mean_val


# ---- 参考帧预检 + 单格提取 ---------------------------------------------------

#: 单格提取时的边距收缩比例（规避格间分隔线残边）
_CELL_INSET_RATIO: float = 0.02


def ensure_single_panel(
    image_path: str | Path,
    *,
    out_dir: str | Path | None = None,
) -> tuple[str, list[str]]:
    """参考帧预检入口：宫格图命中时自动提取左上单格。

    处理流程：
    1. :func:`detect_grid_layout` 检测；未命中宫格 → 原路径直接放行；
    2. 命中 → 把左上单格（含 ``_CELL_INSET_RATIO`` 边距收缩）裁出并
       落盘为独立 PNG，用单格替代整图作为参考帧（告警中记录）；
    3. 提取失败（Pillow 异常等）→ 保守返回原路径并告警，由 negative
       prompt 与逐镜头抽帧复检兜底。

    Args:
        image_path: 候选参考帧图片路径。
        out_dir: 单格提取产物的落盘目录；缺省为源图同级 ``.single/``。

    Returns:
        ``(实际应使用的图片路径, 告警列表)`` 二元组。
    """
    src = Path(image_path)
    detection = detect_grid_layout(src)
    if not detection.get("is_grid"):
        return str(src), []
    warnings = [f"参考帧预检：{src.name} 疑似宫格拼图（{detection.get('reason')}），自动提取单格"]
    try:
        from PIL import Image

        with Image.open(src) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            half_w, half_h = width // 2, height // 2
            inset_x = int(width * _CELL_INSET_RATIO)
            inset_y = int(height * _CELL_INSET_RATIO)
            # 取左上单格（表情集/三视图宫格通常首格为主形象）
            cell = rgb.crop((inset_x, inset_y, half_w - inset_x, half_h - inset_y))
            target_dir = Path(out_dir) if out_dir else src.parent / ".single"
            target_dir.mkdir(parents=True, exist_ok=True)
            out_path = target_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}_cell.png"
            cell.save(out_path, format="PNG")
        warnings.append(f"已提取左上单格 → {out_path.name}")
        return str(out_path), warnings
    except (OSError, ValueError, ImportError) as exc:
        warnings.append(f"单格提取失败（{type(exc).__name__}），保留原图并依赖负面提示词兜底")
        logger.warning("单格提取失败 %s: %s", src.name, exc)
        return str(src), warnings


# ---- 角色资产完整性检测（缺陷四）----------------------------------------------

#: 触边判定：前景包围盒距图像边 < 该比例即视为"触边/裁切"
_EDGE_CONTACT_RATIO: float = 0.012
#: 缺脚判定：全身像前景包围盒底部应留有背景边距，且包围盒高度占比应足够
_FOOT_MARGIN_MIN_RATIO: float = 0.02
#: 全身像判定：前景包围盒高度占图像高度比例下限（半身像会低于此值，跳过脚部检测）
_FULLBODY_MIN_HEIGHT_RATIO: float = 0.75
#: 背景判定：与背景色差小于该值视为背景像素（0-255 灰度）
_BG_TOLERANCE: float = 26.0


def check_edge_contact(image_path: str | Path) -> dict[str, Any]:
    """检测角色资产图的轮廓触边与脚部完整性（缺陷四验收项）。

    算法：以四角中位色作为背景基色，对"非背景像素"求包围盒：
    - ``edge_contact``：包围盒四边任一边距 < ``_EDGE_CONTACT_RATIO`` → 触边；
    - ``missing_feet``：包围盒高度占比 ≥ ``_FULLBODY_MIN_HEIGHT_RATIO``
      （判定为全身像）但底部边距 < ``_FOOT_MARGIN_MIN_RATIO`` → 缺脚部
      留白（脚部被裁切的高危信号）。

    Args:
        image_path: 资产图片磁盘路径。

    Returns:
        ``{checked, edge_contact, missing_feet, bbox_margin}``；
        检测不可用时 ``checked=False``（QC 按通过处理，不阻塞）。
    """
    path = Path(image_path)
    result: dict[str, Any] = {
        "checked": False,
        "edge_contact": False,
        "missing_feet": False,
        "bbox_margin": None,
    }
    if not path.exists() or not path.is_file():
        result["edge_contact"] = False
        result["reason"] = "image_missing"
        return result
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            # 长边压到 512 控制 CPU 成本
            longest = max(width, height)
            if longest > 512:
                scale = 512 / float(longest)
                rgb = rgb.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale)))
                )
            arr = np.asarray(rgb, dtype="float32")
    except (OSError, ValueError, ImportError) as exc:
        logger.debug("触边检测跳过 %s: %s", path.name, exc)
        result["reason"] = f"detect_error:{type(exc).__name__}"
        return result

    height_px, width_px = arr.shape[0], arr.shape[1]
    corners = [arr[0, 0], arr[0, -1], arr[-1, 0], arr[-1, -1]]
    bg_color = np.median(np.stack(corners), axis=0)
    diff = np.abs(arr - bg_color[None, None, :]).max(axis=2)
    fg_mask = diff > _BG_TOLERANCE
    ys, xs = np.nonzero(fg_mask)
    if len(xs) < 32:
        # 无有效前景（纯背景/透明占位）→ 不判定缺陷
        result["checked"] = True
        result["reason"] = "no_foreground"
        return result
    margin = {
        "left": int(xs.min()),
        "right": int(width_px - 1 - xs.max()),
        "top": int(ys.min()),
        "bottom": int(height_px - 1 - ys.max()),
    }
    edge_contact = min(margin.values()) < max(2, int(width_px * _EDGE_CONTACT_RATIO))
    bbox_h_ratio = (ys.max() - ys.min() + 1) / float(height_px)
    missing_feet = (
        bbox_h_ratio >= _FULLBODY_MIN_HEIGHT_RATIO
        and margin["bottom"] < max(2, int(height_px * _FOOT_MARGIN_MIN_RATIO))
    )
    result.update({
        "checked": True,
        "edge_contact": bool(edge_contact),
        "missing_feet": bool(missing_feet),
        "bbox_margin": margin,
        "bbox_h_ratio": round(float(bbox_h_ratio), 3),
    })
    return result


# ---- prompt 诱发词清洗 ---------------------------------------------------------

#: 诱发多宫格/分屏画面的 prompt 词汇（命中后删除或改写）
_GRID_INDUCING_TERMS: tuple[tuple[str, str], ...] = (
    ("multi-view", "single view"),
    ("multiview", "single view"),
    ("multiple views", "single view"),
    ("three views", "single view"),
    ("three view", "single view"),
    ("turnaround sheet", "single full-body view"),
    ("expression sheet", "single expression"),
    ("collage", "single scene"),
    ("split screen", "full screen"),
    ("multiple panels", "single panel"),
    ("multiple angles", "single angle"),
    ("storyboard layout", "single shot"),
    ("多视图", "单视图"),
    ("三视图", "全身单视角"),
    ("多角度", "单角度"),
    ("表情集", "单表情"),
    ("九宫格", "单格"),
    ("宫格", "单格"),
    ("拼图", "单画面"),
    ("分屏", "满屏"),
    ("多画面", "单画面"),
)


def sanitize_prompt_terms(text: str | None) -> str:
    """清洗生成 prompt 中诱发多宫格/分屏画面的词汇。

    Args:
        text: 原始 prompt 文本（可空）。

    Returns:
        清洗后的 prompt；诱发词整体删除并替换为单视角等价描述。
    """
    blob = str(text or "")
    for bad, good in _GRID_INDUCING_TERMS:
        if bad in blob:
            blob = blob.replace(bad, good)
    return blob


#: 统一附加到视频生成的宫格类负面提示词（拼入网关 negative_prompt）
GRID_NEGATIVE_PROMPT: str = (
    "split screen, grid, collage, multiple panels, storyboard layout, "
    "expression sheet, multi-view, multiple angles, 分屏, 网格拼图, 多宫格, 多画面"
)

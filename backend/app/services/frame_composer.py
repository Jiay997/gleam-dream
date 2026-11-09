"""镜头整幅要素画面的本地合成器（Pillow 主路径 + ffmpeg 兜底，0 积分）。

职责：把 ``CompositionSpec`` 描述的场景基底与角色/道具合成为一张
竖屏 PNG。主路径使用 :mod:`app.services.frame_image_ops` 的软抠像与
置入算法（干净边缘、接地阴影、道具特写整幅呈现）；当 Pillow/NumPy
不可用或图像异常时，回退到历史 ffmpeg ``colorkey`` 实现，保证链路
不中断。两种实现共用 :class:`FrameComposer` 协议，调用方无需感知。

为什么需要两套实现：生产镜像可能未安装 Pillow；而纯 ffmpeg 的
``colorkey`` 无法处理影棚灰米色渐变背景，会产出白边与半透明残块。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger("gleamdream.services.frame_composer")

#: 抠像参数：先转 rgba（PNG 常为 rgb24，colorkey 需要 alpha 通道才能工作），
#: 再按颜色抠白并做边缘混合。
#: 相似度取 0.40：实测 0.14 无法抠掉白墙棚拍的浅灰阴影，会留下大片半透明
#: 残影并造成"双重曝光"穿帮；0.40 能在保留人物轮廓的同时清掉阴影。
_KEY_FILTER = "format=rgba,colorkey=0xFFFFFF:0.40:0.08"

#: 前景覆盖率低于该值视为抠像失败（素材非白底）
_MIN_COVERAGE = 0.05

#: 人物最大宽度占比（主/次角色）：不加宽度上限时，方形定妆照按屏高缩放会
#: 横向溢出并与相邻角色重叠，形成"两个人糊在一起"的穿帮
_MAX_WIDTH_MAIN = 0.46
#: 次角色最大宽度占比
_MAX_WIDTH_SECONDARY = 0.42
#: 道具最大宽度占比
_MAX_WIDTH_PROP = 0.28

#: 深色渐变底（无场景素材时使用）
_FALLBACK_BASE = "0x16181D"


@dataclass
class LayerImage:
    """一张待叠加的前景图层（磁盘路径已解析）。

    Attributes:
        asset_id: 图层来源资产 ID。
        name: 图层名称（角色名/道具名）。
        path: 图片磁盘绝对路径。
        kind: 图层类型（character/prop）。
    """

    asset_id: int
    name: str
    path: Path
    kind: str


@dataclass
class ComposeResult:
    """合成结果。

    Attributes:
        png: PNG 文件字节。
        width: 输出宽度（像素）。
        height: 输出高度（像素）。
        degraded: 降级标记列表（no_setting/card_layout/no_subject/cutout_failed…）。
        placed: 成功置入的图层名称列表。
        renderer: 实际使用的合成实现（pillow/ffmpeg），便于审计。
    """

    png: bytes
    width: int
    height: int
    degraded: list[str] = field(default_factory=list)
    placed: list[str] = field(default_factory=list)
    renderer: str = "pillow"


class FrameComposer(Protocol):
    """合成器协议：本地实现与模型精修实现共用同一签名。"""

    def compose(
        self,
        *,
        spec: object,
        setting_path: Path | None,
        layers: list[LayerImage],
    ) -> ComposeResult:
        """把 spec 与图层合成为一张整幅画面。

        Args:
            spec: CompositionSpec 实例（含画布/坐标/安全区）。
            setting_path: 场景基底图片路径（可空）。
            layers: 前景图层列表。

        Returns:
            合成结果。

        Raises:
            RuntimeError: 合成失败。
        """
        ...


def _run_ffmpeg(command: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """执行 ffmpeg 并返回结果（统一异常语义）。

    Args:
        command: 完整命令行参数列表。
        timeout: 超时秒数。

    Returns:
        已完成进程对象。

    Raises:
        RuntimeError: ffmpeg 不存在或执行返回非零。
    """
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg 不可用")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        raise RuntimeError(f"ffmpeg 执行失败: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "")[-500:]
        raise RuntimeError(f"ffmpeg 返回 {result.returncode}: {detail}")
    return result


def alpha_coverage(image_path: Path) -> float:
    """估算白底抠像后的前景覆盖率（ffmpeg 兜底路径使用）。

    Args:
        image_path: 待检测图片路径。

    Returns:
        覆盖率（0~1）；无法解析时返回 0.0。

    Raises:
        无：ffmpeg 不可用或解析失败一律返回 0.0，由调用方降级。
    """
    command = [
        "ffmpeg", "-hide_banner", "-v", "info", "-i", str(image_path),
        "-vf", f"{_KEY_FILTER},alphaextract,signalstats,metadata=print",
        "-frames:v", "1", "-f", "null", "-",
    ]
    try:
        result = _run_ffmpeg(command, timeout=60)
    except RuntimeError:
        return 0.0
    output = f"{result.stdout or ''}\n{result.stderr or ''}"
    match = re.search(r"lavfi\.signalstats\.YAVG=([0-9.]+)", output)
    if match is None:
        return 0.0
    return max(0.0, min(1.0, float(match.group(1)) / 255.0))


def _xy_expr(placement_x: float, placement_y: float | None, anchor: str, safe_bottom: float) -> tuple[str, str]:
    """把相对坐标转换为 overlay 表达式（ffmpeg 兜底路径使用）。

    Args:
        placement_x: 前景中心横坐标（0~1）。
        placement_y: 前景中心纵坐标（0~1）；底部对齐时忽略。
        anchor: 垂直锚点（bottom/center/top）。
        safe_bottom: 底部安全区比例。

    Returns:
        ``(x 表达式, y 表达式)``。

    Raises:
        无。
    """
    x_expr = f"W*{placement_x:.4f}-w/2"
    if anchor == "bottom":
        y_expr = f"H*{1.0 - safe_bottom:.4f}-h"
    elif anchor == "top":
        y_expr = f"-H*0.04"
    else:
        center_y = 0.5 if placement_y is None else placement_y
        y_expr = f"H*{center_y:.4f}-h/2"
    return x_expr, y_expr


class LocalFrameComposer:
    """基于本地图像的合成器（0 积分，CPU 合成）。

    主路径：Pillow 软抠像 + 接地阴影 + 场景调色（见 frame_image_ops）；
    兜底路径：ffmpeg colorkey + overlay（历史实现）。图层顺序为
    场景基底 → 远角色 → 近角色，道具特写时整幅改为道具画面。
    """

    def compose(
        self,
        *,
        spec: object,
        setting_path: Path | None,
        layers: list[LayerImage],
    ) -> ComposeResult:
        """合成一张整幅要素画面（Pillow 优先，失败回退 ffmpeg）。

        Args:
            spec: CompositionSpec 实例（鸭子类型读取 canvas/focus/safe_area）。
            setting_path: 场景基底图片路径；None 时用深色底并标记 no_setting。
            layers: 前景图层列表（角色与道具）。

        Returns:
            合成结果（含 PNG 字节、降级标记与实际渲染器）。

        Raises:
            RuntimeError: ffmpeg 不可用且 Pillow 路径也失败。
        """
        try:
            return self._compose_pillow(spec=spec, setting_path=setting_path, layers=layers)
        except Exception as exc:  # noqa: BLE001 - 图像库异常统一回退 ffmpeg
            logger.warning("Pillow 合成失败，回退 ffmpeg：%s", exc, exc_info=True)
            try:
                return self._compose_ffmpeg(spec=spec, setting_path=setting_path, layers=layers)
            except RuntimeError as fallback_error:
                raise RuntimeError(
                    f"合成失败：Pillow={type(exc).__name__}: {exc}; ffmpeg={fallback_error}"
                ) from fallback_error

    def _compose_pillow(
        self,
        *,
        spec: object,
        setting_path: Path | None,
        layers: list[LayerImage],
    ) -> ComposeResult:
        """Pillow 主路径：软抠像 + 规则化置入。

        Args:
            spec: 构图规格。
            setting_path: 场景图路径（可空）。
            layers: 前景图层。

        Returns:
            合成结果。

        Raises:
            RuntimeError: 画布非法或图像全部不可用。
        """
        # 延迟导入：无 Pillow/NumPy 的生产环境仍可加载本模块并走 ffmpeg 兜底
        from app.services.frame_image_ops import (
            READABLE_TEXT_PROP_MARKERS,
            compose_scene,
            cutout_subject,
            detect_panel_artifact,
            load_prop_full,
            soften_readable_text,
        )

        canvas = getattr(spec, "canvas")
        width = int(canvas["w"])
        height = int(canvas["h"])
        degraded: list[str] = []
        placed: list[str] = []

        focus = str(getattr(spec, "focus", "character"))
        prop_placements = list(getattr(spec, "props", []) or [])
        prop_layer = None
        prop_cards: list[tuple[object, object]] = []

        def _prepare_prop(image: object, asset_name: str) -> object:
            """对"含可读文字"的道具图做虚化，避免乱码进入首帧参考。

            Args:
                image: 道具 RGB 图（PIL）。
                asset_name: 道具资产名。

            Returns:
                处理后的图；非文字类道具原样返回。
            """
            if image is None:
                return image
            if any(marker in asset_name for marker in READABLE_TEXT_PROP_MARKERS):
                return soften_readable_text(image)
            return image

        if focus == "prop" and prop_placements:
            target = prop_placements[0]
            source = next(
                (layer for layer in layers if layer.asset_id == int(getattr(target, "asset_id"))),
                None,
            )
            if source is not None:
                prop_layer = load_prop_full(source.path)
                if prop_layer is not None:
                    prop_layer = _prepare_prop(prop_layer, str(getattr(source, "name", "") or ""))
                    placed.append(source.name)
            if prop_layer is None:
                degraded.append("missing_prop")
        else:
            # 叙事道具（锅/碗/照片）不能从画面里消失：非特写镜以右下角
            # 静物卡呈现，保证"台词里在吃面、画面里真有碗"。
            for target in prop_placements:
                source = next(
                    (layer for layer in layers if layer.asset_id == int(getattr(target, "asset_id"))),
                    None,
                )
                image = load_prop_full(source.path) if source is not None else None
                if image is None:
                    degraded.append("missing_prop")
                    continue
                image = _prepare_prop(image, str(getattr(source, "name", "") or ""))
                if detect_panel_artifact(image):
                    degraded.append(f"prop_artifact_skipped:{source.name}")
                    continue
                prop_cards.append((image, target))
                if source.name not in placed:
                    placed.append(source.name)

        subject_layers: list[tuple[object, object, bool, bool]] = []
        depth_rank = {"far": 0, "mid": 1, "near": 2}
        subjects = list(getattr(spec, "subjects", []) or [])
        for placement in subjects:
            source = next(
                (layer for layer in layers if layer.asset_id == int(getattr(placement, "asset_id"))),
                None,
            )
            if source is None:
                continue
            try:
                cutout = cutout_subject(source.path)
            except RuntimeError:
                cutout = None
            if cutout is not None and cutout.coverage >= 0.03:
                # 2026-11 QA 修复（首帧人体结构崩坏）：抠像可能把头部/上半身
                # 误判成背景删掉，贴进场景后就成了「无头悬浮手臂」（镜 10 实测）。
                # 顶部空洞过大或包围盒明显偏矮时判定为残缺抠像，降级为相卡排版
                # （观感一般但不会出现畸形人体）。
                partial = (
                    getattr(cutout, "top_gap_ratio", 0.0) > 0.34
                    or getattr(cutout, "bbox_height_ratio", 1.0) < 0.45
                )
                artifact = detect_panel_artifact(cutout.image)
                if artifact:
                    # Q-002：抠像结果仍带大面积黑块/白底 → 直接跳过该图层，
                    # 不再退回“整图相卡”，避免把黑块/白底贴进首帧。
                    degraded.append(
                        f"layer_artifact_skipped:{source.name}:{','.join(artifact)}"
                    )
                    continue
                if not partial:
                    subject_layers.append((cutout.image, placement, cutout.bust, False))
                    placed.append(source.name)
                    continue
                if "cutout_partial" not in degraded:
                    degraded.append("cutout_partial")
                degraded.append(f"cutout_partial:{source.name}")
            # 抠像失败/覆盖率过低：整图退化为相卡排版（显式标记，QC 可见）
            card = load_prop_full(source.path)
            if card is None:
                degraded.append(f"cutout_failed:{source.name}")
                continue
            if detect_panel_artifact(card):
                # Q-002：白底/黑块整图不得作为相卡贴入画面。
                degraded.append(f"card_artifact_skipped:{source.name}")
                continue
            if "card_layout" not in degraded:
                degraded.append("card_layout")
            subject_layers.append((card, placement, False, True))
            placed.append(source.name)
        subject_layers.sort(key=lambda item: depth_rank.get(str(getattr(item[1], "depth", "mid")), 1))

        if not subject_layers and prop_layer is None and setting_path is None:
            raise RuntimeError("无场景、无可用人物、无道具，无法合成")
        if not subject_layers and prop_layer is None:
            degraded.append("no_subject")
        if setting_path is None and prop_layer is None:
            degraded.append("no_setting")

        image = compose_scene(
            setting_path=setting_path,
            subject_layers=subject_layers,
            prop_layer=prop_layer,
            prop_cards=prop_cards,
            spec=spec,
        )
        # Q-002：成片首帧兜底检测，命中黑块/白底贴图时显式标记，供 QC 拦截。
        final_artifact = detect_panel_artifact(image, ignore_transparent=False)
        if final_artifact:
            degraded.append(f"visual_artifact:{','.join(final_artifact)}")
        with tempfile.TemporaryDirectory(prefix="frame_compose_") as tmp:
            output = Path(tmp) / "frame.png"
            image.save(output, format="PNG")
            png = output.read_bytes()
        return ComposeResult(
            png=png,
            width=width,
            height=height,
            degraded=degraded,
            placed=placed,
            renderer="pillow",
        )

    def _compose_ffmpeg(
        self,
        *,
        spec: object,
        setting_path: Path | None,
        layers: list[LayerImage],
    ) -> ComposeResult:
        """ffmpeg 兜底路径（历史实现，保留用于无 Pillow 环境）。

        Args:
            spec: 构图规格。
            setting_path: 场景图路径（可空）。
            layers: 前景图层。

        Returns:
            合成结果。

        Raises:
            RuntimeError: ffmpeg 不可用或滤镜链执行失败。
        """
        canvas = getattr(spec, "canvas")
        width = int(canvas["w"])
        height = int(canvas["h"])
        safe_bottom = float(getattr(spec, "safe_area")["bottom"])
        subjects = list(getattr(spec, "subjects"))
        props = list(getattr(spec, "props"))
        focus = str(getattr(spec, "focus", "character"))

        degraded: list[str] = []
        placed: list[str] = []

        inputs: list[str] = []
        if setting_path is not None and Path(setting_path).exists():
            inputs += ["-i", str(setting_path)]
        else:
            inputs += ["-f", "lavfi", "-i", f"color=c={_FALLBACK_BASE}:s={width}x{height}"]
            degraded.append("no_setting")

        ordered_layers: list[tuple[LayerImage, object, str]] = []
        if focus == "prop" and props:
            target = props[0]
            match = next(
                (layer for layer in layers if layer.asset_id == int(getattr(target, "asset_id"))),
                None,
            )
            if match is not None:
                ordered_layers.append((match, target, "prop"))
        else:
            for subject in subjects:
                match = next(
                    (layer for layer in layers if layer.asset_id == int(getattr(subject, "asset_id"))),
                    None,
                )
                if match is not None:
                    ordered_layers.append((match, subject, "subject"))
        depth_rank = {"far": 0, "mid": 1, "near": 2}
        ordered_layers.sort(
            key=lambda item: depth_rank.get(str(getattr(item[1], "depth", "mid")), 1)
        )
        if not ordered_layers:
            degraded.append("no_subject")

        filter_parts = [
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},eq=saturation=0.92:brightness=-0.015,"
            f"gblur=sigma=1.2[bg]"
        ]
        for index, (layer, placement, kind) in enumerate(ordered_layers, start=1):
            inputs += ["-i", str(layer.path)]
            target_h = int(round(height * float(getattr(placement, "height_ratio", 0.4))))
            target_h = max(2, min(target_h, height * 2))
            if kind == "prop":
                width_ratio = _MAX_WIDTH_PROP
            elif index == 1:
                width_ratio = _MAX_WIDTH_MAIN
            else:
                width_ratio = _MAX_WIDTH_SECONDARY
            max_w = max(2, int(width * width_ratio))
            coverage = alpha_coverage(Path(layer.path))
            if coverage < _MIN_COVERAGE:
                filter_parts.append(
                    f"[{index}:v]scale=w='min({max_w}\\,iw*{target_h}/ih)':h=-1,"
                    f"format=rgba,drawbox=x=0:y=0:w=iw:h=ih:color=white@0.22:t=10[fg{index}]"
                )
                if "card_layout" not in degraded:
                    degraded.append("card_layout")
            else:
                filter_parts.append(
                    f"[{index}:v]{_KEY_FILTER},"
                    f"scale=w='min({max_w}\\,iw*{target_h}/ih)':h=-1,format=rgba[fg{index}]"
                )
            x_expr, y_expr = _xy_expr(
                float(getattr(placement, "x", 0.5)),
                float(getattr(placement, "y", 0.5)) if kind == "prop" else None,
                str(getattr(placement, "anchor", "bottom")),
                safe_bottom,
            )
            source = "[bg]" if index == 1 else f"[t{index - 1}]"
            filter_parts.append(
                f"{source}[fg{index}]overlay=x='{x_expr}':y='{y_expr}'[t{index}]"
            )
            placed.append(layer.name)

        last = f"[t{len(ordered_layers)}]" if ordered_layers else "[bg]"
        filter_parts.append(f"{last}vignette=PI/5,format=rgb24[out]")

        with tempfile.TemporaryDirectory(prefix="frame_compose_") as tmp:
            output = Path(tmp) / "frame.png"
            command = ["ffmpeg", "-y", "-v", "error", *inputs,
                       "-filter_complex", ";".join(filter_parts),
                       "-map", "[out]", "-frames:v", "1", str(output)]
            _run_ffmpeg(command)
            if not output.exists():
                raise RuntimeError("合成失败：未生成输出文件")
            png = output.read_bytes()

        return ComposeResult(
            png=png,
            width=width,
            height=height,
            degraded=degraded,
            placed=placed,
            renderer="ffmpeg",
        )

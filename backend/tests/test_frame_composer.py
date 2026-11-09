"""本地合成器集成测试（真实 ffmpeg，不依赖数据库与模型网关）。

测试素材由 ffmpeg lavfi 现场生成：白底人形块、白底道具块、深色场景。
断言画布尺寸、图层确实叠加（亮度跨度变大）、纯白素材降级为卡片排版。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.frame_composer import LayerImage, LocalFrameComposer, alpha_coverage
from app.services.shot_frame_spec import (
    AssetRef,
    CompositionSpec,
    derive_composition_spec,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="本机未安装 ffmpeg"
)


def _make_image(path: Path, *, size: str, box: str | None = None) -> Path:
    """用 ffmpeg lavfi 生成测试图片。

    Args:
        path: 输出文件路径。
        size: lavfi 尺寸（如 ``400x600``）。
        box: 可选 drawbox 参数（``x:y:w:h:color``），用于造前景。

    Returns:
        生成的文件路径。

    Raises:
        subprocess.CalledProcessError: ffmpeg 生成失败。
    """
    filters = []
    if box:
        x, y, w, h, color = box.split(":")
        filters.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill")
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=white:s={size}",
    ]
    if filters:
        command += ["-vf", ",".join(filters)]
    command += ["-frames:v", "1", str(path)]
    subprocess.run(command, check=True, timeout=60, capture_output=True)
    return path


def _probe_size(path: Path) -> tuple[int, int]:
    """读取图片宽高。

    Args:
        path: 图片路径。

    Returns:
        ``(width, height)`` 像素值。

    Raises:
        RuntimeError: ffprobe 无法解析尺寸。
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
        ],
        check=True, timeout=60, capture_output=True, text=True,
    )
    width, height = str(result.stdout).strip().split(",")[:2]
    return int(width), int(height)


def _luma_span(path: Path) -> float:
    """读取图片亮度最大最小值之差（用于判断是否纯色）。

    Args:
        path: 图片路径。

    Returns:
        亮度跨度（0~255）；解析失败返回 0。

    Raises:
        subprocess.CalledProcessError: ffmpeg 执行失败。
    """
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "info", "-i", str(path),
            "-vf", "signalstats,metadata=print", "-frames:v", "1", "-f", "null", "-",
        ],
        check=True, timeout=60, capture_output=True, text=True,
    )
    values = {}
    for line in str(result.stderr).splitlines():
        for key in ("YMIN", "YMAX"):
            if f"lavfi.signalstats.{key}=" in line:
                values[key] = float(line.split("=", 1)[1])
    if "YMIN" not in values or "YMAX" not in values:
        return 0.0
    return values["YMAX"] - values["YMIN"]


def _spec(**overrides) -> CompositionSpec:
    """构造用于合成测试的 spec。

    Args:
        **overrides: 覆盖默认参数。

    Returns:
        合法的 CompositionSpec。
    """
    kwargs = {
        "shot_no": 1,
        "shot_size": "medium",
        "camera_move": "static",
        "action_desc": "林晚站在便利店货架前",
        "dialogue": "",
        "aspect_ratio": "9:16",
        "characters": [AssetRef(asset_id=11, name="林晚")],
        "scene": AssetRef(asset_id=21, name="便利店", role="scene"),
        "props": [],
    }
    kwargs.update(overrides)
    return derive_composition_spec(**kwargs)


def test_alpha_coverage_high_for_white_bg_subject(tmp_path: Path) -> None:
    """白底人形素材抠像后仍有可观前景覆盖率。"""
    fg = _make_image(tmp_path / "fg.png", size="400x600", box="140:80:120:460:red")
    assert alpha_coverage(fg) > 0.10


def test_alpha_coverage_near_zero_for_all_white(tmp_path: Path) -> None:
    """纯白素材抠像后覆盖率接近 0（用于触发降级）。"""
    blank = _make_image(tmp_path / "blank.png", size="400x600")
    assert alpha_coverage(blank) < 0.02


def test_compose_produces_vertical_canvas_with_layers(tmp_path: Path) -> None:
    """合成结果为 720×1280，且亮度跨度证明图层已叠加。"""
    scene = _make_image(tmp_path / "scene.png", size="1280x720")
    fg = _make_image(tmp_path / "fg.png", size="400x600", box="140:80:120:460:red")
    prop = _make_image(tmp_path / "prop.png", size="200x200", box="60:60:80:80:blue")
    spec = _spec(props=[AssetRef(asset_id=31, name="手写纸条", role="prop")])

    result = LocalFrameComposer().compose(
        spec=spec,
        setting_path=scene,
        layers=[
            LayerImage(asset_id=11, name="林晚", path=fg, kind="character"),
            LayerImage(asset_id=31, name="手写纸条", path=prop, kind="prop"),
        ],
    )

    assert (result.width, result.height) == (720, 1280)
    out = tmp_path / "out.png"
    out.write_bytes(result.png)
    assert _probe_size(out) == (720, 1280)
    assert _luma_span(out) > 20
    assert result.degraded == []
    assert "林晚" in result.placed


def test_compose_degrades_to_card_when_key_fails(tmp_path: Path) -> None:
    """纯白前景（抠像无主体）降级为卡片排版并显式记录。"""
    scene = _make_image(tmp_path / "scene.png", size="1280x720")
    blank = _make_image(tmp_path / "blank.png", size="400x600")
    spec = _spec()

    result = LocalFrameComposer().compose(
        spec=spec,
        setting_path=scene,
        layers=[LayerImage(asset_id=11, name="林晚", path=blank, kind="character")],
    )

    assert "card_layout" in result.degraded


def test_compose_without_setting_uses_gradient_base(tmp_path: Path) -> None:
    """无场景素材时用深色底，仍产出合法画布。"""
    fg = _make_image(tmp_path / "fg.png", size="400x600", box="140:80:120:460:red")
    spec = _spec(scene=None)

    result = LocalFrameComposer().compose(
        spec=spec,
        setting_path=None,
        layers=[LayerImage(asset_id=11, name="林晚", path=fg, kind="character")],
    )

    assert (result.width, result.height) == (720, 1280)
    assert "no_setting" in result.degraded

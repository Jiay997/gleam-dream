"""沙箱占位（Stub）媒体提供商模块。

在未接入真实三方媒体模型前，以确定性方式生成占位产物并落本地存储：

- **image**：生成 SVG 占位图（渐变背景 + 中央「AI 占位图」文字 + 资产名/
  prompt 摘要水印 + 生成时间），``media_type`` 为 image/svg+xml；
- **video**：生成一段视频元数据 JSON 描述文件（duration_ms 2000~4000、fps、
  resolution、frames），并附带一张首帧 SVG 关键帧（``meta.poster_key``），
  ``meta.placeholder="video"``；
- **tts**：生成真实 WAV 文件（标准库 wave + math，16kHz 单声道 16bit 轻微
  嗡鸣），时长按文本长度估算（4 字/秒，上限 30s）；
- **bgm**：生成 30s WAV，``meta.duration_ms=30000``。

所有产物均落 :class:`~app.infra.storage.LocalStorage` 并返回
``object_key`` / ``media_url``，且 ``meta.simulated=True`` 标识占位性质。
"""

from __future__ import annotations

import colorsys
import hashlib
import io
import json
import logging
import math
import random
import shutil
import subprocess
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.sax.saxutils import escape

from app.core.ids import new_request_id
from app.infra.model_gateway.base import (
    CAPABILITY_BGM,
    CAPABILITY_IMAGE,
    CAPABILITY_TTS,
    CAPABILITY_VIDEO,
    GenRequest,
    GenResult,
    ModelProvider,
)

if TYPE_CHECKING:
    from app.infra.storage import LocalStorage
    from app.models import ModelRegistry

logger = logging.getLogger("gleamdream.gateway")

# TTS 时长估算：每秒 4 字
TTS_CHARS_PER_SECOND: int = 4
# TTS/朗读时长上下限（秒）
TTS_MIN_SECONDS: int = 1
TTS_MAX_SECONDS: int = 30
# BGM 固定时长（毫秒）
BGM_DURATION_MS: int = 30000
# WAV 采样率（Hz），单声道 16bit
WAV_SAMPLE_RATE: int = 16000
# 占位视频时长范围（毫秒）
VIDEO_DURATION_MS_MIN: int = 2000
VIDEO_DURATION_MS_MAX: int = 4000

# Stub 提供商支持的能力集合
_STUB_CAPABILITIES = {CAPABILITY_IMAGE, CAPABILITY_VIDEO, CAPABILITY_TTS, CAPABILITY_BGM}


def _now_iso() -> str:
    """获取当前 UTC 时间的 ISO 字符串（用于占位产物时间戳）。

    Returns:
        如 ``2026-10-08T12:00:00.000000+00:00`` 的字符串。
    """
    return datetime.now(timezone.utc).isoformat()


def _hue_from_text(text: str) -> float:
    """从文本派生稳定色相（0~360），保证同一 prompt 渐变配色一致。

    Args:
        text: 输入文本（prompt 或资产名）。

    Returns:
        0~360 的色相值。
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 360


def _hex_color(hue: float, lightness: float, saturation: float = 0.6) -> str:
    """将 HSL 颜色转换为十六进制字符串。

    Args:
        hue: 色相（0~360）。
        lightness: 亮度（0~1）。
        saturation: 饱和度（0~1），默认 0.6。

    Returns:
        如 ``#1a2b3c`` 的十六进制颜色。
    """
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, lightness, saturation)
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _parse_size(params: dict[str, Any], default_w: int, default_h: int) -> tuple[int, int]:
    """从参数字典解析宽高。

    支持 ``size``（"1024x1024" 字符串或 [w, h] 序列）与独立 ``width``/
    ``height`` 键；解析失败回退默认值；结果限制在 64~4096 像素。

    Args:
        params: 能力参数字典。
        default_w: 默认宽度。
        default_h: 默认高度。

    Returns:
        (width, height) 元组。
    """
    width, height = default_w, default_h
    size = params.get("size")
    if isinstance(size, str) and "x" in size.lower():
        left, _, right = size.lower().partition("x")
        try:
            width, height = int(left), int(right)
        except ValueError:
            pass
    elif isinstance(size, (list, tuple)) and len(size) == 2:
        try:
            width, height = int(size[0]), int(size[1])
        except (TypeError, ValueError):
            pass
    if params.get("width") is not None:
        try:
            width = int(params["width"])
        except (TypeError, ValueError):
            pass
    if params.get("height") is not None:
        try:
            height = int(params["height"])
        except (TypeError, ValueError):
            pass
    clamp = lambda v: max(64, min(4096, v))  # noqa: E731
    return clamp(width), clamp(height)


def _build_placeholder_svg(
    title: str,
    subtitle: str,
    footer: str,
    width: int,
    height: int,
    hue_seed: str,
) -> bytes:
    """构造占位 SVG 图片字节内容。

    P2-13 修复：占位图只使用渐变背景 + 几何图形，不再绘制任何文字/水印。
    历史占位图把「AI 占位图 / 资产名 / prompt 摘要 / 生成时间」直接画进画面，
    资产图落到镜头后画面里出现成片不该有的文字，人工验收误判为模型产出。
    标题等信息改为写入 ``<desc>``（不可见，仅审计可读）。

    Args:
        title: 中央标题（仅写入 desc，不渲染）。
        subtitle: 副标题（仅写入 desc，不渲染）。
        footer: 底部信息（仅写入 desc，不渲染）。
        width: 画布宽度（像素）。
        height: 画布高度（像素）。
        hue_seed: 用于派生渐变色相的种子文本。

    Returns:
        UTF-8 编码的 SVG 字节内容。
    """
    hue = _hue_from_text(hue_seed)
    color_start = _hex_color(hue, 0.32)
    color_end = _hex_color((hue + 45) % 360, 0.55)
    # 几何主体尺寸按画面短边派生，保持不同资产可区分
    short = max(64, min(width, height))
    head_r = short * 0.13
    body_w = short * 0.30
    body_h = short * 0.34
    desc_text = escape(f"{title} | {subtitle[:40]} | {footer}")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        "<defs>"
        f'<linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">'
        f'<stop offset="0%" stop-color="{color_start}"/>'
        f'<stop offset="100%" stop-color="{color_end}"/>'
        "</linearGradient>"
        "</defs>"
        '<rect width="100%" height="100%" fill="url(#bg)"/>'
        f"<desc>{desc_text}</desc>"
        f'<circle cx="{width * 0.5:.1f}" cy="{height * 0.36:.1f}" r="{head_r:.1f}" '
        'fill="#ffffff" opacity="0.18"/>'
        f'<rect x="{width * 0.5 - body_w / 2:.1f}" y="{height * 0.36 + head_r:.1f}" '
        f'width="{body_w:.1f}" height="{body_h:.1f}" rx="{body_w * 0.3:.1f}" '
        'fill="#ffffff" opacity="0.12"/>'
        "</svg>"
    )
    return svg.encode("utf-8")


def _build_wav_bytes(duration_seconds: float, frequency: float, amplitude: float = 0.05) -> bytes:
    """生成指定时长的低幅正弦嗡鸣 WAV 字节内容。

    使用标准库 wave + math：16kHz 采样率、单声道、16bit PCM，波形为
    轻微嗡鸣正弦（振幅默认满幅的 5%，接近静音但可被播放器识别）。

    Args:
        duration_seconds: 时长（秒，支持小数，用于体现语速差异）。
        frequency: 正弦频率（Hz），如 TTS 220 / BGM 110。
        amplitude: 振幅比例（0~1），默认 0.05。

    Returns:
        完整 WAV 文件字节内容。

    Raises:
        无（参数已被调用方约束）。
    """
    total_frames = max(1, int(round(float(duration_seconds) * WAV_SAMPLE_RATE)))
    max_amplitude = 32767 * amplitude
    samples = (
        int(max_amplitude * math.sin(2.0 * math.pi * frequency * i / WAV_SAMPLE_RATE))
        for i in range(total_frames)
    )
    from array import array

    pcm = array("h", samples)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(WAV_SAMPLE_RATE)
        wav_file.writeframes(pcm.tobytes())
    return buffer.getvalue()


def _motion_filter(camera_move: str, seconds: float, fps: int, width: int, height: int) -> str:
    """按运镜语义生成 zoompan 表达式（推/拉/横移/手持/固定）。

    修复点：历史所有镜头只有同一种极慢推，成片像"定点图片轮播"。本函数
    让 dolly_in / dolly_out / tracking / handheld 各自产生可辨识的运动，
    同时保留 3% 的微小推拉，避免完全静止。

    Args:
        camera_move: 镜头运镜枚举（dolly_in/dolly_out/tracking/handheld/static）。
        seconds: 视频时长（秒）。
        fps: 帧率。
        width: 输出宽度。
        height: 输出高度。

    Returns:
        可直接拼入 ``-vf`` 的 zoompan 片段。

    Raises:
        无：未知运镜按 static 处理。
    """
    frames = max(1, int(round(seconds * fps)))
    progress = f"on/{frames}"
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    move = str(camera_move or "static").lower()
    if move == "dolly_in":
        zoom = f"1.0+0.10*{progress}"
        x_expr, y_expr = center_x, center_y
    elif move == "dolly_out":
        zoom = f"max(1.10-0.10*{progress},1.0)"
        x_expr, y_expr = center_x, center_y
    elif move == "tracking":
        zoom = "1.06"
        x_expr = f"(iw-iw/zoom)*(0.18+0.64*{progress})"
        y_expr = center_y
    elif move == "handheld":
        zoom = "1.04"
        x_expr = f"{center_x}+sin(on/7)*3"
        y_expr = f"{center_y}+cos(on/9)*3"
    else:
        zoom = f"min(1.03,1.0+0.03*{progress})"
        x_expr, y_expr = center_x, center_y
    return (
        f"zoompan=z='{zoom}':x='{x_expr}':y='{y_expr}':"
        f"d=1:s={width}x{height}:fps={fps}"
    )


def _build_placeholder_mp4(
    width: int,
    height: int,
    duration_ms: int,
    fps: int,
    *,
    image_path: Path | None = None,
    camera_move: str = "static",
) -> bytes | None:
    """用 ffmpeg 生成可播放的镜头视频（图片驱动 + 运镜）。

    当提供 ``image_path`` 时，以合成帧为首帧执行按运镜语义的推拉/平移/
    手持抖动，并保留 0.2s 淡入；未提供图片或图片不可解码时，回退为纯色
    占位视频，保证浏览器 ``<video>`` 始终可播放。

    Args:
        width: 视频宽度（像素，内部向下取偶）。
        height: 视频高度（像素，内部向下取偶）。
        duration_ms: 视频时长（毫秒，最少 1 秒）。
        fps: 帧率（正整数）。
        image_path: 可选的首帧图片绝对路径（PNG/JPEG/WebP）。
        camera_move: 运镜枚举（dolly_in/dolly_out/tracking/handheld/static）。

    Returns:
        MP4 文件字节；ffmpeg 不可用或生成失败时返回 None（调用方回退 JSON）。
    """
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg 不可用，视频占位回退 JSON")
        return None
    # yuv420p 要求宽高为偶数
    width = max(2, width - width % 2)
    height = max(2, height - height % 2)
    seconds = max(1.0, duration_ms / 1000)
    with tempfile.TemporaryDirectory(prefix="gd_stub_video_") as tmpdir:
        output = Path(tmpdir) / "placeholder.mp4"
        usable_image = (
            image_path is not None
            and image_path.exists()
            and image_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        )
        if usable_image:
            # 画幅统一：等比放大后居中裁剪，再按运镜执行 zoompan。
            # P0 修复（长镜头黑屏）：历史每镜都做 0.35s 淡出，合成层为容纳同镜
            # 多句台词会用 tpad 克隆「末帧」补足时长——而末帧恰是淡出到黑的帧，
            # 导致被拉长的镜头（如双人对白 6.3s）后半段整段黑屏。镜头在时间线
            # 上本就是硬切，不需要逐镜淡出，故仅保留 0.2s 淡入。
            motion = _motion_filter(camera_move, seconds, fps, width, height)
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},"
                f"{motion},"
                f"fade=t=in:st=0:d=0.20,"
                "format=yuv420p"
            )
            command = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-loop", "1", "-framerate", str(fps), "-i", str(image_path),
                "-t", f"{seconds:.2f}",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(output),
            ]
        else:
            command = [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi",
                "-i", f"color=c=0x14141f:s={width}x{height}:d={seconds:.2f}:r={fps}",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                str(output),
            ]
        try:
            subprocess.run(command, check=True, timeout=120, capture_output=True)
            return output.read_bytes()
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("镜头视频生成失败，回退 JSON：%s", exc)
            return None


class StubProvider(ModelProvider):
    """沙箱占位提供商：生成 SVG / JSON / WAV 占位产物并落本地存储。

    Attributes:
        _storage: 本地存储客户端（对象键即相对路径）。
    """

    def __init__(self, storage: LocalStorage) -> None:
        """构造占位提供商。

        Args:
            storage: 本地存储客户端实例。
        """
        self._storage = storage

    def supports(self, capability: str) -> bool:
        """判断是否支持指定能力。

        Args:
            capability: 能力名。

        Returns:
            image/video/tts/bgm 返回 True，其余（含 llm）False。
        """
        return capability in _STUB_CAPABILITIES

    async def generate(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """按能力分发生成占位产物。

        Args:
            request: 统一生成请求。
            model_spec: 路由命中的模型注册行。

        Returns:
            成功：ok=True，object_key/media_url 指向占位产物，
            meta.simulated=True；失败：ok=False，error 携带原因。
        """
        try:
            if request.capability == CAPABILITY_IMAGE:
                return self._generate_image(request, model_spec)
            if request.capability == CAPABILITY_VIDEO:
                return self._generate_video(request, model_spec)
            if request.capability == CAPABILITY_TTS:
                return self._generate_audio(request, model_spec, is_bgm=False)
            if request.capability == CAPABILITY_BGM:
                return self._generate_audio(request, model_spec, is_bgm=True)
            return GenResult(
                ok=False,
                error=f"Stub 提供商不支持能力: {request.capability}",
                meta={"provider": "stub", "model_key": model_spec.model_key},
            )
        except Exception as exc:  # 落盘失败等意外统一转为失败结果
            logger.exception("Stub 生成失败 capability=%s", request.capability)
            return GenResult(
                ok=False,
                error=f"Stub 生成失败: {type(exc).__name__}: {exc}",
                meta={"provider": "stub", "model_key": model_spec.model_key},
            )

    def _generate_image(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """生成 SVG 占位图并落存储。

        Args:
            request: 统一生成请求（params.size / width / height / asset_name）。
            model_spec: 模型注册行。

        Returns:
            成功结果：object_key 指向 SVG，meta 含 width/height/simulated。
        """
        width, height = _parse_size(request.params, 1024, 1024)
        asset_name = str(request.params.get("asset_name") or "未命名资产")
        summary = (request.prompt or asset_name).strip().replace("\n", " ")
        footer = f"{asset_name} · {_now_iso()}"
        data = _build_placeholder_svg("AI 占位图", summary, footer, width, height, summary or asset_name)
        object_key = self._storage.save_bytes(f"stub/images/{new_request_id().lower()}.svg", data)
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "stub",
                "model_key": model_spec.model_key,
                "simulated": True,
                "mime_type": "image/svg+xml",
                "width": width,
                "height": height,
                "asset_name": asset_name,
            },
        )

    def _generate_video(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """生成视频元数据 JSON 描述文件与首帧 SVG 关键帧。

        Args:
            request: 统一生成请求（params.duration_ms / fps / size）。
            model_spec: 模型注册行。

        Returns:
            成功结果：object_key 指向 JSON 描述文件，meta.placeholder="video"，
            meta.poster_key 指向首帧关键帧。
        """
        duration_ms = request.params.get("duration_ms")
        if not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
            duration_ms = random.randint(VIDEO_DURATION_MS_MIN, VIDEO_DURATION_MS_MAX)
        duration_ms = int(max(500, min(60000, duration_ms)))
        fps = int(request.params.get("fps") or 24)
        # P1 修复（竖屏构图）：历史默认 1280x720 横屏，与短剧 9:16 竖屏不符，
        # 成片合成时只能等比缩小并上下补黑边（人物极小、黑边占 2/3）。
        # 现按项目画幅推导默认尺寸，显式 size/width/height 仍优先。
        aspect = str(request.params.get("aspect_ratio") or "9:16")
        if aspect == "16:9":
            default_w, default_h = 1280, 720
        elif aspect == "1:1":
            default_w, default_h = 720, 720
        else:
            default_w, default_h = 720, 1280
        width, height = _parse_size(request.params, default_w, default_h)
        resolution = f"{width}x{height}"
        frames = max(1, round(duration_ms / 1000 * fps))
        summary = (request.prompt or "未提供提示词").strip().replace("\n", " ")
        poster_data = _build_placeholder_svg(
            "视频首帧 · 占位", summary, f"{resolution} · {_now_iso()}", width, height, summary,
        )
        poster_key = self._storage.save_bytes(
            f"stub/videos/{new_request_id().lower()}_poster.svg", poster_data,
        )
        # P0 修复：镜头视频优先由定妆图/场景图驱动，避免成片画面为纯色块。
        # executor 会把镜头首帧或锚点资产当前版本的媒体 key 放进 image_key；
        # 图片不可用时回退纯色源，仍保证浏览器可播放。
        image_key = str(request.params.get("image_key") or "").strip()
        image_path: Path | None = None
        if image_key:
            storage_key = image_key[len("/files/"):] if image_key.startswith("/files/") else image_key
            try:
                candidate = self._storage.object_path(storage_key)
                if candidate.exists() and candidate.is_file():
                    image_path = candidate
                    poster_key = storage_key
            except (OSError, ValueError):
                image_path = None
        metadata = {
            "placeholder": True,
            "simulated": True,
            "duration_ms": duration_ms,
            "fps": fps,
            "resolution": resolution,
            "frames": frames,
            "poster_key": poster_key,
            "prompt_summary": summary[:200],
            "generated_at": _now_iso(),
        }
        # P0-3 修复：优先产出可播放的 MP4（浏览器 <video> 可直接播放）；
        # ffmpeg 不可用或生成失败时才回退为历史 JSON 描述文件。
        camera_move = str(request.params.get("camera_move") or "static")
        mp4_bytes = _build_placeholder_mp4(
            width, height, duration_ms, fps, image_path=image_path,
            camera_move=camera_move,
        )
        if mp4_bytes is not None:
            object_key = self._storage.save_bytes(
                f"stub/videos/{new_request_id().lower()}.mp4", mp4_bytes,
            )
            return GenResult(
                ok=True,
                object_key=object_key,
                media_url=f"/files/{object_key}",
                meta={
                    "provider": "stub",
                    "model_key": model_spec.model_key,
                    "simulated": True,
                    "placeholder": "video",
                    "mime_type": "video/mp4",
                    "poster_key": poster_key,
                    "width": width,
                    "height": height,
                    "duration_ms": duration_ms,
                    "fps": fps,
                    "frames": frames,
                    "image_driven": image_path is not None,
                    "file_size_bytes": len(mp4_bytes),
                },
            )

        data = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        object_key = self._storage.save_bytes(f"stub/videos/{new_request_id().lower()}.json", data)
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "stub",
                "model_key": model_spec.model_key,
                "simulated": True,
                "placeholder": "video",
                "mime_type": "application/json",
                "poster_key": poster_key,
                "width": width,
                "height": height,
                "duration_ms": duration_ms,
                "fps": fps,
                "frames": frames,
            },
        )

    def _generate_audio(self, request: GenRequest, model_spec: ModelRegistry, is_bgm: bool) -> GenResult:
        """生成占位音频（TTS 朗读或 BGM 配乐）WAV 文件。

        TTS 时长按文本长度估算（4 字/秒，1~30s）；BGM 固定 30s。

        Args:
            request: 统一生成请求。
            model_spec: 模型注册行。
            is_bgm: True 生成 BGM（30s、低频嗡鸣），False 生成 TTS。

        Returns:
            成功结果：object_key 指向 WAV，meta.duration_ms 为实际时长。
        """
        if is_bgm:
            duration_seconds = BGM_DURATION_MS // 1000
            frequency = 110.0
        else:
            char_count = len((request.prompt or "").strip())
            duration_seconds = max(
                TTS_MIN_SECONDS, min(TTS_MAX_SECONDS, math.ceil(char_count / TTS_CHARS_PER_SECOND)),
            )
            frequency = 220.0
        data = _build_wav_bytes(duration_seconds, frequency)
        object_key = self._storage.save_bytes(f"stub/audio/{new_request_id().lower()}.wav", data)
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "stub",
                "model_key": model_spec.model_key,
                "simulated": True,
                "mime_type": "audio/wav",
                "duration_ms": duration_seconds * 1000,
                "sample_rate": WAV_SAMPLE_RATE,
                "channels": 1,
                "kind": "bgm" if is_bgm else "tts",
            },
        )

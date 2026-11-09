"""千问 AI · Qwen3-TTS 提供商（HTTP 同步语音合成）。

对接千问 AI（DashScope）的 ``qwen3-tts-flash`` 模型：

- ``POST /api/v1/services/aigc/multimodal-generation/generation``
- 请求体：``{"model", "input": {"text", "voice"}, "parameters": {"language_type"}}``
- 响应：``output.audio.url``（WAV 地址）或 ``output.audio.data``（Base64）

与百炼 ``BailianProvider`` 的 WebSocket cosyvoice 协议不同，本提供商使用
同步 HTTP + 音频 URL 下载，因此独立成类；语音路由优先指向本提供商，
百炼保留为降级候选。

参考文档：https://www.qianwenai.com/models/qwen3-tts-flash
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from app.core.config import settings
from app.core.ids import new_request_id
from app.infra.model_gateway.base import (
    CAPABILITY_TTS,
    GenRequest,
    GenResult,
    ModelProvider,
)

if TYPE_CHECKING:
    from app.infra.storage import LocalStorage
    from app.models import ModelRegistry

logger = logging.getLogger("gleamdream.gateway.qwen_tts")

#: 语音合成 HTTP 超时（秒）
_HTTP_TIMEOUT = 120.0
#: 音频下载超时（秒）
_DOWNLOAD_TIMEOUT = 60.0
#: 合成端点路径（DashScope 原生多模态生成）
_TTS_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
#: 默认语种（本项目为中文短剧）
_DEFAULT_LANGUAGE = "Chinese"
#: 情绪 → (语速倍率, 音调半音) 映射（P1-11：情绪参数必须真正影响音频）
_EMOTION_ADJUST: dict[str, tuple[float, int]] = {
    "joy": (1.08, 1),
    "romance": (0.96, 1),
    "warm": (0.96, 0),
    "calm": (0.94, 0),
    "tension": (1.06, 1),
    "fear": (1.08, 2),
    "anger": (1.10, 2),
    "surprise": (1.10, 2),
    "sadness": (0.90, -1),
    "sad": (0.90, -1),
}
#: 语速/音调合法范围
_RATE_RANGE: tuple[float, float] = (0.5, 2.0)
_PITCH_RANGE: tuple[int, int] = (-12, 12)
#: 音频后处理采样率
_PROCESS_SAMPLE_RATE = 48000


def _clamp(value: float, low: float, high: float) -> float:
    """把数值限制在闭区间内。

    Args:
        value: 原始数值。
        low: 下限。
        high: 上限。

    Returns:
        钳位后的数值。
    """
    return max(low, min(high, value))


def _resolve_style(params: dict[str, Any]) -> tuple[float, int, str | None]:
    """把请求参数中的语速/音调/情绪换算为实际音频调整量。

    Args:
        params: 生成请求参数（可含 rate / pitch / emotion）。

    Returns:
        ``(rate, pitch_semitone, emotion)``；情绪未知时 rate/pitch 保持原值。
    """
    try:
        rate = float(params.get("rate") or 1.0)
    except (TypeError, ValueError):
        rate = 1.0
    try:
        pitch = int(params.get("pitch") or 0)
    except (TypeError, ValueError):
        pitch = 0
    emotion = str(params.get("emotion") or "").strip().lower() or None
    emotion_rate, emotion_pitch = _EMOTION_ADJUST.get(emotion or "", (1.0, 0))
    return (
        _clamp(rate * emotion_rate, *_RATE_RANGE),
        int(_clamp(pitch + emotion_pitch, *_PITCH_RANGE)),
        emotion,
    )


def _atempo_stages(tempo: float) -> list[float]:
    """把 atempo 拆分为 ffmpeg 支持的 0.5~2.0 区间串联。

    Args:
        tempo: 目标语速倍率（>0）。

    Returns:
        atempo 阶段倍率列表（乘积等于 tempo）。
    """
    stages: list[float] = []
    value = max(0.05, tempo)
    while value > 2.0:
        stages.append(2.0)
        value /= 2.0
    while value < 0.5:
        stages.append(0.5)
        value /= 0.5
    stages.append(round(value, 4))
    return stages


def _probe_duration_ms(path: Path) -> int:
    """用 ffprobe 读取音频时长。

    Args:
        path: 音频文件路径。

    Returns:
        时长（毫秒）；ffprobe 不可用或解析失败返回 0。
    """
    if not shutil.which("ffprobe"):
        return 0
    try:
        completed = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    try:
        return max(0, int(float(completed.stdout.decode().strip()) * 1000))
    except (ValueError, AttributeError):
        return 0


def _apply_style(audio_bytes: bytes, rate: float, pitch: int) -> tuple[bytes, int] | None:
    """按语速/音调对合成音频做后处理（P1-11：情绪/语速真实生效）。

    Args:
        audio_bytes: 原始音频字节。
        rate: 目标语速倍率（0.5~2.0）。
        pitch: 目标音调半音（-12~12）。

    Returns:
        ``(处理后的音频字节, 实际时长毫秒)``；无需处理、ffmpeg 不可用或失败时
        返回 None（调用方使用原始音频）。
    """
    if abs(rate - 1.0) < 0.01 and pitch == 0:
        return None
    if not shutil.which("ffmpeg"):
        return None
    with tempfile.TemporaryDirectory(prefix="gd_tts_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        source = tmpdir / "src.wav"
        output = tmpdir / "out.wav"
        source.write_bytes(audio_bytes)
        filters = [f"aresample={_PROCESS_SAMPLE_RATE}"]
        tempo = rate
        if pitch:
            factor = 2 ** (pitch / 12)
            filters.append(f"asetrate={int(_PROCESS_SAMPLE_RATE * factor)}")
            filters.append(f"aresample={_PROCESS_SAMPLE_RATE}")
            tempo = rate / factor
        for stage in _atempo_stages(tempo):
            filters.append(f"atempo={stage}")
        command = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(source),
            "-filter:a", ",".join(filters),
            "-c:a", "pcm_s16le",
            "-ar", str(_PROCESS_SAMPLE_RATE),
            str(output),
        ]
        try:
            completed = subprocess.run(
                command, check=False, capture_output=True, timeout=120,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0 or not output.is_file():
            logger.warning(
                "TTS 音频后处理失败：%s",
                completed.stderr.decode("utf-8", errors="replace")[-400:],
            )
            return None
        return output.read_bytes(), _probe_duration_ms(output)



def _bytes_duration_ms(audio: bytes, mime_type: str | None) -> int:
    """估算音频字节时长（优先 ffprobe，WAV 直接解析）。

    Args:
        audio: 音频字节。
        mime_type: MIME 类型（用于选择解析方式）。

    Returns:
        时长（毫秒，≥100）。
    """
    if "wav" in str(mime_type or "").lower() or audio[:4] == b"RIFF":
        return _wav_duration_ms(audio)
    if shutil.which("ffprobe"):
        with tempfile.TemporaryDirectory(prefix="gd_probe_") as tmpdir_str:
            path = Path(tmpdir_str) / "probe.bin"
            path.write_bytes(audio)
            probed = _probe_duration_ms(path)
            if probed > 0:
                return probed
    return _wav_duration_ms(audio)


def _wav_duration_ms(audio: bytes) -> int:
    """从 WAV 字节推算时长（毫秒）。

    按标准 44 字节头读取采样率/声道/位深；解析失败时按 32KB/s 粗估。

    Args:
        audio: WAV 文件字节。

    Returns:
        时长（毫秒，≥100）。
    """
    if len(audio) <= 44:
        return 100
    try:
        channels = int.from_bytes(audio[22:24], "little") or 1
        sample_rate = int.from_bytes(audio[24:28], "little") or 24000
        bits = int.from_bytes(audio[34:36], "little") or 16
        byte_rate = sample_rate * channels * max(bits, 8) // 8
        if byte_rate <= 0:
            raise ValueError("byte_rate<=0")
        return max(100, int((len(audio) - 44) / byte_rate * 1000))
    except (ValueError, IndexError):
        return max(100, int(len(audio) / 32))


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    attempts: int = 3,
) -> dict[str, Any]:
    """提交合成请求并对 429 做指数退避重试。

    Args:
        client: 复用的 httpx 客户端。
        url: 完整请求地址。
        headers: 请求头。
        body: JSON 请求体。
        attempts: 最大尝试次数（含首次）。

    Returns:
        解析后的 JSON 响应。

    Raises:
        RuntimeError: 非可重试状态码或重试耗尽。
    """
    delay = 1.0
    resp: httpx.Response | None = None
    for attempt in range(attempts):
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code == 429 and attempt < attempts - 1:
            logger.warning("千问 TTS 限流 429，%.1fs 后重试", delay)
            await asyncio.sleep(delay)
            delay *= 2
            continue
        break
    assert resp is not None
    if resp.status_code >= 400:
        raise RuntimeError(f"千问 TTS HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"千问 TTS 响应非 JSON: {resp.text[:200]}") from exc


class QwenTtsProvider(ModelProvider):
    """Qwen3-TTS-Flash 语音合成提供商。"""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        default_voice: str | None = None,
    ) -> None:
        """初始化提供商。

        Args:
            storage: 本地存储（音频落盘）。
            api_key: API Key；缺省取 ``settings.QWEN_TTS_API_KEY``。
            base_url: 服务基址；缺省取 ``settings.QWEN_TTS_BASE_URL``。
            model: 模型名；缺省取 ``settings.QWEN_TTS_MODEL``。
            default_voice: 默认音色；缺省取 ``settings.QWEN_TTS_VOICE``。

        Raises:
            无（参数均为内存赋值）。
        """
        self._storage = storage
        self._api_key = (api_key or settings.QWEN_TTS_API_KEY).strip()
        self._base_url = (base_url or settings.QWEN_TTS_BASE_URL).strip().rstrip("/")
        self._model = model or settings.QWEN_TTS_MODEL
        self._default_voice = default_voice or settings.QWEN_TTS_VOICE

    def supports(self, capability: str) -> bool:
        """判断是否支持指定能力。

        Args:
            capability: 能力名。

        Returns:
            仅 TTS 返回 True。
        """
        return capability == CAPABILITY_TTS

    async def generate(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """执行一次语音合成。

        Args:
            request: 统一生成请求（prompt 为待合成文本；params.voice /
                params.language_type 可选）。
            model_spec: 模型注册行。

        Returns:
            成功：ok=True + object_key/media_url；失败：ok=False + error。
        """
        text = (request.prompt or "").strip()
        if not text:
            return GenResult(
                ok=False,
                error="TTS 文本为空",
                meta={"provider": "qwen-tts", "model_key": model_spec.model_key},
            )
        voice = str(request.params.get("voice") or request.params.get("voice_name") or self._default_voice)
        language = str(request.params.get("language_type") or _DEFAULT_LANGUAGE)
        # P1-11：情绪/语速/音调在本地音频后处理中真正生效（DashScope 请求体不
        # 支持这些扩展字段，直接发送会被拒绝，故不写入 body）。
        rate, pitch, emotion = _resolve_style(request.params)
        body = {
            "model": self._model,
            "input": {"text": text, "voice": voice},
            "parameters": {"language_type": language},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                data = await _post_json(
                    client,
                    f"{self._base_url}{_TTS_PATH}",
                    headers=headers,
                    body=body,
                )
                audio_node = (data.get("output") or {}).get("audio") or {}
                audio_url = str(audio_node.get("url") or "")
                audio_data = audio_node.get("data")
                if audio_url:
                    resp = await client.get(audio_url, timeout=_DOWNLOAD_TIMEOUT)
                    if resp.status_code >= 400:
                        raise RuntimeError(f"语音下载失败 HTTP {resp.status_code}")
                    audio_bytes = resp.content
                    mime_type = (resp.headers.get("Content-Type") or "audio/wav").split(";")[0]
                elif isinstance(audio_data, str) and audio_data:
                    audio_bytes = base64.b64decode(audio_data)
                    mime_type = "audio/wav"
                else:
                    return GenResult(
                        ok=False,
                        error="千问 TTS 响应未包含音频地址",
                        meta={"provider": "qwen-tts", "model_key": model_spec.model_key},
                    )
        except Exception as exc:  # noqa: BLE001 网络等意外统一转失败结果
            logger.exception("千问 TTS 合成失败")
            return GenResult(
                ok=False,
                error=f"千问 TTS 合成失败: {type(exc).__name__}: {str(exc)[:200]}",
                meta={"provider": "qwen-tts", "model_key": model_spec.model_key},
            )

        suffix = ".mp3" if "mpeg" in str(mime_type or "").lower() else ".wav"
        style_applied = False
        # 情绪映射出的语速/音调在这里后处理；失败时保留原始音频并继续
        processed = await asyncio.to_thread(_apply_style, audio_bytes, rate, pitch)
        if processed is not None:
            processed_bytes, processed_ms = processed
            audio_bytes = processed_bytes
            mime_type = "audio/wav"
            suffix = ".wav"
            style_applied = True
            duration_ms = processed_ms or _wav_duration_ms(audio_bytes)
        else:
            duration_ms = _bytes_duration_ms(audio_bytes, mime_type)

        object_key = self._storage.save_bytes(
            f"qwen-tts/{new_request_id().lower()}{suffix}", audio_bytes
        )
        return GenResult(
            ok=True,
            object_key=object_key,
            media_url=f"/files/{object_key}",
            meta={
                "provider": "qwen-tts",
                "model_key": model_spec.model_key,
                "mime_type": mime_type or "audio/wav",
                "duration_ms": duration_ms,
                "characters": len(text),
                "voice": voice,
                "language_type": language,
                "rate": round(rate, 3),
                "pitch": pitch,
                "emotion": emotion,
                "emotion_applied": style_applied or (emotion is not None and abs(rate - 1.0) < 0.01 and pitch == 0),
                "bytes": len(audio_bytes),
                "remote_url": audio_url or None,
                "wall_time_ms": int((time.monotonic() - started) * 1000),
            },
        )

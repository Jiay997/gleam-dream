"""音效（SFX）合成服务（P0 剪辑模块优化：音效点自动对位）。

背景：成片此前只有 BGM 与配音，关键动作（推门、怒斥、对峙）缺少
音效点缀，观感"哑"。引入真实 SFX 素材库成本高（版权+存储+选型），
故内置参数化合成器，按情绪/动作确定性地合成三类短音效并缓存到
本地存储（``sfx/{kind}.wav``），合成时作为普通 audio 片段混入：

- ``impact``：低频冲击（对峙/怒斥/突发，配 tension/anger/fear/surprise）；
- ``whoosh``：噪声扫频（快速动作/转场，配推/拉/跑/摔等动作动词）；
- ``ding``：清亮泛音（暖点/转念，配 joy/romance）。

合成过程为纯确定性 DSP（无随机、无外部素材），同一 kind 多次生成
结果一致，缓存命中后零成本复用。
"""

from __future__ import annotations

import io
import logging
import math
import struct
import wave

import numpy as np

from app.infra.storage import storage

logger = logging.getLogger("gleamdream.sfx")

__all__ = ["SFX_KINDS", "ensure_sfx_wav", "synthesize_sfx_bytes"]

#: 支持的音效种类
SFX_KINDS: tuple[str, ...] = ("impact", "whoosh", "ding")

#: 采样率（Hz）与目标响度
_SAMPLE_RATE: int = 44100
_PEAK: float = 0.6


def _env(length: int, attack: float, release: float) -> np.ndarray:
    """构建 attack/release 包络（线性起落，中段持平）。

    Args:
        length: 样本数。
        attack: 起音占比（0~1）。
        release: 释放占比（0~1）。

    Returns:
        归一化包络数组（shape=(length,)）。
    """
    env = np.ones(length, dtype=np.float32)
    a = max(1, int(length * attack))
    r = max(1, int(length * release))
    env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    env[length - r:] = np.linspace(1.0, 0.0, r, dtype=np.float32)
    return env


def _impact(duration_ms: int) -> np.ndarray:
    """合成低频冲击音效。

    Args:
        duration_ms: 时长（毫秒）。

    Returns:
        浮点波形数组（-1~1）。
    """
    n = int(_SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(n, dtype=np.float32) / _SAMPLE_RATE
    # 低频正弦 + 指数衰减 + 起始噪声点击
    body = np.sin(2 * math.pi * 82.0 * t) * np.exp(-t * 14.0)
    click = (np.random.default_rng(7).standard_normal(n).astype(np.float32)) * np.exp(-t * 120.0) * 0.4
    wave_data = (body * 0.9 + click) * _env(n, 0.002, 0.35)
    return wave_data


def _whoosh(duration_ms: int) -> np.ndarray:
    """合成噪声扫频（whoosh）音效。

    Args:
        duration_ms: 时长（毫秒）。

    Returns:
        浮点波形数组（-1~1）。
    """
    n = int(_SAMPLE_RATE * duration_ms / 1000)
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(n).astype(np.float32)
    # 一阶低通"扫"：随时间降低截止系数，得到由亮转暗的扫频感
    alpha = np.linspace(0.35, 0.06, n, dtype=np.float32)
    smoothed = np.empty(n, dtype=np.float32)
    acc = 0.0
    for i in range(n):
        acc += alpha[i] * (noise[i] - acc)
        smoothed[i] = acc
    envelope = np.maximum(np.sin(np.linspace(0.0, math.pi, n, dtype=np.float32)), 0.0) ** 1.5
    return smoothed * envelope * 2.2


def _ding(duration_ms: int) -> np.ndarray:
    """合成清亮泛音（ding）音效。

    Args:
        duration_ms: 时长（毫秒）。

    Returns:
        浮点波形数组（-1~1）。
    """
    n = int(_SAMPLE_RATE * duration_ms / 1000)
    t = np.arange(n, dtype=np.float32) / _SAMPLE_RATE
    tone = (
        np.sin(2 * math.pi * 880.0 * t) * 0.7
        + np.sin(2 * math.pi * 1318.5 * t) * 0.3
    ) * np.exp(-t * 6.0)
    return tone * _env(n, 0.004, 0.30)


def synthesize_sfx_bytes(kind: str, duration_ms: int | None = None) -> bytes:
    """合成指定种类的音效并返回 16bit 单声道 WAV 字节。

    Args:
        kind: 音效种类（impact/whoosh/ding，未知种类回退 impact）。
        duration_ms: 时长（毫秒）；None 按种类默认（impact 320/
            whoosh 480/ding 700）。

    Returns:
        WAV 文件字节流。
    """
    kind = kind if kind in SFX_KINDS else "impact"
    duration_ms = duration_ms or {"impact": 320, "whoosh": 480, "ding": 700}[kind]
    if kind == "impact":
        data = _impact(duration_ms)
    elif kind == "whoosh":
        data = _whoosh(duration_ms)
    else:
        data = _ding(duration_ms)
    peak = float(np.max(np.abs(data))) or 1.0
    data = data / peak * _PEAK
    pcm = (np.clip(data, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(_SAMPLE_RATE)
        writer.writeframes(pcm)
    return buffer.getvalue()


def ensure_sfx_wav(kind: str, duration_ms: int | None = None) -> str | None:
    """确保音效文件已存在于本地存储并返回对象键。

    Args:
        kind: 音效种类（impact/whoosh/ding）。
        duration_ms: 时长（毫秒）；None 用默认值。

    Returns:
        存储对象键（``sfx/{kind}_{duration}ms.wav``）；写盘失败返回 None。
    """
    kind = kind if kind in SFX_KINDS else "impact"
    duration_ms = duration_ms or {"impact": 320, "whoosh": 480, "ding": 700}[kind]
    object_key = f"sfx/{kind}_{duration_ms}ms.wav"
    try:
        path = storage.object_path(object_key)
        if path.exists() and path.stat().st_size > 0:
            return object_key
        data = synthesize_sfx_bytes(kind, duration_ms)
        return storage.save_bytes(object_key, data)
    except Exception:  # noqa: BLE001 SFX 失败不应阻断合成主链路
        logger.warning("SFX 合成/落盘失败 kind=%s", kind, exc_info=True)
        return None

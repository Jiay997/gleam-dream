"""BGM 程序化合成器（P1-7 修复：曲库媒体缺口）。

背景：``bgm_tracks`` 种子数据只有元信息，全部 20 条曲目没有实际音频媒体，
导致时间线 BGM 轨在成片合成时全部被「音频片段不可用，已跳过」，
成片出现整镜静音段（全链路测试报告 P1-7）。

本模块按曲目的情绪标签 / BPM / 时长，用 numpy 程序化合成原创暖调
钢琴/环境垫音频（pad + 琶音 + 低音三层），输出 16bit 立体声 WAV 字节。
合成结果是平台原创内容，对应 ``license_type=self``，可商用。

设计要点：
- 确定性：同一 (emotion, bpm, duration_ms, seed) 恒定产出同一音频，
  便于测试与幂等重建；
- 分层混音：pad（和弦长音垫底）+ arp（钢琴感琶音）+ bass（低音），
  情绪仅影响和弦进行、琶音密度与音色亮度，保证「能听、不抢配音」；
- 峰值归一到 0.35（约 -9 dBFS），首尾各 1.5s 淡入淡出，方便循环铺设。
"""

from __future__ import annotations

import io
import wave
from typing import Any

import numpy as np

#: 输出采样率（Hz）
_SAMPLE_RATE = 44100

#: 合成峰值幅度（线性，约 -9 dBFS），为后续混音 ducking 留动态空间
_PEAK = 0.35

#: 情绪 → 音乐参数画像。
#: chords: (根音 MIDI, 和弦音程列表)；密度: 每拍琶音触发数；
#: bright: 高次谐波亮度系数；detune: pad 失谐幅度（营造弦乐感）。
_MOOD_PROFILES: dict[str, dict[str, Any]] = {
    # 暖调大调系：C 大调 I-vi-IV-V 的加九变体，缓慢琶音
    "calm": {
        "chords": [(48, [0, 4, 7, 14]), (45, [0, 4, 7, 14]),
                   (41, [0, 4, 7, 14]), (43, [0, 4, 7, 12])],
        "density": 1, "bright": 0.20, "detune": 0.0012,
    },
    # 明快大调系：G 大调 I-V-vi-IV，八分琶音
    "joy": {
        "chords": [(43, [0, 4, 7, 12]), (50, [0, 4, 7, 12]),
                   (45, [0, 3, 7, 12]), (41, [0, 4, 7, 12])],
        "density": 2, "bright": 0.30, "detune": 0.0008,
    },
    # 小调系：A 小调 i-VI-III-VII，稀疏琶音
    "sadness": {
        "chords": [(45, [0, 3, 7, 10]), (41, [0, 3, 7, 10]),
                   (48, [0, 3, 7, 10]), (43, [0, 3, 7, 10])],
        "density": 1, "bright": 0.16, "detune": 0.0015,
    },
    # 紧张半音簇：D 小调 ii 减和弦色彩
    "tension": {
        "chords": [(38, [0, 3, 6, 10]), (45, [0, 3, 7, 10]),
                   (36, [0, 3, 6, 10]), (43, [0, 3, 6, 10])],
        "density": 2, "bright": 0.24, "detune": 0.0020,
    },
    # 浪漫大调系：F 大调 IV-iii-vi-I
    "romance": {
        "chords": [(41, [0, 4, 7, 11]), (40, [0, 3, 7, 11]),
                   (45, [0, 3, 7, 12]), (36, [0, 4, 7, 11])],
        "density": 1, "bright": 0.22, "detune": 0.0013,
    },
    # 惊讶：增三与挂留交替，短促琶音
    "surprise": {
        "chords": [(40, [0, 4, 8, 12]), (45, [0, 5, 7, 12]),
                   (47, [0, 4, 8, 12]), (43, [0, 5, 7, 12])],
        "density": 2, "bright": 0.32, "detune": 0.0010,
    },
    # 愤怒：E 小调强力和弦
    "anger": {
        "chords": [(40, [0, 3, 7, 12]), (36, [0, 3, 7, 12]),
                   (43, [0, 3, 7, 12]), (38, [0, 3, 7, 12])],
        "density": 2, "bright": 0.28, "detune": 0.0016,
    },
    # 恐惧：减和弦 + 半音摩擦
    "fear": {
        "chords": [(37, [0, 3, 6, 9]), (44, [0, 3, 6, 9]),
                   (39, [0, 3, 6, 9]), (42, [0, 3, 6, 9])],
        "density": 1, "bright": 0.18, "detune": 0.0024,
    },
}


def _midi_to_hz(midi: float) -> float:
    """把 MIDI 音符号转换为频率（Hz）。

    Args:
        midi: MIDI 音符号（69 = A4 = 440Hz）。

    Returns:
        对应频率（Hz）。
    """
    return 440.0 * (2.0 ** ((float(midi) - 69.0) / 12.0))


def _add_tone(
    buffer: np.ndarray,
    start: int,
    length: int,
    freq: float,
    amp: float,
    *,
    bright: float,
    attack_s: float,
    decay_tau: float,
    detune: float = 0.0,
) -> None:
    """把单音（带谐波与包络）叠加进音频缓冲区。

    音色 = 基频正弦 + 亮度控制的二次/三次谐波，包络 = 线性 attack +
    指数衰减（``decay_tau`` 为时间常数）。可选轻微失谐形成双振荡器
    合唱效果。

    Args:
        buffer: 目标单声道缓冲区（就地累加）。
        start: 起始采样点。
        length: 音长（采样数）。
        freq: 基频（Hz）。
        amp: 幅度（线性）。
        bright: 高次谐波亮度（0~1）。
        attack_s: 起音时长（秒）。
        decay_tau: 衰减时间常数（秒）。
        detune: 双振荡器失谐比例（如 0.0012）。

    Returns:
        None

    Raises:
        无：纯数值计算，不产生异常路径。
    """
    if length <= 0 or amp <= 0:
        return
    t = np.arange(length) / _SAMPLE_RATE
    env = np.ones(length)
    attack_n = min(length, max(1, int(attack_s * _SAMPLE_RATE)))
    env[:attack_n] = np.linspace(0.0, 1.0, attack_n)
    env *= np.exp(-t / max(decay_tau, 1e-3))
    wave_main = np.sin(2 * np.pi * freq * t)
    wave_second = np.sin(2 * np.pi * freq * 2.0 * t + 0.7)
    wave_third = np.sin(2 * np.pi * freq * 3.0 * t + 1.3)
    signal = wave_main + bright * wave_second + (bright * 0.4) * wave_third
    if detune > 0:
        signal = 0.5 * signal + 0.5 * np.sin(2 * np.pi * freq * (1 + detune) * t)
    end = min(len(buffer), start + length)
    buffer[start:end] += (amp * env * signal)[: end - start]


def synthesize_bgm_wav(
    *,
    emotion: str,
    bpm: int,
    duration_ms: int,
    seed: int = 0,
) -> bytes:
    """按情绪画像合成一段原创 BGM，返回 16bit 立体声 WAV 字节。

    结构：以 4/4 拍小节为单位循环和弦进行；pad 铺每小节和弦长音、
    琶音按情绪密度逐半拍/逐拍触发、bass 每两拍点根音；整段首尾
    1.5s 淡入淡出，峰值归一到固定电平。

    Args:
        emotion: 情绪标签（calm/joy/sadness/tension/romance/surprise/
            anger/fear）；未收录情绪回退 calm。
        bpm: 每分钟拍数（决定小节长度与琶音节奏）。
        duration_ms: 目标时长（毫秒），最少 20s，不足时按 20s 生成。
        seed: 随机种子（预留艺术变体；当前实现为确定性合成，不影响输出）。

    Returns:
        WAV 文件字节（``audio/wav``，44100Hz/16bit/stereo）。

    Raises:
        ValueError: duration_ms 非正数。
    """
    if duration_ms <= 0:
        raise ValueError("duration_ms 必须为正数")
    profile = _MOOD_PROFILES.get(emotion) or _MOOD_PROFILES["calm"]
    total_s = max(20.0, duration_ms / 1000.0)
    total_n = int(total_s * _SAMPLE_RATE)
    mix = np.zeros(total_n, dtype=np.float64)

    beat_s = 60.0 / max(30, int(bpm or 72))
    bar_s = beat_s * 4.0
    bar_n = int(bar_s * _SAMPLE_RATE)
    chord_count = len(profile["chords"])
    bright = float(profile["bright"])
    detune = float(profile["detune"])
    density = int(profile["density"])

    bar_index = 0
    pos = 0
    while pos < total_n:
        root_midi, intervals = profile["chords"][bar_index % chord_count]
        bar_len = min(bar_n, total_n - pos)
        # ---- pad：和弦长音垫底（慢起音、整小节延音） ----
        for step, interval in enumerate(intervals):
            freq = _midi_to_hz(root_midi + 12 + interval)
            _add_tone(
                mix, pos, bar_len, freq, 0.16 - 0.015 * step,
                bright=bright * 0.5, attack_s=0.45,
                decay_tau=bar_s * 1.6, detune=detune,
            )
        # ---- bass：根音低八度，每两拍一次 ----
        bass_freq = _midi_to_hz(root_midi - 12)
        for half in range(0, max(1, int(round(bar_len / (beat_s * 2 * _SAMPLE_RATE))))):
            start = pos + int(half * 2 * beat_s * _SAMPLE_RATE)
            length = min(int(beat_s * 1.8 * _SAMPLE_RATE), total_n - start)
            if length <= 0:
                break
            _add_tone(
                mix, start, length, bass_freq, 0.20,
                bright=0.10, attack_s=0.02, decay_tau=beat_s * 1.2,
            )
        # ---- arp：钢琴感琶音（按情绪密度触发） ----
        step_n = int(beat_s / density * _SAMPLE_RATE)
        step_n = max(step_n, 1)
        arp_order = list(intervals) + [intervals[0] + 12]
        arp_pos = 0
        while arp_pos < bar_len:
            note = arp_order[(arp_pos // step_n + bar_index) % len(arp_order)]
            freq = _midi_to_hz(root_midi + 24 + note)
            length = min(int(beat_s * 0.9 / density * _SAMPLE_RATE), total_n - pos - arp_pos)
            if length <= 0:
                break
            _add_tone(
                mix, pos + arp_pos, length, freq, 0.12,
                bright=bright, attack_s=0.008, decay_tau=0.35,
            )
            arp_pos += step_n
        pos += bar_len
        bar_index += 1

    # ---- 峰值归一 + 首尾淡入淡出 ----
    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 0:
        mix *= _PEAK / peak
    fade_n = min(total_n // 2, int(1.5 * _SAMPLE_RATE))
    if fade_n > 0:
        ramp_in = np.linspace(0.0, 1.0, fade_n)
        ramp_out = np.linspace(1.0, 0.0, fade_n)
        mix[:fade_n] *= ramp_in
        mix[-fade_n:] *= ramp_out

    # ---- 轻度立体声展宽：右声道 12ms 延迟叠加 ----
    delay_n = int(0.012 * _SAMPLE_RATE)
    right = np.copy(mix)
    right[delay_n:] = right[delay_n:] * 0.75 + mix[:-delay_n] * 0.25
    stereo = np.stack([mix, right], axis=1)
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(_SAMPLE_RATE)
        wav_file.writeframes(pcm.tobytes())
    return buf.getvalue()

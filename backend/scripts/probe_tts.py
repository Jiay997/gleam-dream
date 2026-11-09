# -*- coding: utf-8 -*-
"""TTS 协议参数矩阵排查（voice × format × sample_rate）。"""
import asyncio
import io
import json
import sys
import uuid

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"e:\FeiSi\Project code\gleam-dream\backend")

import websockets  # noqa: E402

from app.core.config import settings  # noqa: E402

TEXT = "浮光造梦测试。"


async def probe(voice: str, fmt: str, rate: int, streaming: str) -> str:
    """探测一组 TTS 参数。

    参数:
        voice: 音色名。
        fmt: 音频格式。
        rate: 采样率。
        streaming: 流模式。
    返回:
        结果摘要（OK/失败原因）。
    """
    headers = {"Authorization": f"bearer {settings.BAILIAN_API_KEY}", "user-agent": "gd-probe"}
    task_id = uuid.uuid4().hex
    audio = 0
    err = None
    finished = False
    try:
        async with websockets.connect(settings.BAILIAN_WS_URL, additional_headers=headers, max_size=16 * 1024 * 1024) as ws:
            await ws.send(json.dumps({
                "header": {"action": "run-task", "task_id": task_id, "streaming": streaming},
                "payload": {
                    "model": settings.BAILIAN_TTS_MODEL,
                    "task_group": "audio",
                    "task": "tts",
                    "function": "SpeechSynthesizer",
                    "parameters": {"text_type": "PlainText", "voice": voice, "format": fmt, "sample_rate": rate, "volume": 60, "rate": 1.0, "pitch": 1.0},
                    "input": {},
                },
            }, ensure_ascii=False))
            for _ in range(40):
                msg = await asyncio.wait_for(ws.recv(), timeout=12)
                if isinstance(msg, (bytes, bytearray)):
                    audio += len(msg)
                    continue
                frame = json.loads(msg)
                event = (frame.get("header") or {}).get("event")
                if event == "task-started":
                    await ws.send(json.dumps({
                        "header": {"action": "continue-task", "task_id": task_id, "streaming": streaming},
                        "payload": {"input": {"text": TEXT}},
                    }, ensure_ascii=False))
                    await ws.send(json.dumps({
                        "header": {"action": "finish-task", "task_id": task_id, "streaming": streaming},
                        "payload": {"input": {}},
                    }, ensure_ascii=False))
                elif event == "task-finished":
                    finished = True
                    break
                elif event == "task-failed":
                    err = str((frame.get("payload") or {}).get("message") or frame)
                    break
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {str(exc)[:120]}"
    if finished and audio:
        return f"OK audio={audio}B"
    return f"FAIL err={err} audio={audio}B"


async def main() -> None:
    """跑关键参数组合矩阵。"""
    cases = [
        ("Cherry", "wav", 16000, "duplex"),
        ("longxiaochun", "wav", 16000, "duplex"),
        ("Chelsie", "wav", 16000, "duplex"),
        ("Cherry", "mp3", 24000, "duplex"),
        ("longxiaochun", "mp3", 22050, "duplex"),
        ("Cherry", "wav", 16000, "out"),
    ]
    for voice, fmt, rate, streaming in cases:
        result = await probe(voice, fmt, rate, streaming)
        print(f"voice={voice:<14} fmt={fmt:<4} rate={rate:<6} streaming={streaming:<7} -> {result}")


asyncio.run(main())

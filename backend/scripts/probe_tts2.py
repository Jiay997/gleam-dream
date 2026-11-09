# -*- coding: utf-8 -*-
"""TTS 参数变体排查（第二轮：int 参数 / 去 text_type / voice 变体）。"""
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


async def probe(tag: str, parameters: dict, streaming: str = "duplex") -> str:
    """探测一组 TTS 参数。

    参数:
        tag: 组合标记。
        parameters: parameters 字典。
        streaming: 流模式。
    返回:
        结果摘要。
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
                    "parameters": parameters,
                    "input": {},
                },
            }, ensure_ascii=False))
            for _ in range(40):
                msg = await asyncio.wait_for(ws.recv(), timeout=12)
                if isinstance(msg, (bytes, bytearray)):
                    audio += len(msg)
                    continue
                frame = json.loads(msg)
                header = frame.get("header") or {}
                event = header.get("event")
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
                    err = str(header.get("error_message") or frame)
                    break
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {str(exc)[:120]}"
    if finished and audio:
        return f"OK audio={audio}B"
    return f"FAIL err={str(err)[:140]} audio={audio}B"


async def main() -> None:
    """跑参数变体。"""
    base = {"voice": "Cherry", "format": "wav", "sample_rate": 16000, "volume": 50}
    cases = [
        ("A-int-rate-pitch", {**base, "text_type": "PlainText", "rate": 1, "pitch": 1}),
        ("B-no-text-type", {**base, "rate": 1, "pitch": 1}),
        ("C-no-rate-pitch", {**base, "text_type": "PlainText"}),
        ("D-voice-alfred", {"voice": "Alfred", "format": "wav", "sample_rate": 16000, "volume": 50, "text_type": "PlainText", "rate": 1, "pitch": 1}),
        ("E-voice-tolove", {"voice": "toLove", "format": "wav", "sample_rate": 16000, "volume": 50, "text_type": "PlainText", "rate": 1, "pitch": 1}),
    ]
    for tag, parameters in cases:
        result = await probe(tag, parameters)
        print(f"[{tag}] -> {result}")


asyncio.run(main())

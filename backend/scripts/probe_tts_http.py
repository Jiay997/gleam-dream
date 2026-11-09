# -*- coding: utf-8 -*-
"""TTS HTTP 端点形态探测（qwen-audio-3.1-tts-plus）。"""
import asyncio
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"e:\FeiSi\Project code\gleam-dream\backend")

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402

TEXT = "浮光造梦测试。"


async def probe(tag: str, body: dict) -> None:
    """探测一种 HTTP 请求形态。

    参数:
        tag: 形态标记。
        body: 请求体。
    """
    headers = {"Authorization": f"Bearer {settings.BAILIAN_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=90) as client:
        try:
            resp = await client.post(
                f"{settings.BAILIAN_BASE_URL}/api/v1/services/aigc/multimodal-generation/generation",
                headers=headers, json=body,
            )
            text = resp.text[:500]
            print(f"[{tag}] HTTP {resp.status_code}: {text}")
        except Exception as exc:  # noqa: BLE001
            print(f"[{tag}] EXC: {type(exc).__name__}: {str(exc)[:150]}")


async def main() -> None:
    """跑三种 body 形态。"""
    await probe("A-input-text-voice", {
        "model": settings.BAILIAN_TTS_MODEL,
        "input": {"text": TEXT, "voice": "Cherry"},
    })
    await probe("B-messages-params-voice", {
        "model": settings.BAILIAN_TTS_MODEL,
        "input": {"messages": [{"role": "user", "content": [{"text": TEXT}]}]},
        "parameters": {"voice": "Cherry"},
    })
    await probe("C-messages-content-audio", {
        "model": settings.BAILIAN_TTS_MODEL,
        "input": {"messages": [{"role": "user", "content": [{"text": TEXT}, {"audio": ""}]}]},
        "parameters": {"voice": "Cherry", "audio_format": "wav"},
    })


asyncio.run(main())

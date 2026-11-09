# -*- coding: utf-8 -*-
"""TTS 单项快速验证（qwen-audio-3.1-tts-plus WebSocket 协议调试）。"""
import asyncio
import io
import json
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"e:\FeiSi\Project code\gleam-dream\backend")

from app.infra.model_gateway import GenRequest, model_gateway  # noqa: E402


async def main() -> None:
    """调用网关 TTS 并输出结果/事件细节。"""
    t0 = time.monotonic()
    r = await model_gateway.generate(
        "tts", "tts",
        GenRequest(capability="tts", prompt="浮光造梦，让每一个灵感都成为光影故事。", params={"voice": "Cherry"}),
    )
    print(f"elapsed {time.monotonic()-t0:.1f}s")
    if r.ok:
        print("[OK]", r.object_key, {k: v for k, v in r.meta.items() if k != "remote_url"})
    else:
        print("[FAIL]", r.error, r.meta)


asyncio.run(main())

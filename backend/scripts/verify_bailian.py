# -*- coding: utf-8 -*-
"""百炼三模型真实调用连通性验证（TTS / 图片 / 视频）。

直接经 model_gateway 网关调用（路由按 model_routes 命中百炼模型），
产物落本地存储；输出每步耗时与元数据。视频为异步任务（轮询至完成）。
"""
import asyncio
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"e:\FeiSi\Project code\gleam-dream\backend")

from app.infra.model_gateway import GenRequest, model_gateway  # noqa: E402


def show(tag: str, result) -> None:
    """打印一次生成结果摘要。

    参数:
        tag: 用例标记。
        result: GenResult。
    """
    if result.ok:
        meta = {k: v for k, v in result.meta.items() if k not in ("remote_url",)}
        print(f"[OK] {tag}: object_key={result.object_key}")
        print(f"     meta={meta}")
    else:
        print(f"[FAIL] {tag}: {result.error}")
        print(f"     meta={result.meta}")


async def main() -> None:
    """按命令行参数验证指定模型真实调用（缺省 all：TTS / 图片 / 视频依次验证）。"""
    case = sys.argv[1].lower() if len(sys.argv) > 1 else "all"
    # 1. TTS
    if case in ("all", "tts"):
        t0 = time.monotonic()
        r1 = await model_gateway.generate(
            "tts", "tts",
            GenRequest(capability="tts", prompt="浮光造梦，让每一个灵感都成为光影故事。", params={"voice": "Cherry"}),
        )
        print(f"---- TTS elapsed {time.monotonic()-t0:.1f}s ----")
        show("TTS qwen-audio-3.1-tts-plus", r1)

    # 2. 图片
    if case in ("all", "image"):
        t1 = time.monotonic()
        r2 = await model_gateway.generate(
            "image", "text_to_image",
            GenRequest(capability="image", prompt="雨夜的老城区巷口，一名外卖骑手停在路灯下，电影感，暖黄灯光，写实风格", params={"size": "1024x1024"}),
        )
        print(f"---- IMAGE elapsed {time.monotonic()-t1:.1f}s ----")
        show("IMAGE qwen-image-3.0", r2)

    # 3. 视频（异步任务，可能数分钟）
    if case in ("all", "video"):
        t2 = time.monotonic()
        r3 = await model_gateway.generate(
            "video", "text_to_video",
            GenRequest(capability="video", prompt="雨夜街道，骑手骑车穿过积水的老巷，镜头缓慢推进，电影质感", params={"duration_ms": 5000}),
        )
        print(f"---- VIDEO elapsed {time.monotonic()-t2:.1f}s ----")
        show("VIDEO wan3.0-video", r3)


asyncio.run(main())

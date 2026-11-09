"""LLM 深度探测：内容字段、reasoning 行为、JSON 模式兼容性。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.core.config import settings  # noqa: E402
from app.infra.model_gateway.openai_compat import build_endpoint  # noqa: E402


async def call(name: str, api_key: str, base_url: str, model: str, payload: dict) -> None:
    endpoint = build_endpoint(base_url)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                endpoint, json=payload, headers={"Authorization": f"Bearer {api_key}"}
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            print(f"[{name}] HTTP {resp.status_code} ({duration_ms}ms) body={resp.text[:400]}")
            return
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        usage = data.get("usage") or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content")
        print(f"[{name}] OK ({duration_ms}ms) finish={data.get('choices', [{}])[0].get('finish_reason')}")
        print(f"  content={content[:200]!r}")
        print(f"  reasoning_content={'(present, %d chars)' % len(reasoning) if reasoning else None}")
        print(f"  usage={ {k: usage.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')} }")
    except Exception as exc:  # noqa: BLE001
        print(f"[{name}] FAIL {type(exc).__name__}: {exc}")


async def main() -> None:
    providers = [
        ("deepseek", settings.DEEPSEEK_API_KEY, settings.DEEPSEEK_BASE_URL, settings.DEEPSEEK_MODEL),
        ("glm", settings.GLM_API_KEY, settings.GLM_BASE_URL, settings.GLM_MODEL),
    ]
    print("=== 测试1: 普通 JSON 模式生成（max_tokens=2048） ===")
    for name, key, url, model in providers:
        await call(name, key, url, model, {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是JSON生成器，只输出JSON。"},
                {"role": "user", "content": '输出 {"title": "一个AI短剧的中文标题(5-10字)", "acts": 3}'},
            ],
            "max_tokens": 2048,
            "response_format": {"type": "json_object"},
            "stream": False,
        })
    print()
    print("=== 测试2: 默认参数（不带 max_tokens） ===")
    for name, key, url, model in providers:
        await call(name, key, url, model, {
            "model": model,
            "messages": [{"role": "user", "content": "用一句话介绍短剧《浮光造梦》。"}],
            "stream": False,
        })


if __name__ == "__main__":
    asyncio.run(main())

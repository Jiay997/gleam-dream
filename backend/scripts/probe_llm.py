"""LLM 连通性探测脚本（运维诊断用）。

对 .env 中配置的 DeepSeek / GLM 两个提供商各发一次最小 ChatCompletions
请求，输出状态码、模型回复摘要、token 用量与耗时；用于验证网关依赖的
外部模型服务是否真实可达。

用法：
    python scripts/probe_llm.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx

# 把 backend 根目录加入 sys.path（脚本直接运行时需要）
_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.core.config import settings  # noqa: E402
from app.infra.model_gateway.openai_compat import build_endpoint  # noqa: E402


async def probe(name: str, api_key: str, base_url: str, model: str) -> None:
    endpoint = build_endpoint(base_url)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                endpoint,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "回复两个字：在线"}],
                    "max_tokens": 16,
                    "stream": False,
                },
                headers={"Authorization": f"Bearer {api_key}"},
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            print(f"[{name}] FAIL HTTP {resp.status_code} ({duration_ms}ms)")
            print(f"  endpoint={endpoint}")
            print(f"  body={resp.text[:300]}")
            return
        data = resp.json()
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage") or {}
        print(f"[{name}] OK ({duration_ms}ms)")
        print(f"  endpoint={endpoint}")
        print(f"  reply={text!r}")
        print(f"  usage={usage}")
    except Exception as exc:  # noqa: BLE001 — 探测脚本打印一切异常
        duration_ms = int((time.monotonic() - started) * 1000)
        print(f"[{name}] FAIL ({duration_ms}ms) {type(exc).__name__}: {exc}")
        print(f"  endpoint={endpoint}")


async def main() -> None:
    print("== LLM 连通性探测 ==")
    print(f"DEEPSEEK_BASE_URL={settings.DEEPSEEK_BASE_URL} model={settings.DEEPSEEK_MODEL}")
    print(f"GLM_BASE_URL={settings.GLM_BASE_URL} model={settings.GLM_MODEL}")
    await probe(
        "deepseek",
        settings.DEEPSEEK_API_KEY,
        settings.DEEPSEEK_BASE_URL,
        settings.DEEPSEEK_MODEL,
    )
    await probe(
        "glm",
        settings.GLM_API_KEY,
        settings.GLM_BASE_URL,
        settings.GLM_MODEL,
    )


if __name__ == "__main__":
    asyncio.run(main())

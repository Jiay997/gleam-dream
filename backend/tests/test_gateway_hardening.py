"""模型网关强化单元测试（推理模型空内容防护）。

覆盖：
1. ``build_endpoint`` 端点推导（无 /v1、带 /v1、完整 chat/completions、尾斜杠）；
2. 空内容 + finish_reason=length → 自适应翻倍 max_tokens 重试成功；
3. 空内容重试仍为空 → ok=False 且错误信息含 finish_reason 与翻倍信息；
4. max_tokens 翻倍超上限（ADAPTIVE_RETRY_MAX_TOKENS）→ 直接失败不重试；
5. finish_reason=stop 且空内容 → 不重试直接失败；
6. 正常响应 → ok=True，meta 携带 finish_reason / reasoning_tokens；
7. HTTP >= 400 → ok=False（错误摘要不含密钥）；
8. ``log_call_type`` 映射：llm_text 原样落库（迁移 a7c1d2e94b05 后），
   未知类型兜底 compose。

说明：通过 monkeypatch 替换 ``OpenAICompatProvider._post_chat`` 模拟提供商
响应，不发起真实网络请求；ModelRegistry 以 SimpleNamespace 鸭子类型替代
（generate 仅读取 model_key）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.infra.model_gateway.openai_compat import (
    ADAPTIVE_RETRY_MAX_TOKENS,
    LLMCallError,
    OpenAICompatProvider,
    build_endpoint,
)
from app.infra.model_gateway.gateway import log_call_type


def _provider() -> OpenAICompatProvider:
    """构造测试用提供商实例。"""
    return OpenAICompatProvider(
        name="deepseek",
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        default_model="deepseek-flash",
    )


def _model_spec() -> SimpleNamespace:
    """构造鸭子类型 ModelRegistry 替身。"""
    return SimpleNamespace(model_key="test-model")


def _chat_response(content: str, finish_reason: str = "stop", reasoning_tokens: int = 0) -> dict[str, Any]:
    """构造 ChatCompletions 响应体。"""
    usage: dict[str, Any] = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    if reasoning_tokens:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": usage,
    }


# ---- build_endpoint ----


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.deepseek.com/", "https://api.deepseek.com/v1/chat/completions"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/chat/completions"),
        (
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        ),
    ],
)
def test_build_endpoint(base_url: str, expected: str) -> None:
    """端点推导：无 /v1 补 /v1/chat/completions；已是完整端点原样保留。"""
    assert build_endpoint(base_url) == expected


# ---- 空内容自适应重试 ----


@pytest.mark.asyncio
async def test_empty_content_retries_with_doubled_max_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """finish_reason=length 空内容 → max_tokens 翻倍重试一次并成功。"""
    provider = _provider()
    calls: list[dict[str, Any]] = []

    async def fake_post_chat(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if len(calls) == 1:
            return _chat_response("", finish_reason="length")
        return _chat_response('{"ok": true}')

    monkeypatch.setattr(provider, "_post_chat", fake_post_chat)
    result = await provider.generate(
        _gen_request(max_tokens=1000), _model_spec()  # type: ignore[arg-type]
    )
    assert result.ok, result.error
    assert result.text == '{"ok": true}'
    assert len(calls) == 2
    assert calls[0]["max_tokens"] == 1000
    assert calls[1]["max_tokens"] == 2000


def _gen_request(max_tokens: int | None = None) -> Any:
    """构造 GenRequest 替身（避免导入循环，鸭子类型即可）。"""
    return SimpleNamespace(
        capability="llm",
        prompt="测试",
        system_prompt=None,
        messages=None,
        params={"max_tokens": max_tokens} if max_tokens else {},
        model_hint=None,
    )


@pytest.mark.asyncio
async def test_empty_content_retry_exhausted_reports_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """翻倍重试后仍为空 → ok=False，错误含 finish_reason 与翻倍信息。"""
    provider = _provider()

    async def fake_post_chat(payload: dict[str, Any]) -> dict[str, Any]:
        return _chat_response("", finish_reason="length")

    monkeypatch.setattr(provider, "_post_chat", fake_post_chat)
    result = await provider.generate(
        _gen_request(max_tokens=1000), _model_spec()  # type: ignore[arg-type]
    )
    assert not result.ok
    assert "finish_reason=length" in result.error
    assert "2000" in result.error, "错误信息应包含翻倍后的 max_tokens"


@pytest.mark.asyncio
async def test_empty_content_over_retry_cap_no_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """max_tokens 翻倍后超过自适应上限 → 不发起重试直接失败。"""
    provider = _provider()
    calls: list[dict[str, Any]] = []

    async def fake_post_chat(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return _chat_response("", finish_reason="length")

    monkeypatch.setattr(provider, "_post_chat", fake_post_chat)
    result = await provider.generate(
        _gen_request(max_tokens=ADAPTIVE_RETRY_MAX_TOKENS - 100),
        _model_spec(),  # type: ignore[arg-type]
    )
    assert not result.ok
    assert len(calls) == 1, "超上限时不应发起第二次调用"
    assert "上限" in result.error


@pytest.mark.asyncio
async def test_empty_content_with_stop_reason_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """finish_reason=stop 且空内容 → 不重试，直接失败。"""
    provider = _provider()
    calls: list[dict[str, Any]] = []

    async def fake_post_chat(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return _chat_response("", finish_reason="stop")

    monkeypatch.setattr(provider, "_post_chat", fake_post_chat)
    result = await provider.generate(
        _gen_request(max_tokens=1000), _model_spec()  # type: ignore[arg-type]
    )
    assert not result.ok
    assert len(calls) == 1
    assert "finish_reason=stop" in result.error


@pytest.mark.asyncio
async def test_empty_content_retry_http_error_reports_original_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自适应重试收到 HTTP 错误 → 以空内容 + 原始 finish_reason 上报。"""
    provider = _provider()
    calls: list[dict[str, Any]] = []

    async def fake_post_chat(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if len(calls) == 1:
            return _chat_response("", finish_reason="length")
        raise LLMCallError("HTTP 400: max_tokens exceeds model limit")

    monkeypatch.setattr(provider, "_post_chat", fake_post_chat)
    result = await provider.generate(
        _gen_request(max_tokens=1000), _model_spec()  # type: ignore[arg-type]
    )
    assert not result.ok
    assert "finish_reason=length" in result.error, "应保留原始结束原因而非 HTTP 错误"
    assert "HTTP 400" not in result.error


# ---- 正常响应元数据 ----


@pytest.mark.asyncio
async def test_success_meta_carries_reasoning_and_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常响应：meta 含 finish_reason / reasoning_tokens / token 计量。"""
    provider = _provider()

    async def fake_post_chat(payload: dict[str, Any]) -> dict[str, Any]:
        return _chat_response("你好", finish_reason="stop", reasoning_tokens=17)

    monkeypatch.setattr(provider, "_post_chat", fake_post_chat)
    result = await provider.generate(
        _gen_request(max_tokens=512), _model_spec()  # type: ignore[arg-type]
    )
    assert result.ok
    assert result.meta["finish_reason"] == "stop"
    assert result.meta["reasoning_tokens"] == 17
    assert result.meta["tokens_in"] == 10
    assert result.meta["tokens_out"] == 20
    assert result.meta["model_key"] == "test-model"


@pytest.mark.asyncio
async def test_http_error_masks_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP >= 400 → ok=False，错误摘要不含 API Key。"""
    provider = _provider()

    async def fake_post_chat(payload: dict[str, Any]) -> dict[str, Any]:
        raise LLMCallError("HTTP 401: invalid api key")

    monkeypatch.setattr(provider, "_post_chat", fake_post_chat)
    result = await provider.generate(
        _gen_request(max_tokens=512), _model_spec()  # type: ignore[arg-type]
    )
    assert not result.ok
    assert "sk-test" not in (result.error or "")
    assert "HTTP 401" in result.error


# ---- call_type 映射 ----


def test_log_call_type_llm_text_now_direct() -> None:
    """迁移 a7c1d2e94b05 后 llm_text 原样落库（不再以 compose 承载）。"""
    assert log_call_type("llm_text") == "llm_text"


def test_log_call_type_unknown_falls_back_to_compose() -> None:
    """未知任务类型兜底 compose，避免 CHECK 约束拒绝记账。"""
    assert log_call_type("no_such_type") == "compose"
    assert log_call_type("text_to_image") == "text_to_image"

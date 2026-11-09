"""OpenAI 兼容 LLM 提供商模块。

使用 httpx.AsyncClient 直连 OpenAI 兼容 ChatCompletions 端点（不依赖
langchain，保持轻量可控），同时服务两个提供商：

- **DeepSeek**：``DEEPSEEK_BASE_URL``（默认 https://api.deepseek.com，
  无 ``/v1`` 后缀时自动补全为 ``/v1/chat/completions``）；
- **智谱 GLM**：``GLM_BASE_URL`` 已是完整 ``chat/completions`` 端点，原样 POST。

支持参数：messages / system / temperature / max_tokens /
response_format={"type": "json_object"}（``params.response_json`` 为真时）。

**推理模型稳定性防护**（DeepSeek/GLM 当前均为推理模型，输出 token 会先被
``reasoning_content`` 消耗）：

1. **空内容自适应重试**：``content`` 为空且 ``finish_reason == "length"``
   （或无显式 max_tokens 时直接为空）说明推理消耗尽/挤占输出预算，自动把
   ``max_tokens`` 翻倍重试一次（上限 :data:`ADAPTIVE_RETRY_MAX_TOKENS`）；
   仍为空则返回 ``ok=False``（错误信息含 finish_reason，供上层自修循环）；
2. **reasoning 计量**：``usage.completion_tokens_details.reasoning_tokens``
   记入 meta.reasoning_tokens，供成本观测与预算调优；
3. **finish_reason 透传**：meta.finish_reason 保留原始结束原因。

网络传输错误（超时/连接失败）重试 1 次；HTTP >= 400 不重试，直接失败。
自适应重试若收到 HTTP >= 400（如超过模型输出上限），以「空内容」失败上报，
不把配额类 HTTP 错误当作主因。
API Key 仅在请求头中使用，绝不写入日志、快照或错误信息。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.infra.model_gateway.base import CAPABILITY_LLM, GenRequest, GenResult, ModelProvider

if TYPE_CHECKING:
    from app.models import ModelRegistry

logger = logging.getLogger("gleamdream.gateway")

# 请求超时（秒）：LLM 长文本生成可能较慢，按任务约定 120s
REQUEST_TIMEOUT_SECONDS: float = 120.0
# 网络传输错误最大尝试次数（首次 + 重试 1 次）
MAX_ATTEMPTS: int = 2
# 空内容自适应重试的 max_tokens 上限（翻倍后仍超过则放弃重试）
ADAPTIVE_RETRY_MAX_TOKENS: int = 16000


class LLMCallError(Exception):
    """LLM 调用失败异常：携带不含密钥的错误摘要。

    Attributes:
        message: 错误摘要（HTTP 状态码 + 响应体片段，或网络错误描述）。
    """

    def __init__(self, message: str) -> None:
        """构造调用异常。

        Args:
            message: 不含密钥的错误摘要。
        """
        self.message = message
        super().__init__(message)


def build_endpoint(base_url: str) -> str:
    """根据提供商 base_url 推导完整 ChatCompletions 端点。

    规则：
    1. 已含 ``/chat/completions``（如 GLM）→ 原样使用；
    2. 以 ``/v1`` 结尾 → 追加 ``/chat/completions``；
    3. 其余（如 https://api.deepseek.com）→ 追加 ``/v1/chat/completions``。

    Args:
        base_url: 提供商 API 基础地址。

    Returns:
        完整 ChatCompletions 端点 URL。
    """
    trimmed = base_url.strip().rstrip("/")
    if "/chat/completions" in trimmed:
        return trimmed
    if trimmed.endswith("/v1"):
        return f"{trimmed}/chat/completions"
    return f"{trimmed}/v1/chat/completions"


class OpenAICompatProvider(ModelProvider):
    """OpenAI 兼容 ChatCompletions 提供商（DeepSeek / GLM 共用实现）。

    Attributes:
        _name: 提供商标识（deepseek/glm，用于日志与 meta）。
        _api_key: API Key（仅用于请求头，绝不落日志/快照）。
        _endpoint: 完整 ChatCompletions 端点。
        _default_model: API 请求使用的模型名（来自 settings，如
            DEEPSEEK_MODEL / GLM_MODEL；model_registry.model_key 仅用于路由
            与记账，二者可能大小写/命名不同）。
    """

    def __init__(
        self,
        name: str,
        api_key: str,
        base_url: str,
        default_model: str,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        """构造提供商实例。

        Args:
            name: 提供商标识（deepseek/glm）。
            api_key: API Key（从 settings 读取）。
            base_url: API 基础地址（从 settings 读取）。
            default_model: 默认 API 模型名（从 settings 读取）。
            timeout_seconds: 请求超时秒数，默认 120。
        """
        self._name = name
        self._api_key = api_key
        self._endpoint = build_endpoint(base_url)
        self._default_model = default_model
        self._timeout_seconds = timeout_seconds

    def supports(self, capability: str) -> bool:
        """本提供商仅支持 LLM 能力。

        Args:
            capability: 能力名。

        Returns:
            capability 为 llm 时 True，否则 False。
        """
        return capability == CAPABILITY_LLM

    async def generate(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """调用 ChatCompletions 生成文本（含空内容自适应重试）。

        Args:
            request: 统一生成请求（messages 优先，否则 prompt + system_prompt）。
            model_spec: 路由命中的模型注册行。

        Returns:
            成功：ok=True，text 为回复文本，meta 含 tokens_in/tokens_out/
            reasoning_tokens/finish_reason/model_key/duration_ms/provider；
            失败：ok=False，error 为不含密钥的错误摘要。
        """
        started = time.monotonic()
        payload = self._build_payload(request)
        try:
            data = await self._call_with_empty_retry(payload)
        except LLMCallError as exc:
            return GenResult(
                ok=False,
                error=f"[{self._name}] {exc.message}",
                meta={
                    "provider": self._name,
                    "model_key": model_spec.model_key,
                    "api_model": self._default_model,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        finish_reason = choices[0].get("finish_reason") if choices else None
        text = ""
        if choices:
            message = choices[0].get("message") or {}
            text = message.get("content") or ""
        if not text.strip():
            # 自适应重试后仍为空：按失败上报（上层自修循环可感知并重试）
            return GenResult(
                ok=False,
                error=(
                    f"[{self._name}] 返回内容为空（finish_reason={finish_reason}，"
                    "推理模型可能将输出预算耗尽在思考上，建议增大 max_tokens）"
                ),
                meta={
                    "provider": self._name,
                    "model_key": model_spec.model_key,
                    "api_model": self._default_model,
                    "finish_reason": finish_reason,
                    "duration_ms": duration_ms,
                },
            )
        usage_details = usage.get("completion_tokens_details") or {}
        return GenResult(
            ok=True,
            text=text,
            meta={
                "provider": self._name,
                "model_key": model_spec.model_key,
                "api_model": self._default_model,
                "finish_reason": finish_reason,
                "tokens_in": int(usage.get("prompt_tokens") or 0),
                "tokens_out": int(usage.get("completion_tokens") or 0),
                "reasoning_tokens": int(usage_details.get("reasoning_tokens") or 0),
                "duration_ms": duration_ms,
            },
        )

    async def _call_with_empty_retry(self, payload: dict[str, Any]) -> dict[str, Any]:
        """执行调用，空内容（finish_reason=length）时自动翻倍 max_tokens 重试一次。

        推理模型可能把输出预算全部消耗在 reasoning 上导致 content 为空。
        判定条件：content 为空且 finish_reason == "length"；无显式 max_tokens
        时不翻倍（无法推断原始预算），仅把空内容交由上层判定。

        自适应重试收到 HTTP >= 400 时按「空内容失败」处理（抛出保留原始
        finish_reason 的 LLMCallError），避免配额错误掩盖真实原因。

        Args:
            payload: 首次请求体字典。

        Returns:
            提供商返回的 JSON 字典（成功取得非空内容，或重试后仍为空的原始响应）。

        Raises:
            LLMCallError: 网络/HTTP/解析失败，或重试后仍为空内容。
        """
        data = await self._post_chat(payload)
        if self._has_content(data):
            return data

        finish_reason = (data.get("choices") or [{}])[0].get("finish_reason")
        current_max = payload.get("max_tokens")
        if finish_reason != "length" or current_max is None:
            raise LLMCallError(
                f"返回内容为空（finish_reason={finish_reason}）"
            )
        doubled = int(current_max) * 2
        if doubled > ADAPTIVE_RETRY_MAX_TOKENS:
            raise LLMCallError(
                f"返回内容为空且 max_tokens 已达自适应上限 {ADAPTIVE_RETRY_MAX_TOKENS}"
                f"（finish_reason={finish_reason}）"
            )
        retry_payload = dict(payload)
        retry_payload["max_tokens"] = doubled
        logger.warning(
            "LLM 空内容（finish_reason=length），自适应翻倍 max_tokens 重试: "
            "endpoint=%s %s->%s", self._endpoint, current_max, doubled,
        )
        try:
            retry_data = await self._post_chat(retry_payload)
        except LLMCallError:
            # 重试的 HTTP/网络错误不作为主因：以空内容 + 原始 finish_reason 上报
            raise LLMCallError(
                f"返回内容为空（finish_reason={finish_reason}），"
                f"max_tokens 翻倍至 {doubled} 重试亦未取得内容"
            ) from None
        if self._has_content(retry_data):
            return retry_data
        raise LLMCallError(
            f"返回内容为空（finish_reason={finish_reason}），"
            f"max_tokens 翻倍至 {doubled} 重试后仍为空"
        )

    @staticmethod
    def _has_content(data: dict[str, Any]) -> bool:
        """判断响应中是否含有非空 content。

        Args:
            data: 提供商响应 JSON 字典。

        Returns:
            choices[0].message.content 非空白时 True。
        """
        choices = data.get("choices") or []
        if not choices:
            return False
        message = choices[0].get("message") or {}
        return bool((message.get("content") or "").strip())

    def _build_payload(self, request: GenRequest) -> dict[str, Any]:
        """构造 ChatCompletions 请求体。

        Args:
            request: 统一生成请求。

        Returns:
            OpenAI 兼容请求体字典（model/messages/temperature/max_tokens/
            response_format 等，仅包含显式提供的参数）。
        """
        if request.messages:
            messages: list[dict[str, str]] = list(request.messages)
        else:
            messages = []
            if request.system_prompt:
                messages.append({"role": "system", "content": request.system_prompt})
            messages.append({"role": "user", "content": request.prompt})
        payload: dict[str, Any] = {
            "model": self._default_model,
            "messages": messages,
            "stream": False,
        }
        temperature = request.params.get("temperature")
        if temperature is not None:
            payload["temperature"] = temperature
        max_tokens = request.params.get("max_tokens")
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        top_p = request.params.get("top_p")
        if top_p is not None:
            payload["top_p"] = top_p
        if request.params.get("response_json"):
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        """发送 ChatCompletions 请求并解析 JSON 响应。

        网络传输错误（超时/连接类）重试 1 次；HTTP >= 400 抛出
        :class:`LLMCallError`（含状态码与响应体摘要，不含密钥）。

        Args:
            payload: 请求体字典。

        Returns:
            提供商返回的 JSON 字典。

        Raises:
            LLMCallError: 重试后仍网络失败，或 HTTP 状态码 >= 400，
                或响应体不是合法 JSON。
        """
        headers = {"Authorization": f"Bearer {self._api_key}"}
        last_error: str | None = None
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = await client.post(self._endpoint, json=payload, headers=headers)
                except httpx.TransportError as exc:
                    # 仅网络传输错误重试；记录摘要时不含请求头，避免泄密
                    last_error = f"网络错误（第 {attempt} 次尝试）: {type(exc).__name__}: {exc}"
                    logger.warning("LLM 网络错误 endpoint=%s attempt=%s error=%s",
                                   self._endpoint, attempt, last_error)
                    continue
                if response.status_code >= 400:
                    body_preview = response.text[:500]
                    raise LLMCallError(
                        f"HTTP {response.status_code}: {body_preview}"
                    )
                try:
                    return response.json()
                except ValueError as exc:
                    raise LLMCallError(f"响应体非合法 JSON: {exc}") from exc
        raise LLMCallError(last_error or "未知网络错误")

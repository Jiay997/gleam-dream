"""模型网关包。

对外暴露网关单例与数据契约，业务层（服务/Agent/渲染任务）统一通过
``model_gateway.generate(...)`` 发起模型调用，通过 ``pick_model`` 做确定性
路由决策（ADR-07：LLM 不参与路由）。

用法::

    from app.infra.model_gateway import GenRequest, model_gateway

    result = await model_gateway.generate(
        "llm", "llm_text", GenRequest(capability="llm", prompt="..."),
    )
"""

from __future__ import annotations

from app.infra.model_gateway.base import (
    CAPABILITY_BGM,
    CAPABILITY_IMAGE,
    CAPABILITY_LLM,
    CAPABILITY_TTS,
    CAPABILITY_VIDEO,
    GenRequest,
    GenResult,
    ModelProvider,
)
from app.infra.model_gateway.gateway import ModelGateway, model_gateway
from app.infra.model_gateway.openai_compat import OpenAICompatProvider
from app.infra.model_gateway.router import CircuitBreaker, circuit_breaker, pick_model
from app.infra.model_gateway.stub import StubProvider

__all__ = [
    "CAPABILITY_BGM",
    "CAPABILITY_IMAGE",
    "CAPABILITY_LLM",
    "CAPABILITY_TTS",
    "CAPABILITY_VIDEO",
    "CircuitBreaker",
    "GenRequest",
    "GenResult",
    "ModelGateway",
    "ModelProvider",
    "OpenAICompatProvider",
    "StubProvider",
    "circuit_breaker",
    "model_gateway",
    "pick_model",
]

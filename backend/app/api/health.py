"""健康检查路由模块。

提供：
- ``GET /health``        服务存活探针（不触库）；
- ``GET /health/db``     数据库连通探针（SELECT 1；失败降级为 degraded，
  不抛异常不崩溃，业务码恒为 0）；
- ``GET /health/models`` 模型网关提供商配置探针（只读 settings，不发起
  真实模型调用、不产生费用；密钥仅返回是否已配置布尔位）。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.config import settings
from app.core.db import async_engine
from app.core.deps import get_request_id
from app.core.errors import success_envelope

router = APIRouter()


@router.get("/health")
async def health(request_id: Annotated[str, Depends(get_request_id)]) -> dict:
    """服务存活探针。

    Args:
        request_id: 请求级 ULID（依赖注入）。

    Returns:
        成功包络，data 含 status/env/app。
    """
    return success_envelope(
        {
            "status": "ok",
            "env": settings.ENV,
            "app": settings.APP_NAME,
        },
        request_id,
    )


@router.get("/health/db")
async def health_db(request_id: Annotated[str, Depends(get_request_id)]) -> dict:
    """数据库连通探针。

    对 DATABASE_URL 执行 ``SELECT 1``；任何异常（含库不存在、连接拒绝）
    均降级为 degraded 并以 code=0 正常返回，保证探针不崩溃。

    Args:
        request_id: 请求级 ULID（依赖注入）。

    Returns:
        成功包络，data.status 为 ok/degraded，database 子对象携带详情。
    """
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        database: dict[str, str] = {"status": "ok"}
    except Exception as exc:  # noqa: BLE001 探针场景必须吞掉所有异常并降级
        database = {"status": "degraded", "error": str(exc)}

    overall = "ok" if database["status"] == "ok" else "degraded"
    return success_envelope(
        {"status": overall, "database": database},
        request_id,
        message="ok" if overall == "ok" else "数据库不可达，服务降级运行",
    )


def _provider_status(name: str, api_key: str, base_url: str, model: str) -> dict[str, Any]:
    """构造单个模型提供商的配置状态（不含任何密钥明文）。

    Args:
        name: 提供商标识。
        api_key: API Key（仅判断非空）。
        base_url: API 基础地址。
        model: 默认模型名。

    Returns:
        {name, base_url, model, api_key_configured}。
    """
    return {
        "name": name,
        "base_url": base_url,
        "model": model,
        "api_key_configured": bool(api_key.strip()),
    }


@router.get("/health/models")
async def health_models(request_id: Annotated[str, Depends(get_request_id)]) -> dict:
    """模型网关提供商配置探针。

    只读 settings 校验 DeepSeek / GLM 两个 LLM 提供商的配置完整性
    （密钥是否注入、base_url / 模型名），**不发起真实模型调用**（不产生
    费用与外部副作用）；连通性验证用 ``scripts/probe_llm.py``。

    Args:
        request_id: 请求级 ULID（依赖注入）。

    Returns:
        成功包络，data 含 providers / media_providers 与配置就绪布尔位；
        ``all_configured`` 仅约束 LLM，媒体提供商单独用 ``media_configured`` 展示。
    """
    providers = [
        _provider_status(
            "deepseek",
            settings.DEEPSEEK_API_KEY,
            settings.DEEPSEEK_BASE_URL,
            settings.DEEPSEEK_MODEL,
        ),
        _provider_status(
            "glm",
            settings.GLM_API_KEY,
            settings.GLM_BASE_URL,
            settings.GLM_MODEL,
        ),
    ]
    # 媒体提供商（图片/视频/TTS）：缺失时链路会回退占位媒体，需提前暴露配置缺口
    media_providers = [
        _provider_status(
            "bailian",
            settings.BAILIAN_API_KEY,
            settings.BAILIAN_BASE_URL,
            settings.BAILIAN_VIDEO_MODEL,
        ),
        _provider_status(
            "agnes",
            settings.AGNES_API_KEY,
            settings.AGNES_BASE_URL,
            settings.AGNES_VIDEO_MODEL,
        ),
        _provider_status(
            "qwen-tts",
            settings.QWEN_TTS_API_KEY,
            settings.QWEN_TTS_BASE_URL,
            settings.QWEN_TTS_MODEL,
        ),
    ]
    all_configured = all(item["api_key_configured"] for item in providers)
    media_configured = all(item["api_key_configured"] for item in media_providers)
    return success_envelope(
        {
            "status": "ok" if all_configured else "degraded",
            "providers": providers,
            "media_providers": media_providers,
            "all_configured": all_configured,
            "media_configured": media_configured,
        },
        request_id,
        message="ok" if all_configured else "存在未配置密钥的模型提供商",
    )

"""模型配置域路由模块（/model-configs）。

提供应用内模型配置页所需的全部端点：
- ``GET    /model-configs``                  列出配置（可按 capability 过滤）；
- ``POST   /model-configs``                  新增配置；
- ``PATCH  /model-configs/{config_id}``      编辑配置（部分更新）；
- ``DELETE /model-configs/{config_id}``      删除配置（连带清理路由与单价）；
- ``POST   /model-configs/{config_id}/test``      测试连接（真实最小调用）；
- ``POST   /model-configs/{config_id}/activate``   设为该类型默认模型。

配置写入 ``model_registry``，网关每次生成时实时读取，故修改即时生效，
无需重启服务。密钥仅以掩码形式下发。

安全约束：全部端点（含列表读取）要求 ``X-Admin-Token`` 与
``settings.ADMIN_TOKEN`` 匹配；普通登录用户无权读写平台模型配置。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_request_id, require_admin_token
from app.core.errors import success_envelope
from app.schemas.model_config import ModelConfigCreateRequest, ModelConfigUpdateRequest
from app.services import model_config_service

#: 模型配置路由（主控聚合：prefix=/model-configs, tags=[model-configs]）
router = APIRouter(prefix="/model-configs", tags=["model-configs"])


@router.get("")
async def list_model_configs(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    capability: Annotated[str | None, Query(description="按模型类型过滤：llm/image/tts/video")] = None,
    _admin: Annotated[None, Depends(require_admin_token)] = None,
) -> dict[str, Any]:
    """查询模型配置列表（管理端专属；密钥掩码，附默认模型标记）。

    Args:
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        capability: 可选能力过滤。
        _admin: 管理端口令校验占位（不参与业务）。

    Returns:
        成功包络，data 含 items（配置列表）与 capability_meta（类型元信息）。

    Raises:
        BizError: 10001 capability 非法；20001 未登录。
    """
    data = await model_config_service.list_configs(session, capability)
    return success_envelope(data, request_id)


@router.post("")
async def create_model_config(
    payload: ModelConfigCreateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    _admin: Annotated[None, Depends(require_admin_token)] = None,
) -> dict[str, Any]:
    """新增模型配置（管理端专属：X-Admin-Token）。

    Args:
        payload: 新增请求体。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        _admin: 管理端口令校验占位（不参与业务）。

    Returns:
        成功包络，data 为新建配置（密钥掩码）。

    Raises:
        BizError: 10001 参数非法；90001 模型键已存在；20001 未登录。
    """
    data = await model_config_service.create_config(
        session,
        capability=payload.capability,
        model_key=payload.model_key,
        model_name=payload.model_name,
        protocol=payload.protocol,
        base_url=payload.base_url,
        api_key=payload.api_key,
        api_model=payload.api_model,
        extra_config=payload.extra_config,
        unit=payload.unit,
        unit_price_credits=payload.unit_price_credits,
        quality_score=payload.quality_score,
        is_available=payload.is_available,
        is_default=payload.is_default,
    )
    return success_envelope(data, request_id)


@router.patch("/{config_id}")
async def update_model_config(
    config_id: int,
    payload: ModelConfigUpdateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    _admin: Annotated[None, Depends(require_admin_token)] = None,
) -> dict[str, Any]:
    """编辑模型配置（管理端专属；api_key 缺省表示不修改）。

    Args:
        config_id: 模型注册行 ID。
        payload: 编辑请求体。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        _admin: 管理端口令校验占位（不参与业务）。

    Returns:
        成功包络，data 为更新后的配置（密钥掩码）。

    Raises:
        BizError: 10002 配置不存在；10001 参数非法；20001 未登录。
    """
    fields = payload.model_dump(exclude_unset=True)
    data = await model_config_service.update_config(
        session,
        config_id,
        model_name=fields.get("model_name"),
        protocol=fields.get("protocol"),
        base_url=fields.get("base_url"),
        api_key=fields.get("api_key"),
        api_key_present="api_key" in fields,
        api_model=fields.get("api_model"),
        extra_config=fields.get("extra_config"),
        unit=fields.get("unit"),
        unit_price_credits=fields.get("unit_price_credits"),
        quality_score=fields.get("quality_score"),
        is_available=fields.get("is_available"),
    )
    return success_envelope(data, request_id)


@router.delete("/{config_id}")
async def delete_model_config(
    config_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    _admin: Annotated[None, Depends(require_admin_token)] = None,
) -> dict[str, Any]:
    """删除模型配置（管理端专属；连带清理其路由与计费规则）。

    Args:
        config_id: 模型注册行 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        _admin: 管理端口令校验占位（不参与业务）。

    Returns:
        成功包络，data 为 None。

    Raises:
        BizError: 10002 配置不存在；90001 该模型为某任务类型最后可用模型。
    """
    await model_config_service.delete_config(session, config_id)
    return success_envelope(None, request_id)


@router.post("/{config_id}/test")
async def test_model_config(
    config_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    _admin: Annotated[None, Depends(require_admin_token)] = None,
) -> dict[str, Any]:
    """测试模型配置连通性（管理端专属；真实最小调用，视频仅提交任务）。

    Args:
        config_id: 模型注册行 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        _admin: 管理端口令校验占位（不参与业务）。

    Returns:
        成功包络，data 含 ok / message / latency_ms / detail。

    Raises:
        BizError: 10002 配置不存在；20001 未登录。
    """
    data = await model_config_service.test_connection(session, config_id)
    return success_envelope(data, request_id)


@router.post("/{config_id}/activate")
async def activate_model_config(
    config_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    _admin: Annotated[None, Depends(require_admin_token)] = None,
) -> dict[str, Any]:
    """将模型设为所属类型的默认模型（管理端专属）。

    Args:
        config_id: 模型注册行 ID。
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        _admin: 管理端口令校验占位（不参与业务）。

    Returns:
        成功包络，data 为更新后的配置（is_default=True）。

    Raises:
        BizError: 10002 配置不存在；20001 未登录。
    """
    data = await model_config_service.activate_config(session, config_id)
    return success_envelope(data, request_id)

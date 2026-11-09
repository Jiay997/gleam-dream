"""模型配置服务模块。

面向应用内「模型配置」页，维护 ``model_registry`` 中文本 / 图片 / 语音 / 视频
四类模型的接入参数（模型名、接口地址、密钥、协议、计费口径）：

- **查询**：按能力分组列出配置，密钥仅返回掩码与"是否已配置"布尔位；
- **新增 / 编辑 / 删除**：校验能力与协议匹配、计费单位合法、模型键唯一；
- **设为默认**：重写该能力对应任务类型的 ``model_routes``（priority 1 指向
  本模型，其余模型顺延为候选），使生成流程优先调用该配置；
- **测试连接**：按配置现场构建提供商发起一次最小真实调用（视频仅提交任务，
  不等待成片），验证凭据 / 地址 / 模型名是否有效。

即时生效约定：网关每次调用都从 ``model_registry`` 读取配置行并现场构建
提供商实例（见 :meth:`ModelGateway.build_provider`），因此本模块写入后
无需重启服务，下一次生成即使用新配置。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.infra.model_gateway import GenRequest, model_gateway
from app.models import ModelRegistry, ModelRoute, PriceRule

logger = logging.getLogger("gleamdream.model_config")

#: 模型配置页暴露的四类能力（bgm 仍由沙箱固定承担，不在配置范围）
CONFIG_CAPABILITIES: tuple[str, ...] = ("llm", "image", "tts", "video")

#: 能力 → 生成任务类型（用于路由绑定与"默认模型"判定）
CAPABILITY_TASK_TYPES: dict[str, tuple[str, ...]] = {
    "llm": ("llm_text",),
    "image": ("text_to_image", "image_to_image"),
    "tts": ("tts",),
    "video": ("text_to_video", "image_to_video"),
}

#: 能力 → 可选协议（Agnes 为当前默认媒体提供商，百炼保留为降级）
CAPABILITY_PROTOCOLS: dict[str, tuple[str, ...]] = {
    "llm": ("openai_chat", "stub"),
    "image": ("agnes_image", "dashscope_image", "stub"),
    "tts": ("qwen_tts", "dashscope_tts", "stub"),
    "video": ("agnes_video", "dashscope_video", "stub"),
}

#: 协议 → 默认 provider 标识（用于记账与内置提供商回退）
PROTOCOL_PROVIDER: dict[str, str] = {
    "openai_chat": "openai-compatible",
    "agnes_image": "agnes",
    "agnes_video": "agnes",
    "qwen_tts": "qwen-tts",
    "dashscope_image": "bailian",
    "dashscope_video": "bailian",
    "dashscope_tts": "bailian",
    "stub": "stub",
}

#: 能力 → 合法计费单位（per_call 为通用兜底）
CAPABILITY_UNITS: dict[str, tuple[str, ...]] = {
    "llm": ("per_1k_token", "per_call"),
    "image": ("per_image", "per_call"),
    "tts": ("per_second", "per_call"),
    "video": ("per_second", "per_call"),
}

#: 模型键命名约束（小写字母开头，允许数字 / 点 / 下划线 / 连字符）
_MODEL_KEY_RE = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")

#: 测试连接的最小请求（按能力区分；视频只提交不等待成片）
_TEST_PROMPTS: dict[str, tuple[str, dict[str, Any]]] = {
    "llm": ("请只回复四个字：连接成功", {"max_tokens": 64}),
    "image": ("纯深蓝背景中央一枚发光的青蓝色光球，极简摄影风格", {"size": "1024x1024"}),
    "tts": ("连接测试。", {}),
    "video": ("夜色中缓慢推进的镜头，一盏路灯照亮湿润的街道", {"duration_ms": 4000, "test_only": True}),
}


def mask_api_key(api_key: str | None) -> str:
    """生成密钥掩码（接口下发用，绝不返回明文）。

    Args:
        api_key: 原始密钥（可能为空）。

    Returns:
        如 ``sk-****abcd`` 的掩码；未配置时返回空串。
    """
    key = (api_key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


def _task_types(capability: str) -> tuple[str, ...]:
    """获取能力对应的生成任务类型。

    Args:
        capability: 能力名（llm/image/tts/video）。

    Returns:
        该能力对应的任务类型元组。

    Raises:
        BizError: 10001 能力不在配置范围。
    """
    task_types = CAPABILITY_TASK_TYPES.get(capability)
    if task_types is None:
        raise BizError(ErrorCode.PARAM_INVALID, f"不支持的模型类型: {capability}")
    return task_types


def _validate_payload(
    capability: str,
    protocol: str,
    unit: str,
    base_url: str | None,
    api_model: str | None,
) -> None:
    """校验能力 / 协议 / 计费单位 / 必要参数的一致性。

    Args:
        capability: 能力名。
        protocol: 调用协议。
        unit: 计费单位。
        base_url: 接口地址（stub 协议可为空）。
        api_model: 三方 API 模型名（stub 协议可为空）。

    Returns:
        无。

    Raises:
        BizError: 10001 参数不合法（能力未知 / 协议与能力不匹配 / 单位非法 /
            非 stub 协议缺少接口地址或模型名）。
    """
    _task_types(capability)
    allowed_protocols = CAPABILITY_PROTOCOLS[capability]
    if protocol not in allowed_protocols:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"协议 {protocol} 不适用于{capability}模型，可选：{'/'.join(allowed_protocols)}",
        )
    allowed_units = CAPABILITY_UNITS[capability]
    if unit not in allowed_units:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"计费单位 {unit} 不适用于{capability}模型，可选：{'/'.join(allowed_units)}",
        )
    if protocol != "stub":
        if not (base_url or "").strip():
            raise BizError(ErrorCode.PARAM_INVALID, "非沙箱协议必须填写接口地址")
        if not (api_model or "").strip():
            raise BizError(ErrorCode.PARAM_INVALID, "非沙箱协议必须填写 API 模型名")


async def _get_config(session: AsyncSession, config_id: int) -> ModelRegistry:
    """按 ID 读取模型配置行。

    Args:
        session: 数据库会话。
        config_id: 模型注册行 ID。

    Returns:
        模型注册行。

    Raises:
        BizError: 10002 配置不存在。
    """
    row = (
        await session.execute(select(ModelRegistry).where(ModelRegistry.id == config_id))
    ).scalar_one_or_none()
    if row is None:
        raise BizError(ErrorCode.NOT_FOUND, "模型配置不存在")
    return row


async def _default_keys(session: AsyncSession, task_types: tuple[str, ...]) -> dict[str, str | None]:
    """查询各任务类型当前 priority 1 的模型键。

    Args:
        session: 数据库会话。
        task_types: 任务类型集合。

    Returns:
        任务类型 → 默认（priority 1 且启用）模型键的字典；无路由时值为 None。
    """
    rows = (
        await session.execute(
            select(ModelRoute).where(ModelRoute.task_type.in_(task_types))
        )
    ).scalars().all()
    by_type: dict[str, str | None] = {task_type: None for task_type in task_types}
    for route in rows:
        if route.priority == 1 and route.is_active:
            by_type[route.task_type] = route.model_key
    return by_type


def serialize_config(row: ModelRegistry, default_keys: dict[str, str | None]) -> dict[str, Any]:
    """将模型注册行序列化为接口返回结构（密钥掩码）。

    Args:
        row: 模型注册行。
        default_keys: 各任务类型当前默认模型键（用于计算 is_default）。

    Returns:
        可直接 JSON 序列化的配置字典，含掩码密钥与默认标记。
    """
    task_types = CAPABILITY_TASK_TYPES.get(row.capability, ())
    is_default = bool(task_types) and all(
        default_keys.get(task_type) == row.model_key for task_type in task_types
    )
    return {
        "id": int(row.id),
        "model_key": row.model_key,
        "model_name": row.model_name,
        "provider": row.provider,
        "capability": row.capability,
        "protocol": row.protocol or "",
        "base_url": row.base_url or "",
        "api_model": row.api_model or "",
        "api_key_configured": bool((row.api_key or "").strip()),
        "api_key_masked": mask_api_key(row.api_key),
        "extra_config": row.extra_config or {},
        "unit": row.unit,
        "unit_price_credits": int(row.unit_price_credits or 0),
        "quality_score": float(row.quality_score) if row.quality_score is not None else None,
        "is_available": bool(row.is_available),
        "is_default": is_default,
        "task_types": list(task_types),
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def list_configs(
    session: AsyncSession, capability: str | None = None
) -> dict[str, Any]:
    """列出模型配置（可按能力过滤）。

    Args:
        session: 数据库会话。
        capability: 能力过滤（llm/image/tts/video）；为空返回四类全部。

    Returns:
        含 ``items``（配置列表）与 ``capabilities``（四类能力元信息）的字典。

    Raises:
        BizError: 10001 能力非法。
    """
    if capability:
        _task_types(capability)
        capabilities = (capability,)
    else:
        capabilities = CONFIG_CAPABILITIES

    rows = (
        await session.execute(
            select(ModelRegistry)
            .where(ModelRegistry.capability.in_(capabilities))
            .order_by(ModelRegistry.capability.asc(), ModelRegistry.id.asc())
        )
    ).scalars().all()

    default_map: dict[str, dict[str, str | None]] = {}
    for item in capabilities:
        default_map[item] = await _default_keys(session, CAPABILITY_TASK_TYPES[item])

    items = [
        serialize_config(row, default_map.get(row.capability, {}))
        for row in rows
    ]
    return {
        "items": items,
        "capability_meta": [
            {
                "capability": item,
                "protocols": list(CAPABILITY_PROTOCOLS[item]),
                "units": list(CAPABILITY_UNITS[item]),
                "task_types": list(CAPABILITY_TASK_TYPES[item]),
            }
            for item in CONFIG_CAPABILITIES
        ],
    }


async def create_config(
    session: AsyncSession,
    *,
    capability: str,
    model_key: str,
    model_name: str,
    protocol: str,
    base_url: str | None = None,
    api_key: str | None = None,
    api_model: str | None = None,
    extra_config: dict[str, Any] | None = None,
    unit: str = "per_call",
    unit_price_credits: int = 0,
    quality_score: float | None = None,
    is_available: bool = True,
    is_default: bool = False,
) -> dict[str, Any]:
    """新增一个模型配置。

    Args:
        session: 数据库会话。
        capability: 能力（llm/image/tts/video）。
        model_key: 模型键（唯一，路由与记账使用）。
        model_name: 展示名称。
        protocol: 调用协议。
        base_url: 接口地址（TTS 填 WebSocket 地址）。
        api_key: 访问密钥（可空，空则回退 .env 默认）。
        api_model: 三方 API 模型名。
        extra_config: 扩展参数（如 ``{"voice": "Cherry"}``）。
        unit: 计费单位。
        unit_price_credits: 单价（积分）。
        quality_score: 质量分（0~10）。
        is_available: 是否启用。
        is_default: 是否设为该类型默认模型（重写路由 priority 1）。

    Returns:
        新建配置的序列化字典。

    Raises:
        BizError: 10001 参数非法；90001 模型键已存在。
    """
    if not _MODEL_KEY_RE.match(model_key):
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "模型键须以小写字母开头，仅含小写字母/数字/点/下划线/连字符（2~64 位）",
        )
    _validate_payload(capability, protocol, unit, base_url, api_model)
    exists = (
        await session.execute(
            select(ModelRegistry.id).where(ModelRegistry.model_key == model_key)
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise BizError(ErrorCode.CONFIG_CONFLICT, f"模型键已存在: {model_key}")

    row = ModelRegistry(
        model_key=model_key,
        model_name=model_name.strip(),
        provider=PROTOCOL_PROVIDER[protocol],
        capability=capability,
        task_types=list(_task_types(capability)),
        quality_score=quality_score,
        unit=unit,
        unit_price_credits=unit_price_credits,
        is_available=is_available,
        region="cn",
        protocol=protocol,
        base_url=(base_url or "").strip().rstrip("/") or None,
        api_key=(api_key or "").strip() or None,
        api_model=(api_model or "").strip() or None,
        extra_config=extra_config or None,
    )
    session.add(row)
    await session.flush()

    if is_default:
        await _rebind_routes(session, row)

    await session.commit()
    # updated_at 由 DB 端 now() 维护，UPDATE/INSERT 后需刷新再序列化
    await session.refresh(row)
    defaults = await _default_keys(session, _task_types(capability))
    logger.info(
        "模型配置新增 key=%s capability=%s protocol=%s",
        model_key, capability, protocol,
    )
    return serialize_config(row, defaults)


async def update_config(
    session: AsyncSession,
    config_id: int,
    *,
    model_name: str | None = None,
    protocol: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    api_key_present: bool = False,
    api_model: str | None = None,
    extra_config: dict[str, Any] | None = None,
    unit: str | None = None,
    unit_price_credits: int | None = None,
    quality_score: float | None = None,
    is_available: bool | None = None,
) -> dict[str, Any]:
    """更新模型配置（部分字段）。

    密钥语义：``api_key_present=False`` 表示不修改；为 True 时，
    空串表示清除密钥（回退 .env），非空写入新密钥。

    Args:
        session: 数据库会话。
        config_id: 模型注册行 ID。
        model_name: 展示名称。
        protocol: 调用协议。
        base_url: 接口地址。
        api_key: 新密钥。
        api_key_present: 请求是否显式携带 api_key 字段。
        api_model: 三方 API 模型名。
        extra_config: 扩展参数。
        unit: 计费单位。
        unit_price_credits: 单价。
        quality_score: 质量分。
        is_available: 是否启用。

    Returns:
        更新后的配置序列化字典。

    Raises:
        BizError: 10002 配置不存在；10001 参数非法。
    """
    row = await _get_config(session, config_id)
    next_protocol = protocol if protocol is not None else (row.protocol or "stub")
    next_unit = unit if unit is not None else row.unit
    next_base_url = base_url if base_url is not None else row.base_url
    next_api_model = api_model if api_model is not None else row.api_model
    _validate_payload(row.capability, next_protocol, next_unit, next_base_url, next_api_model)

    if model_name is not None:
        row.model_name = model_name.strip()
    if protocol is not None:
        row.protocol = protocol
        row.provider = PROTOCOL_PROVIDER[protocol]
    if base_url is not None:
        row.base_url = base_url.strip().rstrip("/") or None
    if api_model is not None:
        row.api_model = api_model.strip() or None
    if api_key_present:
        row.api_key = (api_key or "").strip() or None
    if extra_config is not None:
        row.extra_config = extra_config or None
    if unit is not None:
        row.unit = unit
    if unit_price_credits is not None:
        row.unit_price_credits = unit_price_credits
    if quality_score is not None:
        row.quality_score = quality_score
    if is_available is not None:
        row.is_available = is_available

    await session.commit()
    # updated_at 为 DB 端 now()（onupdate），提交后刷新避免过期属性懒加载
    await session.refresh(row)
    defaults = await _default_keys(session, _task_types(row.capability))
    logger.info("模型配置更新 id=%s key=%s", config_id, row.model_key)
    return serialize_config(row, defaults)


async def delete_config(session: AsyncSession, config_id: int) -> None:
    """删除模型配置，并清理其路由与计费规则。

    守卫：若删除后某任务类型将没有任何"指向可用模型"的路由，则拒绝
    （避免生成流程失去候选模型）。

    Args:
        session: 数据库会话。
        config_id: 模型注册行 ID。

    Returns:
        无。

    Raises:
        BizError: 10002 配置不存在；90001 该模型为某任务类型最后可用模型。
    """
    row = await _get_config(session, config_id)
    task_types = _task_types(row.capability)

    if row.is_available:
        routes = (
            await session.execute(
                select(ModelRoute).where(ModelRoute.task_type.in_(task_types))
            )
        ).scalars().all()
        other_keys = {
            route.model_key
            for route in routes
            if route.is_active and route.model_key != row.model_key
        }
        if other_keys:
            available = (
                await session.execute(
                    select(ModelRegistry.model_key).where(
                        ModelRegistry.model_key.in_(other_keys),
                        ModelRegistry.is_available.is_(True),
                    )
                )
            ).scalars().all()
            available_set = set(available)
        else:
            available_set = set()
        for task_type in task_types:
            has_route = any(
                route.task_type == task_type
                and route.model_key != row.model_key
                and route.model_key in available_set
                for route in routes
            )
            if not has_route:
                raise BizError(
                    ErrorCode.CONFIG_CONFLICT,
                    f"任务类型 {task_type} 仅剩该可用模型，请先新增替代模型再删除",
                )

    await session.execute(
        delete(ModelRoute).where(ModelRoute.model_key == row.model_key)
    )
    await session.execute(
        delete(PriceRule).where(PriceRule.model_key == row.model_key)
    )
    model_key = row.model_key
    await session.delete(row)
    await session.commit()
    logger.info("模型配置删除 key=%s", model_key)


async def _rebind_routes(session: AsyncSession, row: ModelRegistry) -> None:
    """把某能力对应任务类型的 priority 1 路由重绑到目标模型。

    实现：按任务类型删除旧路由后按新顺序重建（目标优先，其余按原优先级
    顺延），避免 ``(task_type, priority)`` 唯一约束冲突。

    Args:
        session: 数据库会话（调用方负责提交）。
        row: 目标模型注册行。

    Returns:
        无。

    Raises:
        无（参数已由调用方校验）。
    """
    task_types = _task_types(row.capability)
    existing = (
        await session.execute(
            select(ModelRoute).where(ModelRoute.task_type.in_(task_types))
        )
    ).scalars().all()

    for task_type in task_types:
        ordered = sorted(
            [route for route in existing if route.task_type == task_type],
            key=lambda route: route.priority,
        )
        await session.execute(
            delete(ModelRoute).where(ModelRoute.task_type == task_type)
        )
        await session.flush()

        keys = [row.model_key] + [
            route.model_key for route in ordered if route.model_key != row.model_key
        ]
        for index, key in enumerate(keys, start=1):
            source = next((route for route in ordered if route.model_key == key), None)
            session.add(
                ModelRoute(
                    task_type=task_type,
                    priority=index,
                    model_key=key,
                    condition=source.condition if source is not None else None,
                    is_fallback=(source.is_fallback if source is not None else True)
                    if index > 1
                    else False,
                    is_active=True,
                )
            )
    await session.flush()


async def activate_config(session: AsyncSession, config_id: int) -> dict[str, Any]:
    """将模型设为所属能力的默认模型（路由 priority 1）并启用。

    Args:
        session: 数据库会话。
        config_id: 模型注册行 ID。

    Returns:
        更新后的配置序列化字典。

    Raises:
        BizError: 10002 配置不存在。
    """
    row = await _get_config(session, config_id)
    row.is_available = True
    await _rebind_routes(session, row)
    await session.commit()
    await session.refresh(row)
    defaults = await _default_keys(session, _task_types(row.capability))
    logger.info("模型配置设为默认 key=%s capability=%s", row.model_key, row.capability)
    return serialize_config(row, defaults)


async def test_connection(session: AsyncSession, config_id: int) -> dict[str, Any]:
    """测试模型配置连通性（按能力发起一次最小真实调用）。

    视频协议仅提交异步任务、不轮询成片，任务被受理即视为连接成功。

    Args:
        session: 数据库会话。
        config_id: 模型注册行 ID。

    Returns:
        测试结果字典：ok / message / latency_ms / detail。

    Raises:
        BizError: 10002 配置不存在。
    """
    row = await _get_config(session, config_id)
    provider = model_gateway.build_provider(row)
    if provider is None or not provider.supports(row.capability):
        return {
            "ok": False,
            "message": "配置不完整：请检查协议、接口地址与 API 模型名",
            "latency_ms": 0,
            "detail": {"model_key": row.model_key, "protocol": row.protocol or ""},
        }

    prompt, params = _TEST_PROMPTS[row.capability]
    request = GenRequest(capability=row.capability, prompt=prompt, params=dict(params))
    started = time.monotonic()
    result = await provider.generate(request, row)
    latency_ms = int((time.monotonic() - started) * 1000)
    if not result.ok:
        return {
            "ok": False,
            "message": result.error or "连接失败",
            "latency_ms": latency_ms,
            "detail": {"model_key": row.model_key},
        }

    detail: dict[str, Any] = {"model_key": row.model_key}
    for key in ("api_model", "mime_type", "width", "height", "duration_ms", "task_id", "test_only", "simulated"):
        if key in result.meta:
            detail[key] = result.meta[key]
    if result.text:
        detail["text_preview"] = result.text[:40]
    if result.object_key:
        detail["object_key"] = result.object_key
    message = (
        "连接成功（视频任务已受理，成片将异步生成）"
        if row.capability == "video"
        else "连接成功"
    )
    return {"ok": True, "message": message, "latency_ms": latency_ms, "detail": detail}

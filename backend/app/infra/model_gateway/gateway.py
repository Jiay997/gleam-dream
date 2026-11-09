"""模型网关统一入口模块。

:class:`ModelGateway` 是所有模型调用的唯一入口，串联三件事：

1. **确定性路由**（ADR-07）：通过 :func:`app.infra.model_gateway.router.pick_model`
   从 model_registry / model_routes 选出模型（支持用户锁定模型）；
2. **提供商调用**：按 registry.provider 分发到注册表中的提供商实例
   （deepseek / glm 走 OpenAI 兼容实现，stub 走沙箱占位实现）；
3. **记账留痕**：无论成功失败，都在独立短会话中追加一条
   :class:`~app.models.AiCallLog`（token/时长/成本/trace_id/参数摘要），
   并更新熔断器计数。

会话策略：网关可能在后台任务中运行，**不依赖请求级 get_db**，每次需要
DB 时自建短会话（``session_factory`` 可注入）。日志写入失败仅告警，
绝不影响生成主流程。

call_type 口径：数据库 ``ck_ai_call_logs_call_type`` CHECK 约束已扩展纳入
``llm_text``（迁移 a7c1d2e94b05），LLM 文本调用原样落库，模型成本观测
可按调用类型直接聚合（历史 compose 承载数据保留不动）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_request_id
from app.infra.model_gateway.base import (
    CAPABILITY_IMAGE,
    CAPABILITY_LLM,
    CAPABILITY_TTS,
    CAPABILITY_VIDEO,
    GenRequest,
    GenResult,
    ModelProvider,
)
from app.infra.model_gateway.agnes import AgnesProvider
from app.infra.model_gateway.bailian import BailianProvider
from app.infra.model_gateway.openai_compat import OpenAICompatProvider
from app.infra.model_gateway.qwen_tts import QwenTtsProvider
from app.infra.model_gateway.router import circuit_breaker, pick_model, pick_models
from app.infra.model_gateway.stub import StubProvider
from app.infra.storage import storage
from app.models import AiCallLog, ModelRegistry

logger = logging.getLogger("gleamdream.gateway")

# 会话工厂类型别名（便于注入测试/后台任务）
SessionFactory = async_sessionmaker[AsyncSession]

#: 可配置协议 → 能力映射（网关按 protocol 动态构建提供商实例）
PROTOCOL_CAPABILITY: dict[str, str] = {
    "openai_chat": CAPABILITY_LLM,
    "dashscope_image": CAPABILITY_IMAGE,
    "dashscope_video": CAPABILITY_VIDEO,
    "dashscope_tts": CAPABILITY_TTS,
    "agnes_image": CAPABILITY_IMAGE,
    "agnes_video": CAPABILITY_VIDEO,
    "qwen_tts": CAPABILITY_TTS,
}

# ai_call_logs.call_type 的 DB CHECK 约束允许值（9 种渲染任务类型 + llm_text）
_LOGGABLE_CALL_TYPES: frozenset[str] = frozenset({
    "text_to_image", "image_to_image", "text_to_video", "image_to_video",
    "tts", "bgm", "subtitle", "compose", "upscale", "llm_text",
})
# 未知任务类型的兜底承载类型
_LLM_CALL_TYPE_FALLBACK: str = "compose"

# 请求快照中 prompt 预览最大长度
_SNAPSHOT_PROMPT_PREVIEW: int = 200
# 错误码字段最大长度（ai_call_logs.error_code VARCHAR(64)）
_ERROR_CODE_MAX_LEN: int = 64


def log_call_type(task_type: str) -> str:
    """将网关任务类型映射为 ai_call_logs.call_type 可落库的值。

    CHECK 约束已含 ``llm_text``（迁移 a7c1d2e94b05）；未知类型兜底为
    ``compose`` 以防日志写入被约束拒绝（记账失败仅告警语义优先）。

    Args:
        task_type: 网关层任务类型（如 llm_text / text_to_image）。

    Returns:
        可直接写入 ai_call_logs.call_type 的字符串。
    """
    return task_type if task_type in _LOGGABLE_CALL_TYPES else _LLM_CALL_TYPE_FALLBACK


def compute_cost(registry: ModelRegistry, meta: dict[str, Any]) -> int:
    """按模型计费单位与调用元数据计算积分成本（向上取整，恒 ≥0）。

    规则（对齐 model_registry.unit 四值）：
    - ``per_1k_token``（LLM）：``ceil((tokens_in + tokens_out) / 1000 * unit_price)``，
      采用纯整数运算避免浮点误差；同时计入输出 token，避免成本被低估；
    - ``per_image``：固定单价；
    - ``per_second``：``ceil(duration_ms / 1000) * unit_price``；
    - ``per_call``：固定单价。

    Args:
        registry: 模型注册行（unit / unit_price_credits）。
        meta: 生成结果元数据（LLM 取 tokens_in/tokens_out，按秒计费取 duration_ms）。

    Returns:
        本次调用成本（积分，非负整数）。
    """
    price = int(registry.unit_price_credits or 0)
    unit = registry.unit
    if unit == "per_1k_token":
        tokens_in = int(meta.get("tokens_in") or 0)
        tokens_out = int(meta.get("tokens_out") or 0)
        return ((tokens_in + tokens_out) * price + 999) // 1000
    if unit == "per_image" or unit == "per_call":
        return price
    if unit == "per_second":
        duration_ms = int(meta.get("duration_ms") or 0)
        return ((duration_ms + 999) // 1000) * price
    return 0


def _snapshot_param_value(value: Any) -> Any:
    """压缩快照中的超长参数值（如 I2V 首帧 data URI）。

    Args:
        value: 原始参数值。

    Returns:
        短字符串原样返回；超长字符串替换为 ``{"redacted": True, "len": N, "prefix": ...}``，
        避免单条 ai_call_logs 写入数百 KB 的 base64 图片。
    """
    if isinstance(value, str) and len(value) > 512:
        return {"redacted": True, "len": len(value), "prefix": value[:48]}
    if isinstance(value, dict):
        return {k: _snapshot_param_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_param_value(v) for v in value]
    return value


def build_request_snapshot(request: GenRequest, task_type: str) -> dict[str, Any]:
    """构造请求快照（参数摘要，绝不含密钥）。

    Args:
        request: 统一生成请求。
        task_type: 任务类型。

    Returns:
        可 JSONB 落库的摘要字典（能力/任务类型/模型锁/prompt 预览/参数）。
    """
    sensitive_keys = {"api_key", "authorization", "secret", "token", "password"}
    safe_params = {
        key: _snapshot_param_value(value)
        for key, value in request.params.items()
        if str(key).lower() not in sensitive_keys
    }
    prompt = request.prompt or ""
    return {
        "task_type": task_type,
        "capability": request.capability,
        "model_hint": request.model_hint,
        "prompt_len": len(prompt),
        "prompt_preview": prompt[:_SNAPSHOT_PROMPT_PREVIEW],
        "has_system_prompt": bool(request.system_prompt),
        "messages_count": len(request.messages or []),
        "params": safe_params,
    }


def build_response_snapshot(result: GenResult) -> dict[str, Any]:
    """构造响应快照（结果摘要，绝不含密钥）。

    Args:
        result: 统一生成结果。

    Returns:
        可 JSONB 落库的摘要字典（文本预览或对象键/媒体元信息）。
    """
    snapshot: dict[str, Any] = {"ok": result.ok}
    if result.text is not None:
        snapshot["text_preview"] = result.text[:_SNAPSHOT_PROMPT_PREVIEW]
        snapshot["text_len"] = len(result.text)
    if result.object_key is not None:
        snapshot["object_key"] = result.object_key
    for key in (
        "simulated", "placeholder", "mime_type", "width", "height", "duration_ms",
        "finish_reason", "reasoning_tokens",
    ):
        if key in result.meta:
            snapshot[key] = result.meta[key]
    return snapshot


class ModelGateway:
    """统一模型网关：路由 → 提供商调用 → 记账与熔断。

    Attributes:
        _providers: 提供商注册表（键为 model_registry.provider 值）。
    """

    def __init__(self) -> None:
        """构造网关并注册内置提供商。

        密钥仅从 settings 读取一次注入提供商实例，网关层不保存明文。

        Raises:
            无（存储与配置初始化失败会在此前阶段暴露）。
        """
        self._providers: dict[str, ModelProvider] = {
            "deepseek": OpenAICompatProvider(
                name="deepseek",
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.DEEPSEEK_BASE_URL,
                default_model=settings.DEEPSEEK_MODEL,
            ),
            "glm": OpenAICompatProvider(
                name="glm",
                api_key=settings.GLM_API_KEY,
                base_url=settings.GLM_BASE_URL,
                default_model=settings.GLM_MODEL,
            ),
            "stub": StubProvider(storage),
            # 阿里云百炼（AI 漫剧）：TTS / 文生图 / 视频真实调用
            "bailian": BailianProvider(storage),
            # Agnes AI：图片 / 视频真实调用（语音仍走百炼）
            "agnes": AgnesProvider(storage),
            # 千问 AI：qwen3-tts-flash 同步语音合成
            "qwen-tts": QwenTtsProvider(storage),
        }

    @property
    def providers(self) -> dict[str, ModelProvider]:
        """获取提供商注册表（只读视图，测试/诊断用）。

        Returns:
            provider 名到实例的字典。
        """
        return dict(self._providers)

    @staticmethod
    def _env_defaults() -> dict[str, dict[str, str]]:
        """读取各提供商的 .env 默认接入参数（作为模型配置行的兜底值）。

        Returns:
            provider 名 → 参数键值（api_key/base_url/model 等）的字典。
            密钥仅在此处内存读取，不落日志、不出现在接口响应中。
        """
        return {
            "deepseek": {
                "api_key": settings.DEEPSEEK_API_KEY,
                "base_url": settings.DEEPSEEK_BASE_URL,
                "model": settings.DEEPSEEK_MODEL,
            },
            "glm": {
                "api_key": settings.GLM_API_KEY,
                "base_url": settings.GLM_BASE_URL,
                "model": settings.GLM_MODEL,
            },
            "bailian": {
                "api_key": settings.BAILIAN_API_KEY,
                "base_url": settings.BAILIAN_BASE_URL,
                "ws_url": settings.BAILIAN_WS_URL,
                "tts_model": settings.BAILIAN_TTS_MODEL,
                "image_model": settings.BAILIAN_IMAGE_MODEL,
                "video_model": settings.BAILIAN_VIDEO_MODEL,
                "voice": settings.BAILIAN_TTS_VOICE,
            },
            "agnes": {
                "api_key": settings.AGNES_API_KEY,
                "base_url": settings.AGNES_BASE_URL,
                "image_model": settings.AGNES_IMAGE_MODEL,
                "video_model": settings.AGNES_VIDEO_MODEL,
            },
            "qwen_tts": {
                "api_key": settings.QWEN_TTS_API_KEY,
                "base_url": settings.QWEN_TTS_BASE_URL,
                "model": settings.QWEN_TTS_MODEL,
                "voice": settings.QWEN_TTS_VOICE,
            },
        }

    def build_provider(self, registry: ModelRegistry) -> ModelProvider | None:
        """按模型注册行构建提供商实例（每次调用实时构建）。

        ``model_registry`` 由路由每次从数据库读取，本方法据行内 ``protocol``
        现场实例化提供商，因此模型配置页的修改在下一次调用立即生效。

        兜底规则：
        - ``protocol`` 为空或 ``stub`` → 回退进程级内置提供商（按 provider 名）；
        - 行内 ``api_key`` / ``base_url`` / ``api_model`` 为空 → 回退 .env 默认值，
          保证历史行无需回填即可继续工作。

        Args:
            registry: 路由命中的模型注册行。

        Returns:
            可用的提供商实例；协议非法或必要参数缺失时返回 None。
        """
        protocol = (registry.protocol or "").strip().lower()
        if protocol in ("", "stub"):
            return self._providers.get(registry.provider)

        env = self._env_defaults()
        if protocol == "openai_chat":
            defaults = env.get(registry.provider, {})
            base_url = (registry.base_url or defaults.get("base_url", "")).strip()
            api_model = (registry.api_model or defaults.get("model", "")).strip()
            if not base_url or not api_model:
                return None
            return OpenAICompatProvider(
                name=registry.provider or "openai-compatible",
                api_key=(registry.api_key or defaults.get("api_key", "")).strip(),
                base_url=base_url,
                default_model=api_model,
            )

        if protocol in ("agnes_image", "agnes_video"):
            agnes_env = env["agnes"]
            api_key = (registry.api_key or agnes_env["api_key"]).strip()
            base_url = (registry.base_url or agnes_env["base_url"]).strip()
            if not api_key or not base_url:
                return None
            if protocol == "agnes_image":
                return AgnesProvider(
                    storage,
                    api_key=api_key,
                    base_url=base_url,
                    image_model=registry.api_model or agnes_env["image_model"],
                    capabilities={CAPABILITY_IMAGE},
                )
            return AgnesProvider(
                storage,
                api_key=api_key,
                base_url=base_url,
                video_model=registry.api_model or agnes_env["video_model"],
                capabilities={CAPABILITY_VIDEO},
            )

        if protocol == "qwen_tts":
            tts_env = env["qwen_tts"]
            api_key = (registry.api_key or tts_env["api_key"]).strip()
            base_url = (registry.base_url or tts_env["base_url"]).strip()
            if not api_key or not base_url:
                return None
            extra = registry.extra_config or {}
            return QwenTtsProvider(
                storage,
                api_key=api_key,
                base_url=base_url,
                model=registry.api_model or tts_env["model"],
                default_voice=str(extra.get("voice") or tts_env["voice"]),
            )

        if protocol in PROTOCOL_CAPABILITY:
            capability = PROTOCOL_CAPABILITY[protocol]
            bailian_env = env["bailian"]
            api_key = (registry.api_key or bailian_env["api_key"]).strip()
            extra = registry.extra_config or {}
            if protocol == "dashscope_tts":
                # TTS 行的 base_url 存 WebSocket 推理地址
                ws_url = (registry.base_url or bailian_env["ws_url"]).strip()
                if not ws_url:
                    return None
                return BailianProvider(
                    storage,
                    api_key=api_key,
                    base_url=bailian_env["base_url"],
                    ws_url=ws_url,
                    tts_model=registry.api_model or bailian_env["tts_model"],
                    default_voice=str(extra.get("voice") or bailian_env["voice"]),
                    capabilities={capability},
                )
            base_url = (registry.base_url or bailian_env["base_url"]).strip()
            if not base_url:
                return None
            if protocol == "dashscope_image":
                return BailianProvider(
                    storage,
                    api_key=api_key,
                    base_url=base_url,
                    image_model=registry.api_model or bailian_env["image_model"],
                    capabilities={capability},
                )
            return BailianProvider(
                storage,
                api_key=api_key,
                base_url=base_url,
                video_model=registry.api_model or bailian_env["video_model"],
                capabilities={capability},
            )
        return None

    async def generate(
        self,
        capability: str,
        task_type: str,
        request: GenRequest,
        project_id: int | None = None,
        task_id: int | None = None,
        session_factory: SessionFactory = async_session_factory,
    ) -> GenResult:
        """执行一次完整生成：路由 → 调用 → 记账。

        路由失败抛出 :class:`BizError`（80001）；提供商调用失败返回
        ``ok=False`` 的结果。两种情况下均会尽力追加一条失败日志
        （调用类型/错误码/trace_id），且日志写入失败不影响主流程。

        Args:
            capability: 生成能力（llm/image/video/tts/bgm）。
            task_type: 任务类型（如 llm_text / text_to_image / tts）。
            request: 统一生成请求。
            project_id: 所属项目 ID（可选，仅用于日志关联）。
            task_id: 渲染任务 ID（可选，落库到 ai_call_logs.render_task_id）。
            session_factory: 会话工厂，默认全局 async_session_factory。

        Returns:
            统一生成结果（meta 额外携带 cost_credits / trace_id）。

        Raises:
            BizError: 80001 路由失败（锁定模型不可用/无路由/无候选），
                或提供商注册表中无对应 provider。
        """
        trace_id = new_request_id()
        started = time.monotonic()

        # ---- 1. 确定性路由（自建短会话） ----
        # LLM 允许最多 3 个候选：首个候选返回 5xx/超时/空响应时切换到下一
        # 路由，避免 DeepSeek 单点故障导致整条剧本链路失败（Q-004）。
        candidates: list[ModelRegistry] = []
        try:
            async with session_factory() as session:
                if capability == CAPABILITY_LLM:
                    candidates = await pick_models(
                        session, capability, task_type, request.model_hint, limit=3
                    )
                else:
                    candidates = [
                        await pick_model(session, capability, task_type, request.model_hint)
                    ]
        except BizError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            await self._write_call_log(
                session_factory=session_factory,
                registry=None,
                capability=capability,
                task_type=task_type,
                project_id=project_id,
                task_id=task_id,
                trace_id=trace_id,
                request=request,
                result=None,
                duration_ms=duration_ms,
                error=f"{exc.code}: {exc.message}",
            )
            raise
        if not candidates:
            raise BizError(ErrorCode.MODEL_UNAVAILABLE, "无可用候选模型")

        # ---- 2. 逐候选调用（LLM 失败自动降级到下一提供商） ----
        last_result: GenResult | None = None
        for index, registry in enumerate(candidates):
            attempt_started = time.monotonic()
            provider = self.build_provider(registry)
            if provider is None or not provider.supports(capability):
                error = (
                    f"提供商未注册或不支持该能力: provider={registry.provider}"
                    f" protocol={registry.protocol or '-'} capability={capability}"
                )
                logger.error("%s model=%s", error, registry.model_key)
                result = GenResult(
                    ok=False,
                    error=error,
                    meta={"provider": registry.provider, "model_key": registry.model_key},
                )
            else:
                try:
                    result = await provider.generate(request, registry)
                except Exception as exc:  # noqa: BLE001 - 供应商异常按本次候选失败处理
                    result = GenResult(
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        meta={"provider": registry.provider, "model_key": registry.model_key},
                    )
            duration_ms = int((time.monotonic() - attempt_started) * 1000)

            # ---- 3. 熔断计数 + 记账（每个候选都落一条日志） ----
            if result.ok:
                circuit_breaker.record_success(registry.model_key)
            else:
                circuit_breaker.record_failure(registry.model_key)
            cost_credits = compute_cost(registry, result.meta) if result.ok else 0
            result.meta.setdefault("model_key", registry.model_key)
            result.meta["cost_credits"] = cost_credits
            result.meta["trace_id"] = trace_id
            result.meta["duration_ms"] = result.meta.get("duration_ms") or duration_ms
            await self._write_call_log(
                session_factory=session_factory,
                registry=registry,
                capability=capability,
                task_type=task_type,
                project_id=project_id,
                task_id=task_id,
                trace_id=trace_id,
                request=request,
                result=result,
                duration_ms=duration_ms,
                error=result.error,
            )
            last_result = result
            if result.ok:
                return result
            # 仅 LLM 跨候选降级；图片/视频保持单模型语义，避免风格/计费漂移。
            if capability != CAPABILITY_LLM or index >= len(candidates) - 1:
                return result
            logger.warning(
                "LLM 候选失败，切换下一模型 task_type=%s failed=%s next=%s err=%s",
                task_type, registry.model_key, candidates[index + 1].model_key,
                str(result.error or "")[:200],
            )
        return last_result or GenResult(ok=False, error="无可用候选模型")

    async def _write_call_log(
        self,
        session_factory: SessionFactory,
        registry: ModelRegistry | None,
        capability: str,
        task_type: str,
        project_id: int | None,
        task_id: int | None,
        trace_id: str,
        request: GenRequest,
        result: GenResult | None,
        duration_ms: int,
        error: str | None,
    ) -> None:
        """在独立短会话中追加一条 AI 调用日志。

        计费：成功时按 :func:`compute_cost` 计算，失败恒 0。
        快照：请求/响应摘要，绝不含密钥。
        任何落库异常仅告警，不向调用方传播。

        Args:
            session_factory: 会话工厂。
            registry: 命中的模型注册行；路由失败时为 None。
            capability: 生成能力。
            task_type: 网关任务类型。
            project_id: 项目 ID（可选）。
            task_id: 渲染任务 ID（可选）。
            trace_id: 链路 ID（ULID）。
            request: 原始生成请求（用于构造请求快照）。
            result: 生成结果；路由失败时为 None。
            duration_ms: 本次调用总耗时（毫秒）。
            error: 失败摘要（成功时为 None）。
        """
        meta = result.meta if result is not None else {}
        tokens_in = int(meta.get("tokens_in") or 0)
        tokens_out = int(meta.get("tokens_out") or 0)
        cost = compute_cost(registry, meta) if (result is not None and result.ok and registry) else 0
        try:
            async with session_factory() as session:
                session.add(
                    AiCallLog(
                        project_id=project_id,
                        render_task_id=task_id,
                        model_key=registry.model_key if registry else None,
                        provider=registry.provider if registry else None,
                        call_type=log_call_type(task_type),
                        prompt_tokens=tokens_in or None,
                        completion_tokens=tokens_out or None,
                        total_tokens=(tokens_in + tokens_out) or None,
                        latency_ms=duration_ms,
                        cost_credits=max(0, cost),
                        status="success" if (result is not None and result.ok) else "failed",
                        error_code=error[:_ERROR_CODE_MAX_LEN] if error else None,
                        trace_id=trace_id,
                        request_snapshot=build_request_snapshot(request, task_type),
                        response_snapshot=build_response_snapshot(result) if result else None,
                    )
                )
                await session.commit()
        except Exception:
            logger.warning(
                "写入 ai_call_logs 失败 trace_id=%s task_type=%s", trace_id, task_type,
                exc_info=True,
            )


# 进程级网关单例
model_gateway = ModelGateway()

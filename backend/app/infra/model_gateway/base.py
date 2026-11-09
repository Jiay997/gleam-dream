"""模型网关抽象层模块。

定义网关统一的数据契约与提供商抽象：
- :class:`GenRequest`：跨能力的统一生成请求（llm/image/video/tts/bgm）；
- :class:`GenResult`：统一生成结果（文本或媒体对象键 + 元数据）；
- :class:`ModelProvider`：提供商抽象基类，由 OpenAI 兼容 LLM 提供商、
  沙箱占位（stub）提供商等具体实现继承。

设计约定（ADR-07）：路由决策不交给 LLM，由 :mod:`app.infra.model_gateway.router`
基于 model_registry/model_routes 确定性完成；本模块只定义"怎么调"与"返回什么"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models import ModelRegistry

# 网关支持的四种生成能力（model_registry.capability 中的生成类子集）
CAPABILITY_LLM = "llm"
CAPABILITY_IMAGE = "image"
CAPABILITY_VIDEO = "video"
CAPABILITY_TTS = "tts"
CAPABILITY_BGM = "bgm"


@dataclass
class GenRequest:
    """统一生成请求数据类。

    一个请求对应一次模型调用；LLM 多轮对话用 ``messages``，单轮生成用
    ``prompt``（可叠加 ``system_prompt``）。媒体尺寸/时长/音色等能力特有
    参数统一放入 ``params`` 字典，由具体提供商按需解析。

    Attributes:
        capability: 生成能力（llm/image/video/tts/bgm）。
        prompt: 用户提示词（单轮生成必填）。
        system_prompt: 系统提示词（可选，仅 LLM）。
        messages: OpenAI 风格多轮消息列表（可选，仅 LLM；提供时优先于
            prompt/system_prompt 构造消息）。
        params: 能力特有参数字典，常用键：temperature / max_tokens /
            response_json（bool，LLM JSON 模式）/ size（如 "1024x1024"）/
            width / height / duration_ms / fps / voice / asset_name。
        model_hint: 用户锁定的模型键（可选；非空时路由只允许命中该模型）。
    """

    capability: str
    prompt: str = ""
    system_prompt: str | None = None
    # 内容可为字符串（纯文本）或多模态分段列表（如 image_url），
    # 因此元素类型放宽为 Any；是否支持图片由具体提供商与模型决定。
    messages: list[dict[str, Any]] | None = None
    params: dict[str, Any] = field(default_factory=dict)
    model_hint: str | None = None


@dataclass
class GenResult:
    """统一生成结果数据类。

    成功时：LLM 调用填充 ``text``，媒体调用填充 ``object_key`` 与
    ``media_url``；失败时 ``ok=False`` 且 ``error`` 携带可读原因。
    ``meta`` 携带记账与展示所需元数据（token 数、时长、成本、媒体尺寸等），
    绝不包含任何密钥。

    Attributes:
        ok: 是否成功。
        text: 生成文本（仅 LLM 成功时）。
        object_key: 媒体对象键（仅媒体成功时，可经 /files/{object_key} 下载）。
        media_url: 媒体访问 URL（/files/{object_key}）。
        meta: 元数据字典。常用键：tokens_in / tokens_out / model_key /
            duration_ms / cost_credits / simulated（bool，占位产物）/
            width / height / placeholder（媒体占位类型）/ trace_id。
        error: 失败原因（人类可读，不含密钥）。
    """

    ok: bool
    text: str | None = None
    object_key: str | None = None
    media_url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ModelProvider:
    """模型提供商抽象基类。

    具体提供商实现 :meth:`generate` 完成真实调用（或占位生成），实现
    :meth:`supports` 声明支持的能力。提供商不负责路由与记账，只负责
    "给一个 GenRequest，返回一个 GenResult"。
    """

    async def generate(self, request: GenRequest, model_spec: ModelRegistry) -> GenResult:
        """执行一次生成调用。

        Args:
            request: 统一生成请求。
            model_spec: 路由命中的模型注册行（含 model_key/provider/unit 等）。

        Returns:
            统一生成结果；失败以 ``ok=False`` 表达而非抛异常。

        Raises:
            NotImplementedError: 子类未实现本方法。
        """
        raise NotImplementedError("提供商必须实现 generate()")

    def supports(self, capability: str) -> bool:
        """判断本提供商是否支持指定能力。

        Args:
            capability: 能力名（llm/image/video/tts/bgm）。

        Returns:
            支持返回 True，否则 False。

        Raises:
            NotImplementedError: 子类未实现本方法。
        """
        raise NotImplementedError("提供商必须实现 supports()")

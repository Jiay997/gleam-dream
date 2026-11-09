"""模型配置域请求模型。

对齐模型配置页（文本 / 图片 / 语音 / 视频四类）的新增与编辑表单：
- ``capability`` / ``model_key`` 仅新增时可填，编辑不可变更（避免破坏路由与记账）；
- ``api_key`` 编辑时"缺省=不修改、空串=清除、非空=覆盖"；
- 其余字段均为行内可配置参数。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 四类可配置能力
ConfigCapability = Literal["llm", "image", "tts", "video"]
#: 支持的调用协议（含 Agnes 图片/视频）
ConfigProtocol = Literal[
    "openai_chat",
    "dashscope_image",
    "dashscope_video",
    "dashscope_tts",
    "agnes_image",
    "agnes_video",
    "qwen_tts",
    "stub",
]
#: 计费单位（对齐 model_registry CHECK 约束）
ConfigUnit = Literal["per_image", "per_second", "per_1k_token", "per_call"]


class ModelConfigCreateRequest(BaseModel):
    """新增模型配置请求体。

    Attributes:
        capability: 模型类型（llm/image/tts/video）。
        model_key: 模型键（唯一，小写字母开头）。
        model_name: 展示名称。
        protocol: 调用协议（须与能力匹配）。
        base_url: 接口地址（TTS 填 WebSocket 地址；stub 可空）。
        api_key: 访问密钥（可空，空则回退 .env 默认）。
        api_model: 三方 API 模型名（如 qwen-image-3.0）。
        extra_config: 扩展参数（如 {"voice": "Cherry"}）。
        unit: 计费单位。
        unit_price_credits: 单价（积分）。
        quality_score: 质量分（0~10）。
        is_available: 是否启用。
        is_default: 是否设为该类型默认模型。
    """

    model_config = ConfigDict(extra="forbid")

    capability: ConfigCapability = Field(description="模型类型：llm/image/tts/video")
    model_key: str = Field(min_length=2, max_length=64, description="模型键（唯一）")
    model_name: str = Field(min_length=1, max_length=128, description="展示名称")
    protocol: ConfigProtocol = Field(description="调用协议")
    base_url: str | None = Field(default=None, max_length=512, description="接口地址")
    api_key: str | None = Field(default=None, max_length=1024, description="访问密钥")
    api_model: str | None = Field(default=None, max_length=128, description="三方 API 模型名")
    extra_config: dict[str, Any] | None = Field(default=None, description="扩展参数")
    unit: ConfigUnit = Field(default="per_call", description="计费单位")
    unit_price_credits: int = Field(default=0, ge=0, le=1_000_000, description="单价（积分）")
    quality_score: float | None = Field(default=None, ge=0, le=10, description="质量分 0~10")
    is_available: bool = Field(default=True, description="是否启用")
    is_default: bool = Field(default=False, description="是否设为默认模型")


class ModelConfigUpdateRequest(BaseModel):
    """编辑模型配置请求体（部分更新，能力与模型键不可改）。

    Attributes:
        model_name: 展示名称。
        protocol: 调用协议。
        base_url: 接口地址（空串表示清除）。
        api_key: 访问密钥（缺省不修改，空串清除）。
        api_model: 三方 API 模型名（空串表示清除）。
        extra_config: 扩展参数。
        unit: 计费单位。
        unit_price_credits: 单价（积分）。
        quality_score: 质量分（0~10）。
        is_available: 是否启用。
    """

    model_config = ConfigDict(extra="forbid")

    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    protocol: ConfigProtocol | None = None
    base_url: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=1024)
    api_model: str | None = Field(default=None, max_length=128)
    extra_config: dict[str, Any] | None = None
    unit: ConfigUnit | None = None
    unit_price_credits: int | None = Field(default=None, ge=0, le=1_000_000)
    quality_score: float | None = Field(default=None, ge=0, le=10)
    is_available: bool | None = None

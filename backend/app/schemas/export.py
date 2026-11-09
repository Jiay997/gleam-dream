"""导出分发域请求/响应模型（Task 9 · Pydantic schemas）。

定义导出与分发 API 的请求体结构。响应实际经
:func:`app.core.errors.success_envelope` 包络输出，本模块的响应模型
主要用于文档化与内部构造校验。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 支持分发的平台（对齐 platform_accounts CHECK 约束）
PlatformKey = Literal["douyin", "kuaishou", "wechat_channels", "jianying"]


class ExportCreateRequest(BaseModel):
    """创建导出任务请求体。

    Attributes:
        resolution: 分辨率（720p/1080p/4k，受套餐权益限制）。
        with_watermark: 是否带水印（默认 True；False 需 no_watermark 权益）。
        episode_no: 集号（可选；缺省导出全剧拼接）。
        preset_id: 套用的导出预设 ID（V4.0，AC-F7-01-3）。
        name_template: 命名模板（V4.0，AC-F7-02-3；变量
            ``{剧名}/{集号}/{集标题}/{日期}/{分辨率}``）。
    """

    model_config = ConfigDict(extra="forbid")

    resolution: Literal["720p", "1080p", "4k"] = Field(description="分辨率（3 值）")
    with_watermark: bool = Field(default=True, description="是否带水印")
    episode_no: int | None = Field(default=None, ge=1, description="集号（可选）")
    preset_id: int | None = Field(default=None, ge=1, description="导出预设 ID")
    name_template: str | None = Field(
        default=None, max_length=100, description="命名模板（变量见文档）"
    )


class SpecCheckRequest(BaseModel):
    """目标平台规格自检请求体（AC-F7-07-1，V4.0）。

    Attributes:
        platform: 目标平台键。
        resolution: 计划导出的分辨率（可空，默认 1080p）。
        episode_no: 目标集号（可空，校验全剧各集时长）。
    """

    model_config = ConfigDict(extra="forbid")

    platform: PlatformKey = Field(description="目标平台")
    resolution: Literal["720p", "1080p", "4k"] = Field(
        default="1080p", description="计划分辨率"
    )
    episode_no: int | None = Field(default=None, ge=1, description="目标集号")


class ExportReuseRequest(BaseModel):
    """导出参数复用请求体（AC-F7-06-2，V4.0）。

    Attributes:
        resolution: 覆写分辨率（可空，沿用历史记录）。
        name_template: 覆写命名模板（可空）。
    """

    model_config = ConfigDict(extra="forbid")

    resolution: Literal["720p", "1080p", "4k"] | None = Field(
        default=None, description="覆写分辨率"
    )
    name_template: str | None = Field(default=None, max_length=100, description="覆写命名模板")


class ExportPresetRequest(BaseModel):
    """导出预设保存请求体（AC-F7-01-3，V4.0）。

    Attributes:
        name: 预设名（≤128 字）。
        resolution: 分辨率。
        aspect: 画面比例（9:16/16:9/1:1/4:3）。
        watermark: 是否带水印。
        name_template: 命名模板（可空）。
        is_default: 是否设为默认预设。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, description="预设名")
    resolution: Literal["720p", "1080p", "4k"] = Field(default="1080p", description="分辨率")
    aspect: Literal["9:16", "16:9", "1:1", "4:3"] = Field(default="9:16", description="画面比例")
    watermark: bool = Field(default=True, description="是否带水印")
    name_template: str | None = Field(default=None, max_length=100, description="命名模板")
    is_default: bool = Field(default=False, description="是否默认预设")


class BatchExportRequest(BaseModel):
    """批量导出请求体（每集一个导出任务 + ZIP 打包）。

    Attributes:
        resolution: 分辨率（720p/1080p/4k，受套餐权益限制）。
        with_watermark: 是否带水印（默认 True）。
    """

    model_config = ConfigDict(extra="forbid")

    resolution: Literal["720p", "1080p", "4k"] = Field(description="分辨率（3 值）")
    with_watermark: bool = Field(default=True, description="是否带水印")


class MetadataGenerateRequest(BaseModel):
    """发行元数据生成请求体（预留字段位，当前为空对象）。

    Attributes:
        extra: 预留扩展字段（可选）。
    """

    model_config = ConfigDict(extra="allow")

    pass


class PlatformAccountCreateRequest(BaseModel):
    """平台授权（Stub OAuth）请求体。

    Attributes:
        platform: 平台键（douyin/kuaishou/wechat_channels/jianying）。
        mock_authorize: 是否走模拟授权（演示环境必须为 True）。
    """

    model_config = ConfigDict(extra="forbid")

    platform: PlatformKey = Field(description="平台键（4 值）")
    mock_authorize: bool = Field(default=True, description="是否走模拟授权")


class DistributeRequest(BaseModel):
    """分发请求体。

    Attributes:
        platform: 平台键。
        metadata: 发行元数据（title/description/tags，前端可编辑）。
        export_job_id: 关联导出任务 ID（可选）。
    """

    model_config = ConfigDict(extra="forbid")

    platform: PlatformKey = Field(description="平台键（4 值）")
    metadata: dict[str, Any] = Field(description="发行元数据（title/description/tags）")
    export_job_id: int | None = Field(default=None, description="关联导出任务 ID（可选）")


class DistributionMetadata(BaseModel):
    """发行元数据结构（文档化）。

    Attributes:
        title: 发行标题（≤20 字）。
        description: 平台简介（≤80 字）。
        tags: 话题标签（5 个）。
        cover_suggestions: 封面建议。
    """

    title: str
    description: str
    tags: list[str]
    cover_suggestions: list[str] = Field(default_factory=list)

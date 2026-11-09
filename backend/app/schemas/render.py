"""生成执行域请求/响应模型（Task 8 · Pydantic schemas）。

定义渲染任务 API 的请求体与响应结构描述。响应实际经
:func:`app.core.errors.success_envelope` 包络输出，本模块的响应模型
主要用于文档化与内部构造校验。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class BatchRenderRequest(BaseModel):
    """批量提交渲染请求体。

    Attributes:
        shot_ids: 镜头 ID 列表（可选；为空/缺省表示该项目全部
            status=locked 的镜头）。
        strategy_tier: 路由策略档（V4.0，AC-F5-06-2）：速度优先 / 均衡（默认）/
            质量优先；已锁模型优先级高于策略档。
        max_credits: 本次生成上限（可调，S1，AC-F5-01-2；0 表示不限）。
    """

    model_config = ConfigDict(extra="forbid")

    shot_ids: list[int] | None = Field(
        default=None, description="镜头 ID 列表；空表示全部锁定镜头", max_length=100,
    )
    strategy_tier: Literal["speed", "balanced", "quality"] = Field(
        default="balanced", description="路由策略档（3 值）"
    )
    max_credits: int = Field(default=0, ge=0, description="本次生成上限（0 不限）")


class ManualPassRequest(BaseModel):
    """人工标记合格请求体（AC-F5-09-1，V4.0）。

    Attributes:
        reason: 放行理由（≥5 字）。
        acknowledged: 是否已勾选确认声明（必须为 true）。
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=100, description="放行理由（≥5 字）")
    acknowledged: bool = Field(description="是否已勾选确认声明")


class BatchRetryRequest(BaseModel):
    """批量重试请求体（AC-F5-08-1~2，V4.0）。

    Attributes:
        task_ids: 待重试任务 ID 列表（1~100）。
        delay_minutes: 延迟重试分钟数（0 立即；>0 定时批量重试）。
    """

    model_config = ConfigDict(extra="forbid")

    task_ids: list[int] = Field(min_length=1, max_length=100, description="任务 ID 列表")
    delay_minutes: int = Field(default=0, ge=0, le=1440, description="延迟重试分钟数")


class EpisodeLockRequest(BaseModel):
    """按集锁定镜头版本请求体（AC-F5-10-1~2，V4.0）。

    Attributes:
        selections: 逐镜头指定版本号（缺省时自动取质检得分最高版本）。
    """

    model_config = ConfigDict(extra="forbid")

    selections: dict[str, int] = Field(
        default_factory=dict, description="镜头 ID → 版本号（缺省自动选最优版）"
    )


class QueueJumpRequest(BaseModel):
    """单镜头插队请求体（AC-F5-01-3，V4.0）。

    Attributes:
        task_id: 需要插队的渲染任务 ID。
    """

    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(ge=1, description="渲染任务 ID")


class ShotVersionLabelRequest(BaseModel):
    """镜头版本标签/备注请求体（AC-F5-03-3，V4.0）。

    Attributes:
        label: 预设标签枚举（可空）。
        note: 备注（≤100 字，可空）。
    """

    model_config = ConfigDict(extra="forbid")

    label: Literal["color_good", "action_right", "expression_natural", "other"] | None = Field(
        default=None, description="预设标签"
    )
    note: str | None = Field(default=None, max_length=100, description="备注（≤100 字）")


class RenderDashboardResponse(BaseModel):
    """生成仪表盘响应（AC-F5-07-1，文档化结构）。

    Attributes:
        counts: 六态计数。
        total: 任务总数。
        progress_percent: 总进度百分比。
        eta_seconds: 预计剩余秒数（None 表示不可估算）。
        eta_volatile: 速率波动是否 > 50%（波动大时不给精确承诺）。
        consumed_credits: 实时消耗积分。
        available_credits: 当前可用积分。
    """

    counts: dict[str, int]
    total: int
    progress_percent: int
    eta_seconds: int | None
    eta_volatile: bool
    consumed_credits: int
    available_credits: int
    extra: dict[str, Any] | None = None


class RenderEstimateItem(BaseModel):
    """单镜头成本估算项。

    Attributes:
        shot_id: 镜头 ID。
        estimated_credits: 预估成本（积分）。
    """

    shot_id: int
    estimated_credits: int


class RenderEstimateResponse(BaseModel):
    """渲染成本估算响应。

    Attributes:
        per_shot: 逐镜头估算列表。
        total_credits: 合计预估成本（积分）。
        shot_count: 参与估算的镜头数。
        available_credits: 当前用户可用积分（balance - frozen）。
    """

    per_shot: list[RenderEstimateItem]
    total_credits: int
    shot_count: int
    available_credits: int


class BatchRenderTaskItem(BaseModel):
    """批量提交结果中的单任务摘要。

    Attributes:
        id: 渲染任务 ID。
        shot_id: 镜头 ID。
        status: 任务状态（创建后恒为 queued）。
    """

    id: int
    shot_id: int | None
    status: str


class BatchRenderResponse(BaseModel):
    """批量提交渲染响应。

    Attributes:
        tasks: 任务摘要列表。
        frozen_credits: 本次冻结积分总额（全部镜头冻结之和）。
        estimated_credits: 预估积分总额。
        warnings: 跳过镜头的提示信息（镜头未锁定/不存在等）。
    """

    tasks: list[BatchRenderTaskItem]
    frozen_credits: int
    estimated_credits: int
    warnings: list[str] = Field(default_factory=list)


class RenderTaskListResponse(BaseModel):
    """渲染任务分页列表响应（文档化结构，实际由服务层 dict 输出）。

    Attributes:
        items: 任务条目列表。
        page: 页码。
        page_size: 每页条数。
        total: 总数。
    """

    items: list[dict]
    page: int
    page_size: int
    total: int

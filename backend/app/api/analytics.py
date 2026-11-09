"""埋点路由模块（验收报告 7.1 P0 · 埋点最小集）。

端点：
- ``POST /analytics/events``  客户端行为事件上报（登录用户；事件名
  snake_case 3~64 字符，属蛇形命名白名单校验，属性上限 2KB）；
- ``GET  /analytics/funnel``  平台漏斗摘要（管理端：``X-Admin-Token``
  匹配 ``settings.ADMIN_TOKEN``；未配置口令时端点禁用 20003）。

服务端自动埋点（注册/激活/四关口/导出漏斗）在服务层挂钩，不经过本路由。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import BizError, ErrorCode, success_envelope
from app.services import analytics_service

logger = logging.getLogger("gleamdream.api.analytics")

#: 埋点子路由（主控聚合：include_router(analytics.router)）
router = APIRouter(prefix="/analytics", tags=["analytics"])

# 客户端事件名命名约束（snake_case，3~64 字符）
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
# 单条事件属性序列化上限（JSONB 体积保护）
_PROPS_MAX_JSON_BYTES = 2048


class AnalyticsEventIn(BaseModel):
    """客户端埋点上报请求体。

    Attributes:
        event_name: 事件名（snake_case 3~64 字符）。
        project_id: 关联项目 ID（可空）。
        session_id: 客户端漏斗会话 ID（可空）。
        props: 事件属性（可空，扁平字典）。
        occurred_at: 事件发生时间（可空，缺省服务端接收时刻；容忍客户端
            时钟偏差，仅接受合法 ISO 时间）。
    """

    model_config = ConfigDict(extra="forbid")

    event_name: str = Field(description="事件名（snake_case 3~64 字符）")
    project_id: int | None = Field(default=None, ge=1, description="关联项目 ID")
    session_id: str | None = Field(default=None, max_length=64, description="漏斗会话 ID")
    props: dict[str, Any] | None = Field(default=None, description="事件属性（扁平字典）")
    occurred_at: datetime | None = Field(default=None, description="事件发生时间（ISO 8601）")

    @field_validator("event_name")
    @classmethod
    def _validate_event_name(cls, value: str) -> str:
        """校验事件名命名格式。

        Args:
            value: 原始事件名。

        Returns:
            校验通过的事件名。

        Raises:
            ValueError: 不符合 snake_case 3~64 字符约束。
        """
        if not _EVENT_NAME_RE.match(value):
            raise ValueError("event_name 须为 snake_case（字母开头，3~64 位小写字母/数字/下划线）")
        return value

    @field_validator("props")
    @classmethod
    def _validate_props(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """校验事件属性体积上限（防大 payload 写入）。

        Args:
            value: 原始属性字典。

        Returns:
            校验通过的属性字典。

        Raises:
            ValueError: 序列化后超过 2KB。
        """
        if value is None:
            return None
        if len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")) > _PROPS_MAX_JSON_BYTES:
            raise ValueError("props 序列化后不得超过 2KB")
        return value


@router.post("/events")
async def post_event(
    body: AnalyticsEventIn,
    user_id: Annotated[str, Depends(get_current_user)],
) -> dict[str, Any]:
    """客户端行为事件上报（服务端自动埋点的补充通道）。

    Args:
        body: 事件请求体。
        user_id: 当前登录用户 ID（归属事件归属人，客户端不可伪造他人）。

    Returns:
        202 成功包络（受理即返回，落库异步容错）。
    """
    await analytics_service.track(
        body.event_name,
        user_id=int(user_id),
        project_id=body.project_id,
        session_id=body.session_id,
        props=body.props,
        occurred_at=body.occurred_at,
    )
    return JSONResponse(status_code=202, content=success_envelope({"accepted": True}))


@router.get("/funnel")
async def get_funnel(
    session: Annotated[AsyncSession, Depends(get_db)],
    x_admin_token: Annotated[str | None, Header(alias="X-Admin-Token")] = None,
    days: Annotated[int, Query(ge=1, le=365, description="统计窗口天数")] = 30,
) -> dict[str, Any]:
    """平台漏斗摘要（管理端）。

    鉴权：``X-Admin-Token`` 请求头须等于 ``settings.ADMIN_TOKEN``；
    服务端未配置口令（空串）时端点恒禁用（20003），不提供匿名聚合读。

    Args:
        session: 请求级数据库会话。
        x_admin_token: 管理端口令请求头。
        days: 统计窗口天数（1~365，默认 30）。

    Returns:
        成功包络，data 为漏斗摘要（注册/激活/四关口/导出漏斗）。

    Raises:
        BizError: 20003 口令缺失/不匹配或管理端未启用。
    """
    expected = settings.ADMIN_TOKEN.strip()
    if not expected or not x_admin_token or x_admin_token.strip() != expected:
        raise BizError(ErrorCode.FORBIDDEN, "管理端口令缺失或不匹配（X-Admin-Token）")
    data = await analytics_service.funnel_summary(session, days=days)
    return success_envelope(data)

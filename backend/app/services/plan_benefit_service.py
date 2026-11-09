"""套餐权益判定服务模块（Task 9 · 导出分发域）。

职责：
- :func:`resolve_benefits`：解析用户当前生效套餐的权益位
  （subscriptions(active 最新) → plans.benefits JSONB），无订阅按 free 兜底；
- :func:`assert_export_allowed` / :func:`assert_resolution_allowed` /
  :func:`assert_watermark_allowed`：导出参数与权益的匹配校验，
  超权益抛 70003（data 携带 ``current_plan`` / ``required_benefit``）。

权益键名对齐种子数据（scripts/seed.py ``_plan_rows``）：
- ``max_resolution``：可导出的最高分辨率（720p/1080p/4k）；
- ``no_watermark``：是否允许去除水印（free/pro 为 False，studio 为 True）。

套餐档位约定（契约）：
- free  → 仅 720p + 强制水印；
- pro   → 最高 1080p + 强制水印；
- studio → 4k + 可去水印。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.models import Plan, Subscription

# 分辨率等级（数值越大权益越高）
_RESOLUTION_RANK: dict[str, int] = {"720p": 1, "1080p": 2, "4k": 3}

# 无任何有效订阅时的 free 兜底权益
_FREE_DEFAULT_BENEFITS: dict[str, Any] = {
    "plan_key": "free",
    "max_resolution": "720p",
    "allow_no_watermark": False,
    "concurrency": 2,
}


def resolution_rank(resolution: str) -> int:
    """查询分辨率的权益等级。

    Args:
        resolution: 分辨率字符串（720p/1080p/4k）。

    Returns:
        等级整数（未知取值返回 0，必然低于任何合法权益）。
    """
    return _RESOLUTION_RANK.get(resolution, 0)


async def resolve_benefits(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """解析用户当前生效套餐的权益位。

    查询顺序：用户名下 status=active 且未过期的最新订阅 → 关联套餐的
    ``benefits`` JSONB 与并发上限；无命中按 free 兜底。

    Args:
        session: 异步数据库会话（只读）。
        user_id: 用户 ID。

    Returns:
        权益字典 ``{plan_key, max_resolution, allow_no_watermark,
        concurrency}``。

    Raises:
        无（查询失败按 free 兜底语义返回，不抛业务异常）。
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(Plan)
        .join(Subscription, Subscription.plan_id == Plan.id)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
            or_(Subscription.expires_at.is_(None), Subscription.expires_at > now),
        )
        .order_by(Subscription.started_at.desc(), Subscription.id.desc())
        .limit(1)
    )
    plan = (await session.execute(stmt)).scalar_one_or_none()
    if plan is None:
        return dict(_FREE_DEFAULT_BENEFITS)

    benefits = plan.benefits if isinstance(plan.benefits, dict) else {}
    return {
        "plan_key": plan.key,
        "max_resolution": str(benefits.get("max_resolution") or "720p"),
        "allow_no_watermark": bool(benefits.get("no_watermark", False)),
        "concurrency": max(1, int(plan.concurrency_limit or 2)),
    }


def assert_resolution_allowed(benefits: dict[str, Any], resolution: str) -> None:
    """校验请求分辨率是否在套餐权益范围内。

    Args:
        benefits: :func:`resolve_benefits` 返回的权益字典。
        resolution: 请求分辨率（720p/1080p/4k）。

    Raises:
        BizError: 70003 权益不足，data 携带 current_plan 与 required_benefit。
    """
    if resolution_rank(resolution) > resolution_rank(str(benefits.get("max_resolution") or "720p")):
        raise BizError(
            ErrorCode.ENTITLEMENT_INSUFFICIENT,
            f"当前套餐「{benefits.get('plan_key')}」不支持 {resolution} 导出，请升级套餐",
            data={
                "current_plan": benefits.get("plan_key", "free"),
                "required_benefit": f"max_resolution:{resolution}",
            },
        )


def assert_watermark_allowed(benefits: dict[str, Any], with_watermark: bool) -> None:
    """校验去水印请求是否在套餐权益范围内。

    Args:
        benefits: :func:`resolve_benefits` 返回的权益字典。
        with_watermark: 是否带水印导出（False 表示请求去水印）。

    Raises:
        BizError: 70003 权益不足，data 携带 current_plan 与 required_benefit。
    """
    if not with_watermark and not bool(benefits.get("allow_no_watermark")):
        raise BizError(
            ErrorCode.ENTITLEMENT_INSUFFICIENT,
            f"当前套餐「{benefits.get('plan_key')}」不支持去除水印，请升级套餐",
            data={
                "current_plan": benefits.get("plan_key", "free"),
                "required_benefit": "no_watermark",
            },
        )


def assert_export_allowed(
    benefits: dict[str, Any], resolution: str, with_watermark: bool
) -> None:
    """导出参数与权益的完整校验（分辨率 + 水印）。

    Args:
        benefits: :func:`resolve_benefits` 返回的权益字典。
        resolution: 请求分辨率（720p/1080p/4k）。
        with_watermark: 是否带水印导出。

    Raises:
        BizError: 70003 权益不足（data 携带 current_plan/required_benefit）。
    """
    assert_resolution_allowed(benefits, resolution)
    assert_watermark_allowed(benefits, with_watermark)

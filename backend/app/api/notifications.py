"""通知域 API 路由。

提供站内通知的查询与已读管理能力（P1 通知中心 + P22 页面数据源）：
- ``GET  /notifications``          分页列表（type 筛选 / unread 过滤）；
- ``GET  /notifications/unread-count`` 未读计数（供顶栏徽标轮询）；
- ``POST /notifications/{id}/read``   单条已读；
- ``POST /notifications/read-all``    全部已读。

数据来源：``event_bus`` 发布 ``notification.*`` 前缀事件时自动落库的
``notifications`` 表（Task 4 事件总线约定）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_request_id
from app.core.errors import BizError, ErrorCode, success_envelope
from app.models.collab import Notification

#: 通知域路由（路径自带 /notifications 前缀）
router = APIRouter(prefix="/notifications", tags=["notifications"])


def _notification_out(n: Notification) -> dict:
    """将通知 ORM 实例序列化为响应字典。

    Args:
        n: 通知 ORM 实例。

    Returns:
        snake_case 字段字典（与前端 NotificationItem 契约一致）。
    """
    return {
        "id": n.id,
        "user_id": n.user_id,
        "project_id": n.project_id,
        "type": n.type,
        "title": n.title,
        "content": n.content,
        "payload": n.payload or {},
        "is_read": n.is_read,
        "read_at": n.read_at.isoformat() if n.read_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


@router.get("")
async def list_notifications(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    type: Annotated[str | None, Query(description="通知类型筛选")] = None,
    unread: Annotated[bool | None, Query(description="仅看未读")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """分页查询当前用户的通知列表。

    Args:
        user_id: 当前登录用户 ID。
        session: 数据库会话。
        request_id: 请求 ID。
        type: 通知类型筛选（render_done/render_failed/approval_pending/credit_low/system）。
        unread: 为 true 时仅返回未读。
        page: 页码（从 1 起）。
        page_size: 每页条数（1-100）。

    Returns:
        统一包络，data 为 ``{"items": [...], "page", "page_size", "total"}``。

    Raises:
        BizError: 参数校验失败时由框架统一抛出 10001。
    """
    uid = int(user_id)
    conditions = [Notification.user_id == uid]
    if type:
        conditions.append(Notification.type == type)
    if unread is True:
        conditions.append(Notification.is_read.is_(False))

    where = *conditions,
    total = (
        await session.execute(select(func.count()).select_from(Notification).where(*where))
    ).scalar_one()
    stmt = (
        select(Notification)
        .where(*where)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return success_envelope(
        {"items": [_notification_out(n) for n in rows], "page": page, "page_size": page_size, "total": total},
        request_id,
    )


@router.get("/unread-count")
async def unread_count(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict:
    """查询当前用户未读通知数量。

    Args:
        user_id: 当前登录用户 ID。
        session: 数据库会话。
        request_id: 请求 ID。

    Returns:
        统一包络，data 为 ``{"count": int}``。

    Raises:
        BizError: 查询异常时按统一异常处理。
    """
    uid = int(user_id)
    stmt = select(func.count()).select_from(Notification).where(
        Notification.user_id == uid, Notification.is_read.is_(False)
    )
    count = (await session.execute(stmt)).scalar_one()
    return success_envelope({"count": count}, request_id)


@router.post("/{notification_id}/read")
async def mark_read(
    notification_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict:
    """将单条通知标记为已读（幂等：重复标记返回成功）。

    Args:
        notification_id: 通知 ID（路径参数）。
        user_id: 当前登录用户 ID（只能操作自己的通知）。
        session: 数据库会话。
        request_id: 请求 ID。

    Returns:
        统一包络，data 为 ``{"ok": True}``。

    Raises:
        BizError: 10002 通知不存在或不属于当前用户。
    """
    uid = int(user_id)
    stmt = select(Notification).where(Notification.id == notification_id, Notification.user_id == uid)
    n = (await session.execute(stmt)).scalar_one_or_none()
    if n is None:
        raise BizError(ErrorCode.NOT_FOUND, "通知不存在")
    if not n.is_read:
        n.is_read = True
        n.read_at = datetime.now(UTC)
        await session.commit()
    return success_envelope({"ok": True}, request_id)


@router.post("/read-all")
async def mark_all_read(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict:
    """将当前用户全部未读通知标记为已读。

    Args:
        user_id: 当前登录用户 ID。
        session: 数据库会话。
        request_id: 请求 ID。

    Returns:
        统一包络，data 为 ``{"updated": int}``（本次更新的条数）。

    Raises:
        BizError: 更新异常时按统一异常处理。
    """
    uid = int(user_id)
    result = await session.execute(
        update(Notification)
        .where(Notification.user_id == uid, Notification.is_read.is_(False))
        .values(is_read=True, read_at=datetime.now(UTC))
    )
    await session.commit()
    return success_envelope({"updated": result.rowcount or 0}, request_id)

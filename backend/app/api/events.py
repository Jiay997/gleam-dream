"""全局 SSE 事件流 API 模块。

提供登录用户维度的全局事件流端点，聚合推送该用户所有可见项目
（Owner 或项目成员）的实时事件，供 AppShell 全局订阅使用：

- ``GET /events/stream``：多项目聚合 SSE 流（EventSource 兼容，
  token 经 ``?access_token=`` 查询参数传递，断线补偿经
  ``Last-Event-ID`` 请求头）。

路由变量名：``router = APIRouter(tags=["events"])``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.infra.event_bus import event_bus
from app.models.iam import ProjectMember
from app.models.project import Project

logger = logging.getLogger("gleamdream.events")

router = APIRouter(tags=["events"])


async def _visible_project_ids(session: AsyncSession, user_id: int) -> list[int]:
    """查询用户可见（Owner 或成员）且未软删的项目 ID 集合。

    Args:
        session: 异步数据库会话。
        user_id: 当前用户 ID。

    Returns:
        项目 ID 列表（升序）。
    """
    owned = (
        await session.execute(
            select(Project.id).where(
                Project.owner_user_id == user_id, Project.deleted_at.is_(None)
            )
        )
    ).scalars().all()
    joined = (
        await session.execute(
            select(ProjectMember.project_id).where(ProjectMember.user_id == user_id)
        )
    ).scalars().all()
    return sorted({int(pid) for pid in (*owned, *joined)})


async def _multi_project_stream(
    project_ids: list[int], last_id: int | None
) -> AsyncIterator[dict]:
    """聚合订阅多个项目的事件流（按事件序号全局排序去重）。

    为每个项目启动一个泵任务把 ``event_bus.subscribe`` 的记录转发到
    合并队列，主生成器按 ``id`` 去重后产出；客户端断连时取消全部泵。

    Args:
        project_ids: 要订阅的项目 ID 列表。
        last_id: 客户端最后收到的事件序号（None 表示全新连接）。

    Yields:
        事件记录字典；心跳时为 ``{"heartbeat": True}``。
    """
    merged: asyncio.Queue[dict] = asyncio.Queue()
    last_yielded = last_id or 0

    async def _pump(pid: int) -> None:
        """单项目订阅泵：把该项目的记录转发进合并队列。

        Args:
            pid: 项目 ID。
        """
        try:
            # 断线补偿在项目内回放；跨项目重复由主循环按 id 去重兜底
            async for record in event_bus.subscribe(pid, last_id):
                await merged.put(record)
        except asyncio.CancelledError:
            raise

    tasks = [asyncio.create_task(_pump(pid)) for pid in project_ids]
    try:
        if not project_ids:
            # 无可见项目：仅心跳保活，避免客户端频繁重连
            while True:
                await asyncio.sleep(15)
                yield {"heartbeat": True}
            return
        while True:
            try:
                record = await asyncio.wait_for(merged.get(), timeout=15.0)
            except asyncio.TimeoutError:
                yield {"heartbeat": True}
                continue
            if record.get("heartbeat") or record["id"] > last_yielded:
                last_yielded = record.get("id", last_yielded)
                yield record
    finally:
        for task in tasks:
            task.cancel()


@router.get("/events/stream")
async def global_event_stream(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    access_token: Annotated[str | None, Query(description="EventSource 兼容鉴权")] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """全局 SSE 事件流：聚合推送当前用户全部可见项目的实时事件。

    认证兼容浏览器 EventSource（无法自定义请求头）：token 可经
    ``?access_token=`` 查询参数传递（get_current_user 内建支持）。

    Args:
        user_id: 当前登录用户 ID。
        session: 请求级数据库会话（用于解析可见项目集合）。
        access_token: 查询参数令牌（get_current_user 已消费，此处仅声明文档）。
        last_event_id: 客户端最后收到的事件序号（断线补偿）。

    Returns:
        text/event-stream 的 StreamingResponse。
    """
    _ = access_token  # 鉴权由 get_current_user 完成（header 或 query）
    project_ids = await _visible_project_ids(session, int(user_id))
    # Q-006：SSE 生命周期远长于一次请求，不能持续占用请求级 DB 连接；
    # 可见项目集合解析完成后立即释放连接，避免 idle in transaction 堆积。
    await session.close()
    last_id: int | None = None
    if last_event_id and last_event_id.strip().isdigit():
        last_id = int(last_event_id.strip())

    async def event_stream() -> AsyncIterator[str]:
        """SSE 帧渲染生成器。

        Yields:
            SSE 事件帧或心跳注释行。
        """
        try:
            async for record in _multi_project_stream(project_ids, last_id):
                if record.get("heartbeat"):
                    yield ": ping\n\n"
                    continue
                data = json.dumps(record["data"], ensure_ascii=False, default=str)
                yield f"id: {record['id']}\nevent: {record['event']}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

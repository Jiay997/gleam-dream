"""进程内 SSE 事件总线模块。

提供项目维度的发布/订阅能力，供状态机、Agent 编排等任务向客户端推送
实时事件（SSE）。特性：

- 每个项目保留最近 :data:`HISTORY_MAX`（500）条事件历史（deque），
  事件带全局单调递增整数序号（id），字典序即时间序可安全比较；
- 新订阅者携带 Last-Event-ID 时先回放历史中之后的事件（断线补偿），
  再进入实时推送；回放与实时之间通过序号去重，保证不丢不重；
- 空闲超过 :data:`HEARTBEAT_INTERVAL`（15s）时向订阅者注入心跳记录，
  由 SSE 路由层渲染为 ``: ping`` 注释行；
- ``notification.*`` 前缀事件发布时同步落 :class:`~app.models.collab.Notification`
  表（独立短会话，失败仅记日志不阻断事件流）。

使用方式（后续任务发事件）::

    from app.infra.event_bus import event_bus
    await event_bus.publish(project_id, "status_changed", {"from": ..., "to": ...})
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("gleamdream.event_bus")

#: 每个项目保留的历史事件条数上限
HISTORY_MAX = 500
#: 单订阅者队列上限（慢客户端/断线时丢弃最旧事件，防止内存无界增长）
SUBSCRIBER_QUEUE_MAX = 1000
#: 心跳间隔（秒）：订阅队列空闲超过该时长时注入一次心跳
HEARTBEAT_INTERVAL = 15.0

#: Notification.type 允许的枚举值（与 collab.Notification CHECK 约束一致）
_NOTIFICATION_TYPES = frozenset({"render_done", "render_failed", "approval_pending", "credit_low", "system"})


class EventBus:
    """进程内项目维度事件总线（发布/订阅 + 历史回放 + 心跳）。

    Attributes:
        _seq: 全局单调递增事件序号计数器（publish 时在锁内自增）。
        _seq_lock: 保护序号分配/历史追加/订阅投递原子性的异步锁。
        _history: project_id -> 事件历史 deque（每项目上限 HISTORY_MAX）。
        _subscribers: project_id -> 订阅队列集合（每个 SSE 连接一个队列）。
        _history_max: 历史条数上限（可注入覆盖，便于测试）。
        _heartbeat_interval: 心跳间隔秒数（可注入覆盖，便于测试）。
    """

    def __init__(self, history_max: int = HISTORY_MAX, heartbeat_interval: float = HEARTBEAT_INTERVAL) -> None:
        """构造事件总线。

        Args:
            history_max: 每项目历史事件保留条数上限。
            heartbeat_interval: 心跳注入间隔（秒）。
        """
        self._history_max = history_max
        self._heartbeat_interval = heartbeat_interval
        self._seq = 0
        self._seq_lock = asyncio.Lock()
        self._history: dict[int, deque[dict[str, Any]]] = {}
        self._subscribers: dict[int, set[asyncio.Queue[dict[str, Any]]]] = {}

    async def publish(self, project_id: int, event_type: str, data: dict[str, Any] | None = None) -> int:
        """向指定项目发布一条事件（写历史 + 投递全部订阅者）。

        ``notification.*`` 前缀的事件会额外尝试落 Notification 表
        （data 中需含 ``user_id`` 字段标识收件人）。

        Args:
            project_id: 事件所属项目 ID。
            event_type: 事件类型（如 ``status_changed`` / ``notification.render_done``）。
            data: 事件负载（JSON 可序列化字典）。

        Returns:
            本条事件的全局单调递增序号（即 SSE 的 id 字段）。
        """
        async with self._seq_lock:
            self._seq += 1
            record: dict[str, Any] = {
                "id": self._seq,
                "event": event_type,
                "data": data if data is not None else {},
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            self._history.setdefault(project_id, deque(maxlen=self._history_max)).append(record)
            for queue in list(self._subscribers.get(project_id, ())):
                if queue.full():
                    # 慢消费者：丢弃最旧事件，保证事件总线内存有界
                    try:
                        queue.get_nowait()
                        queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                queue.put_nowait(record)
        if event_type.startswith("notification."):
            await self._persist_notification(project_id, event_type, record["data"])
        return record["id"]

    async def subscribe(
        self,
        project_id: int,
        last_event_id: int | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """订阅指定项目的事件流。

        生成器逐条产出事件记录（``{id, event, data, ts}``）；空闲超过心跳
        间隔时产出 ``{"heartbeat": True}`` 哨兵记录，由调用方渲染为 SSE
        注释行。携带 ``last_event_id`` 时先回放历史中序号更大的事件（补偿）。
        生成器被关闭（客户端断连）时自动清理订阅队列。

        Args:
            project_id: 要订阅的项目 ID。
            last_event_id: 客户端最后收到的事件序号；None 表示全新连接
                （不回放历史，仅接收订阅之后的实时事件）。

        Yields:
            事件记录字典；心跳时为 ``{"heartbeat": True}``。
        """
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subscribers.setdefault(project_id, set()).add(queue)
        last_yielded = last_event_id or 0
        try:
            # 1) 断线补偿：先回放历史中位于 Last-Event-ID 之后的事件
            if last_event_id is not None:
                for record in list(self._history.get(project_id, ())):
                    if record["id"] > last_yielded:
                        last_yielded = record["id"]
                        yield record
            # 2) 实时循环：等待队列，空闲则注入心跳；按序号去重防回放/实时重叠
            while True:
                try:
                    record = await asyncio.wait_for(queue.get(), timeout=self._heartbeat_interval)
                except asyncio.TimeoutError:
                    yield {"heartbeat": True}
                    continue
                if record["id"] <= last_yielded:
                    continue
                last_yielded = record["id"]
                yield record
        finally:
            group = self._subscribers.get(project_id)
            if group is not None:
                group.discard(queue)
                if not group:
                    self._subscribers.pop(project_id, None)

    async def _persist_notification(self, project_id: int, event_type: str, data: dict[str, Any]) -> None:
        """将 notification.* 事件落站内通知表（尽力而为，失败仅记日志）。

        Args:
            project_id: 关联项目 ID。
            event_type: 完整事件类型（如 notification.render_done）。
            data: 事件负载（需含 user_id，可选 title/content/payload）。
        """
        user_id = data.get("user_id")
        if user_id is None:
            logger.debug("notification 事件缺少 user_id，跳过落库 event=%s", event_type)
            return
        try:
            # 局部导入规避循环依赖（db/models 不反向依赖 infra）
            from app.core.db import async_session_factory
            from app.models.collab import Notification

            ntype = event_type.removeprefix("notification.")
            if ntype not in _NOTIFICATION_TYPES:
                ntype = "system"
            payload_in = data.get("payload")
            async with async_session_factory() as session:
                session.add(
                    Notification(
                        user_id=int(user_id),
                        project_id=project_id,
                        type=ntype,
                        title=str(data.get("title") or event_type)[:255],
                        content=data.get("content"),
                        payload={"event": event_type, **(payload_in if isinstance(payload_in, dict) else {})},
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 通知落库失败不能阻断事件流
            logger.exception("站内通知落库失败 project_id=%s event=%s", project_id, event_type)


#: 全局事件总线单例（后续任务统一从此导入发事件）
event_bus = EventBus()

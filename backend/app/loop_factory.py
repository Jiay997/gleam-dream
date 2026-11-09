"""Windows 事件循环工厂模块（uvicorn 自定义 loop 支持）。

背景：uvicorn 0.36+ 的 ``asyncio_loop_factory`` 在 Windows 平台无条件
返回 ``asyncio.ProactorEventLoop``（官方为支持 subprocess），**完全无视
``asyncio.set_event_loop_policy``**——导致 psycopg3 异步连接池
（LangGraph checkpointer 依赖）无法建连，编排管线整体卡死。

本模块提供一个自定义 loop 工厂，供 uvicorn 的 ``--loop`` /
``uvicorn.run(loop=...)`` 以导入字符串方式使用：

    python -m uvicorn app.main:app --port 8100 \\
        --loop app.loop_factory:selector_loop_factory

或程序化::

    uvicorn.run("app.main:app", loop="app.loop_factory:selector_loop_factory")

契约说明：当 ``--loop`` 为导入字符串（不在内置 LOOP_FACTORIES 表）时，
uvicorn 直接把该函数作为 ``asyncio.Runner(loop_factory=...)`` 的工厂
**无参调用**，返回值即为事件循环实例——因此本工厂不接收 use_subprocess
参数，直接返回 ``asyncio.SelectorEventLoop()``。

SelectorEventLoop 兼容 asyncpg / psycopg3 / httpx；仅不支持 Windows 平台
的 subprocess（本服务不使用）。
"""

from __future__ import annotations

import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """无参 loop 工厂：生成一个 SelectorEventLoop 实例。

    Args:
        无（由 asyncio.Runner 无参调用）。

    Returns:
        新建的 asyncio.SelectorEventLoop 事件循环实例。
    """
    return asyncio.SelectorEventLoop()

"""后端服务启动入口（Windows 事件循环策略修复版）。

问题：uvicorn 0.36+ 在 Windows 上硬编码使用 ``ProactorEventLoop``
（``uvicorn.loops.asyncio.asyncio_loop_factory`` 直接返回，为支持
subprocess），**完全无视 ``asyncio.set_event_loop_policy``**——
main.py / checkpointer.py 中的策略设置因此无效，psycopg3 异步连接池
（LangGraph checkpointer 依赖）在 ProactorEventLoop 上无法建连，
编排管线整体卡死。

本入口通过 uvicorn 官方扩展点（自定义 loop 工厂导入字符串）强制
使用 SelectorEventLoop，行为与 ``python -m uvicorn app.main:app`` 等价：

    cd backend
    .venv\\Scripts\\python run_server.py              # 默认 127.0.0.1:8100
    .venv\\Scripts\\python run_server.py --port 8100  # 指定端口

等价命令行（不经过本脚本）::

    .venv\\Scripts\\python -m uvicorn app.main:app --port 8100 ^
        --loop app.loop_factory:selector_loop_factory
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    """解析命令行参数并启动 uvicorn 服务（SelectorEventLoop）。

    Args:
        无（读命令行 --host/--port/--reload）。

    Raises:
        SystemExit: argparse 参数错误时退出码 2。
    """
    parser = argparse.ArgumentParser(description="GleamDream Studio 后端服务启动入口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8100, help="监听端口（默认 8100）")
    parser.add_argument("--reload", action="store_true", help="开发热重载")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # 关键：覆盖 uvicorn Windows 默认的 ProactorEventLoop（见模块 docstring）
        loop="app.loop_factory:selector_loop_factory",
    )


if __name__ == "__main__":
    main()

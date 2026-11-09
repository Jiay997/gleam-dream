"""LangGraph Postgres 检查点装配模块。

基于 ``langgraph-checkpoint-postgres``（3.1.x，psycopg3）提供进程级
:class:`AsyncPostgresSaver` 单例：

- :func:`get_checkpointer`：惰性初始化并返回单例（首次调用自动建连接池
  并执行 ``setup()`` 幂等建 3 张框架表 checkpoints/checkpoint_blobs/
  checkpoint_writes）；
- :func:`close_checkpointer`：进程退出时释放连接池；
- :func:`delete_project_checkpoints`：清理指定项目（thread）的全部
  checkpoint 数据（测试/级联删除用）。

连接约定：项目主库为 asyncpg（``postgresql+asyncpg://``），psycopg3
不识别该驱动段，:func:`_psycopg_conninfo` 剥离驱动后缀得到
``postgresql://`` 同步格式连接串（psycopg3 内部自行切换异步实现）。

thread_id 约定：``str(project_id)``——一个项目一条主线程，HITL
interrupt 状态持久化于 checkpoint 表，服务重启后可从审批单恢复
（E2E-01 断点续跑）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

logger = logging.getLogger("gleamdream.graphs.checkpointer")

# Windows 兼容：psycopg3 异步模式不支持默认的 ProactorEventLoop，
# 须在本模块加载时（任何事件循环创建之前）切换为 SelectorEventLoop。
# asyncpg 同样兼容 SelectorEventLoop，主库连接不受影响。
if sys.platform == "win32" and isinstance(
    asyncio.get_event_loop_policy(), asyncio.WindowsProactorEventLoopPolicy
):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 进程级单例（连接池 + saver）
_pool: AsyncConnectionPool | None = None
_saver: AsyncPostgresSaver | None = None
_init_lock_ready: bool = False
# 惰性初始化并发保护（避免多协程同时建池）
_init_lock = asyncio.Lock()


def _psycopg_conninfo(database_url: str) -> str:
    """把 SQLAlchemy 连接串转换为 psycopg3 兼容连接串。

    Args:
        database_url: 原始连接串（可能含 ``+asyncpg`` 驱动段）。

    Returns:
        ``postgresql://`` 格式的连接串。
    """
    return database_url.replace("+asyncpg", "")


async def get_checkpointer() -> AsyncPostgresSaver:
    """获取进程级 AsyncPostgresSaver 单例（惰性初始化，幂等）。

    首次调用时创建 AsyncConnectionPool（autocommit，psycopg3 要求）
    并执行 ``await saver.setup()`` 建框架表（setup 内部按
    checkpoint_migrations 版本号幂等迁移）。

    事件循环校验：AsyncPostgresSaver/psycopg 连接池均绑定创建时的
    事件循环（saver.__init__ 记录 running loop）。检测到当前运行
    循环与创建循环不一致时（测试每用例新建循环的典型场景）自动
    关闭旧池并重建，规避跨循环使用导致的挂起/失败。

    Returns:
        已完成 setup、绑定当前事件循环的 AsyncPostgresSaver 实例。

    Raises:
        Exception: 数据库不可达或建表失败时向上传播（由调用方决定
            转换为 BizError/500）。
    """
    global _pool, _saver, _init_lock_ready
    async with _init_lock:
        current_loop = asyncio.get_running_loop()
        if _saver is not None:
            if getattr(_saver, "loop", None) is current_loop:
                return _saver
            # 循环已更换（典型：测试每用例新建循环）——关闭旧池重建
            logger.warning("检测到 checkpointer 绑定旧事件循环，重建连接池")
            try:
                if _pool is not None:
                    await _pool.close()
            except Exception:  # noqa: BLE001
                logger.warning("关闭旧 checkpointer 连接池失败", exc_info=True)
            _pool = None
            _saver = None

        conninfo = _psycopg_conninfo(settings.DATABASE_URL)
        pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=6,
            open=False,
            # autocommit 必须（saver 期望自动提交）；prepare_threshold=0 走服务端预编译；
            # dict_row 行工厂由 saver 在每个 cursor 层显式指定，池连接无需额外配置
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.open()
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        _pool = pool
        _saver = saver
        _init_lock_ready = True
        logger.info("LangGraph checkpointer 初始化完成（thread_id=project_id，框架表已幂等建表）")
        return _saver


async def close_checkpointer() -> None:
    """关闭并清空进程级 checkpointer 单例（lifespan shutdown 调用）。

    Raises:
        无（关闭失败仅告警，不向上传播）。
    """
    global _pool, _saver, _init_lock_ready
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:  # noqa: BLE001 关闭失败不影响退出
            logger.warning("关闭 checkpointer 连接池失败", exc_info=True)
    _pool = None
    _saver = None
    _init_lock_ready = False


async def delete_project_checkpoints(project_id: int) -> None:
    """删除指定项目（thread_id）的全部 checkpoint 数据。

    用于测试清理或项目级联删除（框架表无项目外键，须手动清理）。
    有意不使用进程级连接池（其连接绑定创建时的旧事件循环，测试等
    多循环场景下不可用），而是每次建立临时短连接执行。表不存在时
    （checkpointer 尚未 setup）静默跳过。

    Args:
        project_id: 项目 ID（thread_id = str(project_id)）。

    Raises:
        无（表缺失/连接失败仅告警，不向上传播）。
    """
    thread_id = str(project_id)
    try:
        from psycopg import AsyncConnection

        conninfo = _psycopg_conninfo(settings.DATABASE_URL)
        async with await AsyncConnection.connect(
            conninfo, autocommit=True, prepare_threshold=0
        ) as conn:
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                await conn.execute(f"DELETE FROM {table} WHERE thread_id = %s", (thread_id,))
    except Exception:  # noqa: BLE001 清理失败仅告警
        logger.warning("清理 checkpoint 失败 project_id=%s", project_id, exc_info=True)


def checkpointer_ready() -> bool:
    """查询 checkpointer 是否已初始化（诊断/测试用）。

    Returns:
        已初始化返回 True，否则 False。
    """
    return _init_lock_ready and _saver is not None


def graph_config(project_id: int) -> dict[str, Any]:
    """构造 LangGraph 运行配置（thread_id 绑定项目）。

    Args:
        project_id: 项目 ID。

    Returns:
        ``{"configurable": {"thread_id": str(project_id)}, "recursion_limit": 50}``。
    """
    return {
        "configurable": {"thread_id": str(project_id)},
        "recursion_limit": 50,
    }

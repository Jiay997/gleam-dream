"""Alembic 迁移环境配置。

特性：
- 连接串从 ``app.core.config.settings.DATABASE_URL`` 读取（asyncpg 驱动），
  覆盖 alembic.ini 中的占位 URL；
- 使用 SQLAlchemy 官方异步模式：``async_engine_from_config`` +
  ``connection.run_sync(do_run_migrations)``；
- ``target_metadata`` 指向 ``Base.metadata``，并通过导入 ``app.models``
  注册全部 56 张表模型（autogenerate 依赖）。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings
from app.core.db import Base

# 导入全部模型，确保 Base.metadata 注册完整（autogenerate 依赖此步骤）
import app.models  # noqa: F401

# Alembic Config 对象
config = context.config

# 从应用配置注入真实数据库连接串（asyncpg 异步驱动）
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 迁移目标元数据
target_metadata = Base.metadata

#: LangGraph checkpointer 运行时自建表：不属于业务模型，autogenerate 必须忽略，
#: 否则每次检查都会误判为「需要 drop_table」。
_LANGGRAPH_RUNTIME_TABLES: frozenset[str] = frozenset(
    {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
)


def include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    """Autogenerate 对象过滤器：排除运行时自建表。

    Args:
        obj: 被检查的 schema 对象。
        name: 对象名。
        type_: 对象类型（``table`` / ``index`` / ``column`` ...）。
        reflected: 是否来自数据库反射。
        compare_to: 对比目标对象。

    Returns:
        True 表示纳入比较；False 表示跳过。
    """
    if type_ == "table" and name in _LANGGRAPH_RUNTIME_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    """离线模式：仅生成 SQL 脚本，不连接数据库。

    Raises:
        无（通过 context.run_migrations 输出 SQL 至 stdout）。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在同步连接上执行迁移（由 run_sync 回调）。

    Args:
        connection: 由异步连接 run_sync 提供的同步外观连接。
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # 关闭默认命名约定干扰：Base.metadata 已带 naming_convention
        render_as_batch=False,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步模式执行迁移：创建异步引擎并在 run_sync 中跑迁移。

    Raises:
        无（连接失败时异常向上抛出，由调用方观察）。
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移（asyncpg 异步驱动）。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

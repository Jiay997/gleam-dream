"""一键初始化数据库脚本（幂等全流程）。

执行步骤（重复执行不报错）：
1. 连接 PostgreSQL 服务器（postgres 库），检查并按需创建 ``gleam-dream`` 数据库；
2. 调用 backend/.venv 的 alembic 执行 ``upgrade head``（autogen 迁移建表）；
3. 执行 ``scripts/seed.py`` 写入种子数据（ON CONFLICT DO NOTHING / 先查后插）。

用法（任选其一，均从 backend 目录或任意目录可用）::

    backend/.venv/Scripts/python backend/scripts/init_db.py
    cd backend && .venv/Scripts/python scripts/init_db.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

# sys.path 处理：scripts 目录（便于 import seed）+ backend 根目录（便于 import app）
SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPTS_DIR.parent
for _p in (str(SCRIPTS_DIR), str(BACKEND_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402

# 目标业务库名（含连字符，SQL 中必须加双引号）
TARGET_DB = "gleam-dream"


def _log(message: str) -> None:
    """打印日志行（统一前缀，兼容 GBK 控制台）。

    Args:
        message: 日志内容。
    """
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"[init_db] {message}")


async def ensure_database() -> bool:
    """检查并按需创建目标业务库（幂等）。

    连接服务器默认维护库 ``postgres``，在 AUTOCOMMIT 隔离级别下执行
    ``CREATE DATABASE``（该语句不允许在事务块内运行）。

    Returns:
        True 表示本次新建了库，False 表示库已存在。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: 连接服务器失败时抛出。
    """
    from sqlalchemy.engine import make_url

    url = make_url(settings.DATABASE_URL).set(database="postgres")
    admin_engine = create_async_engine(url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TARGET_DB},
            )
            if exists.scalar() == 1:
                _log(f"database '{TARGET_DB}' already exists, skip creation")
                return False
            await conn.execute(text(f'CREATE DATABASE "{TARGET_DB}"'))
            _log(f"database '{TARGET_DB}' created")
            return True
    finally:
        await admin_engine.dispose()


def run_alembic_upgrade() -> int:
    """执行 ``alembic upgrade head``（子进程，cwd=backend）。

    Returns:
        子进程退出码（0 表示成功）。

    Raises:
        SystemExit: alembic 退出码非 0 时终止初始化流程。
    """
    _log("running alembic upgrade head ...")
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode != 0:
        _log(f"alembic upgrade failed with exit code {proc.returncode}")
        raise SystemExit(proc.returncode)
    _log("alembic upgrade head done")
    return proc.returncode


async def run_seed() -> dict[str, int]:
    """执行种子数据写入。

    Returns:
        各表实际新插入行数字典。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: 种子写入失败时抛出。
    """
    from seed import seed_all

    _log("seeding global data ...")
    counts = await seed_all()
    _log(f"seed done: {counts}")
    return counts


async def main() -> None:
    """初始化主流程：建库 → 迁移 → 种子。"""
    created = await ensure_database()
    run_alembic_upgrade()
    counts = await run_seed()
    _log(
        f"init finished (db_created={created}, "
        f"seed_total={sum(counts.values())})"
    )


if __name__ == "__main__":
    asyncio.run(main())

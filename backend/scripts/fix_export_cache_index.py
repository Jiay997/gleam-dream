"""一次性修复脚本：把 export_jobs.cache_key 的唯一索引改为非唯一索引。

背景：``uq_export_jobs_cache_key``（唯一）会阻断 AC-F7-06-2「复用历史参数
重新导出」——复用必然以相同 cache_key 新建导出任务。迁移文件
``d4e8b1f60c27`` 已改为非唯一索引 ``idx_export_jobs_cache_key``，本脚本用于
修复已按旧版迁移文件建库的本地库（幂等：索引已正确时不做任何事）。

用法::

    cd backend && python scripts/fix_export_cache_index.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.db import async_engine


async def main() -> None:
    """执行索引修正（幂等）。"""
    async with async_engine.begin() as conn:
        rows = (
            await conn.execute(
                text("select indexname from pg_indexes where tablename = 'export_jobs'")
            )
        ).all()
        names = {row[0] for row in rows}
        if "uq_export_jobs_cache_key" in names:
            await conn.execute(text("DROP INDEX uq_export_jobs_cache_key"))
            print("dropped unique index uq_export_jobs_cache_key")
        if "idx_export_jobs_cache_key" not in names:
            await conn.execute(
                text(
                    "CREATE INDEX idx_export_jobs_cache_key ON export_jobs (cache_key) "
                    "WHERE cache_key IS NOT NULL"
                )
            )
            print("created index idx_export_jobs_cache_key")
        print("done")


if __name__ == "__main__":
    asyncio.run(main())

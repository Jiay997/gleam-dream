"""一次性诊断脚本：核对 V4 迁移落库状态与 export_jobs 索引。"""

from __future__ import annotations

import asyncio

from sqlalchemy import text

from app.core.db import async_engine

V4_TABLES = (
    "script_runs",
    "export_presets",
    "voice_bindings",
    "render_batches",
    "subtitle_lines",
    "mix_profiles",
    "director_notes",
    "storyboard_ops",
    "storyboard_templates",
    "script_template_refs",
    "script_export_logs",
)


async def main() -> None:
    """打印索引、迁移版本与 V4 表存在性。"""
    async with async_engine.connect() as conn:
        rows = (
            await conn.execute(
                text("select indexname from pg_indexes where tablename = 'export_jobs' order by 1")
            )
        ).all()
        print("export_jobs indexes:", [row[0] for row in rows])
        version = (await conn.execute(text("select version_num from alembic_version"))).all()
        print("alembic version:", version)
        present = (
            await conn.execute(
                text(
                    "select table_name from information_schema.tables where table_name = any(:names)"
                ),
                {"names": list(V4_TABLES)},
            )
        ).all()
        print("v4 tables present:", len(present), "/", len(V4_TABLES))


if __name__ == "__main__":
    asyncio.run(main())

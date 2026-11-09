"""校验迁移状态与 analytics_events 表存在性（一次性诊断脚本）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import text

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.core.db import async_engine  # noqa: E402


async def main() -> None:
    async with async_engine.connect() as conn:
        version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
        table = (await conn.execute(text("SELECT to_regclass('public.analytics_events')"))).scalar()
        constraint = (
            await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_ai_call_logs_call_type'"
                )
            )
        ).scalar()
        print("alembic_version =", version)
        print("analytics_events =", table)
        print("ai_call_logs CHECK =", constraint)


asyncio.run(main())

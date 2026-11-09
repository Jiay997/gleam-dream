"""校验 shots.composition_spec 列已由迁移创建。

迁移的验收不能只靠“命令退出码为 0”：这里直接查 information_schema，
确认列名与类型都正确。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.db import async_session_factory

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_composition_spec_column_exists_as_jsonb() -> None:
    """表 shots 必须存在 jsonb 类型的 composition_spec 列。"""
    async with async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'shots' AND column_name = 'composition_spec'"
                )
            )
        ).first()
    assert row is not None, "shots.composition_spec 不存在，请执行 alembic upgrade head"
    assert row[0] == "jsonb"
    assert row[1] == "YES"

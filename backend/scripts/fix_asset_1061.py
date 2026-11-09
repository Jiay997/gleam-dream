# -*- coding: utf-8 -*-
"""修复脚本：将资产1061（沈教授）从僵尸 generating 状态恢复为 planning。

背景：1061 处于 generating 但无版本行、无扣费（状态不一致缺陷），
UI 无法自行恢复。本脚本把状态重置为 planning 以便在 UI 重新发起生成。

返回值：无（打印更新结果）。
异常：连接失败或 SQL 错误打印并退出非零。
"""
import asyncio
import os
import sys

import asyncpg

DSN = None
with open(os.path.join(os.path.dirname(__file__), "..", ".env"), encoding="utf-8") as f:
    for line in f:
        if line.startswith("DATABASE_URL="):
            DSN = line.split("=", 1)[1].strip().replace("postgresql+asyncpg://", "postgresql://")
            break


async def main() -> None:
    """主流程：条件更新 1061 状态并回读确认。"""
    conn = await asyncpg.connect(DSN)
    try:
        status = await conn.fetchval(
            "UPDATE assets SET status='planning', updated_at=now() "
            "WHERE id=1061 AND status='generating' RETURNING status"
        )
        print("updated to:", status)
        row = await conn.fetchrow("SELECT id, name, status FROM assets WHERE id=1061")
        print("now:", dict(row) if row else None)
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        sys.exit(1)

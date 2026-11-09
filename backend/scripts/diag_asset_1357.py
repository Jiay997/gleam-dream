# -*- coding: utf-8 -*-
"""诊断脚本：查项目1357渲染任务状态汇总。

输出 render_tasks 中 12 个镜头任务的状态分布与单个任务明细（含失败原因）。
返回值：无（打印结果）。
异常：连接失败打印错误并退出非零。
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
    """主流程：汇总渲染任务状态。"""
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT status, COUNT(*) n, SUM(actual_cost_credits) cost FROM render_tasks WHERE project_id=1357 GROUP BY status"
        )
        print("== 状态分布 ==")
        for r in rows:
            print(f"  {r['status']}: {r['n']} (cost={r['cost']})")

        rows = await conn.fetch(
            "SELECT id, shot_id, status, retry_count, failure_reason, actual_cost_credits FROM render_tasks WHERE project_id=1357 ORDER BY id"
        )
        for r in rows:
            fr = (r["failure_reason"] or "")[:50]
            print(f"  #{r['id']} shot={r['shot_id']} {r['status']} retry={r['retry_count']} cost={r['actual_cost_credits']} {fr}")
    finally:
        await conn.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}")
        sys.exit(1)

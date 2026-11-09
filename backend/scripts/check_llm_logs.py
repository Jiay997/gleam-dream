"""查询最近 LLM 调用记账（诊断脚本）：验证 llm_text 落库口径与 reasoning 计量。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import desc, select

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.core.db import async_session_factory  # noqa: E402
from app.models import AiCallLog  # noqa: E402


async def main() -> None:
    async with async_session_factory() as session:
        rows = (
            await session.execute(
                select(AiCallLog)
                .where(AiCallLog.call_type == "llm_text")
                .order_by(desc(AiCallLog.id))
                .limit(8)
            )
        ).scalars().all()
    print(f"最近 {len(rows)} 条 llm_text 调用：")
    for r in rows:
        snap = r.request_snapshot or {}
        resp = r.response_snapshot or {}
        print(
            f"  id={r.id} model={r.model_key} status={r.status} "
            f"tokens={r.prompt_tokens}/{r.completion_tokens} cost={r.cost_credits} "
            f"latency={r.latency_ms}ms task_type={snap.get('task_type')} "
            f"finish={resp.get('finish_reason')} preview={str(resp.get('text_preview'))[:40]!r}"
        )


asyncio.run(main())

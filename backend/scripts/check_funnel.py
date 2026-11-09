"""直接调用 funnel_summary 验证新用户激活口径（一次性诊断脚本）。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from app.core.db import async_session_factory  # noqa: E402
from app.services.analytics_service import funnel_summary  # noqa: E402


async def main() -> None:
    async with async_session_factory() as session:
        summary = await funnel_summary(session, days=1)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    rate = summary["activation_rate"]
    assert rate is None or rate <= 1.0, f"激活率超标: {rate}"
    print("activation_rate ≤ 1 校验通过")


asyncio.run(main())

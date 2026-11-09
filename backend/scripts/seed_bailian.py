# -*- coding: utf-8 -*-
"""百炼（AI 漫剧）模型接入种子脚本（幂等）。

注册三个真实媒体模型到 model_registry，并把相关任务类型的路由
调整为百炼优先（stub 降级为兜底），同步 price_rules 参考单价。

用法：
    backend/.venv/Scripts/python.exe backend/scripts/seed_bailian.py
"""

from __future__ import annotations

import asyncio
import sys
import io
from pathlib import Path

import asyncpg

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 兼容直接运行：加载 .env 中的 DATABASE_URL
ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_database_url() -> str:
    """从 .env 读取 DATABASE_URL 并转为 asyncpg 连接串。

    返回:
        postgresql:// 开头的连接串。
    """
    url = ""
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
                break
    if not url:
        # 不再使用硬编码兜底凭据，缺失配置时显式失败
        raise SystemExit("未找到 DATABASE_URL（请先配置 backend/.env）")
    return url.replace("postgresql+asyncpg://", "postgresql://")


# ---- model_registry 注册行 ----
REGISTRY_ROWS = [
    # model_key, model_name, provider, capability, task_types, quality, unit, price, latency, concurrency
    (
        "bailian-tts", "Qwen Audio TTS Plus（百炼语音合成）", "bailian", "tts",
        ["tts"], 8.0, "per_second", 2, 6000, 4,
    ),
    (
        "bailian-image", "Qwen Image 3.0（百炼文生图）", "bailian", "image",
        ["text_to_image", "image_to_image"], 8.5, "per_image", 15, 15000, 4,
    ),
    (
        "bailian-video", "Wan 3.0（百炼视频生成）", "bailian", "video",
        ["text_to_video", "image_to_video"], 8.0, "per_second", 40, 180000, 2,
    ),
]

# ---- 路由调整：(task_type, priority, model_key, is_fallback) ----
ROUTE_ROWS = [
    ("tts", 1, "bailian-tts", False),
    ("tts", 2, "stub-tts", True),
    ("text_to_image", 1, "bailian-image", False),
    ("text_to_image", 2, "stub-image-hd", False),
    ("text_to_image", 3, "stub-image", True),
    ("image_to_image", 1, "bailian-image", False),
    ("image_to_image", 2, "stub-image-hd", False),
    ("image_to_image", 3, "stub-image", True),
    ("text_to_video", 1, "bailian-video", False),
    ("text_to_video", 2, "stub-video", True),
    ("image_to_video", 1, "bailian-video", False),
    ("image_to_video", 2, "stub-video", True),
]

# ---- price_rules：(task_type, model_key, price) ----
PRICE_ROWS = [
    ("tts", "bailian-tts", 2),
    ("text_to_image", "bailian-image", 15),
    ("image_to_image", "bailian-image", 15),
    ("text_to_video", "bailian-video", 40),
    ("image_to_video", "bailian-video", 40),
]


async def main() -> None:
    """执行幂等种子：注册模型 → 清理冲突路由 → 写路由 → 写价格。"""
    conn = await asyncpg.connect(load_database_url())
    try:
        # 1. model_registry（按 model_key 幂等更新）
        for key, name, provider, capability, task_types, quality, unit, price, latency, conc in REGISTRY_ROWS:
            await conn.execute(
                """
                INSERT INTO model_registry
                    (model_key, model_name, provider, capability, task_types,
                     quality_score, unit, unit_price_credits, avg_latency_ms,
                     max_concurrency, is_available, region, version_tag)
                VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10,true,'cn','2026-10')
                ON CONFLICT (model_key) DO UPDATE SET
                    model_name = EXCLUDED.model_name,
                    provider = EXCLUDED.provider,
                    capability = EXCLUDED.capability,
                    task_types = EXCLUDED.task_types,
                    quality_score = EXCLUDED.quality_score,
                    unit = EXCLUDED.unit,
                    unit_price_credits = EXCLUDED.unit_price_credits,
                    avg_latency_ms = EXCLUDED.avg_latency_ms,
                    max_concurrency = EXCLUDED.max_concurrency,
                    is_available = true,
                    updated_at = now()
                """,
                key, name, provider, capability, json.dumps(task_types),
                quality, unit, price, latency, conc,
            )
        print(f"model_registry upserted: {len(REGISTRY_ROWS)}")

        # 2. 路由：先删除受影响任务类型的既有路由（约束 task_type+priority 唯一），
        #    再按计划重写（stub 全部降级为兜底）。
        affected = {row[0] for row in ROUTE_ROWS}
        await conn.execute(
            "DELETE FROM model_routes WHERE task_type = ANY($1::text[])",
            sorted(affected),
        )
        for task_type, priority, model_key, is_fallback in ROUTE_ROWS:
            await conn.execute(
                """
                INSERT INTO model_routes (task_type, priority, model_key, condition, is_fallback, is_active)
                VALUES ($1,$2,$3,NULL,$4,true)
                """,
                task_type, priority, model_key, is_fallback,
            )
        print(f"model_routes rewritten: {len(ROUTE_ROWS)} rows across {len(affected)} task types")

        # 3. price_rules（task_type+model_key 幂等；unit 取自模型计费单位）
        for task_type, model_key, price in PRICE_ROWS:
            unit = await conn.fetchval(
                "SELECT unit FROM model_registry WHERE model_key=$1", model_key
            )
            exists = await conn.fetchval(
                "SELECT 1 FROM price_rules WHERE task_type=$1 AND model_key=$2",
                task_type, model_key,
            )
            if exists:
                await conn.execute(
                    "UPDATE price_rules SET unit_price_credits=$3, unit=$4 WHERE task_type=$1 AND model_key=$2",
                    task_type, model_key, price, unit,
                )
            else:
                await conn.execute(
                    "INSERT INTO price_rules (task_type, model_key, unit, unit_price_credits) VALUES ($1,$2,$4,$3)",
                    task_type, model_key, price, unit,
                )
        print(f"price_rules upserted: {len(PRICE_ROWS)}")
        print("SEED DONE")
    finally:
        await conn.close()


import json  # noqa: E402  置底避免顶部噪音

if __name__ == "__main__":
    asyncio.run(main())

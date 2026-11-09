# -*- coding: utf-8 -*-
"""模型配置回填种子脚本（幂等）。

为已有的 ``model_registry`` 行补齐"可配置接入参数"（protocol / base_url /
api_model / extra_config），使应用内模型配置页可以直接查看与编辑；同时
把 ``.env`` 中的默认值作为初始配置写入，密钥仍留在 .env（api_key 不回填，
避免把明文密钥复制进数据库）。

回填规则：仅当该行 ``protocol`` 为空时写入，已通过界面修改过的配置不会被覆盖。

用法：
    backend/.venv/Scripts/python.exe backend/scripts/seed_model_configs.py
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import asyncpg

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_database_url() -> str:
    """从 .env 读取 DATABASE_URL 并转为 asyncpg 连接串。

    Returns:
        ``postgresql://`` 开头的连接串。
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


def backfill_rows() -> list[dict[str, object]]:
    """构造待回填的模型配置行（model_key → 接入参数）。

    Returns:
        含 model_key / protocol / base_url / api_model / extra_config 的列表。
    """
    return [
        {
            "model_key": "deepseek-flash",
            "protocol": "openai_chat",
            "base_url": settings.DEEPSEEK_BASE_URL,
            "api_model": settings.DEEPSEEK_MODEL,
            "extra_config": None,
        },
        {
            "model_key": "glm-5.3-flash",
            "protocol": "openai_chat",
            "base_url": settings.GLM_BASE_URL,
            "api_model": settings.GLM_MODEL,
            "extra_config": None,
        },
        {
            "model_key": "bailian-image",
            "protocol": "dashscope_image",
            "base_url": settings.BAILIAN_BASE_URL,
            "api_model": settings.BAILIAN_IMAGE_MODEL,
            "extra_config": None,
        },
        {
            "model_key": "bailian-video",
            "protocol": "dashscope_video",
            "base_url": settings.BAILIAN_BASE_URL,
            "api_model": settings.BAILIAN_VIDEO_MODEL,
            "extra_config": None,
        },
        {
            # TTS 行走 WebSocket，base_url 存 WS 推理地址
            "model_key": "bailian-tts",
            "protocol": "dashscope_tts",
            "base_url": settings.BAILIAN_WS_URL,
            "api_model": settings.BAILIAN_TTS_MODEL,
            "extra_config": {"voice": settings.BAILIAN_TTS_VOICE},
        },
        {"model_key": "stub-image", "protocol": "stub", "base_url": None, "api_model": None, "extra_config": None},
        {"model_key": "stub-image-hd", "protocol": "stub", "base_url": None, "api_model": None, "extra_config": None},
        {"model_key": "stub-video", "protocol": "stub", "base_url": None, "api_model": None, "extra_config": None},
        {"model_key": "stub-tts", "protocol": "stub", "base_url": None, "api_model": None, "extra_config": None},
        {"model_key": "stub-bgm", "protocol": "stub", "base_url": None, "api_model": None, "extra_config": None},
    ]


async def main() -> None:
    """执行幂等回填：仅补齐 protocol 为空的历史行。"""
    conn = await asyncpg.connect(load_database_url())
    try:
        updated = 0
        for row in backfill_rows():
            result = await conn.execute(
                """
                UPDATE model_registry
                   SET protocol = $2,
                       base_url = $3,
                       api_model = $4,
                       extra_config = $5::jsonb
                 WHERE model_key = $1 AND protocol IS NULL
                """,
                row["model_key"],
                row["protocol"],
                row["base_url"],
                row["api_model"],
                json.dumps(row["extra_config"]) if row["extra_config"] else None,
            )
            if result.endswith("1"):
                updated += 1
        print(f"model_registry config backfilled: {updated} rows")
        rows = await conn.fetch(
            "SELECT model_key, capability, protocol, api_model FROM model_registry "
            "WHERE capability IN ('llm', 'image', 'tts', 'video') ORDER BY capability, id"
        )
        for item in rows:
            print(dict(item))
        print("SEED MODEL CONFIGS DONE")
    finally:
        await conn.close()


asyncio.run(main())

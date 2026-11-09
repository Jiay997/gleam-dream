"""种子数据脚本（幂等）。

写入全局主数据与系统配置，可重复执行（ON CONFLICT DO NOTHING / 先查后插）：
- ``plans``          套餐 3 档（free/pro/studio）；
- ``style_presets``  画风预设 4 套；
- ``model_registry`` 模型注册 7 个（stub 系列用于沙箱）；
- ``model_routes``   任务类型 → 候选模型路由；
- ``price_rules``    计费单价规则（生效期固定起点，保证幂等）；
- ``tts_voices``     音色 10 条；
- ``bgm_tracks``     BGM 曲库 20 条（覆盖 8 种情绪标签）；
- ``subtitle_styles`` 字幕样式 3 套（全局）；
- ``system_configs`` 系统阈值配置 17 项。

用法::

    python scripts/seed.py            # 直接运行
    from scripts.seed import seed_all # 由 init_db.py 调用
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

# 将 backend 根目录加入 sys.path，保证可导入 app 包
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.db import async_session_factory  # noqa: E402
from app.models import (  # noqa: E402
    BgmTrack,
    ModelRegistry,
    ModelRoute,
    Plan,
    PriceRule,
    StylePreset,
    SubtitleStyle,
    SystemConfig,
    TtsVoice,
)

# price_rules 唯一键含 effective_from，必须固定值才能保证重复执行幂等
PRICE_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)

# 第三方授权 BGM 的版权到期时间（固定值，幂等）
BGM_LICENSE_EXPIRES_AT = datetime(2027, 12, 31, tzinfo=timezone.utc)


async def _insert_conflict_none(session: AsyncSession, model, rows: list[dict], constraint: str) -> int:
    """批量插入并在唯一约束冲突时跳过（ON CONFLICT DO NOTHING）。

    Args:
        session: 异步数据库会话。
        model: 目标 ORM 模型类。
        rows: 待插入行（字典列表）。
        constraint: 冲突检测的唯一约束名。

    Returns:
        实际新插入的行数。
    """
    if not rows:
        return 0
    stmt = pg_insert(model).values(rows).on_conflict_do_nothing(constraint=constraint)
    result = await session.execute(stmt)
    return int(result.rowcount or 0)


async def _insert_missing_by_key(
    session: AsyncSession, model, rows: list[dict], key_attr: str
) -> int:
    """对无唯一约束的表按业务键先查后插（仅插入缺失键）。

    Args:
        session: 异步数据库会话。
        model: 目标 ORM 模型类。
        rows: 待插入行（字典列表）。
        key_attr: 业务键字段名（如 title/name）。

    Returns:
        实际新插入的行数。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: 查询或插入失败时向上抛出。
    """
    keys = [row[key_attr] for row in rows]
    existing = (await session.execute(select(getattr(model, key_attr)).where(getattr(model, key_attr).in_(keys)))).scalars().all()
    existing_set = set(existing)
    missing = [row for row in rows if row[key_attr] not in existing_set]
    if not missing:
        return 0
    session.add_all([model(**row) for row in missing])
    await session.flush()
    return len(missing)


def _plan_rows() -> list[dict]:
    """构造套餐种子行。

    Returns:
        plans 表行列表（free/pro/studio 三档）。
    """
    return [
        {
            "key": "free", "name": "免费版", "billing_period": "month",
            "price_cents": 0, "currency": "CNY", "credits_per_period": 500,
            "concurrency_limit": 2,
            "benefits": {"max_resolution": "720p", "no_watermark": False,
                         "four_k": False, "watermark": True},
            "is_active": True, "sort_order": 1,
        },
        {
            "key": "pro", "name": "专业版", "billing_period": "month",
            "price_cents": 4900, "currency": "CNY", "credits_per_period": 5000,
            "concurrency_limit": 5,
            "benefits": {"max_resolution": "1080p", "no_watermark": False,
                         "four_k": False, "watermark": False},
            "is_active": True, "sort_order": 2,
        },
        {
            "key": "studio", "name": "工作室版", "billing_period": "month",
            "price_cents": 19900, "currency": "CNY", "credits_per_period": 30000,
            "concurrency_limit": 10,
            "benefits": {"max_resolution": "4k", "no_watermark": True,
                         "four_k": True, "watermark": False},
            "is_active": True, "sort_order": 3,
        },
    ]


def _style_preset_rows() -> list[dict]:
    """构造画风预设种子行。

    Returns:
        style_presets 表行列表（4 套画风）。
    """
    return [
        {
            "preset_key": "live_action", "name": "真人感",
            "description": "写实电影质感，自然光影，贴近真人短剧观感",
            "prompt_prefix": "cinematic photography, realistic skin texture, natural lighting, 35mm film grain, shallow depth of field",
            "negative_prompt": "cartoon, anime, illustration, 3d render, plastic skin",
            "params": {"style_weight": 0.8, "photorealism": True, "default_lens": "35mm"},
            "is_active": True, "sort_order": 1,
        },
        {
            "preset_key": "anime", "name": "二次元",
            "description": "日系动漫风格，明快色彩，赛璐璐/厚涂均可",
            "prompt_prefix": "anime style, cel shading, vibrant colors, clean line art, japanese animation aesthetic",
            "negative_prompt": "photorealistic, realistic skin, 3d render, western cartoon",
            "params": {"style_weight": 0.9, "cel_shading": True, "line_art": True},
            "is_active": True, "sort_order": 2,
        },
        {
            "preset_key": "chinese_style", "name": "国风",
            "description": "中国古风水墨/工笔质感，东方审美配色",
            "prompt_prefix": "chinese ink painting style, guofeng aesthetic, elegant oriental color palette, traditional chinese costume",
            "negative_prompt": "western style, cyberpunk, neon, modern architecture",
            "params": {"style_weight": 0.85, "ink_wash": True, "palette": "oriental"},
            "is_active": True, "sort_order": 3,
        },
        {
            "preset_key": "three_d", "name": "3D",
            "description": "三维渲染质感，皮克斯/UE 风格",
            "prompt_prefix": "3d render, pixar style, global illumination, subsurface scattering, octane render",
            "negative_prompt": "flat 2d, sketch, hand drawn, photorealistic skin",
            "params": {"style_weight": 0.85, "renderer": "octane", "ray_tracing": True},
            "is_active": True, "sort_order": 4,
        },
    ]


def _model_registry_rows() -> list[dict]:
    """构造模型注册表种子行。

    Returns:
        model_registry 表行列表（7 个模型）。
    """
    return [
        {
            "model_key": "deepseek-flash", "model_name": "DeepSeek-Flash", "provider": "deepseek",
            "capability": "llm", "task_types": ["llm_text"], "quality_score": 0.88,
            "unit": "per_1k_token", "unit_price_credits": 2, "avg_latency_ms": 1500,
            "max_concurrency": 8, "is_available": True, "region": "cn",
            "protocol": "openai_chat", "base_url": settings.DEEPSEEK_BASE_URL,
            "api_model": settings.DEEPSEEK_MODEL,
        },
        {
            "model_key": "glm-5.3-flash", "model_name": "GLM-5.3-Flash", "provider": "glm",
            "capability": "llm", "task_types": ["llm_text"], "quality_score": 0.82,
            "unit": "per_1k_token", "unit_price_credits": 1, "avg_latency_ms": 1200,
            "max_concurrency": 8, "is_available": True, "region": "cn",
            "protocol": "openai_chat", "base_url": settings.GLM_BASE_URL,
            "api_model": settings.GLM_MODEL,
        },
        {
            "model_key": "stub-image", "model_name": "Stub Image（沙箱文生图）", "provider": "stub",
            "capability": "image", "task_types": ["text_to_image", "image_to_image"],
            "quality_score": 0.60, "unit": "per_image", "unit_price_credits": 8,
            "avg_latency_ms": 8000, "max_concurrency": 4, "is_available": True, "region": "cn",
            "protocol": "stub", "base_url": None, "api_model": None,
        },
        {
            "model_key": "stub-image-hd", "model_name": "Stub Image HD（沙箱高清文生图）", "provider": "stub",
            "capability": "image", "task_types": ["text_to_image", "image_to_image"],
            "quality_score": 0.75, "unit": "per_image", "unit_price_credits": 20,
            "avg_latency_ms": 12000, "max_concurrency": 4, "is_available": True, "region": "cn",
            "protocol": "stub", "base_url": None, "api_model": None,
        },
        {
            "model_key": "stub-video", "model_name": "Stub Video（沙箱视频生成）", "provider": "stub",
            "capability": "video", "task_types": ["text_to_video", "image_to_video"],
            "quality_score": 0.60, "unit": "per_second", "unit_price_credits": 30,
            "avg_latency_ms": 30000, "max_concurrency": 2, "is_available": True, "region": "cn",
            "protocol": "stub", "base_url": None, "api_model": None,
        },
        {
            "model_key": "stub-tts", "model_name": "Stub TTS（沙箱语音合成）", "provider": "stub",
            "capability": "tts", "task_types": ["tts"], "quality_score": 0.65,
            "unit": "per_second", "unit_price_credits": 2, "avg_latency_ms": 2000,
            "max_concurrency": 8, "is_available": True, "region": "cn",
            "protocol": "stub", "base_url": None, "api_model": None,
        },
        {
            "model_key": "stub-bgm", "model_name": "Stub BGM（沙箱音乐生成）", "provider": "stub",
            "capability": "bgm", "task_types": ["bgm"], "quality_score": 0.60,
            "unit": "per_call", "unit_price_credits": 10, "avg_latency_ms": 1500,
            "max_concurrency": 4, "is_available": True, "region": "cn",
            "protocol": "stub", "base_url": None, "api_model": None,
        },
    ]


def _model_route_rows() -> list[dict]:
    """构造模型路由种子行（任务类型 → 候选模型，priority 1 最高）。

    Returns:
        model_routes 表行列表。
    """
    return [
        {"task_type": "text_to_image", "priority": 1, "model_key": "stub-image-hd", "is_fallback": False},
        {"task_type": "text_to_image", "priority": 2, "model_key": "stub-image", "is_fallback": True},
        {"task_type": "image_to_image", "priority": 1, "model_key": "stub-image-hd", "is_fallback": False},
        {"task_type": "image_to_image", "priority": 2, "model_key": "stub-image", "is_fallback": True},
        {"task_type": "text_to_video", "priority": 1, "model_key": "stub-video", "is_fallback": True},
        {"task_type": "image_to_video", "priority": 1, "model_key": "stub-video", "is_fallback": True},
        {"task_type": "tts", "priority": 1, "model_key": "stub-tts", "is_fallback": True},
        {"task_type": "bgm", "priority": 1, "model_key": "stub-bgm", "is_fallback": True},
        {"task_type": "llm_text", "priority": 1, "model_key": "deepseek-flash", "is_fallback": False},
        {"task_type": "llm_text", "priority": 2, "model_key": "glm-5.3-flash", "is_fallback": True},
    ]


def _price_rule_rows() -> list[dict]:
    """构造计费单价规则种子行（与 model_registry 单价一一对应）。

    Returns:
        price_rules 表行列表。
    """
    common = {"currency_basis": "CNY", "effective_from": PRICE_EFFECTIVE_FROM, "is_active": True}
    return [
        {**common, "model_key": "deepseek-flash", "task_type": "llm_text", "unit": "per_1k_token", "unit_price_credits": 2},
        {**common, "model_key": "glm-5.3-flash", "task_type": "llm_text", "unit": "per_1k_token", "unit_price_credits": 1},
        {**common, "model_key": "stub-image", "task_type": "text_to_image", "unit": "per_image", "unit_price_credits": 8},
        {**common, "model_key": "stub-image-hd", "task_type": "text_to_image", "unit": "per_image", "unit_price_credits": 20},
        {**common, "model_key": "stub-image", "task_type": "image_to_image", "unit": "per_image", "unit_price_credits": 8},
        {**common, "model_key": "stub-image-hd", "task_type": "image_to_image", "unit": "per_image", "unit_price_credits": 20},
        {**common, "model_key": "stub-video", "task_type": "text_to_video", "unit": "per_second", "unit_price_credits": 30},
        {**common, "model_key": "stub-video", "task_type": "image_to_video", "unit": "per_second", "unit_price_credits": 30},
        {**common, "model_key": "stub-tts", "task_type": "tts", "unit": "per_second", "unit_price_credits": 2},
        {**common, "model_key": "stub-bgm", "task_type": "bgm", "unit": "per_call", "unit_price_credits": 10},
    ]


def _tts_voice_rows() -> list[dict]:
    """构造音色库种子行。

    Returns:
        tts_voices 表行列表（10 条，全部 zh-CN / available）。
    """
    return [
        {"voice_key": "female-warm-01", "name": "温暖女声", "gender": "female", "language": "zh-CN", "style_tags": ["warm", "gentle"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "female-sweet-02", "name": "甜美女声", "gender": "female", "language": "zh-CN", "style_tags": ["sweet", "young"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "female-calm-03", "name": "沉稳女声", "gender": "female", "language": "zh-CN", "style_tags": ["calm", "professional"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "female-lively-04", "name": "活泼女声", "gender": "female", "language": "zh-CN", "style_tags": ["lively", "cheerful"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "female-romantic-05", "name": "浪漫女声", "gender": "female", "language": "zh-CN", "style_tags": ["romance", "soft"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "male-deep-01", "name": "低沉男声", "gender": "male", "language": "zh-CN", "style_tags": ["deep", "serious"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "male-warm-02", "name": "温和男声", "gender": "male", "language": "zh-CN", "style_tags": ["warm", "narration"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "male-energetic-03", "name": "活力男声", "gender": "male", "language": "zh-CN", "style_tags": ["energetic", "young"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "male-magnetic-04", "name": "磁性男声", "gender": "male", "language": "zh-CN", "style_tags": ["magnetic", "tension"], "provider": "stub-tts", "status": "available"},
        {"voice_key": "neutral-narrator-01", "name": "纪录片旁白", "gender": "neutral", "language": "zh-CN", "style_tags": ["narration", "documentary"], "provider": "stub-tts", "status": "available"},
    ]


def _bgm_track_rows() -> list[dict]:
    """构造 BGM 曲库种子行（20 条，覆盖 8 种情绪标签）。

    Returns:
        bgm_tracks 表行列表。
    """
    catalog: list[tuple[str, str, str, int, int, int]] = [
        # (title, artist, emotion, bpm, duration_ms, license_type 0=self 1=third_party)
        ("晨光漫步", "GleamDream 音效组", "calm", 72, 120000, 0),
        ("午后微风", "GleamDream 音效组", "calm", 68, 150000, 0),
        ("深夜书房", "GleamDream 音效组", "calm", 60, 180000, 1),
        ("街角咖啡", "GleamDream 音效组", "joy", 108, 90000, 0),
        ("毕业旅行", "GleamDream 音效组", "joy", 120, 105000, 1),
        ("元气早晨", "GleamDream 音效组", "joy", 126, 75000, 0),
        ("暴雨将至", "GleamDream 音效组", "anger", 140, 90000, 1),
        ("对峙", "GleamDream 音效组", "anger", 132, 72000, 0),
        ("雨夜站台", "GleamDream 音效组", "sadness", 66, 150000, 0),
        ("旧照片", "GleamDream 音效组", "sadness", 62, 165000, 1),
        ("告别", "GleamDream 音效组", "sadness", 58, 180000, 0),
        ("地下室脚步", "GleamDream 音效组", "fear", 90, 96000, 1),
        ("走廊尽头", "GleamDream 音效组", "fear", 84, 120000, 0),
        ("惊人反转", "GleamDream 音效组", "surprise", 128, 48000, 0),
        ("揭晓时刻", "GleamDream 音效组", "surprise", 118, 60000, 1),
        ("倒计时", "GleamDream 音效组", "tension", 138, 84000, 0),
        ("无声跟踪", "GleamDream 音效组", "tension", 100, 132000, 1),
        ("谈判破裂", "GleamDream 音效组", "tension", 144, 78000, 0),
        ("初遇心动", "GleamDream 音效组", "romance", 80, 144000, 0),
        ("月下告白", "GleamDream 音效组", "romance", 76, 168000, 1),
    ]
    license_map = {0: "self", 1: "third_party"}
    rows: list[dict] = []
    for idx, (title, artist, emotion, bpm, duration_ms, lic) in enumerate(catalog, start=1):
        rows.append(
            {
                "title": title,
                "artist": artist,
                "mood_tags": [emotion],
                "bpm": bpm,
                "duration_ms": duration_ms,
                "license_type": license_map[lic],
                "license_expires_at": BGM_LICENSE_EXPIRES_AT if lic == 1 else None,
                "is_active": True,
            }
        )
    return rows


def _subtitle_style_rows() -> list[dict]:
    """构造字幕样式种子行（3 套全局样式）。

    Returns:
        subtitle_styles 表行列表。
    """
    return [
        {
            "name": "经典白字黑边",
            "font_family": "Noto Sans SC", "font_size": 64, "color": "#FFFFFF",
            "stroke": {"color": "#000000", "width": 3, "shadow": False},
            "position": "bottom", "safe_area": {"top": 0.10, "bottom": 0.12},
            "is_default": True,
        },
        {
            "name": "综艺黄底强调",
            "font_family": "Noto Sans SC", "font_size": 72, "color": "#1A1A1A",
            "stroke": {"background": "#FFD700", "width": 8, "border_color": "#000000"},
            "position": "bottom", "safe_area": {"top": 0.10, "bottom": 0.12},
            "is_default": False,
        },
        {
            "name": "极简白字阴影",
            "font_family": "Noto Sans SC", "font_size": 56, "color": "#FFFFFF",
            "stroke": {"color": "#000000", "width": 0, "shadow": True, "shadow_blur": 6},
            "position": "bottom", "safe_area": {"top": 0.10, "bottom": 0.12},
            "is_default": False,
        },
    ]


def _system_config_rows() -> list[dict]:
    """构造系统配置种子行（阈值项，对齐 00 契约 §6.8）。

    Returns:
        system_configs 表行列表（17 项）。
    """
    rows = [
        ("face_pass_threshold", 0.80, "角色人脸一致性通过线"),
        ("face_warn_low", 0.68, "人脸一致性警告区间下限"),
        ("clip_pass_threshold", 0.72, "CLIP 相似度通过线"),
        ("qc_pass_threshold", 0.80, "质检综合分通过线"),
        ("parse_confidence_threshold", 0.75, "剧本解析置信度阈值"),
        ("freeze_timeout_minutes", 30, "积分冻结自动解冻时长（分钟）"),
        ("max_retries", 2, "单任务自动重试上限"),
        ("max_qc_rounds", 3, "质检修正循环上限"),
        ("snapshot_limit", 30, "项目快照上限"),
        ("approval_expire_days", 7, "审批单过期天数"),
        ("episode_max", 30, "单项目集数上限"),
        ("shot_duration_min_ms", 1000, "单镜头时长下限（毫秒）"),
        ("shot_duration_max_ms", 15000, "单镜头时长上限（毫秒）"),
        ("concurrency_free", 2, "免费版并发生成上限"),
        ("concurrency_pro", 5, "专业版并发生成上限"),
        ("concurrency_studio", 10, "工作室版并发生成上限"),
        ("draft_mode_cost_ratio", 0.15, "草稿模式成本系数"),
    ]
    return [
        {
            "config_key": key,
            "config_value": value,
            "value_type": "number",
            "description": desc,
            "scope": "global",
            "is_public": True,
        }
        for key, value, desc in rows
    ]


async def seed_all() -> dict[str, int]:
    """执行全部种子数据写入（幂等，重复执行不重复插入）。

    Returns:
        各表实际新插入行数字典，键为表名。

    Raises:
        sqlalchemy.exc.SQLAlchemyError: 数据库写入失败时抛出（整体回滚）。
    """
    inserted: dict[str, int] = {}
    async with async_session_factory() as session:
        try:
            inserted["plans"] = await _insert_conflict_none(
                session, Plan, _plan_rows(), "uq_plans_key"
            )
            inserted["style_presets"] = await _insert_conflict_none(
                session, StylePreset, _style_preset_rows(), "uq_style_presets_preset_key"
            )
            inserted["model_registry"] = await _insert_conflict_none(
                session, ModelRegistry, _model_registry_rows(), "uq_model_registry_model_key"
            )
            inserted["model_routes"] = await _insert_conflict_none(
                session, ModelRoute, _model_route_rows(), "uq_model_routes_task_type_priority"
            )
            inserted["price_rules"] = await _insert_conflict_none(
                session, PriceRule, _price_rule_rows(),
                "uq_price_rules_model_key_task_type_effective_from",
            )
            inserted["tts_voices"] = await _insert_conflict_none(
                session, TtsVoice, _tts_voice_rows(), "uq_tts_voices_voice_key"
            )
            inserted["bgm_tracks"] = await _insert_missing_by_key(
                session, BgmTrack, _bgm_track_rows(), "title"
            )
            inserted["subtitle_styles"] = await _insert_missing_by_key(
                session, SubtitleStyle, _subtitle_style_rows(), "name"
            )
            inserted["system_configs"] = await _insert_conflict_none(
                session, SystemConfig, _system_config_rows(), "uq_system_configs_config_key"
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise
    return inserted


async def main() -> None:
    """脚本入口：执行种子写入并打印统计。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    counts = await seed_all()
    print("Seed completed (inserted rows per table):")
    for table, count in counts.items():
        print(f"  {table}: {count}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())

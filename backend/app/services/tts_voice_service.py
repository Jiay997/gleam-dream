"""音色库详情与试听服务模块。

提供音色详情查询、试听样音合成与「重新生成样音」能力，供资产库
音色详情页与后续配音模块复用。

设计约定：
    - 音色库的唯一标识为 ``tts_voices.id``，``voice_key`` 为稳定的业务键；
    - 千问 ``qwen3-tts-flash`` 只接受原生音色名（Cherry/Serena/Ethan 等），
      音色键到引擎音色的解析统一复用 ``voice_service.resolve_voice_profile``，
      禁止在本模块维护第二套映射（避免试听与成片音色不一致）；
    - 试听为实时合成，不写入配音时间线；「重新生成」会把新样音登记到
      ``media_files`` 并回写 ``tts_voices.sample_media_id``，刷新后仍可播放。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.infra.model_gateway import GenRequest, model_gateway
from app.models.asset import Asset, MediaFile, TtsVoice

# 音色库 voice_key -> 千问 qwen3-tts-flash 原生音色名的映射已统一收口到
# ``app.services.voice_service._PROVIDER_VOICE_ALIASES``，本模块不再保留映射表。

# 每个音色的默认试听文案（贴合音色气质，避免所有音色念同一句）。
VOICE_SAMPLE_TEXT: dict[str, str] = {
    "female-warm-01": "别怕，把今天没说完的话，都留在这盏灯下面。",
    "female-sweet-02": "我把热牛奶放在柜台上，等你说完最后一个故事。",
    "female-calm-03": "城市睡去以后，这条街才会露出它真正的模样。",
    "female-lively-04": "喂，你又在偷吃我藏在收银台后面的糖！",
    "female-romantic-05": "如果那天的雨没有停，我们是不是就不会走散？",
    "male-deep-01": "灯还亮着，说明这间店还在等一个晚归的人。",
    "male-warm-02": "慢点喝，烫。今晚不用急着回家。",
    "male-energetic-03": "最后一班车还有三分钟，跑起来！",
    "male-magnetic-04": "有些故事，只有在凌晨两点才会开口。",
    "neutral-narrator-01": "在这座城市的褶皱里，每一盏灯都有自己的秘密。",
}

# 兜底试听文案（音色未登记时使用）。
DEFAULT_SAMPLE_TEXT = "夜色落下来，故事从这里开始。"

# 试听文本长度上限（字符），与前端输入限制保持一致。
MAX_PREVIEW_TEXT_LENGTH = 80


async def resolve_engine_voice(session: AsyncSession, voice: TtsVoice) -> str:
    """解析音色对应的引擎原生音色名（复用配音服务的统一读取链路）。

    Args:
        session: 数据库会话。
        voice: 音色 ORM 实例。

    Returns:
        千问 TTS 支持的音色名（如 ``Cherry``）；音色异常时按性别兜底。

    Raises:
        无：解析失败回落性别默认音色，不阻断试听。
    """
    from app.services import voice_service

    name, _freq = await voice_service.resolve_voice_profile(session, int(voice.id))
    if name:
        return name
    if voice.gender == "male":
        return "Ethan"
    if voice.gender == "female":
        return "Cherry"
    return "Serena"


def default_sample_text(voice: TtsVoice) -> str:
    """获取音色的默认试听文案。

    Args:
        voice: 音色 ORM 实例。

    Returns:
        与音色气质匹配的中文示例句。
    """
    return VOICE_SAMPLE_TEXT.get(voice.voice_key, DEFAULT_SAMPLE_TEXT)


def _media_url(media: MediaFile | None) -> str | None:
    """把媒体记录转换为可访问 URL。

    Args:
        media: 媒体记录（可空）。

    Returns:
        访问 URL；无记录时返回 None。
    """
    if media is None:
        return None
    return media.url or f"/files/{media.object_key}"


async def get_voice_or_error(session: AsyncSession, voice_id: int) -> TtsVoice:
    """按 ID 读取音色，不存在时抛出业务异常。

    Args:
        session: 数据库会话。
        voice_id: 音色 ID。

    Returns:
        音色 ORM 实例。

    Raises:
        BizError: 音色不存在（NOT_FOUND）。
    """
    voice = await session.get(TtsVoice, voice_id)
    if voice is None:
        raise BizError(ErrorCode.NOT_FOUND, "音色不存在")
    return voice


async def _bound_characters(session: AsyncSession, voice_id: int) -> tuple[int, list[dict[str, Any]]]:
    """统计并返回绑定了该音色的角色。

    Args:
        session: 数据库会话。
        voice_id: 音色 ID。

    Returns:
        ``(绑定角色总数, 最近绑定角色列表)``，列表元素含 ``id`` 与 ``name``。
    """
    base = select(Asset).where(
        Asset.voice_id == voice_id,
        Asset.asset_type == "character",
        Asset.deleted_at.is_(None),
    )
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await session.execute(
            base.with_only_columns(Asset.id, Asset.name).order_by(Asset.id.desc()).limit(6)
        )
    ).all()
    return int(total or 0), [{"id": row.id, "name": row.name} for row in rows]


async def get_voice_detail(session: AsyncSession, voice_id: int) -> dict[str, Any]:
    """查询音色详情（含样音、绑定角色与引擎映射）。

    Args:
        session: 数据库会话。
        voice_id: 音色 ID。

    Returns:
        音色详情字典，可直接作为 API ``data`` 返回。

    Raises:
        BizError: 音色不存在（NOT_FOUND）。
    """
    voice = await get_voice_or_error(session, voice_id)
    media = await session.get(MediaFile, voice.sample_media_id) if voice.sample_media_id else None
    bound_total, bound_characters = await _bound_characters(session, voice_id)
    return {
        "id": voice.id,
        "voice_key": voice.voice_key,
        "name": voice.name,
        "gender": voice.gender,
        "language": voice.language,
        "style_tags": voice.style_tags,
        "provider": voice.provider,
        "status": voice.status,
        "engine_voice": await resolve_engine_voice(session, voice),
        "sample_text": default_sample_text(voice),
        "sample_audio_url": _media_url(media),
        "sample_duration_ms": media.duration_ms if media else None,
        "bound_character_count": bound_total,
        "bound_characters": bound_characters,
        "offline_at": voice.offline_at.isoformat() if voice.offline_at else None,
        "license_expires_at": (
            voice.license_expires_at.isoformat() if voice.license_expires_at else None
        ),
        "updated_at": voice.updated_at.isoformat() if voice.updated_at else None,
        "rate_range": {"min": 0.5, "max": 2.0},
        "pitch_range": {"min": -12, "max": 12},
        "max_preview_text_length": MAX_PREVIEW_TEXT_LENGTH,
    }


async def synthesize_preview(
    session: AsyncSession,
    voice_id: int,
    *,
    text: str | None = None,
    rate: float = 1.0,
    pitch: int = 0,
    persist: bool = False,
) -> dict[str, Any]:
    """使用指定音色合成试听样音。

    Args:
        session: 数据库会话。
        voice_id: 音色 ID。
        text: 试听文本（可空，空则使用该音色的默认示例句）。
        rate: 语速倍率（0.5–2.0）。
        pitch: 音调偏移（-12–12 半音）。
        persist: 是否把样音登记到 media_files 并回写音色样音。

    Returns:
        试听结果，含 ``audio_url``、``duration_ms``、``engine_voice`` 等。

    Raises:
        BizError: 音色不存在（NOT_FOUND）、不可用或文本非法（PARAM_INVALID）、
            模型合成失败（MODEL_UNAVAILABLE）。
    """
    voice = await get_voice_or_error(session, voice_id)
    if voice.status != "available":
        raise BizError(ErrorCode.PARAM_INVALID, "该音色已下架，无法试听")

    sample_text = (text or default_sample_text(voice)).strip()
    if not sample_text:
        raise BizError(ErrorCode.PARAM_INVALID, "试听文本不能为空")
    if len(sample_text) > MAX_PREVIEW_TEXT_LENGTH:
        raise BizError(ErrorCode.PARAM_INVALID, f"试听文本不能超过 {MAX_PREVIEW_TEXT_LENGTH} 字")

    engine_voice = await resolve_engine_voice(session, voice)
    result = await model_gateway.generate(
        "tts",
        "tts",
        GenRequest(
            capability="tts",
            prompt=sample_text,
            params={"voice": engine_voice, "rate": rate, "pitch": pitch},
        ),
    )
    if not result.ok or not result.object_key:
        raise BizError(
            ErrorCode.MODEL_UNAVAILABLE,
            f"音色合成失败：{result.error or '模型未返回音频'}",
        )

    duration_ms = int(result.meta.get("duration_ms") or 0)
    audio_url = result.media_url or f"/files/{result.object_key}"
    payload: dict[str, Any] = {
        "voice_id": voice.id,
        "voice_key": voice.voice_key,
        "name": voice.name,
        "engine_voice": engine_voice,
        "text": sample_text,
        "rate": rate,
        "pitch": pitch,
        "audio_key": result.object_key,
        "audio_url": audio_url,
        "duration_ms": duration_ms,
        "mime_type": result.meta.get("mime_type") or "audio/wav",
        "sandbox": bool(result.meta.get("simulated")),
        "generated_at": datetime.now(UTC).isoformat(),
    }

    if persist:
        media = MediaFile(
            owner_user_id=None,
            project_id=None,
            storage_provider="oss",
            bucket="local",
            object_key=result.object_key,
            url=audio_url,
            media_type="audio",
            mime_type=payload["mime_type"],
            file_size_bytes=0,
            duration_ms=duration_ms or None,
            meta={
                "voice_id": voice.id,
                "voice_key": voice.voice_key,
                "engine_voice": engine_voice,
                "preview_text": sample_text,
                "source": "tts_voice_sample",
            },
        )
        session.add(media)
        await session.flush()
        voice.sample_media_id = media.id
        await session.commit()
        payload["sample_media_id"] = media.id
        payload["persisted"] = True
    else:
        payload["persisted"] = False

    return payload


async def regenerate_voice_sample(
    session: AsyncSession,
    voice_id: int,
    *,
    text: str | None = None,
    rate: float = 1.0,
    pitch: int = 0,
) -> dict[str, Any]:
    """重新生成音色样音并持久化到音色库。

    Args:
        session: 数据库会话。
        voice_id: 音色 ID。
        text: 自定义试听文本（可空，空则使用默认示例句）。
        rate: 语速倍率（0.5–2.0）。
        pitch: 音调偏移（-12–12 半音）。

    Returns:
        新样音结果（与试听结构一致，``persisted`` 为 True）。

    Raises:
        BizError: 音色不存在、不可用、文本非法或模型合成失败。
    """
    return await synthesize_preview(
        session,
        voice_id,
        text=text,
        rate=rate,
        pitch=pitch,
        persist=True,
    )

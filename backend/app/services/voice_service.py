"""F9 配音轨服务：逐句配音、对齐降级、时长报告、关口 V 与漂移分级。

设计目标（PRD V6.4 F9 / F4.13）：
- **逐句生成**：每句独立 TTS，失败只影响该句，可单句重配；
- **对齐降级**：优先使用 TTS 时间戳；沙箱或模型无时间戳时按字符比例生成
  词级时间戳并标记 ``align_degraded=True``，不阻塞流程；
- **音频驱动时长**：把句级实测时长写回 ``script_lines.actual_ms``，并联动
  重算绑定镜头的时长（见 :mod:`app.services.line_service`）；
- **漂移分级**：重配后按 120ms / 10% 阈值给出 absorb / recalc / regenerate；
- **关口 V**：整集全部完成且无失败才允许审批通过，通过后轨道锁定。

沙箱模式下不发起真实三方调用，生成确定性的占位音频对象键与词级时间戳，
保证演示与自动化测试可重复。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import async_session_factory
from app.core.errors import BizError, ErrorCode
from app.infra.model_gateway import model_gateway as gateway
from app.infra.model_gateway.base import GenRequest
from app.infra.model_gateway.stub import _build_wav_bytes
from app.infra.storage import storage
from app.models import MediaFile
from app.models.asset import Asset, TtsVoice
from app.models.project import Project
from app.models.script import Episode, Scene, ScriptLine
from app.models.v6 import VoiceClip, VoiceDriftRecord, VoiceParam, VoiceTrack
from app.services import billing_service

logger = logging.getLogger("gleamdream.services.voice")

#: 中文语速基准：每秒字符数（rate=1.0 时）
_CHARS_PER_SECOND = 4.5
#: 句首固定开销（吸气/起音）
_HEAD_MS = 180
#: 漂移吸收阈值（毫秒）
_ABSORB_MS = 120
#: 漂移重算阈值（毫秒）
_RECALC_MS = 500
#: 漂移重算百分比阈值
_RECALC_PCT = 10.0
#: TTS 计费单价（积分/秒），与 price_rules.tts（unit=per_second, price=2）对齐
_TTS_CREDITS_PER_SECOND: int = 2
#: 单句冻结预估秒数（宁多冻结后解冻，避免实际超冻结导致结算失败）
_TTS_ESTIMATE_SECONDS_PER_LINE: int = 10

#: 平台内置音色键 → 真实 TTS 引擎音色名（P0-5 修复）
#: 内置音色库的 voice_key 是业务标识（如 female-calm-03），并非 Qwen3-TTS 的
#: 音色名；此前配音生成未把音色传给引擎（voice=None），所有角色都落到默认
#: 音色。这里给出确定性映射，未知音色再按性别兜底。
_PROVIDER_VOICE_ALIASES: dict[str, str] = {
    # 说明：这是全项目唯一的「业务音色键 → 引擎音色名」映射。试听、生成、
    # 单句重配都必须经由本模块 ``resolve_voice_profile`` 读取，禁止再维护
    # 第二套映射（历史上 tts_voice_service 另有一套同类映射，导致试听与
    # 成片音色不一致）。
    "female-warm-01": "Serena",
    "female-sweet-02": "Cherry",
    "female-calm-03": "Chelsie",
    "female-lively-04": "Jada",
    "female-romantic-05": "Sunny",
    "male-deep-01": "Ethan",
    "male-warm-02": "Dylan",
    "male-energetic-03": "Ethan",
    "male-magnetic-04": "Dylan",
    "neutral-narrator-01": "Sunny",
}
#: 已具备真实引擎音色映射、可用于角色绑定/试听的业务音色键集合。
#: 音色库中其余 voice_key 未映射到任何引擎音色，绑定后只能按性别兜底，
#: 会造成“不同音色听起来一样”，因此在音色列表接口中予以过滤。
SUPPORTED_VOICE_KEYS: tuple[str, ...] = tuple(_PROVIDER_VOICE_ALIASES.keys())

#: 性别 → 引擎音色兜底
_GENDER_DEFAULT_VOICE: dict[str, str] = {
    "female": "Cherry",
    "male": "Ethan",
    "neutral": "Chelsie",
}
#: 引擎音色 → 沙箱占位音频基频（Hz），用于在演示环境区分不同角色音色
_VOICE_BASE_FREQ: dict[str, float] = {
    "Cherry": 523.25,
    "Serena": 440.00,
    "Sunny": 587.33,
    "Jada": 659.25,
    "Chelsie": 392.00,
    "Ethan": 196.00,
    "Dylan": 164.81,
}
#: 情绪 → (语速倍率, 音调半音)，沙箱占位音频与生产后处理保持同一口径
_EMOTION_STYLE: dict[str, tuple[float, int]] = {
    "joy": (1.08, 1),
    "romance": (0.96, 1),
    "warm": (0.96, 0),
    "calm": (0.94, 0),
    "tension": (1.06, 1),
    "fear": (1.08, 2),
    "anger": (1.10, 2),
    "surprise": (1.10, 2),
    "sadness": (0.90, -1),
    "sad": (0.90, -1),
}
#: 已完成/归档项目禁止再做配音写入（P0-6）
_VOICE_LOCKED_STATUSES: tuple[str, ...] = ("completed", "archived")


def estimate_ms(text: str, rate: float = 1.0) -> int:
    """估算一句台词的朗读时长。

    Args:
        text: 台词文本（去除首尾空白）。
        rate: 语速倍率（0.5~2.0）。

    Returns:
        估算时长（毫秒，最小 200ms）。
    """
    clean = (text or "").strip()
    if not clean:
        return 200
    chars = len(clean)
    duration = _HEAD_MS + chars / _CHARS_PER_SECOND * 1000
    return max(200, int(duration / max(rate, 0.1)))


def _params_hash(voice_id: int | None, rate: float, pitch: int, emotion: str | None) -> str:
    """计算配音参数哈希（缓存键组成部分）。

    Args:
        voice_id: 音色 ID。
        rate: 语速倍率。
        pitch: 音调偏移。
        emotion: 默认情绪。

    Returns:
        64 位十六进制 SHA-256。
    """
    raw = f"{voice_id or 0}|{rate:.2f}|{pitch}|{emotion or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _age_floor(meta: dict | None) -> int | None:
    """从角色资产元数据中解析年龄区间下界。

    Args:
        meta: 资产 meta（可含 ``age_range``，如 ``"52-58"`` 或 ``55``）。

    Returns:
        年龄下界整数；缺失或无法解析时返回 None。
    """
    raw = (meta or {}).get("age_range") if isinstance(meta, dict) else None
    if isinstance(raw, (int, float)):
        return int(raw)
    match = re.search(r"\d{2,3}", str(raw or ""))
    return int(match.group(0)) if match else None


async def resolve_voice_profile(
    session: AsyncSession, voice_id: int | None
) -> tuple[str | None, float]:
    """把平台音色解析为真实引擎音色名与沙箱基频（P0-5 修复）。

    Args:
        session: 数据库会话。
        voice_id: 平台音色 ID（可空）。

    Returns:
        ``(引擎音色名, 沙箱基频Hz)``；音色缺失时返回 ``(None, 440.0)``。

    Raises:
        无：查询失败按音色缺失处理，不阻断配音。
    """
    if not voice_id:
        return None, 440.0
    try:
        voice = await session.get(TtsVoice, int(voice_id))
    except Exception:  # noqa: BLE001 音色查询失败不影响主流程
        return None, 440.0
    if voice is None:
        return None, 440.0
    key = str(voice.voice_key or "").strip()
    name = _PROVIDER_VOICE_ALIASES.get(key)
    if name is None:
        # 已经是引擎音色名（字母开头且无连字符）时直接使用
        if key and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            name = key
        else:
            name = _GENDER_DEFAULT_VOICE.get(str(voice.gender or "").lower(), "Cherry")
    return name, _VOICE_BASE_FREQ.get(name, 440.0)


async def _assert_voice_editable(
    session: AsyncSession, project_id: int, action: str = "配音"
) -> None:
    """校验项目状态是否允许配音写入（P0-6 修复）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        action: 操作名称（用于错误提示）。

    Returns:
        None。

    Raises:
        BizError: 10002 项目不存在；10004 项目已完成/归档，配音已锁定。
    """
    project = await session.get(Project, int(project_id))
    if project is None:
        raise BizError(ErrorCode.NOT_FOUND, "项目不存在")
    if str(project.status) in _VOICE_LOCKED_STATUSES:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"项目已{'完成' if project.status == 'completed' else '归档'}，{action}已锁定",
        )


def _text_hash(text: str) -> str:
    """计算台词文本哈希。

    Args:
        text: 台词文本。

    Returns:
        64 位十六进制 SHA-256。
    """
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _build_words(text: str, duration_ms: int) -> list[dict[str, Any]]:
    """按标点与长度切分，生成比例分配的伪词级时间戳。

    Args:
        text: 台词文本。
        duration_ms: 音频总时长（毫秒）。

    Returns:
        词级时间戳数组 ``[{w, start_ms, end_ms}]``。
    """
    chunks = [c for c in re.split(r"(?<=[，。！？；、,.!?;])|\s+", text) if c]
    if not chunks:
        chunks = [text or ""]
    total_chars = sum(len(c) for c in chunks) or 1
    cursor = 0
    words: list[dict[str, Any]] = []
    for chunk in chunks:
        span = max(1, int(duration_ms * len(chunk) / total_chars))
        words.append({"w": chunk, "start_ms": cursor, "end_ms": min(duration_ms, cursor + span)})
        cursor += span
    return words


async def _project_lines(session: AsyncSession, project_id: int, ep_no: int) -> list[ScriptLine]:
    """加载某集全部有效台词（按场次/行号排序）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        台词 ORM 列表（过滤软删除与动作/舞台提示行）。
    """
    stmt = (
        select(ScriptLine)
        .join(Scene, Scene.id == ScriptLine.scene_id)
        .join(Episode, Episode.id == Scene.episode_id)
        .where(
            ScriptLine.project_id == project_id,
            Episode.episode_no == ep_no,
            ScriptLine.is_deleted.is_(False),
            ScriptLine.line_type.in_(("dialogue", "narration")),
        )
        .order_by(Scene.scene_no, ScriptLine.order_index)
    )
    # P0 修复（双稿并存）：剧本 reject→重新生成会创建新 Script + 同 episode_no
    # 的新 Episode（旧版本保留作历史），同一集会出现两套 scene_no 相同的台词。
    # 台词查询必须锚定项目当前生效剧本（current_script_id），否则旧稿台词会
    # 混入配音与统计（配音句数翻倍、成片被音频驱动重排拉长近一倍）。
    current_script_id = (
        await session.execute(
            select(Project.current_script_id).where(Project.id == project_id)
        )
    ).scalar_one_or_none()
    if current_script_id is not None:
        stmt = stmt.where(Episode.script_id == int(current_script_id))
    return list((await session.execute(stmt)).scalars().all())


async def ensure_line_keys(session: AsyncSession, project_id: int) -> None:
    """补齐台词稳定键与预估时长（幂等）。

    历史台词没有 ``line_key`` 时使用 ``ln_<id>`` 生成，保证跨剧本版本稳定；
    ``est_ms`` 为空时按当前语速基准估算。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。

    Returns:
        None
    """
    rows = (
        await session.execute(select(ScriptLine).where(ScriptLine.project_id == project_id))
    ).scalars().all()
    changed = False
    for line in rows:
        if not line.line_key:
            line.line_key = f"ln_{line.id:010d}"
            changed = True
        if not line.est_ms:
            line.est_ms = estimate_ms(line.content)
            changed = True
    if changed:
        await session.commit()


async def get_or_create_track(session: AsyncSession, project_id: int, ep_no: int) -> VoiceTrack:
    """获取或创建集级配音轨。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        配音轨 ORM 实例。

    Raises:
        BizError: 10002 集不存在。
    """
    episode = (
        await session.execute(
            select(Episode).where(
                Episode.project_id == project_id, Episode.episode_no == ep_no
            )
        )
    ).scalars().first()
    if episode is None:
        raise BizError(ErrorCode.NOT_FOUND, "集不存在")

    track = (
        await session.execute(
            select(VoiceTrack).where(
                VoiceTrack.project_id == project_id, VoiceTrack.ep_no == ep_no
            )
        )
    ).scalars().first()
    if track is not None:
        return track

    lines = await _project_lines(session, project_id, ep_no)
    track = VoiceTrack(
        project_id=project_id,
        ep_no=ep_no,
        status="not_started",
        line_count=len(lines),
        gap_ms=120,
        report={"episode": ep_no, "generated": False},
    )
    session.add(track)
    await session.flush()
    return track


async def track_detail(session: AsyncSession, project_id: int, ep_no: int) -> dict[str, Any]:
    """查询配音轨详情（含句级音频与汇总报告）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        含 ``track`` / ``clips`` / ``report`` / ``characters`` 的字典。

    Raises:
        BizError: 10002 集不存在。
    """
    track = await get_or_create_track(session, project_id, ep_no)
    clips = (
        await session.execute(
            select(VoiceClip)
            .where(VoiceClip.track_id == track.id)
            .order_by(VoiceClip.id)
        )
    ).scalars().all()
    lines = await _project_lines(session, project_id, ep_no)
    line_map = {line.id: line for line in lines}

    clip_items: list[dict[str, Any]] = []
    for clip in clips:
        line = line_map.get(clip.line_id)
        clip_items.append(
            {
                "id": clip.id,
                "line_id": clip.line_id,
                "line_key": clip.line_key,
                "text": line.content if line else "",
                "speaker_name": line.speaker_name if line else "",
                "character_asset_id": clip.character_asset_id,
                "voice_id": clip.voice_id,
                # P1 修复：返回实际使用的情绪（旁白/对白均可见），供页面评审
                "emotion": clip.emotion or (line.emotion_tag if line else None),
                "status": clip.status,
                "source": clip.source,
                "duration_ms": clip.duration_ms,
                "audio_url": clip.audio_url,
                "alignment_source": clip.alignment_source,
                "align_degraded": clip.align_degraded,
                "words": clip.words,
                "error_message": clip.error_message,
                "retry_count": clip.retry_count,
            }
        )

    unvoiced = [line for line in lines if line.id not in {c.line_id for c in clips}]
    for line in unvoiced:
        clip_items.append(
            {
                "id": None,
                "line_id": line.id,
                "line_key": line.line_key,
                "text": line.content,
                "speaker_name": line.speaker_name,
                "character_asset_id": line.character_asset_id,
                "voice_id": None,
                "emotion": line.emotion_tag,
                "status": "pending",
                "source": "ai",
                "duration_ms": line.est_ms,
                "audio_url": None,
                "alignment_source": "none",
                "align_degraded": False,
                "words": [],
                "error_message": None,
                "retry_count": 0,
            }
        )

    report = await build_report(session, project_id, ep_no)
    return {
        "track": _track_dict(track),
        "clips": clip_items,
        "report": report,
        "missing_voice_characters": report["missing_voice_characters"],
    }


def _track_dict(track: VoiceTrack) -> dict[str, Any]:
    """序列化配音轨（供 API 输出）。

    Args:
        track: 配音轨 ORM 实例。

    Returns:
        可 JSON 化的字典。
    """
    return {
        "id": track.id,
        "ep_no": track.ep_no,
        "status": track.status,
        "total_ms": track.total_ms,
        "speech_ms": track.speech_ms,
        "silence_ms": track.silence_ms,
        "gap_ms": track.gap_ms,
        "line_count": track.line_count,
        "voiced_count": track.voiced_count,
        "failed_count": track.failed_count,
        "gate_status": track.gate_status,
        "align_degraded": track.align_degraded,
        "approved_at": track.approved_at.isoformat() if track.approved_at else None,
        "locked_at": track.locked_at.isoformat() if track.locked_at else None,
    }


async def build_report(session: AsyncSession, project_id: int, ep_no: int) -> dict[str, Any]:
    """构建整集配音时长报告（不落库）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        含总时长、语音/静默时长、逐场统计、缺失音色角色与对齐降级的字典。
    """
    track = (
        await session.execute(
            select(VoiceTrack).where(
                VoiceTrack.project_id == project_id, VoiceTrack.ep_no == ep_no
            )
        )
    ).scalars().first()
    lines = await _project_lines(session, project_id, ep_no)
    clips: list[VoiceClip] = []
    if track is not None:
        clips = list(
            (
                await session.execute(
                    select(VoiceClip).where(VoiceClip.track_id == track.id)
                )
            ).scalars().all()
        )
    clip_map = {clip.line_id: clip for clip in clips}
    gap_ms = track.gap_ms if track else 120

    total = 0
    scene_stats: dict[str, dict[str, int]] = {}
    for line in lines:
        clip = clip_map.get(line.id)
        duration = clip.duration_ms if clip and clip.duration_ms else line.est_ms
        total += duration + gap_ms
        scene_key = str(line.scene_id)
        bucket = scene_stats.setdefault(
            scene_key, {"line_count": 0, "duration_ms": 0, "voiced_count": 0}
        )
        bucket["line_count"] += 1
        bucket["duration_ms"] += duration + gap_ms
        if clip is not None and clip.status == "voiced":
            bucket["voiced_count"] += 1

    character_ids = sorted({line.character_asset_id for line in lines if line.character_asset_id})
    missing_voice: list[dict[str, Any]] = []
    if character_ids:
        assets = (
            await session.execute(select(Asset).where(Asset.id.in_(character_ids)))
        ).scalars().all()
        for asset in assets:
            if asset.voice_id is None:
                missing_voice.append({"id": asset.id, "name": asset.name})

    speech_ms = sum(
        (c.duration_ms for c in clips if c.status == "voiced" and c.duration_ms), 0
    )
    return {
        "episode": ep_no,
        "line_count": len(lines),
        "voiced_count": sum(1 for c in clips if c.status == "voiced"),
        "failed_count": sum(1 for c in clips if c.status == "failed"),
        "speech_ms": speech_ms,
        "gap_ms": gap_ms,
        "total_ms": total,
        "estimated_total_ms": sum(line.est_ms for line in lines) + gap_ms * len(lines),
        "scene_stats": scene_stats,
        "missing_voice_characters": missing_voice,
        "align_degraded": any(c.align_degraded for c in clips),
    }


async def generate_sample(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    character_asset_id: int,
    text: str | None,
    rate: float,
    pitch: int,
) -> dict[str, Any]:
    """生成角色样音（30 秒内），返回可试听地址与时长。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        character_asset_id: 角色资产 ID。
        text: 试听文本（空则取该角色首句台词）。
        rate: 语速倍率。
        pitch: 音调偏移。

    Returns:
        含 ``audio_url`` / ``duration_ms`` / ``text`` 的字典。

    Raises:
        BizError: 10002 角色资产不存在；10004 项目已完成/归档（配音锁定）。
    """
    # P0-6 修复：项目完成后不再允许生成样音/改动配音
    await _assert_voice_editable(session, project_id, "样音试听")

    asset = await session.get(Asset, character_asset_id)
    if asset is None or asset.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "角色资产不存在")

    sample_text = (text or "").strip()
    if not sample_text:
        first_line = (
            await session.execute(
                select(ScriptLine)
                .where(
                    ScriptLine.project_id == project_id,
                    ScriptLine.character_asset_id == character_asset_id,
                    ScriptLine.is_deleted.is_(False),
                )
                .order_by(ScriptLine.id)
                .limit(1)
            )
        ).scalars().first()
        sample_text = first_line.content if first_line else f"你好，我是{asset.name}。"
    sample_text = sample_text[:120]

    voice_name, voice_freq = await resolve_voice_profile(session, asset.voice_id)
    result = await _synthesize(
        project_id=project_id,
        text=sample_text,
        voice_id=asset.voice_id,
        rate=rate,
        pitch=pitch,
        emotion=None,
        voice_name=voice_name,
        voice_freq=voice_freq,
    )
    await session.commit()
    return {
        "character_asset_id": character_asset_id,
        "character_name": asset.name,
        "text": sample_text,
        "audio_url": result["audio_url"],
        "audio_key": result["audio_key"],
        "duration_ms": result["duration_ms"],
        "sandbox": result["sandbox"],
        "voice": result.get("voice"),
        "degraded": bool(result.get("degraded")),
        "error": result.get("error"),
        "requested_by": user_id,
    }


async def _settle_voice_cost(
    *,
    project_id: int,
    owner_user_id: int | None,
    ep_no: int,
    track_id: int,
    line_count: int,
    actual_ms: int,
    idempotency_suffix: str = "",
) -> dict[str, Any]:
    """按实际配音总秒数结算 TTS 成本（P1-12 修复：真实配音成本必须落账）。

    历史实现只把 ``voice_clips.billed`` 置为 True，积分账户余额从未扣减，
    项目消耗统计与真实 TTS 成本脱节。这里在配音主数据提交后，用独立短会话
    走「冻结 → 结算」链路：预估按句数 × 10s 冻结，实际按音频秒数 × 2 积分
    结算，差额自动解冻；幂等键保证同一批内容重复提交不重复扣费。

    计费与配音主数据解耦：任何计费异常只记日志，不回滚已生成的配音。

    Args:
        project_id: 项目 ID。
        owner_user_id: 项目所有者用户 ID（空则不结算）。
        ep_no: 集号。
        track_id: 配音轨 ID。
        line_count: 本次真实（非沙箱、非降级）生成成功的句数。
        actual_ms: 本次真实生成音频的总时长（毫秒）。
        idempotency_suffix: 幂等键附加后缀（单句重配用 clip_id + 轮次）。

    Returns:
        ``{status, actual_credits?, freeze_id?, reason?}``：skipped / settled / failed。

    Raises:
        无：计费失败不阻断配音结果返回。
    """
    if line_count <= 0 or actual_ms <= 0:
        return {"status": "skipped", "reason": "no_real_audio"}
    if not owner_user_id:
        return {"status": "skipped", "reason": "no_owner"}
    estimated = max(
        1, int(line_count) * _TTS_ESTIMATE_SECONDS_PER_LINE * _TTS_CREDITS_PER_SECOND
    )
    actual = max(1, math.ceil(int(actual_ms) / 1000) * _TTS_CREDITS_PER_SECOND)
    actual = min(actual, estimated)
    suffix = f":{idempotency_suffix}" if idempotency_suffix else ""
    idempotency_key = (
        f"voice-track:{int(project_id)}:{int(ep_no)}:{int(track_id)}:"
        f"{int(line_count)}:{int(actual_ms)}{suffix}"
    )
    try:
        async with async_session_factory() as pay_session:
            account = await billing_service.BillingRepository(pay_session).get_account_by_user(
                int(owner_user_id)
            )
            if account is None:
                return {"status": "skipped", "reason": "no_account"}
            freeze = await billing_service.precheck_and_freeze(
                pay_session,
                account_id=int(account.id),
                project_id=int(project_id),
                task_type="tts",
                estimated_credits=estimated,
                idempotency_key=idempotency_key,
            )
            if freeze.status != "frozen":
                # 幂等命中已结算的历史冻结：本次内容此前已计费，不重复扣款
                return {
                    "status": "skipped",
                    "reason": "already_settled",
                    "freeze_id": int(freeze.id),
                }
            detail = await billing_service.settle(
                pay_session, freeze_id=int(freeze.id), actual_credits=actual
            )
    except BizError as exc:
        logger.warning(
            "TTS 结算失败 project_id=%s ep_no=%s track_id=%s code=%s message=%s",
            project_id,
            ep_no,
            track_id,
            exc.code,
            str(exc.message)[:200],
        )
        return {"status": "failed", "code": exc.code, "message": str(exc.message)[:200]}
    except Exception as exc:  # noqa: BLE001 计费异常不影响配音结果
        logger.warning(
            "TTS 结算异常 project_id=%s ep_no=%s track_id=%s：%s",
            project_id,
            ep_no,
            track_id,
            str(exc)[:200],
        )
        return {"status": "failed", "message": str(exc)[:200]}
    return {"status": "settled", "actual_credits": actual, **detail}


async def ensure_audio_media(
    session: AsyncSession,
    *,
    project_id: int,
    audio_key: str,
    audio_url: str,
    duration_ms: int,
) -> MediaFile | None:
    """确保配音文件在 ``media_files`` 中有记录（P0 修复）。

    历史问题：配音生成只把文件写入本地存储并更新 ``voice_clips``，
    未创建 ``media_files`` 记录；后期合成按音频 key 反查媒体时失败，
    整条成片音轨被跳过（audio=0）。本函数按 object_key 幂等补齐记录。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        audio_key: 音频存储键（如 ``qwen-tts/xxx.wav``）。
        audio_url: 浏览器可访问 URL。
        duration_ms: 音频时长（毫秒）。

    Returns:
        已存在或新建的 MediaFile；audio_key 为空时返回 None。

    Raises:
        无：文件大小读取失败时按 0 处理，不阻断配音主流程。
    """
    if not audio_key:
        return None
    existing = (
        await session.execute(
            select(MediaFile).where(MediaFile.object_key == audio_key).limit(1)
        )
    ).scalars().first()
    if existing is not None:
        if existing.project_id is None:
            existing.project_id = project_id
        if not existing.duration_ms:
            existing.duration_ms = max(0, int(duration_ms))
        return existing
    file_size = 0
    try:
        file_size = int(storage.object_path(audio_key).stat().st_size)
    except OSError:
        # 文件尚未落盘时保持 0，静态文件服务仍可在生成完成后试听
        file_size = 0
    media = MediaFile(
        project_id=project_id,
        # P0 修复：media_files 的 ck_media_files_storage_provider 约束只允许
        # cos/oss；本地存储在本项目统一登记为 cos（与导出、分镜媒体一致），
        # 否则真实 TTS 落库会触发 CheckViolationError（配音接口 500）。
        storage_provider="cos",
        bucket="gleamdream-local",
        object_key=audio_key,
        url=audio_url,
        media_type="audio",
        mime_type="audio/wav",
        file_size_bytes=file_size,
        duration_ms=max(0, int(duration_ms)),
        meta={"kind": "voice"},
    )
    session.add(media)
    return media


#: 可安全重试的 TTS 失败特征（仅连接/超时类，最多重试一次）。
#: 业务失败（密钥无效、音色非法、4xx/5xx）不重试，避免重复扣费与死循环。
_RETRYABLE_TTS_HINTS: tuple[str, ...] = (
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "TimeoutException",
    "NetworkError",
    "RemoteProtocolError",
    "ConnectionError",
)


def _is_retryable_tts_error(message: str | None) -> bool:
    """判断 TTS 失败是否属于可安全重试的网络类异常。

    Args:
        message: 模型网关返回的错误文本。

    Returns:
        ``True`` 表示命中了连接/超时类异常，可重试一次；否则 ``False``。
    """
    text = str(message or "")
    return any(hint in text for hint in _RETRYABLE_TTS_HINTS)


async def _synthesize(
    project_id: int,
    text: str,
    voice_id: int | None,
    rate: float,
    pitch: int,
    emotion: str | None,
    *,
    voice_name: str | None = None,
    voice_freq: float = 440.0,
) -> dict[str, Any]:
    """合成一句音频（沙箱走确定性占位，生产走模型网关）。

    Args:
        project_id: 项目 ID（日志关联）。
        text: 台词文本。
        voice_id: 音色 ID。
        rate: 语速倍率。
        pitch: 音调偏移。
        emotion: 情绪标签。
        voice_name: 真实引擎音色名（P0-5：必须透传给 TTS，否则角色音色一致）。
        voice_freq: 沙箱占位音频基频（按音色区分，保证可听出差异）。

    Returns:
        ``{audio_key, audio_url, duration_ms, sandbox, degraded, error, words,
        alignment_source, voice, emotion_applied}``。

    Raises:
        无：网关失败时回退为占位音频，但会标记 ``degraded`` 与失败原因，
        由调用方把该句置为失败（可单句重配），避免伪装成真实配音成功。
    """
    last_error: str | None = None
    if not settings.SANDBOX_MODE or settings.TTS_ALLOW_REAL:
        params: dict[str, Any] = {"rate": rate, "pitch": pitch, "emotion": emotion}
        if voice_name:
            params["voice"] = voice_name
        # 仅网络类异常重试一次：真实 TTS 可能失败后已产生费用，无限重试会
        # 造成重复扣费；业务失败（密钥/参数/4xx）直接进入失败分支。
        for attempt in range(2):
            try:
                result = await gateway.generate(
                    capability="tts",
                    task_type="tts",
                    request=GenRequest(
                        capability="tts",
                        prompt=text,
                        params=params,
                    ),
                    project_id=project_id,
                )
                if result.ok and result.object_key:
                    duration = int(result.meta.get("duration_ms") or estimate_ms(text, rate))
                    return {
                        "audio_key": result.object_key,
                        "audio_url": result.media_url,
                        "duration_ms": duration,
                        "sandbox": bool(result.meta.get("simulated")),
                        "words": _build_words(text, duration),
                        "alignment_source": "tts_timestamp",
                        "voice": result.meta.get("voice") or voice_name,
                        "emotion_applied": bool(result.meta.get("emotion_applied")),
                        "degraded": False,
                        "error": None,
                    }
                last_error = str(result.error or "TTS 供应商返回空产物")[:300]
            except BizError as exc:
                last_error = str(exc)[:300]
            except Exception as exc:  # noqa: BLE001 - 外部依赖失败时降级为占位音频
                last_error = f"{type(exc).__name__}: {str(exc)[:280]}"
            if attempt == 0 and _is_retryable_tts_error(last_error):
                logger.warning(
                    "TTS 网络异常，重试一次 project_id=%s error=%s",
                    project_id,
                    last_error,
                )
                await asyncio.sleep(0.3)
                continue
            break

    # 沙箱占位：情绪映射到语速/音高，音色映射到基频，保证不同角色/情绪可区分
    emotion_key = str(emotion or "").strip().lower()
    emotion_rate, emotion_pitch = _EMOTION_STYLE.get(emotion_key, (1.0, 0))
    effective_rate = max(0.5, min(2.0, float(rate) * emotion_rate))
    duration = estimate_ms(text, effective_rate)
    freq = max(80.0, float(voice_freq) * (2 ** ((int(pitch) + emotion_pitch) / 12)))
    digest = hashlib.sha256(
        f"{text}|{voice_id}|{voice_name}|{rate}|{pitch}|{emotion_key}".encode("utf-8")
    ).hexdigest()[:16]
    key = f"voice/stub/{digest}.wav"
    # P0-4 修复：占位音频必须真实落盘。
    # 历史上这里只返回 key 而不写文件，导致前端请求 /files/voice/stub/*.wav 恒为
    # 404、网页无法试听。现按整秒生成可播放的 WAV 并写入本地存储（同一摘要幂等）。
    # 时长精度取 0.1s，保留语速/情绪对时长的细微影响（整秒会抹平差异）
    seconds = max(0.5, round(duration / 100) / 10)
    actual_ms = int(round(seconds * 1000))
    storage.save_bytes(key, _build_wav_bytes(seconds, freq))
    return {
        "audio_key": key,
        "audio_url": f"/files/{key}",
        "duration_ms": actual_ms,
        "sandbox": True,
        "words": _build_words(text, actual_ms),
        "alignment_source": "tts_timestamp",
        "voice": voice_name,
        "emotion_applied": emotion_key in _EMOTION_STYLE,
        "degraded": bool(last_error),
        "error": last_error,
    }


async def confirm_params(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    character_asset_id: int,
    voice_id: int | None,
    rate: float,
    pitch: int,
    emotion: str | None,
    sample_audio_key: str | None,
    sample_duration_ms: int | None,
) -> VoiceParam:
    """确认并锁定角色配音参数（旧参数置为 superseded）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        character_asset_id: 角色资产 ID。
        voice_id: 音色 ID。
        rate: 语速倍率。
        pitch: 音调偏移。
        emotion: 默认情绪。
        sample_audio_key: 样音对象键。
        sample_duration_ms: 样音时长。

    Returns:
        新建的配音参数快照。

    Raises:
        BizError: 10002 角色资产不存在；10001 音色不可用；
        10004 项目已完成/归档（配音参数已锁定）。
    """
    # P0-6 修复：项目完成后不允许再改动/锁定配音参数
    await _assert_voice_editable(session, project_id, "配音参数")

    asset = await session.get(Asset, character_asset_id)
    if asset is None or asset.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "角色资产不存在")
    if voice_id is not None:
        voice = await session.get(TtsVoice, voice_id)
        if voice is None or voice.status != "available":
            raise BizError(ErrorCode.PARAM_INVALID, "音色不存在或已下架")

    old_params = (
        await session.execute(
            select(VoiceParam).where(
                VoiceParam.project_id == project_id,
                VoiceParam.character_asset_id == character_asset_id,
                VoiceParam.status == "active",
            )
        )
    ).scalars().all()
    for row in old_params:
        row.status = "superseded"

    params_hash = _params_hash(voice_id, rate, pitch, emotion)
    record = VoiceParam(
        project_id=project_id,
        character_asset_id=character_asset_id,
        voice_id=voice_id,
        rate=rate,
        pitch=pitch,
        emotion_default=emotion,
        params_hash=params_hash,
        status="active",
        sample_audio_key=sample_audio_key,
        sample_duration_ms=sample_duration_ms,
        confirmed_by=int(user_id) if str(user_id).isdigit() else None,
        confirmed_at=datetime.now(UTC),
    )
    session.add(record)
    asset.voice_id = voice_id
    asset.voice_status = "bound" if voice_id else "pending"
    await session.commit()
    return {
        "id": record.id,
        "character_asset_id": record.character_asset_id,
        "voice_id": record.voice_id,
        "rate": float(record.rate),
        "pitch": record.pitch,
        "emotion_default": record.emotion_default,
        "sample_audio_key": record.sample_audio_key,
        "sample_duration_ms": record.sample_duration_ms,
        "status": record.status,
    }


async def active_params(
    session: AsyncSession, project_id: int, character_asset_id: int
) -> VoiceParam | None:
    """查询角色当前生效的配音参数。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        character_asset_id: 角色资产 ID。

    Returns:
        生效参数或 None。
    """
    return (
        await session.execute(
            select(VoiceParam)
            .where(
                VoiceParam.project_id == project_id,
                VoiceParam.character_asset_id == character_asset_id,
                VoiceParam.status == "active",
            )
            .order_by(VoiceParam.id.desc())
            .limit(1)
        )
    ).scalars().first()


async def generate_track(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    ep_no: int,
    line_ids: list[int],
    force: bool,
) -> dict[str, Any]:
    """逐句生成整集（或指定）配音，并同步台词实测时长。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        ep_no: 集号。
        line_ids: 指定台词 ID（空表示整集）。
        force: 是否强制重配。

    Returns:
        含生成/跳过/失败统计与轨道状态的字典。

    Raises:
        BizError: 10002 集不存在；10001 轨道已锁定。
    """
    # P0-6 修复：项目完成后配音写入必须被拒绝（此前仍可整集重配/改动）
    await _assert_voice_editable(session, project_id, "配音生成")
    project = await session.get(Project, project_id)
    owner_user_id = int(project.owner_user_id) if project and project.owner_user_id else None

    track = await get_or_create_track(session, project_id, ep_no)
    if track.status == "voice_locked":
        raise BizError(ErrorCode.PARAM_INVALID, "配音轨已锁定，请先解锁")

    await ensure_line_keys(session, project_id)
    lines = await _project_lines(session, project_id, ep_no)
    if line_ids:
        wanted = set(line_ids)
        lines = [line for line in lines if line.id in wanted]
    if not lines:
        raise BizError(ErrorCode.PARAM_INVALID, "没有可配音的台词")

    # P0 修复：台词说话人 → 角色资产自动回填。此前解析结果 character_asset_id
    # 全为空，整集配音只能落到默认音色，两个角色听起来完全一样。
    # 状态过滤放宽为 approved/locked：approved 角色已通过形象审批且完成音色
    # 绑定，仅缺三视图不 lock 时（如快速验收/单集小样流程）同样必须参与
    # 说话人回填，否则整集配音全部落到默认旁白音色（同腔同调）。
    if any(line.speaker_name and not line.character_asset_id for line in lines):
        character_assets = (
            await session.execute(
                select(Asset).where(
                    Asset.project_id == project_id,
                    Asset.asset_type == "character",
                    Asset.status.in_(("approved", "locked")),
                )
            )
        ).scalars().all()
        asset_by_speaker = {
            str(asset.name or "").strip(): asset for asset in character_assets
        }
        for line in lines:
            if line.speaker_name and not line.character_asset_id:
                matched = asset_by_speaker.get(str(line.speaker_name).strip())
                if matched is not None:
                    line.character_asset_id = int(matched.id)

    existing = {
        clip.line_id: clip
        for clip in (
            await session.execute(select(VoiceClip).where(VoiceClip.track_id == track.id))
        ).scalars().all()
    }
    # P0 修复（双稿并存残留）：整集模式下，清理不属于当前生效剧本的旧配音
    # 片段（剧本重新生成后旧稿行保留历史，但其 VoiceClip 仍挂在轨道上，
    # 会虚增 voiced 统计并被 _sync_voice_track 捞进时间线）。单句重配模式
    # （line_ids 非空）不动其他句。
    if not line_ids:
        current_line_ids = {line.id for line in lines}
        for stale_id, stale_clip in list(existing.items()):
            if stale_id not in current_line_ids:
                await session.delete(stale_clip)
                existing.pop(stale_id, None)

    generated = skipped = failed = 0
    # P0 修复（TTS 整集管线整体回滚）：session 为 autoflush=False，同一事务内
    # 若剧本存在重复台词文本（同一句台词在多个场景出现，且角色/情绪/参数一致，
    # stub 与部分真实引擎的 audio_key 由"文本+音色+参数"确定性决定），第二句的
    # ensure_audio_media SELECT 看不到第一句尚未 flush 的 pending INSERT，会再次
    # add 一份同 object_key 的 MediaFile；循环末尾 flush 时批量双插撞
    # uq_media_files_object_key 唯一约束 → IntegrityError → 整集配音事务回滚
    # （voice_clips/timeline 全丢，仅 storage 里的 wav 文件留存）。这里按
    # audio_key 缓存本次会话已登记的 MediaFile，同 key 直接复用同一实例，
    # 从数据流上杜绝同事务双插。
    media_by_key: dict[str, MediaFile] = {}
    # Q-015：角色未绑定音色时的默认音色缓存（按角色资产区分性别）。
    default_voice_cache: dict[int, int | None] = {}
    # P1-12：本次真实音频（非沙箱、非降级）的句数与总时长，用于结算 TTS 成本
    billed_lines = billed_ms = 0
    # P2-9：角色资产缓存（asset_id → (绑定音色, 年龄下界)）
    asset_voice_cache: dict[int, tuple[int | None, int | None]] = {}
    # P0-5：音色 ID → (引擎音色名, 沙箱基频)，避免逐句重复查询
    voice_profile_cache: dict[int, tuple[str | None, float]] = {}
    # P2 修复（旁白音色缺失）：旁白没有角色资产，此前 voice_id 为 NULL，
    # 引擎回退自身默认音色，与女主角撞音；这里显式解析内置旁白音色。
    narrator_voice = (
        await session.execute(
            select(TtsVoice).where(TtsVoice.voice_key == "neutral-narrator-01")
        )
    ).scalars().first()
    narrator_voice_id = int(narrator_voice.id) if narrator_voice else None
    for line in lines:
        clip = existing.get(line.id)
        if clip and clip.status == "voiced" and not force:
            skipped += 1
            continue

        params = (
            await active_params(session, project_id, line.character_asset_id)
            if line.character_asset_id
            else None
        )
        asset_pk = int(line.character_asset_id) if line.character_asset_id else None
        asset_voice, asset_age = None, None
        if asset_pk is not None:
            if asset_pk not in asset_voice_cache:
                asset = await session.get(Asset, asset_pk)
                asset_voice_cache[asset_pk] = (
                    int(asset.voice_id) if asset is not None and asset.voice_id else None,
                    _age_floor(asset.meta if asset is not None else None),
                )
            asset_voice, asset_age = asset_voice_cache[asset_pk]
        # ---- P2-9 修复：参数表未显式指定音色时回退角色资产绑定音色 ----
        # 现象：整集 TTS 生成后 voice_clips.voice_id 全为 NULL，所有角色都用
        # 默认音色；原因是一条台词都没走「参数中心显式选音色」路径，而此处
        # 只读 VoiceParam.voice_id，忽略了资产上已绑定的音色。
        voice_id = (params.voice_id if params else None) or asset_voice
        if voice_id is None and asset_pk is None:
            voice_id = narrator_voice_id
        # Q-015：角色资产未显式绑定音色时，按性别自动选择可用的默认音色，
        # 避免整集所有角色 voice_id 为 NULL、全员同腔同调。
        if voice_id is None and asset_pk is not None:
            if asset_pk not in default_voice_cache:
                asset_row = await session.get(Asset, int(asset_pk))
                description = str(getattr(asset_row, "description", "") or "")
                gender = (
                    "female"
                    if "女性" in description
                    else ("male" if "男性" in description else None)
                )
                voice_stmt = select(TtsVoice).where(TtsVoice.status == "available")
                if gender:
                    voice_stmt = voice_stmt.where(TtsVoice.gender == gender)
                default_voice = (
                    await session.execute(
                        voice_stmt.order_by(TtsVoice.id.asc()).limit(1)
                    )
                ).scalars().first()
                default_voice_cache[asset_pk] = (
                    int(default_voice.id) if default_voice is not None else None
                )
            voice_id = default_voice_cache[asset_pk]
        rate = float(params.rate) if params else 1.0
        pitch = params.pitch if params else 0
        # 人设修复：老年角色未在参数中心显式调参时，按年龄下界降速降调，
        # 避免 52-58 岁的陈姨与年轻角色同腔同调
        if params is None and asset_age is not None and asset_age >= 50:
            rate = round(rate * 0.92, 2)
            pitch = pitch - 2
        # P1 修复（情绪丢失）：参数表可能只配了音色、未配默认情绪，
        # 直接取 params.emotion_default 会得到 None，导致整集配音丢失情绪；
        # 这里改为「参数显式情绪优先，否则回退剧本行情绪标签」。
        emotion = (params.emotion_default if params else None) or line.emotion_tag
        params_hash = _params_hash(voice_id, rate, pitch, emotion)

        # P0-5 修复：解析并透传真实音色，保证不同角色音色不同
        if voice_id is not None:
            if int(voice_id) not in voice_profile_cache:
                voice_profile_cache[int(voice_id)] = await resolve_voice_profile(
                    session, int(voice_id)
                )
            voice_name, voice_freq = voice_profile_cache[int(voice_id)]
        else:
            voice_name, voice_freq = None, 440.0

        result = await _synthesize(
            project_id=project_id,
            text=line.content,
            voice_id=voice_id,
            rate=rate,
            pitch=pitch,
            emotion=emotion,
            voice_name=voice_name,
            voice_freq=voice_freq,
        )
        # P1-12：真实供应商失败时不得伪装成配音成功，置 failed 支持单句重配
        degraded = bool(result.get("degraded"))
        if clip is None:
            clip = VoiceClip(
                project_id=project_id,
                track_id=track.id,
                line_id=line.id,
                line_key=line.line_key or f"ln_{line.id:010d}",
                text_hash=_text_hash(line.content),
                params_hash=params_hash,
                voice_id=voice_id,
                character_asset_id=line.character_asset_id,
                source="ai",
            )
            session.add(clip)
        clip.text_hash = _text_hash(line.content)
        clip.params_hash = params_hash
        clip.voice_id = voice_id
        clip.character_asset_id = line.character_asset_id
        clip.status = "failed" if degraded else "voiced"
        clip.audio_key = result["audio_key"]
        clip.audio_url = result["audio_url"]
        clip.duration_ms = result["duration_ms"]
        # P1 修复：持久化实际传入引擎的情绪标签，供页面展示与评审复核
        clip.emotion = emotion
        # P0 修复：同 audio_key 复用已登记的 MediaFile（见 media_by_key 注释），
        # 仅首次出现时调用 ensure_audio_media 补登记。
        audio_key = str(result["audio_key"] or "")
        if audio_key and audio_key not in media_by_key:
            media = await ensure_audio_media(
                session,
                project_id=project_id,
                audio_key=audio_key,
                audio_url=str(result["audio_url"]),
                duration_ms=int(result["duration_ms"] or 0),
            )
            if media is not None:
                media_by_key[audio_key] = media
        clip.words = result["words"]
        clip.alignment_source = result["alignment_source"]
        clip.align_degraded = result["sandbox"]
        clip.error_message = (
            result.get("error")
            if degraded
            else ("沙箱占位音频，非真实人声" if result.get("sandbox") else None)
        )
        clip.billed = not result["sandbox"]

        if degraded:
            failed += 1
        else:
            line.actual_ms = result["duration_ms"]
            line.voice_status = "voiced"
            generated += 1
            if not result["sandbox"]:
                billed_lines += 1
                billed_ms += int(result["duration_ms"])

    await session.flush()
    totals = await _refresh_track(session, track, ep_no)
    await session.commit()
    # P1-12：主数据提交后再结算，计费失败不回滚已生成的配音
    billing = await _settle_voice_cost(
        project_id=project_id,
        owner_user_id=owner_user_id,
        ep_no=ep_no,
        track_id=int(track.id),
        line_count=billed_lines,
        actual_ms=billed_ms,
    )
    return {
        "track": _track_dict(track),
        "generated": generated,
        "skipped": skipped,
        "failed": failed,
        "totals": totals,
        "requested_by": user_id,
        "billing": billing,
    }


async def _refresh_track(
    session: AsyncSession, track: VoiceTrack, ep_no: int
) -> dict[str, Any]:
    """重算并写回集级汇总（句数、总时长、状态、报告）。

    Args:
        session: 数据库会话。
        track: 配音轨 ORM 实例。
        ep_no: 集号。

    Returns:
        汇总字典。
    """
    clips = list(
        (
            await session.execute(select(VoiceClip).where(VoiceClip.track_id == track.id))
        ).scalars().all()
    )
    lines = list(
        (
            await session.execute(
                select(ScriptLine)
                .join(Scene, Scene.id == ScriptLine.scene_id)
                .join(Episode, Episode.id == Scene.episode_id)
                .join(Project, Project.id == track.project_id)
                .where(
                    ScriptLine.project_id == track.project_id,
                    Episode.episode_no == ep_no,
                    ScriptLine.is_deleted.is_(False),
                    ScriptLine.line_type.in_(("dialogue", "narration")),
                    # P0 修复（双稿并存）：与 _project_lines 同口径，只统计当前
                    # 生效剧本的台词，避免旧稿行混入集级汇总（line_count 虚高）；
                    # 未设置当前剧本的项目（current_script_id 为空）退回全量口径。
                    or_(
                        Episode.script_id == Project.current_script_id,
                        Project.current_script_id.is_(None),
                    )
                )
            )
        ).scalars().all()
    )
    voiced = [c for c in clips if c.status == "voiced"]
    failed = [c for c in clips if c.status == "failed"]
    track.line_count = len(lines)
    track.voiced_count = len(voiced)
    track.failed_count = len(failed)
    track.speech_ms = sum(c.duration_ms for c in voiced)
    track.total_ms = track.speech_ms + track.gap_ms * len(lines)
    track.silence_ms = max(0, track.total_ms - track.speech_ms)
    track.align_degraded = any(c.align_degraded for c in clips)

    if track.status != "voice_locked":
        if not clips:
            track.status = "not_started"
        elif failed:
            track.status = "voice_drift"
        elif len(voiced) == len(lines) and lines:
            track.status = "voice_ready"
        else:
            track.status = "previewing_done"

    track.report = await build_report(session, track.project_id, ep_no)
    track.report["generated"] = True
    return {
        "line_count": track.line_count,
        "voiced_count": track.voiced_count,
        "failed_count": track.failed_count,
        # P2-9：完成率统一由后端口径给出（对白/旁白为分母），避免前端各算一套
        "completion_rate": (
            round(track.voiced_count / track.line_count, 4) if track.line_count else 0.0
        ),
        "speech_ms": track.speech_ms,
        "total_ms": track.total_ms,
        "status": track.status,
    }


async def update_track(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    gap_ms: int | None,
    pipeline_mode: str | None,
    user_id: str,
) -> dict[str, Any]:
    """调整配音轨气口或项目制作流程模式。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        gap_ms: 句间气口。
        pipeline_mode: 制作流程模式。
        user_id: 操作用户 ID。

    Returns:
        更新后的轨道字典。

    Raises:
        BizError: 10001 轨道已锁定。
    """
    track = await get_or_create_track(session, project_id, ep_no)
    if track.status == "voice_locked":
        raise BizError(ErrorCode.PARAM_INVALID, "配音轨已锁定")
    if gap_ms is not None:
        track.gap_ms = gap_ms
    if pipeline_mode is not None:
        from app.models.project import Project

        project = await session.get(Project, project_id)
        if project is None:
            raise BizError(ErrorCode.NOT_FOUND, "项目不存在")
        project.pipeline_mode = pipeline_mode
    await session.flush()
    await _refresh_track(session, track, ep_no)
    await session.commit()
    return {"track": _track_dict(track), "updated_by": user_id}


def _drift_level(delta_ms: int, delta_pct: float) -> str:
    """按 F9.5 阈值判定漂移分级。

    Args:
        delta_ms: 时长差（绝对值）。
        delta_pct: 变化百分比（绝对值）。

    Returns:
        absorb / recalc / regenerate。
    """
    if delta_ms <= _ABSORB_MS:
        return "absorb"
    if delta_ms <= _RECALC_MS or delta_pct <= _RECALC_PCT:
        return "recalc"
    return "regenerate"


async def regenerate_clip(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    clip_id: int,
    rate: float | None,
    pitch: int | None,
    reason: str | None,
) -> dict[str, Any]:
    """单句重配并记录时长漂移分级。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        clip_id: 配音片段 ID。
        rate: 临时语速（空沿用角色参数）。
        pitch: 临时音调（空沿用角色参数）。
        reason: 重配原因。

    Returns:
        含片段、漂移记录与受影响镜头 ID 的字典。

    Raises:
        BizError: 10002 片段不存在；10001 轨道已锁定；
        10004 项目已完成/归档（配音已锁定）。
    """
    # P0-6 修复：项目完成后禁止单句重配
    await _assert_voice_editable(session, project_id, "单句重配")

    clip = await session.get(VoiceClip, clip_id)
    if clip is None or clip.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "配音片段不存在")
    track = await session.get(VoiceTrack, clip.track_id)
    if track is None:
        raise BizError(ErrorCode.NOT_FOUND, "配音轨不存在")
    if track.status == "voice_locked":
        raise BizError(ErrorCode.PARAM_INVALID, "配音轨已锁定")

    line = await session.get(ScriptLine, clip.line_id)
    if line is None:
        raise BizError(ErrorCode.NOT_FOUND, "台词不存在")
    params = await active_params(session, project_id, line.character_asset_id) if line.character_asset_id else None
    use_rate = rate if rate is not None else (float(params.rate) if params else 1.0)
    use_pitch = pitch if pitch is not None else (params.pitch if params else 0)
    old_ms = clip.duration_ms
    voice_name, voice_freq = await resolve_voice_profile(session, clip.voice_id)
    # P1 修复：参数未设默认情绪时回退剧本行情绪，避免重配后情绪归零
    emotion = (params.emotion_default if params else None) or line.emotion_tag
    result = await _synthesize(
        project_id=project_id,
        text=line.content,
        voice_id=clip.voice_id,
        rate=use_rate,
        pitch=use_pitch,
        emotion=emotion,
        voice_name=voice_name,
        voice_freq=voice_freq,
    )
    new_ms = result["duration_ms"]
    delta = new_ms - old_ms
    delta_pct = abs(delta) / max(old_ms, 1) * 100
    level = _drift_level(abs(delta), delta_pct)

    clip.duration_ms = new_ms
    clip.audio_key = result["audio_key"]
    clip.audio_url = result["audio_url"]
    clip.words = result["words"]
    # P1 修复：重配同样持久化实际情绪，保证详情页与首次生成口径一致
    clip.emotion = emotion
    clip.align_degraded = result["sandbox"]
    clip.retry_count += 1
    await ensure_audio_media(
        session,
        project_id=project_id,
        audio_key=str(result["audio_key"]),
        audio_url=str(result["audio_url"]),
        duration_ms=int(new_ms or 0),
    )
    degraded = bool(result.get("degraded"))
    clip.status = "failed" if degraded else "voiced"
    clip.error_message = result.get("error") if degraded else None
    if not degraded:
        line.actual_ms = new_ms
        line.voice_status = "voiced"

    from app.models.v6 import ShotLineBinding

    affected = [
        row.shot_id
        for row in (
            await session.execute(
                select(ShotLineBinding).where(ShotLineBinding.line_id == line.id)
            )
        ).scalars().all()
    ]
    drift = VoiceDriftRecord(
        project_id=project_id,
        ep_no=track.ep_no,
        line_id=line.id,
        old_ms=old_ms,
        new_ms=new_ms,
        delta_ms=delta,
        delta_pct=round(delta_pct, 2),
        level=level,
        affected_shot_ids=affected,
        status="pending",
        source="rerecord",
    )
    session.add(drift)
    await session.flush()
    await _refresh_track(session, track, track.ep_no)
    await session.commit()
    # P1-12：单句重配同样产生真实 TTS 成本，主数据提交后独立结算
    project = await session.get(Project, project_id)
    billed = not result["sandbox"] and not degraded
    billing = await _settle_voice_cost(
        project_id=project_id,
        owner_user_id=(
            int(project.owner_user_id) if project and project.owner_user_id else None
        ),
        ep_no=track.ep_no,
        track_id=int(track.id),
        line_count=1 if billed else 0,
        actual_ms=new_ms if billed else 0,
        idempotency_suffix=f"clip-{int(clip.id)}-{int(clip.retry_count)}",
    )
    return {
        "clip_id": clip.id,
        "old_ms": old_ms,
        "new_ms": new_ms,
        "delta_ms": delta,
        "delta_pct": round(delta_pct, 2),
        "level": level,
        "affected_shot_ids": affected,
        "drift_id": drift.id,
        "reason": reason,
        "requested_by": user_id,
        "billing": billing,
    }


async def decide_drift(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    drift_id: int,
    action: str,
    level: str | None,
) -> dict[str, Any]:
    """处理时长漂移：absorb 不改镜头，recalc 重算绑定镜头，dismiss 忽略。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        drift_id: 漂移记录 ID。
        action: apply/dismiss。
        level: 指定处理级别（空沿用记录建议）。

    Returns:
        处理结果与重算后的镜头 ID。

    Raises:
        BizError: 10002 记录不存在。
    """
    drift = await session.get(VoiceDriftRecord, drift_id)
    if drift is None or drift.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "漂移记录不存在")
    if action == "dismiss":
        drift.status = "dismissed"
        await session.commit()
        return {"drift_id": drift.id, "status": "dismissed", "recalculated_shots": []}

    effective = level or drift.level
    drift.level = effective
    drift.status = "confirmed"
    recalculated: list[int] = []
    if effective == "recalc":
        from app.services import line_service

        for shot_id in drift.affected_shot_ids:
            changed = await line_service.recalculate_shot_duration(
                session, project_id, int(shot_id), force_manual=False, commit=False
            )
            if changed:
                recalculated.append(int(shot_id))
    await session.commit()
    return {
        "drift_id": drift.id,
        "status": "confirmed",
        "level": effective,
        "recalculated_shots": recalculated,
        "handled_by": user_id,
    }


async def approve_track(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    ep_no: int,
    decision: str,
    comment: str | None,
) -> dict[str, Any]:
    """关口 V 审批：全部完成才可通过，通过后锁定。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 审批人。
        ep_no: 集号。
        decision: approve/reject。
        comment: 备注。

    Returns:
        审批后的轨道字典。

    Raises:
        BizError: 10002 集不存在；60003 存在未完成/失败句。
    """
    track = await get_or_create_track(session, project_id, ep_no)
    lines = await _project_lines(session, project_id, ep_no)
    clips = list(
        (
            await session.execute(select(VoiceClip).where(VoiceClip.track_id == track.id))
        ).scalars().all()
    )
    voiced = {c.line_id for c in clips if c.status == "voiced"}
    missing = [line.id for line in lines if line.id not in voiced]
    if decision == "approve" and missing:
        raise BizError(ErrorCode.QC_NOT_PASSED, f"仍有 {len(missing)} 句未完成配音")

    uid = int(user_id) if str(user_id).isdigit() else None
    now = datetime.now(UTC)
    if decision == "approve":
        track.status = "voice_locked"
        track.gate_status = "approved"
        track.approved_by = uid
        track.approved_at = now
        track.locked_at = now
    else:
        track.status = "voice_ready"
        track.gate_status = "rejected"
        track.locked_at = None
    track.report = await build_report(session, project_id, ep_no)
    track.report["gate_comment"] = comment
    await session.commit()
    return {"track": _track_dict(track), "missing_line_ids": missing, "comment": comment}


async def ensure_voice_param_for_characters(
    session: AsyncSession, project_id: int, ep_no: int
) -> list[dict[str, Any]]:
    """检查某集角色是否均已配置音色（供 Overview 展示）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        缺失音色的角色清单。
    """
    report = await build_report(session, project_id, ep_no)
    return report["missing_voice_characters"]

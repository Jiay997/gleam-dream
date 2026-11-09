"""F3.14 照片合成角色服务：四道闸、批次级联合规、身份档与 M1 合成。

设计目标（PRD V6.4 F3.14 / 06 契约照片合成增量）：
- **四道闸串行**：static（格式/大小/分辨率）→ content（人脸/清晰度/姿态/
  光照）→ compliance（公众人物/未成年/敏感/授权）→ dedup（内容指纹重复）。
  任一闸拒绝即落 ``rejected`` 并记录 ``gate`` 与 ``reject_code``；
- **批次级联合规**：批次内出现合规类拒绝时，整批照片标记 ``batch_blocked``，
  禁止继续合成；可用照片数不足下限时同样阻断；
- **身份档**：同批次可用照片聚类为一个身份档，主图缺省取质量分最高者；
- **M1 合成**：照片身份保持合成（沙箱下确定性占位），产出预览键、人脸
  相似度、过度相似告警与自检明细；确认后落角色资产。

沙箱模式（``settings.SANDBOX_MODE``）不调用真实视觉/合成模型，采用确定性
哈希评分，保证演示与自动化测试可重复。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.models.asset import Asset, AssetVersion
from app.models.v6 import CharacterComposition, IdentityProfile, PhotoUpload

#: 允许上传的图片 MIME
_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
#: 短边最小分辨率（像素）
_MIN_SHORT_SIDE = 512
#: 单张照片允许的人脸数量上限（单人照）
_MAX_FACE_COUNT = 1
#: 清晰度阈值
_MIN_BLUR = 0.45
#: 光照阈值
_MIN_LIGHTING = 0.35
#: 姿态偏转上限（度）
_MAX_POSE = 35.0
#: 批次可用照片下限（不足以覆盖多角度要求）
_MIN_BATCH_USABLE = 3
#: 未成年置信度阈值
_MINOR_THRESHOLD = 0.6
#: 合成身份相似度下限（低于则判合成失败）
_FACE_SCORE_MIN = 0.72
#: 过度相似阈值（过高说明近乎复制原图，缺乏角色化）
_OVER_SIMILARITY = 0.97
#: 合规类拒绝闸（触发批次级联拦截）
_BATCH_BLOCK_GATES = {"compliance"}


def _digest(*parts: Any) -> str:
    """对若干片段做 SHA-256 十六进制摘要。

    Args:
        *parts: 任意可字符串化片段。

    Returns:
        64 位十六进制摘要串。
    """
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _stable_score(seed: str, low: float = 0.5, high: float = 0.98) -> float:
    """基于种子生成确定性伪评分（沙箱替代真实模型打分）。

    Args:
        seed: 种子字符串（通常为照片指纹）。
        low: 区间下界。
        high: 区间上界。

    Returns:
        ``[low, high]`` 之间的浮点分。
    """
    value = int(_digest(seed)[:8], 16) / 0xFFFFFFFF
    return round(low + (high - low) * value, 3)


def _photo_dict(photo: PhotoUpload) -> dict[str, Any]:
    """把照片记录序列化为前端可读字典。

    Args:
        photo: 照片上传记录。

    Returns:
        含闸门结果、质量分与状态字段的字典。
    """
    return {
        "id": str(photo.id),
        "batch_key": photo.batch_key,
        "file_name": photo.file_name,
        "mime": photo.mime,
        "size_bytes": photo.size_bytes,
        "width": photo.width,
        "height": photo.height,
        "sha256": photo.sha256,
        "status": photo.status,
        "gate": photo.gate,
        "reject_code": photo.reject_code,
        "reject_message": photo.reject_message,
        "quality_score": float(photo.quality_score) if photo.quality_score is not None else None,
        "face_count": photo.face_count,
        "pose_yaw": float(photo.pose_yaw) if photo.pose_yaw is not None else None,
        "pose_pitch": float(photo.pose_pitch) if photo.pose_pitch is not None else None,
        "blur_score": float(photo.blur_score) if photo.blur_score is not None else None,
        "lighting_score": (
            float(photo.lighting_score) if photo.lighting_score is not None else None
        ),
        "is_public_figure": photo.is_public_figure,
        "minor_confidence": (
            float(photo.minor_confidence) if photo.minor_confidence is not None else None
        ),
        "is_sensitive": photo.is_sensitive,
        "is_duplicate": photo.is_duplicate,
        "is_main": photo.is_main,
        "batch_blocked": photo.batch_blocked,
        "declaration": photo.declaration,
        "authorization_confirmed": photo.authorization_confirmed,
        "exif_stripped": photo.exif_stripped,
    }


def _reject(
    photo: PhotoUpload,
    gate: str,
    code: str,
    message: str,
) -> None:
    """将照片标记为拒绝状态并记录闸门与拒绝码。

    Args:
        photo: 待标记的照片记录。
        gate: 触发闸门（static/content/compliance/dedup）。
        code: 拒绝码（F314_*）。
        message: 用户可读提示。

    Returns:
        无返回值（原地修改记录）。
    """
    photo.status = "rejected"
    photo.gate = gate
    photo.reject_code = code
    photo.reject_message = message


def _check_static(payload: Any) -> tuple[str, str] | None:
    """静态闸：校验格式、大小与分辨率。

    Args:
        payload: 上传登记请求体（``PhotoUploadRequest``）。

    Returns:
        命中拒绝时返回 ``(拒绝码, 提示)``，否则返回 None。
    """
    if payload.mime not in _ALLOWED_MIME:
        return "F314_FORMAT", "仅支持 JPG / PNG / WebP 格式"
    if payload.size_bytes > 20 * 1024 * 1024:
        return "F314_TOO_LARGE", "单张照片不得超过 20MB"
    if payload.width and payload.height:
        if min(payload.width, payload.height) < _MIN_SHORT_SIDE:
            return "F314_RESOLUTION", f"照片短边需不小于 {_MIN_SHORT_SIDE} 像素"
    return None


def _check_content(
    payload: Any,
    fingerprint: str,
) -> tuple[str, str] | None:
    """内容闸：校验人脸数量、清晰度、姿态与光照。

    Args:
        payload: 上传登记请求体。
        fingerprint: 照片内容指纹（用于沙箱确定性打分）。

    Returns:
        命中拒绝时返回 ``(拒绝码, 提示)``，否则返回 None。
    """
    analysis: dict[str, Any] = payload.analysis or {}
    face_count = int(analysis.get("face_count", 1))
    blur = float(analysis.get("blur_score", _stable_score(f"{fingerprint}:blur", 0.4, 0.99)))
    lighting = float(
        analysis.get("lighting_score", _stable_score(f"{fingerprint}:light", 0.3, 0.98))
    )
    yaw = abs(float(analysis.get("pose_yaw", 0)))
    pitch = abs(float(analysis.get("pose_pitch", 0)))
    if face_count < 1:
        return "F314_FACE_COUNT", "未检测到人脸，请更换照片"
    if face_count > _MAX_FACE_COUNT:
        return "F314_FACE_COUNT", "仅支持单人照片，请上传只含一张人脸的图片"
    if blur < _MIN_BLUR:
        return "F314_BLUR", "照片过于模糊，请上传更清晰的照片"
    if max(yaw, pitch) > _MAX_POSE:
        return "F314_POSE", f"人脸偏转超过 {int(_MAX_POSE)} 度，请使用正脸或轻微侧脸照片"
    if lighting < _MIN_LIGHTING:
        return "F314_LIGHTING", "光照不足或过曝，请调整拍摄光线"
    return None


def _check_compliance(
    payload: Any,
    fingerprint: str,
) -> tuple[str, str] | None:
    """合规闸：校验公众人物、未成年、敏感内容与授权声明。

    Args:
        payload: 上传登记请求体。
        fingerprint: 照片内容指纹（用于沙箱确定性打分）。

    Returns:
        命中拒绝时返回 ``(拒绝码, 提示)``，否则返回 None。
    """
    analysis: dict[str, Any] = payload.analysis or {}
    if bool(analysis.get("is_public_figure", False)):
        return "F314_PUBLIC_FIGURE", "疑似公众人物，禁止用于 AI 角色合成"
    minor = float(
        analysis.get("minor_confidence", _stable_score(f"{fingerprint}:age", 0.05, 0.2))
    )
    if minor >= _MINOR_THRESHOLD:
        return "F314_MINOR", "疑似未成年人照片，禁止用于 AI 角色合成"
    if bool(analysis.get("is_sensitive", False)):
        return "F314_SENSITIVE", "照片包含敏感内容，已拦截"
    if payload.declaration == "authorized_other" and not payload.authorization_confirmed:
        return "F314_AUTH_MISSING", "使用他人照片须先取得并确认本人授权"
    return None


async def _find_duplicate(
    session: AsyncSession,
    project_id: int,
    sha256: str | None,
) -> PhotoUpload | None:
    """去重闸：在项目内按内容指纹查重。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        sha256: 内容指纹。

    Returns:
        已存在的同指纹照片记录；无重复时返回 None。
    """
    if not sha256:
        return None
    stmt = select(PhotoUpload).where(
        PhotoUpload.project_id == project_id,
        PhotoUpload.sha256 == sha256,
        PhotoUpload.status.notin_(("removed", "invalid")),
    )
    return (await session.execute(stmt)).scalars().first()


async def _block_batch(
    session: AsyncSession,
    project_id: int,
    batch_key: str,
    code: str,
) -> int:
    """把批次内全部未终态照片标记为级联拦截。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        batch_key: 批次键。
        code: 触发拦截的拒绝码（写入提示文案）。

    Returns:
        被标记的照片数量。
    """
    stmt = select(PhotoUpload).where(
        PhotoUpload.project_id == project_id,
        PhotoUpload.batch_key == batch_key,
    )
    photos = list((await session.execute(stmt)).scalars())
    count = 0
    for photo in photos:
        if photo.status in ("removed",):
            continue
        photo.batch_blocked = True
        if photo.status not in ("rejected", "consumed"):
            photo.status = "rejected"
            photo.gate = photo.gate or "compliance"
            photo.reject_code = photo.reject_code or "F314_BATCH_BLOCKED"
            photo.reject_message = photo.reject_message or f"同批次存在合规问题（{code}），整批不可用"
            count += 1
    return count


async def batch_status(
    session: AsyncSession,
    project_id: int,
    batch_key: str,
) -> dict[str, Any]:
    """统计批次照片的可用情况与阻断状态。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        batch_key: 批次键。

    Returns:
        含总数、各状态计数、可用数下限与阻断标记的字典。
    """
    stmt = select(PhotoUpload).where(
        PhotoUpload.project_id == project_id,
        PhotoUpload.batch_key == batch_key,
    )
    photos = list((await session.execute(stmt)).scalars())
    usable = [p for p in photos if p.status in ("usable", "degraded")]
    blocked = any(p.batch_blocked for p in photos)
    return {
        "batch_key": batch_key,
        "total": len(photos),
        "usable": len(usable),
        "rejected": len([p for p in photos if p.status == "rejected"]),
        "duplicate": len([p for p in photos if p.is_duplicate]),
        "blocked": blocked,
        "min_required": _MIN_BATCH_USABLE,
        "ready": (not blocked) and len(usable) >= _MIN_BATCH_USABLE,
        "photos": [_photo_dict(p) for p in photos],
    }


async def stage_upload(
    session: AsyncSession,
    project_id: int,
    user_id: int | None,
    payload: Any,
) -> dict[str, Any]:
    """登记一张照片并串行执行四道闸。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 上传人用户 ID（可为 None）。
        payload: ``PhotoUploadRequest`` 请求体。

    Returns:
        含照片记录与所属批次状态的字典。

    Raises:
        BizError: 40001 项目已归档或请求非法；50004 命中静态闸；50002 命中
            内容闸；50003 命中合规闸。
    """
    batch_key = payload.batch_key or uuid.uuid4().hex[:16]
    fingerprint = payload.sha256 or _digest(
        payload.file_name, payload.mime, payload.size_bytes, user_id, batch_key
    )
    photo = PhotoUpload(
        project_id=project_id,
        batch_key=batch_key,
        uploader_user_id=user_id,
        file_name=payload.file_name,
        mime=payload.mime,
        size_bytes=payload.size_bytes,
        width=payload.width,
        height=payload.height,
        sha256=fingerprint,
        declaration=payload.declaration,
        authorization_confirmed=payload.authorization_confirmed,
        exif_stripped=True,
        analysis=payload.analysis or {},
    )

    # —— 第一道闸：静态 ——
    static_reject = _check_static(payload)
    if static_reject:
        _reject(photo, "static", *static_reject)
        session.add(photo)
        await session.commit()
        raise BizError(ErrorCode.UPLOAD_FILE_INVALID, static_reject[1], error_detail="F314_STATIC")

    # —— 第四道闸：去重（先查重可在花费分析成本前拦截） ——
    duplicate = await _find_duplicate(session, project_id, fingerprint)
    if duplicate is not None:
        photo.status = "rejected"
        photo.gate = "dedup"
        photo.reject_code = "F314_DUPLICATE"
        photo.reject_message = "该照片已在本项目中上传过，请勿重复上传"
        photo.is_duplicate = True
        photo.batch_blocked = duplicate.batch_blocked
        session.add(photo)
        await session.commit()
        return {"photo": _photo_dict(photo), "batch": await batch_status(session, project_id, batch_key)}

    # —— 第二道闸：内容 ——
    content_reject = _check_content(payload, fingerprint)
    if content_reject:
        _reject(photo, "content", *content_reject)
        session.add(photo)
        await session.commit()
        raise BizError(ErrorCode.FACE_SIMILARITY_LOW, content_reject[1], error_detail="F314_CONTENT")

    # —— 第三道闸：合规 ——
    compliance_reject = _check_compliance(payload, fingerprint)
    if compliance_reject:
        _reject(photo, "compliance", *compliance_reject)
        photo.batch_blocked = True
        session.add(photo)
        await session.flush()
        await _block_batch(session, project_id, batch_key, compliance_reject[0])
        await session.commit()
        raise BizError(
            ErrorCode.HUMAN_AUTH_MISSING,
            compliance_reject[1],
            error_detail="F314_COMPLIANCE",
        )

    # —— 全闸通过：记录分析明细与质量分 ——
    analysis = payload.analysis or {}
    blur = float(analysis.get("blur_score", _stable_score(f"{fingerprint}:blur", 0.4, 0.99)))
    lighting = float(
        analysis.get("lighting_score", _stable_score(f"{fingerprint}:light", 0.3, 0.98))
    )
    quality = round((blur * 0.6 + lighting * 0.4), 3)
    photo.face_count = int(analysis.get("face_count", 1))
    photo.blur_score = Decimal(str(round(blur, 3)))
    photo.lighting_score = Decimal(str(round(lighting, 3)))
    photo.pose_yaw = Decimal(str(float(analysis.get("pose_yaw", 0))))
    photo.pose_pitch = Decimal(str(float(analysis.get("pose_pitch", 0))))
    photo.minor_confidence = Decimal(
        str(float(analysis.get("minor_confidence", _stable_score(f"{fingerprint}:age", 0.05, 0.2))))
    )
    photo.quality_score = Decimal(str(quality))
    photo.status = "usable" if quality >= 0.6 else "degraded"
    photo.analysis = {**analysis, "quality_score": quality, "sandbox": bool(analysis.get("sandbox", True))}

    # 批次已存在合规阻断时，新照片同样标记
    existing = (await session.execute(
        select(PhotoUpload).where(
            PhotoUpload.project_id == project_id,
            PhotoUpload.batch_key == batch_key,
            PhotoUpload.batch_blocked.is_(True),
        )
    )).scalars().first()
    if existing is not None:
        photo.batch_blocked = True
        photo.status = "rejected"
        photo.gate = "compliance"
        photo.reject_code = "F314_BATCH_BLOCKED"
        photo.reject_message = "同批次存在合规问题，整批不可用"

    session.add(photo)
    await session.commit()
    await session.refresh(photo)
    return {
        "photo": _photo_dict(photo),
        "batch": await batch_status(session, project_id, batch_key),
    }


async def list_uploads(
    session: AsyncSession,
    project_id: int,
    batch_key: str | None = None,
) -> dict[str, Any]:
    """列出项目内照片（可按批次过滤）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        batch_key: 可选批次键。

    Returns:
        含 ``items`` 与批次统计的字典。
    """
    stmt = select(PhotoUpload).where(PhotoUpload.project_id == project_id)
    if batch_key:
        stmt = stmt.where(PhotoUpload.batch_key == batch_key)
    photos = list(
        (await session.execute(stmt.order_by(PhotoUpload.created_at.desc()))).scalars()
    )
    items = [_photo_dict(p) for p in photos]
    batches = sorted({p.batch_key for p in photos})
    return {"items": items, "total": len(items), "batches": batches}


async def remove_upload(
    session: AsyncSession,
    project_id: int,
    photo_id: int,
) -> dict[str, Any]:
    """移除一张照片（软状态标记，保留审计痕迹）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        photo_id: 照片 ID。

    Returns:
        ``{id, status}``。

    Raises:
        BizError: 10002 照片不存在。
    """
    photo = await session.get(PhotoUpload, photo_id)
    if photo is None or photo.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "照片不存在")
    photo.status = "removed"
    photo.is_main = False
    await session.commit()
    return {"id": str(photo.id), "status": photo.status}


def _profile_dict(profile: IdentityProfile) -> dict[str, Any]:
    """把身份档记录序列化为前端可读字典。

    Args:
        profile: 身份档记录。

    Returns:
        含主图、照片列表与状态的字典。
    """
    return {
        "id": str(profile.id),
        "batch_key": profile.batch_key,
        "name": profile.name,
        "main_photo_id": str(profile.main_photo_id) if profile.main_photo_id else None,
        "photo_ids": profile.photo_ids or [],
        "fingerprint": profile.fingerprint,
        "cluster_info": profile.cluster_info or {},
        "status": profile.status,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
    }


async def create_profile(
    session: AsyncSession,
    project_id: int,
    user_id: int | None,
    payload: Any,
) -> dict[str, Any]:
    """创建身份档：校验批次可用性并选定主图。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 创建人用户 ID。
        payload: ``IdentityProfileRequest`` 请求体。

    Returns:
        身份档字典。

    Raises:
        BizError: 10002 主图不存在；40001 批次被合规阻断或可用照片不足。
    """
    batch = await batch_status(session, project_id, payload.batch_key)
    if batch["blocked"]:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            "该批次存在合规问题，无法创建身份档",
            error_detail="F314_BATCH_BLOCKED",
        )
    usable = [p for p in batch["photos"] if p["status"] in ("usable", "degraded")]
    if len(usable) < _MIN_BATCH_USABLE:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            f"可用照片不足 {_MIN_BATCH_USABLE} 张，无法稳定合成角色",
            error_detail="F314_NOT_ENOUGH_PHOTOS",
        )
    if payload.photo_ids:
        chosen = [p for p in usable if int(p["id"]) in set(payload.photo_ids)]
        if not chosen:
            raise BizError(ErrorCode.NOT_FOUND, "指定的照片不可用")
    else:
        chosen = usable

    main_id = payload.main_photo_id
    if main_id is None:
        best = max(chosen, key=lambda p: p["quality_score"] or 0)
        main_id = int(best["id"])
    else:
        if not any(int(p["id"]) == main_id for p in usable):
            raise BizError(ErrorCode.NOT_FOUND, "主图不存在或不可用")

    fingerprint = _digest(*sorted(p["sha256"] or "" for p in chosen))
    profile = IdentityProfile(
        project_id=project_id,
        batch_key=payload.batch_key,
        name=payload.name,
        main_photo_id=main_id,
        photo_ids=[int(p["id"]) for p in chosen],
        fingerprint=fingerprint,
        cluster_info={
            "photo_count": len(chosen),
            "avg_quality": round(
                sum(float(p["quality_score"] or 0) for p in chosen) / max(len(chosen), 1), 3
            ),
            "main_photo_id": main_id,
        },
        status="ready",
        created_by=user_id,
    )
    session.add(profile)
    await session.flush()
    for item in chosen:
        photo = await session.get(PhotoUpload, int(item["id"]))
        if photo is not None:
            photo.status = "consumed"
            photo.is_main = int(item["id"]) == main_id
    await session.commit()
    await session.refresh(profile)
    return _profile_dict(profile)


async def list_profiles(
    session: AsyncSession,
    project_id: int,
) -> dict[str, Any]:
    """列出项目内身份档。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。

    Returns:
        含 ``items`` 与总数的字典。
    """
    stmt = select(IdentityProfile).where(IdentityProfile.project_id == project_id)
    rows = list((await session.execute(stmt.order_by(IdentityProfile.created_at.desc()))).scalars())
    return {"items": [_profile_dict(r) for r in rows], "total": len(rows)}


def _composition_dict(comp: CharacterComposition) -> dict[str, Any]:
    """把合成任务序列化为前端可读字典。

    Args:
        comp: 合成任务记录。

    Returns:
        含状态、预览、相似度与自检明细的字典。
    """
    return {
        "id": str(comp.id),
        "profile_id": str(comp.profile_id),
        "batch_key": comp.batch_key,
        "style_preset_id": str(comp.style_preset_id) if comp.style_preset_id else None,
        "stylization": comp.stylization,
        "mode": comp.mode,
        "status": comp.status,
        "asset_id": str(comp.asset_id) if comp.asset_id else None,
        "preview_media_key": comp.preview_media_key,
        "preview_url": f"/files/{comp.preview_media_key}" if comp.preview_media_key else None,
        "face_score": float(comp.face_score) if comp.face_score is not None else None,
        "over_similarity": comp.over_similarity,
        "self_check": comp.self_check or {},
        "error_code": comp.error_code,
        "error_message": comp.error_message,
        "rejected_codes": comp.rejected_codes or [],
        "composed_at": comp.composed_at.isoformat() if comp.composed_at else None,
    }


async def create_composition(
    session: AsyncSession,
    project_id: int,
    user_id: int | None,
    payload: Any,
) -> dict[str, Any]:
    """发起 M1 照片合成角色并执行合成自检。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 发起人用户 ID。
        payload: ``CompositionCreateRequest`` 请求体。

    Returns:
        合成任务字典（含预览与自检结果）。

    Raises:
        BizError: 10002 身份档不存在；40001 身份档未就绪或成功位之外。
    """
    profile = await session.get(IdentityProfile, payload.profile_id)
    if profile is None or profile.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "身份档不存在")
    if profile.status != "ready":
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "身份档状态不可用于合成")

    batch = await batch_status(session, project_id, profile.batch_key)
    if batch["blocked"]:
        raise BizError(
            ErrorCode.PROJECT_STATE_INVALID,
            "该批次存在合规问题，无法合成",
            error_detail="F314_BATCH_BLOCKED",
        )

    comp = CharacterComposition(
        project_id=project_id,
        profile_id=profile.id,
        batch_key=profile.batch_key,
        style_preset_id=payload.style_preset_id,
        stylization=payload.stylization,
        mode="M1",
        status="composing",
        config_version=1,
    )
    session.add(comp)
    await session.flush()

    # —— 合成（沙箱期内为确定性占位产物） ——
    seed = _digest(profile.fingerprint, payload.stylization, payload.style_preset_id)
    face_score = _stable_score(f"{seed}:face", 0.73, 0.94)
    over = face_score >= _OVER_SIMILARITY or payload.stylization <= 10
    preview_key = f"character/m1/{project_id}/{comp.id}/{seed[:12]}.png"

    checks = {
        "face_consistency": face_score >= _FACE_SCORE_MIN,
        "style_applied": payload.stylization > 0,
        "single_person": True,
        "background_clean": True,
        "no_extra_fingers": True,
        "sandbox": True,
    }
    comp.preview_media_key = preview_key
    comp.face_score = Decimal(str(face_score))
    comp.over_similarity = over
    comp.self_check = {
        "checks": checks,
        "passed": all(checks.values()),
        "thresholds": {
            "face_score_min": _FACE_SCORE_MIN,
            "over_similarity": _OVER_SIMILARITY,
        },
    }
    comp.status = "preview_ready" if face_score >= _FACE_SCORE_MIN else "failed"
    if comp.status == "failed":
        comp.error_code = "F314_FACE_DRIFT"
        comp.error_message = "合成结果与原图人脸相似度不足，请调整风格化强度或更换主图"
    comp.composed_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(comp)
    return _composition_dict(comp)


async def list_compositions(
    session: AsyncSession,
    project_id: int,
) -> dict[str, Any]:
    """列出项目内合成任务。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。

    Returns:
        含 ``items`` 与总数的字典。
    """
    stmt = select(CharacterComposition).where(CharacterComposition.project_id == project_id)
    rows = list(
        (await session.execute(stmt.order_by(CharacterComposition.created_at.desc()))).scalars()
    )
    return {"items": [_composition_dict(r) for r in rows], "total": len(rows)}


async def confirm_composition(
    session: AsyncSession,
    project_id: int,
    user_id: int | None,
    composition_id: int,
    name: str | None = None,
) -> dict[str, Any]:
    """确认合成结果并落角色资产。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作人用户 ID。
        composition_id: 合成任务 ID。
        name: 角色资产名称（缺省取身份档名称）。

    Returns:
        含 ``composition`` 与 ``asset_id`` 的字典。

    Raises:
        BizError: 10002 合成任务不存在；40001 当前状态不可确认。
    """
    comp = await session.get(CharacterComposition, composition_id)
    if comp is None or comp.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "合成任务不存在")
    if comp.status != "preview_ready" or comp.asset_id is not None:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "当前合成状态不可确认")

    profile = await session.get(IdentityProfile, comp.profile_id)
    asset = Asset(
        project_id=project_id,
        owner_user_id=user_id,
        asset_type="character",
        name=name or (profile.name if profile else "照片合成角色"),
        description="由照片合成（M1 身份保持）生成的角色资产",
        status="pending_review",
        source="ai_generated",
        tags={"source": "photo_composition"},
        labels=["照片合成"],
        meta={
            "composition_id": str(comp.id),
            "profile_id": str(comp.profile_id),
            "mode": comp.mode,
            "face_score": float(comp.face_score) if comp.face_score is not None else None,
        },
    )
    session.add(asset)
    await session.flush()
    version = AssetVersion(
        asset_id=asset.id,
        version_type="portrait",
        version_no=1,
        prompt=f"photo-composition M1 stylization={comp.stylization}",
        model_key="m1-identity",
        is_current=True,
        revision_notes="照片合成角色确认",
    )
    session.add(version)
    await session.flush()
    asset.current_version_id = version.id
    comp.asset_id = asset.id
    comp.status = "confirmed"
    if profile is not None:
        profile.status = "consumed"
    await session.commit()
    await session.refresh(comp)
    return {"composition": _composition_dict(comp), "asset_id": str(asset.id)}

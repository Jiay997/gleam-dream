"""5A Story Bible / 连续性校验 / 结构化反馈，以及 F2.13 逻辑规则服务。

能力：
- **Story Bible**：角色/场景/道具三类权威设定，完整度低于阈值时提示先补全；
- **连续性事实**：从剧本场次重建“世界状态”，缺失字段落清单（unknown）；
- **L1/L2 校验**：L1 规则类（在场/服装/道具/时段），L2 视觉类（人脸/服装/场景/
  光照）按参数中心阈值裁决；无产出时返回 unavailable 而非 pass；
- **逻辑规则**：LR-1~LR-6 确定性检查，支持“忽略并留痕”豁免；
- **结构化反馈**：维度化 reject → 确定性路由到修正策略 → 反例库沉淀。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.models.project import Project
from app.models.script import Episode, Scene, ScriptLine
from app.models.storyboard import Shot
from app.models.v6 import (
    ContinuityCheck,
    ContinuityFact,
    ContinuitySnapshot,
    HumanFeedback,
    LogicWaiver,
    NegativeExample,
    StoryBibleCharacter,
    StoryBibleProp,
    StoryBibleScene,
)
from app.services import param_service

#: 维度 → 确定性修正策略（5A.4 路由表）
ROUTE_MAP: dict[str, str] = {
    "face": "face_identity_lock",
    "identity": "face_identity_lock",
    "appearance": "face_identity_lock",
    "outfit": "outfit_from_bible",
    "scene": "scene_ref_anchor",
    "space": "scene_ref_anchor",
    "prop": "prop_state_from_bible",
    "lighting": "lighting_continuity",
    "timing_light": "lighting_continuity",
    "plot_logic": "logic_rules_recheck",
    "pacing": "duration_rebalance",
    "audio": "voice_regenerate",
    "voice": "voice_regenerate",
    "subtitle": "subtitle_realign",
    "composition": "composition_recompile",
}

#: L2 维度 → 参数字典键
_L2_THRESHOLD_KEYS = {
    "face": "continuity.l2_face_threshold",
    "outfit": "continuity.l2_outfit_threshold",
    "scene": "asset.scene_similarity_threshold",
    "prop": "asset.prop_similarity_threshold",
    "timing_light": "continuity.l2_lighting_threshold",
}

#: L2 维度默认阈值
_L2_DEFAULT = {
    "face": 0.72,
    "outfit": 0.75,
    "scene": 0.68,
    "prop": 0.70,
    "timing_light": 0.65,
}


def _stable_score(seed: str, low: float = 0.55, high: float = 0.95) -> float:
    """由证据串生成稳定的伪评分（无视觉模型时的确定性退化）。

    Args:
        seed: 证据串（镜头 ID + 维度 + 快照哈希）。
        low: 最低分。
        high: 最高分。

    Returns:
        两位小数评分。
    """
    digest = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    ratio = (digest % 10000) / 10000
    return round(low + (high - low) * ratio, 4)


async def _episode(session: AsyncSession, project_id: int, ep_no: int) -> Episode:
    """查询分集。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        分集 ORM 实例。

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
    return episode


async def story_bible(
    session: AsyncSession, project_id: int, ep_no: int | None = None
) -> dict[str, Any]:
    """获取 Story Bible 全量设定与完整度。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号（场景筛选）。

    Returns:
        含 characters/outfits/scenes/props/completeness 的字典。
    """
    characters = (
        await session.execute(
            select(StoryBibleCharacter)
            .where(StoryBibleCharacter.project_id == project_id)
            .order_by(StoryBibleCharacter.id)
        )
    ).scalars().all()
    scene_stmt = select(StoryBibleScene).where(StoryBibleScene.project_id == project_id)
    if ep_no is not None:
        scene_stmt = scene_stmt.where(StoryBibleScene.ep_no == ep_no)
    scenes = (
        await session.execute(scene_stmt.order_by(StoryBibleScene.ep_no, StoryBibleScene.scene_no))
    ).scalars().all()
    props = (
        await session.execute(
            select(StoryBibleProp)
            .where(StoryBibleProp.project_id == project_id)
            .order_by(StoryBibleProp.id)
        )
    ).scalars().all()

    checks = 0
    filled = 0
    for char in characters:
        checks += 3
        filled += sum(1 for value in (char.base_look, char.body_desc, char.voice_id) if value)
    for scene in scenes:
        checks += 3
        filled += sum(1 for value in (scene.location, scene.time_of_day, scene.layout_ref) if value)
    for prop in props:
        checks += 2
        filled += sum(1 for value in (prop.state_machine, prop.current_state) if value)
    completeness = round(filled / max(1, checks), 4)

    config = await param_service.effective_config(session, project_id)
    threshold = float(config["values"].get("continuity.bible_completeness_min", 0.8))
    return {
        "characters": [
            {
                "id": c.id,
                "char_key": c.char_key,
                "name": c.name,
                "char_asset_id": c.char_asset_id,
                "base_look": c.base_look,
                "body_desc": c.body_desc,
                "voice_id": c.voice_id,
                "outfits": c.outfits,
                "current_state": c.current_state,
            }
            for c in characters
        ],
        "scenes": [
            {
                "id": s.id,
                "ep_no": s.ep_no,
                "scene_no": s.scene_no,
                "location": s.location,
                "layout_ref": s.layout_ref,
                "key_points": s.key_points,
                "time_of_day": s.time_of_day,
                "elapsed_minutes": s.elapsed_minutes,
                "weather": s.weather,
                "lighting_base": s.lighting_base,
                "is_flashback": s.is_flashback,
            }
            for s in scenes
        ],
        "props": [
            {
                "id": p.id,
                "prop_key": p.prop_key,
                "name": p.name,
                "prop_asset_id": p.prop_asset_id,
                "state_machine": p.state_machine,
                "holder_by_scene": p.holder_by_scene,
                "ref_images": p.ref_images,
                "current_state": p.current_state,
            }
            for p in props
        ],
        "completeness": completeness,
        "threshold": threshold,
        "warning": "Story Bible 完整度不足，建议先补全角色外观/场景/道具状态"
        if completeness < threshold
        else None,
    }


async def upsert_character(
    session: AsyncSession, project_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """写入/更新 Story Bible 角色档案。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        payload: 角色字段。

    Returns:
        角色字典。

    Raises:
        BizError: 10002 关联资产不存在。
    """
    if payload.get("char_asset_id") is not None:
        from app.models.asset import Asset

        asset = await session.get(Asset, int(payload["char_asset_id"]))
        if asset is None or asset.project_id != project_id:
            raise BizError(ErrorCode.NOT_FOUND, "角色资产不存在")
    row = (
        await session.execute(
            select(StoryBibleCharacter).where(
                StoryBibleCharacter.project_id == project_id,
                StoryBibleCharacter.char_key == payload["char_key"],
            )
        )
    ).scalars().first()
    if row is None:
        row = StoryBibleCharacter(project_id=project_id, char_key=payload["char_key"], name=payload["name"])
        session.add(row)
    for field in ("name", "char_asset_id", "base_look", "body_desc", "voice_id", "outfits", "current_state", "extra"):
        if field in payload and payload[field] is not None:
            setattr(row, field, payload[field])
    await session.flush()
    await _refresh_bible_flag(session, project_id)
    await session.commit()
    return {"id": row.id, "char_key": row.char_key, "completeness": (await story_bible(session, project_id))["completeness"]}


async def upsert_scene(
    session: AsyncSession, project_id: int, ep_no: int, scene_no: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """写入/更新 Story Bible 场景档案。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        scene_no: 场号。
        payload: 场景字段。

    Returns:
        场景字典与完整度。
    """
    row = (
        await session.execute(
            select(StoryBibleScene).where(
                StoryBibleScene.project_id == project_id,
                StoryBibleScene.ep_no == ep_no,
                StoryBibleScene.scene_no == scene_no,
            )
        )
    ).scalars().first()
    if row is None:
        row = StoryBibleScene(project_id=project_id, ep_no=ep_no, scene_no=scene_no)
        session.add(row)
    for field in ("location", "layout_ref", "key_points", "time_of_day", "elapsed_minutes", "weather", "lighting_base", "is_flashback"):
        if field in payload and payload[field] is not None:
            setattr(row, field, payload[field])
    await session.flush()
    await _refresh_bible_flag(session, project_id)
    await session.commit()
    return {"id": row.id, "ep_no": ep_no, "scene_no": scene_no, "completeness": (await story_bible(session, project_id))["completeness"]}


async def upsert_prop(
    session: AsyncSession, project_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """写入/更新 Story Bible 道具档案。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        payload: 道具字段。

    Returns:
        道具字典与完整度。
    """
    row = (
        await session.execute(
            select(StoryBibleProp).where(
                StoryBibleProp.project_id == project_id,
                StoryBibleProp.prop_key == payload["prop_key"],
            )
        )
    ).scalars().first()
    if row is None:
        row = StoryBibleProp(project_id=project_id, prop_key=payload["prop_key"], name=payload["name"])
        session.add(row)
    for field in ("name", "prop_asset_id", "state_machine", "holder_by_scene", "ref_images", "current_state"):
        if field in payload and payload[field] is not None:
            setattr(row, field, payload[field])
    await session.flush()
    await _refresh_bible_flag(session, project_id)
    await session.commit()
    return {"id": row.id, "prop_key": row.prop_key, "completeness": (await story_bible(session, project_id))["completeness"]}


async def _refresh_bible_flag(session: AsyncSession, project_id: int) -> None:
    """同步项目“Bible 不完整”标记。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。

    Returns:
        None
    """
    bible = await story_bible(session, project_id)
    project = await session.get(Project, project_id)
    if project is not None:
        project.bible_incomplete = bool(bible["warning"])


async def rebuild_facts(
    session: AsyncSession, project_id: int, ep_no: int | None, overwrite: bool
) -> dict[str, Any]:
    """从剧本重建场次连续性事实。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号（空表示全部）。
        overwrite: 是否覆盖人工编辑版本。

    Returns:
        含生成/跳过数与缺失字段统计的字典。
    """
    stmt = (
        select(Scene, Episode.episode_no)
        .join(Episode, Episode.id == Scene.episode_id)
        .where(Scene.project_id == project_id)
        .order_by(Episode.episode_no, Scene.scene_no)
    )
    if ep_no is not None:
        stmt = stmt.where(Episode.episode_no == ep_no)
    rows = (await session.execute(stmt)).all()

    created = skipped = 0
    missing_total: dict[str, int] = {}
    for scene, current_ep in rows:
        latest = (
            await session.execute(
                select(ContinuityFact)
                .where(
                    ContinuityFact.project_id == project_id,
                    ContinuityFact.ep_no == current_ep,
                    ContinuityFact.scene_no == scene.scene_no,
                )
                .order_by(ContinuityFact.version_no.desc())
                .limit(1)
            )
        ).scalars().first()
        if latest is not None and latest.source == "edit" and not overwrite:
            skipped += 1
            continue

        lines = (
            await session.execute(
                select(ScriptLine)
                .where(
                    ScriptLine.scene_id == scene.id,
                    ScriptLine.is_deleted.is_(False),
                )
                .order_by(ScriptLine.order_index)
            )
        ).scalars().all()
        characters = sorted(
            {
                line.speaker_name
                for line in lines
                if line.line_type == "dialogue" and line.speaker_name
            }
        )
        facts: dict[str, Any] = {
            "characters": characters,
            "outfits": {},
            "time": {
                "time_of_day": scene.time_of_day,
                "elapsed_minutes": None,
            },
            "props": {},
            "emotion_arc": [
                {"line_id": line.id, "emotion": line.emotion_tag}
                for line in lines
                if line.emotion_tag
            ],
            "line_ids": [line.id for line in lines if line.line_type in ("dialogue", "narration")],
            "summary": scene.summary,
        }
        missing: list[str] = []
        if not characters:
            missing.append("characters")
        if not scene.location:
            missing.append("location")
        if not scene.time_of_day:
            missing.append("time.time_of_day")
        if not scene.summary:
            missing.append("summary")
        for path in missing:
            missing_total[path] = missing_total.get(path, 0) + 1

        version = (latest.version_no if latest else 0) + 1
        session.add(
            ContinuityFact(
                project_id=project_id,
                script_id=None,
                scene_id=scene.id,
                ep_no=current_ep,
                scene_no=scene.scene_no,
                facts=facts,
                missing_fields=missing,
                completeness_pct=round(max(0, 4 - len(missing)) / 4 * 100, 2),
                version_no=version,
                source="inferred",
            )
        )
        created += 1
    await session.commit()
    return {
        "created": created,
        "skipped": skipped,
        "missing_fields": missing_total,
        "ep_no": ep_no,
    }


async def list_facts(
    session: AsyncSession, project_id: int, ep_no: int | None, latest_only: bool = True
) -> list[dict[str, Any]]:
    """查询连续性事实（默认仅最新版本）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        latest_only: 是否仅返回每场最新版本。

    Returns:
        事实列表。
    """
    stmt = select(ContinuityFact).where(ContinuityFact.project_id == project_id)
    if ep_no is not None:
        stmt = stmt.where(ContinuityFact.ep_no == ep_no)
    rows = (
        await session.execute(
            stmt.order_by(ContinuityFact.ep_no, ContinuityFact.scene_no, ContinuityFact.version_no.desc())
        )
    ).scalars().all()
    if not latest_only:
        return [_fact_dict(row) for row in rows]
    latest: dict[tuple[int, int], ContinuityFact] = {}
    for row in rows:
        latest.setdefault((row.ep_no, row.scene_no), row)
    return [_fact_dict(row) for row in latest.values()]


def _fact_dict(row: ContinuityFact) -> dict[str, Any]:
    """序列化连续性事实。

    Args:
        row: 事实 ORM 实例。

    Returns:
        可 JSON 化字典。
    """
    return {
        "id": row.id,
        "ep_no": row.ep_no,
        "scene_no": row.scene_no,
        "scene_id": row.scene_id,
        "facts": row.facts,
        "missing_fields": row.missing_fields,
        "completeness_pct": float(row.completeness_pct),
        "version_no": row.version_no,
        "source": row.source,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def upsert_fact(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    scene_no: int,
    facts: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    """人工写入/修正场次事实（版本号递增）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        scene_no: 场号。
        facts: 事实主体。
        source: 产出方式。

    Returns:
        新事实版本字典。

    Raises:
        BizError: 10002 场次不存在。
    """
    scene = (
        await session.execute(
            select(Scene)
            .join(Episode, Episode.id == Scene.episode_id)
            .where(
                Scene.project_id == project_id,
                Episode.episode_no == ep_no,
                Scene.scene_no == scene_no,
            )
        )
    ).scalars().first()
    if scene is None:
        raise BizError(ErrorCode.NOT_FOUND, "场次不存在")
    latest = (
        await session.execute(
            select(ContinuityFact)
            .where(
                ContinuityFact.project_id == project_id,
                ContinuityFact.ep_no == ep_no,
                ContinuityFact.scene_no == scene_no,
            )
            .order_by(ContinuityFact.version_no.desc())
            .limit(1)
        )
    ).scalars().first()
    missing = [key for key in ("characters", "location", "time", "summary") if not facts.get(key)]
    row = ContinuityFact(
        project_id=project_id,
        scene_id=scene.id,
        ep_no=ep_no,
        scene_no=scene_no,
        facts=facts,
        missing_fields=missing,
        completeness_pct=round(max(0, 4 - len(missing)) / 4 * 100, 2),
        version_no=(latest.version_no if latest else 0) + 1,
        source=source,
    )
    session.add(row)
    await session.commit()
    return _fact_dict(row)


async def run_checks(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    shot_ids: list[int],
    use_bible: bool,
) -> dict[str, Any]:
    """执行 L1 规则校验与 L2 视觉连续性校验，并保存快照与结果。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        shot_ids: 指定镜头（空表示整集）。
        use_bible: 是否启用 Story Bible 期望状态。

    Returns:
        含 checks / summary / degraded 的字典。

    Raises:
        BizError: 10002 集不存在/无镜头。
    """
    episode = await _episode(session, project_id, ep_no)
    stmt = (
        select(Shot)
        .where(Shot.project_id == project_id, Shot.episode_id == episode.id)
        .order_by(Shot.order_index, Shot.shot_no)
    )
    if shot_ids:
        stmt = stmt.where(Shot.id.in_(shot_ids))
    shots = (await session.execute(stmt)).scalars().all()
    if not shots:
        raise BizError(ErrorCode.NOT_FOUND, "没有可校验的镜头")

    config = await param_service.effective_config(session, project_id, ep_no)
    values = config["values"]
    facts = {row["scene_no"]: row for row in await list_facts(session, project_id, ep_no)}
    checks: list[dict[str, Any]] = []
    degraded = False

    for index, shot in enumerate(shots):
        scene_key = await _scene_no_for(session, shot)
        fact = facts.get(scene_key)
        expected = {
            "characters": (fact["facts"].get("characters") if fact else []) or [],
            "time_of_day": (fact["facts"].get("time", {}).get("time_of_day") if fact else None),
            "scene_id": shot.scene_id,
            "shot_id": shot.id,
            "bible_enabled": use_bible,
        }
        snapshot_hash = hashlib.sha256(
            f"{shot.id}|{expected}|{shot.updated_at}".encode("utf-8")
        ).hexdigest()
        existing_snapshot = (
            await session.execute(
                select(ContinuitySnapshot).where(
                    ContinuitySnapshot.shot_id == shot.id,
                    ContinuitySnapshot.snapshot_hash == snapshot_hash,
                )
            )
        ).scalars().first()
        if existing_snapshot is None:
            session.add(
                ContinuitySnapshot(
                    project_id=project_id,
                    shot_id=shot.id,
                    expected_state=expected,
                    source_version=f"v6:{ep_no}:{shot.id}",
                    snapshot_hash=snapshot_hash,
                )
            )

        # L1：无台词/无期望状态时给出规则类裁决，不依赖视觉模型
        l1_verdict = "pass"
        l1_evidence: dict[str, Any] = {}
        if use_bible and not expected["characters"]:
            l1_verdict = "warn"
            l1_evidence["reason"] = "场次事实缺少在场角色"
            degraded = True
        checks.append(
            await _save_check(
                session, project_id, shot.id, None, "L1", "plot_logic",
                1.0 if l1_verdict == "pass" else 0.5, 1.0, l1_verdict, l1_evidence, config["version"]
            )
        )

        # L2：没有最终产出的镜头返回 unavailable，避免“缺检当通过”
        has_output = shot.status in ("approved", "locked")
        for dimension in ("face", "outfit", "scene", "timing_light"):
            threshold = float(values.get(_L2_THRESHOLD_KEYS[dimension], _L2_DEFAULT[dimension]))
            if not has_output or shot.final_prompt is None:
                checks.append(
                    await _save_check(
                        session, project_id, shot.id, None, "L2", dimension,
                        None, threshold, "unavailable",
                        {"reason": "镜头尚无最终产出或 prompt"}, config["version"],
                    )
                )
                degraded = True
                continue
            score = _stable_score(f"{shot.id}|{dimension}|{snapshot_hash}")
            if score >= threshold:
                verdict = "pass"
            elif score < threshold - 0.05:
                verdict = "fail"
            else:
                verdict = "warn"
            evidence = {"snapshot_hash": snapshot_hash, "mode": "deterministic_guard"}
            checks.append(
                await _save_check(
                    session, project_id, shot.id, None, "L2", dimension,
                    score, threshold, verdict, evidence, config["version"],
                )
            )

        # L2 相邻镜头：仅对已有产出且非首个镜头计算
        if index > 0 and has_output:
            previous = shots[index - 1]
            if previous.status in ("approved", "locked"):
                score = _stable_score(f"{previous.id}|{shot.id}|face|adjacent")
                threshold = float(values.get("continuity.l2_face_threshold", 0.72))
                verdict = "pass" if score >= threshold else ("fail" if score < threshold - 0.05 else "warn")
                checks.append(
                    await _save_check(
                        session, project_id, shot.id, previous.id, "L2", "face",
                        score, threshold, verdict, {"adjacent_shot_id": previous.id}, config["version"],
                    )
                )

    await session.commit()
    summary = {
        "total": len(checks),
        "pass": sum(1 for c in checks if c["verdict"] == "pass"),
        "warn": sum(1 for c in checks if c["verdict"] == "warn"),
        "fail": sum(1 for c in checks if c["verdict"] == "fail"),
        "unavailable": sum(1 for c in checks if c["verdict"] == "unavailable"),
    }
    return {"checks": checks, "summary": summary, "degraded": degraded, "config_version": config["version"]}


async def _scene_no_for(session: AsyncSession, shot: Shot) -> int:
    """查询镜头所属场号。

    Args:
        session: 数据库会话。
        shot: 镜头实例。

    Returns:
        场号；缺失时返回 0。
    """
    if shot.scene_id is None:
        return 0
    scene = await session.get(Scene, shot.scene_id)
    return scene.scene_no if scene else 0


async def _save_check(
    session: AsyncSession,
    project_id: int,
    shot_id: int,
    adjacent_shot_id: int | None,
    level: str,
    dimension: str,
    score: float | None,
    threshold: float,
    verdict: str,
    evidence: dict[str, Any],
    config_version: int,
) -> dict[str, Any]:
    """写入一条连续性校验结果并返回其字典。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        shot_id: 镜头 ID。
        adjacent_shot_id: 相邻镜头 ID。
        level: L1/L2/L3。
        dimension: 维度。
        score: 得分（可为空）。
        threshold: 阈值。
        verdict: 裁决。
        evidence: 证据。
        config_version: 配置版本。

    Returns:
        校验结果字典。
    """
    row = ContinuityCheck(
        project_id=project_id,
        shot_id=shot_id,
        adjacent_shot_id=adjacent_shot_id,
        level=level,
        dimension=dimension,
        score=score,
        threshold=threshold,
        verdict=verdict,
        evidence_ref=evidence,
        reviewer="auto",
        config_version=config_version,
    )
    session.add(row)
    await session.flush()
    return {
        "id": row.id,
        "shot_id": shot_id,
        "adjacent_shot_id": adjacent_shot_id,
        "level": level,
        "dimension": dimension,
        "score": score,
        "threshold": threshold,
        "verdict": verdict,
        "evidence": evidence,
    }


async def report(
    session: AsyncSession, project_id: int, ep_no: int | None
) -> dict[str, Any]:
    """汇总连续性报告（按维度与层级）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号（可空）。

    Returns:
        含 summary / by_dimension / recent 的字典。
    """
    stmt = select(ContinuityCheck).where(ContinuityCheck.project_id == project_id)
    if ep_no is not None:
        episode = await _episode(session, project_id, ep_no)
        shot_ids = set(
            (
                await session.execute(
                    select(Shot.id).where(Shot.episode_id == episode.id)
                )
            ).scalars().all()
        )
        rows = [
            row
            for row in (await session.execute(stmt.order_by(ContinuityCheck.id.desc()).limit(1000))).scalars().all()
            if row.shot_id in shot_ids
        ]
    else:
        rows = list(
            (await session.execute(stmt.order_by(ContinuityCheck.id.desc()).limit(1000))).scalars().all()
        )

    by_dimension: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_dimension.setdefault(row.dimension, {"pass": 0, "warn": 0, "fail": 0, "unavailable": 0})
        bucket[row.verdict] = bucket.get(row.verdict, 0) + 1
    return {
        "ep_no": ep_no,
        "summary": {
            "total": len(rows),
            "pass": sum(1 for row in rows if row.verdict == "pass"),
            "warn": sum(1 for row in rows if row.verdict == "warn"),
            "fail": sum(1 for row in rows if row.verdict == "fail"),
            "unavailable": sum(1 for row in rows if row.verdict == "unavailable"),
        },
        "by_dimension": by_dimension,
        "recent": [
            {
                "id": row.id,
                "shot_id": row.shot_id,
                "level": row.level,
                "dimension": row.dimension,
                "score": float(row.score) if row.score is not None else None,
                "threshold": float(row.threshold) if row.threshold is not None else None,
                "verdict": row.verdict,
                "config_version": row.config_version,
                "checked_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows[:50]
        ],
    }


async def logic_check(
    session: AsyncSession, project_id: int, ep_no: int, rule_codes: list[str]
) -> dict[str, Any]:
    """执行 F2.13 逻辑/连续性规则检查（LR-1~LR-6）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        rule_codes: 指定规则（空表示全部）。

    Returns:
        含 issues / waived / summary 的字典。

    Raises:
        BizError: 10002 集不存在。
    """
    episode = await _episode(session, project_id, ep_no)
    scenes = (
        await session.execute(
            select(Scene).where(Scene.episode_id == episode.id).order_by(Scene.scene_no)
        )
    ).scalars().all()
    facts = {row["scene_no"]: row for row in await list_facts(session, project_id, ep_no)}
    waivers = (
        await session.execute(select(LogicWaiver).where(LogicWaiver.project_id == project_id))
    ).scalars().all()
    waiver_keys = {(w.rule_code, w.target_ref) for w in waivers}
    enabled = set(rule_codes) or {"LR-1", "LR-2", "LR-3", "LR-4", "LR-5", "LR-6"}

    issues: list[dict[str, Any]] = []
    previous_characters: set[str] | None = None
    previous_location: str | None = None
    previous_elapsed: int | None = None
    previous_outfits: dict[str, Any] | None = None
    holders: dict[str, str] = {}

    for scene in scenes:
        fact = facts.get(scene.scene_no)
        if fact is None:
            continue
        characters = set(fact["facts"].get("characters") or [])
        target_ref = f"ep:{ep_no}/scene:{scene.scene_no}"
        dead = [
            {"character": name}
            for name in characters
            if await _is_dead(session, project_id, name)
        ]
        if "LR-1" in enabled and dead:
            for item in dead:
                issue = {
                    "code": "LR-1",
                    "severity": "error",
                    "target_ref": target_ref,
                    "message": f"角色「{item['character']}」已标记死亡/退场，本场再次出场",
                    "evidence": item,
                    "suggestion": "修改设定或在剧情中补充回归解释",
                }
                if ("LR-1", target_ref) not in waiver_keys:
                    issues.append(issue)

        if "LR-2" in enabled and previous_location and scene.location and scene.location != previous_location:
            issues.append(
                {
                    "code": "LR-2",
                    "severity": "warning",
                    "target_ref": target_ref,
                    "message": f"场景由「{previous_location}」直接切到「{scene.location}」，缺少过渡",
                    "evidence": {"from": previous_location, "to": scene.location},
                    "suggestion": "补充转场镜头或调整场序",
                }
            )

        outfits = fact["facts"].get("outfits") or {}
        if "LR-5" in enabled and previous_outfits is not None:
            changed = [
                key
                for key, value in outfits.items()
                if previous_outfits.get(key) is not None and previous_outfits[key] != value
            ]
            if changed:
                issues.append(
                    {
                        "code": "LR-5",
                        "severity": "warning",
                        "target_ref": target_ref,
                        "message": f"角色服装突变：{', '.join(changed)}",
                        "evidence": {"changed": changed},
                        "suggestion": "在剧情中补充换装原因或统一造型",
                    }
                )

        elapsed = (fact["facts"].get("time") or {}).get("elapsed_minutes")
        if "LR-4" in enabled and elapsed is not None and previous_elapsed is not None and elapsed < previous_elapsed:
            issues.append(
                {
                    "code": "LR-4",
                    "severity": "error",
                    "target_ref": target_ref,
                    "message": f"剧情时间倒流：{previous_elapsed} → {elapsed} 分钟",
                    "evidence": {"previous": previous_elapsed, "current": elapsed},
                    "suggestion": "修正时间锚点或标记为闪回",
                }
            )

        props = fact["facts"].get("props") or {}
        if "LR-3" in enabled:
            for prop_key, state in props.items():
                previous_holder = holders.get(prop_key)
                current_holder = (state or {}).get("holder")
                if previous_holder and current_holder and previous_holder != current_holder:
                    target = f"{target_ref}/prop:{prop_key}"
                    if ("LR-3", target) not in waiver_keys:
                        issues.append(
                            {
                                "code": "LR-3",
                                "severity": "warning",
                                "target_ref": target,
                                "message": f"道具「{prop_key}」持有人由 {previous_holder} 变为 {current_holder}",
                                "evidence": {"from": previous_holder, "to": current_holder},
                                "suggestion": "补充交接动作或修正道具状态",
                            }
                        )
                if current_holder:
                    holders[prop_key] = current_holder

        if "LR-6" in enabled:
            for line_id in fact["facts"].get("line_ids") or []:
                line = await session.get(ScriptLine, int(line_id))
                if line is not None and line.line_type == "dialogue" and not line.speaker_name:
                    issues.append(
                        {
                            "code": "LR-6",
                            "severity": "warning",
                            "target_ref": f"{target_ref}/line:{line_id}",
                            "message": "台词缺少说话人，无法确定指代对象",
                            "evidence": {"line_id": line_id},
                            "suggestion": "补充说话人或改为旁白",
                        }
                    )

        previous_characters = characters
        previous_location = scene.location
        if elapsed is not None:
            previous_elapsed = elapsed
        previous_outfits = outfits

    waived = [
        {"rule_code": w.rule_code, "target_ref": w.target_ref, "reason": w.reason}
        for w in waivers
        if w.rule_code in enabled
    ]
    return {
        "ep_no": ep_no,
        "issues": issues,
        "waived": waived,
        "summary": {
            "total": len(issues),
            "error": sum(1 for i in issues if i["severity"] == "error"),
            "warning": sum(1 for i in issues if i["severity"] == "warning"),
            "waived": len(waived),
        },
        "rules": sorted(enabled),
    }


async def _is_dead(session: AsyncSession, project_id: int, name: str) -> bool:
    """判断角色在 Bible 中是否已死亡/退场。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        name: 角色名。

    Returns:
        是否死亡/退场。
    """
    row = (
        await session.execute(
            select(StoryBibleCharacter).where(
                StoryBibleCharacter.project_id == project_id,
                StoryBibleCharacter.name == name,
            )
        )
    ).scalars().first()
    return bool(row and row.current_state in ("dead", "offstage"))


async def create_waiver(
    session: AsyncSession, project_id: int, user_id: str, rule_code: str, target_ref: str, reason: str
) -> dict[str, Any]:
    """创建/更新逻辑规则豁免（忽略并留痕）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        rule_code: 规则编号。
        target_ref: 定位引用。
        reason: 理由（≥10 字）。

    Returns:
        豁免记录字典。

    Raises:
        BizError: 10001 理由过短。
    """
    if len(reason.strip()) < 10:
        raise BizError(ErrorCode.PARAM_INVALID, "豁免理由至少 10 字")
    row = (
        await session.execute(
            select(LogicWaiver).where(
                LogicWaiver.project_id == project_id,
                LogicWaiver.rule_code == rule_code,
                LogicWaiver.target_ref == target_ref,
            )
        )
    ).scalars().first()
    if row is None:
        row = LogicWaiver(
            project_id=project_id,
            rule_code=rule_code,
            target_ref=target_ref,
            reason=reason,
            actor_user_id=int(user_id) if str(user_id).isdigit() else None,
        )
        session.add(row)
    else:
        row.reason = reason
    await session.commit()
    return {"id": row.id, "rule_code": rule_code, "target_ref": target_ref, "reason": reason}


async def list_waivers(session: AsyncSession, project_id: int) -> list[dict[str, Any]]:
    """列出项目逻辑规则豁免。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。

    Returns:
        豁免列表。
    """
    rows = (
        await session.execute(
            select(LogicWaiver).where(LogicWaiver.project_id == project_id).order_by(LogicWaiver.id.desc())
        )
    ).scalars().all()
    return [
        {
            "id": row.id,
            "rule_code": row.rule_code,
            "target_ref": row.target_ref,
            "reason": row.reason,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def create_feedback(
    session: AsyncSession, project_id: int, user_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """写入结构化反馈并按维度路由到修正策略。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        payload: 反馈字段。

    Returns:
        反馈字典（含路由结果与是否触发反例） 。

    Raises:
        BizError: 10001 维度为空/不识别。
    """
    dimensions = [str(item) for item in payload.get("dimensions") or []]
    if not dimensions:
        raise BizError(ErrorCode.PARAM_INVALID, "至少选择一个反馈维度")
    routes = [ROUTE_MAP.get(dim, "generic_regenerate") for dim in dimensions]

    previous_round = (
        await session.execute(
            select(HumanFeedback)
            .where(
                HumanFeedback.project_id == project_id,
                HumanFeedback.target_type == payload["target_type"],
                HumanFeedback.target_id == payload["target_id"],
                HumanFeedback.dimensions.contains([dimensions[0]]),
            )
            .order_by(HumanFeedback.strategy_round.desc())
            .limit(1)
        )
    ).scalars().first()
    round_no = (previous_round.strategy_round if previous_round else 0) + 1

    row = HumanFeedback(
        project_id=project_id,
        target_type=payload["target_type"],
        target_id=payload["target_id"],
        dimensions=dimensions,
        scope=payload.get("scope") or {},
        expectation=payload.get("expectation"),
        expectation_text=payload.get("expectation_text"),
        keep_items=payload.get("keep_items") or [],
        note_text=payload.get("text"),
        source_gate=payload["source_gate"],
        routed_strategy=",".join(sorted(set(routes))),
        strategy_round=round_no,
        actor_user_id=int(user_id) if str(user_id).isdigit() else None,
    )
    session.add(row)
    await session.flush()
    if round_no >= 3:
        session.add(
            NegativeExample(
                project_id=project_id,
                dimension=dimensions[0],
                scene_ref=str(payload.get("scope", {}).get("scene_ref")) if payload.get("scope") else None,
                issue_desc=payload.get("expectation_text") or payload.get("text") or "同一维度连续三次被驳回",
                evidence_ref={"feedback_id": row.id, "dimensions": dimensions},
                fix_action=routes[0],
                passed=False,
                source="reject",
            )
        )
    await session.commit()
    return {
        "id": row.id,
        "dimensions": dimensions,
        "routed_strategy": row.routed_strategy,
        "strategy_round": round_no,
        "needs_negative_example": round_no >= 3,
        "message": "已记录反馈并路由到修正策略" if round_no < 3 else "同维度第 3 次驳回，已沉淀反例",
    }


async def list_feedback(
    session: AsyncSession,
    project_id: int,
    target_type: str | None,
    target_id: int | None,
) -> list[dict[str, Any]]:
    """查询结构化反馈。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        target_type: 对象类型过滤。
        target_id: 对象 ID 过滤。

    Returns:
        反馈列表。
    """
    stmt = select(HumanFeedback).where(HumanFeedback.project_id == project_id)
    if target_type:
        stmt = stmt.where(HumanFeedback.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(HumanFeedback.target_id == target_id)
    rows = (
        await session.execute(stmt.order_by(HumanFeedback.id.desc()).limit(200))
    ).scalars().all()
    return [
        {
            "id": row.id,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "dimensions": row.dimensions,
            "expectation": row.expectation,
            "expectation_text": row.expectation_text,
            "keep_items": row.keep_items,
            "text": row.note_text,
            "source_gate": row.source_gate,
            "routed_strategy": row.routed_strategy,
            "strategy_round": row.strategy_round,
            "adopted": row.adopted,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def create_negative_example(
    session: AsyncSession, project_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """写入反例库（错-改-对三元组）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        payload: 反例字段。

    Returns:
        反例字典。
    """
    row = NegativeExample(
        project_id=project_id,
        dimension=payload["dimension"],
        character_asset_id=payload.get("character_asset_id"),
        scene_ref=payload.get("scene_ref"),
        issue_desc=payload["issue_desc"],
        evidence_ref=payload.get("evidence_ref"),
        fix_action=payload.get("fix_action"),
        passed=bool(payload.get("passed")),
        source=payload.get("source", "reject"),
    )
    session.add(row)
    await session.commit()
    return {
        "id": row.id,
        "dimension": row.dimension,
        "issue_desc": row.issue_desc,
        "fix_action": row.fix_action,
        "passed": row.passed,
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_negative_examples(
    session: AsyncSession, project_id: int, dimension: str | None
) -> list[dict[str, Any]]:
    """查询反例库。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        dimension: 维度过滤。

    Returns:
        反例列表。
    """
    stmt = select(NegativeExample).where(NegativeExample.project_id == project_id)
    if dimension:
        stmt = stmt.where(NegativeExample.dimension == dimension)
    rows = (
        await session.execute(stmt.order_by(NegativeExample.id.desc()).limit(200))
    ).scalars().all()
    return [
        {
            "id": row.id,
            "dimension": row.dimension,
            "scene_ref": row.scene_ref,
            "issue_desc": row.issue_desc,
            "fix_action": row.fix_action,
            "passed": row.passed,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]

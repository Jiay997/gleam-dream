"""F3.13 多维锚点包服务（V6.0 P0）。

把「单张参考图」升级为**一组可追溯的生成约束**：
- 角色：人脸 embedding + 主形象/三视图 + 分场服装描述；
- 场景：场景参考图 + 关键点位/光线基线；
- 道具：状态变体与按场持有人；
- 既有 ``shots.asset_anchor_ids`` 显式锚点。

每项锚点均带 ``ref_type`` / ``ref_id`` / ``ref_kind``，满足 AC-F3-13-6
「输入是锚点包对象而非单张三视图，且可追溯」。

缺项时按 **人脸 → 服装 → 场景 → 道具** 的优先级降级，并在 ``missing``
与 ``degradation`` 中标注，不虚构参考、不冒充完整。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Asset, AssetVersion, Episode, FaceEmbedding, Scene, Shot
from app.models.v6 import StoryBibleCharacter, StoryBibleProp, StoryBibleScene

#: 锚点缺失时的降级优先级（越靠前越关键）
PRIORITY: tuple[str, ...] = ("face", "outfit", "scene", "prop")
#: 角色参考图优先使用的版本类型（主形象优先，其次三视图）
_CHARACTER_VERSION_ORDER: tuple[str, ...] = (
    "portrait",
    "turnaround_front",
    "turnaround_side",
    "turnaround_back",
)


def _pack_hash(items: list[dict[str, Any]]) -> str:
    """计算锚点包引用摘要哈希（供渲染入参与幂等复用）。

    Args:
        items: 锚点项列表。

    Returns:
        64 位十六进制哈希。
    """
    refs = sorted(f"{item.get('ref_type')}:{item.get('ref_id')}" for item in items)
    return hashlib.sha256("|".join(refs).encode("utf-8")).hexdigest()


def _pick_outfit(outfits: Any, scene_no: int) -> dict[str, Any] | None:
    """从 Story Bible 服装定义中取当前场次对应的条目。

    Args:
        outfits: JSONB 服装数组（元素含 ``scene`` / ``desc``）。
        scene_no: 场号。

    Returns:
        匹配的服装条目；无匹配时退回首条或 None。
    """
    if not isinstance(outfits, list) or not outfits:
        return None
    for item in outfits:
        if isinstance(item, dict) and int(item.get("scene") or 0) == int(scene_no):
            return item
    first = outfits[0]
    return first if isinstance(first, dict) else None


def _degradation(missing: list[dict[str, Any]]) -> dict[str, Any]:
    """按优先级生成降级说明。

    Args:
        missing: 缺失项列表（含 ``ref_type``）。

    Returns:
        ``{level, missing_types, note}``；无缺失时 ``level=none``。
    """
    missing_types = {str(item.get("ref_type")) for item in missing}
    ordered = [kind for kind in PRIORITY if kind in missing_types]
    if not ordered:
        return {"level": "none", "missing_types": [], "note": "锚点包完整"}
    return {
        "level": ordered[0],
        "missing_types": ordered,
        "note": f"锚点缺失，按 {ordered[0]} 级降级生成（优先级 人脸>服装>场景>道具）",
    }


async def build_anchor_packs(
    session: AsyncSession,
    project_id: int,
    shots: list[Shot],
) -> dict[int, dict[str, Any]]:
    """批量组装镜头锚点包（一次查询覆盖同一批镜头的全部引用）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        shots: 镜头列表。

    Returns:
        镜头 ID → 锚点包字典（``items/missing/degradation/pack_hash``）。
    """
    if not shots:
        return {}
    episode_ids = sorted({int(shot.episode_id) for shot in shots if shot.episode_id})
    scene_ids = sorted({int(shot.scene_id) for shot in shots if shot.scene_id})
    character_ids: set[int] = set()
    scene_asset_ids: set[int] = set()
    for shot in shots:
        for raw in shot.character_asset_ids or []:
            if str(raw).isdigit():
                character_ids.add(int(raw))
        if shot.scene_asset_id:
            scene_asset_ids.add(int(shot.scene_asset_id))

    episodes = (
        (await session.execute(select(Episode).where(Episode.id.in_(episode_ids))))
        .scalars()
        .all()
        if episode_ids
        else []
    )
    episode_no_by_id = {int(row.id): int(row.episode_no) for row in episodes}
    scenes = (
        (await session.execute(select(Scene).where(Scene.id.in_(scene_ids)))).scalars().all()
        if scene_ids
        else []
    )
    scene_no_by_id = {int(row.id): int(row.scene_no) for row in scenes}

    asset_ids = sorted(character_ids | scene_asset_ids)
    assets = (
        (
            await session.execute(
                select(Asset).where(Asset.id.in_(asset_ids), Asset.project_id == project_id)
            )
        )
        .scalars()
        .all()
        if asset_ids
        else []
    )
    asset_by_id = {int(row.id): row for row in assets}
    versions = (
        (
            await session.execute(
                select(AssetVersion)
                .where(AssetVersion.asset_id.in_(asset_ids), AssetVersion.is_current.is_(True))
                .order_by(AssetVersion.id.desc())
            )
        )
        .scalars()
        .all()
        if asset_ids
        else []
    )
    versions_by_asset: dict[int, list[AssetVersion]] = {}
    for row in versions:
        versions_by_asset.setdefault(int(row.asset_id), []).append(row)
    embeddings = (
        (
            await session.execute(
                select(FaceEmbedding)
                .where(FaceEmbedding.asset_id.in_(sorted(character_ids)))
                .order_by(FaceEmbedding.id.desc())
            )
        )
        .scalars()
        .all()
        if character_ids
        else []
    )
    embedding_by_asset: dict[int, FaceEmbedding] = {}
    for row in embeddings:
        if row.asset_id is not None:
            embedding_by_asset.setdefault(int(row.asset_id), row)
    bible_chars = (
        (
            await session.execute(
                select(StoryBibleCharacter).where(
                    StoryBibleCharacter.project_id == project_id,
                    StoryBibleCharacter.char_asset_id.in_(sorted(character_ids)),
                )
            )
        )
        .scalars()
        .all()
        if character_ids
        else []
    )
    bible_char_by_asset = {
        int(row.char_asset_id): row for row in bible_chars if row.char_asset_id is not None
    }
    bible_scenes = (
        (
            await session.execute(
                select(StoryBibleScene).where(StoryBibleScene.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    bible_scene_map = {
        (int(row.ep_no), int(row.scene_no)): row for row in bible_scenes
    }
    bible_props = (
        (
            await session.execute(
                select(StoryBibleProp).where(StoryBibleProp.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )

    result: dict[int, dict[str, Any]] = {}
    for shot in shots:
        ep_no = episode_no_by_id.get(int(shot.episode_id)) if shot.episode_id else None
        scene_no = scene_no_by_id.get(int(shot.scene_id)) if shot.scene_id else 0
        items: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []

        # ---- 角色：人脸 embedding + 主形象/三视图 + 分场服装 ----
        for asset_id in [int(v) for v in (shot.character_asset_ids or []) if str(v).isdigit()]:
            asset = asset_by_id.get(asset_id)
            if asset is None:
                continue
            embedding = embedding_by_asset.get(asset_id)
            if embedding is not None:
                items.append(
                    {
                        "ref_type": "face",
                        "ref_id": int(embedding.id),
                        "ref_kind": "embedding",
                        "asset_id": asset_id,
                        "source": "face_embeddings",
                        "label": f"{asset.name} 人脸向量",
                    }
                )
            else:
                missing.append(
                    {"ref_type": "face", "asset_id": asset_id, "reason": "缺少人脸 embedding"}
                )
            candidate_versions = sorted(
                versions_by_asset.get(asset_id, []),
                key=lambda row: (
                    _CHARACTER_VERSION_ORDER.index(row.version_type)
                    if row.version_type in _CHARACTER_VERSION_ORDER
                    else 99
                ),
            )
            image_version = next(
                (
                    row
                    for row in candidate_versions
                    if row.media_file_id is not None
                    and row.version_type in _CHARACTER_VERSION_ORDER
                ),
                None,
            )
            if image_version is not None:
                items.append(
                    {
                        "ref_type": "face",
                        "ref_id": int(image_version.id),
                        "ref_kind": "image",
                        "asset_id": asset_id,
                        "source": "asset_versions",
                        "version_type": image_version.version_type,
                        "media_file_id": int(image_version.media_file_id or 0),
                        "label": f"{asset.name} {image_version.version_type}",
                    }
                )
            bible_char = bible_char_by_asset.get(asset_id)
            outfit = _pick_outfit(bible_char.outfits if bible_char else None, scene_no)
            if bible_char is not None and outfit is not None:
                items.append(
                    {
                        "ref_type": "outfit",
                        "ref_id": int(bible_char.id),
                        "ref_kind": "text",
                        "asset_id": asset_id,
                        "source": "story_bible_characters",
                        "scene_no": scene_no,
                        "text": str(outfit.get("desc") or ""),
                        "label": f"{asset.name} 场次服装",
                    }
                )
            else:
                missing.append(
                    {
                        "ref_type": "outfit",
                        "asset_id": asset_id,
                        "reason": "Story Bible 未登记该角色分场服装",
                    }
                )

        # ---- 场景：参考图 + 关键点位/光线基线 ----
        if shot.scene_asset_id:
            scene_asset = asset_by_id.get(int(shot.scene_asset_id))
            scene_versions = versions_by_asset.get(int(shot.scene_asset_id), [])
            scene_ref = next((row for row in scene_versions if row.media_file_id), None)
            if scene_ref is not None:
                items.append(
                    {
                        "ref_type": "scene",
                        "ref_id": int(scene_ref.id),
                        "ref_kind": "image",
                        "asset_id": int(shot.scene_asset_id),
                        "source": "asset_versions",
                        "media_file_id": int(scene_ref.media_file_id or 0),
                        "label": f"{scene_asset.name if scene_asset else '场景'} 参考图",
                    }
                )
            else:
                missing.append(
                    {"ref_type": "scene", "asset_id": int(shot.scene_asset_id), "reason": "场景资产无当前参考图"}
                )
        else:
            missing.append({"ref_type": "scene", "reason": "镜头未绑定场景资产"})
        bible_scene = bible_scene_map.get((int(ep_no or 0), int(scene_no)))
        if bible_scene is not None:
            items.append(
                {
                    "ref_type": "scene",
                    "ref_id": int(bible_scene.id),
                    "ref_kind": "text",
                    "source": "story_bible_scenes",
                    "ep_no": ep_no,
                    "scene_no": scene_no,
                    "key_points": bible_scene.key_points or [],
                    "lighting_base": bible_scene.lighting_base,
                    "label": "场景关键点位/光线基线",
                }
            )

        # ---- 道具：按场持有人 ----
        scene_props = [
            prop
            for prop in bible_props
            if isinstance(prop.holder_by_scene, dict)
            and str(scene_no) in prop.holder_by_scene
        ]
        for prop in scene_props:
            items.append(
                {
                    "ref_type": "prop",
                    "ref_id": int(prop.id),
                    "ref_kind": "state",
                    "source": "story_bible_props",
                    "state": prop.current_state,
                    "holder": (prop.holder_by_scene or {}).get(str(scene_no)),
                    "label": prop.name,
                }
            )
        if not scene_props:
            missing.append({"ref_type": "prop", "reason": "本场无道具锚点"})

        # ---- 显式锚点（shots.asset_anchor_ids） ----
        for anchor_id in [int(v) for v in (shot.asset_anchor_ids or []) if str(v).isdigit()]:
            items.append(
                {
                    "ref_type": "explicit",
                    "ref_id": anchor_id,
                    "ref_kind": "asset_version",
                    "asset_id": anchor_id,
                    "source": "shots.asset_anchor_ids",
                    "label": "镜头显式锚点",
                }
            )

        result[int(shot.id)] = {
            "shot_id": int(shot.id),
            "ep_no": ep_no,
            "scene_no": scene_no,
            "items": items,
            "missing": missing,
            "degradation": _degradation(missing),
            "pack_hash": _pack_hash(items),
        }
    return result


async def build_anchor_pack(
    session: AsyncSession, project_id: int, shot_id: int
) -> dict[str, Any]:
    """组装单个镜头的锚点包。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        shot_id: 镜头 ID。

    Returns:
        锚点包字典。

    Raises:
        BizError: 10002 镜头不存在。
    """
    from app.core.errors import BizError, ErrorCode

    shot = await session.get(Shot, shot_id)
    if shot is None or int(shot.project_id) != int(project_id):
        raise BizError(ErrorCode.NOT_FOUND, "镜头不存在")
    packs = await build_anchor_packs(session, project_id, [shot])
    return packs[int(shot.id)]

"""镜头整幅要素画面的编排服务。

职责链：读取镜头绑定的角色/场景/道具资产 → 推导或复用 CompositionSpec
→ 调合成器产出 PNG → 落 MediaFile → 回写 ``shots.first_frame_media_id``。

设计边界：
- 不负责提交事务与操作历史（由调用方 storyboard_service 负责），便于
  在单测中用 FakeSession 验证编排；
- 合成器通过参数注入，模型精修实现只需替换 ``composer``；
- 资产图片缺失时降级为对应图层缺席并记录，而不是整体失败。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.models.asset import Asset, AssetVersion, MediaFile
from app.models.project import Project
from app.services.frame_composer import (
    ComposeResult,
    FrameComposer,
    LayerImage,
    LocalFrameComposer,
)
from app.services.shot_frame_spec import (
    LAYOUT_VERSION,
    AssetRef,
    CompositionSpec,
    derive_composition_spec,
    layout_issues,
    match_props,
    normalize_spec,
)


@dataclass
class FrameInputs:
    """构图推导所需的镜头快照。

    Attributes:
        shot_no: 镜号。
        shot_size: 景别。
        camera_move: 运镜。
        action_desc: 动作/画面描述。
        dialogue: 对白文本。
        aspect_ratio: 项目画幅。
        characters: 角色资产列表。
        scene: 场景资产（可空）。
        props: 道具资产列表。
        speaker_names: 说话人姓名列表。
        scene_avoid_regions: 场景图规避区（相对坐标，用于虚化/裁掉多余人物）。
        scene_focus_x: 场景裁切横向焦点（0~1，空表示居中）。
        scene_zoom: 场景额外缩放倍率（空表示 1.0）。
    """

    shot_no: int
    shot_size: str
    camera_move: str
    action_desc: str | None
    dialogue: str | None
    aspect_ratio: str
    characters: list[AssetRef] = field(default_factory=list)
    scene: AssetRef | None = None
    props: list[AssetRef] = field(default_factory=list)
    speaker_names: list[str] = field(default_factory=list)
    scene_avoid_regions: list[dict[str, float]] = field(default_factory=list)
    scene_focus_x: float | None = None
    scene_zoom: float | None = None


def build_composition_spec(
    inputs: FrameInputs,
    *,
    existing: CompositionSpec | None = None,
    llm_hint: dict[str, Any] | None = None,
) -> CompositionSpec:
    """推导或复用镜头构图。

    已有人工/LLM 产物（``source`` 为 manual/llm）时直接复用，避免每次
    合成覆盖人工调整；否则按规则推导。

    Args:
        inputs: 镜头快照。
        existing: 已落库的 spec（可空）。
        llm_hint: LLM 构图提示（可空）。

    Returns:
        合法的 CompositionSpec。

    Raises:
        无：内部推导对任何输入都返回合法结果。
    """
    if existing is not None and existing.source == "manual":
        return existing
    base = derive_composition_spec(
        shot_no=inputs.shot_no,
        shot_size=inputs.shot_size,
        camera_move=inputs.camera_move,
        action_desc=inputs.action_desc,
        dialogue=inputs.dialogue,
        aspect_ratio=inputs.aspect_ratio,
        characters=inputs.characters,
        scene=inputs.scene,
        props=inputs.props,
        speaker_names=inputs.speaker_names,
        llm_hint=llm_hint,
    )
    if existing is None:
        return base
    # 规则版本升级后旧 spec 自动重算（否则旧比例/旧锚点会一直复用）
    if int(getattr(existing, "layout_version", 1)) < LAYOUT_VERSION:
        return base
    # LLM/历史 spec 只有在布局可信时才复用：旧版 LLM 会把双人放在 0.15/0.55、
    # 把道具压在人物脸上；这类 spec 若继续复用，修复合成器也救不回画面。
    if not layout_issues(existing):
        return existing
    fallback = normalize_spec(existing.model_dump(), fallback=base)
    return base if layout_issues(fallback) else fallback


async def _resolve_media_key(session: Any, asset_id: int) -> str | None:
    """读取资产参考图的对象键（参考帧准入过滤后）。

    缺陷修复（2026-10-29 多宫格画面）：历史实现直接取
    ``asset.current_version_id``，当最新版本为表情集（九宫格）时，整张
    宫格图会作为"角色图层"被置入首帧合成画面。

    缺陷修复（2026-11-03 背面视图当锚点）：``is_current`` 只指向最新
    审批版本，补生成三视图后背面视图会顶掉正脸定妆照，首帧合成拿到
    背影/残缺人像（实测出现"无脸背影""无头躯干"）。现查询该资产
    **全部带图版本**，经 :func:`reference_guard.pick_safe_reference`
    按 ``portrait → turnaround_front → side → back`` 优先级择优。

    Args:
        session: 数据库会话（含 ``execute``）。
        asset_id: 资产 ID。

    Returns:
        图片对象键；资产/版本/媒体缺失、非图片或全部版本均为宫格类
        素材时返回 None。

    Raises:
        无：任何查询异常由调用方兜底为空图层。
    """
    from app.services import reference_guard

    asset = await session.get(Asset, int(asset_id))
    if asset is None:
        return None
    versions = (
        (await session.execute(
            select(AssetVersion)
            .where(AssetVersion.asset_id == int(asset_id))
            .order_by(AssetVersion.version_no.asc())
        ))
        .scalars()
        .all()
    )
    if not versions:
        return None
    selected, _warnings = reference_guard.pick_safe_reference(
        list(versions), asset_type=str(getattr(asset, "asset_type", "") or "character")
    )
    if selected is None or selected.media_file_id is None:
        return None
    media = await session.get(MediaFile, int(selected.media_file_id))
    if media is None or not media.object_key:
        return None
    mime = str(getattr(media, "mime_type", "") or "").lower()
    key = str(media.object_key).lower()
    if mime and not mime.startswith("image/") and not key.endswith(
        (".png", ".jpg", ".jpeg", ".webp")
    ):
        return None
    return str(media.object_key)


async def _load_refs(session: Any, shot: Any) -> FrameInputs:
    """读取镜头绑定资产并整理为构图输入。

    Args:
        session: 数据库会话。
        shot: 镜头实例。

    Returns:
        构图推导输入（含每项资产的媒体对象键）。

    Raises:
        无：缺失资产按空处理，不阻断合成。
    """
    speaker_names = await _resolve_speakers(session, shot)

    scene_regions: list[dict[str, float]] = []
    scene_focus_x: float | None = None
    scene_zoom: float | None = None
    characters: list[AssetRef] = []
    for raw in shot.character_asset_ids or []:
        if not str(raw).isdigit():
            continue
        asset = await session.get(Asset, int(raw))
        if asset is None:
            continue
        characters.append(
            AssetRef(
                asset_id=int(asset.id),
                name=str(asset.name or f"角色{asset.id}"),
                role="character",
                media_key=await _resolve_media_key(session, int(asset.id)),
            )
        )

    scene: AssetRef | None = None
    if shot.scene_asset_id:
        asset = await session.get(Asset, int(shot.scene_asset_id))
        if asset is not None:
            scene = AssetRef(
                asset_id=int(asset.id),
                name=str(asset.name or f"场景{asset.id}"),
                role="scene",
                media_key=await _resolve_media_key(session, int(asset.id)),
            )
            metadata = getattr(asset, "meta", None) or {}
            if isinstance(metadata, dict):
                raw_regions = metadata.get("avoid_regions")
                if isinstance(raw_regions, list):
                    scene_regions = [item for item in raw_regions if isinstance(item, dict)]
                if isinstance(metadata.get("focus_x"), (int, float)):
                    scene_focus_x = max(0.0, min(1.0, float(metadata["focus_x"])))
                if isinstance(metadata.get("zoom"), (int, float)):
                    scene_zoom = max(1.0, min(2.5, float(metadata["zoom"])))

    props: list[AssetRef] = []
    for raw in shot.prop_asset_ids or []:
        if not str(raw).isdigit():
            continue
        asset = await session.get(Asset, int(raw))
        if asset is None:
            continue
        props.append(
            AssetRef(
                asset_id=int(asset.id),
                name=str(asset.name or f"道具{asset.id}"),
                role="prop",
                media_key=await _resolve_media_key(session, int(asset.id)),
            )
        )
    # 历史分镜未绑定道具时，按画面描述回查项目道具名兜底（如「碗里」→
    # 「关东煮碗」）。否则台词说在吃面、画面却没有碗，叙事道具永远缺席。
    if not props:
        text_blob = " ".join(str(x or "") for x in (shot.action_desc, shot.dialogue))
        if text_blob.strip():
            try:
                project_props = (
                    await session.execute(
                        select(Asset).where(
                            Asset.project_id == int(shot.project_id),
                            Asset.asset_type == "prop",
                        )
                    )
                ).scalars().all()
            except Exception:  # noqa: BLE001 - 道具回查是增强项，失败不阻断合成
                project_props = []
            candidates = [
                AssetRef(
                    asset_id=int(asset.id),
                    name=str(asset.name or f"道具{asset.id}"),
                    role="prop",
                    media_key=await _resolve_media_key(session, int(asset.id)),
                )
                for asset in project_props
            ]
            props = [ref for ref in match_props(candidates, text_blob) if ref.media_key]

    return FrameInputs(
        shot_no=int(shot.shot_no),
        shot_size=str(shot.shot_size or "medium"),
        camera_move=str(shot.camera_move or "static"),
        action_desc=shot.action_desc,
        dialogue=shot.dialogue,
        aspect_ratio=str(getattr(shot, "aspect_ratio", None) or "9:16"),
        characters=characters,
        scene=scene,
        props=props,
        speaker_names=speaker_names,
        scene_avoid_regions=scene_regions,
        scene_focus_x=scene_focus_x,
        scene_zoom=scene_zoom,
    )


async def _resolve_speakers(session: Any, shot: Any) -> list[str]:
    """反查镜头台词归属的说话人姓名。

    分镜表只存 ``dialogue`` 文本，不存说话人；布局需要说话人决定主位，
    因此按（项目 + 台词文本）回查剧本行。查询失败时返回空列表，布局
    退化为"第一个角色为主位"。

    Args:
        session: 数据库会话。
        shot: 镜头实例（需含 project_id/dialogue）。

    Returns:
        说话人姓名列表（0 或 1 个元素）。

    Raises:
        无：查询异常按空列表处理。
    """
    dialogue = str(getattr(shot, "dialogue", "") or "").strip()
    if not dialogue:
        return []
    try:
        from sqlalchemy import select

        from app.models.script import ScriptLine

        result = await session.execute(
            select(ScriptLine.speaker_name)
            .where(
                ScriptLine.project_id == int(shot.project_id),
                ScriptLine.content == dialogue,
                ScriptLine.is_deleted.is_(False),
            )
            .limit(1)
        )
        name = result.scalar_one_or_none()
        return [str(name)] if name else []
    except Exception:  # noqa: BLE001 - 说话人解析是增强项，失败不阻断合成
        return []


async def compose_shot_frame(
    session: Any,
    shot: Any,
    *,
    storage: Any,
    composer: FrameComposer | None = None,
    feedback: str | None = None,
    aspect_ratio: str | None = None,
) -> dict[str, Any]:
    """合成整幅要素画面并绑定为镜头首帧。

    Args:
        session: 数据库会话（调用方负责 commit）。
        shot: 镜头实例。
        storage: 本地存储客户端（提供 save_bytes/object_path）。
        composer: 合成器（默认 LocalFrameComposer）。
        feedback: 人工重出反馈（把 spec 标记为 manual，便于下次复用人工结果）。
        aspect_ratio: 项目画幅（覆盖镜头缺省）。

    Returns:
        ``{shot_id, media_url, object_key, width, height, degraded, placed,
        spec_source, elements}``。

    Raises:
        RuntimeError: 合成器执行失败（由调用方转换为业务错误）。
    """
    inputs = await _load_refs(session, shot)
    if aspect_ratio:
        inputs.aspect_ratio = aspect_ratio
    else:
        # 镜头本身不存画幅，缺省时回读项目设置（16:9 项目不能按 9:16 合成）
        project = await session.get(Project, int(shot.project_id))
        if project is not None and getattr(project, "aspect_ratio", None):
            inputs.aspect_ratio = str(project.aspect_ratio)

    existing = None
    if getattr(shot, "composition_spec", None):
        existing = normalize_spec(
            shot.composition_spec,
            fallback=build_composition_spec(inputs),
        )
    spec = build_composition_spec(inputs, existing=existing)
    if feedback:
        spec = spec.model_copy(update={"source": "manual"})
    # 场景图自带无关人物时，把资产 meta 的规避区/取景注入 spec：合成器
    # 优先收窄取景把边缘路人裁出画外，小块规避区才退化为局部虚化。
    if spec.setting and (
        inputs.scene_avoid_regions or inputs.scene_focus_x is not None or inputs.scene_zoom is not None
    ):
        setting = dict(spec.setting)
        if inputs.scene_avoid_regions:
            setting["avoid_regions"] = inputs.scene_avoid_regions
        if inputs.scene_focus_x is not None:
            setting["focus_x"] = inputs.scene_focus_x
        if inputs.scene_zoom is not None:
            setting["zoom"] = inputs.scene_zoom
        spec = spec.model_copy(update={"setting": setting})

    layers: list[LayerImage] = []
    for ref in [*inputs.characters, *inputs.props]:
        if not ref.media_key:
            continue
        try:
            path = storage.object_path(ref.media_key)
        except Exception:  # noqa: BLE001 - 存储实现差异，统一按缺失处理
            continue
        if Path(path).exists():
            layers.append(
                LayerImage(
                    asset_id=ref.asset_id,
                    name=ref.name,
                    path=Path(path),
                    kind="character" if ref.role == "character" else "prop",
                )
            )

    setting_path: Path | None = None
    if inputs.scene is not None and inputs.scene.media_key:
        try:
            candidate = storage.object_path(inputs.scene.media_key)
        except Exception:  # noqa: BLE001 - 存储实现差异，统一按缺失处理
            candidate = None
        if candidate is not None and Path(candidate).exists():
            setting_path = Path(candidate)

    active_composer = composer or LocalFrameComposer()
    result: ComposeResult = active_composer.compose(
        spec=spec, setting_path=setting_path, layers=layers
    )
    # 场景缺失属于编排层可知的降级（合成器只知道"没拿到基底"，不知道原因）
    degraded = list(result.degraded)
    if setting_path is None and "no_setting" not in degraded:
        degraded.append("no_setting")

    object_key = storage.save_bytes(
        f"frames/{int(shot.project_id)}/{int(shot.shot_no):02d}_{uuid.uuid4().hex[:12]}.png",
        result.png,
    )
    media = MediaFile(
        project_id=int(shot.project_id),
        storage_provider="cos",
        bucket="gleamdream-local",
        object_key=object_key,
        url=f"/files/{object_key}",
        media_type="image",
        mime_type="image/png",
        file_size_bytes=len(result.png),
        width=result.width,
        height=result.height,
        meta={
            "composition": True,
            "degraded": degraded,
            "spec_source": spec.source,
            "placed": result.placed,
        },
    )
    session.add(media)
    await session.flush()

    shot.first_frame_media_id = int(media.id)
    # 把降级标记一并持久化：QC 门禁与前端徽标都从 composition_spec 读取，
    # 不需要回溯 media_files.meta（避免一次合成多次查询）
    shot.composition_spec = {**spec.model_dump(), "degraded": degraded}

    return {
        "shot_id": int(shot.id),
        "media_url": f"/files/{object_key}",
        "object_key": object_key,
        "width": result.width,
        "height": result.height,
        "degraded": degraded,
        "placed": result.placed,
        "renderer": result.renderer,
        "spec_source": spec.source,
        "elements": {
            "setting": spec.setting,
            "subjects": [item.name for item in spec.subjects],
            "props": [item.name for item in spec.props],
        },
    }

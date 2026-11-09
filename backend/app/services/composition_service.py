"""F10 合成编排服务：结构守卫、编译器、幂等计划与双视图。

关键约束（PRD V6.4 F10）：
- **单一语义源**：轨道视图是节点图的投影，切换视图不改变语义；
- **结构守卫**：编译前执行 N1–N8（入口/出口、单入口、无环、引用有效、
  无边悬空等），错误阻止编译，警告仅提示；
- **确定性编译**：节点按拓扑序、参数按字典序规范化，``plan_hash`` 作为
  渲染幂等键；相同图重复编译直接复用计划并累加 ``reused_count``；
- **主路径回写**：仅显式确认后才把节点顺序写回 ``shots.order_index``。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BizError, ErrorCode
from app.models.script import Episode
from app.models.storyboard import Shot
from app.models.v6 import (
    CgEdge,
    CgLayout,
    CgNode,
    CompositionGraph,
    CompositionSwitchLog,
    RenderPlan,
)

logger = logging.getLogger("gleamdream.composition")

#: 可作为成片片段的节点类型
_CLIP_TYPES = {"clip", "title", "audio_overlay", "compose"}


def _canonical_hash(payload: Any) -> str:
    """对任意 JSON 结构做规范化哈希（键排序、紧凑分隔符）。

    Args:
        payload: 可 JSON 序列化对象。

    Returns:
        64 位十六进制 SHA-256。
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _episode(session: AsyncSession, project_id: int, ep_no: int) -> Episode:
    """按项目与集号查询分集。

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


async def _active_graph(
    session: AsyncSession, project_id: int, ep_no: int
) -> CompositionGraph | None:
    """查询当前生效的编排图。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        生效图或 None。
    """
    return (
        await session.execute(
            select(CompositionGraph)
            .where(
                CompositionGraph.project_id == project_id,
                CompositionGraph.ep_no == ep_no,
                CompositionGraph.status == "active",
            )
            .order_by(CompositionGraph.version.desc())
            .limit(1)
        )
    ).scalars().first()


async def _nodes(session: AsyncSession, graph_id: int) -> list[CgNode]:
    """加载图内全部节点。

    Args:
        session: 数据库会话。
        graph_id: 图 ID。

    Returns:
        节点列表（按 order_hint、id 排序）。
    """
    return list(
        (
            await session.execute(
                select(CgNode)
                .where(CgNode.graph_id == graph_id)
                .order_by(CgNode.order_hint, CgNode.id)
            )
        ).scalars().all()
    )


async def _edges(session: AsyncSession, graph_id: int) -> list[CgEdge]:
    """加载图内全部边。

    Args:
        session: 数据库会话。
        graph_id: 图 ID。

    Returns:
        边列表（按 order_hint、id 排序）。
    """
    return list(
        (
            await session.execute(
                select(CgEdge)
                .where(CgEdge.graph_id == graph_id)
                .order_by(CgEdge.order_hint, CgEdge.id)
            )
        ).scalars().all()
    )


async def sync_from_storyboard(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    ep_no: int,
    force: bool = False,
) -> dict[str, Any]:
    """从分镜板镜头序列生成编排图（线性主路径，cut 转场）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        ep_no: 集号。
        force: 已存在生效图时是否强制重建。

    Returns:
        图详情字典。

    Raises:
        BizError: 10002 集不存在/无镜头；10001 图被渲染锁定。
    """
    episode = await _episode(session, project_id, ep_no)
    existing = await _active_graph(session, project_id, ep_no)
    if existing is not None:
        if existing.locked_by_render:
            raise BizError(ErrorCode.PARAM_INVALID, "编排图正在渲染，无法重建")
        if not force:
            return await graph_detail(session, project_id, ep_no)

    shots = (
        await session.execute(
            select(Shot)
            .where(Shot.project_id == project_id, Shot.episode_id == episode.id)
            .order_by(Shot.order_index, Shot.shot_no)
        )
    ).scalars().all()
    if not shots:
        raise BizError(ErrorCode.NOT_FOUND, "该集暂无镜头，无法生成编排图")

    if existing is not None:
        existing.status = "archived"

    version = (
        int(
            (
                await session.execute(
                    select(CompositionGraph.version)
                    .where(
                        CompositionGraph.project_id == project_id,
                        CompositionGraph.ep_no == ep_no,
                    )
                    .order_by(CompositionGraph.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            or 0
        )
        + 1
    )
    graph = CompositionGraph(
        project_id=project_id,
        ep_no=ep_no,
        version=version,
        status="active",
        view_pref="track",
        source="auto",
        created_by=int(user_id) if str(user_id).isdigit() else None,
        activated_at=datetime.now(UTC),
    )
    session.add(graph)
    await session.flush()

    previous: CgNode | None = None
    prev_shot: Shot | None = None
    for index, shot in enumerate(shots):
        node_params: dict[str, Any] = {
            "in_ms": 0,
            "out_ms": shot.duration_ms,
            "duration_ms": shot.duration_ms,
            "shot_no": shot.shot_no,
            "scene_id": shot.scene_id,
        }
        # 2026-11 新增：相邻镜头场景一致性校验。同一场次（scene_id 相同）
        # 却使用了不同场景资产，说明分镜阶段场景绑定错乱（画面会突然换了
        # 房间）。不同场次属于正常硬切，不告警。告警写入节点参数，供编排页
        # 排查与前端提示，不阻断编译。
        if prev_shot is not None and prev_shot.scene_id is not None:
            if (
                prev_shot.scene_id == shot.scene_id
                and prev_shot.scene_asset_id != shot.scene_asset_id
            ):
                warning = {
                    "type": "unexpected_scene_change",
                    "from_asset_id": prev_shot.scene_asset_id,
                    "to_asset_id": shot.scene_asset_id,
                    "message": "同一场次相邻镜头使用了不同场景资产，疑似场景穿帮",
                }
                node_params["scene_warnings"] = [warning]
                logger.warning(
                    "编排场景一致性告警 project_id=%s ep_no=%s shot_no=%s %s",
                    project_id, ep_no, shot.shot_no, warning["message"],
                )
        node = CgNode(
            graph_id=graph.id,
            node_key=f"shot_{shot.id}",
            node_type="clip",
            ref_id=str(shot.id),
            ref_kind="shot",
            params=node_params,
            label=f"镜{shot.shot_no}",
            order_hint=index,
            ui_pos={"x": 40 + index * 200, "y": 160},
            status="normal",
        )
        session.add(node)
        await session.flush()
        if previous is not None:
            session.add(
                CgEdge(
                    graph_id=graph.id,
                    from_node_id=previous.id,
                    to_node_id=node.id,
                    transition="cut",
                    duration_ms=0,
                    order_hint=index,
                    is_main=True,
                )
            )
        previous = node
        prev_shot = shot
    await session.flush()
    await _save_graph_hash(session, graph)
    await session.commit()
    return await graph_detail(session, project_id, ep_no)


async def _save_graph_hash(session: AsyncSession, graph: CompositionGraph) -> str:
    """重算并写回图哈希。

    Args:
        session: 数据库会话。
        graph: 编排图实例。

    Returns:
        图哈希。
    """
    nodes = await _nodes(session, graph.id)
    edges = await _edges(session, graph.id)
    payload = {
        "nodes": [
            {
                "key": node.node_key,
                "type": node.node_type,
                "ref": node.ref_id,
                "params": node.params,
                "status": node.status,
            }
            for node in nodes
        ],
        "edges": [
            {
                "from": next((n.node_key for n in nodes if n.id == edge.from_node_id), ""),
                "to": next((n.node_key for n in nodes if n.id == edge.to_node_id), ""),
                "transition": edge.transition,
                "duration_ms": edge.duration_ms,
            }
            for edge in edges
        ],
    }
    graph.graph_hash = _canonical_hash(payload)
    return graph.graph_hash


async def ensure_graph(
    session: AsyncSession, project_id: int, ep_no: int
) -> CompositionGraph:
    """获取生效图，不存在则自动生成。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。

    Returns:
        编排图 ORM 实例。
    """
    graph = await _active_graph(session, project_id, ep_no)
    if graph is not None:
        return graph
    await sync_from_storyboard(session, project_id, "", ep_no, force=False)
    graph = await _active_graph(session, project_id, ep_no)
    if graph is None:
        raise BizError(ErrorCode.NOT_FOUND, "编排图不存在")
    return graph


def _node_dict(node: CgNode) -> dict[str, Any]:
    """序列化编排节点。

    Args:
        node: 节点 ORM 实例。

    Returns:
        可 JSON 化的字典。
    """
    return {
        "id": node.id,
        "node_key": node.node_key,
        "node_type": node.node_type,
        "ref_id": node.ref_id,
        "ref_kind": node.ref_kind,
        "params": node.params,
        "label": node.label,
        "parent_id": node.parent_id,
        "ui_pos": node.ui_pos,
        "order_hint": node.order_hint,
        "is_stale": node.is_stale,
        "status": node.status,
    }


def _edge_dict(edge: CgEdge, node_map: dict[int, CgNode]) -> dict[str, Any]:
    """序列化编排边（附节点键，便于前端渲染）。

    Args:
        edge: 边 ORM 实例。
        node_map: 节点 ID → 节点映射。

    Returns:
        可 JSON 化的字典。
    """
    return {
        "id": edge.id,
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "from_key": node_map.get(edge.from_node_id).node_key if node_map.get(edge.from_node_id) else None,
        "to_key": node_map.get(edge.to_node_id).node_key if node_map.get(edge.to_node_id) else None,
        "transition": edge.transition,
        "duration_ms": edge.duration_ms,
        "order_hint": edge.order_hint,
        "is_main": edge.is_main,
    }


def _topological_order(nodes: list[CgNode], edges: list[CgEdge]) -> list[CgNode]:
    """对主路径节点做稳定拓扑排序（Kahn，按 order_hint 出队）。

    Args:
        nodes: 节点列表。
        edges: 边列表。

    Returns:
        拓扑序节点列表；存在环时返回已完成排序的部分。

    Raises:
        无：环由结构守卫单独报告。
    """
    node_map = {node.id: node for node in nodes}
    indegree = {node.id: 0 for node in nodes}
    outgoing: dict[int, list[int]] = {node.id: [] for node in nodes}
    for edge in edges:
        if edge.from_node_id in node_map and edge.to_node_id in node_map:
            indegree[edge.to_node_id] += 1
            outgoing[edge.from_node_id].append(edge.to_node_id)

    ready = sorted(
        [node for node in nodes if indegree[node.id] == 0],
        key=lambda n: (n.order_hint, n.id),
    )
    ordered: list[CgNode] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for target in outgoing[current.id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(node_map[target])
        ready.sort(key=lambda n: (n.order_hint, n.id))
    return ordered


async def validate_graph(
    session: AsyncSession, graph: CompositionGraph
) -> dict[str, Any]:
    """执行 N1–N8 结构守卫。

    Args:
        session: 数据库会话。
        graph: 编排图实例。

    Returns:
        含 ``valid`` / ``errors`` / ``warnings`` / ``stats`` 的字典。
    """
    nodes = await _nodes(session, graph.id)
    edges = await _edges(session, graph.id)
    node_map = {node.id: node for node in nodes}
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if not nodes:
        errors.append({"code": "N1", "message": "编排图为空"})

    active_ids = {node.id for node in nodes if node.status != "disabled"}
    entry_ids = [node.id for node in nodes if node.id not in {e.to_node_id for e in edges}]
    exit_ids = [node.id for node in nodes if node.id not in {e.from_node_id for e in edges}]
    entries = [n for n in nodes if n.id in entry_ids and n.id in active_ids]
    if len(entries) != 1:
        errors.append(
            {"code": "N2", "message": f"主路径必须且只能有一个入口，当前 {len(entries)} 个"}
        )
    if not exit_ids:
        errors.append({"code": "N3", "message": "主路径缺少出口节点"})

    for edge in edges:
        if edge.from_node_id not in node_map or edge.to_node_id not in node_map:
            errors.append({"code": "N4", "message": f"边 {edge.id} 引用了不存在的节点"})
        if edge.from_node_id == edge.to_node_id:
            errors.append({"code": "N5", "message": f"边 {edge.id} 存在自环"})

    ordered = _topological_order(nodes, edges)
    if len(ordered) != len(nodes):
        errors.append({"code": "N6", "message": "编排图存在环，无法确定渲染顺序"})

    for node in nodes:
        if node.node_type == "clip" and node.ref_kind == "shot" and node.ref_id:
            shot = await session.get(Shot, int(node.ref_id)) if str(node.ref_id).isdigit() else None
            if shot is None:
                errors.append({"code": "N7", "message": f"节点 {node.node_key} 引用的镜头不存在"})
        if node.node_type in _CLIP_TYPES and node.status == "disabled":
            warnings.append({"code": "N8", "message": f"节点 {node.node_key} 已禁用，将被跳过"})

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "clip_count": sum(1 for n in nodes if n.node_type in _CLIP_TYPES),
            "disabled_count": sum(1 for n in nodes if n.status == "disabled"),
        },
    }


async def graph_detail(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    view: str = "track",
) -> dict[str, Any]:
    """获取编排图双视图详情。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        view: track/node。

    Returns:
        含图、节点、边、布局、校验与轨道投影的字典。
    """
    graph = await ensure_graph(session, project_id, ep_no)
    nodes = await _nodes(session, graph.id)
    edges = await _edges(session, graph.id)
    node_map = {node.id: node for node in nodes}
    ordered = _topological_order(nodes, edges)
    layout = (
        await session.execute(
            select(CgLayout).where(
                CgLayout.graph_id == graph.id, CgLayout.view_type == view
            )
        )
    ).scalars().first()

    track_items = []
    cursor = 0
    for index, node in enumerate(ordered):
        duration = int(node.params.get("duration_ms") or 0)
        track_items.append(
            {
                "node_id": node.id,
                "node_key": node.node_key,
                "type": node.node_type,
                "label": node.label,
                "scene_id": (node.params or {}).get("scene_id"),
                "start_ms": cursor,
                "duration_ms": duration,
                "status": node.status,
                "order_hint": node.order_hint,
            }
        )
        cursor += duration

    return {
        "graph": {
            "id": graph.id,
            "ep_no": graph.ep_no,
            "version": graph.version,
            "status": graph.status,
            "view_pref": graph.view_pref,
            "graph_hash": graph.graph_hash,
            "source": graph.source,
            "locked_by_render": graph.locked_by_render,
        },
        "view": view,
        "nodes": [_node_dict(node) for node in nodes],
        "edges": [_edge_dict(edge, node_map) for edge in edges],
        "track": track_items,
        "layout": layout.payload if layout else {},
        "validation": await validate_graph(session, graph),
        "total_duration_ms": cursor,
    }


async def switch_view(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    ep_no: int,
    to_view: str,
) -> dict[str, Any]:
    """切换视图并记录无损性（节点→轨道可能折叠结构节点）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        ep_no: 集号。
        to_view: 目标视图。

    Returns:
        切换结果与折叠节点清单。
    """
    graph = await ensure_graph(session, project_id, ep_no)
    from_view = graph.view_pref
    nodes = await _nodes(session, graph.id)
    folded = [
        {"id": node.id, "node_key": node.node_key, "node_type": node.node_type}
        for node in nodes
        if to_view == "track" and node.node_type in {"branch", "compose", "group"}
    ]
    lossless = not folded
    graph.view_pref = to_view
    session.add(
        CompositionSwitchLog(
            graph_id=graph.id,
            from_view=from_view,
            to_view=to_view,
            lossless=lossless,
            discarded_nodes=folded,
            actor_user_id=int(user_id) if str(user_id).isdigit() else None,
        )
    )
    await session.commit()
    return {
        "graph_id": graph.id,
        "from_view": from_view,
        "to_view": to_view,
        "lossless": lossless,
        "folded_nodes": folded,
    }


async def create_node(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """新增编排节点（自动分配键与排序，禁止在渲染锁定期修改）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        payload: 节点字段。

    Returns:
        新节点字典。

    Raises:
        BizError: 10001 图被锁定/键重复。
    """
    graph = await ensure_graph(session, project_id, ep_no)
    if graph.locked_by_render:
        raise BizError(ErrorCode.PARAM_INVALID, "编排图正在渲染，无法修改")
    node_key = payload.get("node_key") or f"n_{uuid_like(session, graph.id)}"
    exists = (
        await session.execute(
            select(CgNode.id).where(
                CgNode.graph_id == graph.id, CgNode.node_key == node_key
            )
        )
    ).scalar_one_or_none()
    if exists:
        raise BizError(ErrorCode.PARAM_INVALID, "节点键已存在")
    max_order = max([node.order_hint for node in await _nodes(session, graph.id)] or [0])
    node = CgNode(
        graph_id=graph.id,
        node_key=node_key,
        node_type=payload["node_type"],
        ref_id=payload.get("ref_id"),
        ref_kind=payload.get("ref_kind"),
        params=payload.get("params") or {},
        label=payload.get("label"),
        parent_id=payload.get("parent_id"),
        ui_pos=payload.get("ui_pos"),
        order_hint=int(payload.get("order_hint") or max_order + 1),
    )
    session.add(node)
    await session.flush()
    await _save_graph_hash(session, graph)
    await session.commit()
    return _node_dict(node)


def uuid_like(session: AsyncSession, graph_id: int) -> str:  # noqa: ANN201 - 简单键生成
    """生成图内唯一节点键。

    Args:
        session: 数据库会话（保留参数，便于未来改为序列）。
        graph_id: 图 ID。

    Returns:
        形如 ``n{graph_id}_{时间戳尾数}`` 的键。
    """
    return f"n{graph_id}_{int(datetime.now(UTC).timestamp() * 1000) % 1000000}"


async def update_node(
    session: AsyncSession,
    project_id: int,
    node_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """部分更新编排节点。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        node_id: 节点 ID。
        payload: 待更新字段。

    Returns:
        更新后的节点字典。

    Raises:
        BizError: 10002 节点不存在；10001 图被锁定。
    """
    node = await session.get(CgNode, node_id)
    if node is None:
        raise BizError(ErrorCode.NOT_FOUND, "节点不存在")
    graph = await session.get(CompositionGraph, node.graph_id)
    if graph is None or graph.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "节点不存在")
    if graph.locked_by_render:
        raise BizError(ErrorCode.PARAM_INVALID, "编排图正在渲染，无法修改")
    for field in ("params", "label", "ui_pos", "order_hint", "parent_id", "status"):
        value = payload.get(field)
        if value is not None:
            setattr(node, field, value)
    await session.flush()
    await _save_graph_hash(session, graph)
    await session.commit()
    return _node_dict(node)


async def delete_node(session: AsyncSession, project_id: int, node_id: int) -> dict[str, Any]:
    """删除节点（级联删除关联边，并自动桥接主路径）。

    P1-7 修复：此前直接删除节点会让主路径断开——后续节点失去入边变成
    孤立节点、剩余首节点形成双入口，结构守卫 N1/N2 拦截编译，只能手工
    补边。现在删除前读取该节点的主路径入边/出边，若前驱与后继都存在，
    自动建立「前驱 → 后继」桥接边（沿用原出边转场与时长），保持线性
    主路径完整。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        node_id: 节点 ID。

    Returns:
        ``{deleted, graph_id, bridged_edge}``；无前后驱或无需桥接时
        ``bridged_edge=None``。

    Raises:
        BizError: 10002 节点不存在；10001 图被锁定。
    """
    node = await session.get(CgNode, node_id)
    if node is None:
        raise BizError(ErrorCode.NOT_FOUND, "节点不存在")
    graph = await session.get(CompositionGraph, node.graph_id)
    if graph is None or graph.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "节点不存在")
    if graph.locked_by_render:
        raise BizError(ErrorCode.PARAM_INVALID, "编排图正在渲染，无法修改")

    # ---- P1-7：删除前桥接主路径 ----
    # 主路径优先取 is_main=True 的边；无显式主路径时回退首条边。
    in_edges = list(
        (
            await session.execute(
                select(CgEdge).where(
                    CgEdge.graph_id == graph.id,
                    CgEdge.to_node_id == node_id,
                )
            )
        ).scalars().all()
    )
    out_edges = list(
        (
            await session.execute(
                select(CgEdge).where(
                    CgEdge.graph_id == graph.id,
                    CgEdge.from_node_id == node_id,
                )
            )
        ).scalars().all()
    )
    pred_edge = next((e for e in in_edges if e.is_main), in_edges[0] if in_edges else None)
    succ_edge = next((e for e in out_edges if e.is_main), out_edges[0] if out_edges else None)
    bridged: dict[str, Any] | None = None
    if (
        pred_edge is not None
        and succ_edge is not None
        and int(pred_edge.from_node_id) != int(succ_edge.to_node_id)
    ):
        # 避免前驱与后继之间已有边时重复创建
        duplicate = (
            await session.execute(
                select(CgEdge.id).where(
                    CgEdge.graph_id == graph.id,
                    CgEdge.from_node_id == pred_edge.from_node_id,
                    CgEdge.to_node_id == succ_edge.to_node_id,
                )
            )
        ).scalar_one_or_none()
        if duplicate is None:
            bridge = CgEdge(
                graph_id=graph.id,
                from_node_id=pred_edge.from_node_id,
                to_node_id=succ_edge.to_node_id,
                transition=succ_edge.transition,
                duration_ms=succ_edge.duration_ms,
                order_hint=succ_edge.order_hint,
                is_main=True,
            )
            session.add(bridge)
            await session.flush()
            bridged = {
                "id": int(bridge.id),
                "from_node_id": int(bridge.from_node_id),
                "to_node_id": int(bridge.to_node_id),
                "transition": bridge.transition,
            }

    await session.delete(node)
    await session.flush()
    await _save_graph_hash(session, graph)
    await session.commit()
    return {"deleted": node_id, "graph_id": graph.id, "bridged_edge": bridged}


async def create_edge(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """新增编排边。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        payload: 边字段。

    Returns:
        新边字典。

    Raises:
        BizError: 10002 图/节点不存在；10001 边重复。
    """
    graph = await ensure_graph(session, project_id, ep_no)
    if graph.locked_by_render:
        raise BizError(ErrorCode.PARAM_INVALID, "编排图正在渲染，无法修改")
    from_node = await session.get(CgNode, payload["from_node_id"])
    to_node = await session.get(CgNode, payload["to_node_id"])
    if from_node is None or to_node is None or from_node.graph_id != graph.id or to_node.graph_id != graph.id:
        raise BizError(ErrorCode.NOT_FOUND, "节点不存在")
    duplicate = (
        await session.execute(
            select(CgEdge.id).where(
                CgEdge.graph_id == graph.id,
                CgEdge.from_node_id == from_node.id,
                CgEdge.to_node_id == to_node.id,
            )
        )
    ).scalar_one_or_none()
    if duplicate:
        raise BizError(ErrorCode.PARAM_INVALID, "边已存在")
    edge = CgEdge(
        graph_id=graph.id,
        from_node_id=from_node.id,
        to_node_id=to_node.id,
        transition=payload.get("transition", "cut"),
        duration_ms=int(payload.get("duration_ms") or 0),
        order_hint=int(payload.get("order_hint") or 0),
    )
    session.add(edge)
    await session.flush()
    await _save_graph_hash(session, graph)
    await session.commit()
    return _edge_dict(edge, {from_node.id: from_node, to_node.id: to_node})


async def delete_edge(session: AsyncSession, project_id: int, edge_id: int) -> dict[str, Any]:
    """删除编排边。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        edge_id: 边 ID。

    Returns:
        删除结果。

    Raises:
        BizError: 10002 边不存在。
    """
    edge = await session.get(CgEdge, edge_id)
    if edge is None:
        raise BizError(ErrorCode.NOT_FOUND, "边不存在")
    graph = await session.get(CompositionGraph, edge.graph_id)
    if graph is None or graph.project_id != project_id:
        raise BizError(ErrorCode.NOT_FOUND, "边不存在")
    if graph.locked_by_render:
        raise BizError(ErrorCode.PARAM_INVALID, "编排图正在渲染，无法修改")
    await session.delete(edge)
    await session.flush()
    await _save_graph_hash(session, graph)
    await session.commit()
    return {"deleted": edge_id, "graph_id": graph.id}


async def compile_plan(
    session: AsyncSession,
    project_id: int,
    user_id: str,
    ep_no: int,
    preview: bool,
    force: bool,
) -> dict[str, Any]:
    """编译编排图为不可变渲染计划（幂等）。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        user_id: 操作用户 ID。
        ep_no: 集号。
        preview: 是否试渲。
        force: 图未变时是否强制新建计划。

    Returns:
        含 ``plan_hash`` / ``reused`` / ``plan`` 的字典。

    Raises:
        BizError: 10001 结构校验失败。
    """
    graph = await ensure_graph(session, project_id, ep_no)
    validation = await validate_graph(session, graph)
    if not validation["valid"]:
        raise BizError(ErrorCode.PARAM_INVALID, "编排结构校验失败", data={"errors": validation["errors"]})

    nodes = await _nodes(session, graph.id)
    edges = await _edges(session, graph.id)
    node_map = {node.id: node for node in nodes}
    ordered = _topological_order(nodes, edges)
    main_edges = {
        (edge.from_node_id, edge.to_node_id): edge
        for edge in edges
        if edge.is_main
    }

    segments: list[dict[str, Any]] = []
    cursor = 0
    resources: set[str] = set()
    active_ordered = [node for node in ordered if node.status != "disabled"]
    for index, node in enumerate(active_ordered):
        duration = int((node.params or {}).get("duration_ms") or 0)
        transition = (
            main_edges.get((node.id, active_ordered[index + 1].id))
            if index + 1 < len(active_ordered)
            else None
        )
        segment = {
            "node_key": node.node_key,
            "node_type": node.node_type,
            "ref_kind": node.ref_kind,
            "ref_id": node.ref_id,
            "start_ms": cursor,
            "duration_ms": duration,
            "params": node.params or {},
            "transition": transition.transition if transition else "cut",
            "transition_ms": transition.duration_ms if transition else 0,
        }
        segments.append(segment)
        cursor += duration + (transition.duration_ms if transition else 0)
        if node.ref_kind == "shot" and node.ref_id:
            resources.add(f"shot:{node.ref_id}")
            shot = await session.get(Shot, int(node.ref_id)) if str(node.ref_id).isdigit() else None
            if shot is not None and shot.first_frame_media_id:
                resources.add(f"frame:{shot.first_frame_media_id}")

    plan = {
        "graph_hash": graph.graph_hash,
        "ep_no": ep_no,
        "segments": segments,
        "total_duration_ms": cursor,
        "transition_count": sum(1 for s in segments if s["transition"] != "cut"),
        "resources": sorted(resources),
        "preview": bool(preview),
    }
    plan_hash = _canonical_hash(plan)
    existing = (
        await session.execute(select(RenderPlan).where(RenderPlan.plan_hash == plan_hash))
    ).scalars().first()
    if existing is not None:
        # P1-10 修复：plan_hash 是全局唯一键，force=true 时若仍按新行插入会触发
        # 唯一约束冲突（表现为 500 / 10003）。此处改为“同项目覆盖更新、跨项目
        # 直接复用”，既保证幂等又不产生重复计划。
        if not force:
            existing.reused_count += 1
            existing.status = "reused"
            await session.commit()
            return {
                "plan_hash": plan_hash,
                "reused": True,
                "reused_count": existing.reused_count,
                "plan": existing.plan_json,
                "validation": validation,
            }
        if int(existing.project_id) == int(project_id):
            existing.graph_id = graph.id
            existing.graph_hash = graph.graph_hash or ""
            existing.plan_json = plan
            existing.duration_ms = cursor
            existing.cost_credits = len(segments)
            existing.is_preview = bool(preview)
            existing.status = "compiled"
            await session.commit()
            return {
                "plan_id": int(existing.id),
                "plan_hash": plan_hash,
                "reused": False,
                "forced": True,
                "reused_count": existing.reused_count,
                "plan": plan,
                "validation": validation,
                "compiled_by": user_id,
            }
        return {
            "plan_id": int(existing.id),
            "plan_hash": plan_hash,
            "reused": True,
            "forced": True,
            "reused_count": existing.reused_count,
            "plan": existing.plan_json,
            "validation": validation,
            "note": "内容相同的渲染计划已存在（跨项目复用），无需重复创建",
        }

    record = RenderPlan(
        project_id=project_id,
        graph_id=graph.id,
        graph_hash=graph.graph_hash or "",
        plan_hash=plan_hash,
        plan_json=plan,
        status="compiled",
        cost_credits=len(segments),
        duration_ms=cursor,
        is_preview=preview,
    )
    session.add(record)
    await session.commit()
    return {
        "plan_id": record.id,
        "plan_hash": plan_hash,
        "reused": False,
        "reused_count": 0,
        "plan": plan,
        "validation": validation,
        "compiled_by": user_id,
    }


async def apply_main_order(
    session: AsyncSession,
    project_id: int,
    ep_no: int,
    confirm_impacts: bool,
) -> dict[str, Any]:
    """把主路径 clip 节点顺序回写分镜板 ``shots.order_index``。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        confirm_impacts: 是否已确认影响。

    Returns:
        影响预览或执行结果。

    Raises:
        BizError: 10001 结构校验失败；10002 镜头不存在。
    """
    graph = await ensure_graph(session, project_id, ep_no)
    validation = await validate_graph(session, graph)
    if not validation["valid"]:
        raise BizError(ErrorCode.PARAM_INVALID, "编排结构校验失败", data={"errors": validation["errors"]})
    nodes = await _nodes(session, graph.id)
    edges = await _edges(session, graph.id)
    ordered = [n for n in _topological_order(nodes, edges) if n.node_type == "clip" and n.status != "disabled"]
    impacts: list[dict[str, Any]] = []
    for index, node in enumerate(ordered):
        if not node.ref_id or not str(node.ref_id).isdigit():
            continue
        shot = await session.get(Shot, int(node.ref_id))
        if shot is None:
            raise BizError(ErrorCode.NOT_FOUND, f"镜头 {node.ref_id} 不存在")
        if shot.order_index != index:
            impacts.append(
                {"shot_id": shot.id, "shot_no": shot.shot_no, "from": shot.order_index, "to": index}
            )
    if impacts and not confirm_impacts:
        return {"requires_confirmation": True, "impacts": impacts, "applied": 0}
    for index, node in enumerate(ordered):
        if not node.ref_id or not str(node.ref_id).isdigit():
            continue
        shot = await session.get(Shot, int(node.ref_id))
        if shot is not None:
            shot.order_index = index
    await session.commit()
    return {"requires_confirmation": False, "impacts": impacts, "applied": len(impacts)}


async def list_plans(
    session: AsyncSession, project_id: int, ep_no: int, limit: int = 20
) -> list[dict[str, Any]]:
    """查询某集历史渲染计划。

    Args:
        session: 数据库会话。
        project_id: 项目 ID。
        ep_no: 集号。
        limit: 返回条数上限。

    Returns:
        计划摘要列表。
    """
    rows = (
        await session.execute(
            select(RenderPlan)
            .join(CompositionGraph, CompositionGraph.id == RenderPlan.graph_id)
            .where(
                RenderPlan.project_id == project_id,
                CompositionGraph.ep_no == ep_no,
            )
            .order_by(RenderPlan.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "id": row.id,
            "graph_id": row.graph_id,
            "plan_hash": row.plan_hash,
            "status": row.status,
            "reused_count": row.reused_count,
            "cost_credits": row.cost_credits,
            "duration_ms": row.duration_ms,
            "is_preview": row.is_preview,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


async def compose_from_plan(
    session: AsyncSession,
    project: Any,
    user_id: int,
    ep_no: int,
    plan_hash: str | None = None,
) -> dict[str, Any]:
    """按编排图编译计划发起合成（V6.12 合成入口迁移）。

    先确保本集存在可编译的编排图并生成/复用 ``plan_hash``，再复用后期
    合成管线完成时间线装配与 QC。旧 ``POST /post/compose`` 保留兼容。

    Args:
        session: 数据库会话。
        project: 项目实例（已通过 ACL）。
        user_id: 触发用户 ID。
        ep_no: 集号。
        plan_hash: 期望的计划哈希（可选；不一致时报错）。

    Returns:
        ``{episode_no, plan_hash, reused, compose}``。

    Raises:
        BizError: 40001 本集尚无编排图；10001 计划哈希不一致；
            40001 项目内没有已渲染镜头（由后期合成抛出）。
    """
    graph = await _active_graph(session, int(project.id), ep_no)
    if graph is None:
        raise BizError(ErrorCode.PROJECT_STATE_INVALID, "本集尚无编排图，请先同步或创建")
    compiled = await compile_plan(session, int(project.id), user_id, ep_no, False, False)
    actual_hash = str(compiled.get("plan_hash") or "")
    if plan_hash and plan_hash != actual_hash:
        raise BizError(
            ErrorCode.PARAM_INVALID,
            "计划哈希不一致，请重新编译后再合成",
            data={"expected": plan_hash, "actual": actual_hash},
        )
    # 延迟导入：后期服务依赖较多，避免模块级循环引用
    from app.services import post_service

    result = await post_service.compose(session, project, user_id)
    return {
        "episode_no": ep_no,
        "plan_hash": actual_hash,
        "reused": bool(compiled.get("reused")),
        "compose": result,
    }

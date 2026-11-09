"""F3.13 多维锚点包路由（V6.0 增量）。

提供单镜头锚点包与整集锚点包列表，供前端「生成约束」展示与渲染入参
追溯。锚点包只读，由现有资产版本、人脸向量与 Story Bible 现场组装。

端点：
- ``GET /projects/{pid}/shots/{shot_id}/anchor-pack``；
- ``GET /projects/{pid}/episodes/{ep_no}/anchor-packs``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, get_request_id, project_guard
from app.core.errors import success_envelope
from app.models import Episode, Project, Shot
from app.services import anchor_service

#: 锚点包路由
router = APIRouter(tags=["anchors"])


@router.get("/projects/{project_id}/shots/{shot_id}/anchor-pack")
async def get_shot_anchor_pack(
    shot_id: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询单个镜头的多维锚点包。

    Args:
        shot_id: 镜头 ID。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{shot_id, ep_no, scene_no, items, missing,
        degradation, pack_hash}``。
    """
    data = await anchor_service.build_anchor_pack(session, int(project.id), shot_id)
    return success_envelope(data, request_id)


@router.get("/projects/{project_id}/episodes/{ep_no}/anchor-packs")
async def list_episode_anchor_packs(
    ep_no: int,
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询某集全部镜头的锚点包。

    Args:
        ep_no: 集号。
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{items[], total}``（按镜头顺序）。
    """
    episode = (
        (
            await session.execute(
                select(Episode).where(
                    Episode.project_id == int(project.id), Episode.episode_no == ep_no
                )
            )
        )
        .scalars()
        .first()
    )
    if episode is None:
        return success_envelope({"items": [], "total": 0}, request_id)
    shots = list(
        (
            await session.execute(
                select(Shot)
                .where(
                    Shot.project_id == int(project.id),
                    Shot.episode_id == int(episode.id),
                )
                .order_by(Shot.order_index)
            )
        )
        .scalars()
        .all()
    )
    packs = await anchor_service.build_anchor_packs(session, int(project.id), shots)
    items = [packs[int(shot.id)] for shot in shots if int(shot.id) in packs]
    return success_envelope({"items": items, "total": len(items)}, request_id)

"""项目域路由模块（/projects）。

提供项目 CRUD、状态流转（transition/pause/resume/archive/unarchive）、
快照与回滚、项目成员权限管理（授予/升级/撤销）、以及 SSE 事件流端点。
所有写操作统一返回 ``success_envelope`` 包络；鉴权经
:func:`app.core.deps.project_guard`（owner 或 project_members 三级权限，
归档项目写操作 40004 只读守卫）。

路由变量 ``router``（prefix=/projects, tags=[projects]）由
``app/api/__init__.py`` 聚合引入。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import (
    get_current_user,
    get_db,
    get_request_id,
    project_guard,
    require_project_acl,
)
from app.core.errors import BizError, ErrorCode, success_envelope
from app.infra.event_bus import event_bus
from app.models.project import Project
from app.repositories.project_repo import ProjectRepository
from app.schemas.project import (
    ProjectCreateRequest,
    ProjectPermissionSetRequest,
    ProjectUpdateRequest,
    SnapshotCreateRequest,
    TransitionRequest,
    project_out,
    snapshot_out,
)
from app.services import project_service

#: 项目域路由（主控聚合使用：prefix=/projects, tags=[projects]）
router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("")
async def create_project(
    payload: ProjectCreateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """创建项目（owner=当前用户，status=draft，自动插入 owner 授权）。

    Args:
        payload: 创建请求体（title 必填；budget/集数由服务层校验）。
        user_id: 当前用户 ID（JWT sub）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为项目详情字典（status=draft）。

    Raises:
        BizError: 40005 集数超限；30002 预算超限；20001 未登录。
    """
    project = await project_service.create_project(session, int(user_id), payload, request_id)
    return success_envelope(project_out(project), request_id)


@router.get("")
async def list_projects(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    scope: str = Query(default="created", description="范围 created/joined/all"),
    status: str | None = Query(default=None, max_length=32, description="状态筛选"),
    genre: str | None = Query(default=None, max_length=32, description="题材筛选"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页条数"),
    sort: str = Query(default="-created_at", description="排序字段（- 前缀倒序）"),
) -> dict[str, Any]:
    """分页查询项目列表（scope=created|joined|all，支持状态/题材筛选与排序）。

    Args:
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。
        scope: 可见范围（joined/all 为本人创建或成员包含）。
        status: 状态筛选。
        genre: 题材筛选。
        page: 页码。
        page_size: 每页条数。
        sort: 排序字段。

    Returns:
        成功包络，data 为 ``{"items", "page", "page_size", "total"}``。
    """
    data = await project_service.list_projects(
        session,
        int(user_id),
        scope=scope,
        status=status,
        genre=genre,
        page=page,
        page_size=page_size,
        sort=sort,
    )
    return success_envelope(data, request_id)


@router.get("/{project_id}")
async def get_project(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询项目详情（需 view+ 权限）。

    Args:
        project: ACL 校验通过的项目实例。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为项目详情字典。

    Raises:
        BizError: 10002 项目不存在；20003 无权限。
    """
    _ = session  # 预留：详情聚合（快照/剧本摘要）在后续任务扩展
    return success_envelope(project_out(project), request_id)


@router.patch("/{project_id}")
async def update_project(
    payload: ProjectUpdateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """更新项目基础字段（title/synopsis/budget/config，乐观锁 lock_version）。

    Args:
        payload: 更新请求体（PATCH 语义，仅显式传入字段生效）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为更新后的项目详情。

    Raises:
        BizError: 40004 已归档；10006 乐观锁冲突；30002 预算超限。
    """
    updated = await project_service.update_project(
        session, project, payload, int(user_id), request_id
    )
    return success_envelope(project_out(updated), request_id)


@router.delete("/{project_id}")
async def delete_project(
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """软删除项目（写入 deleted_at）。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"id", "deleted": true}``。
    """
    data = await project_service.delete_project(session, project, int(user_id), request_id)
    return success_envelope(data, request_id)


@router.post("/{project_id}/transition")
async def transition_project(
    payload: TransitionRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """按状态机白名单流转项目状态（非法流转返回 40001 与 allowed 列表）。

    Args:
        payload: 目标状态与原因。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为流转后的项目详情。

    Raises:
        BizError: 40004 已归档；40001 状态不允许该流转。
    """
    updated = await project_service.transition(
        project, payload.target_status, payload.reason, session=session, user_id=int(user_id)
    )
    return success_envelope(project_out(updated), request_id, message="状态已流转")


@router.post("/{project_id}/pause")
async def pause_project(
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """暂停项目（原状态存入 config._paused_before）。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为暂停后的项目详情（status=paused）。
    """
    updated = await project_service.pause_project(session, project, int(user_id))
    return success_envelope(project_out(updated), request_id)


@router.post("/{project_id}/resume")
async def resume_project(
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """恢复暂停或失败的项目。

    - status=paused：还原 config._paused_before 记录的状态；
    - status=failed：还原 config._failed_before 记录的状态（失败恢复入口）。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为恢复后的项目详情。
    """
    updated = await project_service.resume_project(session, project, int(user_id))
    return success_envelope(project_out(updated), request_id)


@router.post("/{project_id}/archive")
async def archive_project(
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """归档项目（终态；重复归档返回 40004）。

    Args:
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为归档后的项目详情（status=archived）。
    """
    updated = await project_service.archive_project(session, project, int(user_id))
    return success_envelope(project_out(updated), request_id)


@router.post("/{project_id}/unarchive")
async def unarchive_project(
    project_id: int,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """恢复归档项目（D16 修复：归档提供显式恢复路径，仅 Owner 可调用）。

    归档为只读终态（ACL 归档守卫拦截 edit/export），故本端点不走
    project_guard("edit")——以 view 档通过 ACL 后显式校验 Owner，再执行
    恢复（还原归档前状态 / 历史归档回退 draft）。

    Args:
        project_id: 项目 ID（路径参数）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为恢复后的项目详情。

    Raises:
        BizError: 10002 项目不存在；20003 非 Owner；40001 项目未处于归档状态。
    """
    project = await require_project_acl(project_id, user_id, session, "view")
    if project.owner_user_id != int(user_id):
        raise BizError(ErrorCode.FORBIDDEN, "仅项目 Owner 可恢复归档项目")
    updated = await project_service.unarchive_project(session, project, int(user_id))
    return success_envelope(project_out(updated), request_id, message="项目已恢复")


# ---- 快照与回滚 ----


@router.post("/{project_id}/snapshots")
async def create_snapshot(
    payload: SnapshotCreateRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """创建手动快照（body.name 可命名；上限 30，超限清理最旧匿名快照）。

    Args:
        payload: 快照名称请求体。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为快照详情字典。
    """
    snapshot = await project_service.create_snapshot(
        session, project, trigger="manual", name=payload.name, user_id=int(user_id)
    )
    await session.commit()
    await session.refresh(snapshot)  # 取回 server default 的 created_at
    return success_envelope(snapshot_out(snapshot), request_id, message="快照已创建")


@router.get("/{project_id}/snapshots")
async def list_snapshots(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询快照列表（创建时间倒序，含 created_at 与 state 摘要）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"items", "total"}``。
    """
    data = await project_service.list_snapshots(session, project.id)
    return success_envelope(data, request_id)


@router.post("/{project_id}/snapshots/{snapshot_id}/rollback")
async def rollback_snapshot(
    snapshot_id: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """回滚项目至指定快照（需 edit+；生成 rollback 快照可再次回滚）。

    Args:
        snapshot_id: 目标快照 ID（路径参数）。
        project: ACL 校验通过的项目实例（需 edit+）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"rolled_back", "message", "project", "snapshot"}``。

    Raises:
        BizError: 10002 快照不存在；40004 已归档。
    """
    snapshot = await ProjectRepository.get_snapshot(session, project.id, snapshot_id)
    if snapshot is None:
        raise BizError(ErrorCode.NOT_FOUND, "快照不存在")
    data = await project_service.rollback_snapshot(session, project, snapshot, int(user_id))
    return success_envelope(data, request_id)


# ---- 项目成员权限 ----


@router.put("/{project_id}/members/{user_id}")
async def set_project_member(
    user_id: int,
    payload: ProjectPermissionSetRequest,
    project: Annotated[Project, Depends(project_guard("edit"))],
    operator_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """设置项目成员权限（view/edit/export，需 edit+；Owner 权限不可变更）。

    Args:
        user_id: 目标用户 ID（路径参数）。
        payload: 权限设置请求体。
        project: ACL 校验通过的项目实例（需 edit+）。
        operator_id: 当前操作者用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为成员授权记录。

    Raises:
        BizError: 10002 用户不存在；20003 目标为 Owner。
    """
    data = await project_service.set_project_permission(
        session, project, int(operator_id), user_id, payload
    )
    return success_envelope(data, request_id, message="权限已更新")


@router.delete("/{project_id}/members/{user_id}")
async def remove_project_member(
    user_id: int,
    project: Annotated[Project, Depends(project_guard("edit"))],
    operator_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """撤销项目成员授权（D14 修复：补齐权限生命周期"撤销"，PR-4 级联口径）。

    撤销后该用户对项目的授权记录删除，后续访问按 ACL 判定为 20003；
    操作落 audit_logs（action=project.permission_revoke）。

    Args:
        user_id: 目标用户 ID（路径参数）。
        project: ACL 校验通过的项目实例（需 edit+）。
        operator_id: 当前操作者用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"revoked", "project_id", "user_id", "removed_permission"}``。

    Raises:
        BizError: 10002 目标不是项目成员；20003 目标为 Owner（权限固定）；
            40004 归档项目只读（先 unarchive 再管理成员）。
    """
    data = await project_service.revoke_project_member(
        session, project, int(operator_id), user_id
    )
    return success_envelope(data, request_id, message="成员授权已撤销")


@router.get("/{project_id}/members")
async def list_project_members(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询项目成员列表（Owner 记录 + project_members 授权）。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"owner_user_id", "items"}``。
    """
    data = await project_service.list_project_members(session, project)
    return success_envelope(data, request_id)


# ---- SSE 事件流 ----


@router.get("/{project_id}/events")
async def project_events(
    project: Annotated[Project, Depends(project_guard("view"))],
    session: Annotated[AsyncSession, Depends(get_db)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """项目 SSE 事件流（需 view+ 权限）。

    事件格式：``id: <seq>\\nevent: <type>\\ndata: <json>\\n\\n``；空闲 15s
    注入心跳注释行 ``: ping``。携带 Last-Event-ID 请求头时先回放其后的
    历史事件（断线补偿）再实时推送；断连时自动清理订阅。

    Args:
        project: ACL 校验通过的项目实例（需 view+）。
        session: 请求级数据库会话（仅用于 ACL 校验，流开始后释放）。
        last_event_id: 客户端最后收到的事件序号（EventSource 自动携带）。

    Returns:
        text/event-stream 的 StreamingResponse。
    """
    project_id = int(project.id)
    # Q-006：ACL 校验完成后立即释放 DB 连接，SSE 不再持有请求级会话。
    await session.close()
    last_id: int | None = None
    if last_event_id and last_event_id.strip().isdigit():
        last_id = int(last_event_id.strip())

    async def event_stream() -> AsyncIterator[str]:
        """SSE 事件生成器：订阅总线并渲染 SSE 帧，断连时清理订阅。

        Yields:
            SSE 帧字符串（事件帧或心跳注释行）。
        """
        try:
            async for record in event_bus.subscribe(project_id, last_id):
                if record.get("heartbeat"):
                    yield ": ping\n\n"
                    continue
                data = json.dumps(record["data"], ensure_ascii=False, default=str)
                yield f"id: {record['id']}\nevent: {record['event']}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            # 客户端断连：await 取消后由 subscribe 的 finally 清理订阅
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

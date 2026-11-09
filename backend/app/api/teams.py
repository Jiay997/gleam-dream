"""团队域路由模块（/teams，P1 基础）。

提供团队创建、我的团队列表、成员邀请（按手机号）与移除（级联清理
项目授权）。所有敏感操作由服务层写 audit_logs；统一返回
``success_envelope`` 包络。

路由变量 ``router``（prefix=/teams, tags=[teams]）由
``app/api/__init__.py`` 聚合引入。项目级成员管理（设置权限/成员列表）
位于 :mod:`app.api.projects`。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, get_db, get_request_id
from app.core.errors import success_envelope
from app.schemas.project import TeamCreateRequest, TeamMemberAddRequest
from app.services import project_service

#: 团队域路由（主控聚合使用：prefix=/teams, tags=[teams]）
router = APIRouter(prefix="/teams", tags=["teams"])


@router.post("")
async def create_team(
    payload: TeamCreateRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """创建团队（创建者为 owner，自动写入团队成员与审计日志）。

    Args:
        payload: 创建请求体（团队名称）。
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为团队详情（my_role=owner）。

    Raises:
        BizError: 20001 未登录。
    """
    data = await project_service.create_team(session, int(user_id), payload)
    return success_envelope(data, request_id)


@router.get("")
async def list_teams(
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """查询我参与的团队列表（含我的角色）。

    Args:
        user_id: 当前用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为团队字典列表。
    """
    data = await project_service.list_my_teams(session, int(user_id))
    return success_envelope(data, request_id)


@router.post("/{team_id}/members")
async def add_team_member(
    team_id: int,
    payload: TeamMemberAddRequest,
    user_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """按手机号邀请用户加入团队（仅 owner/admin；不能管理 owner 角色）。

    Args:
        team_id: 团队 ID（路径参数）。
        payload: 邀请请求体（phone + role member/guest/admin）。
        user_id: 当前操作者用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为团队成员记录。

    Raises:
        BizError: 10002 团队/用户不存在；20003 无权限或目标为 owner。
    """
    data = await project_service.invite_team_member(session, int(user_id), team_id, payload)
    return success_envelope(data, request_id, message="成员已加入团队")


@router.delete("/{team_id}/members/{user_id}")
async def remove_team_member(
    team_id: int,
    user_id: int,
    operator_id: Annotated[str, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> dict[str, Any]:
    """移除团队成员（仅 owner/admin；不能移除 owner），级联删除其项目授权。

    Args:
        team_id: 团队 ID（路径参数）。
        user_id: 被移除用户 ID（路径参数）。
        operator_id: 当前操作者用户 ID。
        session: 请求级数据库会话。
        request_id: 请求 ID。

    Returns:
        成功包络，data 为 ``{"removed", "revoked_project_grants"}``。

    Raises:
        BizError: 10002 团队/成员不存在；20003 无权限或目标为 owner。
    """
    data = await project_service.remove_team_member(session, int(operator_id), team_id, user_id)
    return success_envelope(data, request_id, message="成员已移除")

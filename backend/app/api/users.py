"""用户资料路由模块（Task 3 · 认证与计费域）。

挂载点：主控在 api_router 上以 ``settings.API_V1_PREFIX``（/api/v1）聚合，
最终路径形如 ``GET /api/v1/users/me``。

端点：
- GET   /users/me  当前用户详情 + 积分账户摘要（需登录）；
- PATCH /users/me  更新昵称/头像/偏好（preferences JSONB 浅合并，新手引导 P02）。

路由变量名：``router = APIRouter(prefix="/users", tags=["users"])``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import BizError, ErrorCode, success_envelope
from app.schemas.auth import UserUpdateRequest
from app.services import auth_service

#: 用户子路由（主控聚合：include_router(users.router)）
router = APIRouter(prefix="/users", tags=["users"])


async def current_user_id(user_id: Annotated[str, Depends(get_current_user)]) -> int:
    """依赖：把 get_current_user 返回的字符串用户 ID 转为正整数。

    Args:
        user_id: JWT sub（字符串形式的用户 ID）。

    Returns:
        用户 ID（正整数）。

    Raises:
        BizError: 20001 用户 ID 非法。
    """
    if not user_id.isdigit():
        raise BizError(ErrorCode.UNAUTHORIZED)
    return int(user_id)


@router.get("/me")
async def get_me(
    user_id: Annotated[int, Depends(current_user_id)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """查询当前用户详情与积分账户摘要。

    Args:
        user_id: 当前登录用户 ID（依赖注入）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含 {user, credit}。

    Raises:
        BizError: 10002 用户不存在。
    """
    data = await auth_service.get_profile(session, user_id)
    return success_envelope(data)


@router.patch("/me")
async def patch_me(
    body: UserUpdateRequest,
    user_id: Annotated[int, Depends(current_user_id)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """更新当前用户资料（昵称/头像/偏好，偏好做 JSONB 浅合并）。

    Args:
        body: 请求体（nickname?/avatar_url?/preferences?，至少一项）。
        user_id: 当前登录用户 ID（依赖注入）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含更新后的 {user}。

    Raises:
        BizError: 10001 无可更新字段；10002 用户不存在。
    """
    data = await auth_service.update_profile(
        session,
        user_id,
        nickname=body.nickname,
        avatar_url=body.avatar_url,
        preferences=body.preferences,
    )
    return success_envelope(data)

"""认证路由模块（Task 3 · 认证与计费域）。

挂载点：主控在 api_router 上以 ``settings.API_V1_PREFIX``（/api/v1）聚合，
最终路径形如 ``POST /api/v1/auth/register``。

端点：
- POST /auth/sms-code  发送短信验证码（sandbox 返回 dev_code）；
- POST /auth/register  手机号注册（注册赠送 500 积分）；
- POST /auth/login     密码/短信双通道登录；
- POST /auth/wechat    微信登录 Stub（沙箱模拟 unionid）；
- POST /auth/refresh   刷新访问令牌；
- POST /auth/logout    登出（access/refresh 令牌加入 jti 黑名单，立即失效）。

路由变量名：``router = APIRouter(prefix="/auth", tags=["auth"])``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import success_envelope
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    SmsCodeRequest,
    WechatLoginRequest,
)
from app.services import auth_service

#: 认证子路由（主控聚合：include_router(auth.router)）
router = APIRouter(prefix="/auth", tags=["auth"])

#: 可选 Bearer 凭证（登出时用于把当前 token 加入黑名单）
_optional_bearer = HTTPBearer(auto_error=False, description="Bearer JWT（可选）")


@router.post("/sms-code")
async def post_sms_code(
    body: SmsCodeRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """发送短信验证码。

    同号 60 秒内重复请求返回业务码 10004；沙箱模式响应 data 附带 dev_code。

    Args:
        body: 请求体（phone）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含 {expires_in, dev_code?}。

    Raises:
        BizError: 10004 请求过于频繁。
    """
    data = await auth_service.send_sms_code(session, body.phone)
    return success_envelope(data)


@router.post("/register")
async def post_register(
    body: RegisterRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """手机号 + 验证码注册。

    单事务内创建用户 + 积分账户（赠送 500 积分）+ grant 流水；异常整体回滚。

    Args:
        body: 请求体（phone/sms_code/password?/nickname?）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含双令牌与用户摘要。

    Raises:
        BizError: 10001 验证码错误或已过期 / 手机号已注册。
    """
    data = await auth_service.register(
        session,
        phone=body.phone,
        sms_code=body.sms_code,
        password=body.password,
        nickname=body.nickname,
    )
    return success_envelope(data)


@router.post("/login")
async def post_login(
    body: LoginRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """登录（密码 / 短信双通道）。

    Args:
        body: 请求体（login_type/phone/password?/sms_code?）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含双令牌与用户摘要。

    Raises:
        BizError: 10001 凭证字段缺失/验证码错误；20001 账号或密码错误；
            20004 账号被封禁。
    """
    data = await auth_service.login(
        session,
        login_type=body.login_type,
        phone=body.phone,
        password=body.password,
        sms_code=body.sms_code,
    )
    return success_envelope(data)


@router.post("/wechat")
async def post_wechat_login(
    body: WechatLoginRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """微信登录（沙箱 Stub，按 mock_unionid 幂等：首次建号、再次直接登录）。

    Args:
        body: 请求体（mock_unionid?）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含双令牌与用户摘要。

    Raises:
        BizError: 10002 微信身份绑定的用户不存在；20004 账号被封禁。
    """
    data = await auth_service.wechat_login(session, body.mock_unionid)
    return success_envelope(data)


@router.post("/refresh")
async def post_refresh(
    body: RefreshRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """校验 refresh JWT 并签发新 access 令牌。

    Args:
        body: 请求体（refresh_token）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含 {access_token, token_type, expires_in}。

    Raises:
        BizError: 20001 令牌无效；20002 令牌过期；20004 账号被封禁。
    """
    data = await auth_service.refresh(session, body.refresh_token)
    return success_envelope(data)


@router.post("/logout")
async def post_logout(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)] = None,
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """登出：把当前访问/刷新令牌加入 jti 黑名单，令其立即失效。

    Args:
        credentials: 可选 Bearer 凭证（缺失时仅返回成功，客户端自行清理）。
        body: 可选请求体，支持 ``{"refresh_token": "..."}`` 一并注销刷新令牌。

    Returns:
        成功包络，data 为空对象。
    """
    refresh_token = body.get("refresh_token") if isinstance(body, dict) else None
    data = await auth_service.logout(
        credentials.credentials if credentials else None,
        refresh_token if isinstance(refresh_token, str) else None,
    )
    return success_envelope(data)

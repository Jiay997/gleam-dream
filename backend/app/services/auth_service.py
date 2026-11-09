"""认证服务模块（Task 3 · 认证与计费域）。

职责：
- 短信验证码：进程内存存储 + 同号 60 秒发送限流（sandbox 固定验证码）；
- 注册：单事务内创建 User + CreditAccount(500) + grant 流水，异常整体回滚；
- 登录：密码 / 短信双通道，封禁检查（20004）；
- 微信登录 Stub：按 unionid 查 user_identities，无则建号（覆盖 AC-F1-02-1/2）；
- 刷新令牌 / 登出 / 个人资料查询与更新（preferences JSONB 浅合并）。

事务约定：本模块所有写路径通过 :func:`_transaction` 包裹——
传入会话未开启事务时 ``begin`` 一次并在退出时提交，异常自动回滚；
已处于事务中则沿用外层事务（可组合性）。
"""

from __future__ import annotations

import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_uuid
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    revoke_token,
    verify_password,
)
from app.models import User
from app.repositories.billing_repo import BillingRepository
from app.repositories.user_repo import UserRepository
from app.services import analytics_service

#: 验证码有效期（秒）
SMS_CODE_TTL_SECONDS = 300
#: 同号重复发送验证码的最小间隔（秒）
SMS_SEND_INTERVAL_SECONDS = 60
#: 注册赠送积分
REGISTER_GRANT_CREDITS = 500

#: 进程内存验证码存储：phone -> (code, 到期 unix 时间戳)
_sms_code_store: dict[str, tuple[str, float]] = {}
#: 进程内同号最近发送时刻（monotonic）：phone -> ts
_sms_send_ts: dict[str, float] = {}
#: 验证码/发送限流字典条目上限（防内存无界增长）
_SMS_STORE_MAX = 10_000
#: 登录失败时间窗（秒）与窗口内允许失败次数
_LOGIN_FAILURE_WINDOW = 300.0
_LOGIN_FAILURE_MAX = 5
#: 进程内登录失败记录：phone -> 失败时刻列表（monotonic）
_login_failures: dict[str, list[float]] = {}


def _prune_sms_store() -> None:
    """清理过期验证码与超量条目，防止进程内存无界增长。

    Returns:
        None。
    """
    now = time.time()
    expired = [phone for phone, (_, expire_at) in _sms_code_store.items() if now > expire_at]
    for phone in expired:
        _sms_code_store.pop(phone, None)
    if len(_sms_code_store) > _SMS_STORE_MAX:
        # 超出上限时按到期时间升序淘汰最旧条目
        for phone, _ in sorted(_sms_code_store.items(), key=lambda item: item[1][1])[
            : len(_sms_code_store) - _SMS_STORE_MAX
        ]:
            _sms_code_store.pop(phone, None)
    if len(_sms_send_ts) > _SMS_STORE_MAX:
        for phone, _ in sorted(_sms_send_ts.items(), key=lambda item: item[1])[
            : len(_sms_send_ts) - _SMS_STORE_MAX
        ]:
            _sms_send_ts.pop(phone, None)


def _check_login_rate(phone: str) -> None:
    """登录失败频控：窗口内失败次数超限直接拒绝。

    Args:
        phone: 手机号。

    Raises:
        BizError: 10004 登录尝试过于频繁。
    """
    now = time.monotonic()
    attempts = [ts for ts in _login_failures.get(phone, []) if now - ts < _LOGIN_FAILURE_WINDOW]
    if len(attempts) >= _LOGIN_FAILURE_MAX:
        _login_failures[phone] = attempts
        raise BizError(ErrorCode.RATE_LIMITED, "登录失败次数过多，请稍后再试")
    if attempts:
        _login_failures[phone] = attempts
    else:
        _login_failures.pop(phone, None)


def _record_login_failure(phone: str) -> None:
    """记录一次登录失败（用于频控）。

    Args:
        phone: 手机号。

    Returns:
        None。
    """
    now = time.monotonic()
    attempts = [ts for ts in _login_failures.get(phone, []) if now - ts < _LOGIN_FAILURE_WINDOW]
    attempts.append(now)
    _login_failures[phone] = attempts


@asynccontextmanager
async def _transaction(session: AsyncSession) -> AsyncIterator[None]:
    """事务上下文管理器：统一本服务所有写路径的提交/回滚。

    进入时不强制 ``begin``（规避 SQLAlchemy autobegin 隐式事务导致
    ``session.in_transaction()`` 恒真的误判），退出时显式 ``commit``；
    抛出异常时 ``rollback`` 并向上传播，保证任何写路径失败整体回滚
    （如注册的 用户+账户+流水 三步同生共死）。

    Args:
        session: 异步数据库会话。

    Yields:
        None。
    """
    try:
        yield
        await session.commit()
    except Exception:
        await session.rollback()
        raise


def _iso(dt: datetime | None) -> str | None:
    """把 datetime 转为 ISO8601 字符串（响应序列化用）。

    Args:
        dt: 时间对象（可为 None）。

    Returns:
        ISO 字符串；入参为 None 时返回 None。
    """
    return dt.isoformat() if dt is not None else None


def _account_summary(account: Any) -> dict[str, Any]:
    """构造积分账户摘要 dict（available = balance - frozen）。

    Args:
        account: CreditAccount 实例（避免循环导入，使用鸭子类型）。

    Returns:
        账户摘要字典。
    """
    return {
        "account_id": account.id,
        "balance_credits": account.balance_credits,
        "frozen_credits": account.frozen_credits,
        "available_credits": account.balance_credits - account.frozen_credits,
        "total_granted_credits": account.total_granted_credits,
        "total_consumed_credits": account.total_consumed_credits,
    }


def _user_brief(user: User) -> dict[str, Any]:
    """构造用户摘要 dict（登录/注册响应用，不含敏感字段）。

    Args:
        user: 用户实例。

    Returns:
        用户摘要字典。
    """
    return {
        "id": user.id,
        "phone": user.phone,
        "nickname": user.nickname,
        "avatar_url": user.avatar_url,
        "status": user.status,
        "register_source": user.register_source,
        "created_at": _iso(user.created_at),
    }


def _build_auth_payload(user: User) -> dict[str, Any]:
    """构造登录/注册成功响应：双令牌 + 用户摘要。

    Args:
        user: 用户实例（须已 flush 拿到主键）。

    Returns:
        含 access_token/refresh_token/token_type/expires_in/user 的字典。
    """
    return {
        "access_token": create_access_token(str(user.id)),
        "refresh_token": create_refresh_token(str(user.id)),
        "token_type": "bearer",
        "expires_in": settings.JWT_ACCESS_EXPIRE_MINUTES * 60,
        "user": _user_brief(user),
    }


# ---- 短信验证码 ----


async def send_sms_code(session: AsyncSession, phone: str) -> dict[str, Any]:
    """发送短信验证码（sandbox 固定验证码，存进程内存）。

    同号 60 秒内重复请求抛 10004；sandbox 模式响应附带 dev_code 供测试。

    Args:
        session: 异步数据库会话（本方法不触库，仅为接口一致性保留）。
        phone: 手机号（已通过 schema 校验格式）。

    Returns:
        响应数据：{expires_in, dev_code?}。

    Raises:
        BizError: 10004 请求过于频繁（同号 60 秒内重复发送）。
    """
    _prune_sms_store()
    now = time.monotonic()
    last = _sms_send_ts.get(phone)
    if last is not None and (now - last) < SMS_SEND_INTERVAL_SECONDS:
        raise BizError(ErrorCode.RATE_LIMITED)
    _sms_send_ts[phone] = now

    # sandbox 固定验证码；非 sandbox 使用密码学安全随机数（真实短信通道待接入）
    code = (
        settings.DEV_FIXED_SMS_CODE
        if settings.SANDBOX_MODE
        else f"{secrets.randbelow(1_000_000):06d}"
    )
    _sms_code_store[phone] = (code, time.time() + SMS_CODE_TTL_SECONDS)

    data: dict[str, Any] = {"expires_in": SMS_CODE_TTL_SECONDS}
    if settings.SANDBOX_MODE:
        data["dev_code"] = code
    return data


def _consume_sms_code(phone: str, sms_code: str) -> None:
    """校验并消费短信验证码（一次性，校验通过即删除防重放）。

    Args:
        phone: 手机号。
        sms_code: 用户提交的验证码。

    Raises:
        BizError: 10001 验证码错误或已过期。
    """
    entry = _sms_code_store.get(phone)
    if entry is None:
        raise BizError(ErrorCode.PARAM_INVALID, "验证码错误或已过期")
    code, expire_at = entry
    if time.time() > expire_at:
        _sms_code_store.pop(phone, None)
        raise BizError(ErrorCode.PARAM_INVALID, "验证码错误或已过期")
    if code != sms_code:
        raise BizError(ErrorCode.PARAM_INVALID, "验证码错误或已过期")
    _sms_code_store.pop(phone, None)


# ---- 注册 / 登录 ----


async def _create_user_with_account(
    session: AsyncSession,
    *,
    phone: str | None,
    nickname: str,
    password_hash: str | None,
    register_source: str,
) -> User:
    """在同一事务内创建用户 + 积分账户 + 注册赠送流水（不提交）。

    Args:
        session: 异步数据库会话（须已处于事务中）。
        phone: 手机号（微信注册可为 None）。
        nickname: 昵称。
        password_hash: 密码哈希（可 None）。
        register_source: 注册来源。

    Returns:
        已创建的 User 实例。
    """
    repo = UserRepository(session)
    brepo = BillingRepository(session)
    user = await repo.create(
        phone=phone,
        nickname=nickname,
        password_hash=password_hash,
        register_source=register_source,
    )
    account = await brepo.create_user_account(user.id, initial_balance=REGISTER_GRANT_CREDITS)
    await brepo.add_transaction(
        session,
        account_id=account.id,
        transaction_type="grant",
        amount_credits=REGISTER_GRANT_CREDITS,
        balance_after_credits=account.balance_credits,
        description="注册赠送",
    )
    return user


async def register(
    session: AsyncSession,
    *,
    phone: str,
    sms_code: str,
    password: str | None = None,
    nickname: str | None = None,
) -> dict[str, Any]:
    """手机号注册：校验验证码后单事务创建 用户 + 积分账户(500) + grant 流水。

    Args:
        session: 异步数据库会话。
        phone: 手机号。
        sms_code: 短信验证码。
        password: 可选密码（提供则 bcrypt 哈希存储）。
        nickname: 可选昵称（缺省按手机号尾 4 位生成）。

    Returns:
        登录态响应：{access_token, refresh_token, token_type, expires_in, user}。

    Raises:
        BizError: 10001 验证码错误或已过期 / 手机号已注册。
    """
    repo = UserRepository(session)
    # 先判重再消费验证码：避免已注册手机号白白消耗一次验证码
    if await repo.get_by_phone(phone) is not None:
        raise BizError(ErrorCode.PARAM_INVALID, "手机号已注册")
    _consume_sms_code(phone, sms_code)
    async with _transaction(session):
        user = await _create_user_with_account(
            session,
            phone=phone,
            nickname=nickname if nickname else f"用户{phone[-4:]}",
            password_hash=hash_password(password) if password else None,
            register_source="web",
        )
        await repo.touch_last_login(user)
    # 埋点：注册（埋点失败不影响注册结果）
    await analytics_service.track(
        analytics_service.EVENT_USER_REGISTERED,
        user_id=int(user.id),
        props={"register_source": user.register_source},
    )
    return _build_auth_payload(user)


async def login(
    session: AsyncSession,
    *,
    login_type: str,
    phone: str,
    password: str | None = None,
    sms_code: str | None = None,
) -> dict[str, Any]:
    """登录（密码 / 短信双通道）。

    用户不存在或密码错误统一返回 20001「账号或密码错误」（防枚举）；
    账号被封禁返回 20004；成功后刷新末次登录时间。

    Args:
        session: 异步数据库会话。
        login_type: 登录方式 password / sms。
        phone: 手机号。
        password: 密码（password 通道必填）。
        sms_code: 短信验证码（sms 通道必填）。

    Returns:
        登录态响应：{access_token, refresh_token, token_type, expires_in, user}。

    Raises:
        BizError: 10001 凭证字段缺失/验证码错误；10004 失败次数过多；
            20001 账号或密码错误；20004 账号被封禁。
    """
    _check_login_rate(phone)
    repo = UserRepository(session)
    if login_type == "password":
        if not password:
            raise BizError(ErrorCode.PARAM_INVALID, "密码登录必须提供 password")
        user = await repo.get_by_phone(phone)
        if user is None or not user.password_hash or not verify_password(password, user.password_hash):
            _record_login_failure(phone)
            raise BizError(ErrorCode.UNAUTHORIZED, "账号或密码错误")
    else:
        if not sms_code:
            raise BizError(ErrorCode.PARAM_INVALID, "短信登录必须提供 sms_code")
        try:
            _consume_sms_code(phone, sms_code)
        except BizError:
            _record_login_failure(phone)
            raise
        user = await repo.get_by_phone(phone)
        if user is None:
            _record_login_failure(phone)
            raise BizError(ErrorCode.UNAUTHORIZED, "账号或密码错误")
    if user.status == "banned":
        raise BizError(ErrorCode.ACCOUNT_BANNED)
    _login_failures.pop(phone, None)
    async with _transaction(session):
        await repo.touch_last_login(user)
    return _build_auth_payload(user)


# ---- 微信 Stub 登录 ----


async def wechat_login(session: AsyncSession, mock_unionid: str | None = None) -> dict[str, Any]:
    """微信登录（沙箱 Stub）：按 unionid 查 user_identities 决定登录或建号。

    - 无绑定：单事务创建 用户 + 积分账户(500) + grant 流水 + wechat 身份记录
      （覆盖 AC-F1-02-1 首次登录）；
    - 已绑定：直接登录并刷新末次登录时间（覆盖 AC-F1-02-2 再次登录）。

    Args:
        session: 异步数据库会话。
        mock_unionid: 模拟 unionid；缺省时生成新身份（走首次注册分支）。

    Returns:
        登录态响应：{access_token, refresh_token, token_type, expires_in, user}。

    Raises:
        BizError: 20003 非沙箱环境禁用 Stub 登录；10002 微信身份绑定的用户不存在；
            20004 账号被封禁。
    """
    if not settings.SANDBOX_MODE:
        # 生产环境必须接入真实微信 OAuth，禁止凭 mock_unionid 任意登录
        raise BizError(ErrorCode.FORBIDDEN, "微信登录 Stub 仅允许在沙箱环境使用")
    unionid = mock_unionid if mock_unionid else f"mock-wx-{new_uuid().replace('-', '')[:16]}"
    created = False
    async with _transaction(session):
        repo = UserRepository(session)
        identity = await repo.get_wechat_identity(unionid)
        if identity is None:
            user = await _create_user_with_account(
                session,
                phone=None,
                nickname=f"微信用户{unionid[-6:]}",
                password_hash=None,
                register_source="wechat",
            )
            await repo.create_wechat_identity(user.id, unionid)
            created = True
        else:
            user = await repo.get_by_id(identity.user_id)
            if user is None:
                raise BizError(ErrorCode.NOT_FOUND, "微信身份绑定的用户不存在")
            if user.status == "banned":
                raise BizError(ErrorCode.ACCOUNT_BANNED)
        await repo.touch_last_login(user)
    if created:
        # 埋点：微信首次登录建号视同注册（埋点失败不影响登录结果）
        await analytics_service.track(
            analytics_service.EVENT_USER_REGISTERED,
            user_id=int(user.id),
            props={"register_source": "wechat"},
        )
    return _build_auth_payload(user)


# ---- 令牌刷新 / 登出 ----


async def refresh(session: AsyncSession, refresh_token: str) -> dict[str, Any]:
    """校验 refresh JWT 并签发新 access 令牌。

    Args:
        session: 异步数据库会话。
        refresh_token: 登录时签发的 refresh JWT。

    Returns:
        {access_token, refresh_token, token_type, expires_in}（刷新令牌轮换）。

    Raises:
        BizError: 20001 令牌无效/类型不符/用户不存在；20002 令牌过期；
            20004 账号被封禁。
    """
    payload = decode_token(refresh_token, expected_type="refresh")
    user_id = str(payload.get("sub", ""))
    if not user_id.isdigit():
        raise BizError(ErrorCode.UNAUTHORIZED)
    user = await UserRepository(session).get_by_id(int(user_id))
    if user is None:
        raise BizError(ErrorCode.UNAUTHORIZED)
    if user.status == "banned":
        raise BizError(ErrorCode.ACCOUNT_BANNED)
    return {
        "access_token": create_access_token(user_id),
        # 刷新令牌轮换：每次续期下发新 refresh token，降低长期令牌泄露风险
        "refresh_token": create_refresh_token(user_id),
        "token_type": "bearer",
        "expires_in": settings.JWT_ACCESS_EXPIRE_MINUTES * 60,
    }


async def logout(access_token: str | None = None, refresh_token: str | None = None) -> dict[str, Any]:
    """登出：把当前访问/刷新令牌加入 jti 黑名单（进程内即时失效）。

    Args:
        access_token: 当前请求携带的 access JWT（可空）。
        refresh_token: 当前客户端的 refresh JWT（可空；提供时一并注销）。

    Returns:
        空数据字典（包络 data 为 {}）。
    """
    if access_token:
        revoke_token(access_token)
    if refresh_token:
        revoke_token(refresh_token)
    return {}


# ---- 个人资料 ----


async def get_profile(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """查询当前用户详情 + 积分账户摘要（GET /users/me）。

    Args:
        session: 异步数据库会话。
        user_id: 当前登录用户 ID。

    Returns:
        {user: {...含 preferences}, credit: {账户摘要}|None}。

    Raises:
        BizError: 10002 用户不存在。
    """
    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    account = await BillingRepository(session).get_account_by_user(user_id)
    credit: dict[str, Any] | None = None
    if account is not None:
        credit = _account_summary(account)
    return {
        "user": {
            "id": user.id,
            "phone": user.phone,
            "nickname": user.nickname,
            "avatar_url": user.avatar_url,
            "status": user.status,
            "register_source": user.register_source,
            "preferences": user.preferences,
            "created_at": _iso(user.created_at),
        },
        "credit": credit,
    }


async def update_profile(
    session: AsyncSession,
    user_id: int,
    *,
    nickname: str | None = None,
    avatar_url: str | None = None,
    preferences: dict | None = None,
) -> dict[str, Any]:
    """更新个人资料（PATCH /users/me）。

    preferences 采用 SQL JSONB ``||`` 浅合并（服务端原子合并）；三者全空报 10001。

    Args:
        session: 异步数据库会话。
        user_id: 当前登录用户 ID。
        nickname: 新昵称（None 不更新）。
        avatar_url: 新头像 URL（None 不更新）。
        preferences: 偏好键值对（None 不更新）。

    Returns:
        {user: {...}} 更新后的用户详情。

    Raises:
        BizError: 10001 无可更新字段；10002 用户不存在。
    """
    if nickname is None and avatar_url is None and preferences is None:
        raise BizError(ErrorCode.PARAM_INVALID, "无可更新的字段")
    async with _transaction(session):
        user = await UserRepository(session).update_profile(
            user_id, nickname=nickname, avatar_url=avatar_url, preferences=preferences
        )
    if user is None:
        raise BizError(ErrorCode.NOT_FOUND, "用户不存在")
    return {
        "user": {
            "id": user.id,
            "phone": user.phone,
            "nickname": user.nickname,
            "avatar_url": user.avatar_url,
            "status": user.status,
            "register_source": user.register_source,
            "preferences": user.preferences,
            "created_at": _iso(user.created_at),
        }
    }

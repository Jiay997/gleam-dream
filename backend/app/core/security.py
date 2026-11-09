"""JWT 签发/校验与 bcrypt 密码哈希模块。

- JWT：HS256，payload 含 ``sub``（用户 ID）、``token_type``（access/refresh）、
  ``exp``（过期时间）；校验失败按契约区分 20001（无效）/ 20002（过期）。
- 密码哈希：直接基于 bcrypt 库的薄封装，规避 passlib 与 bcrypt 4.x+ 的
  兼容性告警/运行时错误。
"""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings
from app.core.errors import BizError, ErrorCode

#: JWT 签名算法
ALGORITHM = "HS256"

#: access 令牌类型标识
_TOKEN_TYPE_ACCESS = "access"
#: refresh 令牌类型标识
_TOKEN_TYPE_REFRESH = "refresh"

#: 已注销 token 的 jti 黑名单（jti -> 过期 unix 时间戳；单实例内存实现）
_REVOKED_JTI: dict[str, float] = {}


def _prune_revoked(now: float | None = None) -> None:
    """清理已过期的注销黑名单条目，防止内存增长。

    Args:
        now: 当前 unix 时间戳（None 时取系统时间）。

    Returns:
        None。
    """
    current = now if now is not None else time.time()
    for jti, expire_at in list(_REVOKED_JTI.items()):
        if expire_at <= current:
            _REVOKED_JTI.pop(jti, None)


def revoke_token(token: str) -> bool:
    """把访问令牌加入注销黑名单（登出即时生效）。

    Args:
        token: 待注销的 JWT（access 类型）。

    Returns:
        True 表示已加入黑名单；令牌无效/已过期时返回 False（无需注销）。

    Raises:
        无（内部吞掉解码异常，登出接口保持幂等）。
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return False
    jti = str(payload.get("jti") or "")
    if not jti:
        return False
    exp = float(payload.get("exp") or (time.time() + settings.JWT_ACCESS_EXPIRE_MINUTES * 60))
    _prune_revoked()
    _REVOKED_JTI[jti] = exp
    return True


def _create_token(user_id: str, token_type: str, lifetime: timedelta) -> str:
    """内部工具：按给定类型与有效期签发 JWT。

    Args:
        user_id: 用户 ID（写入 payload.sub）。
        token_type: 令牌类型（access / refresh）。
        lifetime: 有效时长。

    Returns:
        签名后的 JWT 字符串。

    Raises:
        jwt.PyJWTError: 签名过程异常时原样抛出（仅服务端配置错误才会触发）。
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": user_id,
        "token_type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        # jti 用于登出黑名单（避免同 token 注销后继续可用）
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)


def create_access_token(user_id: str) -> str:
    """签发访问令牌（access JWT）。

    Args:
        user_id: 用户 ID。

    Returns:
        JWT 字符串，有效期 settings.JWT_ACCESS_EXPIRE_MINUTES 分钟。
    """
    return _create_token(user_id, _TOKEN_TYPE_ACCESS, timedelta(minutes=settings.JWT_ACCESS_EXPIRE_MINUTES))


def create_refresh_token(user_id: str) -> str:
    """签发刷新令牌（refresh JWT）。

    Args:
        user_id: 用户 ID。

    Returns:
        JWT 字符串，有效期 settings.JWT_REFRESH_EXPIRE_DAYS 天。
    """
    return _create_token(user_id, _TOKEN_TYPE_REFRESH, timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS))


def decode_token(token: str, expected_type: str | None = None) -> dict[str, Any]:
    """解码并校验 JWT。

    Args:
        token: JWT 字符串。
        expected_type: 期望的 token_type（如 "access"）；None 表示不校验类型。

    Returns:
        解码后的 payload 字典（含 sub/token_type/exp/iat）。

    Raises:
        BizError: 20002 token 过期；20001 token 无效或类型不匹配。
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise BizError(ErrorCode.TOKEN_EXPIRED) from exc
    except jwt.InvalidTokenError as exc:
        raise BizError(ErrorCode.UNAUTHORIZED) from exc
    if expected_type is not None and payload.get("token_type") != expected_type:
        # 类型不匹配（如拿 refresh 访问业务接口）按未登录处理
        raise BizError(ErrorCode.UNAUTHORIZED)
    jti = str(payload.get("jti") or "")
    if jti and jti in _REVOKED_JTI:
        # 已登出令牌：在过期前一律拒绝
        raise BizError(ErrorCode.UNAUTHORIZED)
    return payload


# ---- bcrypt 密码哈希（薄封装） ----


def hash_password(password: str) -> str:
    """使用 bcrypt 对密码做哈希。

    Args:
        password: 明文密码。

    Returns:
        bcrypt 哈希字符串（形如 ``$2b$12$...``），可直接存库。
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码与 bcrypt 哈希是否匹配。

    Args:
        password: 明文密码。
        password_hash: 库中保存的 bcrypt 哈希字符串。

    Returns:
        匹配返回 True；哈希格式非法或不匹配返回 False（不抛异常）。
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # 哈希串格式非法（非 bcrypt 格式）按校验失败处理
        return False

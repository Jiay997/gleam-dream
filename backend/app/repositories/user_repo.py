"""用户数据仓储模块（Task 3 · 认证与计费域）。

封装 ``users`` / ``user_identities`` 两张表的读写访问：
- 按手机号 / 主键查询（默认排除软删除行）；
- 创建用户、微信身份绑定；
- 个人资料更新（nickname / avatar_url / preferences JSONB 浅合并）。

SQL 全部经 SQLAlchemy Core/ORM 参数化，禁止字符串拼接。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserIdentity


class UserRepository:
    """用户仓储：聚合用户主表与三方身份表的数据访问。

    Attributes:
        session: 请求级异步数据库会话（事务由服务层控制）。
    """

    def __init__(self, session: AsyncSession) -> None:
        """保存会话引用。

        Args:
            session: 异步数据库会话。
        """
        self.session = session

    async def get_by_id(self, user_id: int) -> User | None:
        """按主键查询存活用户。

        Args:
            user_id: 用户 ID。

        Returns:
            User 实例；不存在或已软删返回 None。
        """
        stmt = select(User).where(User.id == user_id, User.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> User | None:
        """按手机号查询存活用户。

        Args:
            phone: 手机号。

        Returns:
            User 实例；不存在或已软删返回 None。
        """
        stmt = select(User).where(User.phone == phone, User.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(
        self,
        *,
        phone: str | None,
        nickname: str,
        password_hash: str | None = None,
        register_source: str = "web",
    ) -> User:
        """创建用户（不提交，由服务层事务统一提交）。

        Args:
            phone: 手机号（微信等三方注册可为 None）。
            nickname: 昵称。
            password_hash: bcrypt 密码哈希（无密码登录可为 None）。
            register_source: 注册来源 web/wechat/apple/admin。

        Returns:
            已 flush（拿到主键）的 User 实例。
        """
        user = User(
            phone=phone,
            nickname=nickname,
            password_hash=password_hash,
            register_source=register_source,
            status="active",
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def get_wechat_identity(self, unionid: str) -> UserIdentity | None:
        """按 unionid 查询微信身份绑定（provider_user_id 或 union_id 命中均可）。

        Args:
            unionid: 微信 unionid（沙箱为模拟值）。

        Returns:
            UserIdentity 实例；不存在返回 None。
        """
        stmt = select(UserIdentity).where(
            UserIdentity.provider == "wechat",
            UserIdentity.provider_user_id == unionid,
        )
        identity = (await self.session.execute(stmt)).scalar_one_or_none()
        if identity is not None:
            return identity
        stmt2 = select(UserIdentity).where(
            UserIdentity.provider == "wechat",
            UserIdentity.union_id == unionid,
        )
        return (await self.session.execute(stmt2)).scalar_one_or_none()

    async def create_wechat_identity(self, user_id: int, unionid: str) -> UserIdentity:
        """创建微信身份绑定记录（不提交）。

        Args:
            user_id: 归属用户 ID。
            unionid: 微信 unionid（沙箱为模拟值，同时充当 provider_user_id）。

        Returns:
            已 flush 的 UserIdentity 实例。
        """
        identity = UserIdentity(
            user_id=user_id,
            provider="wechat",
            provider_user_id=unionid,
            union_id=unionid,
            raw_profile={"mock": True},
        )
        self.session.add(identity)
        await self.session.flush()
        return identity

    async def touch_last_login(self, user: User) -> None:
        """刷新用户末次登录时间为当前 UTC 时间（不提交）。

        Args:
            user: 用户实例。
        """
        user.last_login_at = datetime.now(timezone.utc)
        await self.session.flush()

    async def update_profile(
        self,
        user_id: int,
        *,
        nickname: str | None = None,
        avatar_url: str | None = None,
        preferences: dict | None = None,
    ) -> User | None:
        """更新个人资料（不提交）。

        preferences 使用 PostgreSQL JSONB ``||`` 操作符做服务端浅合并
        （``coalesce(preferences, '{}'::jsonb) || :prefs``），避免读改写竞态；
        nickname / avatar_url 提供时直接覆盖。

        Args:
            user_id: 用户 ID。
            nickname: 新昵称（None 表示不更新）。
            avatar_url: 新头像 URL（None 表示不更新）。
            preferences: 偏好键值对（None 表示不更新；dict 与库中 JSONB 浅合并）。

        Returns:
            更新后重新查询的 User 实例；用户不存在或已软删返回 None。
        """
        values: dict[str, object] = {}
        if nickname is not None:
            values["nickname"] = nickname
        if avatar_url is not None:
            values["avatar_url"] = avatar_url
        if preferences is not None:
            values["preferences"] = func.coalesce(
                User.preferences, text("'{}'::jsonb")
            ).op("||")(bindparam("prefs", preferences, type_=JSONB))

        stmt = update(User).where(User.id == user_id, User.deleted_at.is_(None)).values(**values)
        result = await self.session.execute(stmt)
        if result.rowcount == 0:
            return None
        return await self.get_by_id(user_id)

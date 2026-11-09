"""计费数据仓储模块（Task 3 · 认证与计费域）。

封装计费域 8 张表的核心数据访问：
- 积分账户：查询（支持 ``FOR UPDATE`` 行锁）、创建、流水追加；
- 冻结记录：按主键 / 幂等键查询、过期扫描；
- 套餐 / 订阅：查询、最新订阅获取；
- 订单 / 支付流水：按订单号 / 商户单号查询（支持 ``FOR UPDATE``）。

资金字段一律 BIGINT 整数；SQL 全部经 SQLAlchemy 参数化。
事务边界由服务层（billing_service）控制，本层不做提交/回滚。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    CreditAccount,
    CreditFreeze,
    CreditTransaction,
    Order,
    Payment,
    Plan,
    Subscription,
)

#: 支持的流水排序键白名单（仅 created_at 正/倒序）
_ALLOWED_SORTS: tuple[str, ...] = ("-created_at", "created_at")


class BillingRepository:
    """计费仓储：聚合积分账户/流水/冻结/套餐/订阅/订单/支付的数据访问。

    Attributes:
        session: 异步数据库会话（事务由服务层控制）。
    """

    def __init__(self, session: AsyncSession) -> None:
        """保存会话引用。

        Args:
            session: 异步数据库会话。
        """
        self.session = session

    # ---- 积分账户 ----

    async def get_account(self, account_id: int, *, for_update: bool = False) -> CreditAccount | None:
        """按主键查询积分账户。

        Args:
            account_id: 账户 ID。
            for_update: True 时加 ``SELECT ... FOR UPDATE`` 行锁。

        Returns:
            CreditAccount 实例；不存在返回 None。
        """
        stmt = select(CreditAccount).where(CreditAccount.id == account_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_account_by_user(
        self, user_id: int, *, for_update: bool = False
    ) -> CreditAccount | None:
        """按用户 ID 查询其个人积分账户（owner_type=user）。

        Args:
            user_id: 用户 ID。
            for_update: True 时加 ``SELECT ... FOR UPDATE`` 行锁。

        Returns:
            CreditAccount 实例；不存在返回 None。
        """
        stmt = select(CreditAccount).where(
            CreditAccount.owner_type == "user",
            CreditAccount.user_id == user_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create_user_account(
        self, user_id: int, *, initial_balance: int = 0
    ) -> CreditAccount:
        """创建用户个人积分账户（不提交）。

        Args:
            user_id: 用户 ID。
            initial_balance: 初始余额（注册赠送等场景）。

        Returns:
            已 flush 的 CreditAccount 实例。
        """
        account = CreditAccount(
            owner_type="user",
            user_id=user_id,
            balance_credits=initial_balance,
            frozen_credits=0,
            total_granted_credits=initial_balance,
            total_consumed_credits=0,
        )
        self.session.add(account)
        await self.session.flush()
        return account

    # ---- 积分流水 ----

    @staticmethod
    async def add_transaction(
        session: AsyncSession,
        *,
        account_id: int,
        transaction_type: str,
        amount_credits: int,
        balance_after_credits: int,
        frozen_after_credits: int | None = None,
        related_freeze_id: int | None = None,
        related_task_id: int | None = None,
        project_id: int | None = None,
        description: str | None = None,
        idempotency_key: str | None = None,
    ) -> CreditTransaction:
        """追加一条积分流水（只追加账本，不提交）。

        Args:
            session: 异步数据库会话。
            account_id: 账户 ID。
            transaction_type: 流水类型 grant/recharge/freeze/unfreeze/settle/refund/expire/adjust。
            amount_credits: 变动额（有符号，≠0）。
            balance_after_credits: 变动后可用余额（≥0）。
            frozen_after_credits: 变动后冻结额（可空）。
            related_freeze_id: 关联冻结 ID（可空）。
            related_task_id: 关联渲染任务 ID（可空）。
            project_id: 关联项目 ID（可空）。
            description: 备注说明。
            idempotency_key: 幂等键（冻结等关键路径使用，部分唯一）。

        Returns:
            已 flush 的 CreditTransaction 实例。

        Raises:
            SQLAlchemyError: 违反唯一/检查约束时抛出（由上层回滚）。
        """
        tx = CreditTransaction(
            account_id=account_id,
            transaction_type=transaction_type,
            amount_credits=amount_credits,
            balance_after_credits=balance_after_credits,
            frozen_after_credits=frozen_after_credits,
            related_freeze_id=related_freeze_id,
            related_task_id=related_task_id,
            project_id=project_id,
            description=description,
            idempotency_key=idempotency_key,
        )
        session.add(tx)
        await session.flush()
        return tx

    async def find_transaction_by_idempotency_key(
        self, idempotency_key: str, *, transaction_type: str | None = None
    ) -> CreditTransaction | None:
        """按幂等键查询流水（用于冻结幂等重放判定）。

        Args:
            idempotency_key: 幂等键。
            transaction_type: 限定流水类型（None 不过滤）。

        Returns:
            CreditTransaction 实例；不存在返回 None。
        """
        stmt = select(CreditTransaction).where(CreditTransaction.idempotency_key == idempotency_key)
        if transaction_type is not None:
            stmt = stmt.where(CreditTransaction.transaction_type == transaction_type)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_transactions(
        self,
        account_id: int,
        *,
        page: int,
        page_size: int,
        transaction_type: str | None = None,
        sort: str = "-created_at",
    ) -> tuple[list[CreditTransaction], int]:
        """分页查询账户流水。

        Args:
            account_id: 账户 ID。
            page: 页码（从 1 开始）。
            page_size: 每页条数（1~100）。
            transaction_type: 流水类型筛选（None 表示全部）。
            sort: 排序，仅支持 "-created_at"（默认，新在前）或 "created_at"。

        Returns:
            (当前页流水列表, 总条数)。

        Raises:
            ValueError: sort 不在白名单内。
        """
        if sort not in _ALLOWED_SORTS:
            raise ValueError(f"unsupported sort: {sort}")
        created_at_col = CreditTransaction.created_at
        order_by = created_at_col.desc() if sort == "-created_at" else created_at_col.asc()

        conditions = [CreditTransaction.account_id == account_id]
        if transaction_type is not None:
            conditions.append(CreditTransaction.transaction_type == transaction_type)

        base: Select = select(CreditTransaction).where(*conditions)
        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()
        stmt = base.order_by(order_by).offset((page - 1) * page_size).limit(page_size)
        items = list((await self.session.execute(stmt)).scalars().all())
        return items, int(total)

    # ---- 冻结记录 ----

    async def get_freeze(self, freeze_id: int, *, for_update: bool = False) -> CreditFreeze | None:
        """按主键查询冻结记录。

        Args:
            freeze_id: 冻结 ID。
            for_update: True 时加行锁。

        Returns:
            CreditFreeze 实例；不存在返回 None。
        """
        stmt = select(CreditFreeze).where(CreditFreeze.id == freeze_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_frozen_expired(self, now: datetime) -> list[CreditFreeze]:
        """查询所有已超时仍处于 frozen 状态的冻结记录（不加锁，逐条处理时再加锁）。

        Args:
            now: 当前 UTC 时间。

        Returns:
            超时冻结记录列表。
        """
        stmt = (
            select(CreditFreeze)
            .where(CreditFreeze.status == "frozen", CreditFreeze.expires_at < now)
            .order_by(CreditFreeze.expires_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ---- 套餐与订阅 ----

    async def list_plans(self) -> list[Plan]:
        """查询全部在售套餐（按展示排序升序）。

        Returns:
            Plan 列表。
        """
        stmt = select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_plan_by_key(self, key: str) -> Plan | None:
        """按套餐键查询套餐（不筛在售状态，由调用方判断）。

        Args:
            key: 套餐键 free/pro/studio。

        Returns:
            Plan 实例；不存在返回 None。
        """
        stmt = select(Plan).where(Plan.key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_latest_subscription(self, user_id: int, plan_id: int | None = None) -> Subscription | None:
        """查询用户某套餐最近一条订阅（任意状态，用于续费判定）。

        Args:
            user_id: 用户 ID。
            plan_id: 套餐 ID（None 表示不限套餐，取最近一条）。

        Returns:
            Subscription 实例；不存在返回 None。
        """
        stmt = select(Subscription).where(
            Subscription.owner_type == "user",
            Subscription.user_id == user_id,
        )
        if plan_id is not None:
            stmt = stmt.where(Subscription.plan_id == plan_id)
        stmt = stmt.order_by(Subscription.started_at.desc(), Subscription.id.desc()).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ---- 订单与支付 ----

    async def get_order_by_no(self, order_no: str, *, for_update: bool = False) -> Order | None:
        """按订单号查询订单。

        Args:
            order_no: 订单号（业务唯一）。
            for_update: True 时加行锁（支付回调串行化）。

        Returns:
            Order 实例；不存在返回 None。
        """
        stmt = select(Order).where(Order.order_no == order_no)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_payment_by_out_trade_no(self, out_trade_no: str) -> Payment | None:
        """按商户订单号查询支付流水（out_trade_no 全局唯一，幂等键）。

        Args:
            out_trade_no: 商户订单号。

        Returns:
            Payment 实例；不存在返回 None。
        """
        stmt = select(Payment).where(Payment.out_trade_no == out_trade_no)
        return (await self.session.execute(stmt)).scalar_one_or_none()

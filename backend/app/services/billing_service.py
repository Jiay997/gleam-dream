"""计费服务模块（Task 3 · 认证与计费域，事务正确性优先）。

核心资金操作（全部在单事务内完成 账户更新 + 冻结 + 流水 三件套）：
- :func:`precheck_and_freeze`：余额预检 + ``FOR UPDATE`` 行锁 + 冻结（幂等键支持）；
- :func:`settle`：按实际用量结算冻结额，差额自动解冻；
- :func:`refund`：全额释放冻结额；
- :func:`expire_stale_freezes`：超时冻结批量解冻（API 惰性触发，60 秒限流一次）；
- 订单 / 支付回调（out_trade_no 幂等 + HMAC 签名）/ 套餐订阅开通（仅沙箱）。

安全约束（生产必读）：
- 非沙箱环境 ``create_subscription_direct`` 一律拒绝（防止跳过支付）；
- 支付回调优先校验 ``PAYMENT_WEBHOOK_SECRET`` 的 HMAC 签名；未配置密钥时
  仅沙箱接受固定签名，生产绝不接受；
- 充值包积分上限 ``MAX_CREDIT_PACK_CREDITS``，防止构造超大额零成本订单。

账务约定（契约）：
- 资金字段一律 BIGINT 整数积分，禁止浮点；
- ``balance_credits`` 只在结算（settle）与入账（grant/recharge）时变动，
  冻结只挪入 ``frozen_credits``；
- freeze 流水 amount 记负值（便于追溯），balance_after 记当前 balance；
- 任何写路径失败由 :func:`_transaction` 统一回滚。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import BizError, ErrorCode
from app.core.ids import new_uuid, sha256_hash
from app.models import CreditAccount, CreditFreeze, Plan, Project
from app.repositories.billing_repo import BillingRepository

#: 冻结默认有效期（分钟，契约：30 分钟自动解冻）
FREEZE_TTL_MINUTES = 30
#: 长耗时视频任务冻结有效期（分钟）：轮询 600s × QC 3 轮 + 重试需覆盖
FREEZE_TTL_BY_TASK: dict[str, int] = {
    "text_to_video": 120,
    "image_to_video": 120,
    "video": 120,
}
#: 单笔积分充值包上限（防创建超大额订单）
MAX_CREDIT_PACK_CREDITS = 100_000
#: 惰性过期扫描限流间隔（秒）
_EXPIRE_SCAN_INTERVAL_SECONDS = 60.0
#: 沙箱支付回调固定签名值（真实渠道接入前，sandbox 联调用）
SANDBOX_WEBHOOK_SIGN = "gleamdream-sandbox-sign"
#: 沙箱模拟支付网关域名（构造模拟支付链接）
_SANDBOX_PAY_HOST = "sandbox.pay.gleamdream.local"

#: 惰性过期扫描的进程内限流时间戳（monotonic）
_last_expire_scan_ts: float = 0.0


@asynccontextmanager
async def _transaction(session: AsyncSession) -> AsyncIterator[None]:
    """事务上下文管理器：统一本服务所有写路径的提交/回滚。

    进入时不强制 ``begin``（规避 SQLAlchemy autobegin 隐式事务导致
    ``session.in_transaction()`` 恒真的误判），退出时显式 ``commit``；
    抛出异常时 ``rollback`` 并向上传播，保证任何写路径失败整体回滚。
    本服务的全部调用点均为顶层方法（API handler / 任务入口），
    不会出现外层事务包裹本上下文的嵌套场景。

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


def _account_summary(account: CreditAccount) -> dict[str, Any]:
    """构造积分账户摘要 dict。

    Args:
        account: 积分账户实例。

    Returns:
        含 available（balance - frozen）的账户摘要字典。
    """
    return {
        "account_id": account.id,
        "owner_type": account.owner_type,
        "balance_credits": account.balance_credits,
        "frozen_credits": account.frozen_credits,
        "available_credits": account.balance_credits - account.frozen_credits,
        "total_granted_credits": account.total_granted_credits,
        "total_consumed_credits": account.total_consumed_credits,
    }


# ---- 核心：冻结 / 结算 / 退款 / 过期 ----


async def precheck_and_freeze(
    session: AsyncSession,
    *,
    account_id: int,
    project_id: int | None,
    task_type: str,
    estimated_credits: int,
    idempotency_key: str | None = None,
    ttl_minutes: int | None = None,
) -> CreditFreeze:
    """余额预检并冻结积分（渲染/生成任务计费入口）。

    流程：
    1. 幂等：按 idempotency_key 查历史 freeze 流水，命中直接返回原冻结记录；
    2. ``SELECT ... FOR UPDATE`` 锁账户行，available = balance - frozen；
    3. available < estimated → 30001（data 携带 needed/available）；
    4. 单事务内：frozen += estimated、建 CreditFreeze（按任务类型取 TTL）、写 freeze 流水。

    Args:
        session: 异步数据库会话。
        account_id: 积分账户 ID。
        project_id: 关联项目 ID（可空）。
        task_type: 任务类型（写入流水备注，并决定冻结 TTL）。
        estimated_credits: 预估积分（必须 > 0）。
        idempotency_key: 幂等键（可空；建议 sha256(project_id+task+input_hash)）。
        ttl_minutes: 显式冻结时长（分钟）；None 时按任务类型取默认值。

    Returns:
        新建或幂等命中的 CreditFreeze 实例（status=frozen）。

    Raises:
        BizError: 10001 参数非法；30001 积分不足；30005 账户/冻结记录不存在。
    """
    if estimated_credits <= 0:
        raise BizError(ErrorCode.PARAM_INVALID, "预估积分必须大于 0")
    brepo = BillingRepository(session)
    ttl = ttl_minutes or FREEZE_TTL_BY_TASK.get(task_type, FREEZE_TTL_MINUTES)

    # 1. 幂等重放：按幂等键定位历史 freeze 流水 → 原冻结记录
    if idempotency_key:
        freeze = await find_freeze_by_idempotency_key(session, idempotency_key)
        if freeze is not None:
            return freeze

    try:
        async with _transaction(session):
            # 2. 行锁预检
            account = await brepo.get_account(account_id, for_update=True)
            if account is None:
                raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")
            available = account.balance_credits - account.frozen_credits
            if available < estimated_credits:
                raise BizError(
                    ErrorCode.CREDIT_INSUFFICIENT,
                    data={"needed": estimated_credits, "available": available},
                )
            # 3. 冻结三件套：账户 + 冻结记录 + 流水（同事务）
            account.frozen_credits += estimated_credits
            account.lock_version += 1
            now = datetime.now(timezone.utc)
            freeze = CreditFreeze(
                account_id=account.id,
                project_id=project_id,
                frozen_credits=estimated_credits,
                status="frozen",
                expires_at=now + timedelta(minutes=ttl),
            )
            session.add(freeze)
            await session.flush()
            await BillingRepository.add_transaction(
                session,
                account_id=account.id,
                transaction_type="freeze",
                amount_credits=-estimated_credits,  # 约定：冻结流水记负值便于追溯
                balance_after_credits=account.balance_credits,
                frozen_after_credits=account.frozen_credits,
                related_freeze_id=freeze.id,
                project_id=project_id,
                description=f"计费冻结:{task_type}",
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        # 并发同幂等键：唯一索引兜底后重新查历史冻结，保证幂等语义
        await session.rollback()
        if idempotency_key:
            existing = await find_freeze_by_idempotency_key(session, idempotency_key)
            if existing is not None:
                return existing
        raise
    return freeze


async def find_freeze_by_idempotency_key(
    session: AsyncSession, idempotency_key: str
) -> CreditFreeze | None:
    """按幂等键查找历史冻结记录（经 freeze 流水反查）。

    Args:
        session: 异步数据库会话。
        idempotency_key: 幂等键。

    Returns:
        CreditFreeze 实例；无历史记录或记录异常缺失时返回 None。
    """
    brepo = BillingRepository(session)
    tx = await brepo.find_transaction_by_idempotency_key(idempotency_key, transaction_type="freeze")
    if tx is None or tx.related_freeze_id is None:
        return None
    return await brepo.get_freeze(int(tx.related_freeze_id))


async def settle(session: AsyncSession, *, freeze_id: int, actual_credits: int) -> dict[str, Any]:
    """按实际用量结算冻结额（任务成功完成时调用）。

    单事务内：freeze → settled；balance -= actual；frozen -= 原冻结额；
    写 settle 流水（amount=-actual）+ 差额 unfreeze 流水（amount=+差额，差额为 0 不写）。

    Args:
        session: 异步数据库会话。
        freeze_id: 冻结记录 ID。
        actual_credits: 实际消耗积分（1 ≤ actual ≤ 原冻结额）。

    Returns:
        结算明细：{freeze_id, actual_credits, released_credits, balance_credits,
        frozen_credits, status}。

    Raises:
        BizError: 10001 参数非法；30005 冻结记录不存在或状态不允许结算。
    """
    if actual_credits <= 0:
        raise BizError(ErrorCode.PARAM_INVALID, "实际结算积分必须大于 0")
    brepo = BillingRepository(session)
    async with _transaction(session):
        freeze = await brepo.get_freeze(freeze_id, for_update=True)
        if freeze is None:
            raise BizError(ErrorCode.FREEZE_NOT_FOUND)
        if freeze.status != "frozen":
            raise BizError(ErrorCode.FREEZE_NOT_FOUND, "冻结记录不存在或状态不允许结算")
        if actual_credits > freeze.frozen_credits:
            raise BizError(ErrorCode.PARAM_INVALID, "实际结算积分不能超过冻结额")

        account = await brepo.get_account(freeze.account_id, for_update=True)
        if account is None:
            raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")

        frozen_before = freeze.frozen_credits
        diff = frozen_before - actual_credits

        # 账户：结算扣余额 + 释放全部冻结额
        account.balance_credits -= actual_credits
        account.frozen_credits -= frozen_before
        account.total_consumed_credits += actual_credits
        account.lock_version += 1

        # 项目累计消耗：所有冻结都带 project_id 时统一在这里累加，避免各
        # 业务服务用独立 session 结算时漏记（配音轨道曾因绕过编排结算导致
        # 项目 spent_credits 长期为 0，与流水对不上）。
        if freeze.project_id:
            project = await session.get(Project, int(freeze.project_id))
            if project is not None:
                project.spent_credits = int(project.spent_credits or 0) + actual_credits
                project.lock_version += 1

        now = datetime.now(timezone.utc)
        freeze.status = "settled"
        freeze.settled_credits = actual_credits
        freeze.settled_at = now

        await BillingRepository.add_transaction(
            session,
            account_id=account.id,
            transaction_type="settle",
            amount_credits=-actual_credits,
            balance_after_credits=account.balance_credits,
            frozen_after_credits=account.frozen_credits,
            related_freeze_id=freeze.id,
            project_id=freeze.project_id,
            description=f"任务结算:{frozen_before}->{actual_credits}",
        )
        if diff > 0:
            await BillingRepository.add_transaction(
                session,
                account_id=account.id,
                transaction_type="unfreeze",
                amount_credits=diff,
                balance_after_credits=account.balance_credits,
                frozen_after_credits=account.frozen_credits,
                related_freeze_id=freeze.id,
                project_id=freeze.project_id,
                description="结算差额解冻",
            )
    return {
        "freeze_id": freeze.id,
        "actual_credits": actual_credits,
        "released_credits": diff,
        "balance_credits": account.balance_credits,
        "frozen_credits": account.frozen_credits,
        "status": freeze.status,
    }


async def refund(session: AsyncSession, *, freeze_id: int, reason: str = "任务失败退款") -> dict[str, Any]:
    """全额释放冻结额（任务失败/取消时调用，冻结积分全部返还可用余额）。

    单事务内：freeze → released；frozen -= 原冻结额（balance 不变）；
    写 unfreeze 流水（amount=+原冻结额）。

    Args:
        session: 异步数据库会话。
        freeze_id: 冻结记录 ID。
        reason: 释放原因（写入流水备注）。

    Returns:
        释放明细：{freeze_id, released_credits, balance_credits, frozen_credits, status}。

    Raises:
        BizError: 30005 冻结记录不存在或状态不允许释放。
    """
    brepo = BillingRepository(session)
    async with _transaction(session):
        freeze = await brepo.get_freeze(freeze_id, for_update=True)
        if freeze is None:
            raise BizError(ErrorCode.FREEZE_NOT_FOUND)
        if freeze.status != "frozen":
            raise BizError(ErrorCode.FREEZE_NOT_FOUND, "冻结记录不存在或状态不允许释放")
        account = await brepo.get_account(freeze.account_id, for_update=True)
        if account is None:
            raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")

        frozen_amount = freeze.frozen_credits
        account.frozen_credits -= frozen_amount
        account.lock_version += 1

        freeze.status = "released"
        freeze.released_at = datetime.now(timezone.utc)

        await BillingRepository.add_transaction(
            session,
            account_id=account.id,
            transaction_type="unfreeze",
            amount_credits=frozen_amount,
            balance_after_credits=account.balance_credits,
            frozen_after_credits=account.frozen_credits,
            related_freeze_id=freeze.id,
            project_id=freeze.project_id,
            description=f"冻结释放:{reason}",
        )
    return {
        "freeze_id": freeze.id,
        "released_credits": frozen_amount,
        "balance_credits": account.balance_credits,
        "frozen_credits": account.frozen_credits,
        "status": freeze.status,
    }


async def expire_stale_freezes(session: AsyncSession) -> int:
    """扫描并释放全部超时冻结（expires_at < now 且仍 frozen）。

    单事务内逐条：freeze → released；frozen -= 冻结额；写 unfreeze 流水（备注超时）。
    供 API 计费预检前惰性调用（见 :func:`lazy_expire_stale_freezes` 限流），
    也可由定时任务全量调用。

    Args:
        session: 异步数据库会话。

    Returns:
        本轮释放的冻结记录条数。
    """
    now = datetime.now(timezone.utc)
    brepo = BillingRepository(session)
    async with _transaction(session):
        stale = await brepo.list_frozen_expired(now)
        count = 0
        for freeze in stale:
            locked = await brepo.get_freeze(freeze.id, for_update=True)
            if locked is None or locked.status != "frozen":
                continue
            account = await brepo.get_account(locked.account_id, for_update=True)
            if account is None:
                continue
            frozen_amount = locked.frozen_credits
            account.frozen_credits -= frozen_amount
            account.lock_version += 1
            locked.status = "released"
            locked.released_at = now
            await BillingRepository.add_transaction(
                session,
                account_id=account.id,
                transaction_type="unfreeze",
                amount_credits=frozen_amount,
                balance_after_credits=account.balance_credits,
                frozen_after_credits=account.frozen_credits,
                related_freeze_id=locked.id,
                project_id=locked.project_id,
                description="冻结超时自动解冻(30min)",
            )
            count += 1
    return count


async def lazy_expire_stale_freezes(session: AsyncSession) -> int:
    """惰性触发超时冻结扫描：进程内 60 秒限流，最多每 60 秒真正扫一次。

    在计费预检类 API 入口调用（低频兜底清理，正式环境可由定时任务接管）。

    Args:
        session: 异步数据库会话。

    Returns:
        本轮释放条数（限流窗口内返回 0）。
    """
    global _last_expire_scan_ts
    now = time.monotonic()
    if (now - _last_expire_scan_ts) < _EXPIRE_SCAN_INTERVAL_SECONDS:
        return 0
    _last_expire_scan_ts = now
    return await expire_stale_freezes(session)


# ---- 入账与订阅 ----


async def _grant_credits(
    session: AsyncSession, *, user_id: int, amount: int, description: str
) -> int:
    """给用户积分账户入账并写 recharge 流水（不提交，须处于事务中）。

    Args:
        session: 异步数据库会话（须已处于事务中）。
        user_id: 用户 ID。
        amount: 入账积分（<=0 时跳过返回 0）。
        description: 流水备注。

    Returns:
        实际入账积分数。

    Raises:
        BizError: 30005 积分账户不存在。
    """
    if amount <= 0:
        return 0
    brepo = BillingRepository(session)
    account = await brepo.get_account_by_user(user_id, for_update=True)
    if account is None:
        raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")
    account.balance_credits += amount
    account.total_granted_credits += amount
    await BillingRepository.add_transaction(
        session,
        account_id=account.id,
        transaction_type="recharge",
        amount_credits=amount,
        balance_after_credits=account.balance_credits,
        description=description,
    )
    return amount


async def _activate_subscription_and_grant(
    session: AsyncSession, *, user_id: int, plan: Plan, order_id: int | None = None
) -> int:
    """开通或续期用户订阅并发放套餐周期积分（不提交，须处于事务中）。

    续期规则：同套餐存在未过期 active 订阅则在原到期时间上叠加一个周期；
    已过期/不存在则从当前时刻起算；free 套餐永久（expires_at=NULL）。

    Args:
        session: 异步数据库会话（须已处于事务中）。
        user_id: 用户 ID。
        plan: 套餐实例。
        order_id: 来源订单 ID（可空）。

    Returns:
        发放的积分数。
    """
    brepo = BillingRepository(session)
    now = datetime.now(timezone.utc)
    period = timedelta(days=365) if plan.billing_period == "year" else timedelta(days=30)

    sub = await brepo.get_latest_subscription(user_id, plan.id)
    if plan.key == "free":
        new_expires = None
    else:
        base = (
            sub.expires_at
            if (sub is not None and sub.status == "active" and sub.expires_at and sub.expires_at > now)
            else now
        )
        new_expires = base + period

    if sub is None:
        from app.models import Subscription

        sub = Subscription(
            owner_type="user",
            user_id=user_id,
            plan_id=plan.id,
            status="active",
            started_at=now,
            expires_at=new_expires,
            auto_renew=False,
            order_id=order_id,
        )
        session.add(sub)
        await session.flush()
    else:
        sub.status = "active"
        sub.expires_at = new_expires
        sub.order_id = order_id if order_id is not None else sub.order_id

    return await _grant_credits(
        session, user_id=user_id, amount=plan.credits_per_period, description=f"订阅「{plan.name}」积分发放"
    )


# ---- 查询类 ----


async def list_plans(session: AsyncSession, user_id: int | None) -> dict[str, Any]:
    """查询在售套餐列表与当前用户订阅态。

    Args:
        session: 异步数据库会话。
        user_id: 当前用户 ID（None 表示未登录，不返回订阅态）。

    Returns:
        {items: [套餐...], subscription: {...}|None}。
    """
    brepo = BillingRepository(session)
    plans = await brepo.list_plans()
    items = [
        {
            "id": plan.id,
            "key": plan.key,
            "name": plan.name,
            "billing_period": plan.billing_period,
            "price_cents": plan.price_cents,
            "currency": plan.currency,
            "credits_per_period": plan.credits_per_period,
            "concurrency_limit": plan.concurrency_limit,
            "benefits": plan.benefits,
            "sort_order": plan.sort_order,
        }
        for plan in plans
    ]
    subscription: dict[str, Any] | None = None
    if user_id is not None:
        sub = await brepo.get_latest_subscription(user_id)
        if sub is not None and sub.status == "active":
            plan = await session.get(Plan, sub.plan_id)
            now = datetime.now(timezone.utc)
            if sub.expires_at is None or sub.expires_at > now:
                subscription = {
                    "plan_id": sub.plan_id,
                    "plan_key": plan.key if plan is not None else None,
                    "status": sub.status,
                    "started_at": _iso(sub.started_at),
                    "expires_at": _iso(sub.expires_at),
                    "auto_renew": sub.auto_renew,
                }
    return {"items": items, "subscription": subscription}


async def get_credit_account(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """查询当前用户积分账户（含 available），并惰性清理超时冻结。

    Args:
        session: 异步数据库会话。

    Returns:
        账户摘要字典。

    Raises:
        BizError: 30005 积分账户不存在。
    """
    await lazy_expire_stale_freezes(session)
    account = await BillingRepository(session).get_account_by_user(user_id)
    if account is None:
        raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")
    return _account_summary(account)


async def list_transactions(
    session: AsyncSession,
    user_id: int,
    *,
    page: int,
    page_size: int,
    transaction_type: str | None = None,
    sort: str = "-created_at",
) -> dict[str, Any]:
    """分页查询当前用户积分流水（可按类型筛选）。

    Args:
        session: 异步数据库会话。
        user_id: 用户 ID。
        page: 页码（≥1）。
        page_size: 每页条数（1~100）。
        transaction_type: 流水类型筛选（None 全部）。
        sort: 排序 "-created_at"/"created_at"。

    Returns:
        {items, page, page_size, total}。

    Raises:
        BizError: 10001 排序参数非法；30005 积分账户不存在。
    """
    if sort not in ("-created_at", "created_at"):
        raise BizError(ErrorCode.PARAM_INVALID, "sort 仅支持 created_at / -created_at")
    brepo = BillingRepository(session)
    account = await brepo.get_account_by_user(user_id)
    if account is None:
        raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")
    items, total = await brepo.list_transactions(
        account.id, page=page, page_size=page_size, transaction_type=transaction_type, sort=sort
    )
    return {
        "items": [
            {
                "id": tx.id,
                "transaction_type": tx.transaction_type,
                "amount_credits": tx.amount_credits,
                "balance_after_credits": tx.balance_after_credits,
                "frozen_after_credits": tx.frozen_after_credits,
                "related_freeze_id": tx.related_freeze_id,
                "project_id": tx.project_id,
                "description": tx.description,
                "created_at": _iso(tx.created_at),
            }
            for tx in items
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


# ---- 订单 / 支付回调 / 订阅开通 ----


def _new_order_no() -> str:
    """生成业务订单号：GD + UTC 时间戳(14 位) + 6 位随机十六进制。

    Returns:
        订单号字符串（orders.order_no 唯一约束兜底）。
    """
    return "GD" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + new_uuid().replace("-", "")[:6]


def verify_payment_sign(out_trade_no: str, status: str, sign: str) -> bool:
    """校验支付回调签名（非沙箱强制 HMAC，沙箱允许固定签名）。

    规则：
    - 配置了 ``PAYMENT_WEBHOOK_SECRET`` 时，一律校验
      ``HMAC-SHA256(out_trade_no|status, secret)``；
    - 未配置密钥时，仅沙箱模式接受固定 ``SANDBOX_WEBHOOK_SIGN``；
    - 非沙箱且未配置密钥一律拒绝，避免生产环境沿用沙箱后门。

    Args:
        out_trade_no: 商户订单号。
        status: 支付状态。
        sign: 回调签名。

    Returns:
        签名是否有效。
    """
    secret = settings.PAYMENT_WEBHOOK_SECRET.strip()
    if secret:
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{out_trade_no}|{status}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(sign, expected)
    if settings.SANDBOX_MODE:
        return hmac.compare_digest(sign, SANDBOX_WEBHOOK_SIGN)
    return False


async def create_order(
    session: AsyncSession, *, user_id: int, plan_key: str | None, credit_pack_credits: int | None
) -> dict[str, Any]:
    """创建订单（套餐订阅 / 积分充值包二选一）。

    沙箱模式返回模拟支付链接（携带固定签名），可直接回放为支付回调。

    Args:
        session: 异步数据库会话。
        user_id: 用户 ID。
        plan_key: 套餐键（与 credit_pack_credits 二选一）。
        credit_pack_credits: 充值包积分数（与 plan_key 二选一）。

    Returns:
        订单数据：{order_no, out_trade_no, order_type, credits, amount_cents,
        status, pay_url}。

    Raises:
        BizError: 10001 参数二选一约束失败；10002 套餐不存在或已下架。
    """
    if (plan_key is None) == (credit_pack_credits is None):
        raise BizError(ErrorCode.PARAM_INVALID, "plan_key 与 credit_pack_credits 必须二选一")
    if credit_pack_credits is not None and not (0 < credit_pack_credits <= MAX_CREDIT_PACK_CREDITS):
        raise BizError(
            ErrorCode.PARAM_INVALID,
            f"充值包积分须在 1~{MAX_CREDIT_PACK_CREDITS} 之间",
        )
    from app.models import Order

    brepo = BillingRepository(session)
    async with _transaction(session):
        if plan_key is not None:
            plan = await brepo.get_plan_by_key(plan_key)
            if plan is None or not plan.is_active:
                raise BizError(ErrorCode.NOT_FOUND, "套餐不存在或已下架")
            order = Order(
                order_no=_new_order_no(),
                owner_type="user",
                user_id=user_id,
                plan_id=plan.id,
                order_type="subscription",
                credits=plan.credits_per_period,
                amount_cents=plan.price_cents,
                currency=plan.currency,
                status="created",
            )
        else:
            order = Order(
                order_no=_new_order_no(),
                owner_type="user",
                user_id=user_id,
                plan_id=None,
                order_type="credit_pack",
                credits=credit_pack_credits or 0,
                amount_cents=credit_pack_credits or 0,  # 定价：1 积分 = 1 分
                status="created",
            )
        session.add(order)
        await session.flush()

    # 仅沙箱模式下发可回放的模拟支付链接；生产环境返回 None，等待真实网关
    pay_url: str | None = None
    if settings.SANDBOX_MODE:
        pay_url = (
            f"https://{_SANDBOX_PAY_HOST}/mock-pay?out_trade_no={order.order_no}"
            f"&amount_cents={order.amount_cents}&sign={SANDBOX_WEBHOOK_SIGN}"
        )
    return {
        "order_no": order.order_no,
        "out_trade_no": order.order_no,
        "order_type": order.order_type,
        "credits": order.credits,
        "amount_cents": order.amount_cents,
        "status": order.status,
        "pay_url": pay_url,
    }


async def handle_payment_webhook(
    session: AsyncSession, *, out_trade_no: str, status: str, sign: str
) -> dict[str, Any]:
    """处理支付回调（沙箱固定签名 + out_trade_no 幂等）。

    幂等保证：payments.out_trade_no 全局唯一。首次回调在单事务内
    （订单行锁串行化）写 payment(success) → order(paid) → 入账
    （套餐：建/续订阅 + 发放周期积分；充值包：加余额）；重复回调直接
    返回成功，不重复入账。

    Args:
        session: 异步数据库会话。
        out_trade_no: 商户订单号。
        status: 支付结果（沙箱仅支持 success）。
        sign: 回调签名。

    Returns:
        {out_trade_no, duplicate, order_status, granted_credits}。

    Raises:
        BizError: 10001 回调状态不支持；10002 订单不存在；
            30004 签名错误。
    """
    if not verify_payment_sign(out_trade_no, status, sign):
        raise BizError(
            ErrorCode.PAY_CALLBACK_SIGN_ERROR,
            "回调签名校验失败（生产环境需配置 PAYMENT_WEBHOOK_SECRET 并使用 HMAC 签名）",
        )
    if status != "success":
        raise BizError(ErrorCode.PARAM_INVALID, "当前仅支持 status=success 的回调")

    from app.models import Payment

    brepo = BillingRepository(session)
    async with _transaction(session):
        # 幂等：已成功入账的回调直接短路返回
        payment = await brepo.get_payment_by_out_trade_no(out_trade_no)
        if payment is not None and payment.status == "success":
            order = await brepo.get_order_by_no(out_trade_no)
            return {
                "out_trade_no": out_trade_no,
                "duplicate": True,
                "order_status": order.status if order is not None else None,
                "granted_credits": 0,
            }

        order = await brepo.get_order_by_no(out_trade_no, for_update=True)
        if order is None:
            raise BizError(ErrorCode.NOT_FOUND, "订单不存在")
        if int(order.amount_cents or 0) <= 0 and order.order_type != "subscription":
            # 零金额充值订单不允许入账（free 套餐订阅除外）
            raise BizError(ErrorCode.PARAM_INVALID, "订单金额非法，拒绝入账")

        now = datetime.now(timezone.utc)
        if payment is None:
            payment = Payment(
                order_id=order.id,
                out_trade_no=out_trade_no,
                channel="wechat",
                channel_trade_no=f"MOCK{new_uuid().replace('-', '')[:12].upper()}",
                amount_cents=order.amount_cents,
                status="success",
                paid_at=now,
                raw_callback={"out_trade_no": out_trade_no, "status": status, "sandbox": True},
            )
            session.add(payment)
            await session.flush()
        else:
            payment.status = "success"
            payment.paid_at = now

        granted = 0
        if order.status != "paid":
            order.status = "paid"
            order.paid_at = now
            if order.order_type == "subscription":
                plan = await session.get(Plan, order.plan_id) if order.plan_id is not None else None
                if plan is None:
                    raise BizError(ErrorCode.NOT_FOUND, "订单关联的套餐不存在")
                granted = await _activate_subscription_and_grant(
                    session, user_id=int(order.user_id or 0), plan=plan, order_id=order.id
                )
            else:
                granted = await _grant_credits(
                    session, user_id=int(order.user_id or 0), amount=order.credits, description="积分充值包"
                )
    return {
        "out_trade_no": out_trade_no,
        "duplicate": False,
        "order_status": order.status,
        "granted_credits": granted,
    }


async def create_subscription_direct(
    session: AsyncSession, *, user_id: int, plan_key: str
) -> dict[str, Any]:
    """直接开通/续期订阅（沙箱联调端点，跳过支付）并发放周期积分。

    Args:
        session: 异步数据库会话。
        user_id: 用户 ID。
        plan_key: 套餐键。

    Returns:
        {plan, granted_credits, credit_account}。

    Raises:
        BizError: 20003 非沙箱环境禁用（防跳过支付）；10002 套餐不存在或已下架。
    """
    if not settings.SANDBOX_MODE:
        # 生产环境禁止"跳过支付直接开通订阅"，避免绕过支付通道
        raise BizError(
            ErrorCode.FORBIDDEN,
            "订阅开通仅允许在沙箱环境使用；生产环境请走订单 + 支付回调",
        )
    brepo = BillingRepository(session)
    async with _transaction(session):
        plan = await brepo.get_plan_by_key(plan_key)
        if plan is None or not plan.is_active:
            raise BizError(ErrorCode.NOT_FOUND, "套餐不存在或已下架")
        granted = await _activate_subscription_and_grant(session, user_id=user_id, plan=plan)
        account = await brepo.get_account_by_user(user_id)
        if account is None:
            raise BizError(ErrorCode.FREEZE_NOT_FOUND, "积分账户不存在")
    return {
        "plan": {
            "id": plan.id,
            "key": plan.key,
            "name": plan.name,
            "billing_period": plan.billing_period,
            "credits_per_period": plan.credits_per_period,
        },
        "granted_credits": granted,
        "credit_account": _account_summary(account),
    }


# ---- 幂等键工具（供渲染任务等调用方生成标准幂等键） ----


def build_freeze_idempotency_key(project_id: int | None, task_type: str, input_hash: str) -> str:
    """生成冻结幂等键：sha256(project_id + task_type + input_hash)。

    Args:
        project_id: 项目 ID（可空，空串参与摘要）。
        task_type: 任务类型。
        input_hash: 输入内容指纹。

    Returns:
        64 位十六进制幂等键。
    """
    return sha256_hash(f"{project_id or ''}|{task_type}|{input_hash}")

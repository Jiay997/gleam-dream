"""认证与计费域集成测试（Task 3）。

覆盖范围：
- 短信验证码发送 / 60 秒限流（10004）；
- 注册（赠送 500 积分 + grant 流水）/ 重复注册（10001）/ 密码登录（decode 校验 sub）；
- 计费核心：precheck_and_freeze → settle（含差额 unfreeze 流水）/ 余额不足（30001）/
  refund / 过期冻结自动释放；
- 支付回调幂等：首次入账、重复回调 10 次余额不变。

运行方式::

    cd backend && .venv\\Scripts\\python -m pytest tests/test_auth_billing.py -x -q

说明：
- 直接复用 ``app.main.app``（异常处理器已注册），并把本任务三个 router
  （auth/users/billing，不在主控聚合器内）手动挂载到 /api/v1 前缀下；
- 所有用例运行在同一 event loop（loop_scope="session"），避免 asyncpg
  连接跨 loop 复用问题；
- 测试数据使用 13900000001~3 手机号段，fixture 前后自动清理。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, select, update

from app.core.config import settings
from app.core.db import async_session_factory
from app.core.security import decode_token
from app.main import app
from app.models import (
    CreditAccount,
    CreditFreeze,
    CreditTransaction,
    Order,
    Payment,
    Subscription,
    User,
    UserIdentity,
)
from app.repositories.billing_repo import BillingRepository
from app.services import auth_service, billing_service

# 本任务三个 router 不在主控聚合器 api_router 内，测试时手动挂载
from app.api.auth import router as auth_router
from app.api.billing import router as billing_router
from app.api.users import router as users_router

#: 全部用例共享 session 级 event loop（asyncpg 连接不能跨 loop）
pytestmark = pytest.mark.asyncio(loop_scope="session")

#: 测试手机号段
PHONE1 = "13900000001"  # 主测用户：注册/登录/冻结结算
PHONE2 = "13900000002"  # 支付回调用户
PHONE3 = "13900000003"  # 重复注册用户
PHONE4 = "13900000004"  # wechat stub 用户（微信建号无手机号）
TEST_PHONES = (PHONE1, PHONE2, PHONE3)

#: 沙箱支付回调固定签名（与 billing_service.SANDBOX_WEBHOOK_SIGN 一致）
SANDBOX_SIGN = billing_service.SANDBOX_WEBHOOK_SIGN


def _ensure_routers() -> None:
    """把本任务的三个 router 挂载到 app（幂等：已挂载则跳过）。

    主控聚合器 api_router 暂未包含本任务路由，测试进程内手动 include，
    以 settings.API_V1_PREFIX（/api/v1）为统一前缀。
    """
    existing_paths = {getattr(route, "path", "") for route in app.routes}
    for prefix, router in (
        ("/auth", auth_router),
        ("/users", users_router),
        ("/billing", billing_router),
    ):
        sample_path = f"{settings.API_V1_PREFIX}{prefix}"
        if not any(p.startswith(sample_path) for p in existing_paths):
            app.include_router(router, prefix=settings.API_V1_PREFIX)


async def _cleanup_test_users() -> None:
    """清理测试手机号段的全部业务数据（RESTRICT 外键按依赖序删除）。"""
    async with async_session_factory() as session, session.begin():
        user_ids = (
            (await session.execute(select(User.id).where(User.phone.in_(TEST_PHONES))))
            .scalars()
            .all()
        )
        if not user_ids:
            return
        account_ids = (
            (
                await session.execute(
                    select(CreditAccount.id).where(
                        CreditAccount.owner_type == "user",
                        CreditAccount.user_id.in_(user_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        order_ids = (
            (await session.execute(select(Order.id).where(Order.user_id.in_(user_ids))))
            .scalars()
            .all()
        )
        if order_ids:
            await session.execute(delete(Payment).where(Payment.order_id.in_(order_ids)))
        await session.execute(delete(Order).where(Order.user_id.in_(user_ids)))
        if account_ids:
            await session.execute(
                delete(CreditTransaction).where(CreditTransaction.account_id.in_(account_ids))
            )
            await session.execute(delete(CreditFreeze).where(CreditFreeze.account_id.in_(account_ids)))
        await session.execute(delete(Subscription).where(Subscription.user_id.in_(user_ids)))
        await session.execute(delete(CreditAccount).where(CreditAccount.user_id.in_(user_ids)))
        await session.execute(delete(UserIdentity).where(UserIdentity.user_id.in_(user_ids)))
        await session.execute(delete(User).where(User.id.in_(user_ids)))


@pytest_asyncio.fixture(loop_scope="session", scope="session", autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    """session 级清理 fixture：跑前清残留、跑后清数据。"""
    await _cleanup_test_users()
    yield
    await _cleanup_test_users()


@pytest_asyncio.fixture(loop_scope="session", scope="session")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """session 级 HTTP 客户端：ASGITransport 直连 app。"""
    _ensure_routers()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.fixture(scope="session")
def ctx() -> dict[str, Any]:
    """session 级共享上下文：跨用例传递 token / 订单号 / 账户 ID 等。"""
    return {}


async def _get_account_by_phone(phone: str) -> CreditAccount:
    """按手机号查询用户积分账户。

    Args:
        phone: 手机号。

    Returns:
        CreditAccount 实例。

    Raises:
        AssertionError: 用户或账户不存在。
    """
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(CreditAccount)
                .join(User, User.id == CreditAccount.user_id)
                .where(User.phone == phone, CreditAccount.owner_type == "user")
            )
        ).scalar_one()
        return row


async def _get_account_transactions(account_id: int) -> list[CreditTransaction]:
    """查询账户全部流水（按创建时间升序）。

    Args:
        account_id: 账户 ID。

    Returns:
        流水列表。
    """
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(CreditTransaction)
                    .where(CreditTransaction.account_id == account_id)
                    .order_by(CreditTransaction.created_at.asc(), CreditTransaction.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


def _auth_headers(token: str) -> dict[str, str]:
    """构造 Bearer 认证请求头。

    Args:
        token: access JWT。

    Returns:
        请求头字典。
    """
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 场景 1：发验证码 → 注册 → 用户/积分账户/流水断言
# ---------------------------------------------------------------------------


async def test_01_send_code_and_register(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """发验证码 → 注册：断言用户创建、账户 balance=500、存在 grant 流水。"""
    # 1. 发送验证码（sandbox 返回 dev_code=123456）
    resp = await client.post("/api/v1/auth/sms-code", json={"phone": PHONE1})
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    assert body["data"]["dev_code"] == settings.DEV_FIXED_SMS_CODE, body
    code = body["data"]["dev_code"]

    # 2. 注册（带密码，供场景 4 密码登录）
    resp = await client.post(
        "/api/v1/auth/register",
        json={"phone": PHONE1, "sms_code": code, "password": "test123456", "nickname": "测试一"},
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    assert data["access_token"] and data["refresh_token"], body
    assert data["user"]["phone"] == PHONE1, body
    ctx["user1_id"] = data["user"]["id"]
    ctx["user1_access"] = data["access_token"]

    # 3. 校验 JWT sub = 用户 ID
    payload = decode_token(data["access_token"], expected_type="access")
    assert str(payload["sub"]) == str(data["user"]["id"])

    # 4. 积分账户 balance=500 + grant 流水 500
    account = await _get_account_by_phone(PHONE1)
    assert account.balance_credits == 500
    assert account.frozen_credits == 0
    assert account.total_granted_credits == 500
    txs = await _get_account_transactions(account.id)
    grants = [t for t in txs if t.transaction_type == "grant"]
    assert len(grants) == 1
    assert grants[0].amount_credits == 500
    assert grants[0].balance_after_credits == 500
    assert grants[0].description == "注册赠送"
    ctx["account1_id"] = account.id


# ---------------------------------------------------------------------------
# 场景 2：60 秒内重复发码 → 10004
# ---------------------------------------------------------------------------


async def test_02_sms_code_rate_limited(client: httpx.AsyncClient) -> None:
    """60 秒内同号重复发码：返回业务码 10004（HTTP 429）。"""
    resp = await client.post("/api/v1/auth/sms-code", json={"phone": PHONE1})
    body = resp.json()
    assert resp.status_code == 429, body
    assert body["code"] == 10004, body


# ---------------------------------------------------------------------------
# 场景 3：重复注册 → 10001（同号）；并用 PHONE3 完成第二次注册供后续场景
# ---------------------------------------------------------------------------


async def test_03_duplicate_register(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """重复注册同号 → 10001「手机号已注册」（验证码走进程内存注入绕过限流）。"""
    # 注入一条有效验证码（避开 60 秒发送限流，直接操作进程内存存储）
    auth_service._sms_code_store[PHONE1] = (settings.DEV_FIXED_SMS_CODE, time.time() + 300)
    resp = await client.post(
        "/api/v1/auth/register",
        json={"phone": PHONE1, "sms_code": settings.DEV_FIXED_SMS_CODE},
    )
    body = resp.json()
    assert body["code"] == 10001, body
    assert "已注册" in body["message"], body

    # 顺带注册 PHONE3（供后续用例使用）
    resp = await client.post("/api/v1/auth/sms-code", json={"phone": PHONE3})
    code = resp.json()["data"]["dev_code"]
    resp = await client.post("/api/v1/auth/register", json={"phone": PHONE3, "sms_code": code})
    body = resp.json()
    assert body["code"] == 0, body
    ctx["user3_id"] = body["data"]["user"]["id"]


# ---------------------------------------------------------------------------
# 场景 4：密码登录 → tokens 有效（decode 校验 sub）
# ---------------------------------------------------------------------------


async def test_04_password_login(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """密码登录成功，access token 可访问 /users/me 且 sub 与用户一致。"""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"login_type": "password", "phone": PHONE1, "password": "test123456"},
    )
    body = resp.json()
    assert resp.status_code == 200, body
    assert body["code"] == 0, body
    data = body["data"]
    payload = decode_token(data["access_token"], expected_type="access")
    assert str(payload["sub"]) == str(ctx["user1_id"])

    # 错误密码 → 20001
    resp = await client.post(
        "/api/v1/auth/login",
        json={"login_type": "password", "phone": PHONE1, "password": "wrong-password"},
    )
    body = resp.json()
    assert body["code"] == 20001, body

    # access token 访问 /users/me
    resp = await client.get("/api/v1/users/me", headers=_auth_headers(data["access_token"]))
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["user"]["id"] == ctx["user1_id"], body
    assert body["data"]["credit"]["balance_credits"] == 500, body

    # refresh 换新 access
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": data["refresh_token"]}
    )
    body = resp.json()
    assert body["code"] == 0, body
    new_payload = decode_token(body["data"]["access_token"], expected_type="access")
    assert str(new_payload["sub"]) == str(ctx["user1_id"])


# ---------------------------------------------------------------------------
# 场景 5：冻结 → 结算（含差额解冻流水）
# ---------------------------------------------------------------------------


async def test_05_freeze_and_settle(ctx: dict[str, Any]) -> None:
    """冻结 300 → frozen=300/available=200 → settle(250) → balance=250/frozen=0。"""
    account = await _get_account_by_phone(PHONE1)
    assert account.balance_credits == 500

    async with async_session_factory() as session:
        freeze = await billing_service.precheck_and_freeze(
            session,
            account_id=account.id,
            project_id=None,
            task_type="text_to_video",
            estimated_credits=300,
            idempotency_key="it-freeze-001",
        )
    assert freeze.status == "frozen"
    assert freeze.frozen_credits == 300

    after_freeze = await _get_account_by_phone(PHONE1)
    assert after_freeze.frozen_credits == 300
    assert after_freeze.balance_credits == 500  # 冻结不动 balance

    # 幂等：同幂等键再次冻结 → 返回同一冻结记录，冻结额不变
    async with async_session_factory() as session:
        freeze2 = await billing_service.precheck_and_freeze(
            session,
            account_id=account.id,
            project_id=None,
            task_type="text_to_video",
            estimated_credits=300,
            idempotency_key="it-freeze-001",
        )
    assert freeze2.id == freeze.id
    after_idem = await _get_account_by_phone(PHONE1)
    assert after_idem.frozen_credits == 300

    # 结算 250（差额 50 解冻）
    async with async_session_factory() as session:
        result = await billing_service.settle(
            session, freeze_id=freeze.id, actual_credits=250
        )
    assert result["status"] == "settled"
    assert result["actual_credits"] == 250
    assert result["released_credits"] == 50
    assert result["balance_credits"] == 250
    assert result["frozen_credits"] == 0

    after_settle = await _get_account_by_phone(PHONE1)
    assert after_settle.balance_credits == 250
    assert after_settle.frozen_credits == 0
    assert after_settle.total_consumed_credits == 250

    txs = await _get_account_transactions(account.id)
    freeze_txs = [t for t in txs if t.transaction_type == "freeze"]
    settle_txs = [t for t in txs if t.transaction_type == "settle"]
    unfreeze_txs = [t for t in txs if t.transaction_type == "unfreeze"]
    assert len(freeze_txs) == 1 and freeze_txs[0].amount_credits == -300
    assert len(settle_txs) == 1 and settle_txs[0].amount_credits == -250
    assert settle_txs[0].balance_after_credits == 250
    assert len(unfreeze_txs) == 1 and unfreeze_txs[0].amount_credits == 50
    assert unfreeze_txs[0].frozen_after_credits == 0
    ctx["account1_balance"] = 250


# ---------------------------------------------------------------------------
# 场景 6：余额不足 → 30001（data 携带 needed/available）
# ---------------------------------------------------------------------------


async def test_06_insufficient_credits() -> None:
    """冻结 10000（余额不足）→ BizError 30001，data 含 needed/available。"""
    account = await _get_account_by_phone(PHONE1)
    from app.core.errors import BizError

    async with async_session_factory() as session:
        with pytest.raises(BizError) as exc_info:
            await billing_service.precheck_and_freeze(
                session,
                account_id=account.id,
                project_id=None,
                task_type="text_to_video",
                estimated_credits=10000,
                idempotency_key="it-freeze-poor",
            )
    exc = exc_info.value
    assert exc.code == 30001
    assert exc.data == {"needed": 10000, "available": 250}

    # 失败路径不得留下任何冻结/流水残留
    after = await _get_account_by_phone(PHONE1)
    assert after.frozen_credits == 0
    txs = await _get_account_transactions(account.id)
    assert all(t.transaction_type != "freeze" or t.idempotency_key != "it-freeze-poor" for t in txs)


# ---------------------------------------------------------------------------
# 场景 7：refund → frozen 归零
# ---------------------------------------------------------------------------


async def test_07_refund(ctx: dict[str, Any]) -> None:
    """冻结 100 后全额退款：frozen 归零，balance 不变，unfreeze 流水正确。"""
    account = await _get_account_by_phone(PHONE1)
    balance_before = account.balance_credits

    async with async_session_factory() as session:
        freeze = await billing_service.precheck_and_freeze(
            session,
            account_id=account.id,
            project_id=None,
            task_type="tts",
            estimated_credits=100,
            idempotency_key="it-freeze-refund",
        )
        result = await billing_service.refund(session, freeze_id=freeze.id, reason="任务失败退款")

    assert result["status"] == "released"
    assert result["released_credits"] == 100
    assert result["frozen_credits"] == 0

    after = await _get_account_by_phone(PHONE1)
    assert after.frozen_credits == 0
    assert after.balance_credits == balance_before  # 全额返还，余额不变

    txs = await _get_account_transactions(account.id)
    refund_txs = [t for t in txs if t.transaction_type == "unfreeze" and t.related_freeze_id == freeze.id]
    assert len(refund_txs) == 1
    assert refund_txs[0].amount_credits == 100
    assert "任务失败退款" in (refund_txs[0].description or "")

    # 已释放的冻结不可再次结算/退款（30005）
    from app.core.errors import BizError

    async with async_session_factory() as session:
        with pytest.raises(BizError) as exc_info:
            await billing_service.settle(session, freeze_id=freeze.id, actual_credits=10)
    assert exc_info.value.code == 30005


# ---------------------------------------------------------------------------
# 场景 8：支付回调幂等（首次入账 + 重复回调 10 次余额不变）
# ---------------------------------------------------------------------------


async def test_08_payment_webhook_idempotent(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """订单支付回调：首次入账（订阅 5000 积分 + 建订阅），重复 10 次余额不变。"""
    # 注册 PHONE2（发码）并取注册返回的 token
    resp = await client.post("/api/v1/auth/sms-code", json={"phone": PHONE2})
    code = resp.json()["data"]["dev_code"]
    resp = await client.post("/api/v1/auth/register", json={"phone": PHONE2, "sms_code": code})
    body = resp.json()
    assert body["code"] == 0, body
    token = body["data"]["access_token"]

    # 创建 pro 套餐订阅订单
    resp = await client.post(
        "/api/v1/billing/orders",
        json={"plan_key": "pro"},
        headers=_auth_headers(token),
    )
    body = resp.json()
    assert body["code"] == 0, body
    out_trade_no = body["data"]["out_trade_no"]
    assert body["data"]["order_no"] == out_trade_no
    assert body["data"]["order_type"] == "subscription"
    assert body["data"]["amount_cents"] == 4900
    assert out_trade_no.startswith("GD")
    ctx["order_no"] = out_trade_no

    # 签名错误 → 30004
    resp = await client.post(
        "/api/v1/billing/webhooks/payment",
        json={"out_trade_no": out_trade_no, "status": "success", "sign": "bad-sign"},
    )
    body = resp.json()
    assert body["code"] == 30004, body

    # 首次正确回调 → 入账
    account = await _get_account_by_phone(PHONE2)
    balance_before = account.balance_credits  # 注册赠送 500
    resp = await client.post(
        "/api/v1/billing/webhooks/payment",
        json={"out_trade_no": out_trade_no, "status": "success", "sign": SANDBOX_SIGN},
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["duplicate"] is False, body
    assert body["data"]["granted_credits"] == 5000, body

    account = await _get_account_by_phone(PHONE2)
    assert account.balance_credits == balance_before + 5000

    # 订单已支付 + 订阅生效 + 充值流水存在
    async with async_session_factory() as session:
        order = (await session.execute(select(Order).where(Order.order_no == out_trade_no))).scalar_one()
        assert order.status == "paid"
        assert order.paid_at is not None
        payment = (
            await session.execute(select(Payment).where(Payment.out_trade_no == out_trade_no))
        ).scalar_one()
        assert payment.status == "success"
        sub = (
            await session.execute(
                select(Subscription).where(
                    Subscription.user_id == order.user_id,
                    Subscription.plan_id == order.plan_id,
                    Subscription.status == "active",
                )
            )
        ).scalar_one_or_none()
        assert sub is not None
        assert sub.expires_at is not None and sub.expires_at > datetime.now(timezone.utc)
        ctx["user2_id"] = order.user_id

    # 重复回调 10 次 → 余额不变、恒返回成功（幂等）
    for _ in range(10):
        resp = await client.post(
            "/api/v1/billing/webhooks/payment",
            json={"out_trade_no": out_trade_no, "status": "success", "sign": SANDBOX_SIGN},
        )
        body = resp.json()
        assert resp.status_code == 200, body
        assert body["code"] == 0, body
        assert body["data"]["duplicate"] is True, body
    account = await _get_account_by_phone(PHONE2)
    assert account.balance_credits == balance_before + 5000

    # 流水：仅一条 5000 充值（订阅发放）
    txs = await _get_account_transactions(account.id)
    recharges = [t for t in txs if t.transaction_type == "recharge"]
    assert len(recharges) == 1 and recharges[0].amount_credits == 5000


# ---------------------------------------------------------------------------
# 场景 9：过期冻结 → expire_stale_freezes → released
# ---------------------------------------------------------------------------


async def test_09_expire_stale_freezes() -> None:
    """把冻结 expires_at 改到过去 → expire_stale_freezes → released + frozen 归零。"""
    account = await _get_account_by_phone(PHONE1)
    balance_before = account.balance_credits

    async with async_session_factory() as session:
        freeze = await billing_service.precheck_and_freeze(
            session,
            account_id=account.id,
            project_id=None,
            task_type="compose",
            estimated_credits=50,
            idempotency_key="it-freeze-expire",
        )

    # 篡改过期时间到过去
    past = datetime.now(timezone.utc) - timedelta(minutes=31)
    async with async_session_factory() as session, session.begin():
        await session.execute(
            update(CreditFreeze).where(CreditFreeze.id == freeze.id).values(expires_at=past)
        )

    async with async_session_factory() as session:
        count = await billing_service.expire_stale_freezes(session)
    assert count >= 1

    after = await _get_account_by_phone(PHONE1)
    assert after.frozen_credits == 0
    assert after.balance_credits == balance_before  # 余额不变

    async with async_session_factory() as session:
        released = (
            await session.execute(select(CreditFreeze).where(CreditFreeze.id == freeze.id))
        ).scalar_one()
    assert released.status == "released"
    assert released.released_at is not None

    txs = await _get_account_transactions(account.id)
    expire_txs = [t for t in txs if t.related_freeze_id == freeze.id and t.transaction_type == "unfreeze"]
    assert len(expire_txs) == 1 and expire_txs[0].amount_credits == 50


# ---------------------------------------------------------------------------
# 补充：微信 Stub 登录幂等 + users/me 偏好合并 + 订阅直开 + 流水分页 + 登出
# ---------------------------------------------------------------------------


async def test_10_wechat_stub_login(client: httpx.AsyncClient) -> None:
    """微信 Stub：首次 mock_unionid 建号（含 500 积分账户），再次登录同用户。"""
    resp = await client.post("/api/v1/auth/wechat", json={"mock_unionid": "mock-unionid-0001"})
    body = resp.json()
    assert body["code"] == 0, body
    user_id_1 = body["data"]["user"]["id"]
    assert body["data"]["user"]["register_source"] == "wechat"

    # 积分账户 500（同注册事务）
    account = await _account_by_user_id(user_id_1)
    assert account is not None and account.balance_credits == 500

    # 再次同 unionid → 同一用户直接登录（AC-F1-02-2）
    resp = await client.post("/api/v1/auth/wechat", json={"mock_unionid": "mock-unionid-0001"})
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["user"]["id"] == user_id_1


async def _account_by_user_id(user_id: int) -> CreditAccount | None:
    """按用户 ID 查询积分账户。

    Args:
        user_id: 用户 ID。

    Returns:
        CreditAccount 或 None。
    """
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(CreditAccount).where(
                    CreditAccount.owner_type == "user", CreditAccount.user_id == user_id
                )
            )
        ).scalar_one_or_none()


async def test_11_users_me_preferences_merge(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """PATCH /users/me：preferences JSONB 浅合并（旧键保留、新键覆盖）。"""
    token = ctx["user1_access"]
    resp = await client.patch(
        "/api/v1/users/me",
        json={"preferences": {"style": "热血", "duration": 60}},
        headers=_auth_headers(token),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["user"]["preferences"] == {"style": "热血", "duration": 60}, body

    # 第二次合并：保留旧键、覆盖同名键、新增键
    resp = await client.patch(
        "/api/v1/users/me",
        json={"preferences": {"style": "悬疑"}, "nickname": "改名了"},
        headers=_auth_headers(token),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["user"]["preferences"] == {"style": "悬疑", "duration": 60}, body
    assert body["data"]["user"]["nickname"] == "改名了"

    # 全空请求 → 10001
    resp = await client.patch("/api/v1/users/me", json={}, headers=_auth_headers(token))
    assert resp.json()["code"] == 10001

    # 未登录 → 401/20001
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == 20001


async def test_12_subscription_direct_and_transactions(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """订阅直开（跳过支付）+ 套餐列表订阅态 + 流水分页/筛选 + 登出。"""
    token = ctx["user1_access"]

    # 直接开通 studio 订阅（+30000）
    resp = await client.post(
        "/api/v1/billing/subscriptions", json={"plan_key": "studio"}, headers=_auth_headers(token)
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["granted_credits"] == 30000, body

    # 套餐列表带订阅态
    resp = await client.get("/api/v1/billing/plans", headers=_auth_headers(token))
    body = resp.json()
    assert body["code"] == 0, body
    assert len(body["data"]["items"]) == 3, body
    assert body["data"]["subscription"]["plan_key"] == "studio", body
    # 未登录也可看套餐列表（订阅态为 null）
    resp = await client.get("/api/v1/billing/plans")
    body = resp.json()
    assert body["code"] == 0 and body["data"]["subscription"] is None, body

    # 账户查询（available）
    resp = await client.get("/api/v1/billing/credit-account", headers=_auth_headers(token))
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["available_credits"] == body["data"]["balance_credits"] - body["data"]["frozen_credits"]

    # 流水分页：第 1 页 2 条 + 按 recharge 筛选
    resp = await client.get(
        "/api/v1/billing/transactions",
        params={"page": 1, "page_size": 2},
        headers=_auth_headers(token),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["page"] == 1 and body["data"]["page_size"] == 2
    assert len(body["data"]["items"]) == 2
    assert body["data"]["total"] >= 3

    resp = await client.get(
        "/api/v1/billing/transactions",
        params={"transaction_type": "recharge"},
        headers=_auth_headers(token),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert all(item["transaction_type"] == "recharge" for item in body["data"]["items"]), body

    # 非法 sort → 10001
    resp = await client.get(
        "/api/v1/billing/transactions", params={"sort": "hack"}, headers=_auth_headers(token)
    )
    assert resp.json()["code"] == 10001

    # 登出恒成功
    resp = await client.post("/api/v1/auth/logout")
    assert resp.json()["code"] == 0


async def test_13_credit_pack_order(client: httpx.AsyncClient, ctx: dict[str, Any]) -> None:
    """充值包订单：回调后按 1 积分=1 分入账，余额精确增加。"""
    token = ctx["user1_access"]
    account = await _get_account_by_phone(PHONE1)
    balance_before = account.balance_credits

    resp = await client.post(
        "/api/v1/billing/orders",
        json={"credit_pack_credits": 1000},
        headers=_auth_headers(token),
    )
    body = resp.json()
    assert body["code"] == 0, body
    assert body["data"]["amount_cents"] == 1000, body
    out_trade_no = body["data"]["out_trade_no"]

    resp = await client.post(
        "/api/v1/billing/webhooks/payment",
        json={"out_trade_no": out_trade_no, "status": "success", "sign": SANDBOX_SIGN},
    )
    body = resp.json()
    assert body["code"] == 0 and body["data"]["granted_credits"] == 1000, body

    account = await _get_account_by_phone(PHONE1)
    assert account.balance_credits == balance_before + 1000

    # 二选一约束：同时提供 → 10001
    resp = await client.post(
        "/api/v1/billing/orders",
        json={"plan_key": "pro", "credit_pack_credits": 100},
        headers=_auth_headers(token),
    )
    assert resp.json()["code"] == 10001

    # 不存在的套餐 → 10002
    resp = await client.post(
        "/api/v1/billing/orders", json={"plan_key": "gold"}, headers=_auth_headers(token)
    )
    assert resp.json()["code"] == 10002

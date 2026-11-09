"""计费路由模块（Task 3 · 认证与计费域）。

挂载点：主控在 api_router 上以 ``settings.API_V1_PREFIX``（/api/v1）聚合，
最终路径形如 ``GET /api/v1/billing/plans``。

端点：
- GET  /billing/plans             套餐列表 + 当前用户订阅态（登录可选）；
- GET  /billing/credit-account    积分账户（available/balance/frozen/total_*）；
- GET  /billing/transactions      积分流水分页（transaction_type 筛选）；
- POST /billing/orders            创建订单（套餐/充值包二选一，sandbox 返回模拟支付链接）；
- POST /billing/webhooks/payment  支付回调（沙箱固定签名 + out_trade_no 幂等）；
- POST /billing/subscriptions     直接开通订阅（沙箱联调，跳过支付）。

路由变量名：``router = APIRouter(prefix="/billing", tags=["billing"])``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.security.http import HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import BizError, ErrorCode, success_envelope
from app.core.security import decode_token
from app.schemas.billing import (
    OrderCreateRequest,
    PaymentWebhookRequest,
    SubscriptionCreateRequest,
)
from app.services import billing_service

#: 计费子路由（主控聚合：include_router(billing.router)）
router = APIRouter(prefix="/billing", tags=["billing"])

#: Bearer 凭证提取器（可选登录场景：缺失/无效不报错，仅视为未登录）
_optional_bearer = HTTPBearer(auto_error=False, description="Bearer JWT（可选）")


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


async def optional_user_id(
    credentials: Annotated[object | None, Depends(_optional_bearer)],
) -> int | None:
    """依赖：可选登录。携带有效 Bearer 时返回用户 ID，否则返回 None。

    Args:
        credentials: Bearer 凭证（可能为 None）。

    Returns:
        用户 ID 或 None（未登录/令牌无效均降级为未登录）。
    """
    if credentials is None or not getattr(credentials, "credentials", None):
        return None
    try:
        payload = decode_token(credentials.credentials, expected_type="access")
    except BizError:
        return None
    sub = str(payload.get("sub", ""))
    return int(sub) if sub.isdigit() else None


@router.get("/plans")
async def get_plans(
    user_id: Annotated[int | None, Depends(optional_user_id)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """查询在售套餐列表与当前用户订阅态（未登录时订阅态为 null）。

    Args:
        user_id: 当前用户 ID（可选登录）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含 {items: [套餐...], subscription}。
    """
    data = await billing_service.list_plans(session, user_id)
    return success_envelope(data)


@router.get("/credit-account")
async def get_credit_account(
    user_id: Annotated[int, Depends(current_user_id)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """查询当前用户积分账户（available = balance - frozen）。

    进入本计费查询前惰性触发一次超时冻结清理（进程内 60 秒限流）。

    Args:
        user_id: 当前登录用户 ID（依赖注入）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 为账户摘要。

    Raises:
        BizError: 30005 积分账户不存在。
    """
    data = await billing_service.get_credit_account(session, user_id)
    return success_envelope(data)


@router.get("/transactions")
async def get_transactions(
    user_id: Annotated[int, Depends(current_user_id)],
    session: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    sort: Annotated[str, Query()] = "-created_at",
    transaction_type: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """分页查询当前用户积分流水。

    Args:
        user_id: 当前登录用户 ID（依赖注入）。
        session: 请求级数据库会话。
        page: 页码（≥1，默认 1）。
        page_size: 每页条数（1~100，默认 20）。
        sort: 排序（-created_at / created_at，默认 -created_at）。
        transaction_type: 流水类型筛选（可空）。

    Returns:
        成功包络，data 含 {items, page, page_size, total}。

    Raises:
        BizError: 10001 排序参数非法；30005 积分账户不存在。
    """
    data = await billing_service.list_transactions(
        session,
        user_id,
        page=page,
        page_size=page_size,
        transaction_type=transaction_type,
        sort=sort,
    )
    return success_envelope(data)


@router.post("/orders")
async def post_order(
    body: OrderCreateRequest,
    user_id: Annotated[int, Depends(current_user_id)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """创建订单（套餐订阅 / 积分充值包二选一）。

    沙箱模式返回模拟支付链接 pay_url（携带固定签名，可直接回放为回调）。

    Args:
        body: 请求体（plan_key / credit_pack_credits 二选一）。
        user_id: 当前登录用户 ID（依赖注入）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含订单号/金额/模拟支付链接。

    Raises:
        BizError: 10001 参数二选一约束失败；10002 套餐不存在或已下架。
    """
    data = await billing_service.create_order(
        session, user_id=user_id, plan_key=body.plan_key, credit_pack_credits=body.credit_pack_credits
    )
    return success_envelope(data)


@router.post("/webhooks/payment")
async def post_payment_webhook(
    body: PaymentWebhookRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """支付回调（沙箱固定签名校验 + out_trade_no 幂等，重复回调不重复入账）。

    Args:
        body: 请求体（out_trade_no/status/sign）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含 {out_trade_no, duplicate, order_status, granted_credits}。

    Raises:
        BizError: 10001 回调状态不支持；10002 订单不存在；30004 签名错误。
    """
    data = await billing_service.handle_payment_webhook(
        session, out_trade_no=body.out_trade_no, status=body.status, sign=body.sign
    )
    return success_envelope(data)


@router.post("/subscriptions")
async def post_subscription(
    body: SubscriptionCreateRequest,
    user_id: Annotated[int, Depends(current_user_id)],
    session: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """直接开通/续期订阅（仅沙箱联调；生产环境禁用，防跳过支付）。

    Args:
        body: 请求体（plan_key）。
        user_id: 当前登录用户 ID（依赖注入）。
        session: 请求级数据库会话。

    Returns:
        成功包络，data 含 {plan, granted_credits, credit_account}。

    Raises:
        BizError: 20003 非沙箱环境禁用；10002 套餐不存在或已下架。
    """
    data = await billing_service.create_subscription_direct(session, user_id=user_id, plan_key=body.plan_key)
    return success_envelope(data)

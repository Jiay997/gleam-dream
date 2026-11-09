"""计费域 DTO 模块（Task 3 · 认证与计费域）。

定义计费域全部请求体验证模型（Pydantic v2）：
- 创建订单（套餐订阅 / 积分充值包二选一）；
- 支付回调（沙箱固定签名）；
- 直接开通订阅（沙箱联调）。

分页参数（page / page_size / sort）由路由层以 Query 参数声明；
响应统一走 :func:`app.core.errors.success_envelope` 包络。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OrderCreateRequest(BaseModel):
    """创建订单请求体。

    ``plan_key``（购买套餐订阅）与 ``credit_pack_credits``（积分充值包）
    必须二选一，同时提供或同时缺失视为参数错误。

    Attributes:
        plan_key: 套餐键（free/pro/studio）。
        credit_pack_credits: 充值包积分数（1 积分 = 1 分人民币）。
    """

    model_config = ConfigDict(extra="forbid")

    plan_key: str | None = Field(default=None, min_length=1, max_length=32, description="套餐键")
    credit_pack_credits: int | None = Field(
        default=None, ge=1, le=10_000_000, description="充值包积分数"
    )


class PaymentWebhookRequest(BaseModel):
    """支付回调请求体（沙箱模拟渠道）。

    Attributes:
        out_trade_no: 商户订单号（与订单 order_no 一致，幂等键）。
        status: 支付结果（沙箱仅支持 success）。
        sign: 签名（沙箱固定值，见 billing_service.SANDBOX_WEBHOOK_SIGN）。
    """

    model_config = ConfigDict(extra="forbid")

    out_trade_no: str = Field(min_length=8, max_length=64, description="商户订单号")
    status: str = Field(min_length=1, max_length=16, description="支付结果")
    sign: str = Field(min_length=1, max_length=128, description="回调签名")


class SubscriptionCreateRequest(BaseModel):
    """直接开通订阅请求体（沙箱联调，跳过支付）。

    Attributes:
        plan_key: 套餐键（free/pro/studio）。
    """

    model_config = ConfigDict(extra="forbid")

    plan_key: str = Field(min_length=1, max_length=32, description="套餐键")

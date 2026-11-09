"""域 B · 计费与商业化（Billing）模型。

包含 8 张表（对齐《02 数据库设计文档》§5.2）：
- ``plans`` 套餐定义；
- ``subscriptions`` 订阅实例；
- ``credit_accounts`` 积分账户；
- ``credit_transactions`` 积分流水（只追加账本）；
- ``credit_freezes`` 冻结记录（30 分钟自动解冻）；
- ``orders`` 订单；
- ``payments`` 支付流水（out_trade_no 幂等）；
- ``price_rules`` 模型计费单价规则。

积分/金额一律 BIGINT/INT 整数最小单位（1 积分 = ¥0.01），禁止浮点。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import IdMixin, TimestampMixin


class Plan(IdMixin, TimestampMixin, Base):
    """套餐定义：free/pro/studio 三档定价与权益（全局主数据，表 plans）。

    Attributes:
        key: 套餐键 free/pro/studio（唯一）。
        name: 显示名。
        billing_period: 计费周期 month/year。
        price_cents: 价格（人民币分）。
        currency: 币种（CHAR(3)，默认 CNY）。
        credits_per_period: 每周期赠送积分。
        concurrency_limit: 并发生成任务上限。
        benefits: 权益位 JSONB（如 max_resolution/no_watermark）。
        is_active: 是否在售。
        sort_order: 展示排序。
    """

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(text("key IN ('free', 'pro', 'studio')"), name="ck_plans_key"),
        CheckConstraint(text("billing_period IN ('month', 'year')"), name="ck_plans_billing_period"),
        CheckConstraint(text("price_cents >= 0"), name="ck_plans_price_cents"),
        CheckConstraint(text("credits_per_period >= 0"), name="ck_plans_credits_per_period"),
        UniqueConstraint("key", name="uq_plans_key"),
        Index("idx_plans_is_active_sort_order", "is_active", "sort_order"),
    )

    key: Mapped[str] = mapped_column(String(32), nullable=False, comment="套餐键")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="套餐显示名")
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False, comment="计费周期")
    price_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="价格（分）"
    )
    currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default="CNY", comment="币种"
    )
    credits_per_period: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="周期赠送积分"
    )
    concurrency_limit: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("2"), comment="并发生成上限"
    )
    benefits: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="权益位")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否在售"
    )
    sort_order: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0"), comment="展示排序"
    )


class Subscription(IdMixin, TimestampMixin, Base):
    """订阅实例：用户/团队的套餐订阅与生效期（表 subscriptions）。

    Attributes:
        owner_type: 主体类型 user/team。
        user_id: 用户 ID（owner_type=user 时必填，FK 级联删除）。
        team_id: 团队 ID（owner_type=team 时必填，FK 级联删除）。
        plan_id: 套餐 ID（FK plans，RESTRICT）。
        status: 状态 active/expired/cancelled/past_due。
        started_at: 生效时间。
        expires_at: 到期时间（NULL 表示永久，如 free）。
        auto_renew: 是否自动续费。
        order_id: 来源订单 ID（FK orders，SET NULL）。
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            text("owner_type IN ('user', 'team')"),
            name="ck_subscriptions_owner_type",
        ),
        CheckConstraint(
            text("status IN ('active', 'expired', 'cancelled', 'past_due')"),
            name="ck_subscriptions_status",
        ),
        # 主体一致性：user 主体必须有 user_id 且无 team_id，反之亦然（对齐 02 文档 B-2）
        CheckConstraint(
            text(
                "(owner_type = 'user' AND user_id IS NOT NULL AND team_id IS NULL) "
                "OR (owner_type = 'team' AND team_id IS NOT NULL AND user_id IS NULL)"
            ),
            name="ck_subscriptions_owner",
        ),
        Index("idx_subscriptions_user_id_status", "user_id", "status"),
        Index("idx_subscriptions_team_id_status", "team_id", "status"),
        Index("idx_subscriptions_expires_at", "expires_at"),
    )

    owner_type: Mapped[str] = mapped_column(String(8), nullable=False, comment="主体类型")
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, comment="用户 ID"
    )
    team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, comment="团队 ID"
    )
    plan_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False, comment="套餐 ID"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="active", comment="订阅状态"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="生效时间"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="到期时间（NULL 表示永久）"
    )
    auto_renew: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否自动续费"
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, comment="来源订单 ID"
    )


class CreditAccount(IdMixin, TimestampMixin, Base):
    """积分账户：积分余额容器，用户或团队各一个（表 credit_accounts）。

    1:1 归属（user 或 team 二选一），余额运算须在 DB 事务内以条件 UPDATE 保证非负原子性。

    Attributes:
        owner_type: 主体类型 user/team。
        user_id: 用户 ID（部分唯一，FK 级联删除）。
        team_id: 团队 ID（部分唯一，FK 级联删除）。
        balance_credits: 可用余额（≥0）。
        frozen_credits: 冻结额（≥0）。
        total_granted_credits: 累计获得。
        total_consumed_credits: 累计消耗。
        lock_version: 乐观锁版本。
    """

    __tablename__ = "credit_accounts"
    __table_args__ = (
        CheckConstraint(
            text("owner_type IN ('user', 'team')"),
            name="ck_credit_accounts_owner_type",
        ),
        CheckConstraint(
            text("balance_credits >= 0 AND frozen_credits >= 0"),
            name="ck_credit_accounts_balance",
        ),
        CheckConstraint(
            text(
                "(owner_type = 'user' AND user_id IS NOT NULL AND team_id IS NULL) "
                "OR (owner_type = 'team' AND team_id IS NOT NULL AND user_id IS NULL)"
            ),
            name="ck_credit_accounts_owner",
        ),
        # 用户/团队各一个账户：仅对非空侧做部分唯一
        Index(
            "uq_credit_accounts_user_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_credit_accounts_team_id",
            "team_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL"),
        ),
        Index("idx_credit_accounts_user_id", "user_id"),
        Index("idx_credit_accounts_team_id", "team_id"),
    )

    owner_type: Mapped[str] = mapped_column(String(8), nullable=False, comment="主体类型")
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, comment="用户 ID"
    )
    team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, comment="团队 ID"
    )
    balance_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="可用余额"
    )
    frozen_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="冻结额"
    )
    total_granted_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="累计获得积分"
    )
    total_consumed_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="累计消耗积分"
    )
    lock_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="乐观锁版本"
    )


class CreditTransaction(IdMixin, TimestampMixin, Base):
    """积分流水：不可变账本，只追加（表 credit_transactions）。

    业务上不允许 UPDATE；amount_credits 有符号（正入负出，≠0），balance_after_credits ≥ 0。

    Attributes:
        account_id: 账户 ID（FK credit_accounts，RESTRICT）。
        transaction_type: 类型 grant/recharge/freeze/unfreeze/settle/refund/expire/adjust。
        amount_credits: 变动额（有符号，≠0）。
        balance_after_credits: 变动后可用余额。
        frozen_after_credits: 变动后冻结额。
        related_freeze_id: 关联冻结 ID（弱引用）。
        related_task_id: 关联渲染任务 ID（弱引用）。
        project_id: 关联项目 ID（弱引用）。
        description: 备注。
        idempotency_key: 幂等键（非空时唯一）。
    """

    __tablename__ = "credit_transactions"
    __table_args__ = (
        CheckConstraint(
            text(
                "transaction_type IN ('grant', 'recharge', 'freeze', 'unfreeze', "
                "'settle', 'refund', 'expire', 'adjust')"
            ),
            name="ck_credit_transactions_transaction_type",
        ),
        CheckConstraint(text("amount_credits != 0"), name="ck_credit_transactions_amount"),
        CheckConstraint(
            text("balance_after_credits >= 0"),
            name="ck_credit_transactions_balance_after",
        ),
        Index(
            "uq_credit_transactions_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index("idx_credit_transactions_account_id_created_at", "account_id", "created_at"),
        Index("idx_credit_transactions_project_id_created_at", "project_id", "created_at"),
        Index("idx_credit_transactions_related_task_id", "related_task_id"),
    )

    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("credit_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        comment="账户 ID",
    )
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="流水类型")
    amount_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="变动额（有符号）")
    balance_after_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="变动后可用余额"
    )
    frozen_after_credits: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="变动后冻结额"
    )
    related_freeze_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="关联冻结 ID"
    )
    related_task_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="关联渲染任务 ID"
    )
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="关联项目 ID")
    description: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="备注")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="幂等键")


class CreditFreeze(IdMixin, TimestampMixin, Base):
    """冻结记录：任务执行前的积分预留，含 30 分钟自动解冻（表 credit_freezes）。

    Attributes:
        account_id: 账户 ID（FK credit_accounts，RESTRICT）。
        project_id: 项目 ID（弱引用）。
        render_task_id: 渲染任务 ID（弱引用）。
        frozen_credits: 冻结额（≥0）。
        settled_credits: 实际结算额。
        status: 状态 frozen/settled/released/expired。
        expires_at: 自动解冻时刻（创建 + 30 分钟）。
        settled_at: 结算时刻。
        released_at: 解冻时刻。
    """

    __tablename__ = "credit_freezes"
    __table_args__ = (
        CheckConstraint(text("frozen_credits >= 0"), name="ck_credit_freezes_frozen_credits"),
        CheckConstraint(
            text("status IN ('frozen', 'settled', 'released', 'expired')"),
            name="ck_credit_freezes_status",
        ),
        Index("idx_credit_freezes_status_expires_at", "status", "expires_at"),
        Index("idx_credit_freezes_render_task_id", "render_task_id"),
    )

    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("credit_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        comment="账户 ID",
    )
    project_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="项目 ID")
    render_task_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="渲染任务 ID")
    frozen_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="冻结额")
    settled_credits: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="实际结算额")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="frozen", comment="冻结状态"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="自动解冻时刻"
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结算时刻"
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="解冻时刻"
    )


class Order(IdMixin, TimestampMixin, Base):
    """订单：订阅购买与积分充值订单（表 orders）。

    Attributes:
        order_no: 订单号（业务唯一）。
        owner_type: 主体类型 user/team。
        user_id: 用户 ID（FK 级联删除）。
        team_id: 团队 ID（FK 级联删除）。
        plan_id: 套餐 ID（FK plans，SET NULL）。
        order_type: 订单类型 subscription/credit_pack。
        credits: 购买积分（≥0）。
        amount_cents: 金额（分，≥0）。
        currency: 币种。
        status: 状态 created/paying/paid/failed/refunded/closed。
        paid_at: 支付时间。
        expired_at: 关单时间。
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(text("owner_type IN ('user', 'team')"), name="ck_orders_owner_type"),
        CheckConstraint(
            text("order_type IN ('subscription', 'credit_pack')"),
            name="ck_orders_order_type",
        ),
        CheckConstraint(text("credits >= 0"), name="ck_orders_credits"),
        CheckConstraint(text("amount_cents >= 0"), name="ck_orders_amount_cents"),
        CheckConstraint(
            text("status IN ('created', 'paying', 'paid', 'failed', 'refunded', 'closed')"),
            name="ck_orders_status",
        ),
        UniqueConstraint("order_no", name="uq_orders_order_no"),
        Index("idx_orders_user_id_status", "user_id", "status"),
        Index("idx_orders_created_at", "created_at"),
    )

    order_no: Mapped[str] = mapped_column(String(64), nullable=False, comment="订单号")
    owner_type: Mapped[str] = mapped_column(String(8), nullable=False, comment="主体类型")
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, comment="用户 ID"
    )
    team_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, comment="团队 ID"
    )
    plan_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("plans.id", ondelete="SET NULL"), nullable=True, comment="套餐 ID"
    )
    order_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="订单类型")
    credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0"), comment="购买积分"
    )
    amount_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="金额（分）"
    )
    currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default="CNY", comment="币种"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="created", comment="订单状态"
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="支付时间"
    )
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="关单时间"
    )


class Payment(IdMixin, TimestampMixin, Base):
    """支付流水：支付回调流水，out_trade_no 幂等（表 payments）。

    Attributes:
        order_id: 订单 ID（FK orders，RESTRICT）。
        out_trade_no: 商户订单号（幂等键，唯一）。
        channel: 支付渠道 wechat/alipay。
        channel_trade_no: 渠道流水号。
        amount_cents: 支付金额（分，≥0）。
        status: 状态 init/success/failed/refunded。
        paid_at: 支付时间。
        raw_callback: 回调原文 JSONB。
    """

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(text("channel IN ('wechat', 'alipay')"), name="ck_payments_channel"),
        CheckConstraint(text("amount_cents >= 0"), name="ck_payments_amount_cents"),
        CheckConstraint(
            text("status IN ('init', 'success', 'failed', 'refunded')"),
            name="ck_payments_status",
        ),
        UniqueConstraint("out_trade_no", name="uq_payments_out_trade_no"),
        Index("idx_payments_order_id", "order_id"),
        Index("idx_payments_status_created_at", "status", "created_at"),
    )

    order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, comment="订单 ID"
    )
    out_trade_no: Mapped[str] = mapped_column(String(64), nullable=False, comment="商户订单号")
    channel: Mapped[str] = mapped_column(String(16), nullable=False, comment="支付渠道")
    channel_trade_no: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="渠道流水号"
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, comment="支付金额（分）")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="init", comment="支付状态"
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="支付时间"
    )
    raw_callback: Mapped[dict | None] = mapped_column(JSONB, nullable=True, comment="回调原文")


class PriceRule(IdMixin, TimestampMixin, Base):
    """计费单价规则：模型计费单价，按任务类型与生效期版本化（表 price_rules）。

    与 model_registry.model_key 为逻辑关联（无物理外键）。

    Attributes:
        model_key: 模型键。
        task_type: 任务类型（同 render_tasks.task_type 枚举）。
        unit: 计费单位 per_image/per_second/per_1k_token/per_call。
        unit_price_credits: 单位单价（积分，≥0）。
        currency_basis: 计费基准币种。
        effective_from: 生效起始时间。
        effective_to: 生效结束时间（NULL 表示当前有效）。
        is_active: 是否启用。
    """

    __tablename__ = "price_rules"
    __table_args__ = (
        # 与 model_routes 对齐：9 种渲染任务类型 + llm_text（Agent 文本生成计费，
        # 见 00 契约 Q2 模型路由与种子数据要求）
        CheckConstraint(
            text(
                "task_type IN ('text_to_image', 'image_to_image', 'text_to_video', "
                "'image_to_video', 'tts', 'bgm', 'subtitle', 'compose', 'upscale', 'llm_text')"
            ),
            name="ck_price_rules_task_type",
        ),
        CheckConstraint(
            text("unit IN ('per_image', 'per_second', 'per_1k_token', 'per_call')"),
            name="ck_price_rules_unit",
        ),
        CheckConstraint(text("unit_price_credits >= 0"), name="ck_price_rules_unit_price"),
        UniqueConstraint(
            "model_key", "task_type", "effective_from",
            name="uq_price_rules_model_key_task_type_effective_from",
        ),
        Index("idx_price_rules_model_key_task_type", "model_key", "task_type"),
        Index("idx_price_rules_is_active", "is_active"),
    )

    model_key: Mapped[str] = mapped_column(String(64), nullable=False, comment="模型键")
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="任务类型")
    unit: Mapped[str] = mapped_column(String(32), nullable=False, comment="计费单位")
    unit_price_credits: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="单位单价（积分）")
    currency_basis: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, server_default="CNY", comment="计费基准币种"
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), comment="生效起始时间"
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="生效结束时间（NULL 表示当前有效）"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否启用"
    )

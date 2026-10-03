import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Integer, String, BigInteger, ForeignKey, Text, Boolean, Date, JSON,
    DateTime, Numeric, Index, UniqueConstraint, CheckConstraint, func, select
)
from sqlalchemy.orm import relationship, Mapped, mapped_column
from bot.database.main import Database


class Permission:
    USE             = 1 << 0   #   1 — basic access
    BROADCAST       = 1 << 1   #   2 — mass messaging
    SETTINGS_MANAGE = 1 << 2   #   4 — bot settings (maintenance, etc.)
    USERS_MANAGE    = 1 << 3   #   8 — view/block/unblock users, referrals, purchases
    CATALOG_MANAGE  = 1 << 4   #  16 — categories, positions, items/goods CRUD
    ADMINS_MANAGE   = 1 << 5   #  32 — role CRUD, role assignment
    OWN             = 1 << 6   #  64 — owner-only operations
    STATS_VIEW      = 1 << 7   # 128 — statistics, logs, bought-item search
    BALANCE_MANAGE  = 1 << 8   # 256 — top-up / deduct user balance
    PROMO_MANAGE    = 1 << 9   # 512 — promo code CRUD

    @staticmethod
    def is_subset(perms: int, of: int) -> bool:
        """True if every bit in `perms` is also set in `of`."""
        return (perms & ~of) == 0

    @staticmethod
    def has_any_admin_perm(perms: int) -> bool:
        """True if `perms` has any permission beyond USE."""
        return (perms & ~Permission.USE) != 0

    @staticmethod
    def granted(perms: int, bit: int) -> bool:
        """True if every bit in `bit` is set in `perms` (same AND semantics as HasPermissionFilter)."""
        return (perms & bit) == bit


class Role(Database.BASE):
    __tablename__ = 'roles'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    default: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, index=True)
    permissions: Mapped[Optional[int]] = mapped_column(Integer)
    users: Mapped[list["User"]] = relationship('User', backref='role', lazy='raise')

    __table_args__ = (
        CheckConstraint(
            'permissions IS NULL OR (permissions >= 0 AND permissions <= 1023)',
            name='ck_roles_permissions_known_bits',
        ),
    )

    def __str__(self):
        return self.name or ""

    @staticmethod
    async def insert_roles():
        roles = {
            'USER': [Permission.USE],
            'ADMIN': [Permission.USE, Permission.BROADCAST,
                      Permission.SETTINGS_MANAGE, Permission.USERS_MANAGE,
                      Permission.CATALOG_MANAGE, Permission.STATS_VIEW,
                      Permission.BALANCE_MANAGE, Permission.PROMO_MANAGE],
            'OWNER': [Permission.USE, Permission.BROADCAST,
                      Permission.SETTINGS_MANAGE, Permission.USERS_MANAGE,
                      Permission.CATALOG_MANAGE, Permission.ADMINS_MANAGE,
                      Permission.OWN, Permission.STATS_VIEW,
                      Permission.BALANCE_MANAGE, Permission.PROMO_MANAGE],
        }
        default_role = 'USER'
        async with Database().session() as s:
            for r, perms in roles.items():
                result = await s.execute(select(Role).filter_by(name=r))
                role = result.scalars().first()
                if role is None:
                    role = Role(name=r)
                    s.add(role)
                role.reset_permissions()
                for perm in perms:
                    role.add_permission(perm)
                role.default = (role.name == default_role)

    def add_permission(self, perm):
        self.permissions |= perm

    def remove_permission(self, perm):
        self.permissions &= ~perm

    def reset_permissions(self):
        self.permissions = 0

    def has_permission(self, perm):
        return self.permissions & perm == perm

    def __repr__(self):
        return '<Role %r>' % self.name


class WebAdmin(Database.BASE):
    """Web panel login bound to a bot role.

    The owner always logs in with ADMIN_USERNAME/ADMIN_PASSWORD from the env.
    Extra people get personal logins here: the linked role's permission bits
    decide which admin views they may open (roles themselves stay
    owner-only, so nobody can escalate their own rights).
    """

    __tablename__ = 'web_admins'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("roles.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    def __str__(self):
        return self.login or ""


class User(Database.BASE):
    __tablename__ = 'users'
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey('roles.id', ondelete="RESTRICT"), default=None, index=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    referral_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete="SET NULL"), nullable=True, index=True)
    registration_date: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    is_blocked: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, index=True)
    locale: Mapped[str] = mapped_column(String(5), nullable=False, default="ru")
    # Retained for compatibility with older deployments. The subscription
    # middleware now requires the community chat for every account.
    community_chat_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true")
    community_prompt_seen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false")
    has_started: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True)
    user_operations: Mapped[list["Operations"]] = relationship(
        "Operations", back_populates="user_telegram_id", lazy='raise')
    user_goods: Mapped[list["BoughtGoods"]] = relationship(
        "BoughtGoods", back_populates="user_telegram_id", lazy='raise')

    __table_args__ = (
        CheckConstraint('referral_id != telegram_id', name='ck_users_no_self_referral'),
        Index('ix_users_registration_date', 'registration_date'),
    )

    referral_earnings_received: Mapped[list["ReferralEarnings"]] = relationship(
        "ReferralEarnings",
        foreign_keys="ReferralEarnings.referrer_id",
        back_populates="referrer",
        lazy='raise',
    )
    referral_earnings_generated: Mapped[list["ReferralEarnings"]] = relationship(
        "ReferralEarnings",
        foreign_keys="ReferralEarnings.referral_id",
        back_populates="referral",
        lazy='raise',
    )

    def __str__(self):
        return str(self.telegram_id)


class PartnerApiKey(Database.BASE):
    """One revocable server-to-server credential bound to a Telegram account."""

    __tablename__ = "partner_api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    last_used_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True)

    __table_args__ = (
        Index("ix_partner_api_keys_user_revoked", "user_id", "revoked_at"),
    )


class ApiIdempotency(Database.BASE):
    """Persistent replay guard for partner API writes; secret delivery stays in BoughtGoods."""

    __tablename__ = "api_idempotency"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_key_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("partner_api_keys.id", ondelete="CASCADE"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(40), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    result_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("api_key_id", "idempotency_key", name="uq_api_idempotency_key"),
        Index("ix_api_idempotency_created", "created_at"),
    )


class BotUserDailyActivity(Database.BASE):
    """Privacy-minimal per-user/day counts; message and callback contents are never stored."""

    __tablename__ = "bot_user_daily_activity"
    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    activity_date: Mapped[datetime.date] = mapped_column(Date, primary_key=True)
    start_click_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")
    interaction_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("start_click_count >= 0", name="ck_bot_activity_start_nonnegative"),
        CheckConstraint("interaction_count >= 0", name="ck_bot_activity_interaction_nonnegative"),
        Index("ix_bot_activity_date", "activity_date"),
    )


class Categories(Database.BASE):
    __tablename__ = 'categories'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    parent_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey('categories.id', ondelete="CASCADE"), nullable=True,
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    image_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parent: Mapped[Optional["Categories"]] = relationship(
        "Categories", remote_side=[id], back_populates="children", lazy='raise'
    )
    children: Mapped[list["Categories"]] = relationship(
        "Categories", back_populates="parent", lazy='raise', passive_deletes=True
    )
    items: Mapped[list["Goods"]] = relationship(
        "Goods", back_populates="category", lazy='raise', passive_deletes=True)

    def __str__(self):
        return self.name or ""


class Goods(Database.BASE):
    __tablename__ = 'goods'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    image_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    availability_note: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    stock_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    delivery_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_variable_pricing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    min_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    max_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_vpn_subscription: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    category_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('categories.id', ondelete="CASCADE"), nullable=False, index=True)
    sale_percent: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), nullable=True)
    sale_until: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    category: Mapped["Categories"] = relationship("Categories", back_populates="items", lazy='raise')
    values: Mapped[list["ItemValues"]] = relationship(
        "ItemValues", back_populates="item", lazy='raise', passive_deletes=True)

    __table_args__ = (
        CheckConstraint(
            "CAST(price AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND price > 0",
            name='ck_goods_price_positive',
        ),
        CheckConstraint(
            "sale_percent IS NULL OR (CAST(sale_percent AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND sale_percent >= 0 AND sale_percent <= 100)",
            name='ck_goods_sale_percent_range',
        ),
        CheckConstraint('stock_quantity >= 0', name='ck_goods_stock_quantity_nonnegative'),
        CheckConstraint(
            "(is_variable_pricing = false AND min_quantity IS NULL AND max_quantity IS NULL) "
            "OR (is_variable_pricing = true AND min_quantity >= 1 "
            "AND max_quantity >= min_quantity AND max_quantity <= 5000)",
            name='ck_goods_variable_quantity_range',
        ),
    )

    def __str__(self):
        return self.name or ""


class ItemValues(Database.BASE):
    __tablename__ = 'item_values'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('goods.id', ondelete="CASCADE"), nullable=False, index=True)
    value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_infinity: Mapped[bool] = mapped_column(Boolean, nullable=False)
    item: Mapped["Goods"] = relationship("Goods", back_populates="values", lazy='raise')

    __table_args__ = (
        UniqueConstraint('item_id', 'value', name='uq_item_value_per_item'),
        CheckConstraint(
            "value IS NOT NULL AND length(trim(value)) > 0",
            name='ck_item_values_value_nonempty',
        ),
        Index('ix_item_values_item_inf', 'item_id', 'is_infinity'),
    )

    def __str__(self):
        return f"#{self.id} ({self.item_id})"


class BoughtGoods(Database.BASE):
    __tablename__ = 'bought_goods'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    buyer_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete="SET NULL"), nullable=True, index=True)
    bought_datetime: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    unique_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    user_telegram_id: Mapped[Optional["User"]] = relationship(
        "User", back_populates="user_goods", lazy='raise')

    __table_args__ = (
        Index('ix_bought_goods_datetime', 'bought_datetime'),
        Index('ix_bought_goods_buyer_datetime_id', 'buyer_id', 'bought_datetime', 'id'),
    )

    def __str__(self):
        return self.item_name or ""


class Operations(Database.BASE):
    __tablename__ = 'operations'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete="SET NULL"), nullable=True, index=True)
    operation_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    operation_time: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    user_telegram_id: Mapped[Optional["User"]] = relationship(
        "User", back_populates="user_operations", lazy='raise')

    __table_args__ = (
        Index('ix_operations_time', 'operation_time'),
    )

    def __str__(self):
        return f"#{self.id}"


class Payments(Database.BASE):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete="SET NULL"), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint('provider', 'external_id', name='uq_payment_provider_ext'),
        CheckConstraint(
            "CAST(amount AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND amount > 0",
            name='ck_payments_amount_positive',
        ),
        Index('ix_payments_status_created', 'status', 'created_at'),
    )

    def __str__(self):
        return f"{self.provider}:{self.external_id}"


class ManualRevenue(Database.BASE):
    """Operator-entered revenue that is kept separate from real purchases."""

    __tablename__ = "manual_revenues"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint("quantity > 0 AND quantity <= 100000", name="ck_manual_revenue_quantity"),
        CheckConstraint(
            "CAST(unit_price AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND unit_price > 0",
            name="ck_manual_revenue_unit_price",
        ),
        Index("ix_manual_revenues_created_at_id", "created_at", "id"),
    )

    def __str__(self):
        return f"{self.category_name}: {self.quantity}"


class ProductExpense(Database.BASE):
    """Operator-entered stock purchase expense, separate from live inventory."""

    __tablename__ = "product_expenses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("goods.id", ondelete="SET NULL"), nullable=True
    )
    finance_receipt_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("finance_receipts.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    product_name: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "quantity > 0 AND quantity <= 100000",
            name="ck_product_expenses_quantity",
        ),
        CheckConstraint(
            "CAST(total_cost AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') "
            "AND total_cost > 0",
            name="ck_product_expenses_total_cost",
        ),
        Index("ix_product_expenses_created_at_id", "created_at", "id"),
        Index("ix_product_expenses_product_created", "product_id", "created_at", "id"),
    )

    def __str__(self):
        return f"{self.product_name}: {self.quantity}"


class FinanceReceipt(Database.BASE):
    """External cash received; bookkeeping only, never a customer-wallet credit."""

    __tablename__ = "finance_receipts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    expenses: Mapped[list["ProductExpense"]] = relationship(
        "ProductExpense", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint("source IN ('crypto', 'card', 'cash', 'other')", name="ck_finance_receipt_source"),
        CheckConstraint("currency IN ('RUB', 'USDT', 'USD', 'BYN')", name="ck_finance_receipt_currency"),
        CheckConstraint(
            "CAST(amount AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND amount > 0",
            name="ck_finance_receipt_amount_positive",
        ),
        CheckConstraint(
            "CAST(amount_rub AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND amount_rub > 0",
            name="ck_finance_receipt_rub_positive",
        ),
        UniqueConstraint("source", "reference", name="uq_finance_receipt_source_reference"),
        Index("ix_finance_receipts_received_at_id", "received_at", "id"),
    )

    def __str__(self):
        return f"#{self.id} {self.source} {self.amount} {self.currency}"


class ProcurementPlan(Database.BASE):
    """Saved forecast only; this is deliberately separate from real expenses."""

    __tablename__ = "procurement_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_date: Mapped[datetime.date] = mapped_column(Date, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    expected_revenue: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross_profit: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross_margin_percent: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    return_on_cost_percent: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    items: Mapped[list["ProcurementPlanItem"]] = relationship(
        "ProcurementPlanItem",
        back_populates="plan",
        cascade="all, delete-orphan",
        lazy="raise",
        order_by="ProcurementPlanItem.id",
    )

    __table_args__ = (
        CheckConstraint(
            "CAST(total_cost AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND total_cost > 0",
            name="ck_procurement_plans_total_cost",
        ),
        CheckConstraint(
            "CAST(expected_revenue AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND expected_revenue > 0",
            name="ck_procurement_plans_expected_revenue",
        ),
        CheckConstraint(
            "CAST(gross_profit AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_procurement_plans_gross_profit",
        ),
        Index("ix_procurement_plans_date_id", "plan_date", "id"),
    )

    def __str__(self):
        return self.title or f"План #{self.id}"


class ProcurementPlanItem(Database.BASE):
    """Immutable-at-save product/category and price snapshot for one plan line."""

    __tablename__ = "procurement_plan_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("procurement_plans.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("goods.id", ondelete="SET NULL"), nullable=True
    )
    category_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    category_name: Mapped[str] = mapped_column(String(100), nullable=False)
    product_name: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    sale_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    sale_price_mode: Mapped[str] = mapped_column(String(12), nullable=False)
    total_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    expected_revenue: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    gross_profit: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    plan: Mapped["ProcurementPlan"] = relationship(
        "ProcurementPlan", back_populates="items", lazy="raise"
    )

    __table_args__ = (
        CheckConstraint(
            "quantity > 0 AND quantity <= 100000",
            name="ck_procurement_plan_items_quantity",
        ),
        CheckConstraint(
            "CAST(unit_cost AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND unit_cost > 0",
            name="ck_procurement_plan_items_unit_cost",
        ),
        CheckConstraint(
            "CAST(sale_price AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND sale_price > 0",
            name="ck_procurement_plan_items_sale_price",
        ),
        CheckConstraint(
            "sale_price_mode IN ('catalog', 'manual')",
            name="ck_procurement_plan_items_sale_mode",
        ),
        CheckConstraint(
            "CAST(total_cost AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND total_cost > 0",
            name="ck_procurement_plan_items_total_cost",
        ),
        CheckConstraint(
            "CAST(expected_revenue AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity') AND expected_revenue > 0",
            name="ck_procurement_plan_items_expected_revenue",
        ),
        CheckConstraint(
            "CAST(gross_profit AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity')",
            name="ck_procurement_plan_items_gross_profit",
        ),
        UniqueConstraint("plan_id", "product_id", name="uq_procurement_plan_product"),
        Index("ix_procurement_plan_items_plan_id_id", "plan_id", "id"),
        Index("ix_procurement_plan_items_product_id", "product_id"),
        Index("ix_procurement_plan_items_category_id", "category_id"),
    )

    def __str__(self):
        return f"{self.product_name}: {self.quantity}"


class ReferralEarnings(Database.BASE):
    __tablename__ = 'referral_earnings'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete="CASCADE"), nullable=False, index=True)
    referral_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete="CASCADE"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    original_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    referrer: Mapped["User"] = relationship(
        "User",
        foreign_keys="ReferralEarnings.referrer_id",
        back_populates="referral_earnings_received",
        lazy='raise',
    )
    referral: Mapped["User"] = relationship(
        "User",
        foreign_keys="ReferralEarnings.referral_id",
        back_populates="referral_earnings_generated",
        lazy='raise',
    )

    __table_args__ = (
        CheckConstraint('referrer_id != referral_id', name='ck_referral_earnings_no_self_referral'),
        Index('ix_referral_earnings_referrer_created_id', 'referrer_id', 'created_at', 'id'),
        Index('ix_referral_earnings_referral_created_id', 'referral_id', 'created_at', 'id'),
        Index('ix_referral_earnings_pair_created', 'referrer_id', 'referral_id', 'created_at', 'id'),
    )

    def __str__(self):
        return f"#{self.id}"


class AuditLog(Database.BASE):
    __tablename__ = 'audit_log'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="INFO")
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    __table_args__ = (
        Index('ix_audit_log_timestamp', 'timestamp'),
        Index('ix_audit_log_user_id', 'user_id'),
        Index('ix_audit_log_action', 'action'),
    )

    def __repr__(self):
        return f'<AuditLog {self.action} user={self.user_id} @ {self.timestamp}>'

    def __str__(self):
        return self.action or ""


class PromoCodes(Database.BASE):
    __tablename__ = 'promo_codes'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    discount_type: Mapped[str] = mapped_column(String(10), nullable=False)  # 'percent' | 'fixed' | 'balance'
    discount_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default='global')
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 0 = unlimited
    current_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    category_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey('categories.id', ondelete='SET NULL'), nullable=True, index=True)
    item_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey('goods.id', ondelete='SET NULL'), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("scope IN ('global','category','item')", name='ck_promo_codes_scope'),
        CheckConstraint('discount_value >= 0', name='ck_promo_discount_nonneg'),
        CheckConstraint(
            'category_id IS NULL OR item_id IS NULL',
            name='ck_promo_single_binding',
        ),
        Index('ix_promo_codes_created_id', 'created_at', 'id'),
    )

    def __str__(self):
        return self.code or ""


def promo_scope_for(category_id: Optional[int], item_id: Optional[int]) -> str:
    """Derive a promo's scope discriminator from its bindings (item wins).

    Item-first matches the precedence in promo_rule_error.
    """
    if item_id is not None:
        return 'item'
    if category_id is not None:
        return 'category'
    return 'global'


class PromoCodeUsages(Database.BASE):
    __tablename__ = 'promo_code_usages'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('promo_codes.id', ondelete='CASCADE'), nullable=False)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False)
    used_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (UniqueConstraint('promo_id', 'user_id', name='uq_promo_usage_per_user'),)


class CartItems(Database.BASE):
    __tablename__ = 'cart_items'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('goods.id', ondelete='CASCADE'), nullable=False, index=True)
    promo_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    added_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint('user_id', 'item_id', name='uq_cart_item_per_user'),
        CheckConstraint('quantity > 0 AND quantity <= 5000', name='ck_cart_items_quantity_range'),
    )

    def __str__(self):
        return f"cart#{self.id} item={self.item_id} x{self.quantity}"


class Reviews(Database.BASE):
    __tablename__ = 'reviews'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('goods.id', ondelete='CASCADE'), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-5
    text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint('user_id', 'item_id', name='uq_review_per_user_item'),
        CheckConstraint('rating >= 1 AND rating <= 5', name='ck_review_rating_range'),
        Index('ix_reviews_item_created_id', 'item_id', 'created_at', 'id'),
    )

    def __str__(self):
        return f"item {self.item_id} ({self.rating}★)"


class StockSubscriptions(Database.BASE):
    __tablename__ = 'stock_subscriptions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False, index=True)
    item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey('goods.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint('user_id', 'item_id', name='uq_stock_sub_per_user_item'),
    )

    def __str__(self):
        return f"sub u={self.user_id} item={self.item_id}"


class VpnSubscriptionLink(Database.BASE):
    """One opaque, revocable proxy link issued for a specific purchase/user."""

    __tablename__ = "vpn_subscription_links"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[Optional[datetime.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_vpn_subscription_links_user_revoked", "user_id", "revoked_at"),
    )

    def __str__(self):
        return f"VPN subscription link for {self.user_id}"


async def register_models():
    """Seed the built-in roles (USER/ADMIN/OWNER)."""
    await Role.insert_roles()

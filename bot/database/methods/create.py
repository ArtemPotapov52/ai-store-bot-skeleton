from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, exists, func as sa_func, insert as sa_insert
from sqlalchemy.exc import IntegrityError

from bot.database.models import User, ItemValues, Goods, Categories, Payments, Role
from bot.database.models.main import PromoCodes, CartItems, Reviews, StockSubscriptions
from bot.database import Database
from bot.database.methods.cache_utils import safe_create_task
from bot.database.methods.read import (
    invalidate_stats_cache,
    invalidate_item_cache,
    invalidate_category_cache,
    find_goods_by_name,
    normalize_item_name,
)
from bot.database.methods.pricing import MAX_PURCHASE_QUANTITY, purchase_quantity_limits

# Cart limits: distinct positions per cart, and units of any one position.
CART_MAX_ITEMS = 10
CART_MAX_QTY_PER_ITEM = MAX_PURCHASE_QUANTITY


async def create_user(telegram_id: int, registration_date: datetime, referral_id: int | None, role: int | None = None) -> None:
    """Create user if missing; commit.

    ``role`` must be a valid role id. When omitted, the current default role
    is resolved (re-seeding the built-in roles if needed) instead of assuming
    the historical ``id=1``, which breaks after roles are recreated.
    """
    if role is None:
        from bot.database.methods.read import get_default_user_role_id
        role = await get_default_user_role_id()
    if role is None:
        from bot.logger_mesh import logger
        logger.error("create_user(%s) skipped: no default role available", telegram_id)
        return
    async with Database().session() as s:
        result = await s.execute(select(exists().where(User.telegram_id == telegram_id)))
        if result.scalar():
            return
        s.add(
            User(
                telegram_id=telegram_id,
                role_id=role,
                registration_date=registration_date,
                referral_id=referral_id,
            )
        )
        try:
            await s.flush()
        except IntegrityError:
            # Lost the race — the user now exists, which is the desired outcome.
            await s.rollback()


async def create_item(
        item_name: str,
        item_description: str,
        item_price: int,
        category_name: str,
        stock_quantity: int = 0,
        delivery_text: str | None = None,
) -> int | None:
    """Insert an item and return its id, or ``None`` when it cannot be created.

    Resolves ``category_name`` to its database id and commits the product.

    ``stock_quantity`` is the simple, counted stock configured on the product
    card. Individual account/key rows remain available through ``ItemValues``.
    """
    item_name = normalize_item_name(item_name)
    if not item_name:
        return None
    try:
        normalized_price = Decimal(str(item_price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("price must be a finite positive number") from exc
    if not normalized_price.is_finite() or normalized_price <= 0:
        raise ValueError("price must be a finite positive number")
    quantity = max(0, int(stock_quantity or 0))
    text = (delivery_text or "").strip() or None
    async with Database().session() as s:
        if await find_goods_by_name(s, item_name):
            return
        cat = (await s.execute(
            select(Categories.id).where(Categories.name == category_name)
        )).scalar()
        if not cat:
            return
        # Parent rows are navigation groups, never product destinations.
        if await s.scalar(select(exists().where(Categories.parent_id == cat))):
            return None
        goods = Goods(
            name=item_name,
            description=item_description,
            price=normalized_price,
            category_id=cat,
            stock_quantity=quantity,
            delivery_text=text,
        )
        s.add(goods)
        await s.flush()
        item_id = int(goods.id)

    safe_create_task(invalidate_stats_cache())
    # The category's cached item count changed.
    safe_create_task(invalidate_category_cache(category_name))
    return item_id


async def add_values_to_item(item_name: str, value: str, is_infinity: bool) -> bool:
    """Add item value if not duplicate; True if inserted. Resolves item_name to item_id."""
    item_name = normalize_item_name(item_name)
    value_norm = (value or "").strip()
    if not value_norm:
        return False

    try:
        async with Database().session() as s:
            goods = await find_goods_by_name(s, item_name)
            if goods is None:
                return False
            item_id = goods.id

            dup = (await s.execute(
                select(exists().where(
                    ItemValues.item_id == item_id,
                    ItemValues.value == value_norm,
                ))
            )).scalar()
            if dup:
                return False

            s.add(ItemValues(item_id=item_id, value=value_norm, is_infinity=bool(is_infinity)))
    except IntegrityError:
        return False

    # Invalidate only after the commit succeeded.
    safe_create_task(invalidate_item_cache(item_name))
    return True


def normalize_values(values: list[str]) -> tuple[list[str], int, int]:
    """Trim, drop blanks and de-duplicate a batch of stock values.

    Returns ``(normalized, skipped_batch_dup, skipped_invalid)``. Shared by the
    bulk insert and replace_item_stock_and_meta so the two cannot drift.
    """
    seen: set[str] = set()
    normalized: list[str] = []
    skipped_dup = 0
    skipped_invalid = 0
    for v in values:
        v_norm = (v or "").strip()
        if not v_norm:
            skipped_invalid += 1
        elif v_norm in seen:
            skipped_dup += 1
        else:
            seen.add(v_norm)
            normalized.append(v_norm)
    return normalized, skipped_dup, skipped_invalid


async def add_values_bulk(
        item_name: str, values: list[str], is_infinity: bool = False
) -> tuple[int, int, int, int]:
    """Add a whole batch of stock values in one transaction.

    Returns ``(added, skipped_db_dup, skipped_batch_dup, skipped_invalid)``.

    ``is_infinity`` keeps the semantics of the single-value path: an infinite
    position holds exactly one row that is never consumed, so only the first
    value counts.
    """
    item_name = normalize_item_name(item_name)
    if not item_name:
        return 0, 0, 0, len(values)
    normalized, skipped_batch_dup, skipped_invalid = normalize_values(values)
    if is_infinity:
        normalized = normalized[:1]
    if not normalized:
        return 0, 0, skipped_batch_dup, skipped_invalid

    try:
        async with Database().session() as s:
            goods = await find_goods_by_name(s, item_name)
            if goods is None:
                return 0, 0, skipped_batch_dup, skipped_invalid
            item_id = goods.id

            existing = set((await s.execute(
                select(ItemValues.value).where(
                    ItemValues.item_id == item_id,
                    ItemValues.value.in_(normalized),
                )
            )).scalars().all())

            to_insert = [v for v in normalized if v not in existing]
            if to_insert:
                # Core insert with a list of parameter sets: one executemany
                await s.execute(
                    sa_insert(ItemValues),
                    [
                        {"item_id": item_id, "value": v, "is_infinity": bool(is_infinity)}
                        for v in to_insert
                    ],
                )

            added = len(to_insert)
            skipped_db_dup = len(normalized) - added
    except IntegrityError:
        # Lost the uq_item_value_per_item race against a concurrent upload: the
        # batch rolled back as a whole, so fall back to inserting one at a time
        # (idempotent) rather than reporting a failure for values that are fine.
        added = 0
        skipped_db_dup = 0
        for v in normalized:
            if await add_values_to_item(item_name, v, is_infinity):
                added += 1
            else:
                skipped_db_dup += 1
        return added, skipped_db_dup, skipped_batch_dup, skipped_invalid

    if added:
        safe_create_task(invalidate_item_cache(item_name))
    return added, skipped_db_dup, skipped_batch_dup, skipped_invalid


async def create_category(category_name: str) -> None:
    """Insert category; commit."""
    async with Database().session() as s:
        result = await s.execute(select(exists().where(Categories.name == category_name)))
        if result.scalar():
            return
        s.add(Categories(name=category_name))

    safe_create_task(invalidate_stats_cache())
    # Drops the cached categories:count
    safe_create_task(invalidate_category_cache(category_name))


async def create_pending_payment(provider: str, external_id: str, user_id: int, amount: int, currency: str) -> None:
    """Create a durable pending payment intent before calling a provider."""
    try:
        normalized_amount = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Payment amount must be a number") from exc
    if not normalized_amount.is_finite() or normalized_amount <= 0:
        raise ValueError("Payment amount must be greater than zero")
    provider = str(provider or "").strip().lower()
    external_id = str(external_id or "").strip()
    currency = str(currency or "").strip().upper()
    if not provider or not external_id or not currency:
        raise ValueError("Payment intent identifiers are required")
    async with Database().session() as s:
        s.add(Payments(
            provider=provider,
            external_id=external_id,
            user_id=user_id,
            amount=normalized_amount,
            currency=currency,
            status="pending"
        ))


async def bind_pending_payment(provider: str, pending_external_id: str, provider_external_id: str) -> bool:
    """Bind a committed local intent to the provider invoice ID."""
    provider = str(provider or "").strip().lower()
    pending_external_id = str(pending_external_id or "").strip()
    provider_external_id = str(provider_external_id or "").strip()
    if not provider or not pending_external_id or not provider_external_id:
        return False
    async with Database().session() as s:
        payment = (await s.execute(
            select(Payments).where(
                Payments.provider == provider,
                Payments.external_id == pending_external_id,
            ).with_for_update()
        )).scalars().one_or_none()
        if payment is None:
            # A fast provider callback may have already rebound this intent
            # before the create-link request returns to the bot handler.
            already_bound = await s.execute(
                select(Payments.id).where(
                    Payments.provider == provider,
                    Payments.external_id == provider_external_id,
                )
            )
            return already_bound.scalar_one_or_none() is not None
        if payment.external_id == provider_external_id:
            return True
        collision = (await s.execute(
            select(Payments.id).where(
                Payments.provider == provider,
                Payments.external_id == provider_external_id,
                Payments.id != payment.id,
            )
        )).scalar_one_or_none()
        if collision is not None:
            raise ValueError("Provider invoice ID is already linked")
        payment.external_id = provider_external_id
    return True


async def get_payment_record(provider: str, external_id: str) -> dict | None:
    """Read the minimal payment fields needed to validate a provider callback."""
    async with Database().session() as s:
        payment = (await s.execute(
            select(Payments).where(
                Payments.provider == str(provider or "").strip().lower(),
                Payments.external_id == str(external_id or "").strip(),
            )
        )).scalars().one_or_none()
        if payment is None:
            return None
        return {
            "provider": payment.provider,
            "external_id": payment.external_id,
            "user_id": payment.user_id,
            "amount": Decimal(str(payment.amount)),
            "currency": str(payment.currency or "").upper(),
            "status": str(payment.status or "").lower(),
        }


async def mark_pending_payment_failed(provider: str, external_id: str) -> bool:
    """Mark an intent failed when provider creation did not complete."""
    async with Database().session() as s:
        payment = (await s.execute(
            select(Payments).where(
                Payments.provider == str(provider or "").strip().lower(),
                Payments.external_id == str(external_id or "").strip(),
                Payments.status == "pending",
            ).with_for_update()
        )).scalars().one_or_none()
        if payment is None:
            return False
        payment.status = "failed"
    return True


async def reopen_failed_payment(provider: str, external_id: str) -> bool:
    """Reopen a failed intent only after an authenticated provider confirms it."""
    async with Database().session() as s:
        payment = (await s.execute(
            select(Payments).where(
                Payments.provider == str(provider or "").strip().lower(),
                Payments.external_id == str(external_id or "").strip(),
                Payments.status == "failed",
            ).with_for_update()
        )).scalars().one_or_none()
        if payment is None:
            return False
        payment.status = "pending"
    return True


async def mark_payment_chargebacked(provider: str, external_id: str) -> bool:
    """Flag a provider-reported refund for manual balance reconciliation."""
    async with Database().session() as s:
        payment = (await s.execute(
            select(Payments).where(
                Payments.provider == str(provider or "").strip().lower(),
                Payments.external_id == str(external_id or "").strip(),
                Payments.status.in_(("pending", "succeeded", "failed")),
            ).with_for_update()
        )).scalars().one_or_none()
        if payment is None:
            return False
        payment.status = "chargebacked"
    return True


async def create_role(name: str, permissions: int) -> int | None:
    """Create a new role. Returns the new role ID, or None if name conflict."""
    async with Database().session() as s:
        result = await s.execute(select(exists().where(Role.name == name)))
        if result.scalar():
            return None
        role = Role(name=name, permissions=permissions)
        s.add(role)
        await s.flush()
        return role.id


async def create_promo_code(
        code: str,
        discount_type: str,
        discount_value,
        max_uses: int = 0,
        expires_at=None,
) -> int | None:
    """Create a balance-credit promo. Returns ID or None if code already exists."""
    from decimal import Decimal

    if discount_type != "balance":
        raise ValueError("Purchase discount promo codes are disabled.")

    async with Database().session() as s:
        result = await s.execute(select(exists().where(PromoCodes.code == code.upper())))
        if result.scalar():
            return None
        promo = PromoCodes(
            code=code.upper(),
            discount_type=discount_type,
            discount_value=Decimal(str(discount_value)),
            scope="global",
            max_uses=max_uses,
            expires_at=expires_at,
        )
        s.add(promo)
        await s.flush()
        return promo.id


async def _add_to_cart_once(user_id: int, item_name: str, promo_code: str, quantity: int) -> tuple[bool, str]:
    """One attempt at add_to_cart. See add_to_cart for semantics."""
    item_name = normalize_item_name(item_name)
    async with Database().session() as s:
        # Resolve to the goods id (also serves as the existence check).
        goods = await find_goods_by_name(s, item_name)
        if goods is None:
            return False, "item_not_found"
        try:
            min_quantity, max_quantity = purchase_quantity_limits(goods)
        except ValueError:
            return False, "invalid_quantity"
        item_id = goods.id

        existing = (await s.execute(
            select(CartItems)
            .where(CartItems.user_id == user_id, CartItems.item_id == item_id)
            .with_for_update()
        )).scalars().first()

        if existing:
            resulting_quantity = existing.quantity + quantity
            if resulting_quantity < min_quantity:
                return False, "cart_qty_min"
            if resulting_quantity > max_quantity:
                return False, "cart_qty_max"
            existing.quantity = resulting_quantity
            existing.promo_code = None
            return True, "success"

        # Only a new line can fill the cart up
        count = (await s.execute(
            select(sa_func.count(CartItems.id)).where(CartItems.user_id == user_id)
        )).scalar() or 0
        if count >= CART_MAX_ITEMS:
            return False, "cart_full"

        # The storefront's Add to cart button means one initial line, which
        # must start at the configured minimum. Subsequent clicks above use
        # the ordinary +1 increment path for an existing line.
        if quantity == 1 and min_quantity > 1:
            quantity = min_quantity
        if quantity < min_quantity:
            return False, "cart_qty_min"
        if quantity > max_quantity:
            return False, "cart_qty_max"

        s.add(CartItems(user_id=user_id, item_id=item_id, promo_code=None, quantity=quantity))
        return True, "success"


async def add_to_cart(user_id: int, item_name: str, promo_code: str = None, quantity: int = 1) -> tuple[bool, str]:
    """Add `quantity` units of an item to the user's cart.

    One row per (user, item): adding something already in the cart increments the existing line rather than inserting a duplicate.

    Returns (success, message).
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= CART_MAX_QTY_PER_ITEM:
        return False, "invalid_quantity"

    try:
        return await _add_to_cart_once(user_id, item_name, promo_code, quantity)
    except IntegrityError:
        # Lost the uq_cart_item_per_user race against a concurrent add. Retry once: this attempt finds the row the winner inserted and increments it.
        try:
            return await _add_to_cart_once(user_id, item_name, promo_code, quantity)
        except IntegrityError:
            return False, "cart_conflict"


async def subscribe_to_stock(user_id: int, item_name: str) -> tuple[bool, str]:
    """Subscribe a user to the restock notification for an item.

    Idempotent: subscribing twice is a success, not an error.
    Returns (success, code).
    """
    item_name = normalize_item_name(item_name)
    try:
        async with Database().session() as s:
            goods = await find_goods_by_name(s, item_name)
            if goods is None:
                return False, "item_not_found"
            item_id = goods.id

            already = (await s.execute(
                select(exists().where(
                    StockSubscriptions.user_id == user_id,
                    StockSubscriptions.item_id == item_id,
                ))
            )).scalar()
            if already:
                return True, "already_subscribed"

            s.add(StockSubscriptions(user_id=user_id, item_id=item_id))
    except IntegrityError:
        # Lost the uq_stock_sub_per_user_item race — the subscription now exists, which is what the caller wanted anyway.
        return True, "already_subscribed"

    return True, "subscribed"


async def create_review(user_id: int, item_name: str, rating: int, text: str = None) -> int | None:
    """Create a review. Returns ID, or None if the item is unknown, already
    reviewed, or the rating is outside 1-5.
    """
    if not isinstance(rating, int) or isinstance(rating, bool) or not 1 <= rating <= 5:
        return None
    item_name = normalize_item_name(item_name)
    if not item_name:
        return None

    async with Database().session() as s:
        goods = await find_goods_by_name(s, item_name)
        if goods is None:
            return None
        item_id = goods.id
        existing = (await s.execute(
            select(exists().where(
                Reviews.user_id == user_id,
                Reviews.item_id == item_id,
            ))
        )).scalar()
        if existing:
            return None
        review = Reviews(user_id=user_id, item_id=item_id, rating=rating, text=text)
        s.add(review)
        await s.flush()
        return review.id

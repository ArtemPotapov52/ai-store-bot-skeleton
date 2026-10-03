import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select, delete as sa_delete, update as sa_update
from sqlalchemy.exc import IntegrityError, OperationalError, DBAPIError

from bot.database.models import User, ItemValues, Goods, Categories, BoughtGoods, Payments, Operations
from bot.database.models.main import PromoCodes, PromoCodeUsages, CartItems, ReferralEarnings, ApiIdempotency
from bot.database import Database
from bot.misc import EnvKeys
from bot.database.methods.read import (
    invalidate_user_cache, invalidate_stats_cache, invalidate_item_cache,
    invalidate_category_cache, promo_rule_error, find_goods_by_name,
    normalize_item_name,
)
from bot.database.methods.cache_utils import safe_create_task
from bot.database.methods.pricing import (
    effective_price, MAX_PURCHASE_QUANTITY, purchase_quantity_limits,
)
from bot.database.methods.audit import log_audit
from bot.misc.vpn_subscription_proxy import (
    create_vpn_subscription_link_in_session,
    vpn_proxy_is_configured,
)

_REDEEM_PROMO_ERRORS = {
    "not_found": "promo.not_found",
    "inactive": "promo.inactive",
    "wrong_type": "promo.not_balance_type",
    "expired": "promo.expired",
    "max_uses": "promo.max_uses_reached",
    "already_used": "promo.already_used",
}


class _Abort(Exception):
    """Abort the current transaction with a user-facing failure code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# PostgreSQL row locks are the authoritative guard in production.  The keyed
# process lock is an additional guard for repeated Telegram callbacks and for
# SQLite/degraded deployments where SELECT ... FOR UPDATE is a no-op.  A
# reference count lets idle entries disappear instead of growing forever.
_PURCHASE_LOCKS: dict[int, tuple[asyncio.Lock, int]] = {}
_PAYMENT_LOCKS: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}


@asynccontextmanager
async def _purchase_lock(user_id: int):
    entry = _PURCHASE_LOCKS.get(user_id)
    if entry is None:
        entry = (asyncio.Lock(), 0)
    lock, refs = entry
    _PURCHASE_LOCKS[user_id] = (lock, refs + 1)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
        _lock, remaining = _PURCHASE_LOCKS.get(user_id, (lock, 1))
        remaining -= 1
        if remaining <= 0:
            _PURCHASE_LOCKS.pop(user_id, None)
        else:
            _PURCHASE_LOCKS[user_id] = (_lock, remaining)


@asynccontextmanager
async def _payment_lock(provider: str, external_id: str):
    key = (provider, external_id)
    entry = _PAYMENT_LOCKS.get(key)
    if entry is None:
        entry = (asyncio.Lock(), 0)
    lock, refs = entry
    _PAYMENT_LOCKS[key] = (lock, refs + 1)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
        _lock, remaining = _PAYMENT_LOCKS.get(key, (lock, 1))
        remaining -= 1
        if remaining <= 0:
            _PAYMENT_LOCKS.pop(key, None)
        else:
            _PAYMENT_LOCKS[key] = (_lock, remaining)


def _split_amount(total: Decimal, n: int) -> list[Decimal]:
    """Split `total` into `n` amounts that sum back to it exactly.

    Each delivered unit gets its own BoughtGoods row. Dividing the line by n
    and rounding each share could drift, so remainder cents are handed out
    one per row: sum(result) == total, always.
    """
    if n <= 0:
        return []
    cents = int((total * 100).to_integral_value())
    base, extra = divmod(cents, n)
    return [
        Decimal(base + (1 if i < extra else 0)) / 100
        for i in range(n)
    ]


async def _buy_item_transaction_unlocked(
        telegram_id: int,
        item_name: str,
        promo_code: str = None,
        quantity: int = 1,
        expected_total: Decimal | None = None,
        api_idempotency_id: int | None = None,
) -> tuple[bool, str, dict | None]:
    """
    Complete transactional purchase of goods with checks and locks.
    Returns: (success, message, purchase_data)

    expected_total is the total shown to the buyer: when the catalog price
    changed, the purchase is refused instead of charging
    a figure the user never confirmed.
    """
    # Keep a conservative upper bound even when this function is called from
    # outside the Telegram handler.  The cart uses the same bound and a larger
    # request would otherwise be an easy way to create an unexpectedly large
    # single transaction.
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= MAX_PURCHASE_QUANTITY:
        return False, "invalid_quantity", None

    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with Database().session() as s:
                api_idempotency = None
                if api_idempotency_id is not None:
                    api_idempotency = (await s.execute(
                        select(ApiIdempotency).where(
                            ApiIdempotency.id == api_idempotency_id
                        ).with_for_update()
                    )).scalars().one_or_none()
                    if api_idempotency is None:
                        raise _Abort("idempotency_conflict")
                    if api_idempotency.status == "completed":
                        return True, "idempotent_replay", api_idempotency.result_json or {}
                    if api_idempotency.status != "processing":
                        raise _Abort("idempotency_conflict")

                # 1. Lock the user to check the balance
                user = (await s.execute(
                    select(User).where(User.telegram_id == telegram_id).with_for_update()
                )).scalars().one_or_none()

                if not user:
                    raise _Abort("user_not_found")

                # 2. Get information about the product
                goods = await find_goods_by_name(s, item_name, for_update=True)

                if not goods or not goods.is_active:
                    raise _Abort("item_not_found")
                try:
                    min_quantity, max_quantity = purchase_quantity_limits(goods)
                except ValueError:
                    raise _Abort("invalid_quantity")
                if not min_quantity <= quantity <= max_quantity:
                    raise _Abort("invalid_quantity")
                is_vpn_subscription = bool(goods.is_vpn_subscription)
                if is_vpn_subscription and not vpn_proxy_is_configured():
                    raise _Abort("vpn_unconfigured")

                # Always persist/display the canonical name, even when this
                # purchase is the first read of a legacy row with edge spaces.
                item_name = normalize_item_name(goods.name)

                # The catalog price is authoritative. Legacy sale and promo
                # records are retained for history, but never affect a charge.
                try:
                    price, _on_sale, _original_price = effective_price(goods)
                except ValueError:
                    raise _Abort("invalid_price")
                final_price = (price * quantity).quantize(Decimal("0.01"))

                if not final_price.is_finite() or final_price <= 0:
                    raise _Abort("invalid_price")

                # 2.6. Refuse to charge a total the buyer did not confirm.
                if expected_total is not None and final_price != expected_total:
                    raise _Abort("price_changed")

                # 3. Checking the balance for the complete quantity
                if user.balance < final_price:
                    raise _Abort("insufficient_funds")

                # 4. Receive and lock the requested quantity of goods.  Prefer
                # an infinite value (never consumed), otherwise claim exactly
                # ``quantity`` finite rows and fill any configured manual stock.
                infinite_value = (await s.execute(
                    select(ItemValues).where(
                        ItemValues.item_id == goods.id,
                        ItemValues.is_infinity.is_(True),
                    ).limit(1).with_for_update()
                )).scalars().first()

                values_to_delete = []
                manual_units = 0
                manual_delivery = (goods.delivery_text or "").strip()
                if is_vpn_subscription:
                    # A single infinite placeholder marks the VPN product as
                    # available; each buyer receives a generated URL instead.
                    if not infinite_value:
                        raise _Abort("out_of_stock")
                    delivered_values = [""] * quantity
                elif infinite_value:
                    # One infinite value can satisfy every requested unit and
                    # is deliberately retained in stock.
                    delivered_values = [infinite_value.value] * quantity
                else:
                    values_to_delete = (await s.execute(
                        select(ItemValues).where(ItemValues.item_id == goods.id)
                        .order_by(ItemValues.id)
                        .limit(quantity)
                        .with_for_update()
                    )).scalars().all()
                    manual_units = quantity - len(values_to_delete)
                    if manual_units and (
                            not manual_delivery or goods.stock_quantity < manual_units
                    ):
                        raise _Abort("out_of_stock")
                    delivered_values = [row.value for row in values_to_delete]
                    delivered_values.extend([manual_delivery] * manual_units)

                # 6. Write off the balance
                for value_row in values_to_delete:
                    await s.delete(value_row)
                if manual_units:
                    goods.stock_quantity -= manual_units

                if is_vpn_subscription:
                    delivered_values = []
                    for _ in range(quantity):
                        link = await create_vpn_subscription_link_in_session(s, telegram_id)
                        if link is None:
                            raise _Abort("vpn_unconfigured")
                        delivered_values.append(link)

                user.balance -= final_price

                # 7. Create a purchase record
                unit_prices = _split_amount(final_price, quantity)
                purchase_records = []
                for delivered_value, unit_price in zip(delivered_values, unit_prices):
                    bought_item = BoughtGoods(
                        item_name=item_name,
                        value=delivered_value,
                        price=unit_price,
                        buyer_id=telegram_id,
                        bought_datetime=datetime.now(timezone.utc),
                        unique_id=uuid4().int >> 65
                    )
                    purchase_records.append((bought_item, {
                        "item_name": item_name,
                        "value": delivered_value,
                        "price": float(unit_price),
                    }))
                s.add_all([row for row, _ in purchase_records])
                await s.flush()
                bought_items = [
                    {
                        **fields,
                        "bought_id": row.id,
                        "unique_id": row.unique_id,
                        "bought_datetime": row.bought_datetime.isoformat(),
                    }
                    for row, fields in purchase_records
                ]

                # Build the result before the session block commits on exit.
                first = bought_items[0]
                result_data = {
                    "item_name": item_name,
                    # Keep the old scalar fields for callers that purchase one
                    # unit, while exposing every delivered unit for the new
                    # quantity flow.
                    "value": first["value"],
                    "price": first["price"],
                    "total_price": float(final_price),
                    "quantity": quantity,
                    "items": bought_items,
                    "values": [row["value"] for row in bought_items],
                    "new_balance": float(user.balance),
                    "unique_id": first["unique_id"],
                    "bought_id": first["bought_id"],
                    "bought_datetime": first["bought_datetime"],
                }
                if api_idempotency is not None:
                    # The purchase rows and replay receipt commit together. Never
                    # persist delivery values in the generic idempotency table.
                    api_idempotency.status = "completed"
                    api_idempotency.result_json = {
                        "purchase_ids": [int(row["bought_id"]) for row in bought_items],
                        "total": format(final_price, ".2f"),
                        "balance": format(user.balance, ".2f"),
                        "quantity": quantity,
                    }

        except _Abort as e:
            return False, e.code, None

        except IntegrityError as e:
            if "unique_id" in str(e).lower() and attempt < max_retries - 1:
                continue  # Retry with a new unique_id
            await log_audit(
                "purchase_failed",
                level="WARNING",
                user_id=telegram_id,
                resource_type="Item",
                resource_id=item_name,
                details=str(e),
            )
            return False, "transaction_error", None

        except Exception as e:
            await log_audit(
                "purchase_failed",
                level="WARNING",
                user_id=telegram_id,
                resource_type="Item",
                resource_id=item_name,
                details=str(e),
            )
            return False, "transaction_error", None

        # Invalidate caches only after the commit succeeded.
        safe_create_task(invalidate_user_cache(telegram_id))
        safe_create_task(invalidate_stats_cache())
        safe_create_task(invalidate_item_cache(item_name))
        return True, "success", result_data

    return False, "transaction_error", None


async def buy_item_transaction(
        telegram_id: int,
        item_name: str,
        promo_code: str = None,
        quantity: int = 1,
        expected_total: Decimal | None = None,
        api_idempotency_id: int | None = None,
) -> tuple[bool, str, dict | None]:
    """Serialize purchases for one user before entering the DB transaction."""
    async with _purchase_lock(telegram_id):
        return await _buy_item_transaction_unlocked(
            telegram_id=telegram_id,
            item_name=item_name,
            promo_code=promo_code,
            quantity=quantity,
            expected_total=expected_total,
            api_idempotency_id=api_idempotency_id,
        )


async def _process_payment_with_referral_unlocked(
        user_id: int,
        amount: Decimal,
        provider: str,
        external_id: str,
        referral_percent: int = 0,
        currency: str | None = None,
) -> tuple[bool, str]:
    """
    Processing a payment with a referral bonus in one transaction.
    Returns (success, message)
    """

    try:
        try:
            amount = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise _Abort("payment_mismatch") from exc
        if not amount.is_finite() or amount <= 0:
            raise _Abort("payment_mismatch")
        provider = str(provider or "").strip().lower()
        external_id = str(external_id or "").strip()
        expected_currency = str(currency or EnvKeys.PAY_CURRENCY or "").strip().upper()
        if not provider or not external_id or not expected_currency:
            raise _Abort("payment_mismatch")

        async with Database().session() as s:
            # 1. Check the idempotency of the payment
            existing_payment = (await s.execute(
                select(Payments).where(
                    Payments.provider == provider,
                    Payments.external_id == external_id
                ).with_for_update()
            )).scalars().first()

            if existing_payment:
                existing_currency = str(existing_payment.currency or "").upper()
                try:
                    existing_amount = Decimal(str(existing_payment.amount))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise _Abort("payment_mismatch") from exc
                if (
                    existing_payment.user_id != user_id
                    or existing_currency != expected_currency
                    or not existing_amount.is_finite()
                    or existing_amount <= 0
                    or existing_amount != amount
                    or existing_payment.status not in {"pending", "succeeded"}
                ):
                    raise _Abort("payment_mismatch")
                if existing_payment.status == "succeeded":
                    raise _Abort("already_processed")
                existing_payment.status = "succeeded"
                amount = existing_amount
            else:
                payment = Payments(
                    provider=provider,
                    external_id=external_id,
                    user_id=user_id,
                    amount=amount,
                    currency=expected_currency,
                    status="succeeded"
                )
                s.add(payment)

            # 2. Update the user's balance
            user = (await s.execute(
                select(User).where(User.telegram_id == user_id).with_for_update()
            )).scalars().one()

            user.balance += amount

            # 3. Create a transaction record
            operation = Operations(
                user_id=user_id,
                operation_value=amount,
                operation_time=datetime.now(timezone.utc)
            )
            s.add(operation)

            # 4. Process the referral bonus
            clamped_percent = min(max(referral_percent, 0), 99)
            if clamped_percent > 0 and user.referral_id and user.referral_id != user_id:
                # Quantize to 2 dp to round on write
                referral_amount = (
                        (Decimal(clamped_percent) / Decimal(100)) * amount
                ).quantize(Decimal("0.01"))

                if referral_amount > 0:
                    referrer = (await s.execute(
                        select(User).where(User.telegram_id == user.referral_id).with_for_update()
                    )).scalars().one_or_none()

                    if referrer:
                        referrer.balance += referral_amount
                        await log_audit(
                            "referral_bonus",
                            user_id=user.referral_id,
                            resource_type="User",
                            resource_id=str(user_id),
                            details=f"paid={amount}, bonus={referral_amount}",
                            session=s,
                        )

                        earning = ReferralEarnings(
                            referrer_id=user.referral_id,
                            referral_id=user_id,
                            amount=referral_amount,
                            original_amount=amount
                        )
                        s.add(earning)

            referrer_id = user.referral_id if clamped_percent > 0 else None

    except _Abort as e:
        return False, e.code

    except IntegrityError:
        # Lost the unique(provider, external_id) race — already credited once.
        return False, "already_processed"

    except Exception as e:
        await log_audit(
            "payment_failed",
            level="WARNING",
            user_id=user_id,
            resource_type="Payment",
            details=f"provider={provider}, amount={amount}, error={e}",
        )
        return False, "payment_error"

    safe_create_task(invalidate_user_cache(user_id))
    safe_create_task(invalidate_stats_cache())
    if referrer_id:
        safe_create_task(invalidate_user_cache(referrer_id))

    return True, "success"


async def process_payment_with_referral(
        user_id: int,
        amount: Decimal,
        provider: str,
        external_id: str,
        referral_percent: int = 0,
        currency: str | None = None,
) -> tuple[bool, str]:
    """Process one provider invoice at most once per process and DB."""
    normalized_provider = str(provider or "").strip().lower()
    normalized_external_id = str(external_id or "").strip()
    async with _payment_lock(normalized_provider, normalized_external_id):
        return await _process_payment_with_referral_unlocked(
            user_id=user_id,
            amount=amount,
            provider=provider,
            external_id=external_id,
            referral_percent=referral_percent,
            currency=currency,
        )


async def checkout_cart_transaction(
        user_id: int, expected_total: Decimal | None = None,
        api_idempotency_id: int | None = None,
) -> tuple[bool, str, list | None]:
    """
    Atomic cart checkout — purchase all items from user's cart in one transaction.
    ``expected_total`` is the total shown to the user on the confirmation dialog.
    If the catalog price changed in between, the recomputed total won't match and the
    checkout aborts with ``price_changed`` instead of silently charging a
    different amount.

    Returns: (success, message, list[purchase_data])
    """
    async with _purchase_lock(user_id):
        return await _checkout_cart_transaction_unlocked(user_id, expected_total, api_idempotency_id)


async def _checkout_cart_transaction_unlocked(
        user_id: int, expected_total: Decimal | None = None,
        api_idempotency_id: int | None = None,
) -> tuple[bool, str, list | None]:
    max_retries = 3
    for attempt in range(max_retries):
        outcome: tuple[bool, str, list | None] | None = None
        try:
            async with Database().session() as s:
                api_idempotency = None
                if api_idempotency_id is not None:
                    api_idempotency = (await s.execute(
                        select(ApiIdempotency).where(
                            ApiIdempotency.id == api_idempotency_id
                        ).with_for_update()
                    )).scalars().one_or_none()
                    if api_idempotency is None:
                        raise _Abort("idempotency_conflict")
                    if api_idempotency.status == "completed":
                        return True, "idempotent_replay", api_idempotency.result_json or {}
                    if api_idempotency.status != "processing":
                        raise _Abort("idempotency_conflict")

                # 1. Lock user
                user = (await s.execute(
                    select(User).where(User.telegram_id == user_id).with_for_update()
                )).scalars().one_or_none()
                if not user:
                    raise _Abort("user_not_found")

                # 2. Get cart items
                cart_items = (await s.execute(
                    select(CartItems).where(CartItems.user_id == user_id)
                )).scalars().all()

                if not cart_items:
                    raise _Abort("cart_empty")

                # Lock all distinct goods up front in a deterministic order (by id)
                # so two concurrent checkouts with overlapping carts acquire the row
                # locks in the same order and cannot form an AB/BA deadlock cycle.
                item_ids = list({ci.item_id for ci in cart_items})
                goods_by_id = {
                    g.id: g for g in (await s.execute(
                        select(Goods).where(Goods.id.in_(item_ids))
                        .order_by(Goods.id).with_for_update()
                    )).scalars().all()
                }

                # 3. Resolve items and calculate the undiscounted total
                purchases = []
                items_to_remove = []

                for ci in cart_items:
                    goods = goods_by_id.get(ci.item_id)

                    if not goods or not goods.is_active:
                        items_to_remove.append(ci.id)
                        continue

                    qty = ci.quantity
                    if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= MAX_PURCHASE_QUANTITY:
                        raise _Abort("invalid_quantity")
                    try:
                        min_quantity, max_quantity = purchase_quantity_limits(goods)
                    except ValueError:
                        raise _Abort("invalid_quantity")
                    if not min_quantity <= qty <= max_quantity:
                        raise _Abort("invalid_quantity")
                    is_vpn_subscription = bool(goods.is_vpn_subscription)
                    if is_vpn_subscription and not vpn_proxy_is_configured():
                        raise _Abort("vpn_unconfigured")

                    # An infinite value satisfies any quantity from a single row and
                    # is never consumed, so check it first and short-circuit: never
                    # mix infinite and limited rows to fill one line.

                    # No FOR UPDATE needed — the goods row lock taken above already
                    # excludes concurrent stock mutation for this position, and
                    # ix_item_values_item_inf serves this predicate exactly.
                    inf_value = (await s.execute(
                        select(ItemValues)
                        .where(ItemValues.item_id == goods.id, ItemValues.is_infinity.is_(True))
                        .limit(1)
                    )).scalars().first()

                    if is_vpn_subscription:
                        if inf_value:
                            delivered = [""] * qty
                            values_to_delete = []
                            manual_units = 0
                        else:
                            items_to_remove.append(ci.id)
                            continue
                    elif inf_value:
                        delivered = [inf_value.value] * qty
                        values_to_delete = []
                        manual_units = 0
                    else:
                        # Claim qty rows. Safe under the goods lock: no other checkout
                        # can be selecting or deleting this position's values, so the
                        # FOR UPDATE ... LIMIT cannot be re-evaluated short by a peer.
                        rows = (await s.execute(
                            select(ItemValues)
                            .where(ItemValues.item_id == goods.id)
                            .order_by(ItemValues.id)
                            .limit(qty)
                            .with_for_update()
                        )).scalars().all()

                        manual_units = qty - len(rows)
                        manual_delivery = (goods.delivery_text or "").strip()

                        if manual_units:
                            if goods.stock_quantity < manual_units or not manual_delivery:
                                if not rows:
                                    # Nothing available: remove this stale cart
                                    # line but continue purchasing other lines.
                                    items_to_remove.append(ci.id)
                                    continue
                                # Partial stock. Also catches the admin delete
                                # path, which does not take the goods lock.
                                raise _Abort("out_of_stock")
                            delivered = [r.value for r in rows] + [manual_delivery] * manual_units
                        else:
                            delivered = [r.value for r in rows]
                        values_to_delete = rows

                    # Legacy sale and promo fields never affect purchase price.
                    try:
                        price, _on_sale, _original_price = effective_price(goods)
                        line_price = (price * qty).quantize(Decimal("0.01"))
                    except ValueError:
                        raise _Abort("invalid_price")

                    purchases.append({
                        'cart_id': ci.id,
                        'goods': goods,
                        'qty': qty,
                        'unit_price': price,
                        'line_price': line_price,
                        'delivered': delivered,
                        'values_to_delete': values_to_delete,
                        'manual_units': manual_units,
                        'is_vpn_subscription': is_vpn_subscription,
                    })

                for p in purchases:
                    # The line total is authoritative; per-unit prices are derived from it so the BoughtGoods rows sum back to what is charged.
                    p['unit_prices'] = _split_amount(p['line_price'], p['qty'])

                total_price = sum((p['line_price'] for p in purchases), Decimal(0))

                # Remove invalid cart items
                if items_to_remove:
                    await s.execute(
                        sa_delete(CartItems).where(CartItems.id.in_(items_to_remove))
                    )

                if not purchases:
                    # Commit the invalid-item cleanup above, but report failure.
                    outcome = (False, "cart_items_unavailable", None)
                elif (
                    not total_price.is_finite()
                    or total_price <= 0
                    or any(
                        not p['line_price'].is_finite() or p['line_price'] <= 0
                        for p in purchases
                    )
                ):
                    # Every item is charged at its positive catalog price.
                    outcome = (False, "invalid_price", None)
                else:
                    # Guard: the catalog price may have changed between confirmation and commit.
                    # Refuse to charge a total the user did not agree to.
                    if expected_total is not None and total_price != expected_total:
                        raise _Abort("price_changed")

                    # 4. Check balance
                    if user.balance < total_price:
                        raise _Abort("insufficient_funds")

                    # 5. Process each purchase — one BoughtGoods row per delivered
                    #    unit, each carrying its own value.
                    purchase_records = []
                    for p in purchases:
                        for v in p['values_to_delete']:
                            await s.delete(v)
                        if p['manual_units']:
                            p['goods'].stock_quantity -= p['manual_units']

                        delivered_values = p['delivered']
                        if p['is_vpn_subscription']:
                            delivered_values = []
                            for _ in range(p['qty']):
                                link = await create_vpn_subscription_link_in_session(s, user_id)
                                if link is None:
                                    raise _Abort("vpn_unconfigured")
                                delivered_values.append(link)

                        for value, unit_price in zip(delivered_values, p['unit_prices']):
                            bought_item = BoughtGoods(
                                item_name=p['goods'].name,
                                value=value,
                                price=unit_price,
                                buyer_id=user_id,
                                bought_datetime=datetime.now(timezone.utc),
                                unique_id=uuid4().int >> 65
                            )
                            purchase_records.append((bought_item, {
                                "item_name": p['goods'].name,
                                "value": value,
                                "price": float(unit_price),
                            }))

                    s.add_all([row for row, _ in purchase_records])
                    await s.flush()
                    results = [
                        {
                            **fields,
                            "bought_id": row.id,
                            "unique_id": row.unique_id,
                            "bought_datetime": row.bought_datetime.isoformat(),
                        }
                        for row, fields in purchase_records
                    ]

                    # 6. Deduct total
                    user.balance -= total_price

                    # 8. Clear cart
                    await s.execute(
                        sa_delete(CartItems).where(CartItems.user_id == user_id)
                    )

                    outcome = (True, "success", results)
                    if api_idempotency is not None:
                        # Delivery values live exclusively on buyer-scoped receipts.
                        api_idempotency.status = "completed"
                        api_idempotency.result_json = {
                            "purchase_ids": [int(row["bought_id"]) for row in results],
                            "total": format(total_price, ".2f"),
                            "balance": format(user.balance, ".2f"),
                            "quantity": len(results),
                        }

        except _Abort as e:
            return False, e.code, None

        except IntegrityError as e:
            if "unique_id" in str(e).lower() and attempt < max_retries - 1:
                continue  # Retry with new unique_ids
            await log_audit(
                "cart_checkout_failed",
                level="WARNING",
                user_id=user_id,
                details=str(e),
            )
            return False, "transaction_error", None

        except (OperationalError, DBAPIError) as e:
            msg = str(e).lower()
            # Postgres aborts one transaction in a deadlock/serialization cycle;
            # the victim is safe to retry (lock goods deterministically now).
            if (("deadlock" in msg or "could not serialize" in msg)
                    and attempt < max_retries - 1):
                continue
            await log_audit(
                "cart_checkout_failed",
                level="WARNING",
                user_id=user_id,
                details=str(e),
            )
            return False, "transaction_error", None

        except Exception as e:
            await log_audit(
                "cart_checkout_failed",
                level="WARNING",
                user_id=user_id,
                details=str(e),
            )
            return False, "transaction_error", None

        # Clean commit. Invalidate caches only on a successful checkout.
        if outcome[0]:
            safe_create_task(invalidate_user_cache(user_id))
            safe_create_task(invalidate_stats_cache())
            for name in {r["item_name"] for r in outcome[2]}:
                safe_create_task(invalidate_item_cache(name))
        return outcome

    return False, "transaction_error", None


async def replace_item_stock_and_meta(
        old_name: str,
        new_name: str,
        description: str,
        price,
        category_name: str,
        values: list[str],
        is_infinity: bool,
) -> tuple[bool, str | None, int]:
    """Swap a position's whole stock and its metadata in one transaction.

    Returns ``(success, error_code, values_added)``
    """
    from bot.database.methods.create import normalize_values
    try:
        normalized_price = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError):
        return False, "invalid_price", 0
    if not normalized_price.is_finite() or normalized_price <= 0:
        return False, "invalid_price", 0
    normalized, _skipped_dup, _skipped_invalid = normalize_values(values)

    try:
        async with Database().session() as s:
            goods = (await s.execute(
                select(Goods).where(Goods.name == old_name).with_for_update()
            )).scalars().one_or_none()
            if not goods:
                raise _Abort("position_invalid")

            category_id = (await s.execute(
                select(Categories.id).where(Categories.name == category_name)
            )).scalar()
            if not category_id:
                raise _Abort("position_invalid")

            if new_name != old_name:
                clash = (await s.execute(
                    select(Goods.id).where(Goods.name == new_name)
                )).scalar()
                if clash:
                    raise _Abort("position_exists")

            # Resolve the old category's name before mutating: if the position moves, that category's cached item list/count is now stale too.
            old_category_name = (await s.execute(
                select(Categories.name).where(Categories.id == goods.category_id)
            )).scalar()

            # 1. Purge the current stock.
            await s.execute(sa_delete(ItemValues).where(ItemValues.item_id == goods.id))

            # 2. Insert the replacement stock. An infinite position holds exactly one row that is never consumed, so only the first value counts.
            to_insert = normalized[:1] if is_infinity else normalized
            for v in to_insert:
                s.add(ItemValues(item_id=goods.id, value=v, is_infinity=is_infinity))

            # 3. Update the metadata.
            goods.name = new_name
            goods.description = description
            goods.price = normalized_price
            goods.category_id = category_id

            if new_name != old_name:
                # Purchase history denormalizes the name, so carry the rename over.
                await s.execute(
                    sa_update(BoughtGoods).where(BoughtGoods.item_name == old_name)
                    .values(item_name=new_name)
                )

            added = len(to_insert)

    except _Abort as e:
        return False, e.code, 0

    except Exception as e:
        await log_audit(
            "replace_item_stock_failed",
            level="WARNING",
            resource_type="Item",
            resource_id=old_name,
            details=str(e),
        )
        return False, "db_error", 0

    # Only after the commit: both names, and both categories when the position moved.
    for name in {old_name, new_name}:
        safe_create_task(invalidate_item_cache(name))
    for cat in {category_name, old_category_name} - {None}:
        safe_create_task(invalidate_category_cache(cat))
    safe_create_task(invalidate_stats_cache())

    return True, None, added


async def admin_balance_change(telegram_id: int, amount: Decimal) -> tuple[bool, str]:
    """
    Atomic admin balance change (top-up or deduction) with operation record.
    amount > 0 for top-up, amount < 0 for deduction.
    Returns (success, message).
    """
    try:
        async with Database().session() as s:
            user = (await s.execute(
                select(User).where(User.telegram_id == telegram_id).with_for_update()
            )).scalars().one_or_none()

            if not user:
                raise _Abort("user_not_found")

            if amount < 0 and user.balance < abs(amount):
                raise _Abort("insufficient_funds")

            user.balance += amount

            operation = Operations(
                user_id=telegram_id,
                operation_value=amount,
                operation_time=datetime.now(timezone.utc)
            )
            s.add(operation)

    except _Abort as e:
        return False, e.code

    except Exception as e:
        await log_audit(
            "admin_balance_change_failed",
            level="WARNING",
            user_id=telegram_id,
            resource_type="User",
            details=f"amount={amount}, error={e}",
        )
        return False, "balance_change_error"

    safe_create_task(invalidate_user_cache(telegram_id))
    safe_create_task(invalidate_stats_cache())

    return True, "success"


async def redeem_balance_promo(
        code: str,
        user_id: int,
        api_idempotency_id: int | None = None,
) -> tuple[bool, str, Decimal | None]:
    """
    Redeem a balance-type promo code: add discount_value to user balance.
    Returns (success, error_key_or_empty, amount_added).
    """
    try:
        async with Database().session() as s:
            api_idempotency = None
            if api_idempotency_id is not None:
                api_idempotency = (await s.execute(
                    select(ApiIdempotency).where(
                        ApiIdempotency.id == api_idempotency_id
                    ).with_for_update()
                )).scalars().one_or_none()
                if api_idempotency is None:
                    raise _Abort("idempotency_conflict")
                if api_idempotency.status == "completed":
                    saved = api_idempotency.result_json or {}
                    return True, "idempotent_replay", Decimal(str(saved.get("amount_added", 0)))
                if api_idempotency.status != "processing":
                    raise _Abort("idempotency_conflict")

            user = (await s.execute(
                select(User).where(User.telegram_id == user_id).with_for_update()
            )).scalars().one_or_none()
            if not user:
                raise _Abort("promo.not_found")

            promo = (await s.execute(
                select(PromoCodes).where(PromoCodes.code == code.upper()).with_for_update()
            )).scalars().first()

            err = await promo_rule_error(s, promo, user_id)
            if err:
                raise _Abort(_REDEEM_PROMO_ERRORS[err])

            amount = Decimal(str(promo.discount_value))
            user.balance += amount
            promo.current_uses += 1
            s.add(PromoCodeUsages(promo_id=promo.id, user_id=user_id))
            s.add(Operations(
                user_id=user_id,
                operation_value=amount,
                operation_time=datetime.now(timezone.utc),
            ))
            if api_idempotency is not None:
                api_idempotency.status = "completed"
                api_idempotency.result_json = {
                    "amount_added": format(amount, ".2f"),
                    "balance": format(user.balance, ".2f"),
                    "currency": str(EnvKeys.PAY_CURRENCY).upper(),
                }

    except _Abort as e:
        return False, e.code, None

    except Exception as e:
        await log_audit(
            "promo_redeem_failed",
            level="WARNING",
            user_id=user_id,
            resource_type="PromoCode",
            resource_id=code,
            details=str(e),
        )
        return False, "errors.something_wrong", None

    safe_create_task(invalidate_user_cache(user_id))
    safe_create_task(invalidate_stats_cache())
    return True, "", amount

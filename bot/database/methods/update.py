from decimal import Decimal, InvalidOperation

from sqlalchemy import exc, select, update

from bot.database.methods.read import (
    invalidate_user_cache,
    invalidate_item_cache,
    invalidate_category_cache,
    find_goods_by_name,
    normalize_item_name,
)
from bot.database.methods.cache_utils import safe_create_task
from bot.database.methods.create import CART_MAX_QTY_PER_ITEM
from bot.database.methods.pricing import purchase_quantity_limits
from bot.database.models import User, Goods, Categories, BoughtGoods, Role
from bot.database.models.main import PromoCodes, CartItems
from bot.database import Database
from bot.logger_mesh import logger


async def set_role(telegram_id: int, role: int) -> None:
    """Set user's role (by Telegram ID) and commit."""
    async with Database().session() as s:
        await s.execute(
            update(User).where(User.telegram_id == telegram_id).values(role_id=role)
        )

    safe_create_task(invalidate_user_cache(telegram_id))


async def set_user_locale(telegram_id: int, locale: str) -> bool:
    """Persist a supported per-user locale and invalidate the profile cache."""
    normalized = (locale or "").lower().strip()
    if normalized not in {"ru", "en", "vi"}:
        return False
    async with Database().session() as s:
        result = await s.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(locale=normalized)
        )
        changed = result.rowcount > 0
    if changed:
        safe_create_task(invalidate_user_cache(telegram_id))
    return changed


async def set_community_prompt_seen(telegram_id: int, seen: bool = True) -> bool:
    """Persist that an existing user has answered the optional chat invite."""
    async with Database().session() as s:
        result = await s.execute(
            update(User)
            .where(User.telegram_id == telegram_id)
            .values(community_prompt_seen=bool(seen))
        )
        changed = result.rowcount > 0
    if changed:
        safe_create_task(invalidate_user_cache(telegram_id))
    return changed


async def update_item(item_name: str, new_name: str, description: str, price, category: str) -> tuple[bool, str | None]:
    """Update a Goods record with proper locking.

    Returns ``(success, error_code)``. The error code is a stable key
    ("position_invalid", "position_exists", "db_error")
    """
    item_name = normalize_item_name(item_name)
    new_name = normalize_item_name(new_name)
    if not item_name or not new_name:
        return False, "position_invalid"

    # Names whose cache entries the commit invalidates. Collected inside the transaction, acted on only once it has succeeded.
    to_invalidate: list[str] = []
    old_category: str | None = None

    try:
        normalized_price = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError):
        return False, "invalid_price"
    if not normalized_price.is_finite() or normalized_price <= 0:
        return False, "invalid_price"

    try:
        async with Database().session() as s:
            goods = await find_goods_by_name(s, item_name, for_update=True)

            if not goods:
                return False, "position_invalid"
            stored_item_name = goods.name

            cat_id = (await s.execute(
                select(Categories.id).where(Categories.name == category)
            )).scalar()
            if not cat_id:
                return False, "position_invalid"

            if cat_id != goods.category_id:
                old_category = (await s.execute(
                    select(Categories.name).where(Categories.id == goods.category_id)
                )).scalar()

            if new_name == item_name:
                goods.description = description
                goods.price = normalized_price
                goods.category_id = cat_id
                to_invalidate = [item_name]
            else:
                existing = await find_goods_by_name(s, new_name)
                if existing:
                    return False, "position_exists"

                goods.name = new_name
                goods.description = description
                goods.price = normalized_price
                goods.category_id = cat_id

                await s.execute(
                    update(BoughtGoods).where(BoughtGoods.item_name == stored_item_name).values(item_name=new_name)
                )
                to_invalidate = [stored_item_name, new_name]

    except exc.SQLAlchemyError:
        logger.error("update_item(%r -> %r) failed", item_name, new_name, exc_info=True)
        return False, "db_error"

    for name in to_invalidate:
        safe_create_task(invalidate_item_cache(name, category))
    if old_category:
        safe_create_task(invalidate_category_cache(old_category))

    return True, None


async def set_user_blocked(telegram_id: int, blocked: bool) -> bool:
    """Set user blocked status and commit."""
    async with Database().session() as s:
        result = await s.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalars().first()
        if not user:
            return False
        user.is_blocked = blocked

    safe_create_task(invalidate_user_cache(telegram_id))
    return True


async def set_cart_item_quantity(cart_item_id: int, user_id: int, delta: int) -> tuple[bool, str, int]:
    """Apply `delta` to a cart line's quantity.

    Dropping to zero or below removes the line — that is what "−" on a line of
    one means. Scoped by user_id so one user cannot touch another's cart.

    Returns (success, code, new_quantity); new_quantity is 0 when the line was
    removed.
    """
    async with Database().session() as s:
        row = (await s.execute(
            select(CartItems)
            .where(CartItems.id == cart_item_id, CartItems.user_id == user_id)
            .with_for_update()
        )).scalars().first()

        if not row:
            return False, "item_not_found", 0

        goods = (await s.execute(
            select(Goods).where(Goods.id == row.item_id).with_for_update()
        )).scalars().first()
        if goods is None:
            return False, "item_not_found", 0
        try:
            min_quantity, max_quantity = purchase_quantity_limits(goods)
        except ValueError:
            return False, "invalid_quantity", row.quantity

        new_qty = row.quantity + delta

        if new_qty <= 0 and min_quantity == 1:
            await s.delete(row)
            return True, "removed", 0

        if new_qty < min_quantity:
            return False, "cart_qty_min", row.quantity

        if new_qty > min(max_quantity, CART_MAX_QTY_PER_ITEM):
            return False, "cart_qty_max", row.quantity

        row.quantity = new_qty
        return True, "success", new_qty


async def clear_cart_item_promo(cart_item_id: int, user_id: int) -> bool:
    """Drop the promo code from one cart line. Scoped by user_id so one user
    cannot touch another's cart. Returns True if a line was updated."""
    async with Database().session() as s:
        result = await s.execute(
            update(CartItems)
            .where(CartItems.id == cart_item_id, CartItems.user_id == user_id)
            .values(promo_code=None)
        )
        return result.rowcount > 0


async def is_user_blocked(telegram_id: int) -> bool:
    """Check if user is blocked."""
    async with Database().session() as s:
        result = await s.execute(
            select(User.is_blocked).where(User.telegram_id == telegram_id)
        )
        return bool(result.scalar())


async def update_category(category_name: str, new_name: str) -> None:
    """Rename a category. With integer PKs, just update the name field."""
    async with Database().session() as s:
        result = await s.execute(
            select(Categories).where(Categories.name == category_name).with_for_update()
        )
        category = result.scalars().one_or_none()

        if not category:
            raise ValueError("Category not found")

        category.name = new_name

    safe_create_task(invalidate_category_cache(category_name))
    if new_name != category_name:
        safe_create_task(invalidate_category_cache(new_name))


async def update_role(role_id: int, name: str, permissions: int) -> tuple[bool, str | None]:
    """Update role name and permissions. Returns (success, error_message)."""
    async with Database().session() as s:
        result = await s.execute(
            select(Role).where(Role.id == role_id).with_for_update()
        )
        role = result.scalars().first()
        if not role:
            return False, "Role not found"
        if role.name != name:
            existing = (await s.execute(select(Role).where(Role.name == name))).scalars().first()
            if existing:
                return False, "Role name already exists"
        role.name = name
        role.permissions = permissions
        return True, None


async def toggle_promo_code(promo_id: int) -> bool | None:
    """Toggle promo code active status. Returns new is_active or None if not found."""
    async with Database().session() as s:
        result = await s.execute(
            select(PromoCodes).where(PromoCodes.id == promo_id).with_for_update()
        )
        promo = result.scalars().first()
        if not promo:
            return None
        if promo.discount_type != "balance":
            promo.is_active = False
            return False
        promo.is_active = not promo.is_active
        return promo.is_active

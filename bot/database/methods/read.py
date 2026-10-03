import datetime
from decimal import Decimal
from functools import wraps
from typing import Optional, Dict, TypeVar, Callable, Any, Coroutine

from sqlalchemy import case, func, exists, select, inspect as sa_inspect

from bot.database.models import Database, User, ItemValues, Goods, Categories, Role, BoughtGoods, \
    Operations, ReferralEarnings, Permission
from bot.database.models.main import PromoCodes, PromoCodeUsages, CartItems, Reviews, StockSubscriptions
from bot.misc.caching import get_cache_manager, single_flight
from bot.misc.timezone import moscow_day_window, moscow_today

F = TypeVar('F', bound=Callable[..., Coroutine[Any, Any, Any]])


def async_cached(ttl: int = 300, key_prefix: str = "", cache_empty: bool = True) -> Callable[[F], F]:
    """Decorator for async functions with caching.

    Misses are single-flighted (see single_flight): concurrent callers of the
    same key wait for one computation instead of all hitting the DB when a hot
    key expires.
    """

    def decorator(async_func: F) -> F:
        @wraps(async_func)
        async def async_wrapper(*args, **kwargs):
            key_parts = [str(arg) for arg in args]
            # Keyword arguments belong in the key too:
            # without them f(1) and f(x=1) would share the entry f: and serve each other's results.
            key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
            cache_key = f"{key_prefix or async_func.__name__}:{':'.join(key_parts)}"

            cache = get_cache_manager()
            if not cache:
                return await async_func(*args, **kwargs)

            return await single_flight(
                cache, cache_key, lambda: async_func(*args, **kwargs), ttl,
                should_cache=(
                    None if cache_empty
                    else (lambda result: result is not None and bool(result))
                ),
            )

        return async_wrapper

    return decorator


def _day_window(date_str: str) -> tuple[datetime.datetime, datetime.datetime]:
    """Return UTC database bounds for one Moscow calendar day."""
    return moscow_day_window(date_str)


def _obj_to_dict(obj, model=None) -> dict:
    """Convert an ORM object to a dict of its column values."""
    mapper = sa_inspect(model if model is not None else type(obj))
    return {attr.key: getattr(obj, attr.key) for attr in mapper.column_attrs}


def normalize_item_name(item_name: str | None) -> str:
    """Return the canonical form used for product names.

    Product names are identifiers throughout the shop flow. Older databases
    may still contain a name with an accidental leading/trailing space, so
    callers should compare the canonical form while the fallback below keeps
    those legacy rows purchasable until they are cleaned up.
    """
    return str(item_name or "").strip()


async def find_goods_by_name(session, item_name: str | None, *, for_update: bool = False):
    """Find a product by its canonical name, tolerating legacy edge spaces.

    The exact canonical value is always preferred. Only when it is absent do
    we fall back to ``TRIM(goods.name)`` so a pre-existing malformed row does
    not surface as ``item_not_found`` during checkout.
    """
    normalized = normalize_item_name(item_name)
    if not normalized:
        return None

    statement = select(Goods).where(Goods.name == normalized)
    if for_update:
        statement = statement.with_for_update()
    goods = (await session.execute(statement)).scalars().first()
    if goods is not None:
        return goods

    statement = select(Goods).where(func.trim(Goods.name) == normalized)
    if for_update:
        statement = statement.with_for_update()
    return (await session.execute(statement)).scalars().first()


async def _fetch_one_dict(model, *whereclauses) -> dict | None:
    """Fetch a single 'model' row matching the where-clauses as a dict, or None."""
    async with Database().session() as s:
        obj = (await s.execute(select(model).where(*whereclauses))).scalars().first()
        return _obj_to_dict(obj, model) if obj else None


# --- Async implementations ---

async def check_user(telegram_id: int | str) -> Optional[dict]:
    """Return user by Telegram ID or None if not found."""
    return await _fetch_one_dict(User, User.telegram_id == telegram_id)


async def check_role(telegram_id: int) -> int:
    """Return permission bitmask for user (0 if none)."""
    async with Database().session() as s:
        result = await s.execute(
            select(Role.permissions).join(User, User.role_id == Role.id).where(User.telegram_id == telegram_id)
        )
        return result.scalar() or 0


async def get_role_id_by_name(role_name: str) -> Optional[int]:
    """Return role id by name or None."""
    async with Database().session() as s:
        return (await s.execute(select(Role.id).where(Role.name == role_name))).scalar()


async def _ensure_roles_seeded() -> None:
    """Re-create the built-in roles when the table is empty.

    The web panel allows deleting roles (bypassing the bot's own guard),
    which previously left registration with no valid role id: new users were
    neither created nor shown in admin, and profile/buy answered with
    ``errors.something_wrong``. Seeding here is idempotent, so concurrent
    callers are safe.
    """
    async with Database().session() as s:
        has_any = (await s.execute(select(Role.id).limit(1))).scalar()
        if has_any is not None:
            return
    await Role.insert_roles()


async def get_default_user_role_id() -> Optional[int]:
    """Return the current default role id used for newly registered users.

    Role ids are database-generated and can change when an administrator
    removes and recreates the built-in roles.  New accounts must therefore be
    linked by the stable ``default`` flag (or the ``USER`` name for databases
    created before that flag was populated), rather than assuming ``id=1``.
    An empty roles table is re-seeded on demand so registration never
    silently skips creating the user.
    """
    async with Database().session() as s:
        role_id = (await s.execute(
            select(Role.id)
            .where(Role.default.is_(True))
            .order_by(Role.id.asc())
            .limit(1)
        )).scalar()
        if role_id is not None:
            return role_id

        role_id = (await s.execute(
            select(Role.id)
            .where(Role.name == "USER")
            .order_by(Role.id.asc())
            .limit(1)
        )).scalar()
        if role_id is not None:
            return role_id

    await _ensure_roles_seeded()

    async with Database().session() as s:
        role_id = (await s.execute(
            select(Role.id)
            .where(Role.default.is_(True))
            .order_by(Role.id.asc())
            .limit(1)
        )).scalar()
        if role_id is not None:
            return role_id
        return (await s.execute(
            select(Role.id)
            .where(Role.name == "USER")
            .order_by(Role.id.asc())
            .limit(1)
        )).scalar()


async def check_role_name_by_id(role_id: int) -> str:
    """Return role name by id (raises if not found)."""
    async with Database().session() as s:
        result = await s.execute(select(Role.name).where(Role.id == role_id))
        return result.scalar_one()


async def select_max_role_id() -> Optional[int]:
    """Return role_id with the highest numeric permissions value (OWNER=127)."""
    async with Database().session() as s:
        result = await s.execute(select(Role.id).order_by(Role.permissions.desc()).limit(1))
        row = result.first()
        if row:
            return row[0]
    await _ensure_roles_seeded()
    async with Database().session() as s:
        result = await s.execute(select(Role.id).order_by(Role.permissions.desc()).limit(1))
        row = result.first()
        return row[0] if row else None


async def get_all_roles() -> list[dict]:
    """Return all roles as list of dicts ordered by permissions asc."""
    async with Database().session() as s:
        result = await s.execute(select(Role).order_by(Role.permissions.asc()))
        roles = result.scalars().all()
        return [{'id': r.id, 'name': r.name, 'permissions': r.permissions, 'default': r.default} for r in roles]


async def get_role_by_id(role_id: int) -> dict | None:
    """Return single role as dict or None."""
    async with Database().session() as s:
        result = await s.execute(select(Role).where(Role.id == role_id))
        r = result.scalars().first()
        return {'id': r.id, 'name': r.name, 'permissions': r.permissions, 'default': r.default} if r else None


async def get_roles_with_max_perms(max_perms: int) -> list[dict]:
    """Return roles whose permissions are a subset of max_perms (bitwise)."""
    async with Database().session() as s:
        result = await s.execute(select(Role).order_by(Role.permissions.asc()))
        roles = result.scalars().all()
        return [
            {'id': r.id, 'name': r.name, 'permissions': r.permissions, 'default': r.default}
            for r in roles
            if (r.permissions & ~max_perms) == 0
        ]


async def count_users_with_role(role_id: int) -> int:
    """Return count of users assigned to a given role."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.count(User.telegram_id)).where(User.role_id == role_id)
        )).scalar() or 0


async def get_roles_with_user_counts() -> list[dict]:
    """Return all non-default roles that have at least 1 user, with user count."""
    async with Database().session() as s:
        result = await s.execute(
            select(Role.name, Role.permissions, func.count(User.telegram_id))
            .join(User, User.role_id == Role.id)
            .where(Role.default == False)  # noqa: E712
            .group_by(Role.id, Role.name, Role.permissions)
            .having(func.count(User.telegram_id) > 0)
            .order_by(Role.permissions.asc())
        )
        return [
            {'name': name, 'permissions': perms, 'user_count': count}
            for name, perms, count in result.all()
        ]


async def select_today_users(date: str) -> int:
    """Return count of users registered on given date (YYYY-MM-DD)."""
    start_of_day, end_of_day = _day_window(date)
    async with Database().session() as s:
        return (await s.execute(
            select(func.count()).select_from(User).where(
                User.registration_date >= start_of_day,
                User.registration_date < end_of_day
            )
        )).scalar() or 0


async def get_user_count() -> int:
    """Return total users count."""
    async with Database().session() as s:
        return (await s.execute(select(func.count()).select_from(User))).scalar() or 0


async def select_admins() -> int:
    """Return count of users whose role has any admin permission (beyond USE)."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.count(User.telegram_id))
            .join(Role, User.role_id == Role.id)
            .where(Role.permissions.op('&')(~Permission.USE) != 0)
        )).scalar() or 0


async def get_all_users() -> list[tuple[int]]:
    """Return list of all user telegram_ids (as tuples)."""
    async with Database().session() as s:
        result = await s.execute(select(User.telegram_id))
        return result.all()


async def get_bought_item_info(item_id: int, buyer_id: int | None = None) -> dict | None:
    """Return bought item row as dict by row id, or None.

    When ``buyer_id`` is given the row must also belong to that buyer, so a user
    can only read their own delivered goods. Admin views pass ``buyer_id=None``
    after an explicit permission check.
    """
    clauses = [BoughtGoods.id == item_id]
    if buyer_id is not None:
        clauses.append(BoughtGoods.buyer_id == buyer_id)
    return await _fetch_one_dict(BoughtGoods, *clauses)


async def get_item_info(item_name: str) -> dict | None:
    """Return item (position) row as dict by name, or None."""
    async with Database().session() as s:
        goods = await find_goods_by_name(s, item_name)
        return _obj_to_dict(goods, Goods) if goods else None


async def get_items_info(item_names: list[str]) -> dict[str, dict]:
    """Return {name: item_row_dict} for the given names in a single query."""
    names = [n for n in dict.fromkeys(item_names)]  # dedupe, preserve order
    if not names:
        return {}
    async with Database().session() as s:
        result = await s.execute(select(Goods).where(Goods.name.in_(names)))
        return {g.name: _obj_to_dict(g, Goods) for g in result.scalars().all()}


async def get_catalog_items_summary(item_names: list[str]) -> dict[str, dict]:
    """Return price and stock for catalog buttons in one database query."""
    names = list(dict.fromkeys(item_names))
    if not names:
        return {}
    limited_count = func.sum(case((ItemValues.is_infinity.is_(False), 1), else_=0))
    has_infinite = func.max(case((ItemValues.is_infinity.is_(True), 1), else_=0))
    async with Database().session() as s:
        rows = (await s.execute(
            select(
                Goods.name,
                Goods.price,
                Goods.availability_note,
                Goods.image_ref,
                Goods.is_variable_pricing,
                Goods.min_quantity,
                (func.coalesce(limited_count, 0) + Goods.stock_quantity).label("quantity"),
                has_infinite.label("is_infinite"),
            )
            .outerjoin(ItemValues, ItemValues.item_id == Goods.id)
            .where(Goods.name.in_(names), Goods.is_active.is_(True))
            .group_by(Goods.id)
        )).all()
    return {
        row.name: {
            "name": row.name,
            "price": row.price,
            "availability_note": row.availability_note,
            "image_ref": row.image_ref,
            "min_quantity": int(row.min_quantity or 1) if row.is_variable_pricing else 1,
            "quantity": int(row.quantity or 0),
            "is_infinite": bool(row.is_infinite),
        }
        for row in rows
    }


async def get_goods_info(item_id: int) -> dict | None:
    """Return item_value row as dict by id, including item_name from Goods."""
    async with Database().session() as s:
        result = await s.execute(
            select(ItemValues, Goods.name.label('item_name'))
            .join(Goods, Goods.id == ItemValues.item_id)
            .where(ItemValues.id == int(item_id))
        )
        row = result.first()
        if not row:
            return None
        d = _obj_to_dict(row.ItemValues, ItemValues)
        d['item_name'] = row.item_name
        return d


async def check_category(category_name: str) -> dict | None:
    """Return category as dict by name, or None."""
    return await _fetch_one_dict(Categories, Categories.name == category_name)


async def get_category_name_by_id(category_id: int) -> str | None:
    """Return a category's name by its id, or None."""
    async with Database().session() as s:
        return (await s.execute(
            select(Categories.name).where(Categories.id == category_id)
        )).scalar()


async def get_item_name_by_id(item_id: int) -> str | None:
    """Return a product's name by its id, or None.

    ItemValues.item is lazy='raise', so callers holding only a stock row cannot
    walk the relationship to get there.
    """
    async with Database().session() as s:
        return (await s.execute(
            select(Goods.name).where(Goods.id == item_id)
        )).scalar()


async def select_item_values_amount(item_name: str) -> int:
    """Return the stock display count for an item.

    The result combines individual account/key rows with the simple counted
    stock entered on a product card. An infinite delivery row keeps the
    long-standing display value of ``1``; callers then render it as
    "unlimited" instead of a numeric remainder.
    """
    async with Database().session() as s:
        goods = await find_goods_by_name(s, item_name)
        if goods is None:
            return 0
        has_infinite = func.max(
            case((ItemValues.is_infinity.is_(True), 1), else_=0)
        )
        limited_count = func.sum(
            case((ItemValues.is_infinity.is_(False), 1), else_=0)
        )
        return (await s.execute(
            select(
                case(
                    (func.coalesce(has_infinite, 0) > 0, 1),
                    else_=func.coalesce(limited_count, 0) + Goods.stock_quantity,
                )
            )
            .outerjoin(ItemValues, ItemValues.item_id == Goods.id)
            .where(Goods.id == goods.id)
            .group_by(Goods.id)
        )).scalar() or 0


async def check_value(item_name: str) -> bool:
    """Return True if item has any infinite value (is_infinity=True)."""
    async with Database().session() as s:
        goods = await find_goods_by_name(s, item_name)
        if goods is None:
            return False
        return bool((await s.execute(
            select(exists().where(
                ItemValues.item_id == goods.id,
                ItemValues.is_infinity.is_(True),
            ))
        )).scalar())


async def select_user_items(buyer_id: int | str) -> int:
    """Return count of bought items for user."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.count()).select_from(BoughtGoods).where(BoughtGoods.buyer_id == buyer_id)
        )).scalar() or 0


async def select_bought_item(unique_id: int) -> dict | None:
    """Return one bought item by unique_id as dict, or None."""
    return await _fetch_one_dict(BoughtGoods, BoughtGoods.unique_id == unique_id)


async def select_count_items() -> int:
    """Return total count of item_values."""
    async with Database().session() as s:
        return (await s.execute(select(func.count()).select_from(ItemValues))).scalar() or 0


async def select_count_goods() -> int:
    """Return total count of goods (positions)."""
    async with Database().session() as s:
        return (await s.execute(select(func.count()).select_from(Goods))).scalar() or 0


async def select_count_categories() -> int:
    """Return total count of categories."""
    async with Database().session() as s:
        return (await s.execute(select(func.count()).select_from(Categories))).scalar() or 0


async def select_count_bought_items() -> int:
    """Return total count of bought items."""
    async with Database().session() as s:
        return (await s.execute(select(func.count()).select_from(BoughtGoods))).scalar() or 0


async def select_unique_buyers() -> int:
    """Return count of unique users who made at least one purchase."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.count(func.distinct(BoughtGoods.buyer_id)))
        )).scalar() or 0


async def select_avg_order() -> Decimal:
    """Return average purchase price."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.avg(BoughtGoods.price))
        )).scalar() or Decimal(0)


async def select_today_orders_count(date: str) -> int:
    """Return number of purchases for given date."""
    start_of_day, end_of_day = _day_window(date)
    async with Database().session() as s:
        return (await s.execute(
            select(func.count()).select_from(BoughtGoods).where(
                BoughtGoods.bought_datetime >= start_of_day,
                BoughtGoods.bought_datetime < end_of_day
            )
        )).scalar() or 0


async def select_blocked_users_count() -> int:
    """Return count of blocked users."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.count()).select_from(User).where(User.is_blocked == True)  # noqa: E712
        )).scalar() or 0


async def get_blocked_user_ids() -> list[int]:
    """Return list of telegram_ids of all blocked users."""
    async with Database().session() as s:
        result = await s.execute(
            select(User.telegram_id).where(User.is_blocked == True)  # noqa: E712
        )
        return [row[0] for row in result.all()]


async def select_today_orders(date: str) -> Decimal:
    """Return total revenue for given date (YYYY-MM-DD)."""
    start_of_day, end_of_day = _day_window(date)
    async with Database().session() as s:
        res = (await s.execute(
            select(func.sum(BoughtGoods.price)).where(
                BoughtGoods.bought_datetime >= start_of_day,
                BoughtGoods.bought_datetime < end_of_day
            )
        )).scalar()
        return res or Decimal(0)


async def select_all_orders() -> Decimal:
    """Return total revenue for all time (sum of BoughtGoods.price)."""
    async with Database().session() as s:
        return (await s.execute(select(func.sum(BoughtGoods.price)))).scalar() or Decimal(0)


async def select_today_operations(date: str) -> Decimal:
    """Return total operations value for given date (YYYY-MM-DD)."""
    start_of_day, end_of_day = _day_window(date)
    async with Database().session() as s:
        res = (await s.execute(
            select(func.sum(Operations.operation_value)).where(
                Operations.operation_time >= start_of_day,
                Operations.operation_time < end_of_day
            )
        )).scalar()
        return res or Decimal(0)


async def select_all_operations() -> Decimal:
    """Return total operations value for all time."""
    async with Database().session() as s:
        return (await s.execute(select(func.sum(Operations.operation_value)))).scalar() or Decimal(0)


async def select_users_balance() -> Decimal:
    """Return sum of all users' balances (0 when there are none)."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.coalesce(func.sum(User.balance), 0))
        )).scalar() or Decimal(0)


async def select_user_operations_total(user_id: int | str) -> Decimal:
    """Total of a user's operations, summed server-side (avoids pulling every row)."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.coalesce(func.sum(Operations.operation_value), 0))
            .where(Operations.user_id == user_id)
        )).scalar() or Decimal(0)


async def select_user_spent_total(user_id: int | str) -> Decimal:
    """Return the exact historical amount spent on delivered orders."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.coalesce(func.sum(BoughtGoods.price), 0))
            .where(BoughtGoods.buyer_id == user_id)
        )).scalar() or Decimal(0)


async def check_user_referrals(user_id: int) -> int:
    """Return count of referrals of the user."""
    async with Database().session() as s:
        return (await s.execute(
            select(func.count()).select_from(User).where(User.referral_id == user_id)
        )).scalar() or 0


async def get_user_referral(user_id: int) -> Optional[int]:
    """Return referral_id of the user or None."""
    async with Database().session() as s:
        result = await s.execute(select(User.referral_id).where(User.telegram_id == user_id))
        row = result.first()
        return row[0] if row else None


async def get_user_profile_aggregates(user_id: int, role_id: int | None) -> dict:
    """Everything the admin user-profile screen needs, in one query."""
    async with Database().session() as s:
        row = (await s.execute(
            select(
                select(func.coalesce(func.sum(Operations.operation_value), 0))
                .where(Operations.user_id == user_id)
                .scalar_subquery().label("operations_total"),

                select(func.count()).select_from(BoughtGoods)
                .where(BoughtGoods.buyer_id == user_id)
                .scalar_subquery().label("items_count"),

                select(func.count()).select_from(User)
                .where(User.referral_id == user_id)
                .scalar_subquery().label("referrals"),

                select(func.count(ReferralEarnings.id))
                .where(ReferralEarnings.referrer_id == user_id)
                .scalar_subquery().label("earnings_count"),

                select(func.coalesce(func.sum(ReferralEarnings.amount), 0))
                .where(ReferralEarnings.referrer_id == user_id)
                .scalar_subquery().label("earnings_amount"),

                select(func.coalesce(func.sum(ReferralEarnings.original_amount), 0))
                .where(ReferralEarnings.referrer_id == user_id)
                .scalar_subquery().label("earnings_original"),

                select(func.count(func.distinct(ReferralEarnings.referral_id)))
                .where(ReferralEarnings.referrer_id == user_id)
                .scalar_subquery().label("active_referrals"),

                select(User.is_blocked).where(User.telegram_id == user_id)
                .scalar_subquery().label("is_blocked"),

                select(Role.name).where(Role.id == role_id)
                .scalar_subquery().label("role_name"),
            )
        )).one()

    return {
        "operations_total": row.operations_total or Decimal(0),
        "items_count": row.items_count or 0,
        "referrals": row.referrals or 0,
        "role_name": row.role_name,
        "blocked": bool(row.is_blocked),
        "earnings": {
            "total_earnings_count": row.earnings_count or 0,
            "total_amount": row.earnings_amount or Decimal(0),
            "total_original_amount": row.earnings_original or Decimal(0),
            "active_referrals_count": row.active_referrals or 0,
        },
    }


async def get_referral_earnings_stats(referrer_id: int) -> Dict:
    """Get statistics on user referral charges."""
    async with Database().session() as s:
        result = await s.execute(
            select(
                func.count(ReferralEarnings.id).label('total_earnings_count'),
                func.sum(ReferralEarnings.amount).label('total_amount'),
                func.sum(ReferralEarnings.original_amount).label('total_original_amount'),
                func.count(func.distinct(ReferralEarnings.referral_id)).label('active_referrals_count')
            ).where(ReferralEarnings.referrer_id == referrer_id)
        )
        stats = result.first()

        return {
            'total_earnings_count': stats.total_earnings_count or 0,
            'total_amount': stats.total_amount or Decimal(0),
            'total_original_amount': stats.total_original_amount or Decimal(0),
            'active_referrals_count': stats.active_referrals_count or 0
        }


async def get_one_referral_earning(earning_id: int, referrer_id: int | None = None) -> dict | None:
    """Get one referral earning as a dict, or None.

    When ``referrer_id`` is given the row must belong to that referrer, so a user
    can only read their own earnings. Admin views pass ``referrer_id=None`` after
    an explicit permission check.
    """
    clauses = [ReferralEarnings.id == earning_id]
    if referrer_id is not None:
        clauses.append(ReferralEarnings.referrer_id == referrer_id)
    return await _fetch_one_dict(ReferralEarnings, *clauses)


# --- Cached versions ---

@async_cached(ttl=600, key_prefix="user")
async def check_user_cached(telegram_id: int | str):
    """Cached version of check_user"""
    return await check_user(telegram_id)


async def check_role_cached(telegram_id: int) -> int:
    """Cached permission bitmask for a user.

    Delegates to the shared role cache in the auth middleware (in-process tier
    first, then Redis, then the DB). role 0 (unknown user) is intentionally not cached.
    """
    from bot.middleware.security import get_role_cached
    return await get_role_cached(telegram_id)


@async_cached(ttl=1800, key_prefix="category")
async def check_category_cached(category_name: str):
    """Cached Category Check"""
    return await check_category(category_name)


@async_cached(ttl=900, key_prefix="item_info")
async def get_item_info_cached(item_name: str):
    """Cached product information"""
    return await get_item_info(item_name)


@async_cached(ttl=300, key_prefix="item_values")
async def select_item_values_amount_cached(item_name: str):
    """Cached quantity of goods"""
    return await select_item_values_amount(item_name)


@async_cached(ttl=300, key_prefix="item_infinite")
async def check_value_cached(item_name: str):
    """Cached check_value (whether the item has an infinite value)."""
    return bool(await check_value(item_name))


@async_cached(ttl=60, key_prefix="user_count")
async def get_user_count_cached():
    """Cached number of users"""
    return await get_user_count()


@async_cached(ttl=60, key_prefix="admin_count")
async def select_admins_cached():
    """Cached number of admins"""
    return await select_admins()


# Cache invalidation functions
async def invalidate_user_cache(user_id: int):
    """Invalidate a user's cached row, role and paginator counts.

    Batched into one round-trip: this runs on every purchase, payment, checkout
    and balance change. Only keys the app actually writes are listed.
    New per-user caches must be added below.
    """
    cache = get_cache_manager()
    if cache:
        await cache.delete_many((
            f"user:{user_id}",
            f"auth:role:{user_id}",
            f"count:bought:{user_id}",
            f"count:ops:{user_id}",
            # A newly registered account must be visible immediately in the
            # paginated admin users list, whose total is cached separately.
            "count:users",
        ))

    # The auth middleware keeps its own in-process role cache; drop that entry
    # too so a role change takes effect before its TTL.
    from bot.middleware.security import get_auth_middleware
    mw = get_auth_middleware()
    if mw is not None:
        mw.admin_cache.pop(user_id, None)


async def invalidate_item_cache(item_name: str, category_name: str = None):
    """Invalidate every cache entry keyed by a product's name.
    """
    cache = get_cache_manager()
    if cache:
        keys = [
            f"item_info:{item_name}",
            f"item_values:{item_name}",
            f"item_infinite:{item_name}",
            f"avg_rating:{item_name}",
            f"count:reviews:{item_name}",
            f"count:stock:{item_name}",
        ]
        if category_name:
            keys.append(f"category:{category_name}")
            keys.append(f"category_items:{category_name}:count")
        await cache.delete_many(keys)


async def invalidate_category_cache(category_name: str):
    """Invalidate category cache.

    ``category_items:<name>:count`` is the only key in that namespace
    (lazy_queries._cached_count), so it is deleted by name.
    """
    cache = get_cache_manager()
    if cache:
        await cache.delete_many((
            f"category:{category_name}",
            "categories:count",
            f"child_categories:{category_name}:count",
            f"category_items:{category_name}:count",
        ))


async def invalidate_stats_cache():
    """Invalidate the statistics caches with targeted deletes (no Redis SCAN).

    This runs on nearly every write (purchases, balance changes, catalog
    edits), so it must not use invalidate_pattern — a SCAN per write. Keys
    match how they are built: async_cached appends a colon to zero-arg
    functions (user_count:/admin_count:), stats:daily is keyed by the
    dashboard's local date (UTC included for the midnight window). The hourly
    scheduler sweep janitors any stale dated keys.
    """
    cache = get_cache_manager()
    if cache:
        today_local = moscow_today().isoformat()
        today_utc = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        await cache.delete_many({
            f"stats:daily:{today_local}",
            f"stats:daily:{today_utc}", "user_count:", "admin_count:",
        })


# --- Promo codes ---

async def get_promo_code(code: str) -> dict | None:
    """Return promo code by code string, or None."""
    return await _fetch_one_dict(PromoCodes, PromoCodes.code == code.upper())


async def promo_rule_error(s, promo, user_id) -> str | None:
    """Validate a one-time balance-credit promo; purchase discounts are unsupported."""
    if not promo:
        return "not_found"
    if not promo.is_active:
        return "inactive"
    if promo.discount_type != "balance":
        return "wrong_type"

    expires_at = promo.expires_at
    if expires_at is not None:
        # SQLite returns naive datetimes for DateTime(timezone=True); treat naive as
        # UTC so the comparison is valid on every backend (mirrors coerce_sale_until).
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=datetime.timezone.utc)
        if expires_at < datetime.datetime.now(datetime.timezone.utc):
            return "expired"

    if 0 < promo.max_uses <= promo.current_uses:
        return "max_uses"

    used = (await s.execute(
        select(exists().where(
            PromoCodeUsages.promo_id == promo.id,
            PromoCodeUsages.user_id == user_id,
        ))
    )).scalar()
    if used:
        return "already_used"
    return None


# --- Cart ---

async def get_cart_items(user_id: int) -> list[dict]:
    """Return all cart items for user; each dict includes the current item_name for display."""
    async with Database().session() as s:
        result = await s.execute(
            select(CartItems, Goods.name.label('item_name'))
            .join(Goods, Goods.id == CartItems.item_id)
            .where(CartItems.user_id == user_id)
            .order_by(CartItems.added_at.desc())
        )
        items = []
        for ci, item_name in result.all():
            d = _obj_to_dict(ci, CartItems)
            d['item_name'] = item_name
            items.append(d)
        return items


async def get_cart_count(user_id: int) -> int:
    """Return the total number of units in a user's cart.

    Units, not lines: this feeds the cart badge and the checkout confirmation,
    and both must agree with the number of items the receipt lists.
    """
    async with Database().session() as s:
        return int((await s.execute(
            select(func.coalesce(func.sum(CartItems.quantity), 0)).where(CartItems.user_id == user_id)
        )).scalar() or 0)


# --- Stock subscriptions ---

async def is_subscribed_to_stock(user_id: int, item_name: str) -> bool:
    """Whether the user is waiting for this item to be restocked.
    """
    async with Database().session() as s:
        return bool((await s.execute(
            select(exists().where(
                StockSubscriptions.user_id == user_id,
                StockSubscriptions.item_id == Goods.id,
                Goods.name == item_name,
            ))
        )).scalar())


# --- Reviews ---

@async_cached(ttl=600, key_prefix="avg_rating")
async def get_item_avg_rating(item_name: str) -> float | None:
    """Return average rating for an item, or None if no reviews."""
    async with Database().session() as s:
        result = (await s.execute(
            select(func.avg(Reviews.rating))
            .join(Goods, Goods.id == Reviews.item_id)
            .where(Goods.name == item_name)
        )).scalar()
        return round(float(result), 1) if result else None


async def has_purchased_item(user_id: int, item_name: str) -> bool:
    """Check if user has purchased an item."""
    async with Database().session() as s:
        return (await s.execute(
            select(exists().where(
                BoughtGoods.buyer_id == user_id,
                BoughtGoods.item_name == item_name
            ))
        )).scalar()


async def get_user_review(user_id: int, item_name: str) -> dict | None:
    """Return user's review for an item, or None."""
    async with Database().session() as s:
        obj = (await s.execute(
            select(Reviews)
            .join(Goods, Goods.id == Reviews.item_id)
            .where(Reviews.user_id == user_id, Goods.name == item_name)
        )).scalars().first()
        return _obj_to_dict(obj, Reviews) if obj else None


async def invalidate_rating_cache(item_name: str):
    """Invalidate the review caches for an item (average and count)."""
    cache = get_cache_manager()
    if cache:
        await cache.delete_many((
            f"avg_rating:{item_name}",
            f"count:reviews:{item_name}",
        ))

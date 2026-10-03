from html import escape as _esc

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.i18n import localize
from bot.logger_mesh import logger
from bot.misc import EnvKeys

# Numeric(12, 2) leaves 10 integer digits; anything larger is a DB error. Shared by the add and the update flows so they cannot drift.
MAX_ITEM_PRICE = 99_999_999


def parse_price(text: str) -> int | None:
    """Parse an item price from admin input. None if it is not a usable price.
    """
    price_text = (text or "").strip()
    if not (price_text.isascii() and price_text.isdigit()):
        return None
    price = int(price_text)
    if price < 1 or price > MAX_ITEM_PRICE:
        return None
    return price


async def _notify_restock_safe(
    bot,
    item_name: str,
    *,
    added_count: int = 1,
    notify_all: bool = False,
) -> None:
    """Fire restock notifications, never letting a failure break the stock add."""
    from bot.misc.services.restock_notifier import notify_restock
    try:
        await notify_restock(
            bot,
            item_name,
            added_count=added_count,
            notify_all=notify_all,
        )
    except Exception:
        logger.exception("restock notification failed for %r", item_name)


async def admin_user_identity(bot, user_id: int) -> tuple[str, str]:
    """Return a display name and a safe Telegram profile link for an admin view."""
    fallback_login = f"<a href='tg://user?id={user_id}'>ID {user_id}</a>"
    try:
        chat = await bot.get_chat(user_id)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        logger.debug("Could not resolve Telegram profile for %s: %s", user_id, exc)
        return str(user_id), fallback_login

    first_name = getattr(chat, "first_name", None) or str(user_id)
    username = getattr(chat, "username", None)
    if isinstance(username, str) and username.strip():
        safe_username = _esc(username.strip().lstrip("@"))
        login = f"<a href='tg://user?id={user_id}'>@{safe_username}</a>"
    else:
        login = fallback_login
    return str(first_name), login


def user_profile_lines(user, first_name, target_id, *, overall_balance,
                       items_count, role, referrals, include_referral_id,
                       profile_login: str | None = None):
    """Build the common user-profile text lines shared by the admin profile views.

    Returns a list of lines (join with ``"\n"``). ``include_referral_id`` inserts
    the referral_id line — the read-only show-user view includes it, the
    action-panel view does not. Callers append their own extra lines afterward
    (blocked status, earnings stats).
    """
    lines = [
        localize('profile.caption', name=_esc(str(first_name or '')), id=target_id),
        '',
        localize('profile.id', id=target_id),
        localize('profile.balance', amount=user.get('balance'), currency=EnvKeys.PAY_CURRENCY),
        localize('profile.total_topup', amount=overall_balance, currency=EnvKeys.PAY_CURRENCY),
        localize('profile.purchased_count', count=items_count),
        '',
    ]
    if profile_login:
        lines.insert(3, localize('profile.login', login=profile_login))
    if include_referral_id:
        lines.append(localize('profile.referral_id', id=user.get('referral_id')))
    lines += [
        localize('admin.users.referrals', count=referrals),
        localize('admin.users.role', role=_esc(str(role))),
        localize('profile.registration_date', dt=user.get('registration_date')),
    ]
    return lines

"""Private notifications for catalog changes made by administrators."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape as html_escape
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

from bot.logger_mesh import logger
from bot.misc import EnvKeys
from bot.database.methods.read import select_item_values_amount


_BOT_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}\Z")


def format_catalog_price(price: object) -> str:
    """Render a database/API price without insignificant trailing zeroes."""
    try:
        value = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError):
        return str(price)

    rendered = format(value, "f")
    # Strip fractional zeroes only. ``rstrip('0')`` on an integer would turn
    # 90 into 9, which is especially harmful in a sales notification.
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


async def product_purchase_link(bot: Bot, item_id: int | None) -> str | None:
    """Build a safe deep link to the product card, when the bot has a username."""
    if not item_id or item_id <= 0:
        return None
    try:
        me = await bot.get_me()
    except Exception:
        logger.debug("Could not resolve bot username for product deep link", exc_info=True)
        return None

    username = getattr(me, "username", None)
    if not isinstance(username, str) or not _BOT_USERNAME.fullmatch(username):
        return None
    return f"https://t.me/{username}?start=item_{item_id}"


def _channel_chat_id() -> int | None:
    """Numeric channel id for shop posts, or None when not configured."""
    raw = str(getattr(EnvKeys, "CHANNEL_ID", "") or "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def catalog_currency_label() -> str:
    code = str(getattr(EnvKeys, "PAY_CURRENCY", "") or "RUB").upper()
    return "₽" if code == "RUB" else code


async def _available_total(item_name: str, added: int, is_infinity: bool) -> str:
    """Fresh sellable total for the post (never the possibly stale cache)."""
    if is_infinity:
        return "∞"
    try:
        total = int(await select_item_values_amount(str(item_name)))
    except Exception:
        logger.debug("Could not resolve stock total for %r", item_name, exc_info=True)
        total = 0
    if total <= 0:
        try:
            total = max(0, int(added))
        except (TypeError, ValueError):
            total = 0
    return str(total)


async def announce_catalog_arrival(
    bot: Bot | None,
    *,
    item_name: str,
    price: object,
    count: int,
    item_id: int | None,
    is_infinity: bool = False,
) -> bool:
    """Post an operator-requested restock announcement to the shop channel.

    This is the "notify" checkbox in the stock web form: one channel post with
    a "К товару" deep-link button that opens the product card in the bot.
    (``notify_restock`` separately pings users who follow one product.)
    """
    if bot is None:
        return False
    chat_id = _channel_chat_id()
    if chat_id is None:
        logger.warning("Restock post skipped: CHANNEL_ID is not configured")
        return False

    safe_name = html_escape(str(item_name), quote=False)
    available = await _available_total(item_name, count, is_infinity)
    units = "" if available == "∞" else " шт."
    try:
        added = str(max(0, int(count)))
    except (TypeError, ValueError):
        added = "0"
    added_units = "" if available == "∞" else " шт."
    text = (
        "📦 <b>Пополнение:</b> "
        f"{safe_name}\n"
        f"➕ Завезли: {added}{added_units} | 📦 На складе: {available}{units}\n"
        f"💰 Цена: {format_catalog_price(price)} {catalog_currency_label()}"
    )
    markup = None
    purchase_link = await product_purchase_link(bot, item_id)
    if purchase_link:
        # URL-buttons are the ones Telegram clients render blue.
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Перейти к товару", url=purchase_link),
        ]])

    import asyncio as _asyncio

    last_error: str | None = None
    for attempt in (1, 2, 3):
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=markup,
                disable_notification=False,
                link_preview_options=_NO_PREVIEW,
            )
            break
        except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as exc:
            # Permanent: no point retrying (no rights, chat gone, bad request).
            logger.warning("Restock channel post was not delivered: %s", exc)
            return False
        except Exception as exc:
            # Transient (network blips): retry, then give up loudly.
            last_error = str(exc)
            logger.warning(
                "Restock channel post attempt %s/3 failed, retrying: %s", attempt, exc
            )
            await _asyncio.sleep(5 * attempt)
    else:
        logger.error("Restock channel post failed after 3 attempts: %s", last_error)
        return False

    logger.info("restock channel post sent: item=%r price=%s", item_name, format_catalog_price(price))
    return True


async def notify_owner_stock_added(
    bot: Bot | None,
    *,
    item_name: str,
    price: object,
    count: int,
    is_infinity: bool = False,
    item_id: int | None = None,
) -> bool:
    """Tell the configured owner that sellable stock was added.

    This is deliberately best-effort: a blocked owner chat or a temporary
    Telegram error must never roll back a successful catalog update.
    """
    if bot is None:
        return False

    try:
        owner_id = int(EnvKeys.OWNER_ID)
    except (TypeError, ValueError):
        logger.error("Cannot notify about catalog stock: OWNER_ID is invalid")
        return False

    try:
        quantity = "∞" if is_infinity else str(max(0, int(count)))
    except (TypeError, ValueError):
        logger.error("Cannot notify about catalog stock: invalid count=%r", count)
        return False
    safe_name = html_escape(str(item_name), quote=False)
    safe_currency = html_escape(str(EnvKeys.PAY_CURRENCY), quote=False)
    text = (
        "📦 <b>Товар добавлен в каталог</b>\n"
        f"🏷️ Товар: <b>{safe_name}</b>\n"
        f"💳 Цена: <b>{format_catalog_price(price)} {safe_currency}</b>\n"
        f"➕ Добавлено: <b>{quantity}</b>"
    )
    markup = None
    purchase_link = await product_purchase_link(bot, item_id)
    if purchase_link:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Перейти к товару", url=purchase_link),
        ]])

    try:
        await bot.send_message(
            chat_id=owner_id, text=text, parse_mode="HTML",
            reply_markup=markup, link_preview_options=_NO_PREVIEW,
        )
    except (TelegramForbiddenError, TelegramNotFound, TelegramBadRequest) as exc:
        logger.warning("Owner catalog notification was not delivered: %s", exc)
        return False
    except Exception:
        logger.exception("Unexpected failure while sending owner catalog notification")
        return False

    logger.info(
        "owner catalog notification sent: item=%r price=%s count=%s infinite=%s",
        item_name,
        format_catalog_price(price),
        quantity,
        is_infinity,
    )
    return True

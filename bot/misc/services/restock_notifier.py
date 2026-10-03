from html import escape as html_escape

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from bot.database import Database
from bot.database.models import Goods
from bot.database.methods.delete import pop_stock_subscribers
from bot.database.methods.read import get_all_users
from bot.i18n import localize
from bot.logger_mesh import logger
from bot.misc.services.broadcast_system import BroadcastManager
from bot.misc.services.catalog_notifications import (
    catalog_currency_label,
    format_catalog_price,
    product_purchase_link,
)


async def notify_restock(
    bot: Bot,
    item_name: str,
    *,
    notify_all: bool = False,
    added_count: int = 1,
    price: object | None = None,
    item_id: int | None = None,
) -> int:
    """Notify the right audience about new stock.

    By default only users who explicitly subscribed to this item are notified.
    ``notify_all`` is an operator-controlled stock-entry option: it adds every
    registered Telegram user to the recipients while still consuming the
    item's pending subscriptions. Recipients are deduplicated before sending.
    ``added_count`` is shown in the notification so a bulk upload reports the
    number of newly saved lots rather than the number of pasted lines.

    Returns the number of messages actually delivered.
    """
    subscribed_ids = await pop_stock_subscribers(item_name)
    user_ids = list(dict.fromkeys(subscribed_ids))
    if notify_all:
        all_user_rows = await get_all_users()
        user_ids = list(dict.fromkeys(
            user_ids + [int(row[0]) for row in all_user_rows]
        ))
    if not user_ids:
        return 0

    if price is None or item_id is None:
        async with Database().session() as session:
            row = (await session.execute(
                select(Goods.id, Goods.price).where(Goods.name == item_name)
            )).one_or_none()
        if row is not None:
            if item_id is None:
                item_id = int(row.id)
            if price is None:
                price = row.price

    purchase_link = await product_purchase_link(bot, item_id)
    reply_markup = None
    if purchase_link:
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➡️ Перейти к товару", url=purchase_link),
        ]])

    try:
        quantity = max(1, int(added_count))
    except (TypeError, ValueError):
        quantity = 1

    manager = BroadcastManager(bot)
    stats = await manager.broadcast(
        user_ids=user_ids,
        text=localize(
            "stock.back_in_stock",
            name=html_escape(item_name, quote=False),
            count=quantity,
            price=format_catalog_price(price) if price is not None else "—",
            currency=catalog_currency_label(),
        ),
        reply_markup=reply_markup,
        parse_mode="HTML",
    )

    logger.info(
        "restock notify %r: mode=%s recipients=%s subscribed=%s sent=%s failed=%s",
        item_name,
        "all" if notify_all else "subscribers",
        len(user_ids),
        len(subscribed_ids),
        stats.sent,
        stats.failed,
    )
    return stats.sent

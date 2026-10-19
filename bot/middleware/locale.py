from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.database.methods import check_user_cached
from bot.i18n import reset_locale, set_locale
from bot.misc.currency import set_user_currency_context, reset_user_currency_context
from bot.logger_mesh import logger


class LocaleMiddleware(BaseMiddleware):
    """Select the saved language and currency for one update without global mutable state."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
        locale = None
        currency = None
        if user:
            try:
                row = await check_user_cached(user.id)
                if row:
                    locale = row.get("locale")
                    currency = row.get("currency")
            except Exception:
                # Authentication middleware still performs its own guarded
                # fallback. A temporary DB outage must not turn localization
                # into a second failure point.
                logger.warning("Could not load locale/currency for user %s", user.id, exc_info=True)

        token_loc = set_locale(locale)
        token_curr = set_user_currency_context(currency)
        try:
            return await handler(event, data)
        finally:
            reset_locale(token_loc)
            reset_user_currency_context(token_curr)

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.database.methods import check_user_cached
from bot.i18n import reset_locale, set_locale
from bot.logger_mesh import logger


class LocaleMiddleware(BaseMiddleware):
    """Select the saved language for one update without global mutable state."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
        locale = None
        if user:
            try:
                row = await check_user_cached(user.id)
                locale = row.get("locale") if row else None
            except Exception:
                # Authentication middleware still performs its own guarded
                # fallback. A temporary DB outage must not turn localization
                # into a second failure point.
                logger.warning("Could not load locale for user %s", user.id, exc_info=True)

        token = set_locale(locale)
        try:
            return await handler(event, data)
        finally:
            reset_locale(token)

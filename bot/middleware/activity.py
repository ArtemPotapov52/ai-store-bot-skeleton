"""Count private bot interactions without retaining user-generated content."""

from __future__ import annotations

from datetime import date
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.enums.chat_type import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.database.methods.bot_activity import record_bot_activity
from bot.logger_mesh import logger
from bot.misc.timezone import moscow_today as _moscow_today


def moscow_today() -> date:
    return _moscow_today()


def _is_start_command(text: str | None) -> bool:
    command = (text or "").strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
    return command == "/start"


class BotActivityMiddleware(BaseMiddleware):
    """Count permitted private messages/callbacks; subscription middleware runs first."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return await handler(event, data)

        is_private = False
        if isinstance(event, Message):
            is_private = event.chat.type == ChatType.PRIVATE
            if is_private and _is_start_command(event.text):
                return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            message = event.message
            is_private = bool(
                message is not None
                and getattr(getattr(message, "chat", None), "type", None) == ChatType.PRIVATE
            )

        if is_private:
            try:
                await record_bot_activity(
                    int(user.id), activity_date=moscow_today(), interaction=True
                )
            except Exception:
                # Metrics must never make normal bot functionality unavailable.
                logger.exception("Could not record bot activity for user %s", user.id)

        return await handler(event, data)

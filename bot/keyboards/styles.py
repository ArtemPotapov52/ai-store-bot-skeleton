"""Telegram's predefined button colors for inline menus."""

from random import choice

from aiogram.types import InlineKeyboardMarkup

from bot.keyboards.icons import button_custom_emoji_id


_MIXED_STYLES = ("primary", "success", "danger")


def colorize_markup(markup, *, uniform: str | None = None):
    """Color inline buttons while keeping their text and actions unchanged."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup

    for row in markup.inline_keyboard:
        for button in row:
            if uniform is not None:
                button.style = uniform
            elif button.style is None:
                button.style = choice(_MIXED_STYLES)
            if button.icon_custom_emoji_id is None:
                icon_id = button_custom_emoji_id(button.callback_data)
                if icon_id:
                    button.icon_custom_emoji_id = icon_id
    return markup

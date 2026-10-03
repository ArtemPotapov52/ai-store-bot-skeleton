"""Presentation labels for the AI storefront categories."""
from __future__ import annotations

from html import escape
import re

from bot.i18n import get_locale


AI_EMOJIS: dict[str, str] = {
    "claude": "5190803448521593596",
    "gemini": "5188496814860441480",
    "perplexity": "5188370779045144289",
    "grok": "5190533058855475557",
    "chatgpt": "5190529511212485938",
    "capcut": "6131794507781382239",
}

# The same fallback is used inside the HTML custom-emoji entity. Buttons use
# ``icon_custom_emoji_id`` directly (see ``category_custom_emoji_id`` below).
AI_BUTTON_EMOJIS: dict[str, str] = {
    "chatgpt": "🤖",
    "gemini": "✨",
    "claude": "🧠",
    "grok": "⚡",
    "perplexity": "🔎",
    "capcut": "✂️",
}

SHOP_CUSTOM_EMOJI_ID = "5226513232549664618"
SHOP_BUTTON_EMOJI = "🛍"

_CATEGORY_INTROS: dict[str, dict[str, str]] = {
    "ru": {
        "chatgpt": "Подберите подходящий доступ к ChatGPT для своих задач.",
        "gemini": "Подберите подходящий доступ к Gemini для своих задач.",
        "claude": "Подберите подходящий доступ к Claude для своих задач.",
        "grok": "Подберите подходящий доступ к Grok для своих задач.",
        "perplexity": "Подберите подходящий доступ к Perplexity для своих задач.",
        "capcut": "Подберите подходящий доступ к CapCut для своих задач.",
    },
    "en": {
        "chatgpt": "Choose the ChatGPT access option that fits your needs.",
        "gemini": "Choose the Gemini access option that fits your needs.",
        "claude": "Choose the Claude access option that fits your needs.",
        "grok": "Choose the Grok access option that fits your needs.",
        "perplexity": "Choose the Perplexity access option that fits your needs.",
        "capcut": "Choose the CapCut access option that fits your needs.",
    },
    "vi": {
        "chatgpt": "Chọn gói truy cập ChatGPT phù hợp với nhu cầu của bạn.",
        "gemini": "Chọn gói truy cập Gemini phù hợp với nhu cầu của bạn.",
        "claude": "Chọn gói truy cập Claude phù hợp với nhu cầu của bạn.",
        "grok": "Chọn gói truy cập Grok phù hợp với nhu cầu của bạn.",
        "perplexity": "Chọn gói truy cập Perplexity phù hợp với nhu cầu của bạn.",
        "capcut": "Chọn gói truy cập CapCut phù hợp với nhu cầu của bạn.",
    },
}


def _category_key(category: str | None) -> str | None:
    """Return the known AI key contained in a category label, if any."""
    value = str(category or "").strip().casefold()
    if not value:
        return None
    for key in AI_EMOJIS:
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", value):
            return key
    return None


def category_button_text(category: str) -> str:
    """Return the plain category label used next to the premium icon."""
    return str(category or "").strip()


def category_custom_emoji_id(category: str | None) -> str | None:
    """Return the premium custom emoji id for a known category."""
    key = _category_key(category)
    return AI_EMOJIS.get(key or "")


def category_custom_emoji_markup(category: str | None) -> str:
    """Return the HTML custom-emoji tag for a known category."""
    emoji_id = category_custom_emoji_id(category)
    key = _category_key(category)
    if not emoji_id or not key:
        return ""
    return f'<tg-emoji emoji-id="{emoji_id}">{AI_BUTTON_EMOJIS[key]}</tg-emoji>'


def shop_heading_icon() -> str:
    """Return the premium storefront icon used by the products heading."""

    return f'<tg-emoji emoji-id="{SHOP_CUSTOM_EMOJI_ID}">{SHOP_BUTTON_EMOJI}</tg-emoji>'


def category_heading_icon(category: str | None) -> str:
    """Return a category logo for the goods-list heading.

    Known AI categories use their own logo; custom categories use the
    storefront's premium products logo instead of a Unicode-only fallback.
    """
    return category_custom_emoji_markup(category) or shop_heading_icon()


def category_intro(category: str | None) -> str:
    """Build the category-specific lead shown before a product heading.

    The returned value is intentionally HTML because all storefront screens
    render in Telegram's HTML parse mode. Unknown/custom categories receive
    no lead and keep the existing generic product layout.
    """
    label = str(category or "").strip()
    key = _category_key(label)
    if not key or not label:
        return ""
    locale = get_locale()
    copy = _CATEGORY_INTROS.get(locale, _CATEGORY_INTROS["ru"])[key]
    emoji_markup = category_custom_emoji_markup(label)
    return (
        f"{emoji_markup} "
        f"<b>{escape(label, quote=False)}</b>\n{copy}"
    )


__all__ = [
    "AI_EMOJIS",
    "AI_BUTTON_EMOJIS",
    "SHOP_CUSTOM_EMOJI_ID",
    "SHOP_BUTTON_EMOJI",
    "category_button_text",
    "category_custom_emoji_id",
    "category_custom_emoji_markup",
    "category_heading_icon",
    "category_intro",
    "shop_heading_icon",
]

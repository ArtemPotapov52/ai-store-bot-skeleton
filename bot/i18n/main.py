from __future__ import annotations
from contextvars import ContextVar, Token
from functools import lru_cache
from html import escape as _html_escape
from typing import Any

from bot.misc import EnvKeys
from bot.misc.timezone import format_moscow_datetime
from .strings import TRANSLATIONS, DEFAULT_LOCALE
from bot.logger_mesh import logger


_request_locale: ContextVar[str | None] = ContextVar("request_locale", default=None)


def esc(value: Any) -> str:
    """Escape a value for interpolation into a message."""
    return _html_escape("" if value is None else str(value), quote=False)


@lru_cache(maxsize=1)
def get_default_locale() -> str:
    loc = EnvKeys.BOT_LOCALE.lower().strip()
    return loc if loc in TRANSLATIONS else DEFAULT_LOCALE


def get_locale() -> str:
    """Return the current update's locale, falling back to the bot default."""
    return _request_locale.get() or get_default_locale()


def set_locale(locale: str | None) -> Token:
    normalized = (locale or "").lower().strip()
    if normalized not in TRANSLATIONS:
        normalized = get_default_locale()
    return _request_locale.set(normalized)


def reset_locale(token: Token) -> None:
    _request_locale.reset(token)


# Backward-compatible hook used by existing callers/tests that previously
# cleared the cached global locale through get_locale.cache_clear().
get_locale.cache_clear = get_default_locale.cache_clear  # type: ignore[attr-defined]


def localize(key: str, /, **kwargs: Any) -> str:
    """
    Get translation by key.
    Fallback: current locale -> DEFAULT_LOCALE -> the key itself.
    """
    loc = get_locale()

    text = TRANSLATIONS.get(loc, {}).get(key)
    if text is None:
        text = TRANSLATIONS.get(DEFAULT_LOCALE, {}).get(key)
    if text is None:
        text = key

    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Failed to format translation key '{key}' with kwargs {kwargs}: {e}")

    return str(text)


def format_dt(value: Any) -> str:
    """Render a datetime or ISO timestamp as ``DD.MM.YYYY HH:MM`` in Moscow."""
    return format_moscow_datetime(value)

from __future__ import annotations
import os
from contextvars import ContextVar, Token
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

# Default exchange rate: 1 USD = 90 RUB (can be overridden via USD_RUB_RATE env)
USD_RUB_RATE: Decimal = Decimal(os.getenv("USD_RUB_RATE", "90.0"))

SUPPORTED_CURRENCIES = ("RUB", "USD")
DEFAULT_CURRENCY = "RUB"

_request_currency: ContextVar[str | None] = ContextVar("request_currency", default=None)

CURRENCY_SYMBOLS = {
    "RUB": "₽",
    "USD": "$",
}

CURRENCY_NAMES = {
    "ru": {
        "RUB": "RUB (₽)",
        "USD": "USD ($)",
    },
    "en": {
        "RUB": "RUB (₽)",
        "USD": "USD ($)",
    },
    "vi": {
        "RUB": "RUB (₽)",
        "USD": "USD ($)",
    },
}


def get_user_currency() -> str:
    """Return the current update's currency, falling back to DEFAULT_CURRENCY ('RUB')."""
    return _request_currency.get() or DEFAULT_CURRENCY


def set_user_currency_context(currency: str | None) -> Token:
    """Bind currency to the current asyncio task/context."""
    norm = (currency or "").upper().strip()
    if norm not in SUPPORTED_CURRENCIES:
        norm = DEFAULT_CURRENCY
    return _request_currency.set(norm)


def reset_user_currency_context(token: Token) -> None:
    _request_currency.reset(token)


def rub_to_usd(rub_amount: Any) -> Decimal:
    """Convert an amount in RUB to USD using 1 USD = 90 RUB."""
    try:
        val = Decimal(str(rub_amount))
    except Exception:
        val = Decimal("0.00")
    if USD_RUB_RATE <= 0:
        return val
    return (val / USD_RUB_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def usd_to_rub(usd_amount: Any) -> Decimal:
    """Convert an amount in USD to RUB using 1 USD = 90 RUB."""
    try:
        val = Decimal(str(usd_amount))
    except Exception:
        val = Decimal("0.00")
    return (val * USD_RUB_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def convert_from_rub(rub_amount: Any, target_currency: str | None = None) -> Decimal:
    """Convert a base RUB amount to target_currency (defaults to request currency)."""
    curr = (target_currency or get_user_currency()).upper()
    if curr == "USD":
        return rub_to_usd(rub_amount)
    try:
        return Decimal(str(rub_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def convert_to_rub(amount: Any, source_currency: str | None = None) -> Decimal:
    """Convert an amount from source_currency to base RUB."""
    curr = (source_currency or get_user_currency()).upper()
    if curr == "USD":
        return usd_to_rub(amount)
    try:
        return Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal("0.00")


def get_currency_symbol(currency: str | None = None) -> str:
    """Return currency symbol, e.g. '$' for USD, '₽' for RUB."""
    curr = (currency or get_user_currency()).upper()
    return CURRENCY_SYMBOLS.get(curr, curr)


def format_currency_amount(rub_amount: Any, currency: str | None = None, show_both: bool = False) -> str:
    """Format a base RUB amount for display in the target currency."""
    curr = (currency or get_user_currency()).upper()
    try:
        base_rub = Decimal(str(rub_amount))
    except Exception:
        return "—"
    if curr == "USD":
        usd_val = rub_to_usd(base_rub)
        if show_both:
            return f"${usd_val:.2f} ({base_rub:.2f} ₽)"
        return f"${usd_val:.2f}"
    return f"{base_rub:.2f} ₽"

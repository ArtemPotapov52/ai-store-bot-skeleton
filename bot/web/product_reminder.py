"""Validation and message formatting for the web-admin product reminder."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any


MAX_REMINDER_PRICE = Decimal("9999999999.99")


class ProductReminderError(ValueError):
    """Invalid selection or price in the product reminder form."""


def parse_reminder_price(raw_value: Any, current_price: Decimal) -> tuple[Decimal, bool]:
    """Return (advertised price, explicitly entered) with strict bounds."""
    raw = str(raw_value or "").strip()
    if not raw:
        return Decimal(str(current_price)).quantize(Decimal("0.01")), False

    try:
        price = Decimal(raw.replace(",", "."))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProductReminderError("Введите корректную цену или оставьте поле пустым.") from exc
    if not price.is_finite() or price <= 0 or price > MAX_REMINDER_PRICE:
        raise ProductReminderError(
            "Цена должна быть положительной суммой не больше 9 999 999 999,99."
        )
    if price.as_tuple().exponent < -2:
        raise ProductReminderError("Цена должна содержать не больше двух знаков после запятой.")
    try:
        price = price.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ProductReminderError("Цена должна содержать не более двух знаков после запятой.") from exc
    return price, True


def format_reminder_money(value: Any) -> str:
    """Format prices without unnecessary trailing decimal zeroes."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal(0)
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def build_reminder_text(
    *,
    item_name: str,
    quantity: int,
    is_infinite: bool,
    old_price: Decimal,
    new_price: Decimal,
    currency: str,
) -> str:
    """Create safe Telegram HTML showing actual availability and current price."""
    safe_name = escape(str(item_name), quote=False)
    safe_currency = escape(str(currency), quote=False)
    stock = "∞" if is_infinite else f"{max(0, int(quantity))} шт."
    price = f"{format_reminder_money(new_price)} {safe_currency}"
    price_line = f"💰 Цена: <b>{price}</b>"
    return (
        "🔔 <b>Товар ещё в наличии!</b>\n"
        f"🏷️ <b>{safe_name}</b>\n"
        f"📦 В наличии: <b>{stock}</b>\n"
        f"{price_line}"
    )

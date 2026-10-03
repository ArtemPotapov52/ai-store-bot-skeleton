"""Validation and aggregation helpers for the web revenue dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from bot.misc.timezone import MOSCOW_TZ

ZERO = Decimal("0")
MAX_MANUAL_QUANTITY = 100_000
MAX_MANUAL_PRICE = Decimal("9999999999.99")
# Keep the launch-period view as the default while letting administrators
# inspect a wider, explicitly selected window when needed.
REVENUE_DAYS = 2
# Exclude known test-traffic dates from every revenue view, without deleting
# the underlying transactions. These are one-time dates, not monthly rules.
REVENUE_EXCLUDED_DATES = frozenset({date(2026, 9, 19), date(2026, 9, 20)})
REVENUE_PERIOD_OPTIONS = {
    2: "2 дня",
    7: "Неделя",
    30: "Месяц",
    90: "3 месяца",
    365: "Год",
}


class RevenueInputError(ValueError):
    """A user-facing validation error for manual revenue input."""


def parse_manual_revenue_fields(
    category_id_raw: Any,
    quantity_raw: Any,
    unit_price_raw: Any,
) -> tuple[int, int, Decimal]:
    """Parse and validate category ID, quantity and unit price from the form."""
    try:
        category_id = int(str(category_id_raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise RevenueInputError("Выберите категорию.") from exc
    if category_id <= 0:
        raise RevenueInputError("Выберите категорию.")

    try:
        quantity = int(str(quantity_raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise RevenueInputError("Количество должно быть целым числом.") from exc
    if quantity < 1 or quantity > MAX_MANUAL_QUANTITY:
        raise RevenueInputError(
            f"Количество должно быть от 1 до {MAX_MANUAL_QUANTITY:,}.".replace(",", " ")
        )

    try:
        unit_price = Decimal(str(unit_price_raw or "").strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise RevenueInputError("Цена должна быть положительной суммой.") from exc
    if not unit_price.is_finite() or unit_price <= 0 or unit_price > MAX_MANUAL_PRICE:
        raise RevenueInputError("Цена должна быть положительной суммой до 9 999 999 999,99.")
    return category_id, quantity, unit_price.quantize(Decimal("0.01"))


def parse_revenue_period(raw_value: Any) -> int:
    """Parse a supported dashboard period, defaulting safely to two days."""
    try:
        days = int(str(raw_value or "").strip())
    except (TypeError, ValueError):
        return REVENUE_DAYS
    return days if days in REVENUE_PERIOD_OPTIONS else REVENUE_DAYS


def revenue_window(today: date, days: int = REVENUE_DAYS) -> tuple[date, date]:
    """Return the inclusive start and exclusive end of a selected period."""
    days = parse_revenue_period(days)
    return today - timedelta(days=days - 1), today + timedelta(days=1)


def _local_date(value: datetime) -> date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ).date()


def is_excluded_revenue_entry_date(value: date | datetime) -> bool:
    """Whether a date or timestamp falls on one of the excluded test dates."""
    day = _local_date(value) if isinstance(value, datetime) else value
    return day in REVENUE_EXCLUDED_DATES


def _money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return ZERO
    return result if result.is_finite() else ZERO


def build_revenue_report(
    actual_rows: Iterable[Any],
    manual_rows: Iterable[Any],
    category_by_item: dict[str, str],
    *,
    period_start: date,
    days: int,
) -> dict[str, Any]:
    """Build period, previous-period, daily, category and product aggregates.

    ``actual_rows`` must contain ``item_name``, ``price``, ``buyer_id`` and
    ``bought_datetime``. ``manual_rows`` must contain ``category_name``,
    ``quantity``, ``unit_price`` and ``created_at``. Rows may include the
    immediately preceding period so the comparison is calculated in one pass.
    """
    period_end = period_start + timedelta(days=days)
    previous_start = period_start - timedelta(days=days)
    daily: dict[date, dict[str, Any]] = {}

    def day_totals(day: date) -> dict[str, Any]:
        return daily.setdefault(day, {
            "date": day,
            "revenue": ZERO,
            "actual_revenue": ZERO,
            "manual_revenue": ZERO,
            "units": 0,
        })
    categories: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "units": 0, "revenue": ZERO}
    )
    products: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "units": 0, "revenue": ZERO}
    )
    previous_revenue = ZERO
    actual_revenue = ZERO
    manual_revenue = ZERO
    actual_units = 0
    manual_units = 0
    orders = 0
    buyers: set[int] = set()

    for row in actual_rows:
        row_date = _local_date(row.bought_datetime)
        if is_excluded_revenue_entry_date(row_date):
            continue
        price = _money(row.price)
        if previous_start <= row_date < period_start:
            previous_revenue += price
            continue
        if row_date < period_start or row_date >= period_end:
            continue

        actual_revenue += price
        actual_units += 1
        orders += 1
        if row.buyer_id is not None:
            buyers.add(int(row.buyer_id))
        day = day_totals(row_date)
        day["revenue"] += price
        day["actual_revenue"] += price
        day["units"] += 1

        item_name = str(row.item_name or "").strip() or "Без названия"
        product = products[item_name]
        product["name"] = item_name
        product["units"] += 1
        product["revenue"] += price

        category_name = category_by_item.get(item_name, "Без категории")
        category = categories[category_name]
        category["name"] = category_name
        category["units"] += 1
        category["revenue"] += price

    for row in manual_rows:
        row_date = _local_date(row.created_at)
        if is_excluded_revenue_entry_date(row_date):
            continue
        quantity = int(row.quantity or 0)
        total = _money(row.unit_price) * quantity
        if previous_start <= row_date < period_start:
            previous_revenue += total
            continue
        if row_date < period_start or row_date >= period_end:
            continue

        manual_revenue += total
        manual_units += quantity
        day = day_totals(row_date)
        day["revenue"] += total
        day["manual_revenue"] += total
        day["units"] += quantity

        category_name = str(row.category_name or "Без категории").strip() or "Без категории"
        category = categories[category_name]
        category["name"] = category_name
        category["units"] += quantity
        category["revenue"] += total

    current_revenue = actual_revenue + manual_revenue
    if previous_revenue == 0:
        change_percent = None if current_revenue == 0 else Decimal("100")
    else:
        change_percent = ((current_revenue - previous_revenue) / previous_revenue * 100).quantize(
            Decimal("0.01")
        )

    return {
        "summary": {
            "revenue": current_revenue,
            "actual_revenue": actual_revenue,
            "manual_revenue": manual_revenue,
            "previous_revenue": previous_revenue,
            "change_percent": change_percent,
            "units": actual_units + manual_units,
            "orders": orders,
            "actual_units": actual_units,
            "manual_units": manual_units,
            "unique_buyers": len(buyers),
        },
        "daily": [
            daily[current] for current in sorted(daily)
            if daily[current]["revenue"] != ZERO
        ],
        "categories": sorted(categories.values(), key=lambda row: (-row["revenue"], row["name"])),
        "products": sorted(products.values(), key=lambda row: (-row["revenue"], row["name"])),
    }

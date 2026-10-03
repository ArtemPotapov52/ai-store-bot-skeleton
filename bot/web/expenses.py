"""Validation and aggregation helpers for operator-entered product expenses."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from bot.misc.timezone import MOSCOW_TZ
from bot.web.revenue import parse_revenue_period

ZERO = Decimal("0")
CENT = Decimal("0.01")
MAX_EXPENSE_QUANTITY = 100_000
MAX_EXPENSE_AMOUNT = Decimal("9999999999.99")


class ExpenseInputError(ValueError):
    """A user-facing validation error for an expense form."""


def parse_expense_fields(
    product_id_raw: Any,
    quantity_raw: Any,
    amount_raw: Any,
    cost_mode_raw: Any,
) -> tuple[int, int, Decimal]:
    """Validate an expense form and return product ID, quantity, total cost."""
    try:
        product_id = int(str(product_id_raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise ExpenseInputError("Выберите товар из списка.") from exc
    if product_id <= 0:
        raise ExpenseInputError("Выберите товар из списка.")

    try:
        quantity = int(str(quantity_raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise ExpenseInputError("Количество должно быть целым числом.") from exc
    if quantity < 1 or quantity > MAX_EXPENSE_QUANTITY:
        raise ExpenseInputError(
            f"Количество должно быть от 1 до {MAX_EXPENSE_QUANTITY:,}.".replace(",", " ")
        )

    mode = str(cost_mode_raw or "").strip().lower()
    if mode not in {"unit", "total"}:
        raise ExpenseInputError("Выберите цену за штуку или сумму закупки.")

    try:
        amount = Decimal(str(amount_raw or "").strip().replace(",", "."))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExpenseInputError("Введите положительную сумму в рублях.") from exc
    if not amount.is_finite() or amount <= ZERO or amount > MAX_EXPENSE_AMOUNT:
        raise ExpenseInputError("Сумма должна быть больше нуля и не превышать 9 999 999 999,99.")
    rounded_amount = amount.quantize(CENT)
    if rounded_amount != amount:
        raise ExpenseInputError("Сумма должна содержать не более двух знаков после запятой.")

    total_cost = rounded_amount * quantity if mode == "unit" else rounded_amount
    if total_cost > MAX_EXPENSE_AMOUNT:
        raise ExpenseInputError("Итоговая сумма закупки не должна превышать 9 999 999 999,99.")
    return product_id, quantity, total_cost.quantize(CENT)


def _local_date(value: datetime) -> date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MOSCOW_TZ).date()


def _money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO
    return result if result.is_finite() else ZERO


def _field(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def build_expense_report(
    rows: Iterable[Any],
    *,
    period_start: date,
    days: int,
) -> dict[str, Any]:
    """Aggregate expense rows over a Moscow-calendar date window."""
    days = parse_revenue_period(days)
    period_end = period_start + timedelta(days=days)
    daily: dict[date, dict[str, Any]] = {}
    products: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "quantity": 0, "entries": 0, "total_cost": ZERO}
    )
    total_cost = ZERO
    total_quantity = 0
    entry_count = 0

    for row in rows:
        day = _local_date(row.created_at)
        if day < period_start or day >= period_end:
            continue
        quantity = int(row.quantity or 0)
        amount = _money(row.total_cost)
        if quantity <= 0 or amount <= ZERO:
            continue

        daily_row = daily.setdefault(day, {
            "date": day, "quantity": 0, "entries": 0, "total_cost": ZERO,
        })
        daily_row["quantity"] += quantity
        daily_row["entries"] += 1
        daily_row["total_cost"] += amount

        product_name = str(row.product_name or "Без названия").strip() or "Без названия"
        product = products[product_name]
        product["name"] = product_name
        product["quantity"] += quantity
        product["entries"] += 1
        product["total_cost"] += amount

        total_cost += amount
        total_quantity += quantity
        entry_count += 1

    product_rows = list(products.values())
    for product in product_rows:
        product["average_unit_cost"] = (
            (product["total_cost"] / product["quantity"]).quantize(CENT)
            if product["quantity"] else ZERO
        )
    product_rows.sort(key=lambda row: (-row["total_cost"], row["name"]))

    return {
        "summary": {
            "total_cost": total_cost,
            "quantity": total_quantity,
            "entries": entry_count,
            "average_unit_cost": (
                (total_cost / total_quantity).quantize(CENT) if total_quantity else ZERO
            ),
        },
        "daily": [daily[day] for day in sorted(daily)],
        "products": product_rows,
    }


def build_inventory_forecast(
    stock_rows: Iterable[Any],
    expense_cost_rows: Iterable[Any],
) -> dict[str, Any]:
    """Estimate proceeds from finite live stock using current prices.

    Expense entries do not decrement inventory, so the weighted average unit
    cost from the supplied purchase period is only an estimate of the remaining
    stock's cost basis. Products without a matching cost are kept out of
    estimated profit, and unlimited-delivery products are displayed but
    excluded from totals.
    """
    costs_by_product: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"quantity": 0, "total_cost": ZERO}
    )
    for row in expense_cost_rows:
        try:
            product_id = int(_field(row, "product_id"))
            quantity = int(_field(row, "quantity", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        amount = _money(_field(row, "total_cost", ZERO))
        if product_id <= 0 or quantity <= 0 or amount <= ZERO:
            continue
        costs = costs_by_product[product_id]
        costs["quantity"] += quantity
        costs["total_cost"] += amount

    items: list[dict[str, Any]] = []
    summary = {
        "quantity": 0,
        "finite_products": 0,
        "unlimited_products": 0,
        "potential_revenue": ZERO,
        "costed_quantity": 0,
        "estimated_cost": ZERO,
        "estimated_revenue_with_cost": ZERO,
        "estimated_profit": ZERO,
        "unpriced_quantity": 0,
        "unpriced_revenue": ZERO,
    }

    for row in stock_rows:
        try:
            product_id = int(_field(row, "id"))
            quantity = max(0, int(_field(row, "quantity", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            continue
        unlimited = bool(_field(row, "is_infinite", False))
        if not unlimited and quantity == 0:
            continue

        sale_price = _money(_field(row, "sale_price", ZERO)).quantize(CENT)
        item = {
            "id": product_id,
            "name": str(_field(row, "name", "Без названия") or "Без названия"),
            "quantity": quantity,
            "unlimited": unlimited,
            "sale_price": sale_price,
            "potential_revenue": ZERO,
            "average_unit_cost": None,
            "estimated_cost": None,
            "estimated_profit": None,
        }
        if unlimited:
            summary["unlimited_products"] += 1
            items.append(item)
            continue

        potential_revenue = (sale_price * quantity).quantize(CENT)
        item["potential_revenue"] = potential_revenue
        summary["quantity"] += quantity
        summary["finite_products"] += 1
        summary["potential_revenue"] += potential_revenue

        cost_history = costs_by_product.get(product_id)
        if cost_history and cost_history["quantity"] > 0:
            average_unit_cost = (
                cost_history["total_cost"] / cost_history["quantity"]
            ).quantize(CENT)
            estimated_cost = (average_unit_cost * quantity).quantize(CENT)
            item.update({
                "average_unit_cost": average_unit_cost,
                "estimated_cost": estimated_cost,
                "estimated_profit": (potential_revenue - estimated_cost).quantize(CENT),
            })
            summary["costed_quantity"] += quantity
            summary["estimated_cost"] += estimated_cost
            summary["estimated_revenue_with_cost"] += potential_revenue
            summary["estimated_profit"] += potential_revenue - estimated_cost
        else:
            summary["unpriced_quantity"] += quantity
            summary["unpriced_revenue"] += potential_revenue
        items.append(item)

    for key in (
        "potential_revenue", "estimated_cost", "estimated_revenue_with_cost",
        "estimated_profit", "unpriced_revenue",
    ):
        summary[key] = summary[key].quantize(CENT)
    items.sort(key=lambda item: (item["unlimited"], -item["potential_revenue"], item["name"]))
    return {"summary": summary, "items": items}

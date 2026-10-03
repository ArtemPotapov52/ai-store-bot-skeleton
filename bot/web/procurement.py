"""Validation and forecast calculations for procurement plans."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_LINES = 50
MAX_QUANTITY = 100_000
MAX_AMOUNT = Decimal("9999999999.99")


class ProcurementInputError(ValueError):
    """A user-facing validation error for procurement forecast input."""


@dataclass(frozen=True)
class ProcurementLineInput:
    category_id: int
    product_id: int
    quantity: int
    unit_cost: Decimal
    sale_price: Decimal
    sale_price_mode: str


def _values(form: Any, name: str) -> list[Any]:
    getter = getattr(form, "getlist", None)
    if callable(getter):
        result = getter(name)
    else:
        result = form.get(name, [])
    if isinstance(result, (str, bytes)) or not isinstance(result, (list, tuple)):
        return [result]
    return list(result)


def _positive_int(raw: Any, label: str, maximum: int) -> int:
    try:
        parsed = int(str(raw or "").strip())
    except (TypeError, ValueError) as exc:
        raise ProcurementInputError(f"{label} должно быть целым числом.") from exc
    if parsed < 1 or parsed > maximum:
        raise ProcurementInputError(f"{label} должно быть от 1 до {maximum:,}.".replace(",", " "))
    return parsed


def _positive_money(raw: Any, label: str) -> Decimal:
    try:
        value = Decimal(str(raw or "").strip().replace(",", "."))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ProcurementInputError(f"Укажите корректную сумму: {label}.") from exc
    if not value.is_finite() or value <= 0 or value > MAX_AMOUNT:
        raise ProcurementInputError(f"{label.capitalize()} должна быть больше нуля и не превышать 9 999 999 999,99.")
    rounded = value.quantize(CENT, rounding=ROUND_HALF_UP)
    if rounded != value:
        raise ProcurementInputError(f"{label.capitalize()} должна содержать не более двух знаков после запятой.")
    return rounded


def parse_procurement_form(
    form: Any,
    catalog_prices: Mapping[int, Decimal],
) -> list[ProcurementLineInput]:
    """Parse repeated form fields; catalog prices must come from the database."""
    names = ("category_id", "product_id", "quantity", "unit_cost", "sale_mode")
    columns = {name: _values(form, name) for name in names}
    line_count = len(columns["product_id"])
    if not 1 <= line_count <= MAX_LINES:
        raise ProcurementInputError(f"Добавьте от 1 до {MAX_LINES} товаров в корзину.")
    if any(len(column) != line_count for column in columns.values()):
        raise ProcurementInputError("Не удалось прочитать строки корзины. Обновите страницу и заполните её заново.")

    # Browsers omit disabled successful controls. In catalog mode the custom
    # price input is disabled, so FormData contains values only for manual rows.
    custom_prices = _values(form, "custom_sale_price")
    manual_indexes = [
        index for index, mode in enumerate(columns["sale_mode"])
        if str(mode or "").strip().lower() == "manual"
    ]
    if len(custom_prices) == line_count:
        custom_prices_by_line = custom_prices  # tolerate clients sending blanks for disabled controls
    elif len(custom_prices) == len(manual_indexes):
        custom_prices_by_line = [""] * line_count
        for index, value in zip(manual_indexes, custom_prices):
            custom_prices_by_line[index] = value
    else:
        raise ProcurementInputError("Не удалось прочитать строки корзины. Обновите страницу и заполните её заново.")

    lines: list[ProcurementLineInput] = []
    seen_products: set[int] = set()
    for index in range(line_count):
        category_id = _positive_int(columns["category_id"][index], "Категория", 2_147_483_647)
        product_id = _positive_int(columns["product_id"][index], "Товар", 2_147_483_647)
        if product_id in seen_products:
            raise ProcurementInputError("Один и тот же товар можно добавить в корзину только один раз.")
        seen_products.add(product_id)

        quantity = _positive_int(columns["quantity"][index], "Количество", MAX_QUANTITY)
        unit_cost = _positive_money(columns["unit_cost"][index], "Закупочная цена за штуку")
        mode = str(columns["sale_mode"][index] or "").strip().lower()
        if mode == "catalog":
            try:
                sale_price = _positive_money(catalog_prices[product_id], "Цена продажи")
            except KeyError as exc:
                raise ProcurementInputError("Не удалось получить актуальную цену товара из каталога.") from exc
        elif mode == "manual":
            sale_price = _positive_money(custom_prices_by_line[index], "Цена продажи")
        else:
            raise ProcurementInputError("Выберите цену из каталога или укажите свою.")

        if unit_cost * quantity > MAX_AMOUNT or sale_price * quantity > MAX_AMOUNT:
            raise ProcurementInputError("Итог по строке не должен превышать 9 999 999 999,99.")
        lines.append(ProcurementLineInput(
            category_id=category_id,
            product_id=product_id,
            quantity=quantity,
            unit_cost=unit_cost,
            sale_price=sale_price,
            sale_price_mode=mode,
        ))
    return lines


def parse_plan_metadata(form: Any) -> tuple[date, str | None]:
    raw_date = str(form.get("plan_date") or "").strip()
    try:
        plan_date = date.fromisoformat(raw_date)
    except (TypeError, ValueError) as exc:
        raise ProcurementInputError("Укажите дату плана в формате ГГГГ-ММ-ДД.") from exc
    if not date(2000, 1, 1) <= plan_date <= date(2100, 12, 31):
        raise ProcurementInputError("Дата плана должна быть между 01.01.2000 и 31.12.2100.")
    title = str(form.get("title") or "").strip()
    if len(title) > 120:
        raise ProcurementInputError("Название плана не должно быть длиннее 120 символов.")
    return plan_date, title or None


def calculate_forecast(lines: Iterable[Any]) -> dict[str, Any]:
    total_cost = ZERO
    expected_revenue = ZERO
    quantity = 0
    line_count = 0
    for line in lines:
        count = int(line.quantity)
        cost = Decimal(str(line.unit_cost)).quantize(CENT)
        sale_price = Decimal(str(line.sale_price)).quantize(CENT)
        total_cost += cost * count
        expected_revenue += sale_price * count
        quantity += count
        line_count += 1

    total_cost = total_cost.quantize(CENT)
    expected_revenue = expected_revenue.quantize(CENT)
    if total_cost > MAX_AMOUNT or expected_revenue > MAX_AMOUNT:
        raise ProcurementInputError("Итог корзины не должен превышать 9 999 999 999,99.")
    gross_profit = (expected_revenue - total_cost).quantize(CENT)
    margin = (
        (gross_profit * 100 / expected_revenue).quantize(CENT, rounding=ROUND_HALF_UP)
        if expected_revenue else ZERO
    )
    return_on_cost = (
        (gross_profit * 100 / total_cost).quantize(CENT, rounding=ROUND_HALF_UP)
        if total_cost else ZERO
    )
    return {
        "total_cost": total_cost,
        "expected_revenue": expected_revenue,
        "gross_profit": gross_profit,
        "gross_margin_percent": margin,
        "return_on_cost_percent": return_on_cost,
        "quantity": quantity,
        "line_count": line_count,
    }


def build_daily_procurement_report(plans: Iterable[Any]) -> list[dict[str, Any]]:
    """Summarize forecast plans by their explicit Moscow-local plan date."""
    days: dict[date, dict[str, Any]] = defaultdict(lambda: {
        "date": None,
        "plans": 0,
        "items": 0,
        "total_cost": ZERO,
        "expected_revenue": ZERO,
        "gross_profit": ZERO,
    })
    for plan in plans:
        plan_day = plan.plan_date
        if not isinstance(plan_day, date):
            continue
        row = days[plan_day]
        row["date"] = plan_day
        row["plans"] += int(getattr(plan, "plan_count", 1) or 0)
        row["items"] += int(getattr(plan, "item_count", 0) or 0)
        row["total_cost"] += Decimal(str(plan.total_cost or 0))
        row["expected_revenue"] += Decimal(str(plan.expected_revenue or 0))
        row["gross_profit"] += Decimal(str(plan.gross_profit or 0))

    result = []
    for plan_day in sorted(days):
        row = days[plan_day]
        for amount_key in ("total_cost", "expected_revenue", "gross_profit"):
            row[amount_key] = row[amount_key].quantize(CENT)
        result.append(row)
    return result

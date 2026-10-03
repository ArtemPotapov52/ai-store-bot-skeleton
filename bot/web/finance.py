"""Input validation and small deterministic helpers for the finance ledger."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from bot.misc.timezone import MOSCOW_TZ
from bot.web.revenue import REVENUE_DAYS, REVENUE_PERIOD_OPTIONS

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal("9999999999.99")
FINANCE_SOURCES = {"crypto", "card", "cash", "other"}
FINANCE_CURRENCIES = {"RUB", "USDT", "USD", "BYN"}
FINANCE_PERIOD_OPTIONS = {1: "Сегодня", **REVENUE_PERIOD_OPTIONS}


def parse_finance_period(raw_value: Any) -> int:
    """Parse a Finance-only date filter; other admin dashboards stay unchanged."""
    try:
        days = int(str(raw_value or "").strip())
    except (TypeError, ValueError):
        return REVENUE_DAYS
    return days if days in FINANCE_PERIOD_OPTIONS else REVENUE_DAYS


def finance_window(today: date, days: Any = REVENUE_DAYS) -> tuple[date, date]:
    """Return Moscow calendar bounds [start, end), including the selected day."""
    period = parse_finance_period(days)
    return today - timedelta(days=period - 1), today + timedelta(days=1)


class FinanceInputError(ValueError):
    """A safe, user-facing finance form validation error."""


def parse_finance_receipt_fields(
    source_raw: Any,
    user_id_raw: Any,
    currency_raw: Any,
    amount_raw: Any,
    amount_rub_raw: Any,
    date_raw: Any,
    reference_raw: Any = "",
    note_raw: Any = "",
) -> dict[str, Any]:
    source = str(source_raw or "").strip().lower()
    currency = str(currency_raw or "").strip().upper()
    if source not in FINANCE_SOURCES:
        raise FinanceInputError("Выберите источник поступления.")
    if currency not in FINANCE_CURRENCIES:
        raise FinanceInputError("Выберите валюту поступления.")

    def money(raw: Any, label: str) -> Decimal:
        try:
            value = Decimal(str(raw or "").strip().replace(" ", "").replace(",", "."))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise FinanceInputError(f"Проверьте поле «{label}». Нужна положительная сумма.") from exc
        if not value.is_finite() or value <= 0 or value > MAX_AMOUNT:
            raise FinanceInputError(f"Поле «{label}» должно быть больше нуля и не превышать 9 999 999 999,99.")
        rounded = value.quantize(CENT)
        if rounded != value:
            raise FinanceInputError(f"В поле «{label}» допустимо не больше двух знаков после запятой.")
        return rounded

    user_raw = str(user_id_raw or "").strip()
    user_id = None
    if user_raw:
        try:
            user_id = int(user_raw)
        except ValueError as exc:
            raise FinanceInputError("ID пользователя должен быть целым числом Telegram.") from exc
        if user_id <= 0:
            raise FinanceInputError("ID пользователя должен быть положительным.")

    date_text = str(date_raw or "").strip()
    try:
        local_day = date.fromisoformat(date_text)
    except ValueError as exc:
        raise FinanceInputError("Укажите дату поступления.") from exc
    if local_day > datetime.now(MOSCOW_TZ).date():
        raise FinanceInputError("Дата поступления не может быть в будущем.")
    received_at = datetime.combine(local_day, time.min, tzinfo=MOSCOW_TZ).astimezone(timezone.utc)

    reference = str(reference_raw or "").strip() or None
    note = str(note_raw or "").strip() or None
    if reference and len(reference) > 128:
        raise FinanceInputError("Номер/хэш операции не должен быть длиннее 128 символов.")
    if note and len(note) > 2000:
        raise FinanceInputError("Комментарий не должен быть длиннее 2000 символов.")

    amount = money(amount_raw, "Сумма поступления")
    amount_rub = money(amount_rub_raw, "Эквивалент в рублях")
    if currency == "RUB" and amount != amount_rub:
        raise FinanceInputError("Для поступления в RUB сумма и рублёвый эквивалент должны совпадать.")

    return {
        "source": source,
        "user_id": user_id,
        "currency": currency,
        "amount": amount,
        "amount_rub": amount_rub,
        "received_at": received_at,
        "reference": reference,
        "note": note,
    }


def aggregate_daily_cash_events(
    sales: Iterable[tuple[Any, Any]],
    receipts: Iterable[tuple[Any, Any]],
    expenses: Iterable[tuple[Any, Any]],
) -> list[dict[str, Any]]:
    """Aggregate RUB sales/receipts/costs by Moscow date; absent days omitted."""
    by_day: dict[date, dict[str, Decimal]] = {}
    for key, rows in (("sales", sales), ("receipts", receipts), ("expenses", expenses)):
        for moment, raw_amount in rows:
            if moment is None:
                continue
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            day = moment.astimezone(MOSCOW_TZ).date()
            data = by_day.setdefault(day, {"sales": Decimal("0"), "receipts": Decimal("0"), "expenses": Decimal("0")})
            data[key] += Decimal(str(raw_amount or 0))
    return [
        {"date": day, **by_day[day], "cash_delta": by_day[day]["receipts"] - by_day[day]["expenses"]}
        for day in sorted(by_day)
    ]


def validate_receipt_allocation(expense_rub: Any, receipt_rub: Any, already_allocated_rub: Any) -> Decimal:
    """Return remaining source balance or reject a negative/overdrawn allocation."""
    try:
        expense = Decimal(str(expense_rub))
        received = Decimal(str(receipt_rub))
        allocated = Decimal(str(already_allocated_rub or 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceInputError("Не удалось проверить остаток источника поступления.") from exc
    if any(not value.is_finite() for value in (expense, received, allocated)):
        raise FinanceInputError("Сумма распределения должна быть конечным числом.")
    available = received - allocated
    if expense <= 0 or available < 0 or expense > available:
        raise FinanceInputError(
            f"В источнике осталось {max(available, Decimal('0')):.2f} RUB; сумма расхода больше остатка."
        )
    return available - expense

"""Shared Moscow-time conversion for user-visible dates and local-day ranges."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


MOSCOW_TZ = ZoneInfo("Europe/Moscow")
UTC = timezone.utc


def as_moscow_datetime(value: Any) -> datetime | None:
    """Convert a datetime or ISO timestamp to Moscow time.

    Naive datetimes from SQLite are application timestamps stored as UTC, so
    they are interpreted as UTC rather than as the host's local timezone.
    """
    moment: datetime | None
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(MOSCOW_TZ)


def format_moscow_datetime(value: Any, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Format a timestamp in Moscow time; pass through unknown values safely."""
    if value is None:
        return ""
    moment = as_moscow_datetime(value)
    if moment is None:
        return str(value)
    return moment.strftime(fmt)


def format_moscow_date(value: Any, fmt: str = "%d.%m.%Y") -> str:
    """Format a date in Moscow time while keeping date-only values unchanged."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        moment = as_moscow_datetime(value)
        return moment.strftime(fmt) if moment else str(value)
    if isinstance(value, date):
        return value.strftime(fmt)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return text
        if "T" in text or " " in text:
            moment = as_moscow_datetime(text)
            return moment.strftime(fmt) if moment else value
        try:
            return date.fromisoformat(text).strftime(fmt)
        except ValueError:
            return value
    return str(value)


def moscow_isoformat(value: Any) -> str | None:
    """Serialize a timestamp with its explicit Moscow UTC offset."""
    if value is None:
        return None
    moment = as_moscow_datetime(value)
    return moment.isoformat() if moment else str(value)


def moscow_input_to_utc(value: Any) -> datetime | None:
    """Interpret a form timestamp as Moscow local time and return its UTC instant."""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=MOSCOW_TZ)
    return moment.astimezone(UTC)


def moscow_today() -> date:
    """Return the current calendar date in the shop's display timezone."""
    return datetime.now(MOSCOW_TZ).date()


def moscow_day_window(day: date | str) -> tuple[datetime, datetime]:
    """Return UTC bounds for one Moscow calendar day as a half-open interval."""
    local_day = date.fromisoformat(day) if isinstance(day, str) else day
    local_start = datetime.combine(local_day, time.min, tzinfo=MOSCOW_TZ)
    next_local_start = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=MOSCOW_TZ)
    return local_start.astimezone(UTC), next_local_start.astimezone(UTC)

"""Validation helpers for the web panel's bulk stock upload."""

from __future__ import annotations


class StockBulkError(ValueError):
    """A user-facing validation error for a bulk stock form."""


MAX_BULK_VALUES = 1_000

SEPARATORS: dict[str, str] = {
    "newline": "Новая строка",
    "dash": "Минус (-)",
    "semicolon": "Точка с запятой (;)",
    "pipe": "Вертикальная черта (|)",
}


def parse_bulk_values(raw: str, separator: str) -> list[str]:
    """Split and validate stock values submitted by an operator.

    The separator is deliberately a small allow-list instead of accepting an
    arbitrary character from the request.  This keeps the form predictable and
    prevents a control character from making the resulting lots ambiguous.
    Empty records are ignored, while surrounding whitespace is removed from
    each lot.  De-duplication against existing database rows is handled by
    ``add_values_bulk``; preserving duplicates here lets the result report tell
    the operator exactly how many entries were repeated in this upload.
    """
    text = str(raw or "")

    if separator not in SEPARATORS:
        raise StockBulkError("Выберите разделитель для аккаунтов.")

    parts = text.splitlines() if separator == "newline" else text.split(
        {"dash": "-", "semicolon": ";", "pipe": "|"}[separator]
    )
    values = [part.strip() for part in parts if part.strip()]
    if not values:
        raise StockBulkError("Добавьте хотя бы один аккаунт.")
    if len(values) > MAX_BULK_VALUES:
        raise StockBulkError(
            f"За одну загрузку можно добавить не больше {MAX_BULK_VALUES} аккаунтов."
        )
    return values

"""Parsing for the administrator's plain-text catalog importer."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


class CatalogImportError(ValueError):
    """A friendly validation error for a supplied catalog file."""


@dataclass(frozen=True)
class ImportedProduct:
    name: str
    price: Decimal
    description: str
    stock_quantity: int
    delivery_text: str | None


_FIELD_ALIASES = {
    "название": "name",
    "товар": "name",
    "name": "name",
    "title": "name",
    "цена": "price",
    "price": "price",
    "описание": "description",
    "description": "description",
    "количество": "stock_quantity",
    "остаток": "stock_quantity",
    "quantity": "stock_quantity",
    "stock": "stock_quantity",
    "текст выдачи": "delivery_text",
    "выдача": "delivery_text",
    "данные выдачи": "delivery_text",
    "delivery text": "delivery_text",
    "delivery": "delivery_text",
}

_FIELD_LINE = re.compile(r"^\s*([^:]{1,48}):\s*(.*)$")
_SEPARATOR = re.compile(r"(?m)^\s*-{3,}\s*$")


def _field_name(label: str) -> str | None:
    normalized = " ".join(label.lower().replace("ё", "е").split())
    return _FIELD_ALIASES.get(normalized)


def _parse_block(block: str, number: int) -> ImportedProduct:
    fields: dict[str, list[str]] = {}
    current_field: str | None = None

    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        match = _FIELD_LINE.match(line)
        field = _field_name(match.group(1)) if match else None
        if field:
            current_field = field
            fields.setdefault(field, []).append(match.group(2).strip())
        elif current_field:
            fields[current_field].append(line)
        elif line.strip():
            raise CatalogImportError(
                f"Блок {number}: строка «{line[:48]}» должна начинаться с названия поля."
            )

    def value(name: str) -> str:
        return "\n".join(fields.get(name, [])).strip()

    name = value("name")
    description = value("description")
    raw_price = value("price").replace(" ", "").replace(",", ".")
    raw_quantity = value("stock_quantity") or "0"
    delivery_text = value("delivery_text") or None

    if not name:
        raise CatalogImportError(f"Блок {number}: укажите «Название».")
    if len(name) > 100:
        raise CatalogImportError(f"Блок {number}: название длиннее 100 символов.")
    if not description:
        raise CatalogImportError(f"Блок {number}: укажите «Описание».")
    try:
        price = Decimal(raw_price)
    except InvalidOperation as exc:
        raise CatalogImportError(f"Блок {number}: цена должна быть числом.") from exc
    if not price.is_finite():
        raise CatalogImportError(f"Блок {number}: цена должна быть конечным числом.")
    if price <= 0:
        raise CatalogImportError(f"Блок {number}: цена должна быть положительной.")
    if price > 10000000:
        raise CatalogImportError(f"Блок {number}: цена больше 10 000 000 — проверьте число.")
    try:
        stock_quantity = int(raw_quantity)
    except ValueError as exc:
        raise CatalogImportError(f"Блок {number}: количество должно быть целым числом.") from exc
    if stock_quantity < 0:
        raise CatalogImportError(f"Блок {number}: количество не может быть отрицательным.")
    if stock_quantity and not delivery_text:
        raise CatalogImportError(
            f"Блок {number}: для товара с количеством добавьте «Текст выдачи»."
        )

    return ImportedProduct(
        name=name,
        price=price,
        description=description,
        stock_quantity=stock_quantity,
        delivery_text=delivery_text,
    )


def parse_catalog_import(text: str) -> list[ImportedProduct]:
    """Parse UTF-8 text where product blocks are separated by ``---``."""
    source = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not source:
        raise CatalogImportError("Файл пустой.")

    blocks = [part.strip() for part in _SEPARATOR.split(source) if part.strip()]
    if not blocks:
        raise CatalogImportError("В файле нет товаров.")
    if len(blocks) > 200:
        raise CatalogImportError("За один импорт можно добавить не больше 200 товаров.")

    products = [_parse_block(block, index) for index, block in enumerate(blocks, start=1)]
    names = [product.name.casefold() for product in products]
    if len(names) != len(set(names)):
        raise CatalogImportError("В одном файле названия товаров не должны повторяться.")
    return products

from decimal import Decimal

import pytest

from bot.web.catalog_import import CatalogImportError, parse_catalog_import


def test_import_parser_recognizes_russian_fields_and_multiline_delivery_text():
    products = parse_catalog_import(
        """Название: ChatGPT Plus
Цена: 90,50
Описание: Подписка на месяц.
Количество: 3
Текст выдачи:
Логин: buyer@example.com
Пароль: safe-password
---
Название: Gemini Pro
Цена: 120
Описание: Подписка на месяц.
Количество: 0
"""
    )

    assert len(products) == 2
    assert products[0].name == "ChatGPT Plus"
    assert products[0].price == Decimal("90.50")
    assert products[0].stock_quantity == 3
    assert products[0].delivery_text == "Логин: buyer@example.com\nПароль: safe-password"
    assert products[1].stock_quantity == 0
    assert products[1].delivery_text is None


@pytest.mark.parametrize(
    "source, message",
    [
        ("Название: Без цены\nОписание: Текст", "цена"),
        ("Название: Ноль\nЦена: 0\nОписание: Текст", "положительной"),
        ("Название: NaN\nЦена: NaN\nОписание: Текст", "конечным числом"),
        ("Название: Без выдачи\nЦена: 90\nОписание: Текст\nКоличество: 1", "Текст выдачи"),
        ("Название: Меньше нуля\nЦена: 90\nОписание: Текст\nКоличество: -1", "не может быть отрицательным"),
    ],
)
def test_import_parser_rejects_incomplete_counted_products(source, message):
    with pytest.raises(CatalogImportError, match=message):
        parse_catalog_import(source)

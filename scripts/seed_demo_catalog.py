"""Idempotently load the catalog reconstructed from the supplied screenshots.

The stock values are intentionally fake and visibly marked DEMO. The command
refuses to run in production unless --allow-production is explicitly supplied.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal

from sqlalchemy import select

from bot.database import Database
from bot.database.models import Categories, Goods, ItemValues


CATEGORIES = [
    ("✨ Gemini", 10, "categories/gemini.jpg"),
    ("🧠 ChatGPT", 20, "categories/chatgpt.jpg"),
    ("◉ Grok", 30, "categories/grok.jpg"),
    ("✳️ Perplexity", 40, "categories/perplexity.jpg"),
    ("🎬 CapCut", 50, "categories/capcut.jpg"),
    ("◉ Opencode", 60, "categories/opencode.jpg"),
]


CHATGPT_PRODUCTS = [
    {
        "name": "ChatGPT Plus 1 Месяц (24ч)",
        "price": "529.00",
        "sort_order": 10,
        "availability_note": "нет в наличии ❌",
        "description": (
            "Подписка ChatGPT Plus на 1 месяц. Срок выполнения — до 24 часов.\n\n"
            "Тестовая карточка: перед запуском укажите реальный способ законной "
            "активации, гарантию и условия возврата."
        ),
    },
    {
        "name": "ChatGPT PLUS 1 Месяц (NW)",
        "price": "410.00",
        "sort_order": 20,
        "availability_note": "нет в наличии ❌",
        "description": (
            "Подписка ChatGPT Plus на 1 месяц.\n\n"
            "Тестовая карточка без реальных учётных данных."
        ),
    },
    {
        "name": "ChatGPT 1 Месяц (FW) Официальная покупка CDK",
        "price": "1719.00",
        "sort_order": 30,
        "description": (
            "Официальная активация на аккаунте покупателя. После оплаты бот "
            "автоматически выдаёт тестовый код и инструкцию."
        ),
        "stock": [
            "DEMO-CDK-0001\nЭто тестовый товар, реальной ценности не имеет.",
            "DEMO-CDK-0002\nЭто тестовый товар, реальной ценности не имеет.",
            "DEMO-CDK-0003\nЭто тестовый товар, реальной ценности не имеет.",
        ],
    },
    {
        "name": "ChatGPT PLUS 1M (NW) - PIX (GMAIL)",
        "price": "410.00",
        "sort_order": 40,
        "availability_note": "нет в наличии ❌",
        "description": "Тестовая карточка позиции. Реальные данные не загружены.",
    },
    {
        "name": "ChatGPT K12 до 2028г (NW)",
        "price": "389.00",
        "sort_order": 50,
        "availability_note": "предзаказ ⏳",
        "description": (
            "Предзаказ. Покупка отключена до появления остатка. "
            "Пользователь может подписаться на уведомление о поступлении."
        ),
    },
    {
        "name": "ChatGPT Plus 1M (Momo) Gmail (NW)",
        "price": "409.00",
        "sort_order": 60,
        "availability_note": "нет в наличии ❌",
        "description": "Тестовая карточка позиции. Реальные данные не загружены.",
    },
]

GROK_PRODUCTS = [
    {
        "name": "Super Grok 7D",
        "price": "315.00",
        "sort_order": 10,
        "description": (
            "Доступ к Super Grok на 7 дней. Тестовая позиция, восстановленная "
            "по истории заказов на предоставленном скриншоте."
        ),
        "stock": [
            "DEMO-GROK-0001\nЭто тестовый товар, реальной ценности не имеет.",
            "DEMO-GROK-0002\nЭто тестовый товар, реальной ценности не имеет.",
        ],
    },
]


async def seed() -> tuple[int, int, int]:
    categories_created = products_created = stock_created = 0
    async with Database().session() as session:
        category_rows: dict[str, Categories] = {}
        for name, sort_order, image_ref in CATEGORIES:
            category = (await session.execute(
                select(Categories).where(Categories.name == name)
            )).scalars().one_or_none()
            if category is None:
                category = Categories(name=name)
                session.add(category)
                await session.flush()
                categories_created += 1
            category.sort_order = sort_order
            category.is_active = True
            category.image_ref = image_ref
            category_rows[name] = category

        product_groups = [
            ("🧠 ChatGPT", "chatgpt", CHATGPT_PRODUCTS),
            ("◉ Grok", "grok", GROK_PRODUCTS),
        ]
        for category_name, image_prefix, products in product_groups:
            category = category_rows[category_name]
            for spec in products:
                product = (await session.execute(
                    select(Goods).where(Goods.name == spec["name"])
                )).scalars().one_or_none()
                if product is None:
                    product = Goods(name=spec["name"], category_id=category.id)
                    session.add(product)
                    await session.flush()
                    products_created += 1
                product.category_id = category.id
                product.price = Decimal(spec["price"])
                product.description = spec["description"]
                product.sort_order = spec["sort_order"]
                product.is_active = True
                product.availability_note = spec.get("availability_note")
                product.image_ref = f"products/{image_prefix}-{spec['sort_order']}.jpg"

                for value in spec.get("stock", []):
                    exists = (await session.execute(
                        select(ItemValues.id).where(
                            ItemValues.item_id == product.id,
                            ItemValues.value == value,
                        )
                    )).scalar()
                    if not exists:
                        session.add(ItemValues(
                            item_id=product.id,
                            value=value,
                            is_infinity=False,
                        ))
                        stock_created += 1

    return categories_created, products_created, stock_created


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-production", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    environment = os.getenv("APP_ENV", "development").lower()
    if environment == "production" and not args.allow_production:
        raise SystemExit(
            "Refusing to seed demo data in APP_ENV=production. "
            "Use --allow-production only after verifying every DEMO value."
        )
    created = asyncio.run(seed())
    print(
        f"Demo catalog ready: categories +{created[0]}, "
        f"products +{created[1]}, stock +{created[2]}"
    )

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select as sa_select

from bot.misc.services.catalog_notifications import (
    announce_catalog_arrival,
    notify_owner_stock_added,
)


class TestCatalogNotifications:
    def test_stock_list_includes_product_name_column(self):
        from bot.web.admin import ItemValuesAdmin

        view = ItemValuesAdmin()

        assert view.get_list_columns() == [
            "id",
            "item",
            "item_id",
            "value",
            "is_infinity",
        ]
        assert view._column_labels["item"] == "Тип товара"
        assert view._column_labels["item_id"] == "ID товара"

    async def test_arrival_announcement_posts_to_channel_with_product_button(
            self, mock_bot
    ):
        mock_bot.get_me = AsyncMock(return_value=SimpleNamespace(username="MyStoreRobot"))

        with patch("bot.misc.services.catalog_notifications.EnvKeys") as env, \
             patch("bot.misc.services.catalog_notifications.select_item_values_amount",
                   new=AsyncMock(return_value=24)):
            env.CHANNEL_ID = "-100123"
            env.PAY_CURRENCY = "RUB"
            posted = await announce_catalog_arrival(
                mock_bot,
                item_name="ChatGPT Plus",
                price=345,
                count=3,
                item_id=42,
            )

        assert posted is True
        mock_bot.send_message.assert_awaited_once()
        call = mock_bot.send_message.await_args.kwargs
        assert call["chat_id"] == -100123
        assert "📦" in call["text"] and "Пополнение" in call["text"]
        assert "ChatGPT Plus" in call["text"]
        assert "Завезли: 3 шт." in call["text"]
        assert "На складе: 24 шт." in call["text"]
        assert "345 ₽" in call["text"]
        button = call["reply_markup"].inline_keyboard[0][0]
        assert button.url == "https://t.me/MyStoreRobot?start=item_42"

    async def test_arrival_announcement_skipped_without_channel(self, mock_bot):
        with patch("bot.misc.services.catalog_notifications.EnvKeys") as env:
            env.CHANNEL_ID = ""
            posted = await announce_catalog_arrival(
                mock_bot,
                item_name="ChatGPT Plus",
                price=90,
                count=3,
                item_id=42,
            )

        assert posted is False
        mock_bot.send_message.assert_not_awaited()

    async def test_stock_view_resolves_name_of_stocked_lot(self):
        """Regression: stocking lot B must never announce lot A's name."""
        from types import SimpleNamespace
        from decimal import Decimal as D
        from bot.database.main import Database
        from bot.database.models.main import Categories, Goods
        from bot.web.admin import ItemValuesAdmin

        async with Database().session() as s:
            s.add(Categories(name="LotCat"))
            await s.flush()
            cat_id = (await s.execute(
                sa_select(Categories.id).where(Categories.name == "LotCat")
            )).scalar_one()
            s.add(Goods(name="Lot Alpha K12", price=D("100"), description="a", category_id=cat_id))
            s.add(Goods(name="Lot Beta CDK", price=D("200"), description="b", category_id=cat_id))
            await s.flush()
            alpha_id = (await s.execute(
                sa_select(Goods.id).where(Goods.name == "Lot Alpha K12")
            )).scalar_one()
            beta_id = (await s.execute(
                sa_select(Goods.id).where(Goods.name == "Lot Beta CDK")
            )).scalar_one()

        view = ItemValuesAdmin.__new__(ItemValuesAdmin)
        assert await view._item_name(SimpleNamespace(item_id=beta_id)) == "Lot Beta CDK"
        data = await view._item_notification_data(SimpleNamespace(item_id=beta_id))
        assert data[0] == beta_id
        assert data[1] == "Lot Beta CDK"
        assert await view._item_name(SimpleNamespace(item_id=alpha_id)) == "Lot Alpha K12"

    async def test_arrival_announcement_infinity_has_no_units(self, mock_bot):
        mock_bot.get_me = AsyncMock(return_value=SimpleNamespace(username="MyStoreRobot"))

        with patch("bot.misc.services.catalog_notifications.EnvKeys") as env:
            env.CHANNEL_ID = "-100123"
            env.PAY_CURRENCY = "RUB"
            posted = await announce_catalog_arrival(
                mock_bot,
                item_name="ChatGPT Plus",
                price=90,
                count=1,
                item_id=42,
                is_infinity=True,
            )

        assert posted is True
        text = mock_bot.send_message.await_args.kwargs["text"]
        assert "На складе: ∞" in text
        assert "шт." not in text

    async def test_owner_receives_purchase_deep_link_when_item_id_is_known(self, mock_bot):
        mock_bot.get_me = AsyncMock(return_value=SimpleNamespace(username="MyStoreRobot"))

        delivered = await notify_owner_stock_added(
            mock_bot,
            item_name="ChatGPT Plus",
            price=90,
            count=3,
            item_id=42,
        )

        assert delivered is True
        call = mock_bot.send_message.await_args.kwargs
        assert 'https://t.me/MyStoreRobot?start=item_42' not in call["text"]
        button = call["reply_markup"].inline_keyboard[0][0]
        assert button.url == 'https://t.me/MyStoreRobot?start=item_42'
        assert "Перейти к товару" in button.text

    async def test_owner_receives_product_price_and_quantity(self, mock_bot):
        delivered = await notify_owner_stock_added(
            mock_bot,
            item_name="ChatGPT <Plus>",
            price=Decimal("529.00"),
            count=12,
        )

        assert delivered is True
        mock_bot.send_message.assert_awaited_once()
        call = mock_bot.send_message.await_args
        assert call.kwargs["chat_id"] == 999999
        assert call.kwargs["parse_mode"] == "HTML"
        assert "ChatGPT &lt;Plus&gt;" in call.kwargs["text"]
        assert "529 RUB" in call.kwargs["text"]
        assert "12" in call.kwargs["text"]

    async def test_delivery_failure_does_not_break_catalog_change(self, mock_bot):
        mock_bot.send_message = AsyncMock(side_effect=RuntimeError("temporary Telegram outage"))

        delivered = await notify_owner_stock_added(
            mock_bot,
            item_name="Product",
            price=100,
            count=1,
        )

        assert delivered is False

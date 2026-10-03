from decimal import Decimal
from unittest.mock import patch

from bot.database.methods.pricing import (
    apply_bulk_discount,
    apply_promo_on_total,
    bulk_settings,
)


class TestBulkPricing:

    def test_threshold_and_percent(self):
        with patch("bot.misc.EnvKeys") as env:
            env.BULK_MIN_QTY = 30
            env.BULK_DISCOUNT_PERCENT = "10"
            assert bulk_settings() == (30, Decimal("10"))

    def test_below_threshold_no_discount(self):
        with patch("bot.misc.EnvKeys") as env:
            env.BULK_MIN_QTY = 30
            env.BULK_DISCOUNT_PERCENT = "10"
            total, pct = apply_bulk_discount(Decimal("1000"), 29)
            assert (total, pct) == (Decimal("1000.00"), Decimal(0))

    def test_at_threshold_discount_applies(self):
        with patch("bot.misc.EnvKeys") as env:
            env.BULK_MIN_QTY = 30
            env.BULK_DISCOUNT_PERCENT = "10"
            total, pct = apply_bulk_discount(Decimal("1000"), 30)
            assert total == Decimal("900.00")
            assert pct == Decimal("10")

    def test_disabled_rule(self):
        with patch("bot.misc.EnvKeys") as env:
            env.BULK_MIN_QTY = 0
            env.BULK_DISCOUNT_PERCENT = "10"
            assert apply_bulk_discount(Decimal("100"), 99) == (Decimal("100.00"), Decimal(0))

    def test_promo_stacks_on_bulked_total(self):
        assert apply_promo_on_total(Decimal("900.00"), "percent", 10) == Decimal("810.00")
        assert apply_promo_on_total(Decimal("900.00"), "fixed", 100) == Decimal("800.00")
        assert apply_promo_on_total(Decimal("50.00"), "fixed", 999) == Decimal("0.00")


class TestBulkPurchase:

    async def test_purchase_screen_shows_available_bulk_discount(self, user_factory, item_factory,
                                                                  make_callback_query, fsm_context):
        from unittest.mock import AsyncMock, patch

        from bot.handlers.user.balance_and_payment import _render_purchase_choice

        await user_factory(telegram_id=810000, balance=100000)
        await item_factory(
            name="BulkChoice",
            price=100,
            values=[(f"value-{index}", False) for index in range(40)],
        )
        await fsm_context.update_data(csrf_item="BulkChoice")
        call = make_callback_query(user_id=810000)

        with patch("bot.misc.EnvKeys", BULK_MIN_QTY=30, BULK_DISCOUNT_PERCENT="10"), \
                patch(
                    "bot.handlers.user.balance_and_payment.edit_screen",
                    new_callable=AsyncMock,
                ) as render:
            await _render_purchase_choice(call, fsm_context)

        rendered_text = render.await_args.args[1]
        assert "shop.purchase.bulk_available" in rendered_text
        assert "shop.purchase.bulk:{" not in rendered_text

    async def test_quote_applies_bulk_over_threshold(self, user_factory, item_factory):
        from bot.handlers.user.balance_and_payment import _item_purchase_quote

        await user_factory(telegram_id=810001, balance=100000)
        await item_factory(name="BulkItem", price=100, values=[(f"v{i}", False) for i in range(40)])
        with patch("bot.misc.EnvKeys", BULK_MIN_QTY=30, BULK_DISCOUNT_PERCENT="10"):
            quote = await _item_purchase_quote(810001, "BulkItem", 30)
        assert quote["total_price"] == Decimal("2700.00")
        assert quote["bulk_pct"] == Decimal("10")

    async def test_quote_no_bulk_below_threshold(self, user_factory, item_factory):
        from bot.handlers.user.balance_and_payment import _item_purchase_quote

        await user_factory(telegram_id=810002, balance=100000)
        await item_factory(name="BulkItem2", price=100, values=[(f"v{i}", False) for i in range(40)])
        with patch("bot.misc.EnvKeys", BULK_MIN_QTY=30, BULK_DISCOUNT_PERCENT="10"):
            quote = await _item_purchase_quote(810002, "BulkItem2", 5)
        assert quote["total_price"] == Decimal("500.00")
        assert quote["bulk_pct"] == Decimal(0)

    async def test_transaction_charges_bulked_total(self, user_factory, item_factory):
        from bot.database.methods.read import check_user
        from bot.database.methods.transactions import buy_item_transaction

        await user_factory(telegram_id=810003, balance=5000)
        await item_factory(name="BulkItem3", price=100, values=[(f"v{i}", False) for i in range(40)])
        with patch("bot.misc.EnvKeys", BULK_MIN_QTY=30, BULK_DISCOUNT_PERCENT="10"):
            ok, msg, data = await buy_item_transaction(810003, "BulkItem3", quantity=30)
        assert ok, msg
        user = await check_user(810003)
        assert user["balance"] == Decimal("5000") - Decimal("2700.00")

    async def test_transaction_without_bulk_unchanged(self, user_factory, item_factory):
        from bot.database.methods.read import check_user
        from bot.database.methods.transactions import buy_item_transaction

        await user_factory(telegram_id=810004, balance=5000)
        await item_factory(name="BulkItem4", price=100, values=[(f"v{i}", False) for i in range(40)])
        with patch("bot.misc.EnvKeys", BULK_MIN_QTY=30, BULK_DISCOUNT_PERCENT="10"):
            ok, msg, _data = await buy_item_transaction(810004, "BulkItem4", quantity=2)
        assert ok, msg
        user = await check_user(810004)
        assert user["balance"] == Decimal("4800.00")

    async def test_expected_total_catches_bulk_drift(self, user_factory, item_factory):
        from bot.database.methods.transactions import buy_item_transaction

        await user_factory(telegram_id=810005, balance=5000)
        await item_factory(name="BulkItem5", price=100, values=[(f"v{i}", False) for i in range(40)])
        with patch("bot.misc.EnvKeys", BULK_MIN_QTY=30, BULK_DISCOUNT_PERCENT="10"):
            ok, msg, _data = await buy_item_transaction(
                810005, "BulkItem5", quantity=30, expected_total=Decimal("3000.00")
            )
        assert (ok, msg) == (False, "price_changed")


class TestBulkButton:

    def test_bulk_button_is_not_shown_on_product_card(self):
        from bot.keyboards.inline import item_info

        markup = item_info("back_to_menu", bulk=(30, Decimal("10")))
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "buy_bulk" not in callbacks
        texts = [b.text for row in markup.inline_keyboard for b in row]
        assert not any("30" in t or "Оптом" in t or "Wholesale" in t for t in texts)

    def test_button_hidden_without_bulk(self):
        from bot.keyboards.inline import item_info

        markup = item_info("back_to_menu")
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "buy_bulk" not in callbacks


class TestBulkCart:

    async def test_cart_line_and_checkout_apply_bulk(self, user_factory, item_factory):
        from bot.database.methods.create import add_to_cart
        from bot.database.methods.read import check_user
        from bot.database.methods.transactions import checkout_cart_transaction
        from bot.handlers.user.cart import _cart_view_data

        await user_factory(telegram_id=810006, balance=10000)
        await item_factory(name="BulkCart", price=100,
                           values=[(f"c{i}", False) for i in range(40)])
        ok, msg = await add_to_cart(810006, "BulkCart", quantity=30)
        assert ok, msg
        with patch("bot.misc.EnvKeys", BULK_MIN_QTY=30, BULK_DISCOUNT_PERCENT="10"):
            items, _info, line_data, total = await _cart_view_data(810006)
            assert total == Decimal("2700.00")
            success, msg, _results = await checkout_cart_transaction(810006)
        assert success, msg
        user = await check_user(810006)
        assert user["balance"] == Decimal("10000") - Decimal("2700.00")

    async def test_cart_without_bulk_unchanged(self, user_factory, item_factory):
        from bot.database.methods.create import add_to_cart
        from bot.database.methods.read import check_user
        from bot.database.methods.transactions import checkout_cart_transaction
        from bot.handlers.user.cart import _cart_view_data

        await user_factory(telegram_id=810007, balance=10000)
        await item_factory(name="BulkCart2", price=100,
                           values=[(f"d{i}", False) for i in range(40)])
        ok, msg = await add_to_cart(810007, "BulkCart2", quantity=3)
        assert ok, msg
        with patch("bot.misc.EnvKeys", BULK_MIN_QTY=30, BULK_DISCOUNT_PERCENT="10"):
            _items, _info, _ld, total = await _cart_view_data(810007)
            assert total == Decimal("300.00")
            success, msg, _results = await checkout_cart_transaction(810007)
        assert success, msg
        user = await check_user(810007)
        assert user["balance"] == Decimal("9700.00")

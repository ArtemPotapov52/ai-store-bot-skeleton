import pytest

from bot.keyboards.inline import (
    main_menu, profile_keyboard, simple_buttons, back, close, item_info, payment_menu,
    get_payment_choice, question_buttons, check_sub, referral_system_keyboard,
    admin_console_keyboard, cart_keyboard, rating_keyboard,
)
from bot.keyboards.icons import BUTTON_CUSTOM_EMOJI_IDS
from bot.database.models import Permission


def _all_callback_data(markup):
    """Extract all callback_data values from markup."""
    result = []
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data:
                result.append(btn.callback_data)
    return result


def _all_button_texts(markup):
    """Extract all button texts from markup."""
    result = []
    for row in markup.inline_keyboard:
        for btn in row:
            result.append(btn.text)
    return result


def _has_url_button(markup):
    """Check if any button has a URL."""
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.url:
                return True
    return False


class TestMainMenu:

    @pytest.mark.parametrize("callback", ["shop", "rules", "profile"])
    def test_basic_buttons_present(self, callback):
        assert callback in _all_callback_data(main_menu(role=1))

    @pytest.mark.parametrize("role", [1, 2])
    def test_customer_main_menu_hides_console_for_every_role(self, role):
        assert "console" not in _all_callback_data(main_menu(role=role))

    @pytest.mark.parametrize("kwargs,expected", [
        ({"channel": "test_channel"}, False),  # channel button removed from menu
        ({"helper": "12345"}, True),
        ({}, False),
    ])
    def test_url_buttons(self, kwargs, expected):
        assert _has_url_button(main_menu(role=1, **kwargs)) is expected

    @pytest.mark.parametrize("callback", ["language", "faq", "support"])
    def test_storefront_buttons_present(self, callback):
        assert callback in _all_callback_data(main_menu(role=1))

    def test_support_username_builds_prefilled_link(self):
        markup = main_menu(role=1, support_username="your_username")
        urls = [btn.url for row in markup.inline_keyboard for btn in row if btn.url]
        assert len(urls) == 1
        assert urls[0].startswith("https://t.me/your_username?text=")
        assert "support" not in _all_callback_data(markup)

    def test_explicit_support_url_wins_over_username(self):
        markup = main_menu(
            role=1,
            support_url="https://t.me/some_channel",
            support_username="your_username",
        )
        urls = [btn.url for row in markup.inline_keyboard for btn in row if btn.url]
        assert urls == ["https://t.me/some_channel"]

    def test_main_menu_uses_requested_premium_icons(self):
        markup = main_menu(role=1, support_url="https://t.me/some_channel")
        buttons = {
            button.callback_data or "support": button
            for row in markup.inline_keyboard
            for button in row
        }
        assert buttons["shop"].icon_custom_emoji_id == BUTTON_CUSTOM_EMOJI_IDS["shop"]
        assert buttons["profile"].icon_custom_emoji_id == BUTTON_CUSTOM_EMOJI_IDS["profile"]
        assert buttons["support"].icon_custom_emoji_id == BUTTON_CUSTOM_EMOJI_IDS["support"]
        assert buttons["language"].icon_custom_emoji_id == BUTTON_CUSTOM_EMOJI_IDS["language"]
        assert buttons["rules"].icon_custom_emoji_id == BUTTON_CUSTOM_EMOJI_IDS["rules"]
        assert buttons["faq"].icon_custom_emoji_id == BUTTON_CUSTOM_EMOJI_IDS["faq"]
        assert [
            buttons["shop"].text,
            buttons["profile"].text,
            buttons["support"].text,
            buttons["language"].text,
            buttons["rules"].text,
            buttons["faq"].text,
        ] == ["Товары", "Профиль", "Поддержка", "Language / Язык", "Правила / Соглашение", "FAQ"]


class TestProfileKeyboard:

    @pytest.mark.parametrize("kwargs,callback,expected", [
        ({"referral_percent": 0, "user_items": 0}, "replenish_balance", True),
        ({"referral_percent": 0}, "back_to_menu", True),
        ({"referral_percent": 10}, "referral_system", True),
        ({"referral_percent": 0}, "referral_system", False),
        ({"referral_percent": 0, "user_items": 5}, "bought_items", True),
        # Order history remains reachable even when it is empty.
        ({"referral_percent": 0, "user_items": 0}, "bought_items", True),
    ])
    def test_conditional_buttons(self, kwargs, callback, expected):
        assert (callback in _all_callback_data(profile_keyboard(**kwargs))) is expected

    def test_profile_buttons_use_requested_premium_icons(self):
        markup = profile_keyboard(referral_percent=10, cart_count=2)
        buttons = {
            button.callback_data: button
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }
        for callback in (
            "replenish_balance",
            "bought_items",
            "referral_system",
            "agreement",
            "cart",
            "redeem_promo",
            "back_to_menu",
        ):
            assert buttons[callback].icon_custom_emoji_id == BUTTON_CUSTOM_EMOJI_IDS[callback]
        assert buttons["replenish_balance"].text == "Пополнить баланс"
        assert buttons["bought_items"].text == "Заказы"
        assert buttons["agreement"].text == "Соглашения"
        assert buttons["cart"].text == "Корзина (2)"
        assert buttons["redeem_promo"].text == "Активировать промокод"
        assert buttons["back_to_menu"].text == "Главное меню"


class TestPaymentMenu:

    def test_payment_menu_has_pay_url(self):
        markup = payment_menu("https://example.com/pay")
        has_url = False
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.url == "https://example.com/pay":
                    has_url = True
        assert has_url

    def test_payment_menu_has_check(self):
        markup = payment_menu("https://example.com/pay")
        cbs = _all_callback_data(markup)
        assert "check" in cbs


class TestItemInfoKeyboard:

    @pytest.mark.parametrize("callback", ["buy_item", "gp_0"])
    def test_has_buy_and_back(self, callback):
        assert callback in _all_callback_data(item_info("gp_0"))

    @pytest.mark.parametrize("kwargs,expected_sub,expected_unsub", [
        ({}, False, False),                                   # in stock: no notify button
        ({"out_of_stock": True}, True, False),                # offer to subscribe
        ({"out_of_stock": True, "subscribed": True}, False, True),  # offer to unsubscribe
    ])
    def test_restock_notify_button(self, kwargs, expected_sub, expected_unsub):
        cbs = _all_callback_data(item_info("gp_0", **kwargs))
        assert ("sub_stock" in cbs) is expected_sub
        assert ("unsub_stock" in cbs) is expected_unsub

    def test_out_of_stock_product_cannot_be_bought(self):
        cbs = _all_callback_data(item_info("gp_0", out_of_stock=True))
        assert "buy_item" not in cbs
        assert "add_to_cart" not in cbs

    def test_review_buttons_carry_no_item_name(self):
        """Telegram caps callback_data at 64 bytes; a 100-char Cyrillic product
        name embedded in it made the whole card unopenable."""
        cbs = _all_callback_data(item_info("gp_0", review_count=3, has_purchased=True))
        assert "reviews:0" in cbs
        assert "review" in cbs
        assert all(len(cb.encode("utf-8")) <= 64 for cb in cbs)


class TestCartKeyboard:

    def _items(self):
        return [{"id": 7, "item_name": "Widget", "quantity": 3}]

    def test_has_quantity_stepper(self):
        cbs = _all_callback_data(cart_keyboard(self._items()))
        assert "cart_qty:7:1" in cbs
        assert "cart_qty:7:-1" in cbs

    def test_has_remove_checkout_and_clear(self):
        cbs = _all_callback_data(cart_keyboard(self._items()))
        assert "cart_remove:7" in cbs
        assert "cart_checkout" in cbs
        assert "cart_clear" in cbs

    def test_shows_quantity_in_label(self):
        markup = cart_keyboard(self._items())
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert any("×3" in t for t in labels)


class TestLazyPaginatedExtraRows:

    async def test_catalog_back_button_has_no_unicode_prefix(self):
        from bot.keyboards.inline import lazy_paginated_keyboard
        from bot.misc import LazyPaginator

        async def _query(offset=0, limit=10, count_only=False):
            return 1 if count_only else ["OnlyCat"]

        markup = await lazy_paginated_keyboard(
            paginator=LazyPaginator(_query, per_page=10),
            item_text=lambda c: c,
            item_callback=lambda c: "cat:0:0",
            back_cb="categories-page_0",
        )
        back_button = markup.inline_keyboard[-1][0]
        assert back_button.text == "Назад"
        assert back_button.icon_custom_emoji_id == "5258336354642697821"

    async def test_item_style_is_preserved(self):
        from bot.keyboards.inline import lazy_paginated_keyboard
        from bot.misc import LazyPaginator

        async def _query(offset=0, limit=10, count_only=False):
            return 2 if count_only else ["AI category", "Sold out item"]

        markup = await lazy_paginated_keyboard(
            paginator=LazyPaginator(_query, per_page=10),
            item_text=lambda value: value,
            item_callback=lambda value: value,
            item_style=lambda value: "primary" if value == "AI category" else "danger",
        )

        buttons = [button for row in markup.inline_keyboard for button in row]
        assert [button.style for button in buttons] == ["primary", "danger"]

    async def test_extra_row_is_rendered(self):
        from aiogram.types import InlineKeyboardButton
        from bot.keyboards.inline import lazy_paginated_keyboard
        from bot.misc import LazyPaginator

        async def _query(offset=0, limit=10, count_only=False):
            return 1 if count_only else ["OnlyCat"]

        markup = await lazy_paginated_keyboard(
            paginator=LazyPaginator(_query, per_page=10),
            item_text=lambda c: c,
            item_callback=lambda c: f"cat:0:0",
            page=0,
            back_cb="back_to_menu",
            nav_cb_prefix="categories-page_",
            extra_rows=[[InlineKeyboardButton(text="🔍", callback_data="shop_search")]],
        )
        assert "shop_search" in _all_callback_data(markup)

    async def test_without_extra_rows_output_is_unchanged(self):
        """Existing call sites keep the same callbacks without opting into styles."""
        from bot.keyboards.inline import lazy_paginated_keyboard
        from bot.misc import LazyPaginator

        async def _query(offset=0, limit=10, count_only=False):
            return 1 if count_only else ["OnlyCat"]

        def _kb():
            return lazy_paginated_keyboard(
                paginator=LazyPaginator(_query, per_page=10),
                item_text=lambda c: c,
                item_callback=lambda c: "cat:0:0",
                page=0,
                back_cb="back_to_menu",
                nav_cb_prefix="categories-page_",
            )

        markup = await _kb()
        assert _all_callback_data(markup) == ["cat:0:0", "back_to_menu"]


class TestSimpleButtons:

    def test_creates_buttons(self):
        markup = simple_buttons([("A", "a"), ("B", "b")])
        cbs = _all_callback_data(markup)
        assert "a" in cbs
        assert "b" in cbs

    def test_button_count(self):
        markup = simple_buttons([("A", "a"), ("B", "b"), ("C", "c")])
        total = sum(len(row) for row in markup.inline_keyboard)
        assert total == 3


class TestBackAndClose:

    @pytest.mark.parametrize("args,expected", [
        ((), "menu"),           # default target
        (("profile",), "profile"),
    ])
    def test_back(self, args, expected):
        assert expected in _all_callback_data(back(*args))

    def test_close_button(self):
        assert "close" in _all_callback_data(close())


class TestReferralSystemKeyboard:

    @pytest.mark.parametrize("has_referrals,has_earnings", [
        (False, False),
        (True, False),
        (False, True),
        (True, True),
    ])
    def test_buttons_follow_available_data(self, has_referrals, has_earnings):
        cbs = _all_callback_data(
            referral_system_keyboard(has_referrals=has_referrals, has_earnings=has_earnings)
        )
        assert ("view_referrals" in cbs) is has_referrals
        assert ("view_all_earnings" in cbs) is has_earnings
        assert "profile" in cbs  # back button is always there


class TestGetPaymentChoice:

    def test_has_all_methods(self):
        markup = get_payment_choice()
        cbs = _all_callback_data(markup)
        assert "pay_cryptopay" in cbs
        assert "pay_sbp_card" in cbs
        assert "pay_admin" in cbs
        assert "pay_stars" in cbs
        assert "pay_fiat" in cbs
        assert "replenish_balance" in cbs  # back
        assert cbs[0] == "pay_sbp_card"
        assert markup.inline_keyboard[0][0].text.isupper()
        assert markup.inline_keyboard[0][0].text == "🏦 СБП/КАРТА"


class TestQuestionButtons:

    def test_has_yes_no_back(self):
        markup = question_buttons("confirm_delete", "shop")
        cbs = _all_callback_data(markup)
        assert "confirm_delete_yes" in cbs
        assert "confirm_delete_no" in cbs
        assert "shop" in cbs


class TestCheckSub:

    def test_has_channel_url(self):
        markup = check_sub("test_channel")
        has_url = False
        for row in markup.inline_keyboard:
            for btn in row:
                if btn.url and "test_channel" in btn.url:
                    has_url = True
        assert has_url

    def test_has_check_callback(self):
        markup = check_sub("test_channel")
        cbs = _all_callback_data(markup)
        assert "sub_channel_done" in cbs


class TestAdminConsoleKeyboard:

    def test_has_roles_button(self):
        markup = admin_console_keyboard()
        cbs = _all_callback_data(markup)
        assert "role_mgmt" in cbs

    def test_has_all_admin_buttons(self):
        markup = admin_console_keyboard()
        cbs = _all_callback_data(markup)
        assert "shop_management" in cbs
        assert "goods_management" in cbs
        assert "categories_management" in cbs
        assert "user_management" in cbs
        assert "send_message" in cbs
        assert "role_mgmt" in cbs

    def test_user_search_button_is_at_bottom_of_admin_menu(self):
        markup = admin_console_keyboard()
        cbs = _all_callback_data(markup)

        assert "user_search" in cbs
        assert cbs[-2:] == ["user_search", "back_to_menu"]

    def test_balance_topup_button_is_at_bottom_for_balance_managers(self):
        markup = admin_console_keyboard(role=Permission.BALANCE_MANAGE)
        cbs = _all_callback_data(markup)

        assert cbs[-2:] == ["admin_replenish_balance", "back_to_menu"]

    def test_maintenance_toggle(self):
        markup_on = admin_console_keyboard(maintenance_mode=True)
        markup_off = admin_console_keyboard(maintenance_mode=False)
        texts_on = _all_button_texts(markup_on)
        texts_off = _all_button_texts(markup_off)
        # The maintenance button text should differ between states
        assert texts_on != texts_off


class TestCallbackDataFitsTelegramLimit:
    LONG_CYRILLIC = "Подарочный сертификат Steam на 1000 рублей регион свободный"
    LONG_ASCII = "S" * 100

    def _assert_all_fit(self, markup):
        for cb in _all_callback_data(markup):
            assert len(cb.encode("utf-8")) <= 64, f"too long ({len(cb.encode())}B): {cb!r}"

    def test_item_card_fits_with_every_button_shown(self):
        markup = item_info(
            "gp_0", avg_rating=4.5, review_count=7, has_purchased=True,
            out_of_stock=True, subscribed=False,
        )
        self._assert_all_fit(markup)

    def test_item_card_fits_with_promo_applied(self):
        self._assert_all_fit(item_info("gp_0", applied_promo="SUMMER-2026", review_count=3))

    def test_cart_keyboard_fits_for_long_names(self):
        for name in (self.LONG_CYRILLIC, self.LONG_ASCII):
            items = [{"id": 987654, "item_name": name, "quantity": 99}]
            self._assert_all_fit(cart_keyboard(items))

    def test_rating_keyboard_fits(self):
        self._assert_all_fit(rating_keyboard())

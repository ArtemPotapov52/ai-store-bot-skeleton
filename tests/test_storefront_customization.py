from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, update

from bot.database import Database
from bot.database.methods import (
    check_user,
    get_catalog_items_summary,
    query_categories,
    set_user_locale,
)
from bot.database.methods.transactions import buy_item_transaction
from bot.database.models import Categories, Goods
from bot.handlers.user.balance_and_payment import process_replenish_balance
from bot.i18n.main import get_locale, localize, reset_locale, set_locale
from bot.misc import screens
from bot.misc.screens import answer_screen, edit_screen, resolve_photo


class TestStorefrontCatalog:
    async def test_categories_follow_admin_sort_order(self, category_factory):
        await category_factory("Second")
        await category_factory("First")
        await category_factory("Hidden")
        async with Database().session() as session:
            await session.execute(update(Categories).where(Categories.name == "First").values(sort_order=10))
            await session.execute(update(Categories).where(Categories.name == "Second").values(sort_order=20))
            await session.execute(update(Categories).where(Categories.name == "Hidden").values(is_active=False))

        assert await query_categories() == ["First", "Second"]

    async def test_catalog_summary_reports_limited_stock(self, item_factory):
        await item_factory(
            name="Stocked",
            price=125,
            values=[("one", False), ("two", False)],
        )
        summary = (await get_catalog_items_summary(["Stocked"]))["Stocked"]
        assert summary["price"] == Decimal("125")
        assert summary["quantity"] == 2
        assert summary["is_infinite"] is False

    async def test_catalog_summary_includes_counted_stock(self, item_factory):
        await item_factory(
            name="Counted summary", price=90, stock_quantity=7,
            delivery_text="One shared delivery message",
        )

        summary = (await get_catalog_items_summary(["Counted summary"]))["Counted summary"]

        assert summary["quantity"] == 7
        assert summary["is_infinite"] is False

    async def test_hidden_product_is_not_summarized(self, item_factory):
        await item_factory(name="Hidden product", price=10, values=[("one", False)])
        async with Database().session() as session:
            await session.execute(
                update(Goods).where(Goods.name == "Hidden product").values(is_active=False)
            )
        assert await get_catalog_items_summary(["Hidden product"]) == {}

    async def test_hidden_product_cannot_be_purchased_from_a_stale_button(
        self, item_factory, user_factory
    ):
        await user_factory(telegram_id=710010, balance=100)
        await item_factory(name="Hidden purchase", price=10, values=[("secret", False)])
        async with Database().session() as session:
            await session.execute(
                update(Goods).where(Goods.name == "Hidden purchase").values(is_active=False)
            )
        ok, code, _ = await buy_item_transaction(710010, "Hidden purchase")
        assert (ok, code) == (False, "item_not_found")


class TestPerUserLanguage:
    async def test_locale_is_persisted(self, user_factory):
        await user_factory(telegram_id=710001)
        assert await set_user_locale(710001, "en") is True
        assert (await check_user(710001))["locale"] == "en"

    async def test_vietnamese_locale_is_persisted(self, user_factory):
        await user_factory(telegram_id=710003)
        assert await set_user_locale(710003, "vi") is True
        assert (await check_user(710003))["locale"] == "vi"
        token = set_locale("vi")
        try:
            assert localize("btn.shop") == "Sản phẩm"
        finally:
            reset_locale(token)

    async def test_context_locale_does_not_mutate_default(self):
        default = get_locale()
        token = set_locale("en")
        try:
            assert get_locale() == "en"
            assert localize("btn.shop") == "Products"
        finally:
            reset_locale(token)
        assert get_locale() == default

    async def test_unknown_locale_is_rejected(self, user_factory):
        await user_factory(telegram_id=710002)
        assert await set_user_locale(710002, "xx") is False
        assert (await check_user(710002))["locale"] == "ru"


class TestDemoPayment:
    async def test_demo_payment_credits_once(self, make_callback_query, fsm_context, user_factory):
        await user_factory(telegram_id=720001)
        await fsm_context.update_data(amount=125, test_payment_id="demo-payment-1")
        first = make_callback_query(data="pay_test", user_id=720001)

        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.TEST_PAYMENT_ENABLED = "1"
            env.DEBUG = "1"
            env.PAY_CURRENCY = "RUB"
            env.REFERRAL_PERCENT = 0
            env.MIN_AMOUNT = 20
            env.MAX_AMOUNT = 10000
            env.PAYMENT_TIME = 1800
            await process_replenish_balance(first, fsm_context)

        assert (await check_user(720001))["balance"] == Decimal("125")

        # A Telegram retry of the same demo transaction must not credit twice.
        await fsm_context.update_data(amount=125, test_payment_id="demo-payment-1")
        replay = make_callback_query(data="pay_test", user_id=720001)
        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.TEST_PAYMENT_ENABLED = "1"
            env.DEBUG = "1"
            env.PAY_CURRENCY = "RUB"
            env.REFERRAL_PERCENT = 0
            env.MIN_AMOUNT = 20
            env.MAX_AMOUNT = 10000
            env.PAYMENT_TIME = 1800
            await process_replenish_balance(replay, fsm_context)

        assert (await check_user(720001))["balance"] == Decimal("125")
        replay.answer.assert_called_once()

    async def test_demo_payment_stays_disabled_without_debug(
        self, make_callback_query, fsm_context, user_factory
    ):
        await user_factory(telegram_id=720002)
        await fsm_context.update_data(amount=50, test_payment_id="demo-payment-2")
        call = make_callback_query(data="pay_test", user_id=720002)
        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.TEST_PAYMENT_ENABLED = "1"
            env.DEBUG = "0"
            env.PAY_CURRENCY = "RUB"
            env.MIN_AMOUNT = 20
            env.MAX_AMOUNT = 10000
            env.PAYMENT_TIME = 1800
            await process_replenish_balance(call, fsm_context)

        assert (await check_user(720002))["balance"] == Decimal("0")
        call.answer.assert_called_once()


class TestArtworkSafety:
    def test_path_traversal_is_rejected(self):
        assert resolve_photo("catalog", "../.env") is None

    def test_unknown_screen_name_is_rejected(self):
        assert resolve_photo("../catalog") is None

    async def test_photo_message_can_transition_to_a_text_screen(self):
        call = MagicMock()
        call.from_user.id = 730001
        call.message.photo = [MagicMock()]
        call.message.delete = AsyncMock()
        call.message.edit_text = AsyncMock()
        call.message.edit_caption = AsyncMock()
        call.message.bot.send_message = AsyncMock()

        await edit_screen(call, "Текстовый экран", screen="missing-screen")

        call.message.delete.assert_not_awaited()
        call.message.edit_text.assert_not_called()
        call.message.bot.send_message.assert_not_called()
        call.message.edit_caption.assert_awaited_once_with(
            caption="Текстовый экран",
            reply_markup=None,
            parse_mode="HTML",
        )

    async def test_photo_screen_replaces_artwork_in_the_same_message(self):
        chat_id = 730003
        call = MagicMock()
        call.from_user.id = chat_id
        call.message.message_id = 103
        call.message.chat.id = chat_id
        call.message.photo = [MagicMock()]
        call.message.delete = AsyncMock()
        call.message.edit_media = AsyncMock()

        try:
            with patch("bot.misc.screens.resolve_photo", return_value="new-artwork"):
                await edit_screen(
                    call,
                    "Новая карточка",
                    screen="product",
                    image_ref="categories/gemini.png",
                )

            call.message.delete.assert_not_awaited()
            call.message.bot.send_photo.assert_not_called()
            call.message.edit_media.assert_awaited_once()
            media = call.message.edit_media.call_args.kwargs["media"]
            assert media.media == "new-artwork"
            assert media.caption == "Новая карточка"
        finally:
            screens._SCREEN_MESSAGES.pop(chat_id, None)
            screens._SCREEN_MEDIA_KEYS.pop(chat_id, None)

    async def test_same_photo_screen_edits_only_caption_and_buttons(self):
        chat_id = 730004
        call = MagicMock()
        call.from_user.id = chat_id
        call.message.message_id = 104
        call.message.chat.id = chat_id
        call.message.photo = [MagicMock()]
        call.message.delete = AsyncMock()
        call.message.edit_caption = AsyncMock()
        call.message.edit_media = AsyncMock()
        screens._remember_screen(
            chat_id,
            call.message,
            media_key="categories/chatgpt.png",
        )

        try:
            with patch("bot.misc.screens.resolve_photo", return_value="same-artwork"):
                await edit_screen(
                    call,
                    "Обновлённое описание",
                    screen="product",
                    image_ref="categories/chatgpt.png",
                )

            call.message.delete.assert_not_awaited()
            call.message.edit_media.assert_not_called()
            call.message.edit_caption.assert_awaited_once_with(
                caption="Обновлённое описание",
                reply_markup=None,
                parse_mode="HTML",
            )
        finally:
            screens._SCREEN_MESSAGES.pop(chat_id, None)
            screens._SCREEN_MEDIA_KEYS.pop(chat_id, None)

    async def test_text_screen_keeps_the_same_message_when_a_banner_is_requested(self):
        """Telegram cannot turn text into a photo, so retain the text message."""
        chat_id = 730005
        call = MagicMock()
        call.from_user.id = chat_id
        call.message.message_id = 105
        call.message.chat.id = chat_id
        call.message.photo = None
        call.message.delete = AsyncMock()
        call.message.edit_text = AsyncMock()
        call.message.bot.send_photo = AsyncMock()

        try:
            with patch("bot.misc.screens.resolve_photo", return_value="menu-artwork"):
                await edit_screen(call, "Главное меню", screen="main-menu")

            call.message.delete.assert_not_awaited()
            call.message.bot.send_photo.assert_not_called()
            call.message.edit_text.assert_awaited_once_with(
                "Главное меню",
                reply_markup=None,
                parse_mode="HTML",
            )
        finally:
            screens._SCREEN_MESSAGES.pop(chat_id, None)
            screens._SCREEN_MEDIA_KEYS.pop(chat_id, None)

    async def test_new_message_screen_deletes_the_previous_rendered_screen(self):
        chat_id = 730002
        previous = MagicMock(message_id=101)
        previous.delete = AsyncMock()
        screens._remember_screen(chat_id, previous)

        message = MagicMock()
        message.chat.id = chat_id
        rendered = MagicMock(message_id=102)
        message.answer = AsyncMock(return_value=rendered)

        try:
            await answer_screen(message, "Главное меню", screen="missing-screen")
            previous.delete.assert_awaited_once()
            message.answer.assert_awaited_once_with(
                "Главное меню", reply_markup=None, parse_mode="HTML"
            )
        finally:
            screens._SCREEN_MESSAGES.pop(chat_id, None)


class TestScreenDeduplication:
    """The main menu must never stack: rapid /start taps, retried updates and
    pre-restart messages all have to collapse into a single screen."""

    async def _answer_message(self, chat_id, bot, rendered_id):
        message = MagicMock()
        message.chat.id = chat_id
        message.from_user.id = chat_id
        message.bot = bot
        rendered = MagicMock(message_id=rendered_id)
        rendered.delete = AsyncMock()
        message.answer = AsyncMock(return_value=rendered)
        return message, rendered

    async def test_concurrent_answers_leave_a_single_menu(self):
        import asyncio

        chat_id = 731001
        bot = AsyncMock()
        msg1, rendered1 = await self._answer_message(chat_id, bot, 501)
        msg2, rendered2 = await self._answer_message(chat_id, bot, 502)
        try:
            await asyncio.gather(
                answer_screen(msg1, "Меню", screen="missing-screen"),
                answer_screen(msg2, "Меню", screen="missing-screen"),
            )
            assert msg1.answer.await_count == 1
            assert msg2.answer.await_count == 1
            tracked = screens._SCREEN_MESSAGES.get(chat_id, [])
            assert len(tracked) == 1
            first_deleted = rendered1.delete.await_count == 1
            second_deleted = rendered2.delete.await_count == 1
            assert first_deleted != second_deleted
            survivor = rendered2 if first_deleted else rendered1
            assert tracked[0] is survivor
        finally:
            screens._SCREEN_MESSAGES.pop(chat_id, None)
            screens._SCREEN_MEDIA_KEYS.pop(chat_id, None)

    async def test_next_answer_deletes_pre_restart_menu(self):
        chat_id = 731002
        bot = AsyncMock()
        message, _rendered = await self._answer_message(chat_id, bot, 504)
        screens._RESTORED_IDS[chat_id] = [499, 500]
        try:
            await answer_screen(message, "Меню", screen="missing-screen")
            bot.delete_message.assert_any_call(chat_id, 499)
            bot.delete_message.assert_any_call(chat_id, 500)
            assert chat_id not in screens._RESTORED_IDS
        finally:
            screens._SCREEN_MESSAGES.pop(chat_id, None)
            screens._SCREEN_MEDIA_KEYS.pop(chat_id, None)
            screens._RESTORED_IDS.pop(chat_id, None)

    async def test_tapping_old_duplicate_deletes_the_other_one(self):
        chat_id = 731003
        bot = AsyncMock()
        call = MagicMock()
        call.from_user.id = chat_id
        call.bot = bot
        call.message.message_id = 511
        call.message.chat.id = chat_id
        call.message.photo = None
        call.message.bot = bot
        call.message.edit_text = AsyncMock()
        screens._RESTORED_IDS[chat_id] = [511, 512]
        try:
            await edit_screen(call, "Меню", screen="missing-screen")
            bot.delete_message.assert_awaited_once_with(chat_id, 512)
            call.message.edit_text.assert_awaited_once()
        finally:
            screens._SCREEN_MESSAGES.pop(chat_id, None)
            screens._SCREEN_MEDIA_KEYS.pop(chat_id, None)
            screens._RESTORED_IDS.pop(chat_id, None)

    async def test_double_tap_on_menu_does_not_raise(self):
        chat_id = 731004
        call = MagicMock()
        call.from_user.id = chat_id
        call.message.message_id = 513
        call.message.chat.id = chat_id
        call.message.photo = None
        call.message.edit_text = AsyncMock(
            side_effect=TelegramBadRequest(
                method=MagicMock(), message="message is not modified"
            )
        )
        try:
            await edit_screen(call, "Меню", screen="missing-screen")
            call.message.edit_text.assert_awaited_once()
        finally:
            screens._SCREEN_MESSAGES.pop(chat_id, None)
            screens._SCREEN_MEDIA_KEYS.pop(chat_id, None)

    def test_identical_start_within_window_is_duplicate(self):
        from bot.handlers.user.main import _is_duplicate_start, _LAST_START

        uid = 731005
        _LAST_START.pop(uid, None)
        try:
            assert _is_duplicate_start(uid, "/start") is False
            assert _is_duplicate_start(uid, "/start") is True
            assert _is_duplicate_start(uid, "/start 123") is False
        finally:
            _LAST_START.pop(uid, None)

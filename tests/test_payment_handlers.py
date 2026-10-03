import asyncio

import pytest
from decimal import Decimal
from unittest.mock import patch, AsyncMock, MagicMock

from sqlalchemy import select

from bot.database.methods.read import check_user
from bot.database.main import Database
from bot.database.models.main import Payments
from bot.handlers.user.balance_and_payment import (
    replenish_balance_callback_handler, buy_item_callback_handler,
    purchase_confirm_handler, purchase_topup_handler,
    checking_payment, successful_payment_handler, process_replenish_balance,
    pre_checkout_handler,
)
from bot.misc.services.payment import currency_to_stars, payload_amount
from bot.states import BalanceStates, ShopStates


class TestReplenishBalance:

    async def test_no_payment_methods_enabled(self, make_callback_query, fsm_context):

        call = make_callback_query(data="replenish_balance", user_id=400001)

        with patch('bot.handlers.user.balance_and_payment._any_payment_method_enabled', return_value=False):
            await replenish_balance_callback_handler(call, fsm_context)

        call.answer.assert_called_once()
        # No provider configured -> the top-up menu is never offered.
        call.message.edit_text.assert_not_called()

    async def test_expired_session_renders_real_menu(self, make_callback_query, fsm_context, user_factory):
        await user_factory(telegram_id=400010)
        await fsm_context.set_state(BalanceStates.waiting_payment)
        call = make_callback_query(data="pay_cryptopay", user_id=400010)

        await process_replenish_balance(call, fsm_context)

        call.answer.assert_awaited_once()
        args, _kwargs = call.message.edit_text.call_args
        assert args[0] != "menu.title"  # formatted menu, not the raw template
        assert await fsm_context.get_state() is None

    async def test_sets_waiting_amount_state(self, make_callback_query, fsm_context):
        call = make_callback_query(data="replenish_balance", user_id=400002)

        with patch('bot.handlers.user.balance_and_payment._any_payment_method_enabled', return_value=True), \
             patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.PAY_CURRENCY = "RUB"
            await replenish_balance_callback_handler(call, fsm_context)

        state = await fsm_context.get_state()
        assert state == BalanceStates.waiting_amount
        call.message.bot.send_photo.assert_not_called()
        call.message.edit_text.assert_awaited_once()
        assert "payments.replenish_prompt" in call.message.edit_text.call_args.args[0]

    async def test_sbp_shows_setup_message_without_platega_credentials(
        self, make_callback_query, fsm_context
    ):
        await fsm_context.update_data(amount=250)
        call = make_callback_query(data="pay_sbp_card", user_id=400003)

        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.PAY_CURRENCY = "RUB"
            env.PAYMENT_TIME = 1800
            env.PLATEGA_MERCHANT_ID = ""
            env.PLATEGA_API_KEY = ""
            await process_replenish_balance(call, fsm_context)

        call.message.bot.send_photo.assert_not_called()
        call.message.edit_text.assert_awaited_once()
        assert call.message.edit_text.call_args.args[0] == "payments.platega.setup"

    async def test_sbp_creates_platega_payment_link(
        self, make_callback_query, fsm_context, user_factory
    ):
        await user_factory(telegram_id=400033)
        await fsm_context.update_data(amount=Decimal("275.50"))
        call = make_callback_query(data="pay_sbp_card", user_id=400033)
        call.bot.me.return_value = MagicMock(username="your_store_bot")
        transaction_id = "3fa85f64-5717-4562-b3fc-2c463f66afa6"
        platega = MagicMock()
        platega.create_sbp_transaction = AsyncMock(return_value={
            "transactionId": transaction_id,
            "redirect": "https://pay.platega.io/qrsbp",
            "status": "PENDING",
            "expiresIn": "00:15:00",
        })

        with (
            patch("bot.handlers.user.balance_and_payment.EnvKeys") as env,
            patch("bot.handlers.user.balance_and_payment.PlategaAPI", return_value=platega),
        ):
            env.PAY_CURRENCY = "RUB"
            env.PAYMENT_TIME = 1800
            env.PLATEGA_MERCHANT_ID = "merchant-test"
            env.PLATEGA_API_KEY = "api-test"
            env.REFERRAL_PERCENT = 0
            await process_replenish_balance(call, fsm_context)

        assert platega.create_sbp_transaction.await_count == 1
        args = platega.create_sbp_transaction.await_args.kwargs
        assert args["amount"] == Decimal("275.50")
        assert args["currency"] == "RUB"
        assert args["user_id"] == 400033
        assert args["return_url"] == "https://t.me/your_store_bot"

        data = await fsm_context.get_data()
        assert data["invoice_id"] == transaction_id
        assert data["payment_type"] == "platega"
        assert "275.50" in call.message.edit_text.call_args.args[0]

        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].url == "https://pay.platega.io/qrsbp"

    async def test_admin_payment_has_amount_and_prepared_message(
        self, make_callback_query, fsm_context
    ):
        await fsm_context.update_data(amount=375)
        call = make_callback_query(data="pay_admin", user_id=400004)

        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.PAY_CURRENCY = "RUB"
            env.PAYMENT_ADMIN_USERNAME = "@your_username"
            env.PAYMENT_TIME = 1800
            await process_replenish_balance(call, fsm_context)

        call.message.bot.send_photo.assert_not_called()
        call.message.edit_text.assert_awaited_once()
        caption = call.message.edit_text.call_args.args[0]
        assert caption.startswith("payments.admin.info:")
        assert "375" in caption
        assert "your_username" in caption
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        button = markup.inline_keyboard[0][0]
        assert button.url.startswith("https://t.me/your_username?text=")
        assert "375" in button.url

    async def test_cryptobot_without_token_explains_setup(
        self, make_callback_query, fsm_context
    ):
        await fsm_context.update_data(amount=100)
        call = make_callback_query(data="pay_cryptopay", user_id=400005)

        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.PAY_CURRENCY = "RUB"
            env.CRYPTO_PAY_TOKEN = ""
            env.PAYMENT_TIME = 1800
            await process_replenish_balance(call, fsm_context)

        call.message.bot.send_photo.assert_not_called()
        call.message.edit_text.assert_awaited_once()
        assert call.message.edit_text.call_args.args[0] == "payments.crypto.setup"

    async def test_stars_disabled_shows_unavailable_message(
        self, make_callback_query, fsm_context
    ):
        await fsm_context.update_data(amount=100)
        call = make_callback_query(data="pay_stars", user_id=400006)

        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.PAY_CURRENCY = "RUB"
            env.STARS_PER_VALUE = 0
            await process_replenish_balance(call, fsm_context)

        call.answer.assert_awaited_once_with(
            "payments.stars.unavailable", show_alert=True
        )
        call.message.edit_text.assert_not_called()


class TestStarsAvailability:

    async def test_stars_pre_checkout_is_rejected_when_disabled(self):
        query = AsyncMock()
        query.currency = "XTR"
        query.invoice_payload = '{"amount": 100, "stars": 91}'

        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.STARS_PER_VALUE = 0
            await pre_checkout_handler(query)

        query.answer.assert_awaited_once_with(
            ok=False, error_message="payments.stars.unavailable"
        )


class TestCheckingPayment:

    async def test_no_active_invoice(self, make_callback_query, fsm_context):

        call = make_callback_query(data="check", user_id=400010)
        # Empty state - no payment_type
        await fsm_context.clear()

        await checking_payment(call, fsm_context)

        call.answer.assert_called_once()
        # Nothing to check means nothing is rendered either.
        call.message.edit_text.assert_not_called()

    async def test_cryptopay_paid_credits_balance(self, make_callback_query, fsm_context, user_factory):

        await user_factory(telegram_id=400011, balance=0)

        call = make_callback_query(data="check", user_id=400011)

        await fsm_context.update_data(
            payment_type="cryptopay",
            invoice_id="inv_123",
        )

        mock_crypto = AsyncMock()
        mock_crypto.get_invoice = AsyncMock(return_value={
            "status": "paid",
            "amount": "100.00",
        })

        with patch('bot.handlers.user.balance_and_payment.CryptoPayAPI', return_value=mock_crypto), \
             patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            await checking_payment(call, fsm_context)

        # Balance should be updated in DB
        user = await check_user(400011)
        assert user['balance'] == Decimal("100")

        # Payment record should exist
        async with Database().session() as s:
            result = await s.execute(
                select(Payments).where(Payments.external_id == "inv_123")
            )
            payment = result.scalars().first()
            assert payment is not None
            assert payment.status == "succeeded"

    async def test_cryptopay_not_paid_yet(self, make_callback_query, fsm_context, user_factory):

        await user_factory(telegram_id=400012)

        call = make_callback_query(data="check", user_id=400012)
        await fsm_context.update_data(payment_type="cryptopay", invoice_id="inv_456")

        mock_crypto = AsyncMock()
        mock_crypto.get_invoice = AsyncMock(return_value={"status": "active"})

        with patch('bot.handlers.user.balance_and_payment.CryptoPayAPI', return_value=mock_crypto):
            await checking_payment(call, fsm_context)

        call.answer.assert_called()
        # Balance should still be 0
        user = await check_user(400012)
        assert user['balance'] == Decimal("0")

    async def test_platega_check_credits_from_authenticated_status_lookup(
        self, make_callback_query, fsm_context, user_factory
    ):
        user_id = 400032
        transaction_id = "3fa85f64-5717-4562-b3fc-2c463f66afa6"
        intent_id = "intent-platega-status-check"
        await user_factory(telegram_id=user_id, balance=0)
        from bot.database.methods import bind_pending_payment, create_pending_payment

        await create_pending_payment("platega", intent_id, user_id, 20, "RUB")
        await bind_pending_payment("platega", intent_id, transaction_id)
        call = make_callback_query(data="check", user_id=user_id)
        await fsm_context.update_data(
            payment_type="platega",
            invoice_id=transaction_id,
            payment_intent_id=intent_id,
        )
        platega = AsyncMock()
        platega.get_transaction.return_value = {
            "id": transaction_id,
            "status": "CONFIRMED",
            "paymentDetails": {"amount": 21.7, "currency": "RUB"},
            "paymentMethod": "SBPQR",
            "comission": 1.7,
            "payload": intent_id,
        }

        with patch(
            "bot.handlers.user.balance_and_payment.PlategaAPI", return_value=platega
        ), patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.REFERRAL_PERCENT = 0
            await checking_payment(call, fsm_context)

        assert (await check_user(user_id))["balance"] == Decimal("20.00")
        call.message.edit_text.assert_awaited_once()
        assert await fsm_context.get_data() == {}

        # A queued/repeated tap after success must not trigger another credit.
        await checking_payment(call, fsm_context)
        await checking_payment(call, fsm_context)

        assert (await check_user(user_id))["balance"] == Decimal("20.00")
        assert platega.get_transaction.await_count == 1
        assert call.message.edit_text.await_count == 1
        assert call.answer.await_count == 2

    async def test_simultaneous_platega_check_clicks_credit_only_once(
        self, make_callback_query, fsm_context, user_factory
    ):
        user_id = 400034
        transaction_id = "3fa85f64-5717-4562-b3fc-2c463f66afa6"
        intent_id = "intent-platega-simultaneous-check"
        await user_factory(telegram_id=user_id, balance=0)
        from bot.database.methods import bind_pending_payment, create_pending_payment

        await create_pending_payment("platega", intent_id, user_id, 20, "RUB")
        await bind_pending_payment("platega", intent_id, transaction_id)
        check_data = {
            "payment_type": "platega",
            "invoice_id": transaction_id,
            "payment_intent_id": intent_id,
        }
        await fsm_context.update_data(**check_data)
        second_state = type(fsm_context)()
        await second_state.update_data(**check_data)
        first_call = make_callback_query(data="check", user_id=user_id)
        second_call = make_callback_query(data="check", user_id=user_id)
        platega = AsyncMock()
        platega.get_transaction.return_value = {
            "id": transaction_id,
            "status": "CONFIRMED",
            "paymentDetails": {"amount": 21.7, "currency": "RUB"},
            "paymentMethod": "SBPQR",
            "comission": 1.7,
            "payload": intent_id,
        }

        with patch(
            "bot.handlers.user.balance_and_payment.PlategaAPI", return_value=platega
        ), patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.REFERRAL_PERCENT = 0
            await asyncio.gather(
                checking_payment(first_call, fsm_context),
                checking_payment(second_call, second_state),
            )

        assert platega.get_transaction.await_count == 2
        assert (await check_user(user_id))["balance"] == Decimal("20.00")
        first_call.message.edit_text.assert_awaited_once()
        second_call.message.edit_text.assert_awaited_once()
        assert await fsm_context.get_data() == {}
        assert await second_state.get_data() == {}

    async def test_cryptopay_expired(self, make_callback_query, fsm_context, user_factory):

        await user_factory(telegram_id=400013)

        call = make_callback_query(data="check", user_id=400013)
        await fsm_context.update_data(payment_type="cryptopay", invoice_id="inv_789")

        mock_crypto = AsyncMock()
        mock_crypto.get_invoice = AsyncMock(return_value={"status": "expired"})

        with patch('bot.handlers.user.balance_and_payment.CryptoPayAPI', return_value=mock_crypto):
            await checking_payment(call, fsm_context)

        call.answer.assert_called()
        # An expired invoice must never credit the balance.
        assert (await check_user(400013))['balance'] == Decimal("0")

    async def test_cryptopay_already_processed(self, make_callback_query, fsm_context, user_factory):

        await user_factory(telegram_id=400014, balance=0)

        # First payment
        call1 = make_callback_query(data="check", user_id=400014)
        await fsm_context.update_data(payment_type="cryptopay", invoice_id="inv_dup")

        mock_crypto = AsyncMock()
        mock_crypto.get_invoice = AsyncMock(return_value={
            "status": "paid", "amount": "50.00"
        })

        with patch('bot.handlers.user.balance_and_payment.CryptoPayAPI', return_value=mock_crypto), \
             patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            await checking_payment(call1, fsm_context)

        # Second attempt with same invoice
        call2 = make_callback_query(data="check", user_id=400014)
        await fsm_context.update_data(payment_type="cryptopay", invoice_id="inv_dup")

        with patch('bot.handlers.user.balance_and_payment.CryptoPayAPI', return_value=mock_crypto), \
             patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            await checking_payment(call2, fsm_context)

        # Balance should only be credited once
        user = await check_user(400014)
        assert user['balance'] == Decimal("50")


class TestCryptoPayFractionalAmounts:
    """Balance is NUMERIC(12,2): the kopecks of an invoice must survive."""

    async def test_fractional_amount_is_credited_in_full(self, make_callback_query,
                                                         fsm_context, user_factory):

        await user_factory(telegram_id=400030, balance=0)
        call = make_callback_query(data="check", user_id=400030)
        await fsm_context.update_data(payment_type="cryptopay", invoice_id="inv_frac")

        mock_crypto = AsyncMock()
        mock_crypto.get_invoice = AsyncMock(return_value={"status": "paid", "amount": "20.50"})

        with patch('bot.handlers.user.balance_and_payment.CryptoPayAPI', return_value=mock_crypto), \
             patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            await checking_payment(call, fsm_context)

        # Was truncated to 20 before: quantize(Decimal("1.")) ate the .50
        user = await check_user(400030)
        assert user['balance'] == Decimal("20.50")

        async with Database().session() as s:
            payment = (await s.execute(
                select(Payments).where(Payments.external_id == "inv_frac")
            )).scalars().first()
            assert payment.amount == Decimal("20.50")   # ledger agrees with the credit

    async def test_zero_amount_is_rejected(self, make_callback_query, fsm_context, user_factory):

        await user_factory(telegram_id=400031, balance=0)
        call = make_callback_query(data="check", user_id=400031)
        await fsm_context.update_data(payment_type="cryptopay", invoice_id="inv_zero")

        mock_crypto = AsyncMock()
        mock_crypto.get_invoice = AsyncMock(return_value={"status": "paid", "amount": "0"})

        with patch('bot.handlers.user.balance_and_payment.CryptoPayAPI', return_value=mock_crypto), \
             patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            await checking_payment(call, fsm_context)

        user = await check_user(400031)
        assert user['balance'] == Decimal("0")


class TestSuccessfulPaymentIdempotency:

    def _make_successful_payment(self, charge_id=None):
        sp = MagicMock()
        sp.currency = "XTR"
        sp.total_amount = 100
        sp.invoice_payload = '{"amount": 100}'
        sp.telegram_payment_charge_id = charge_id
        sp.provider_payment_charge_id = None
        return sp

    async def test_replay_without_charge_id_credits_once(self, make_message, user_factory):
        """A missing charge id must still yield a stable idempotency key.

        The old uuid4() fallback made every replay look like a new payment,
        defeating uq_payment_provider_ext entirely.
        """

        await user_factory(telegram_id=400040, balance=0)

        with patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            env.STARS_PER_VALUE = 1

            first = make_message(text="", user_id=400040)
            first.successful_payment = self._make_successful_payment()
            await successful_payment_handler(first)

            second = make_message(text="", user_id=400040)
            second.successful_payment = self._make_successful_payment()
            await successful_payment_handler(second)

        user = await check_user(400040)
        assert user['balance'] == Decimal("100")   # not 200

        async with Database().session() as s:
            payments = (await s.execute(
                select(Payments).where(Payments.user_id == 400040)
            )).scalars().all()
            assert len(payments) == 1

    async def test_fallback_key_is_deterministic(self, make_message, user_factory):

        await user_factory(telegram_id=400041, balance=0)

        with patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            env.STARS_PER_VALUE = 1

            message = make_message(text="", user_id=400041)
            message.successful_payment = self._make_successful_payment()
            await successful_payment_handler(message)

        async with Database().session() as s:
            payment = (await s.execute(
                select(Payments).where(Payments.user_id == 400041)
            )).scalars().one()
            assert payment.external_id.startswith("stars:fallback:")

    async def test_charge_id_is_used_when_present(self, make_message, user_factory):

        await user_factory(telegram_id=400042, balance=0)

        with patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            env.STARS_PER_VALUE = 1

            message = make_message(text="", user_id=400042)
            message.successful_payment = self._make_successful_payment(charge_id="tg_charge_1")
            await successful_payment_handler(message)

        async with Database().session() as s:
            payment = (await s.execute(
                select(Payments).where(Payments.user_id == 400042)
            )).scalars().one()
            assert payment.external_id == "tg_charge_1"


class TestBuyItemHandler:

    async def test_buy_item_success(self, make_callback_query, fsm_context, user_factory, item_factory):

        await user_factory(telegram_id=400020, balance=500)
        await item_factory(name="TestWidget", price=100, values=[("widget_value_1", False)])

        call = make_callback_query(data="buy_item", user_id=400020)
        await fsm_context.update_data(csrf_item="TestWidget")

        with patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.PAY_CURRENCY = "RUB"
            await buy_item_callback_handler(call, fsm_context)

        assert await fsm_context.get_state() == ShopStates.confirming_purchase
        assert (await check_user(400020))['balance'] == Decimal("500")

        confirm = make_callback_query(data="buy_confirm", user_id=400020)
        with patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.PAY_CURRENCY = "RUB"
            await purchase_confirm_handler(confirm, fsm_context)

        user = await check_user(400020)
        assert user['balance'] == Decimal("400")

    async def test_buy_item_insufficient_funds(self, make_callback_query, fsm_context, user_factory, item_factory):

        await user_factory(telegram_id=400021, balance=10)
        await item_factory(name="ExpensiveItem", price=1000, values=[("val", False)])

        call = make_callback_query(data="buy_item", user_id=400021)
        await fsm_context.update_data(csrf_item="ExpensiveItem")

        with patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.PAY_CURRENCY = "RUB"
            env.MIN_AMOUNT = 20
            env.STARS_PER_VALUE = 0
            env.TELEGRAM_PROVIDER_TOKEN = ""
            env.TEST_PAYMENT_ENABLED = "0"
            await buy_item_callback_handler(call, fsm_context)

        # Balance should be unchanged
        user = await check_user(400021)
        assert user['balance'] == Decimal("10")
        assert await fsm_context.get_state() == ShopStates.confirming_purchase
        call.message.bot.send_photo.assert_not_called()
        call.message.edit_text.assert_awaited_once()
        rendered_text = call.message.edit_text.call_args.args[0]
        assert "shop.purchase.insufficient" in rendered_text
        assert "buy_topup" in {
            button.callback_data
            for row in call.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data
        }

        topup = make_callback_query(data="buy_topup", user_id=400021)
        await purchase_topup_handler(topup, fsm_context)
        assert await fsm_context.get_state() == BalanceStates.waiting_payment
        callbacks = {
            button.callback_data
            for row in topup.message.edit_text.call_args.kwargs["reply_markup"].inline_keyboard
            for button in row
            if button.callback_data
        }
        assert {"pay_cryptopay", "pay_sbp_card", "pay_admin"}.issubset(callbacks)

    async def test_buy_item_registers_missing_user_before_purchase(self, make_callback_query, fsm_context, item_factory):
        """An old product button must not fail merely because the user row is absent."""
        await item_factory(name="FreshUserItem", price=100, values=[("value", False)])
        call = make_callback_query(data="buy_item", user_id=400023)
        await fsm_context.update_data(csrf_item="FreshUserItem")

        with patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.PAY_CURRENCY = "RUB"
            env.MIN_AMOUNT = 20
            env.STARS_PER_VALUE = 0
            env.TELEGRAM_PROVIDER_TOKEN = ""
            env.TEST_PAYMENT_ENABLED = "0"
            await buy_item_callback_handler(call, fsm_context)

        assert (await check_user(400023)) is not None
        assert await fsm_context.get_state() == ShopStates.confirming_purchase
        call.message.bot.send_photo.assert_not_called()
        assert "shop.purchase.insufficient" in call.message.edit_text.call_args.args[0]

    async def test_quantity_limit_matches_real_stock(self, make_callback_query, fsm_context,
                                                     user_factory, item_factory):
        """Counted stock must not be doubled: 30 units allow at most 30, not 60."""
        await user_factory(telegram_id=400024, balance=100000)
        await item_factory(name="CountedItem", price=10, stock_quantity=30,
                           delivery_text="shared delivery")
        call = make_callback_query(data="buy_item", user_id=400024)
        await fsm_context.update_data(csrf_item="CountedItem")

        with patch('bot.handlers.user.balance_and_payment.EnvKeys') as env:
            env.PAY_CURRENCY = "RUB"
            await buy_item_callback_handler(call, fsm_context)

        data = await fsm_context.get_data()
        assert data["purchase_max_quantity"] == 30
        assert data["purchase_quantity"] == 1

    async def test_buy_item_no_csrf_item(self, make_callback_query, fsm_context, user_factory):
        await user_factory(telegram_id=400022, balance=500)

        call = make_callback_query(data="buy_item", user_id=400022)
        # No csrf_item in state

        await buy_item_callback_handler(call, fsm_context)

        call.answer.assert_called_once_with('middleware.security.invalid_csrf', show_alert=True)
        # A purchase without the CSRF-guarded item name must not charge anything.
        assert (await check_user(400022))['balance'] == Decimal("500")


class TestStarsAmountFromPayload:
    def test_payload_amount_prefers_explicit_keys(self):

        assert payload_amount({"amount": 25}) == 25
        assert payload_amount({"amount_rub": 20, "stars": 19}) == 20
        assert payload_amount({}) == 0
        assert payload_amount({"amount": "nope"}) == 0

    @pytest.mark.parametrize("requested", [20, 21, 30, 33, 50, 111])
    async def test_stars_payment_credits_requested_amount(
        self, make_message, user_factory, requested
    ):
        import json
        import math

        await user_factory(telegram_id=400100 + requested, balance=0)

        stars = currency_to_stars(requested)
        assert stars == math.ceil(requested * 0.91)

        msg = make_message(text="", user_id=400100 + requested)
        msg.successful_payment = MagicMock()
        msg.successful_payment.currency = "XTR"
        msg.successful_payment.total_amount = stars
        msg.successful_payment.invoice_payload = json.dumps(
            {"op": "topup_balance_stars", "amount_rub": requested, "stars": stars}
        )
        msg.successful_payment.telegram_payment_charge_id = f"charge_{requested}"
        msg.successful_payment.provider_payment_charge_id = None

        await successful_payment_handler(msg)

        user = await check_user(400100 + requested)
        assert user['balance'] == Decimal(requested)

    async def test_stars_payment_falls_back_when_payload_missing(
        self, make_message, user_factory
    ):
        """No usable payload: the lossy reverse conversion is the last resort."""

        await user_factory(telegram_id=400199, balance=0)

        msg = make_message(text="", user_id=400199)
        msg.successful_payment = MagicMock()
        msg.successful_payment.currency = "XTR"
        msg.successful_payment.total_amount = 91
        msg.successful_payment.invoice_payload = ""
        msg.successful_payment.telegram_payment_charge_id = "charge_no_payload"
        msg.successful_payment.provider_payment_charge_id = None

        await successful_payment_handler(msg)

        user = await check_user(400199)
        assert user['balance'] == Decimal(100)


def _mock_xrocket(status="active"):
    mock_x = AsyncMock()
    mock_x.asset = "USDT"
    mock_x.fiat_to_asset_amount = AsyncMock(return_value="1.18")
    mock_x.create_invoice = AsyncMock(return_value={
        "id": "xr_1",
        "status": "active",
        "links": {"telegramBotLink": "https://t.me/xrocket?start=inv_x"},
    })
    mock_x.get_invoice = AsyncMock(return_value={"id": "xr_1", "status": status})
    return mock_x


class TestXrocketPayment:

    async def test_without_token_explains_setup(self, make_callback_query, fsm_context):
        await fsm_context.update_data(amount=100)
        call = make_callback_query(data="pay_xrocket", user_id=400200)

        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.PAY_CURRENCY = "RUB"
            env.XROCKET_PAY_TOKEN = ""
            env.PAYMENT_TIME = 1800
            await process_replenish_balance(call, fsm_context)

        call.message.edit_text.assert_awaited_once()
        assert call.message.edit_text.call_args.args[0] == "payments.xrocket.setup"

    async def test_create_invoice_registers_pending(
        self, make_callback_query, fsm_context, user_factory
    ):
        await user_factory(telegram_id=400201, balance=0)
        await fsm_context.update_data(amount=100)
        call = make_callback_query(data="pay_xrocket", user_id=400201)

        with patch("bot.handlers.user.balance_and_payment.XRocketPayAPI",
                   return_value=_mock_xrocket()), \
             patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.PAY_CURRENCY = "RUB"
            env.XROCKET_PAY_TOKEN = "tok"
            env.PAYMENT_TIME = 1800
            await process_replenish_balance(call, fsm_context)

        call.message.edit_text.assert_awaited_once()
        caption = call.message.edit_text.call_args.args[0]
        assert caption.startswith("payments.xrocket.invoice:")
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].url == "https://t.me/xrocket?start=inv_x"

        async with Database().session() as s:
            payment = (await s.execute(
                select(Payments).where(Payments.external_id == "xr_1")
            )).scalars().first()
            assert payment is not None
            assert payment.provider == "xrocket"
            assert payment.status == "pending"

        state = await fsm_context.get_data()
        assert state["payment_type"] == "xrocket"
        assert state["invoice_id"] == "xr_1"

    async def test_paid_credits_requested_fiat_amount(
        self, make_callback_query, fsm_context, user_factory
    ):
        await user_factory(telegram_id=400202, balance=0)
        call = make_callback_query(data="check", user_id=400202)
        await fsm_context.update_data(
            payment_type="xrocket", invoice_id="xr_paid", amount=100
        )

        with patch("bot.handlers.user.balance_and_payment.XRocketPayAPI",
                   return_value=_mock_xrocket(status="paid")), \
             patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.REFERRAL_PERCENT = 0
            env.PAY_CURRENCY = "RUB"
            await checking_payment(call, fsm_context)

        user = await check_user(400202)
        assert user['balance'] == Decimal("100")

    async def test_not_paid_yet(self, make_callback_query, fsm_context, user_factory):
        await user_factory(telegram_id=400203, balance=0)
        call = make_callback_query(data="check", user_id=400203)
        await fsm_context.update_data(
            payment_type="xrocket", invoice_id="xr_wait", amount=100
        )

        with patch("bot.handlers.user.balance_and_payment.XRocketPayAPI",
                   return_value=_mock_xrocket(status="active")):
            await checking_payment(call, fsm_context)

        call.answer.assert_called()
        assert (await check_user(400203))['balance'] == Decimal("0")

    async def test_expired_never_credits(
        self, make_callback_query, fsm_context, user_factory
    ):
        await user_factory(telegram_id=400204, balance=0)
        call = make_callback_query(data="check", user_id=400204)
        await fsm_context.update_data(
            payment_type="xrocket", invoice_id="xr_old", amount=100
        )

        with patch("bot.handlers.user.balance_and_payment.XRocketPayAPI",
                   return_value=_mock_xrocket(status="expired")):
            await checking_payment(call, fsm_context)

        call.answer.assert_called()
        assert (await check_user(400204))['balance'] == Decimal("0")


class TestManualCryptoPayment:

    async def test_shows_wallets_and_admin_link(
        self, make_callback_query, fsm_context
    ):
        await fsm_context.update_data(amount=500)
        call = make_callback_query(data="pay_manual_crypto", user_id=400205)

        with patch("bot.handlers.user.balance_and_payment.EnvKeys") as env:
            env.PAY_CURRENCY = "RUB"
            env.PAYMENT_ADMIN_USERNAME = "your_username"
            env.PAYMENT_TIME = 1800
            env.XROCKET_PAY_TOKEN = ""
            env.MANUAL_USDT_BEP20 = "0xUSDT"
            env.MANUAL_TON = "TONADDR"
            env.MANUAL_SOL = "SOLADDR"
            await process_replenish_balance(call, fsm_context)

        call.message.edit_text.assert_awaited_once()
        caption = call.message.edit_text.call_args.args[0]
        assert caption.startswith("payments.manual_crypto.info:")
        assert "0xUSDT" in caption and "TONADDR" in caption and "SOLADDR" in caption
        # The 0.1 USD minimum lives in the real template (localize is mocked).
        from bot.i18n.strings import TRANSLATIONS
        assert "0.1 USD" in TRANSLATIONS["ru"]["payments.manual_crypto.info"]
        markup = call.message.edit_text.call_args.kwargs["reply_markup"]
        assert markup.inline_keyboard[0][0].url.startswith("https://t.me/your_username?text=")


class TestPaymentMenuContents:

    async def test_no_test_button_when_disabled(self):
        from bot.keyboards.inline import get_payment_choice

        with patch("bot.keyboards.inline.EnvKeys") as env:
            env.STARS_PER_VALUE = 0
            env.TELEGRAM_PROVIDER_TOKEN = ""
            env.TEST_PAYMENT_ENABLED = "0"
            markup = get_payment_choice()

        callbacks = [
            b.callback_data for row in markup.inline_keyboard for b in row
            if b.callback_data
        ]
        assert "pay_xrocket" in callbacks
        assert "pay_manual_crypto" in callbacks
        assert "pay_test" not in callbacks

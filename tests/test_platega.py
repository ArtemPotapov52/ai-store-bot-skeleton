from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from bot.database.methods import bind_pending_payment, check_user, create_pending_payment
from bot.misc import EnvKeys
from bot.misc.services.platega import (
    PlategaAPI,
    PlategaCallbackError,
    process_platega_callback,
)


class TestPlategaAPI:

    async def test_create_sbp_transaction_uses_documented_endpoint_and_headers(self):
        api = PlategaAPI(merchant_id="merchant-test", api_key="api-test")
        api._request = AsyncMock(return_value={
            "transactionId": "3fa85f64-5717-4562-b3fc-2c463f66afa6",
            "redirect": "https://pay.platega.io/qrsbp",
            "status": "PENDING",
            "expiresIn": "00:15:00",
        })

        result = await api.create_sbp_transaction(
            amount=Decimal("155.00"),
            currency="RUB",
            intent_id="intent-test-123",
            user_id=730001,
            user_name="@buyer",
            return_url="https://t.me/your_store_bot",
            failed_url="https://t.me/your_store_bot",
        )

        api._request.assert_awaited_once()
        method, path = api._request.await_args.args
        body = api._request.await_args.kwargs["json_body"]
        assert (method, path) == ("POST", "/transaction/process")
        assert body["paymentMethod"] == 2
        assert body["paymentDetails"] == {"amount": 155, "currency": "RUB"}
        assert body["payload"] == "intent-test-123"
        assert body["orderId"] == "intent-test-123"
        assert body["metadata"] == {"userId": "730001", "userName": "@buyer"}
        assert result["redirect"] == "https://pay.platega.io/qrsbp"

    async def test_rejects_non_platega_payment_redirect(self):
        api = PlategaAPI(merchant_id="merchant-test", api_key="api-test")
        api._request = AsyncMock(return_value={
            "transactionId": "3fa85f64-5717-4562-b3fc-2c463f66afa6",
            "redirect": "https://attacker.example/pay",
            "status": "PENDING",
        })

        with pytest.raises(ValueError, match="payment URL"):
            await api.create_sbp_transaction(
                amount=Decimal("155"), currency="RUB", intent_id="intent-test-123",
                user_id=730001, user_name="730001",
                return_url="https://t.me/your_store_bot",
                failed_url="https://t.me/your_store_bot",
            )

    async def test_rejects_non_rub_sbp_before_api_request(self):
        api = PlategaAPI(merchant_id="merchant-test", api_key="api-test")
        api._request = AsyncMock()

        with pytest.raises(ValueError, match="requires RUB"):
            await api.create_sbp_transaction(
                amount=Decimal("155"), currency="USD", intent_id="intent-test-123",
                user_id=730001, user_name="730001",
                return_url="https://t.me/your_store_bot",
                failed_url="https://t.me/your_store_bot",
            )

        api._request.assert_not_awaited()


class TestPlategaCallback:

    @pytest.fixture(autouse=True)
    def platega_credentials(self, monkeypatch):
        monkeypatch.setattr(EnvKeys, "PLATEGA_MERCHANT_ID", "merchant-test", raising=False)
        monkeypatch.setattr(EnvKeys, "PLATEGA_API_KEY", "api-test-secret", raising=False)
        monkeypatch.setattr(EnvKeys, "PAY_CURRENCY", "RUB", raising=False)
        monkeypatch.setattr(EnvKeys, "REFERRAL_PERCENT", 0, raising=False)

    @staticmethod
    def _headers(secret="api-test-secret"):
        return {"X-MerchantId": "merchant-test", "X-Secret": secret}

    @staticmethod
    def _event(intent_id, *, status="CONFIRMED", amount=250, currency="RUB"):
        return {
            "id": str(uuid4()),
            "amount": amount,
            "currency": currency,
            "status": status,
            "paymentMethod": 2,
            "payload": intent_id,
        }

    async def _process_callback(self, event, *, commission=0, headers=None):
        provider_status = {
            "id": event["id"],
            "status": event["status"],
            "paymentDetails": {
                "amount": event["amount"],
                "currency": event["currency"],
            },
            "paymentMethod": "SBPQR",
            "comission": commission,
            "payload": event.get("payload"),
        }
        with patch("bot.misc.services.platega.PlategaAPI") as api_class:
            api_class.return_value.get_transaction = AsyncMock(
                return_value=provider_status
            )
            return await process_platega_callback(
                headers or self._headers(), event
            )

    async def test_confirmed_callback_credits_matching_intent_only_once(self, user_factory):
        user_id = 730002
        intent_id = "intent-platega-confirmed"
        await user_factory(telegram_id=user_id, balance=0)
        await create_pending_payment("platega", intent_id, user_id, 250, "RUB")
        event = self._event(intent_id)

        first = await self._process_callback(event)
        duplicate = await self._process_callback(event)
        third_delivery = await self._process_callback(event)

        assert first.credited is True
        assert duplicate.credited is False
        assert duplicate.outcome == "duplicate"
        assert third_delivery.credited is False
        assert third_delivery.outcome == "duplicate"
        assert (await check_user(user_id))["balance"] == Decimal("250.00")

    async def test_documented_callback_without_payload_uses_bound_transaction_id(
        self, user_factory
    ):
        user_id = 730006
        intent_id = "intent-platega-no-payload"
        transaction_id = str(uuid4())
        await user_factory(telegram_id=user_id, balance=0)
        await create_pending_payment("platega", intent_id, user_id, 250, "RUB")
        assert await bind_pending_payment("platega", intent_id, transaction_id)
        event = {
            "id": transaction_id,
            "amount": 250,
            "currency": "RUB",
            "status": "CONFIRMED",
            "paymentMethod": 2,
        }

        result = await self._process_callback(event)

        assert result.credited is True
        assert (await check_user(user_id))["balance"] == Decimal("250.00")

    async def test_callback_with_wrong_secret_does_not_bind_or_credit(self, user_factory):
        user_id = 730003
        intent_id = "intent-platega-bad-secret"
        await user_factory(telegram_id=user_id, balance=0)
        await create_pending_payment("platega", intent_id, user_id, 250, "RUB")

        with pytest.raises(PlategaCallbackError) as error:
            await self._process_callback(
                self._event(intent_id), headers=self._headers("wrong-secret")
            )

        assert error.value.status_code == 401
        assert (await check_user(user_id))["balance"] == Decimal("0.00")

    async def test_amount_mismatch_is_rejected_without_credit(self, user_factory):
        user_id = 730004
        intent_id = "intent-platega-wrong-amount"
        await user_factory(telegram_id=user_id, balance=0)
        await create_pending_payment("platega", intent_id, user_id, 250, "RUB")

        with pytest.raises(PlategaCallbackError) as error:
            await self._process_callback(self._event(intent_id, amount=251))

        assert error.value.status_code == 400
        assert (await check_user(user_id))["balance"] == Decimal("0.00")

    async def test_canceled_callback_marks_payment_failed_without_credit(self, user_factory):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Payments

        user_id = 730005
        intent_id = "intent-platega-canceled"
        await user_factory(telegram_id=user_id, balance=0)
        await create_pending_payment("platega", intent_id, user_id, 250, "RUB")
        event = self._event(intent_id, status="CANCELED")

        result = await self._process_callback(event)

        assert result.credited is False
        assert (await check_user(user_id))["balance"] == Decimal("0.00")
        async with Database().session() as session:
            payment = (await session.execute(
                select(Payments).where(
                    Payments.provider == "platega",
                    Payments.external_id == event["id"],
                )
            )).scalars().one()
            assert payment.status == "failed"

    async def test_chargeback_is_audited_and_flagged_for_manual_balance_review(
        self, user_factory
    ):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Payments

        user_id = 730007
        intent_id = "intent-platega-chargeback"
        await user_factory(telegram_id=user_id, balance=0)
        await create_pending_payment("platega", intent_id, user_id, 250, "RUB")
        event = self._event(intent_id, status="CONFIRMED")
        await self._process_callback(event)

        event["status"] = "CHARGEBACK"
        result = await self._process_callback(event)

        assert result.outcome == "chargebacked"
        # Do not silently overdraw a user's wallet: the owner alert/audit record
        # requests manual reconciliation for the refunded provider transaction.
        assert (await check_user(user_id))["balance"] == Decimal("250.00")
        async with Database().session() as session:
            payment = (await session.execute(
                select(Payments).where(
                    Payments.provider == "platega",
                    Payments.external_id == event["id"],
                )
            )).scalars().one()
            assert payment.status == "chargebacked"

    async def test_confirmed_payment_with_provider_fee_credits_requested_amount(
        self, user_factory
    ):
        from bot.database.methods import bind_pending_payment

        user_id = 730008
        intent_id = "intent-platega-with-fee"
        transaction_id = str(uuid4())
        await user_factory(telegram_id=user_id, balance=0)
        await create_pending_payment("platega", intent_id, user_id, 20, "RUB")
        await bind_pending_payment("platega", intent_id, transaction_id)
        event = {
            "id": transaction_id,
            "amount": Decimal("21.70"),
            "currency": "RUB",
            "status": "CONFIRMED",
            "paymentMethod": 2,
        }

        result = await self._process_callback(event, commission=Decimal("1.70"))

        assert result.credited is True
        assert result.amount == Decimal("20.00")
        assert (await check_user(user_id))["balance"] == Decimal("20.00")

    async def test_callback_gross_amount_must_match_provider_status(
        self, user_factory
    ):
        user_id = 730009
        intent_id = "intent-platega-provider-amount-mismatch"
        await user_factory(telegram_id=user_id, balance=0)
        await create_pending_payment("platega", intent_id, user_id, 20, "RUB")
        event = self._event(intent_id, amount=Decimal("21.70"))

        with patch("bot.misc.services.platega.PlategaAPI") as api_class:
            api_class.return_value.get_transaction = AsyncMock(return_value={
                "id": event["id"],
                "status": "CONFIRMED",
                "paymentDetails": {"amount": Decimal("22.00"), "currency": "RUB"},
                "paymentMethod": "SBPQR",
                "comission": Decimal("2.00"),
                "payload": event["payload"],
            })
            with pytest.raises(PlategaCallbackError):
                await process_platega_callback(self._headers(), event)

        assert (await check_user(user_id))["balance"] == Decimal("0.00")

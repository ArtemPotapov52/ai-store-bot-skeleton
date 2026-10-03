import asyncio
from decimal import Decimal

from sqlalchemy import select, func

from bot.database.main import Database
import pytest

from bot.database.methods.transactions import buy_item_transaction, \
    process_payment_with_referral, \
    admin_balance_change, checkout_cart_transaction
from bot.database.methods.create import create_pending_payment, bind_pending_payment, add_to_cart
from bot.handlers.user.cart import _receipt_total
from bot.database.models.main import BoughtGoods, ItemValues, Goods, Payments, Operations, ReferralEarnings, User, \
    CartItems, Categories, PromoCodes, PromoCodeUsages


async def _get_balance(telegram_id: int) -> float:
    """Read user balance directly from DB to avoid cache issues."""
    async with Database().session() as s:
        result = await s.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalars().one()
        return float(user.balance)


class TestBuyItemTransaction:

    async def test_buy_item_accepts_legacy_edge_space_name(
            self, user_factory, category_factory
    ):
        """A legacy product with a trailing space must remain purchasable."""
        await user_factory(telegram_id=100098, balance=500)
        await category_factory("LegacyK12")

        async with Database().session() as s:
            category_id = (await s.execute(
                select(Categories.id).where(Categories.name == "LegacyK12")
            )).scalar_one()
            goods = Goods(
                name="ChatGPT K12 EDU 2 ГОДА ",
                price=Decimal("100"),
                description="legacy product",
                category_id=category_id,
            )
            s.add(goods)
            await s.flush()
            s.add(ItemValues(item_id=goods.id, value="k12-account", is_infinity=False))

        success, message, data = await buy_item_transaction(
            100098, "ChatGPT K12 EDU 2 ГОДА"
        )

        assert (success, message) == (True, "success")
        assert data["item_name"] == "ChatGPT K12 EDU 2 ГОДА"
        assert await _get_balance(100098) == 400.0

    async def test_concurrent_repeated_purchases_cannot_overdraw_stock_or_balance(
            self, user_factory, item_factory
    ):
        await user_factory(telegram_id=100099, balance=600)
        await item_factory(
            name="ConcurrentStock",
            price=100,
            values=[("v1", False), ("v2", False), ("v3", False)],
        )

        results = await asyncio.gather(*(
            buy_item_transaction(100099, "ConcurrentStock") for _ in range(6)
        ))

        successes = [result for result in results if result[0]]
        failures = [result for result in results if not result[0]]
        assert len(successes) == 3
        assert all(result[1] == "out_of_stock" for result in failures)
        assert await _get_balance(100099) == 300.0

        async with Database().session() as s:
            bought = (await s.execute(
                select(BoughtGoods).where(BoughtGoods.buyer_id == 100099)
            )).scalars().all()
            assert len(bought) == 3

    async def test_counted_stock_decreases_after_each_purchase(self, user_factory, item_factory):
        await user_factory(telegram_id=100000, balance=500)
        delivery_text = "Логин: demo@example.com\nПароль: change-me"
        await item_factory(
            name="CountedStock", price=100, stock_quantity=2,
            delivery_text=delivery_text,
        )

        first_ok, first_code, first = await buy_item_transaction(100000, "CountedStock")
        second_ok, second_code, second = await buy_item_transaction(100000, "CountedStock")
        third_ok, third_code, third = await buy_item_transaction(100000, "CountedStock")

        assert (first_ok, first_code, first["value"]) == (True, "success", delivery_text)
        assert (second_ok, second_code, second["value"]) == (True, "success", delivery_text)
        assert (third_ok, third_code, third) == (False, "out_of_stock", None)

        async with Database().session() as session:
            goods = (await session.execute(
                select(Goods).where(Goods.name == "CountedStock")
            )).scalars().one()
        assert goods.stock_quantity == 0

    async def test_buy_item_success(self, user_factory, item_factory):
        await user_factory(telegram_id=100001, balance=500)
        await item_factory(name="Widget", price=100, values=[("val1", False)])

        success, msg, data = await buy_item_transaction(100001, "Widget")

        assert success is True
        assert msg == "success"
        assert data is not None
        assert data["item_name"] == "Widget"
        assert data["value"] == "val1"
        assert data["price"] == 100.0
        assert data["new_balance"] == 400.0

        # Verify DB state
        assert await _get_balance(100001) == 400.0

        async with Database().session() as s:
            bought = (await s.execute(select(BoughtGoods).where(
                BoughtGoods.buyer_id == 100001
            ))).scalars().all()
            assert len(bought) == 1
            assert bought[0].item_name == "Widget"
            assert bought[0].value == "val1"
            assert float(bought[0].price) == 100.0

            widget = (await s.execute(select(Goods).where(Goods.name == "Widget"))).scalars().first()
            iv_count = (await s.execute(select(func.count()).select_from(ItemValues).where(
                ItemValues.item_id == widget.id
            ))).scalar()
            assert iv_count == 0

    async def test_buy_item_insufficient_funds(self, user_factory, item_factory):
        await user_factory(telegram_id=100002, balance=50)
        await item_factory(name="Expensive", price=100, values=[("val1", False)])

        success, msg, data = await buy_item_transaction(100002, "Expensive")

        assert success is False
        assert msg == "insufficient_funds"
        assert data is None

        # Balance unchanged
        assert await _get_balance(100002) == 50.0

    async def test_buy_item_out_of_stock(self, user_factory, item_factory):
        await user_factory(telegram_id=100003, balance=500)
        # Item exists but no values
        await item_factory(name="Empty", price=100, values=None)

        success, msg, data = await buy_item_transaction(100003, "Empty")

        assert success is False
        assert msg == "out_of_stock"
        assert data is None

    async def test_buy_item_user_not_found(self, item_factory):
        await item_factory(name="Gadget", price=100, values=[("val1", False)])

        success, msg, data = await buy_item_transaction(999999, "Gadget")

        assert success is False
        assert msg == "user_not_found"
        assert data is None

    async def test_buy_item_item_not_found(self, user_factory):
        await user_factory(telegram_id=100004, balance=500)

        success, msg, data = await buy_item_transaction(100004, "NonExistent")

        assert success is False
        assert msg == "item_not_found"
        assert data is None

    async def test_buy_item_infinite_stock(self, user_factory, item_factory):
        await user_factory(telegram_id=100005, balance=500)
        await item_factory(name="InfItem", price=100, values=[("infinite_val", True)])

        success, msg, data = await buy_item_transaction(100005, "InfItem")

        assert success is True
        assert msg == "success"
        assert data["value"] == "infinite_val"
        assert data["new_balance"] == 400.0

        assert await _get_balance(100005) == 400.0

        # ItemValues should still exist
        async with Database().session() as s:
            inf_item = (await s.execute(select(Goods).where(Goods.name == "InfItem"))).scalars().first()
            iv_count = (await s.execute(select(func.count()).select_from(ItemValues).where(
                ItemValues.item_id == inf_item.id
            ))).scalar()
            assert iv_count == 1

    async def test_buy_item_multiple_purchases(self, user_factory, item_factory):
        await user_factory(telegram_id=100006, balance=1000)
        await item_factory(
            name="Multi",
            price=100,
            values=[("v1", False), ("v2", False), ("v3", False)],
        )

        purchased_values = []
        for _ in range(3):
            success, msg, data = await buy_item_transaction(100006, "Multi")
            assert success is True
            assert msg == "success"
            purchased_values.append(data["value"])

        # All three values should have been purchased
        assert sorted(purchased_values) == ["v1", "v2", "v3"]

        # Fourth attempt should be out of stock
        success, msg, data = await buy_item_transaction(100006, "Multi")
        assert success is False
        assert msg == "out_of_stock"

        # Verify balance: 1000 - 3*100 = 700
        assert await _get_balance(100006) == 700.0

        # All ItemValues gone
        async with Database().session() as s:
            multi = (await s.execute(select(Goods).where(Goods.name == "Multi"))).scalars().first()
            iv_count = (await s.execute(select(func.count()).select_from(ItemValues).where(
                ItemValues.item_id == multi.id
            ))).scalar()
            assert iv_count == 0

    async def test_buy_item_exact_balance(self, user_factory, item_factory):
        await user_factory(telegram_id=100007, balance=100)
        await item_factory(name="Exact", price=100, values=[("exactval", False)])

        success, msg, data = await buy_item_transaction(100007, "Exact")

        assert success is True
        assert msg == "success"
        assert data["new_balance"] == 0.0

        assert await _get_balance(100007) == 0.0


class TestProcessPaymentWithReferral:

    async def test_payment_intent_is_created_before_provider_id_is_bound(self, user_factory):
        await user_factory(telegram_id=200097, balance=0)
        await create_pending_payment("intent_provider", "intent:abc", 200097, 150, "RUB")

        assert await bind_pending_payment("intent_provider", "intent:abc", "provider-150") is True
        async with Database().session() as s:
            intent = (await s.execute(select(Payments).where(
                Payments.provider == "intent_provider",
                Payments.external_id == "provider-150",
            ))).scalars().one()
            assert intent.status == "pending"
            assert intent.user_id == 200097
            assert intent.amount == Decimal("150")

    async def test_concurrent_repeated_provider_callback_credits_once(self, user_factory):
        await user_factory(telegram_id=200098, balance=0)

        results = await asyncio.gather(*(
            process_payment_with_referral(
                user_id=200098,
                amount=Decimal("100"),
                provider="same_provider",
                external_id="same_external_id",
            ) for _ in range(5)
        ))

        assert sum(1 for ok, _message in results if ok) == 1
        assert sum(1 for ok, message in results if not ok and message == "already_processed") == 4
        assert await _get_balance(200098) == 100.0

    async def test_processed_payment_still_requires_exact_identity(self, user_factory):
        await user_factory(telegram_id=200101, balance=0)
        await user_factory(telegram_id=200102, balance=0)

        first, first_msg = await process_payment_with_referral(
            user_id=200101,
            amount=Decimal("100"),
            provider="identity_provider",
            external_id="identity_external_id",
            currency="RUB",
        )
        wrong_user, wrong_user_msg = await process_payment_with_referral(
            user_id=200102,
            amount=Decimal("100"),
            provider="identity_provider",
            external_id="identity_external_id",
            currency="RUB",
        )
        wrong_currency, wrong_currency_msg = await process_payment_with_referral(
            user_id=200101,
            amount=Decimal("100"),
            provider="identity_provider",
            external_id="identity_external_id",
            currency="USD",
        )

        assert (first, first_msg) == (True, "success")
        assert (wrong_user, wrong_user_msg) == (False, "payment_mismatch")
        assert (wrong_currency, wrong_currency_msg) == (False, "payment_mismatch")
        assert await _get_balance(200101) == 100.0
        assert await _get_balance(200102) == 0.0

    async def test_existing_pending_payment_is_bound_to_user_and_exact_amount(self, user_factory):
        await user_factory(telegram_id=200099, balance=0)
        await user_factory(telegram_id=200100, balance=0)
        await create_pending_payment("prov_bind", "ext_bind", 200099, 250, "RUB")

        wrong_user, wrong_user_msg = await process_payment_with_referral(
            user_id=200100,
            amount=Decimal("250"),
            provider="prov_bind",
            external_id="ext_bind",
        )
        wrong_amount, wrong_amount_msg = await process_payment_with_referral(
            user_id=200099,
            amount=Decimal("249"),
            provider="prov_bind",
            external_id="ext_bind",
        )

        assert (wrong_user, wrong_user_msg) == (False, "payment_mismatch")
        assert (wrong_amount, wrong_amount_msg) == (False, "payment_mismatch")
        assert await _get_balance(200099) == 0.0
        assert await _get_balance(200100) == 0.0

    async def test_payment_success(self, user_factory):
        await user_factory(telegram_id=200001, balance=0)

        success, msg = await process_payment_with_referral(
            user_id=200001,
            amount=Decimal("500"),
            provider="test_provider",
            external_id="ext_001",
        )

        assert success is True
        assert msg == "success"

        # Balance increased
        assert await _get_balance(200001) == 500.0

        # Payment record created
        async with Database().session() as s:
            payment = (await s.execute(select(Payments).where(
                Payments.external_id == "ext_001"
            ))).scalars().first()
            assert payment is not None
            assert payment.status == "succeeded"
            assert float(payment.amount) == 500.0
            assert payment.provider == "test_provider"

            # Operation record created
            ops = (await s.execute(select(Operations).where(
                Operations.user_id == 200001
            ))).scalars().all()
            assert len(ops) == 1
            assert float(ops[0].operation_value) == 500.0

    async def test_payment_idempotency(self, user_factory):
        await user_factory(telegram_id=200002, balance=0)

        # First call succeeds
        success1, msg1 = await process_payment_with_referral(
            user_id=200002,
            amount=Decimal("300"),
            provider="prov_a",
            external_id="ext_dup",
        )
        assert success1 is True
        assert msg1 == "success"

        # Second call with same provider+external_id
        success2, msg2 = await process_payment_with_referral(
            user_id=200002,
            amount=Decimal("300"),
            provider="prov_a",
            external_id="ext_dup",
        )
        assert success2 is False
        assert msg2 == "already_processed"

        # Balance only credited once
        assert await _get_balance(200002) == 300.0

    async def test_payment_with_referral_bonus(self, user_factory):
        # Create referrer first
        await user_factory(telegram_id=200010, balance=0)
        # Create user with referrer
        await user_factory(telegram_id=200003, balance=0, referral_id=200010)

        success, msg = await process_payment_with_referral(
            user_id=200003,
            amount=Decimal("100"),
            provider="prov_ref",
            external_id="ext_ref_001",
            referral_percent=10,
        )

        assert success is True
        assert msg == "success"

        # User got 100
        assert await _get_balance(200003) == 100.0

        # Referrer got 10 (10% of 100)
        assert await _get_balance(200010) == 10.0

        # ReferralEarnings record created
        async with Database().session() as s:
            earnings = (await s.execute(select(ReferralEarnings).where(
                ReferralEarnings.referrer_id == 200010,
                ReferralEarnings.referral_id == 200003,
            ))).scalars().all()
            assert len(earnings) == 1
            assert float(earnings[0].amount) == 10.0
            assert float(earnings[0].original_amount) == 100.0

    async def test_payment_no_referrer(self, user_factory):
        # User without referral_id
        await user_factory(telegram_id=200004, balance=0)

        success, msg = await process_payment_with_referral(
            user_id=200004,
            amount=Decimal("200"),
            provider="prov_noref",
            external_id="ext_noref",
            referral_percent=10,
        )

        assert success is True
        assert msg == "success"

        # No referral earnings created
        async with Database().session() as s:
            earnings = (await s.execute(select(func.count()).select_from(ReferralEarnings).where(
                ReferralEarnings.referral_id == 200004
            ))).scalar()
            assert earnings == 0

    async def test_payment_zero_percent(self, user_factory):
        # Create referrer
        await user_factory(telegram_id=200020, balance=0)
        # Create user with referrer
        await user_factory(telegram_id=200005, balance=0, referral_id=200020)

        success, msg = await process_payment_with_referral(
            user_id=200005,
            amount=Decimal("100"),
            provider="prov_zero",
            external_id="ext_zero",
            referral_percent=0,
        )

        assert success is True
        assert msg == "success"

        # Referrer balance unchanged
        assert await _get_balance(200020) == 0.0

        # No referral earnings
        async with Database().session() as s:
            earnings = (await s.execute(select(func.count()).select_from(ReferralEarnings).where(
                ReferralEarnings.referrer_id == 200020
            ))).scalar()
            assert earnings == 0

    async def test_payment_existing_pending(self, user_factory):
        await user_factory(telegram_id=200006, balance=0)

        # Create a pending payment first
        await create_pending_payment(
            provider="prov_pend",
            external_id="ext_pend",
            user_id=200006,
            amount=250,
            currency="RUB",
        )

        # Verify it exists as pending
        async with Database().session() as s:
            p = (await s.execute(select(Payments).where(
                Payments.provider == "prov_pend",
                Payments.external_id == "ext_pend",
            ))).scalars().first()
            assert p is not None
            assert p.status == "pending"

        # Now process it
        success, msg = await process_payment_with_referral(
            user_id=200006,
            amount=Decimal("250"),
            provider="prov_pend",
            external_id="ext_pend",
        )

        assert success is True
        assert msg == "success"

        # Status updated to succeeded
        async with Database().session() as s:
            p = (await s.execute(select(Payments).where(
                Payments.provider == "prov_pend",
                Payments.external_id == "ext_pend",
            ))).scalars().first()
            assert p.status == "succeeded"

        # Balance credited
        assert await _get_balance(200006) == 250.0

    async def test_payment_large_amount(self, user_factory):
        await user_factory(telegram_id=200007, balance=0)

        success, msg = await process_payment_with_referral(
            user_id=200007,
            amount=Decimal("99999"),
            provider="prov_large",
            external_id="ext_large",
        )

        assert success is True
        assert msg == "success"

        assert await _get_balance(200007) == 99999.0

        # Verify Decimal precision in payment record
        async with Database().session() as s:
            payment = (await s.execute(select(Payments).where(
                Payments.external_id == "ext_large"
            ))).scalars().first()
            assert payment is not None
            assert float(payment.amount) == 99999.0


class TestAdminBalanceChange:

    async def test_topup_success(self, user_factory):
        await user_factory(telegram_id=300001, balance=100)

        success, msg = await admin_balance_change(300001, 500)

        assert success is True
        assert msg == "success"
        assert await _get_balance(300001) == 600.0

        # Operation record created
        async with Database().session() as s:
            ops = (await s.execute(select(Operations).where(
                Operations.user_id == 300001
            ))).scalars().all()
            assert len(ops) == 1
            assert float(ops[0].operation_value) == 500.0

    async def test_deduct_success(self, user_factory):
        await user_factory(telegram_id=300002, balance=500)

        success, msg = await admin_balance_change(300002, -200)

        assert success is True
        assert msg == "success"
        assert await _get_balance(300002) == 300.0

        # Operation record created with negative value
        async with Database().session() as s:
            ops = (await s.execute(select(Operations).where(
                Operations.user_id == 300002
            ))).scalars().all()
            assert len(ops) == 1
            assert float(ops[0].operation_value) == -200.0

    async def test_deduct_insufficient_funds(self, user_factory):
        await user_factory(telegram_id=300003, balance=100)

        success, msg = await admin_balance_change(300003, -200)
        assert success is False
        assert msg == "insufficient_funds"

        # Balance unchanged
        assert await _get_balance(300003) == 100.0

        # No operation record created
        async with Database().session() as s:
            ops = (await s.execute(select(Operations).where(
                Operations.user_id == 300003
            ))).scalars().all()
            assert len(ops) == 0

    async def test_deduct_exact_balance(self, user_factory):
        await user_factory(telegram_id=300004, balance=500)

        success, msg = await admin_balance_change(300004, -500)

        assert success is True
        assert await _get_balance(300004) == 0.0

    async def test_user_not_found(self):
        success, msg = await admin_balance_change(999888, 100)

        assert success is False
        assert msg == "user_not_found"

    async def test_topup_and_deduct_atomic(self, user_factory):
        """Verify that balance and operation are created atomically."""
        await user_factory(telegram_id=300005, balance=1000)

        await admin_balance_change(300005, 500)
        await admin_balance_change(300005, -300)

        assert await _get_balance(300005) == 1200.0

        async with Database().session() as s:
            ops = (await s.execute(select(Operations).where(
                Operations.user_id == 300005
            ).order_by(Operations.id))).scalars().all()
            assert len(ops) == 2
            assert float(ops[0].operation_value) == 500.0
            assert float(ops[1].operation_value) == -300.0


async def _make_promo(code, discount_type="percent", value="10", *, category_id=None):
    async with Database().session() as s:
        s.add(PromoCodes(
            code=code.upper(), discount_type=discount_type,
            discount_value=Decimal(str(value)), max_uses=0,
            current_uses=0, is_active=True, category_id=category_id,
        ))


async def _cart_rows(user_id: int) -> int:
    async with Database().session() as s:
        return (await s.execute(
            select(func.count()).select_from(CartItems).where(CartItems.user_id == user_id)
        )).scalar()


async def _stock_rows(item_name: str) -> int:
    async with Database().session() as s:
        goods = (await s.execute(select(Goods).where(Goods.name == item_name))).scalars().first()
        return (await s.execute(
            select(func.count()).select_from(ItemValues).where(ItemValues.item_id == goods.id)
        )).scalar()


class TestCheckoutCartTransaction:
    async def test_counted_stock_cart_decreases_by_selected_quantity(self, user_factory, item_factory):
        await user_factory(telegram_id=400000, balance=500)
        await item_factory(
            name="Counted cart", price=90, stock_quantity=3,
            delivery_text="Общие данные для выдачи",
        )
        await add_to_cart(400000, "Counted cart", quantity=2)

        success, message, results = await checkout_cart_transaction(400000)

        assert (success, message) == (True, "success")
        assert len(results) == 2
        assert [result["value"] for result in results] == [
            "Общие данные для выдачи", "Общие данные для выдачи",
        ]
        assert await _get_balance(400000) == 320.0

        async with Database().session() as session:
            goods = (await session.execute(
                select(Goods).where(Goods.name == "Counted cart")
            )).scalars().one()
        assert goods.stock_quantity == 1

    async def test_quantity_delivers_one_row_per_unit(self, user_factory, item_factory):
        await user_factory(telegram_id=400001, balance=500)
        await item_factory(name="Qty3", price=50,
                           values=[("a", False), ("b", False), ("c", False)])
        await add_to_cart(400001, "Qty3", quantity=3)

        success, msg, results = await checkout_cart_transaction(400001)

        assert (success, msg) == (True, "success")
        assert len(results) == 3
        assert {r["value"] for r in results} == {"a", "b", "c"}   # distinct values
        assert await _get_balance(400001) == 350.0                # 500 - 3*50
        assert await _stock_rows("Qty3") == 0
        assert await _cart_rows(400001) == 0

        async with Database().session() as s:
            bought = (await s.execute(
                select(BoughtGoods).where(BoughtGoods.buyer_id == 400001)
            )).scalars().all()
            assert len(bought) == 3
            assert sum(float(b.price) for b in bought) == 150.0

    async def test_partial_stock_aborts_and_changes_nothing(self, user_factory, item_factory):
        await user_factory(telegram_id=400002, balance=500)
        await item_factory(name="Qty2of3", price=50, values=[("a", False), ("b", False)])
        await add_to_cart(400002, "Qty2of3", quantity=3)

        success, msg, results = await checkout_cart_transaction(400002)

        assert (success, msg, results) == (False, "out_of_stock", None)
        # The abort must be atomic: balance, stock and cart all untouched.
        assert await _get_balance(400002) == 500.0
        assert await _stock_rows("Qty2of3") == 2
        assert await _cart_rows(400002) == 1

    async def test_infinite_value_serves_every_unit(self, user_factory, item_factory):
        await user_factory(telegram_id=400003, balance=500)
        await item_factory(name="QtyInf", price=20, values=[("forever", True)])
        await add_to_cart(400003, "QtyInf", quantity=5)

        success, msg, results = await checkout_cart_transaction(400003)

        assert (success, msg) == (True, "success")
        assert len(results) == 5
        assert [r["value"] for r in results] == ["forever"] * 5
        assert await _get_balance(400003) == 400.0     # 500 - 5*20
        assert await _stock_rows("QtyInf") == 1        # infinite row is not consumed

    async def test_zero_stock_drops_the_line(self, user_factory, item_factory):
        """No stock at all drops the line; the rest of the cart still buys."""
        await user_factory(telegram_id=400004, balance=500)
        await item_factory(name="QtyGone", price=50, values=[])
        await item_factory(name="QtyOk", price=50, values=[("a", False)])
        await add_to_cart(400004, "QtyGone")
        await add_to_cart(400004, "QtyOk")

        success, msg, results = await checkout_cart_transaction(400004)

        assert (success, msg) == (True, "success")
        assert [r["item_name"] for r in results] == ["QtyOk"]
        assert await _get_balance(400004) == 450.0

    async def test_insufficient_funds_for_quantity(self, user_factory, item_factory):
        await user_factory(telegram_id=400005, balance=100)
        await item_factory(name="QtyRich", price=50,
                           values=[("a", False), ("b", False), ("c", False)])
        await add_to_cart(400005, "QtyRich", quantity=3)

        success, msg, results = await checkout_cart_transaction(400005)

        assert (success, msg, results) == (False, "insufficient_funds", None)
        assert await _get_balance(400005) == 100.0
        assert await _stock_rows("QtyRich") == 3

    async def test_percent_promo_scales_per_unit(self, user_factory, item_factory):
        await user_factory(telegram_id=400006, balance=500)
        await item_factory(name="QtyPct", price=100, values=[("a", False), ("b", False)])
        await _make_promo("PCT10", "percent", 10)
        await add_to_cart(400006, "QtyPct", promo_code="PCT10", quantity=2)

        success, msg, results = await checkout_cart_transaction(400006)

        assert (success, msg) == (True, "success")
        # 10% off each unit: 90 * 2 = 180
        assert await _get_balance(400006) == 320.0
        assert sum(r["price"] for r in results) == 180.0

    async def test_fixed_promo_applies_once_per_line(self, user_factory, item_factory):
        await user_factory(telegram_id=400007, balance=500)
        await item_factory(name="QtyFix", price=100, values=[("a", False), ("b", False)])
        await _make_promo("FIX30", "fixed", 30)
        await add_to_cart(400007, "QtyFix", promo_code="FIX30", quantity=2)

        success, msg, results = await checkout_cart_transaction(400007)

        assert (success, msg) == (True, "success")
        # 200 - 30 once = 170, NOT 200 - 60
        assert await _get_balance(400007) == 330.0
        assert sum(r["price"] for r in results) == 170.0

    async def test_fixed_promo_cannot_zero_the_cart(self, user_factory, item_factory):
        await user_factory(telegram_id=400008, balance=500)
        await item_factory(name="QtyFloor", price=10, values=[("a", False)])
        await _make_promo("FIX999", "fixed", 999)
        await add_to_cart(400008, "QtyFloor", promo_code="FIX999")

        success, msg, _results = await checkout_cart_transaction(400008)

        assert (success, msg) == (False, "promo_zero_total")
        assert await _get_balance(400008) == 500.0

    async def test_uneven_split_still_sums_to_charge(self, user_factory, item_factory):
        await user_factory(telegram_id=400009, balance=500)
        await item_factory(name="QtySplit", price=10,
                           values=[("a", False), ("b", False), ("c", False)])
        # 30 - 0.01 = 29.99 across 3 units -> 10.00 / 10.00 / 9.99
        await _make_promo("FIX001", "fixed", "0.01")
        await add_to_cart(400009, "QtySplit", promo_code="FIX001", quantity=3)

        success, msg, results = await checkout_cart_transaction(400009)

        assert (success, msg) == (True, "success")
        assert len(results) == 3
        assert sorted(r["price"] for r in results) == [9.99, 10.0, 10.0]
        assert _receipt_total(results) == Decimal("29.99")   # no cents lost or invented
        assert await _get_balance(400009) == round(500.0 - 29.99, 2)

        async with Database().session() as s:
            bought = (await s.execute(
                select(BoughtGoods).where(BoughtGoods.buyer_id == 400009)
            )).scalars().all()
            assert sum((b.price for b in bought), Decimal(0)) == Decimal("29.99")

    async def test_category_promo_recorded_once_across_lines(self, user_factory, item_factory,
                                                             category_factory):
        await user_factory(telegram_id=400010, balance=1000)
        await category_factory("PromoCat")
        await item_factory(name="PC1", price=50, category="PromoCat", values=[("a", False)])
        await item_factory(name="PC2", price=50, category="PromoCat", values=[("b", False)])

        async with Database().session() as s:
            cat_id = (await s.execute(
                select(Categories.id).where(Categories.name == "PromoCat")
            )).scalar()
        await _make_promo("CAT10", "percent", 10, category_id=cat_id)

        await add_to_cart(400010, "PC1", promo_code="CAT10")
        await add_to_cart(400010, "PC2", promo_code="CAT10")

        success, msg, results = await checkout_cart_transaction(400010)

        assert (success, msg) == (True, "success")
        assert len(results) == 2

        async with Database().session() as s:
            usages = (await s.execute(
                select(func.count()).select_from(PromoCodeUsages)
                .where(PromoCodeUsages.user_id == 400010)
            )).scalar()
            assert usages == 1
            promo = (await s.execute(
                select(PromoCodes).where(PromoCodes.code == "CAT10")
            )).scalars().one()
            assert promo.current_uses == 1

    async def test_empty_cart(self, user_factory):
        await user_factory(telegram_id=400011, balance=100)
        success, msg, results = await checkout_cart_transaction(400011)
        assert (success, msg, results) == (False, "cart_empty", None)

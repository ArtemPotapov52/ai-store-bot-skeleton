import asyncio
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class _TemplateStub:
    async def TemplateResponse(self, _request, _template, context):
        return context


class TestProductReminderFormatting:
    def test_blank_price_keeps_current_catalog_price(self):
        from bot.web.product_reminder import parse_reminder_price

        assert parse_reminder_price("", Decimal("99.90")) == (
            Decimal("99.90"), False
        )

    def test_parses_explicit_discount_price(self):
        from bot.web.product_reminder import parse_reminder_price

        assert parse_reminder_price("79,50", Decimal("99.90")) == (
            Decimal("79.50"), True
        )

    @pytest.mark.parametrize(
        "raw_price", ["0", "-1", "NaN", "inf", "bad", "10000000000", "12.345"]
    )
    def test_rejects_invalid_or_out_of_bounds_prices(self, raw_price):
        from bot.web.product_reminder import ProductReminderError, parse_reminder_price

        with pytest.raises(ProductReminderError):
            parse_reminder_price(raw_price, Decimal("99.90"))

    def test_discount_message_escapes_product_html_and_strikes_old_price(self):
        from bot.web.product_reminder import build_reminder_text

        text = build_reminder_text(
            item_name="GPT <Plus>",
            quantity=7,
            is_infinite=False,
            old_price=Decimal("100"),
            new_price=Decimal("79.50"),
            currency="RUB",
        )

        assert "GPT &lt;Plus&gt;" in text
        assert "Товар ещё в наличии!" in text
        assert "Товар снова в наличии" not in text
        assert "В наличии: <b>7 шт.</b>" in text
        assert "<s>100 RUB</s>" in text
        assert "<b>79.5 RUB</b>" in text

    def test_regular_and_infinite_stock_price_format(self):
        from bot.web.product_reminder import build_reminder_text

        text = build_reminder_text(
            item_name="Product",
            quantity=1,
            is_infinite=True,
            old_price=Decimal("100"),
            new_price=Decimal("100"),
            currency="RUB",
        )

        assert "В наличии: <b>∞</b>" in text
        assert "<s>" not in text
        assert "100 RUB" in text

    def test_admin_preview_uses_the_current_in_stock_wording(self):
        from pathlib import Path

        template = (
            Path(__file__).parents[1] / "bot" / "web" / "templates" / "product_reminder.html"
        ).read_text(encoding="utf-8")

        assert "Товар ещё в наличии!" in template
        assert "Товар снова в наличии!" not in template


def _post_request(form, session=None):
    return SimpleNamespace(
        method="POST",
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
        form=AsyncMock(return_value=form),
        session=session if session is not None else {},
    )


class TestProductReminderView:
    def test_reminder_view_is_registered_inside_the_admin_panel(self):
        from sqladmin import BaseView, ModelView
        from starlette.routing import Mount

        import bot.web.admin as web_admin

        view_classes = [
            value for value in vars(web_admin).values()
            if isinstance(value, type)
            and (issubclass(value, BaseView) or issubclass(value, ModelView))
        ]
        previous_refs = {
            view: (hasattr(view, "_admin_ref"), getattr(view, "_admin_ref", None))
            for view in view_classes
        }
        try:
            app = web_admin.create_admin_app()
            admin_app = next(
                route.app for route in app.routes
                if isinstance(route, Mount) and route.path == "/admin"
            )

            assert any(route.path == "/product-reminder" for route in admin_app.routes)
        finally:
            for view, (had_admin_ref, previous_admin_ref) in previous_refs.items():
                if had_admin_ref:
                    view._admin_ref = previous_admin_ref
                elif hasattr(view, "_admin_ref"):
                    delattr(view, "_admin_ref")

    async def test_preview_does_not_change_catalog_or_schedule_broadcast(
        self, item_factory, mock_bot
    ):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Goods
        from bot.web.admin import ProductReminderView, set_notifier_bot

        await item_factory(name="Reminder preview", price=100, values=[("a", False), ("b", False)])
        async with Database().session() as session:
            item_id = (await session.execute(
                select(Goods.id).where(Goods.name == "Reminder preview")
            )).scalar_one()

        request = _post_request({
            "action": "preview", "item_id": str(item_id), "price": "75",
        })
        scheduled = []
        view = ProductReminderView()
        with patch.object(ProductReminderView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.safe_create_task", side_effect=scheduled.append):
            context = await view.product_reminder(request)

        assert context["preview"]["discount"] is True
        assert context["preview"]["old_price"] == "100"
        assert context["preview"]["new_price"] == "75"
        assert context["preview"]["quantity"] == "2 шт."
        assert scheduled == []
        assert request.session[ProductReminderView._session_key]["item_id"] == item_id
        async with Database().session() as session:
            assert (await session.execute(
                select(Goods.price).where(Goods.id == item_id)
            )).scalar_one() == Decimal("100.00")
        set_notifier_bot(None)

    async def test_confirm_updates_price_and_queues_one_linked_all_user_broadcast(
        self, item_factory, mock_bot
    ):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Goods
        from bot.web.admin import ProductReminderView, set_notifier_bot

        await item_factory(name="Reminder confirmed", price=100, values=[("one", False), ("two", False)])
        async with Database().session() as session:
            product = (await session.execute(
                select(Goods.id).where(Goods.name == "Reminder confirmed")
            )).scalar_one()
            item_id = product
            row = await session.get(Goods, item_id)
            row.sale_percent = Decimal("20")
            row.sale_until = datetime.now(timezone.utc) + timedelta(days=3)

        session_data = {}
        scheduled = []
        view = ProductReminderView()
        manager = SimpleNamespace(broadcast=AsyncMock(return_value=SimpleNamespace(
            total=2, sent=2, failed=0, blocked=0,
        )))
        with patch.object(ProductReminderView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.safe_create_task", side_effect=scheduled.append), \
                patch("bot.web.admin.get_all_users", new=AsyncMock(return_value=[(800001,), (800002,)])), \
                patch("bot.web.admin.product_purchase_link", new=AsyncMock(return_value="https://t.me/MyStore?start=item_42")), \
                patch("bot.web.admin.BroadcastManager", return_value=manager), \
                patch("bot.web.admin.invalidate_item_cache", new=AsyncMock()), \
                patch("bot.web.admin.invalidate_stats_cache", new=AsyncMock()), \
                patch("bot.web.admin.log_audit", new=AsyncMock()):
            set_notifier_bot(mock_bot)
            preview = await view.product_reminder(_post_request({
                "action": "preview", "item_id": str(item_id), "price": "75",
            }, session_data))
            assert preview["preview"]["old_price"] == "80"
            token = preview["preview"]["token"]
            context = await view.product_reminder(_post_request({
                "action": "send", "confirmation": token,
            }, session_data))
            for coroutine in scheduled:
                await coroutine
            set_notifier_bot(None)

        assert context["result"].endswith("для 2 пользователей.")
        async with Database().session() as session:
            product = await session.get(Goods, item_id)
            assert product.price == Decimal("75.00")
            assert product.sale_percent is None
            assert product.sale_until is None
        manager.broadcast.assert_awaited_once()
        args = manager.broadcast.await_args.kwargs
        assert args["user_ids"] == [800001, 800002]
        assert "<s>80 RUB</s> <b>75 RUB</b>" in args["text"]
        button = args["reply_markup"].inline_keyboard[0][0]
        assert button.text == "➡️ Перейти к товару"
        assert button.url == "https://t.me/MyStore?start=item_42"

    async def test_confirmation_token_is_required_and_does_not_change_price(
        self, item_factory
    ):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Goods
        from bot.web.admin import ProductReminderView

        await item_factory(name="Reminder token", price=100, values=[("one", False)])
        async with Database().session() as session:
            item_id = (await session.execute(
                select(Goods.id).where(Goods.name == "Reminder token")
            )).scalar_one()
        session_data = {
            ProductReminderView._session_key: {
                "token": "right-token", "item_id": item_id,
                "old_price": "100.00", "new_price": "50.00",
                "price_was_entered": True,
            }
        }
        view = ProductReminderView()
        with patch.object(ProductReminderView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.safe_create_task") as schedule, \
                patch("bot.web.admin.product_purchase_link", new=AsyncMock()) as product_link:
            context = await view.product_reminder(_post_request({
                "action": "send", "confirmation": "wrong-token",
            }, session_data))

        assert "предпросмотр устарел" in context["error"].lower()
        schedule.assert_not_called()
        product_link.assert_not_awaited()
        async with Database().session() as session:
            assert (await session.execute(
                select(Goods.price).where(Goods.id == item_id)
            )).scalar_one() == Decimal("100.00")

    async def test_out_of_stock_product_cannot_be_selected_for_reminder(self, item_factory):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Goods
        from bot.database.models.main import Permission
        from bot.web.admin import ProductReminderView

        await item_factory(name="Empty reminder product", price=100, values=[])
        async with Database().session() as session:
            item_id = (await session.execute(
                select(Goods.id).where(Goods.name == "Empty reminder product")
            )).scalar_one()
        request = _post_request({
            "action": "preview", "item_id": str(item_id), "price": "80",
        })
        view = ProductReminderView()
        with patch.object(ProductReminderView, "templates", _TemplateStub(), create=True):
            context = await view.product_reminder(request)

        assert context["preview"] is None
        assert "отсутствует на складе" in context["error"].lower()
        assert ProductReminderView.required_perm == Permission.CATALOG_MANAGE

    async def test_simultaneous_confirmations_start_only_one_broadcast(
        self, item_factory, mock_bot
    ):
        from sqlalchemy import select

        from bot.database.main import Database
        from bot.database.models.main import Goods
        from bot.web.admin import ProductReminderView, set_notifier_bot

        await item_factory(name="Reminder concurrent", price=100, values=[("one", False)])
        async with Database().session() as session:
            item_id = (await session.execute(
                select(Goods.id).where(Goods.name == "Reminder concurrent")
            )).scalar_one()

        users_started = asyncio.Event()
        continue_users = asyncio.Event()

        async def slow_get_users():
            users_started.set()
            await continue_users.wait()
            return [(800010,)]

        scheduled = []
        manager = SimpleNamespace(broadcast=AsyncMock(return_value=SimpleNamespace(
            total=1, sent=1, failed=0, blocked=0,
        )))
        view = ProductReminderView()
        set_notifier_bot(mock_bot)
        with patch.object(ProductReminderView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.safe_create_task", side_effect=scheduled.append), \
                patch("bot.web.admin.get_all_users", side_effect=slow_get_users), \
                patch("bot.web.admin.product_purchase_link", new=AsyncMock(return_value="https://t.me/MyStore?start=item_7")), \
                patch("bot.web.admin.BroadcastManager", return_value=manager), \
                patch("bot.web.admin.log_audit", new=AsyncMock()):
            first_session = {
                ProductReminderView._session_key: {
                    "token": "first", "item_id": item_id,
                    "old_price": "100.00", "new_price": "100.00",
                    "price_was_entered": False,
                }
            }
            first_task = asyncio.create_task(view.product_reminder(_post_request({
                "action": "send", "confirmation": "first",
            }, first_session)))
            await users_started.wait()

            second_session = {
                ProductReminderView._session_key: {
                    "token": "second", "item_id": item_id,
                    "old_price": "100.00", "new_price": "100.00",
                    "price_was_entered": False,
                }
            }
            second_context = await view.product_reminder(_post_request({
                "action": "send", "confirmation": "second",
            }, second_session))
            continue_users.set()
            first_context = await first_task
            for coroutine in scheduled:
                await coroutine
        set_notifier_bot(None)

        assert "другая рассылка уже выполняется" in second_context["error"].lower()
        assert first_context["result"].endswith("для 1 пользователей.")
        manager.broadcast.assert_awaited_once()

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from bot.web.stock_bulk import (
    MAX_BULK_VALUES,
    StockBulkError,
    parse_bulk_values,
)


class _TemplateStub:
    async def TemplateResponse(self, _request, _template, context):
        return context


class TestStockBulkParser:
    def test_newline_separator_keeps_each_account_as_one_lot(self):
        assert parse_bulk_values(
            "  first@example.com:one  \r\n\nsecond@example.com:two\n",
            "newline",
        ) == ["first@example.com:one", "second@example.com:two"]

    @pytest.mark.parametrize(
        ("separator", "source"),
        [
            ("dash", "first---second"),
            ("semicolon", "first; second"),
            ("pipe", "first | second"),
        ],
    )
    def test_supported_delimiters(self, separator, source):
        assert parse_bulk_values(source, separator) == ["first", "second"]

    def test_empty_parts_are_ignored_but_empty_upload_is_rejected(self):
        assert parse_bulk_values("first|| second|", "pipe") == ["first", "second"]
        with pytest.raises(StockBulkError, match="хотя бы один"):
            parse_bulk_values(" | \n", "pipe")

    def test_unknown_separator_is_rejected(self):
        with pytest.raises(StockBulkError, match="разделитель"):
            parse_bulk_values("first", "comma")

    def test_long_single_line_is_kept_intact_and_record_limit_remains(self):
        long_activation_url = "https://activate.example/" + ("x" * (2 * 1024 * 1024))
        assert parse_bulk_values(long_activation_url, "newline") == [long_activation_url]
        with pytest.raises(StockBulkError, match=str(MAX_BULK_VALUES)):
            parse_bulk_values("\n".join(f"account-{i}" for i in range(MAX_BULK_VALUES + 1)), "newline")


class TestStockBulkView:
    async def test_saves_each_record_and_reports_actual_count(self, item_factory, mock_bot):
        from bot.database.main import Database
        from bot.database.models.main import Goods, ItemValues
        from bot.web.admin import StockBulkView, set_notifier_bot

        await item_factory(name="BulkRoute", price=75, values=[])
        async with Database().session() as session:
            item_id = (await session.execute(
                select(Goods.id).where(Goods.name == "BulkRoute")
            )).scalar_one()
        request = SimpleNamespace(
            method="POST",
            client=SimpleNamespace(host="127.0.0.1"),
            headers={},
            form=AsyncMock(return_value={
                "item_id": str(item_id),
                "separator": "newline",
                "bulk_values": "one\ntwo\nthree",
                "notify_all": "1",
            }),
        )
        scheduled = []
        view = StockBulkView()
        with patch.object(StockBulkView, "templates", _TemplateStub(), create=True), \
                patch("bot.web.admin.safe_create_task", side_effect=scheduled.append), \
                patch("bot.web.admin.notify_restock", new_callable=AsyncMock) as restock, \
                patch("bot.web.admin.notify_owner_stock_added", new_callable=AsyncMock) as owner_notice:
            set_notifier_bot(mock_bot)
            try:
                context = await view.stock_bulk(request)
                for coroutine in scheduled:
                    await coroutine
            finally:
                set_notifier_bot(None)

        assert context["result"].startswith("Добавлено аккаунтов: 3")
        async with Database().session() as session:
            rows = (await session.execute(
                select(ItemValues.value).order_by(ItemValues.id)
            )).scalars().all()
        assert rows == ["one", "two", "three"]
        restock.assert_awaited_once_with(
            mock_bot, "BulkRoute", notify_all=True, added_count=3,
        )
        owner_notice.assert_awaited_once()
        assert owner_notice.await_args.kwargs["count"] == 3

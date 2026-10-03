from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest


class TestRevenueInput:

    def test_parses_manual_revenue_values(self):
        from bot.web.revenue import parse_manual_revenue_fields

        assert parse_manual_revenue_fields("12", "3", "55.50") == (
            12, 3, Decimal("55.50")
        )

    @pytest.mark.parametrize("values", [
        ("", "3", "55"),
        ("12", "0", "55"),
        ("12", "3", "0"),
        ("12", "3", "not-a-price"),
    ])
    def test_rejects_invalid_manual_revenue_values(self, values):
        from bot.web.revenue import RevenueInputError, parse_manual_revenue_fields

        with pytest.raises(RevenueInputError):
            parse_manual_revenue_fields(*values)


class TestRevenuePeriod:

    def test_dashboard_supports_two_seven_thirty_ninety_and_365_days(self):
        from bot.web.revenue import (
            REVENUE_DAYS,
            REVENUE_PERIOD_OPTIONS,
            parse_revenue_period,
            revenue_window,
        )

        assert REVENUE_DAYS == 2
        assert parse_revenue_period(None) == REVENUE_DAYS
        assert set(REVENUE_PERIOD_OPTIONS) == {2, 7, 30, 90, 365}
        assert parse_revenue_period("7") == 7
        assert parse_revenue_period("30") == 30
        assert parse_revenue_period("invalid") == REVENUE_DAYS
        assert revenue_window(date(2026, 9, 22), 2) == (
            date(2026, 9, 21), date(2026, 9, 23)
        )
        assert revenue_window(date(2026, 9, 22), 7) == (
            date(2026, 9, 16), date(2026, 9, 23)
        )
        assert revenue_window(date(2026, 9, 22), 30) == (
            date(2026, 8, 24), date(2026, 9, 23)
        )


class TestRevenueAggregation:

    def test_daily_report_omits_dates_without_income(self):
        from bot.web.revenue import build_revenue_report

        actual_rows = [
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("100"), buyer_id=101,
                bought_datetime=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("0"), buyer_id=102,
                bought_datetime=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("50"), buyer_id=103,
                bought_datetime=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
            ),
        ]

        report = build_revenue_report(
            actual_rows,
            [],
            {"AI Plus": "AI"},
            period_start=date(2026, 9, 18),
            days=7,
        )

        assert [row["date"] for row in report["daily"]] == [
            date(2026, 9, 21), date(2026, 9, 24)
        ]
        assert [row["revenue"] for row in report["daily"]] == [
            Decimal("100"), Decimal("50")
        ]

    def test_combines_real_and_manual_revenue_by_day_and_category(self):
        from bot.web.revenue import build_revenue_report

        actual_rows = [
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("100"), buyer_id=101,
                bought_datetime=datetime(2026, 8, 20, 7, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("150"), buyer_id=102,
                bought_datetime=datetime(2026, 8, 21, 7, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("50"), buyer_id=103,
                bought_datetime=datetime(2026, 8, 19, 7, 0, tzinfo=timezone.utc),
            ),
        ]
        manual_rows = [
            SimpleNamespace(
                category_name="AI", quantity=3, unit_price=Decimal("50"),
                created_at=datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc),
            )
        ]

        report = build_revenue_report(
            actual_rows,
            manual_rows,
            {"AI Plus": "AI"},
            period_start=date(2026, 8, 20),
            days=2,
        )

        assert report["summary"]["revenue"] == Decimal("400")
        assert report["summary"]["units"] == 5
        assert report["summary"]["orders"] == 2
        assert report["summary"]["manual_revenue"] == Decimal("150")
        assert report["summary"]["previous_revenue"] == Decimal("50")
        assert report["summary"]["change_percent"] == Decimal("700")
        assert [day["revenue"] for day in report["daily"]] == [Decimal("100"), Decimal("300")]
        assert report["categories"][0] == {
            "name": "AI", "units": 5, "revenue": Decimal("400")
        }
        assert report["products"][0] == {
            "name": "AI Plus", "units": 2, "revenue": Decimal("250")
        }

    def test_excludes_september_19_and_20_from_all_revenue_totals(self):
        from bot.web.revenue import build_revenue_report

        actual_rows = [
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("100"), buyer_id=101,
                bought_datetime=datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("200"), buyer_id=102,
                bought_datetime=datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("50"), buyer_id=103,
                bought_datetime=datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("25"), buyer_id=104,
                bought_datetime=datetime(2026, 9, 24, 7, 0, tzinfo=timezone.utc),
            ),
        ]
        manual_rows = [
            SimpleNamespace(
                category_name="AI", quantity=2, unit_price=Decimal("75"),
                created_at=datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                category_name="AI", quantity=1, unit_price=Decimal("20"),
                created_at=datetime(2026, 9, 21, 8, 0, tzinfo=timezone.utc),
            ),
        ]

        report = build_revenue_report(
            actual_rows,
            manual_rows,
            {"AI Plus": "AI"},
            period_start=date(2026, 9, 19),
            days=7,
        )

        assert report["summary"]["revenue"] == Decimal("95")
        assert report["summary"]["actual_revenue"] == Decimal("75")
        assert report["summary"]["manual_revenue"] == Decimal("20")
        assert report["summary"]["orders"] == 2
        assert report["summary"]["units"] == 3
        assert [row["date"] for row in report["daily"]] == [
            date(2026, 9, 21), date(2026, 9, 24)
        ]
        assert report["categories"] == [
            {"name": "AI", "units": 3, "revenue": Decimal("95")}
        ]
        assert report["products"] == [
            {"name": "AI Plus", "units": 2, "revenue": Decimal("75")}
        ]

    def test_excluded_days_do_not_count_in_previous_period_comparison(self):
        from bot.web.revenue import build_revenue_report

        actual_rows = [
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("100"), buyer_id=101,
                bought_datetime=datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("200"), buyer_id=102,
                bought_datetime=datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                item_name="AI Plus", price=Decimal("50"), buyer_id=103,
                bought_datetime=datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc),
            ),
        ]

        report = build_revenue_report(
            actual_rows,
            [],
            {"AI Plus": "AI"},
            period_start=date(2026, 9, 21),
            days=2,
        )

        assert report["summary"]["previous_revenue"] == Decimal("0")
        assert report["summary"]["revenue"] == Decimal("50")

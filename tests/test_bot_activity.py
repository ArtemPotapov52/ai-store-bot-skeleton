from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from aiogram.enums.chat_type import ChatType
from aiogram.types import CallbackQuery, Message

from bot.database.main import Database
from bot.database.methods.bot_activity import purge_bot_activity, record_bot_activity
from bot.database.models.main import BotUserDailyActivity, User
from bot.middleware.activity import BotActivityMiddleware
from bot.web.bot_statistics import (
    BOT_STATS_PERIOD_OPTIONS,
    load_bot_statistics,
    parse_bot_stats_period,
)


class TestBotActivityRecording:

    async def test_start_clicks_and_verified_interactions_are_counted_separately(
        self, user_factory
    ):
        await user_factory(telegram_id=771001)
        await user_factory(telegram_id=771002)
        day = date(2026, 9, 24)

        await record_bot_activity(771001, activity_date=day, start_click=True)
        await record_bot_activity(771002, activity_date=day, start_click=True)
        await record_bot_activity(771002, activity_date=day, interaction=True)
        await record_bot_activity(771002, activity_date=day, interaction=True)

        report = await load_bot_statistics(today=day, days=7)

        assert report["summary"] == {
            "total_starters": 2,
            "active_today": 1,
            "active_yesterday": 0,
            "clicks_today": 4,
        }
        assert report["daily"][-1] == {
            "date": day,
            "active_users": 1,
            "interactions": 2,
            "start_clicks": 2,
            "clicks": 4,
            "bar_width": 100,
        }

        async with Database().session() as session:
            started_ids = set((await session.execute(
                select(User.telegram_id).where(User.has_started.is_(True))
            )).scalars())
        assert started_ids == {771001, 771002}

    async def test_activity_is_deduplicated_per_user_and_day_but_not_per_click(
        self, user_factory
    ):
        await user_factory(telegram_id=771003)
        first_day = date(2026, 9, 23)

        await record_bot_activity(771003, activity_date=first_day, interaction=True)
        await record_bot_activity(771003, activity_date=first_day, interaction=True)
        await record_bot_activity(771003, activity_date=first_day + timedelta(days=1), interaction=True)

        report = await load_bot_statistics(today=first_day + timedelta(days=1), days=7)

        assert report["summary"]["active_yesterday"] == 1
        assert report["summary"]["active_today"] == 1
        assert report["daily"][-2]["interactions"] == 2
        assert report["daily"][-2]["active_users"] == 1
        assert report["daily"][-1]["interactions"] == 1

    async def test_old_activity_can_be_purged_without_removing_recent_rows(self, user_factory):
        await user_factory(telegram_id=771004)
        cutoff = date(2026, 7, 1)
        await record_bot_activity(
            771004, activity_date=cutoff - timedelta(days=1), interaction=True
        )
        await record_bot_activity(771004, activity_date=cutoff, interaction=True)

        deleted = await purge_bot_activity(before_date=cutoff)

        assert deleted == 1
        async with Database().session() as session:
            rows = (await session.execute(
                select(BotUserDailyActivity.activity_date)
            )).scalars().all()
        assert rows == [cutoff]

    async def test_activity_record_requires_a_real_start_or_interaction(self):
        with pytest.raises(ValueError, match="start click or interaction"):
            await record_bot_activity(771005, activity_date=date(2026, 9, 24))


class TestBotStatisticsPeriod:

    def test_period_parser_accepts_seven_fourteen_and_thirty_days(self):
        assert set(BOT_STATS_PERIOD_OPTIONS) == {7, 14, 30}
        assert parse_bot_stats_period("7") == 7
        assert parse_bot_stats_period("30") == 30
        assert parse_bot_stats_period("unsupported") == 14


class TestBotActivityMiddleware:

    async def test_private_callback_is_recorded_without_retaining_callback_data(self):
        event = AsyncMock(spec=CallbackQuery)
        event.data = "profile:private-value"
        event.from_user = MagicMock(id=771006, is_bot=False)
        event.message = MagicMock()
        event.message.chat.type = ChatType.PRIVATE
        handler = AsyncMock(return_value="handled")
        recorder = AsyncMock()
        activity_day = date(2026, 9, 24)

        with (
            patch("bot.middleware.activity.record_bot_activity", recorder),
            patch("bot.middleware.activity.moscow_today", return_value=activity_day),
        ):
            result = await BotActivityMiddleware()(handler, event, {})

        assert result == "handled"
        recorder.assert_awaited_once_with(
            771006, activity_date=activity_day, interaction=True
        )

    async def test_start_is_left_for_the_handler_to_track_after_subscription_check(self):
        event = AsyncMock(spec=Message)
        event.text = "/start payload"
        event.chat = MagicMock()
        event.chat.type = ChatType.PRIVATE
        event.from_user = MagicMock(id=771007, is_bot=False)
        handler = AsyncMock(return_value="handled")
        recorder = AsyncMock()

        with patch("bot.middleware.activity.record_bot_activity", recorder):
            result = await BotActivityMiddleware()(handler, event, {})

        assert result == "handled"
        recorder.assert_not_awaited()

    async def test_activity_storage_failure_does_not_break_the_bot_flow(self):
        event = AsyncMock(spec=Message)
        event.text = "hello"
        event.chat = MagicMock()
        event.chat.type = ChatType.PRIVATE
        event.from_user = MagicMock(id=771008, is_bot=False)
        handler = AsyncMock(return_value="handled")
        recorder = AsyncMock(side_effect=RuntimeError("database unavailable"))

        with patch("bot.middleware.activity.record_bot_activity", recorder):
            result = await BotActivityMiddleware()(handler, event, {})

        assert result == "handled"
        handler.assert_awaited_once_with(event, {})

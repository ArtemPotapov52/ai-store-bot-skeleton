"""Read-only aggregation for private bot usage statistics."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select

from bot.database import Database
from bot.database.models.main import BotUserDailyActivity, User

BOT_STATS_PERIOD_OPTIONS = {7: "Неделя", 14: "14 дней", 30: "Месяц"}
DEFAULT_BOT_STATS_DAYS = 14


def parse_bot_stats_period(raw_value: Any) -> int:
    try:
        days = int(str(raw_value or "").strip())
    except (TypeError, ValueError):
        return DEFAULT_BOT_STATS_DAYS
    return days if days in BOT_STATS_PERIOD_OPTIONS else DEFAULT_BOT_STATS_DAYS


async def load_bot_statistics(*, today: date, days: int = DEFAULT_BOT_STATS_DAYS) -> dict[str, Any]:
    """Return starters, active users and daily counts for a bounded period."""
    days = parse_bot_stats_period(days)
    window_start = today - timedelta(days=days - 1)

    async with Database().session() as session:
        total_starters = int((await session.execute(
            select(func.count()).select_from(User).where(User.has_started.is_(True))
        )).scalar_one())
        activity_rows = (await session.execute(
            select(
                BotUserDailyActivity.activity_date,
                func.count().filter(BotUserDailyActivity.interaction_count > 0),
                func.coalesce(func.sum(BotUserDailyActivity.interaction_count), 0),
                func.coalesce(func.sum(BotUserDailyActivity.start_click_count), 0),
            )
            .where(
                BotUserDailyActivity.activity_date >= window_start,
                BotUserDailyActivity.activity_date <= today,
            )
            .group_by(BotUserDailyActivity.activity_date)
            .order_by(BotUserDailyActivity.activity_date)
        )).all()

    grouped = {
        row[0]: {
            "active_users": int(row[1] or 0),
            "interactions": int(row[2] or 0),
            "start_clicks": int(row[3] or 0),
        }
        for row in activity_rows
    }
    # Don't draw a misleading run of zeros before tracking has any records.
    first_recorded_day = min(grouped, default=today)
    first_day = max(window_start, first_recorded_day)
    max_clicks = max(
        (row["interactions"] + row["start_clicks"] for row in grouped.values()),
        default=0,
    )
    daily = []
    current_day = first_day
    while current_day <= today:
        counts = grouped.get(current_day, {})
        interactions = counts.get("interactions", 0)
        start_clicks = counts.get("start_clicks", 0)
        clicks = interactions + start_clicks
        daily.append({
            "date": current_day,
            "active_users": counts.get("active_users", 0),
            "interactions": interactions,
            "start_clicks": start_clicks,
            "clicks": clicks,
            "bar_width": int(clicks * 100 / max_clicks) if max_clicks else 0,
        })
        current_day += timedelta(days=1)

    by_day = {row["date"]: row for row in daily}
    yesterday = today - timedelta(days=1)
    today_row = by_day.get(today, {})
    yesterday_row = by_day.get(yesterday, {})
    return {
        "summary": {
            "total_starters": total_starters,
            "active_today": today_row.get("active_users", 0),
            "active_yesterday": yesterday_row.get("active_users", 0),
            "clicks_today": today_row.get("clicks", 0),
        },
        "daily": daily,
        "period_start": first_day,
        "period_end": today,
        "days": days,
    }

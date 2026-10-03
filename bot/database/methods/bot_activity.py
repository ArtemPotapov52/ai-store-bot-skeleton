"""Minimal aggregate bot-activity persistence helpers."""

from __future__ import annotations

from datetime import date

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import delete, update

from bot.database import Database
from bot.database.models.main import BotUserDailyActivity, User


async def record_bot_activity(
    telegram_id: int,
    *,
    activity_date: date,
    start_click: bool = False,
    interaction: bool = False,
) -> None:
    """Increment daily counters atomically without persisting event contents."""
    if not start_click and not interaction:
        raise ValueError("a start click or interaction must be recorded")
    if isinstance(telegram_id, bool) or int(telegram_id) <= 0:
        raise ValueError("telegram_id must be a positive integer")

    database = Database()
    dialect = database.engine.dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:
        raise RuntimeError(f"unsupported activity database dialect: {dialect}")

    table = BotUserDailyActivity.__table__
    values = {
        "telegram_id": int(telegram_id),
        "activity_date": activity_date,
        "start_click_count": int(start_click),
        "interaction_count": int(interaction),
    }
    statement = insert(table).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.telegram_id, table.c.activity_date],
        set_={
            "start_click_count": (
                table.c.start_click_count + statement.excluded.start_click_count
            ),
            "interaction_count": (
                table.c.interaction_count + statement.excluded.interaction_count
            ),
        },
    )

    async with database.session() as session:
        if start_click:
            await session.execute(
                update(User)
                .where(User.telegram_id == int(telegram_id))
                .values(has_started=True)
            )
        await session.execute(statement)


async def purge_bot_activity(*, before_date: date) -> int:
    """Delete daily aggregates older than the exclusive retention cutoff."""
    async with Database().session() as session:
        result = await session.execute(
            delete(BotUserDailyActivity).where(
                BotUserDailyActivity.activity_date < before_date
            )
        )
        return max(0, int(result.rowcount or 0))

"""Create the standard AI-service categories without adding demo products or stock.

The command is safe to repeat: existing categories keep their names and order.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

from dotenv import load_dotenv
from sqlalchemy import select


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from bot.database import Database
from bot.database.models import Categories


AI_CATEGORIES = (
    "ChatGPT",
    "Gemini",
    "Claude",
    "Grok",
    "Perplexity",
)


async def seed() -> int:
    """Create missing categories and return their count."""
    created = 0
    async with Database().session() as session:
        existing = set((await session.execute(
            select(Categories.name).where(Categories.name.in_(AI_CATEGORIES))
        )).scalars())
        for sort_order, name in enumerate(AI_CATEGORIES, start=1):
            if name in existing:
                continue
            session.add(Categories(name=name, sort_order=sort_order * 10, is_active=True))
            created += 1
    return created


if __name__ == "__main__":
    count = asyncio.run(seed())
    print(f"AI categories ready: added {count}, total {len(AI_CATEGORIES)}")

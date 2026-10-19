"""Pre-create a referrer user in the database so their referral link is active immediately.

Usage:
    python scripts/create_referrer.py <telegram_id>
"""
import asyncio
import sys
from datetime import datetime, timezone

from bot.database.main import Database
from bot.database.methods.create import create_user
from bot.database.methods.read import check_user, get_default_user_role_id


async def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/create_referrer.py <telegram_id>")
        sys.exit(1)

    try:
        telegram_id = int(sys.argv[1])
    except ValueError:
        print(f"Invalid telegram_id: {sys.argv[1]}")
        sys.exit(1)

    existing = await check_user(telegram_id)
    if existing:
        print(f"User {telegram_id} already exists in database (role: {existing.get('role_id')}).")
        print(f"Referral link: https://t.me/<BOT_USERNAME>?start={telegram_id}")
        return

    default_role = await get_default_user_role_id()
    await create_user(
        telegram_id=telegram_id,
        registration_date=datetime.now(timezone.utc),
        referral_id=None,
        role=default_role,
    )
    print(f"Successfully created referrer user {telegram_id} in database!")
    print(f"Referral link: https://t.me/<BOT_USERNAME>?start={telegram_id}")


if __name__ == "__main__":
    asyncio.run(main())

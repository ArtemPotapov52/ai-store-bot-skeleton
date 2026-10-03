"""Partner API credentials, scoped to one existing Telegram account."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update

from bot.database import Database
from bot.database.models import PartnerApiKey, User

API_KEY_PREFIX = "ps_live_"


@dataclass(frozen=True)
class ApiPrincipal:
    api_key_id: int
    user_id: int
    is_blocked: bool


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_partner_api_key(user_id: int) -> str | None:
    """Issue/rotate the account's one key; the plaintext is returned only once."""
    token = API_KEY_PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)

    async with Database().session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == int(user_id)).with_for_update()
        )).scalar_one_or_none()
        if user is None or bool(user.is_blocked):
            return None

        existing = (await session.execute(
            select(PartnerApiKey)
            .where(PartnerApiKey.user_id == int(user_id))
            .with_for_update()
        )).scalar_one_or_none()
        if existing is None:
            session.add(PartnerApiKey(
                user_id=int(user_id),
                key_hash=_digest(token),
                key_prefix=token[:16],
                created_at=now,
            ))
        else:
            existing.key_hash = _digest(token)
            existing.key_prefix = token[:16]
            existing.created_at = now
            existing.last_used_at = None
            existing.revoked_at = None
    return token


async def revoke_partner_api_key(user_id: int) -> bool:
    """Revoke a key from the account owner; no administrator API is involved."""
    async with Database().session() as session:
        row = (await session.execute(
            select(PartnerApiKey)
            .where(
                PartnerApiKey.user_id == int(user_id),
                PartnerApiKey.revoked_at.is_(None),
            )
            .with_for_update()
        )).scalar_one_or_none()
        if row is None:
            return False
        row.revoked_at = datetime.now(timezone.utc)
    return True


async def authenticate_api_key(token: str) -> ApiPrincipal | None:
    """Resolve a bearer secret without accepting a user ID from the request."""
    if not isinstance(token, str) or not token.startswith(API_KEY_PREFIX) or len(token) > 100:
        return None

    key_hash = _digest(token)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=5)
    async with Database().session() as session:
        row = (await session.execute(
            select(PartnerApiKey, User.is_blocked)
            .join(User, User.telegram_id == PartnerApiKey.user_id)
            .where(
                PartnerApiKey.key_hash == key_hash,
                PartnerApiKey.revoked_at.is_(None),
            )
        )).first()
        if row is None:
            return None

        key, is_blocked = row
        await session.execute(
            update(PartnerApiKey)
            .where(
                PartnerApiKey.id == key.id,
                or_(PartnerApiKey.last_used_at.is_(None), PartnerApiKey.last_used_at < cutoff),
            )
            .values(last_used_at=now)
            .execution_options(synchronize_session=False)
        )
        return ApiPrincipal(
            api_key_id=int(key.id),
            user_id=int(key.user_id),
            is_blocked=bool(is_blocked),
        )

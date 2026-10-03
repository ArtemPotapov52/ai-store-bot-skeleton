"""Channel and community-chat subscription gates.

The news channel and community chat are mandatory for every user. /start has
its own gate, while this middleware closes the hole of stale keyboards and
direct state input.

The channel URL and the membership check are deliberately handled separately.
Telegram rotates a private channel's primary invite link when an administrator
exports a new one, so a URL kept in ``CHANNEL_URL`` can become stale while
``getChatMember`` continues to work.  For private channels we resolve the
current invite from ``getChat`` before rendering the subscribe screen.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from aiogram import BaseMiddleware
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.i18n import localize
from bot.keyboards import check_sub
from bot.logger_mesh import logger
from bot.misc import EnvKeys

# Callbacks that must stay usable while unsubscribed.
_EXEMPT_CALLBACKS = frozenset({
    "sub_channel_done",
    "close",
    "dummy_button",
})
_BANNED_STATUSES = frozenset({ChatMemberStatus.LEFT, ChatMemberStatus.KICKED})

# getChatMember cache: (channel, user_id) -> (subscribed, monotonic deadline).
# Positive results are cached for a minute, while negative results live only a
# few seconds.  A user who joins the channel must not remain blocked for the
# whole positive-cache window after the first check raced Telegram's state.
_CACHE_TTL = 60.0
_NEGATIVE_CACHE_TTL = 5.0
_CACHE_MAX = 5000
_cache: dict[tuple[str, int], tuple[bool, float]] = {}

# A channel's current primary invite is stable for normal bot traffic.  Cache
# it briefly so every blocked update does not call getChat, but refresh it
# quickly after an administrator rotates the invite.
_LINK_CACHE_TTL = 300.0
_link_cache: dict[str, tuple[str, float]] = {}


def channel_token() -> str | None:
    """Raw channel token from CHANNEL_URL (username or +invite-hash)."""
    raw = str(getattr(EnvKeys, "CHANNEL_URL", "") or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    token = (parsed.path.lstrip("/") if parsed.path else raw).lstrip("@")
    return token or None


def channel_chat_id() -> int | str | None:
    """Chat id for membership checks. Private channels need CHANNEL_ID."""
    raw = str(getattr(EnvKeys, "CHANNEL_ID", "") or "").strip()
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("CHANNEL_ID=%r is not numeric; subscription check disabled", raw)
            return None
    token = channel_token()
    if token and not token.startswith("+"):
        return f"@{token}"
    if token:
        logger.warning("Private channel link needs a numeric CHANNEL_ID to verify subscription")
    return None


def community_chat_token() -> str | None:
    """Return the community username or private invite token."""
    raw = str(getattr(EnvKeys, "COMMUNITY_CHAT_URL", "") or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    token = (parsed.path.lstrip("/") if parsed.path else raw).lstrip("@")
    return token or None


def community_chat_url() -> str | None:
    """Return a Telegram URL suitable for a chat invite button."""
    raw = str(getattr(EnvKeys, "COMMUNITY_CHAT_URL", "") or "").strip()
    return _valid_channel_url(raw)


def community_chat_id() -> int | str | None:
    """Resolve a community chat ID; private invite links need COMMUNITY_CHAT_ID."""
    raw = str(getattr(EnvKeys, "COMMUNITY_CHAT_ID", "") or "").strip()
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("COMMUNITY_CHAT_ID is not a numeric Telegram chat ID")
            return None
    token = community_chat_token()
    if token and not token.startswith("+"):
        return f"@{token}"
    if token:
        logger.warning("Private community invite needs a numeric COMMUNITY_CHAT_ID to verify membership")
    return None


def is_member_subscribed(chat_member) -> bool:
    """Return whether a Telegram ``ChatMember`` represents membership.

    ``restricted`` is special: Telegram uses it both for a member with
    limited permissions and for a user who is no longer a member.  The latter
    is represented by ``is_member=False`` and must not pass the gate.
    """
    status = getattr(chat_member, "status", None)
    if status is None:
        return False
    if status in _BANNED_STATUSES:
        return False
    if status == ChatMemberStatus.RESTRICTED:
        return getattr(chat_member, "is_member", False) is True
    return True


def _valid_channel_url(value: object) -> str | None:
    """Return a Telegram link suitable for an inline URL button."""
    if not isinstance(value, str):
        return None
    link = value.strip()
    if link.startswith(("https://t.me/", "http://t.me/", "tg://")):
        return link
    return None


async def resolve_channel_link(
    bot,
    token: str | None = None,
    *,
    chat_id: int | str | None = None,
    configured_url: str | None = None,
) -> str | None:
    """Resolve the current channel URL used by the subscribe keyboard.

    Public channels have a stable username URL and need no API round-trip. For
    private channels, ``Chat.invite_link`` is the current primary invite link;
    it avoids presenting a revoked link from the environment.  The configured
    URL remains a last-resort fallback when Telegram is temporarily
    unavailable, and is never used in preference to the current API value.
    """
    token = token or channel_token()
    if not token:
        return None

    if not token.startswith("+"):
        return f"https://t.me/{token}"

    if chat_id is None:
        chat_id = channel_chat_id()
    fallback = _valid_channel_url(configured_url)
    if fallback is None:
        raw = str(getattr(EnvKeys, "CHANNEL_URL", "") or "").strip()
        fallback = _valid_channel_url(raw)

    if chat_id is not None:
        cache_key = str(chat_id)
        now = time.monotonic()
        cached = _link_cache.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]
        try:
            chat = await bot.get_chat(chat_id=chat_id)
            invite = _valid_channel_url(getattr(chat, "invite_link", None))
        except Exception as exc:
            logger.warning("Could not resolve current channel invite: %s", exc)
            invite = None
        if invite:
            if len(_link_cache) >= _CACHE_MAX:
                _link_cache.clear()
            _link_cache[cache_key] = (invite, now + _LINK_CACHE_TTL)
            return invite

    return fallback


async def is_chat_subscribed(bot, chat_id: int | str | None, user_id: int) -> bool | None:
    """Return membership status, or None when Telegram cannot verify it."""
    if chat_id is None:
        return None

    now = time.monotonic()
    cache_key = (str(chat_id), user_id)
    hit = _cache.get(cache_key)
    if hit and hit[1] > now:
        return hit[0]
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
    except Exception as exc:
        logger.warning("Subscription check failed for user %s in chat %s: %s", user_id, chat_id, exc)
        return None

    result = is_member_subscribed(member)
    if len(_cache) >= _CACHE_MAX:
        _cache.clear()
    ttl = _CACHE_TTL if result else _NEGATIVE_CACHE_TTL
    _cache[cache_key] = (result, now + ttl)
    return result


async def build_subscription_gate(bot, user_id: int, user: dict | None):
    """Return (message, keyboard) when an update must stop at the gate.

    ``user`` remains in the signature for existing callers; chat membership is
    now mandatory globally and no longer depends on per-user policy fields.
    """
    token = channel_token()
    community_url = community_chat_url()

    channel_status = True
    if token:
        channel_status = await is_chat_subscribed(bot, channel_chat_id(), user_id)
    channel_blocked = bool(token) and channel_status is False

    community_status = True
    if community_url:
        # Membership must be positively confirmed. Unknown/API-failure status
        # stays blocked so Telegram outages cannot silently bypass the gate.
        community_status = await is_chat_subscribed(
            bot, community_chat_id(), user_id
        )

    community_blocked = bool(community_url and community_status is not True)
    if not (channel_blocked or community_blocked):
        return None

    channel_link = None
    if token and (channel_blocked or community_blocked):
        channel_link = await resolve_channel_link(
            bot,
            token,
            chat_id=channel_chat_id(),
            configured_url=EnvKeys.CHANNEL_URL,
        )

    text_parts = []
    if channel_blocked:
        text_parts.append(localize("subscribe.prompt"))
    if community_blocked:
        text_parts.append(localize("subscribe.community_required"))

    markup = check_sub(
        token,
        channel_url=channel_link,
        community_url=community_url,
        allow_skip_community=False,
    )
    return "\n\n".join(text_parts), markup


class SubscriptionMiddleware(BaseMiddleware):
    """Enforce configured channel and community gates for every user."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not channel_token() and not community_chat_url():
            return await handler(event, data)

        if isinstance(event, Message):
            if (event.text or "").startswith("/start"):
                return await handler(event, data)
            if event.chat is None or event.chat.type != "private":
                return await handler(event, data)
        elif isinstance(event, CallbackQuery):
            if (event.data or "") in _EXEMPT_CALLBACKS:
                return await handler(event, data)
            msg = event.message
            if msg is None or getattr(getattr(msg, "chat", None), "type", None) != "private":
                return await handler(event, data)
        else:
            return await handler(event, data)

        user = event.from_user
        if user is None or user.is_bot:
            return await handler(event, data)

        # The policy is global now; avoid a database lookup that could make
        # enforcement dependent on whether a user's legacy row can be loaded.
        gate = await build_subscription_gate(event.bot, user.id, None)
        if gate is None:
            return await handler(event, data)

        await self.ask_subscribe(event, *gate)
        return None

    async def is_subscribed(self, bot, user_id: int) -> bool | None:
        """True/False, or None when the check could not be performed."""
        return await is_chat_subscribed(bot, channel_chat_id(), user_id)

    async def ask_subscribe(
        self,
        event: Message | CallbackQuery,
        text: str,
        markup,
    ) -> None:
        try:
            if isinstance(event, CallbackQuery):
                if event.message is not None:
                    await event.message.edit_text(text, reply_markup=markup)
                else:
                    await event.answer(text, show_alert=True)
            else:
                await event.answer(text, reply_markup=markup)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.debug("Could not render subscribe screen: %s", e)
            try:
                await event.answer(text, show_alert=True)
            except Exception:
                pass


def clear_subscription_cache(user_id: int | None = None) -> None:
    """Invalidate membership and invite caches.

    ``sub_channel_done`` calls this for the current user so a previous
    negative result cannot keep a newly subscribed user blocked.
    """
    if user_id is None:
        _cache.clear()
    else:
        for key in [key for key in _cache if key[1] == user_id]:
            _cache.pop(key, None)
    _link_cache.clear()

from unittest.mock import AsyncMock, MagicMock, patch

from aiogram.enums import ChatMemberStatus
from aiogram.types import CallbackQuery, Message

from bot.middleware.subscription import (
    SubscriptionMiddleware,
    channel_chat_id,
    channel_token,
    clear_subscription_cache,
    build_subscription_gate,
    is_member_subscribed,
    resolve_channel_link,
)


def _callback(user_id: int, data: str = "profile") -> AsyncMock:
    call = AsyncMock(spec=CallbackQuery)
    call.data = data
    call.from_user = MagicMock()
    call.from_user.id = user_id
    call.from_user.is_bot = False
    call.message = MagicMock()
    call.message.chat.type = "private"
    call.message.edit_text = AsyncMock()
    call.answer = AsyncMock()
    call.bot = AsyncMock()
    return call


def _message(user_id: int, text: str = "hello") -> AsyncMock:
    msg = AsyncMock(spec=Message)
    msg.text = text
    msg.chat = MagicMock()
    msg.chat.type = "private"
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.from_user.is_bot = False
    msg.answer = AsyncMock()
    msg.bot = AsyncMock()
    return msg


def _member(status) -> MagicMock:
    m = MagicMock()
    m.status = status
    return m


class TestChannelParsing:

    def test_public_username(self):
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "https://t.me/mychannel"
            env.CHANNEL_ID = ""
            assert channel_token() == "mychannel"
            assert channel_chat_id() == "@mychannel"

    def test_private_invite_link_needs_id(self):
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "https://t.me/+testinvite123"
            env.CHANNEL_ID = ""
            assert channel_token() == "+testinvite123"
            assert channel_chat_id() is None

    def test_private_channel_with_id(self):
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "https://t.me/+testinvite123"
            env.CHANNEL_ID = "-1001234567890"
            assert channel_chat_id() == -1001234567890

    def test_no_channel(self):
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = ""
            env.CHANNEL_ID = ""
            assert channel_token() is None

    async def test_private_link_uses_current_channel_invite(self):
        bot = AsyncMock()
        bot.get_chat.return_value = MagicMock(
            invite_link="https://t.me/+test-fresh-invite"
        )
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "https://t.me/+test-expired-invite"
            env.CHANNEL_ID = "-1001234567890"
            assert await resolve_channel_link(
                bot,
                "+test-expired-invite",
            ) == "https://t.me/+test-fresh-invite"
        bot.get_chat.assert_awaited_once_with(chat_id=-1001234567890)


class TestSubscriptionMiddleware:

    def setup_method(self):
        clear_subscription_cache()
        self.mw = SubscriptionMiddleware()

    async def test_no_channel_passes_through(self):
        call = _callback(700001)
        handler = AsyncMock(return_value="ok")
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = ""
            env.CHANNEL_ID = ""
            env.OWNER_ID = 1
            assert await self.mw(handler, call, {}) == "ok"
        handler.assert_awaited_once()

    async def test_exempt_callback_passes(self):
        call = _callback(700002, data="sub_channel_done")
        call.bot.get_chat_member = AsyncMock(
            return_value=_member(ChatMemberStatus.LEFT)
        )
        handler = AsyncMock(return_value="ok")
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "https://t.me/+testhash"
            env.CHANNEL_ID = "-1001"
            env.OWNER_ID = 1
            assert await self.mw(handler, call, {}) == "ok"
        handler.assert_awaited_once()
        call.bot.get_chat_member.assert_not_awaited()

    async def test_start_message_passes(self):
        msg = _message(700003, text="/start")
        handler = AsyncMock(return_value="ok")
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "@chan"
            env.CHANNEL_ID = ""
            env.OWNER_ID = 1
            assert await self.mw(handler, msg, {}) == "ok"
        handler.assert_awaited_once()

    async def test_subscribed_user_passes(self):
        call = _callback(700004)
        call.bot.get_chat_member = AsyncMock(
            return_value=_member(ChatMemberStatus.MEMBER)
        )
        handler = AsyncMock()
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "@chan"
            env.CHANNEL_ID = ""
            env.OWNER_ID = 1
            await self.mw(handler, call, {})
        handler.assert_awaited_once()
        call.message.edit_text.assert_not_awaited()

    async def test_unsubscribed_callback_blocked_with_subscribe_screen(self):
        call = _callback(700005)
        call.bot.get_chat_member = AsyncMock(
            return_value=_member(ChatMemberStatus.LEFT)
        )
        handler = AsyncMock()
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "@chan"
            env.CHANNEL_ID = ""
            env.OWNER_ID = 1
            assert await self.mw(handler, call, {}) is None
        handler.assert_not_awaited()
        call.message.edit_text.assert_awaited_once()

    async def test_unsubscribed_message_blocked(self):
        msg = _message(700006, text="100")
        msg.bot.get_chat_member = AsyncMock(
            return_value=_member(ChatMemberStatus.KICKED)
        )
        handler = AsyncMock()
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "@chan"
            env.CHANNEL_ID = ""
            env.OWNER_ID = 1
            assert await self.mw(handler, msg, {}) is None
        handler.assert_not_awaited()
        msg.answer.assert_awaited_once()

    async def test_chat_is_mandatory_for_legacy_users_who_previously_skipped(self):
        bot = AsyncMock()
        bot.get_chat_member.return_value = _member(ChatMemberStatus.LEFT)
        legacy_user = {
            "community_chat_required": False,
            "community_prompt_seen": True,
        }

        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = ""
            env.CHANNEL_ID = ""
            env.COMMUNITY_CHAT_URL = "https://t.me/+testcommunity"
            env.COMMUNITY_CHAT_ID = "-1002"
            text, markup = await build_subscription_gate(bot, 700009, legacy_user)

        assert "необходимо вступить в чат" in text
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        assert "sub_channel_done" in callbacks
        assert "subscription_continue_without_community" not in callbacks

    async def test_owner_does_not_bypass_subscription_gate(self):
        call = _callback(1)
        call.bot.get_chat_member = AsyncMock(
            return_value=_member(ChatMemberStatus.LEFT)
        )
        handler = AsyncMock(return_value="ok")
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = ""
            env.CHANNEL_ID = ""
            env.COMMUNITY_CHAT_URL = "https://t.me/+testcommunity"
            env.COMMUNITY_CHAT_ID = "-1002"
            env.OWNER_ID = 1
            assert await self.mw(handler, call, {}) is None
        handler.assert_not_awaited()
        call.message.edit_text.assert_awaited_once()

    async def test_stale_continue_button_cannot_bypass_required_chat(self):
        call = _callback(700012, data="subscription_continue_without_community")
        call.bot.get_chat_member = AsyncMock(
            return_value=_member(ChatMemberStatus.LEFT)
        )
        handler = AsyncMock(return_value="ok")
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = ""
            env.CHANNEL_ID = ""
            env.COMMUNITY_CHAT_URL = "https://t.me/+testcommunity"
            env.COMMUNITY_CHAT_ID = "-1002"
            env.OWNER_ID = 1
            assert await self.mw(handler, call, {}) is None

        handler.assert_not_awaited()
        call.message.edit_text.assert_awaited_once()

    async def test_check_unavailable_fails_open(self):
        call = _callback(700007)
        call.bot.get_chat_member = AsyncMock(side_effect=Exception("boom"))
        handler = AsyncMock(return_value="ok")
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "https://t.me/+testhash"
            env.CHANNEL_ID = "-1001"
            env.OWNER_ID = 1
            assert await self.mw(handler, call, {}) == "ok"
        handler.assert_awaited_once()

    async def test_community_check_unavailable_blocks_access(self):
        call = _callback(700010)
        call.bot.get_chat_member = AsyncMock(side_effect=Exception("telegram unavailable"))
        handler = AsyncMock(return_value="ok")
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = ""
            env.CHANNEL_ID = ""
            env.COMMUNITY_CHAT_URL = "https://t.me/+testcommunity"
            env.COMMUNITY_CHAT_ID = "-1002"
            env.OWNER_ID = 1
            assert await self.mw(handler, call, {}) is None

        handler.assert_not_awaited()
        call.message.edit_text.assert_awaited_once()

    async def test_community_gate_does_not_depend_on_database_policy_lookup(self):
        call = _callback(700011)
        call.bot.get_chat_member = AsyncMock(
            return_value=_member(ChatMemberStatus.LEFT)
        )
        handler = AsyncMock(return_value="ok")
        with (
            patch("bot.middleware.subscription.EnvKeys") as env,
            patch("bot.database.methods.check_user", new_callable=AsyncMock, side_effect=Exception("database unavailable")) as check_user,
        ):
            env.CHANNEL_URL = ""
            env.CHANNEL_ID = ""
            env.COMMUNITY_CHAT_URL = "https://t.me/+testcommunity"
            env.COMMUNITY_CHAT_ID = "-1002"
            env.OWNER_ID = 1
            assert await self.mw(handler, call, {}) is None

        handler.assert_not_awaited()
        check_user.assert_not_awaited()
        call.message.edit_text.assert_awaited_once()

    async def test_negative_cache_is_cleared_after_user_joins(self):
        user_id = 700008
        call = _callback(user_id)
        call.bot.get_chat_member = AsyncMock(
            side_effect=[
                _member(ChatMemberStatus.LEFT),
                _member(ChatMemberStatus.MEMBER),
            ]
        )
        with patch("bot.middleware.subscription.EnvKeys") as env:
            env.CHANNEL_URL = "@chan"
            env.CHANNEL_ID = ""
            env.OWNER_ID = 1
            assert await self.mw.is_subscribed(call.bot, user_id) is False
            # The negative result is cached until the user taps the explicit
            # re-check button; the button invalidates it before querying.
            assert await self.mw.is_subscribed(call.bot, user_id) is False
            clear_subscription_cache(user_id)
            assert await self.mw.is_subscribed(call.bot, user_id) is True
        assert call.bot.get_chat_member.await_count == 2


class TestMemberStatus:

    def test_restricted_member_is_subscribed(self):
        member = _member(ChatMemberStatus.RESTRICTED)
        member.is_member = True
        assert is_member_subscribed(member) is True

    def test_restricted_non_member_is_not_subscribed(self):
        member = _member(ChatMemberStatus.RESTRICTED)
        member.is_member = False
        assert is_member_subscribed(member) is False

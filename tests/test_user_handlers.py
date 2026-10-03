import pytest
from unittest.mock import AsyncMock, patch
from aiogram.enums.chat_type import ChatType

from bot.database.methods.read import check_user, get_item_info, select_max_role_id
from bot.database.models import Permission
from bot.handlers.user.main import (
    start, rules_callback_handler, profile_callback_handler,
    back_to_menu_callback_handler, ensure_user, _env_text,
    restart_from_stale_session,
)


def _screen_render(call):
    """Return screen text and markup for either edit_text or send_photo."""
    if call.message.edit_text.call_args is not None:
        args, kwargs = call.message.edit_text.call_args
        return args[0], kwargs
    kwargs = call.message.bot.send_photo.call_args.kwargs
    return kwargs["caption"], kwargs


class TestConfiguredFaqText:

    def test_faq_normalizes_escaped_newlines_and_markdown_bold(self):
        with patch('bot.handlers.user.main.EnvKeys') as env:
            env.FAQ = r"**FAQ**\n\n\&#x6E;**FW** — полная гарантия."

            result = _env_text("FAQ")

        assert result == "<b>FAQ</b>\n\n\n<b>FW</b> — полная гарантия."


class TestStartHandler:

    async def test_start_creates_new_user(self, make_message, fsm_context):

        msg = make_message(text="/start", user_id=300001)
        msg.chat.type = ChatType.PRIVATE

        with patch('bot.handlers.user.main.EnvKeys') as env:
            env.OWNER_ID = 999999
            env.CHANNEL_URL = ""
            env.HELPER_ID = ""
            env.RULES = ""
            await start(msg, fsm_context)

        user = await check_user(300001)
        assert user is not None
        assert user['telegram_id'] == 300001
        msg.delete.assert_not_awaited()

    async def test_start_with_referral(self, make_message, fsm_context, user_factory):

        # Create referrer first
        await user_factory(telegram_id=300010)

        msg = make_message(text="/start 300010", user_id=300011)
        msg.chat.type = ChatType.PRIVATE

        with patch('bot.handlers.user.main.EnvKeys') as env:
            env.OWNER_ID = 999999
            env.CHANNEL_URL = ""
            env.HELPER_ID = ""
            env.RULES = ""
            await start(msg, fsm_context)

        user = await check_user(300011)
        assert user is not None
        assert user['referral_id'] == 300010

    async def test_start_self_referral_ignored(self, make_message, fsm_context):

        msg = make_message(text="/start 300020", user_id=300020)
        msg.chat.type = ChatType.PRIVATE

        with patch('bot.handlers.user.main.EnvKeys') as env:
            env.OWNER_ID = 999999
            env.CHANNEL_URL = ""
            env.HELPER_ID = ""
            env.RULES = ""
            await start(msg, fsm_context)

        user = await check_user(300020)
        assert user is not None
        assert user['referral_id'] is None

    async def test_start_item_link_opens_the_product_card(self, make_message, fsm_context, item_factory):
        await item_factory(name="Linked item", price=90, values=[("delivery", False)])
        item = await get_item_info("Linked item")
        msg = make_message(text=f"/start item_{item['id']}", user_id=300021)
        msg.chat.type = ChatType.PRIVATE

        with patch('bot.handlers.user.main.EnvKeys') as env, \
                patch('bot.handlers.user.shop_and_goods._open_item', new_callable=AsyncMock) as open_item:
            env.OWNER_ID = 999999
            env.CHANNEL_URL = ""
            env.HELPER_ID = ""
            await start(msg, fsm_context)

        open_item.assert_awaited_once_with(
            msg,
            fsm_context,
            "Linked item",
            back_data="back_to_menu",
        )
        assert (await check_user(300021))["referral_id"] is None

    async def test_start_owner_gets_max_role(self, make_message, fsm_context):

        msg = make_message(text="/start", user_id=300030)
        msg.chat.type = ChatType.PRIVATE

        max_role = await select_max_role_id()
        with patch('bot.handlers.user.main.EnvKeys') as env:
            env.OWNER_ID = 300030
            env.CHANNEL_URL = ""
            env.HELPER_ID = ""
            env.RULES = ""
            await start(msg, fsm_context)

        user = await check_user(300030)
        assert user['role_id'] == max_role

    async def test_start_non_private_ignored(self, make_message, fsm_context):

        msg = make_message(text="/start", user_id=300040)
        msg.chat.type = ChatType.GROUP

        with patch('bot.handlers.user.main.EnvKeys') as env:
            env.OWNER_ID = 999999
            await start(msg, fsm_context)

        # User should NOT be created
        user = await check_user(300040)
        assert user is None

    async def test_rapid_duplicate_start_renders_menu_once(self, make_message, fsm_context):
        """A double-tapped /start must render the main menu exactly once."""
        from bot.handlers.user.main import _LAST_START
        from bot.misc import screens

        uid = 300046
        _LAST_START.pop(uid, None)
        try:
            msg1 = make_message(text="/start", user_id=uid)
            msg1.chat.type = ChatType.PRIVATE
            msg2 = make_message(text="/start", user_id=uid)
            msg2.chat.type = ChatType.PRIVATE

            with patch('bot.handlers.user.main.EnvKeys') as env:
                env.OWNER_ID = 999999
                env.CHANNEL_URL = ""
                env.HELPER_ID = ""
                env.RULES = ""
                await start(msg1, fsm_context)
                await start(msg2, fsm_context)

            first_renders = msg1.answer.await_count + msg1.answer_photo.await_count
            second_renders = msg2.answer.await_count + msg2.answer_photo.await_count
            assert first_renders == 1
            assert second_renders == 0
            assert await check_user(uid) is not None
        finally:
            _LAST_START.pop(uid, None)
            screens._SCREEN_MESSAGES.pop(uid, None)
            screens._SCREEN_MEDIA_KEYS.pop(uid, None)

    async def test_stale_session_restart_sends_a_fresh_start_screen(
        self, make_message, fsm_context, user_factory
    ):
        uid = 300048
        await user_factory(telegram_id=uid)
        message = make_message(user_id=uid)
        await fsm_context.set_state("stale-session")

        with patch('bot.handlers.user.main.EnvKeys') as env:
            env.CHANNEL_URL = ""
            env.HELPER_ID = ""
            env.SUPPORT_URL = ""
            env.SUPPORT_USERNAME = ""
            env.SHOP_NAME = "My Store"
            env.REFERRAL_PERCENT = 0
            await restart_from_stale_session(message.bot, message.from_user, fsm_context)

        message.bot.send_message.assert_awaited_once()
        assert message.bot.send_message.await_args.kwargs["chat_id"] == uid
        assert message.bot.send_message.await_args.kwargs["reply_markup"] is not None
        assert await fsm_context.get_state() is None


class TestProfileHandler:

    async def test_profile_shows_balance(self, make_callback_query, fsm_context, user_factory):

        await user_factory(telegram_id=300050, balance=500)

        call = make_callback_query(data="profile", user_id=300050)

        with patch('bot.handlers.user.main.EnvKeys') as env:
            env.PAY_CURRENCY = "RUB"
            env.REFERRAL_PERCENT = 0
            await profile_callback_handler(call, fsm_context)

        text, _ = _screen_render(call)
        assert "500" in str(text)


class TestRulesHandler:

    async def test_rules_opens_agreements_menu(self, make_callback_query, fsm_context):

        call = make_callback_query(data="rules", user_id=300060)

        await rules_callback_handler(call, fsm_context)

        text, kwargs = _screen_render(call)
        assert "legal.title" in text
        urls = [
            b.url for row in kwargs["reply_markup"].inline_keyboard for b in row if b.url
        ]
        assert any("telegra.ph" in (u or "") for u in urls)

    async def test_agreements_menu_has_both_links(self, make_callback_query, fsm_context):
        from bot.handlers.user.main import agreement_callback_handler

        call = make_callback_query(data="agreement", user_id=300070)

        await agreement_callback_handler(call, fsm_context)

        _text, kwargs = _screen_render(call)
        urls = [
            b.url for row in kwargs["reply_markup"].inline_keyboard for b in row if b.url
        ]
        assert len([u for u in urls if u and "telegra.ph" in u]) == 2


class TestMainMenuReceivesPermissionBitmask:
    async def test_custom_role_without_admin_perms_gets_no_admin_button(
        self, make_callback_query, fsm_context, user_factory, role_factory
    ):

        # A plain-user role whose *id* is >= 4, so role_id and bitmask diverge.
        role_id = await role_factory(name="PLAINCUSTOM", permissions=Permission.USE)
        assert role_id >= 4, "fixture assumption: custom roles get ids past the built-ins"

        await user_factory(telegram_id=630001, role_id=role_id)

        call = make_callback_query(data="back_to_menu", user_id=630001)
        await back_to_menu_callback_handler(call, fsm_context)

        _, screen_kwargs = _screen_render(call)
        markup = screen_kwargs["reply_markup"]
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "console" not in cbs

    async def test_role_with_admin_perms_keeps_customer_menu_clean(
        self, make_callback_query, fsm_context, user_factory, role_factory
    ):

        role_id = await role_factory(
            name="REALADMIN", permissions=Permission.USE | Permission.CATALOG_MANAGE
        )
        await user_factory(telegram_id=630002, role_id=role_id)

        call = make_callback_query(data="back_to_menu", user_id=630002)
        await back_to_menu_callback_handler(call, fsm_context)

        _, screen_kwargs = _screen_render(call)
        markup = screen_kwargs["reply_markup"]
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "console" not in cbs


class TestProfileWithoutUserRow:
    """A stale keyboard (or a wiped database) can deliver a callback from
    someone with no row; reading user fields off None used to raise."""

    async def test_profile_registers_the_missing_user(self, make_callback_query, fsm_context):

        assert await check_user(630010) is None

        call = make_callback_query(data="profile", user_id=630010)
        await profile_callback_handler(call, fsm_context)

        assert await check_user(630010) is not None
        _screen_render(call)

    async def test_registration_uses_current_default_role_id(self, user_factory):
        """A recreated USER role may no longer have the historical id=1."""
        from sqlalchemy import update
        from bot.database.main import Database
        from bot.database.models import Role

        async with Database().session() as s:
            await s.execute(
                update(Role).where(Role.name == "USER").values(default=False)
            )
            s.add(Role(
                id=42,
                name="DEFAULT_USER",
                default=True,
                permissions=Permission.USE,
            ))

        user = await ensure_user(630011)

        assert user is not None
        assert user["role_id"] == 42

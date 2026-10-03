from unittest.mock import AsyncMock, patch

import pytest

from bot.database.models import Permission
from bot.handlers.admin import bot_panel
from bot.handlers.admin.bot_panel import (
    BotAdminFSM,
    bot_admin_command,
    process_bot_admin_password,
)
from bot.misc import EnvKeys


def _callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


@pytest.fixture(autouse=True)
def reset_bot_admin_attempts():
    bot_panel._failed_attempts.clear()
    bot_panel._locked_until.clear()
    yield
    bot_panel._failed_attempts.clear()
    bot_panel._locked_until.clear()


async def test_xyz67_is_owner_only(make_message, fsm_context, monkeypatch):
    monkeypatch.setattr(EnvKeys, "OWNER_ID", 900001)

    message = make_message(text="/xyz67", user_id=900002)
    await bot_admin_command(message, fsm_context)

    assert await fsm_context.get_state() is None
    message.answer.assert_called_once()
    assert "доступ" in message.answer.call_args.args[0].lower()


async def test_xyz67_prompts_owner_for_password(make_message, fsm_context, monkeypatch):
    monkeypatch.setattr(EnvKeys, "OWNER_ID", 900003)

    message = make_message(text="/xyz67", user_id=900003)
    await bot_admin_command(message, fsm_context)

    assert await fsm_context.get_state() == BotAdminFSM.waiting_password
    prompt = message.answer.call_args.args[0]
    assert "парол" in prompt.lower()


async def test_wrong_password_is_deleted_and_does_not_open_panel(
    make_message, fsm_context, monkeypatch
):
    monkeypatch.setattr(EnvKeys, "OWNER_ID", 900004)
    monkeypatch.setattr(EnvKeys, "BOT_ADMIN_PASSWORD", "correct-secret")

    await fsm_context.set_state(BotAdminFSM.waiting_password)
    message = make_message(text="wrong-secret", user_id=900004)
    await process_bot_admin_password(message, fsm_context)

    message.delete.assert_awaited_once()
    assert await fsm_context.get_state() == BotAdminFSM.waiting_password
    assert "невер" in message.answer.call_args.args[0].lower()


async def test_correct_password_opens_existing_full_admin_panel(
    make_message, fsm_context, monkeypatch
):
    monkeypatch.setattr(EnvKeys, "OWNER_ID", 900005)
    monkeypatch.setattr(EnvKeys, "BOT_ADMIN_PASSWORD", "correct-secret")

    await fsm_context.set_state(BotAdminFSM.waiting_password)
    message = make_message(text="correct-secret", user_id=900005)

    with patch(
        "bot.handlers.admin.bot_panel.check_role_cached",
        new=AsyncMock(return_value=1023),
    ):
        await process_bot_admin_password(message, fsm_context)

    message.delete.assert_awaited_once()
    assert await fsm_context.get_state() is None
    message.answer.assert_called_once()
    callbacks = _callbacks(message.answer.call_args.kwargs["reply_markup"])
    assert {
        "shop_management",
        "goods_management",
        "categories_management",
        "user_management",
        "role_mgmt",
        "send_message",
        "promo_mgmt",
    }.issubset(callbacks)


async def test_password_falls_back_to_existing_admin_password(
    make_message, fsm_context, monkeypatch
):
    monkeypatch.setattr(EnvKeys, "OWNER_ID", 900006)
    monkeypatch.setattr(EnvKeys, "BOT_ADMIN_PASSWORD", "")
    monkeypatch.setattr(EnvKeys, "ADMIN_PASSWORD", "existing-admin-secret")

    await fsm_context.set_state(BotAdminFSM.waiting_password)
    message = make_message(text="existing-admin-secret", user_id=900006)

    with patch(
        "bot.handlers.admin.bot_panel.check_role_cached",
        new=AsyncMock(return_value=Permission.USE | Permission.OWN),
    ):
        await process_bot_admin_password(message, fsm_context)

    assert await fsm_context.get_state() is None
    message.answer.assert_called_once()


async def test_password_attempts_are_rate_limited(make_message, fsm_context, monkeypatch):
    monkeypatch.setattr(EnvKeys, "OWNER_ID", 900007)
    monkeypatch.setattr(EnvKeys, "BOT_ADMIN_PASSWORD", "correct-secret")

    await fsm_context.set_state(BotAdminFSM.waiting_password)
    for _ in range(bot_panel._MAX_PASSWORD_ATTEMPTS):
        message = make_message(text="wrong-secret", user_id=900007)
        await process_bot_admin_password(message, fsm_context)
        if await fsm_context.get_state() is None:
            await fsm_context.set_state(BotAdminFSM.waiting_password)

    locked_message = make_message(text="correct-secret", user_id=900007)
    await process_bot_admin_password(locked_message, fsm_context)

    assert await fsm_context.get_state() is None
    assert "слишком много" in locked_message.answer.call_args.args[0].lower()

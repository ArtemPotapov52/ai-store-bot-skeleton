from sqlalchemy import select

from bot.database.main import Database
from bot.database.models import PartnerApiKey, User
from bot.web.api.auth import (
    authenticate_api_key,
    issue_partner_api_key,
    revoke_partner_api_key,
)
from bot.handlers.user.api_keys import issue_api_key_handler, revoke_api_key_handler


async def test_api_key_is_stored_as_digest_and_bound_to_registered_account(user_factory):
    await user_factory(telegram_id=660001, balance=125)

    token = await issue_partner_api_key(660001)
    principal = await authenticate_api_key(token)

    assert token.startswith("ps_live_")
    assert principal.user_id == 660001
    assert principal.is_blocked is False
    async with Database().session() as session:
        stored = (await session.execute(
            select(PartnerApiKey).where(PartnerApiKey.user_id == 660001)
        )).scalar_one()
    assert stored.key_hash != token
    assert stored.key_prefix == token[:16]


async def test_rotating_api_key_invalidates_old_secret(user_factory):
    await user_factory(telegram_id=660002)

    old_token = await issue_partner_api_key(660002)
    new_token = await issue_partner_api_key(660002)

    assert old_token != new_token
    assert await authenticate_api_key(old_token) is None
    assert (await authenticate_api_key(new_token)).user_id == 660002


async def test_revoked_and_blocked_api_keys_cannot_authenticate(user_factory):
    await user_factory(telegram_id=660003)
    token = await issue_partner_api_key(660003)

    assert await revoke_partner_api_key(660003) is True
    assert await authenticate_api_key(token) is None

    replacement = await issue_partner_api_key(660003)
    async with Database().session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == 660003)
        )).scalar_one()
        user.is_blocked = True

    principal = await authenticate_api_key(replacement)
    assert principal is not None
    assert principal.is_blocked is True


async def test_api_key_cannot_be_issued_to_unknown_account():
    assert await issue_partner_api_key(669999) is None


async def test_private_bot_command_displays_new_key_once_and_protects_message(
    user_factory, make_message,
):
    await user_factory(telegram_id=660004)
    message = make_message("/apikey", user_id=660004)

    await issue_api_key_handler(message)

    answer = message.answer.call_args
    displayed = answer.args[0]
    token = displayed.split("<code>", 1)[1].split("</code>", 1)[0]
    assert token.startswith("ps_live_")
    assert answer.kwargs["protect_content"] is True
    assert (await authenticate_api_key(token)).user_id == 660004


async def test_private_bot_command_can_revoke_api_key(user_factory, make_message):
    await user_factory(telegram_id=660005)
    token = await issue_partner_api_key(660005)
    message = make_message("/revokeapikey", user_id=660005)

    await revoke_api_key_handler(message)

    assert await authenticate_api_key(token) is None
    assert "отключён" in message.answer.call_args.args[0]

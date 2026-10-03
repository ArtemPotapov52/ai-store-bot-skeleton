"""Private-chat lifecycle for partner API bearer credentials."""

from aiogram import F, Router
from aiogram.enums.chat_type import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.web.api.auth import issue_partner_api_key, revoke_partner_api_key

router = Router()


@router.message(Command("api-key", "apikey"), F.chat.type == ChatType.PRIVATE)
async def issue_api_key_handler(message: Message) -> None:
    if message.from_user is None:
        return

    token = await issue_partner_api_key(message.from_user.id)
    if token is None:
        await message.answer(
            "Сначала запустите бота командой /start. Если аккаунт заблокирован, "
            "обратитесь в поддержку."
        )
        return

    await message.answer(
        "Ключ партнёрского API создан. Скопируйте его сейчас: повторная команда "
        "выпустит новый ключ и отключит этот. Не отправляйте ключ посторонним.\n\n"
        f"<code>{token}</code>",
        parse_mode="HTML",
        protect_content=True,
    )


@router.message(Command("revokeapikey"), F.chat.type == ChatType.PRIVATE)
async def revoke_api_key_handler(message: Message) -> None:
    if message.from_user is None:
        return

    revoked = await revoke_partner_api_key(message.from_user.id)
    if revoked:
        await message.answer("Ключ партнёрского API отключён.")
    else:
        await message.answer("У аккаунта нет активного API-ключа.")

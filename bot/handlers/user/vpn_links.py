"""Owner-only recovery and manual issuance for VPN subscription links."""

from aiogram import F, Router
from aiogram.enums.chat_type import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.misc import EnvKeys
from bot.misc.vpn_subscription_proxy import (
    issue_vpn_subscription_link,
    revoke_vpn_subscription_links,
    vpn_proxy_is_configured,
)

router = Router()


def _command_user_id(message: Message) -> int | None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        return None
    value = parts[1].strip()
    if not value.isdecimal():
        return None
    user_id = int(value)
    return user_id if 0 < user_id < 2**63 else None


@router.message(Command("vpnlink"), F.chat.type == ChatType.PRIVATE)
async def issue_vpn_link_handler(message: Message) -> None:
    if message.from_user is None or message.from_user.id != EnvKeys.OWNER_ID:
        return
    user_id = _command_user_id(message)
    if user_id is None:
        await message.answer("Использование: <code>/vpnlink TELEGRAM_ID</code>", parse_mode="HTML")
        return
    if not vpn_proxy_is_configured():
        await message.answer("VPN-прокси не настроен. Ссылка не создана.")
        return

    link = await issue_vpn_subscription_link(user_id)
    if link is None:
        await message.answer("Не удалось создать ссылку: пользователь не найден или заблокирован.")
        return
    await message.answer(
        "Персональная ссылка создана. Передайте её только этому пользователю:\n\n"
        f"<code>{link}</code>",
        parse_mode="HTML",
        protect_content=True,
        disable_web_page_preview=True,
    )


@router.message(Command("vpnrevoke"), F.chat.type == ChatType.PRIVATE)
async def revoke_vpn_links_handler(message: Message) -> None:
    if message.from_user is None or message.from_user.id != EnvKeys.OWNER_ID:
        return
    user_id = _command_user_id(message)
    if user_id is None:
        await message.answer("Использование: <code>/vpnrevoke TELEGRAM_ID</code>", parse_mode="HTML")
        return
    if await revoke_vpn_subscription_links(user_id):
        await message.answer(
            "Все ссылки этого пользователя отозваны. Уже импортированные конфиги могут продолжать подключаться, "
            "пока VPN-провайдер не отзовёт их узлы."
        )
    else:
        await message.answer("Активных ссылок этого пользователя нет.")

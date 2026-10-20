from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database.methods import check_role_cached
from bot.database.methods.traffic import (
    create_traffic_campaign,
    delete_traffic_campaign,
    get_campaign_hourly_stats,
    get_traffic_campaign_by_id,
    list_traffic_campaigns,
    render_hourly_chart,
)
from bot.database.models import Permission
from bot.handlers.other import get_bot_info
from bot.i18n import esc, localize
from bot.keyboards import back, simple_buttons
from bot.keyboards.styles import colorize_markup
from bot.misc import EnvKeys
from bot.misc.currency import format_currency_amount
from bot.states.traffic_state import TrafficFSM

router = Router()


async def _has_admin_access(user_id: int) -> bool:
    """Return True if user is bot owner or has admin rights."""
    if user_id == EnvKeys.OWNER_ID:
        return True
    role = await check_role_cached(user_id) or 0
    return Permission.has_any_admin_perm(role)


@router.message(Command("traffic"))
@router.message(Command("zakupy"))
@router.callback_query(F.data == "traffic_campaigns")
async def traffic_campaigns_main_handler(event: Message | CallbackQuery, state: FSMContext):
    """Show list of all advertising / procurement campaigns."""
    user_id = event.from_user.id
    if not await _has_admin_access(user_id):
        if isinstance(event, CallbackQuery):
            await event.answer(localize("admin.menu.rights"), show_alert=True)
        else:
            await event.answer(localize("admin.menu.rights"))
        return

    await state.clear()
    campaigns = await list_traffic_campaigns()
    bot_username = await get_bot_info(event)

    text = (
        "📈 <b>Раздел «Закупы» (Рекламные кампании)</b>\n\n"
        "Здесь вы можете создавать ссылки для закупа рекламы и отслеживать переходы, "
        "регистрации и активность по часам.\n\n"
    )

    kb = InlineKeyboardBuilder()

    if campaigns:
        text += f"Всего активных закупок: <b>{len(campaigns)}</b>\nВыберите закуп для просмотра диаграммы:"
        for c in campaigns[:25]:
            btn_title = f"📢 {c['name']} (👥 {c['clicks']} | 🆕 {c['new_users']})"
            kb.button(text=btn_title, callback_data=f"traffic_view:{c['id']}:24")
        kb.adjust(1)
    else:
        text += "<i>У вас пока нет созданных ссылок для закупа. Нажмите кнопку ниже, чтобы создать первую ссылку.</i>"

    kb.row(
        InlineKeyboardButton(text="➕ Создать ссылку для закупа", callback_data="traffic_create")
    )
    kb.row(
        InlineKeyboardButton(text=localize("btn.back"), callback_data="console"),
        InlineKeyboardButton(text=localize("btn.main_menu"), callback_data="back_to_menu"),
    )
    markup = colorize_markup(kb.as_markup(), uniform="success")

    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "traffic_create")
async def traffic_create_prompt_handler(call: CallbackQuery, state: FSMContext):
    """Prompt admin for the campaign title/name."""
    if not await _has_admin_access(call.from_user.id):
        await call.answer(localize("admin.menu.rights"), show_alert=True)
        return

    await state.set_state(TrafficFSM.waiting_campaign_name)
    text = (
        "➕ <b>Создание ссылки для закупа рекламы</b>\n\n"
        "Введите название для закупа (например: название канала, ник блогера или ссылку, "
        "например: <code>Канал Новости</code> или <code>@major_tg</code>):"
    )
    await call.message.edit_text(
        text,
        reply_markup=back("traffic_campaigns"),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(TrafficFSM.waiting_campaign_name, F.text)
async def traffic_create_process_handler(message: Message, state: FSMContext):
    """Create the campaign and show the ready link."""
    if not await _has_admin_access(message.from_user.id):
        await message.answer(localize("admin.menu.rights"))
        return

    name = message.text.strip()
    if not name or len(name) > 120:
        await message.answer("❌ Название должно быть от 1 до 120 символов. Попробуйте снова:")
        return

    campaign = await create_traffic_campaign(
        name=name,
        created_by=message.from_user.id,
    )
    await state.clear()

    bot_username = await get_bot_info(message)
    full_link = f"https://t.me/{bot_username}?start={campaign['tag']}"

    text = (
        "✅ <b>Ссылка для закупа успешно создана!</b>\n\n"
        f"📌 <b>Название:</b> {esc(campaign['name'])}\n"
        f"🏷 <b>Тег:</b> <code>{campaign['tag']}</code>\n\n"
        f"🔗 <b>Готовая ссылка для рекламы:</b>\n"
        f"<code>{full_link}</code>\n\n"
        "<i>(нажмите на ссылку, чтобы скопировать её)</i>"
    )

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(
            text="📊 Открыть диаграмму и статистику",
            callback_data=f"traffic_view:{campaign['id']}:24",
        )
    )
    kb.row(
        InlineKeyboardButton(text="◀️ К списку закупов", callback_data="traffic_campaigns")
    )

    await message.answer(
        text,
        reply_markup=colorize_markup(kb.as_markup(), uniform="success"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("traffic_view:"))
async def traffic_view_handler(call: CallbackQuery):
    """Show details, statistics and hourly diagram for a campaign."""
    if not await _has_admin_access(call.from_user.id):
        await call.answer(localize("admin.menu.rights"), show_alert=True)
        return

    parts = call.data.split(":")
    try:
        campaign_id = int(parts[1])
        hours = int(parts[2]) if len(parts) > 2 else 24
    except (ValueError, IndexError):
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return

    campaign = await get_traffic_campaign_by_id(campaign_id)
    if not campaign:
        await call.answer("❌ Закуп не найден или был удалён", show_alert=True)
        await traffic_campaigns_main_handler(call, None)  # type: ignore[arg-type]
        return

    bot_username = await get_bot_info(call)
    stats = await get_campaign_hourly_stats(campaign_id, hours=hours)
    chart = render_hourly_chart(stats["hourly_data"])

    # Aggregate total purchases & revenue
    all_campaigns = await list_traffic_campaigns()
    matching = next((c for c in all_campaigns if c["id"] == campaign_id), None)
    purchases = matching["purchases"] if matching else 0
    revenue = matching["revenue"] if matching else 0
    rev_str = format_currency_amount(revenue, "RUB")

    full_link = f"https://t.me/{bot_username}?start={campaign['tag']}"

    text = (
        f"📊 <b>Закуп: {esc(campaign['name'])}</b>\n"
        f"🔗 Ссылка: <code>{full_link}</code>\n\n"
        f"👥 <b>Всего переходов:</b> {stats['total_clicks']}\n"
        f"🆕 <b>Новых пользователей:</b> {stats['new_users']}\n"
        f"🛒 <b>Покупок от пришедших:</b> {purchases}\n"
        f"💰 <b>Выручка от пришедших:</b> {rev_str}\n"
        f"🔥 <b>Пик переходов:</b> {stats['peak_hour']} ({stats['peak_clicks']} переходов)\n\n"
        f"📈 <b>Диаграмма переходов по часам ({hours} ч.):</b>\n"
        f"{chart}"
    )

    kb = InlineKeyboardBuilder()
    # Time window selector buttons
    kb.row(
        InlineKeyboardButton(
            text=f"{'🔘 ' if hours == 12 else ''}12 ч",
            callback_data=f"traffic_view:{campaign_id}:12",
        ),
        InlineKeyboardButton(
            text=f"{'🔘 ' if hours == 24 else ''}24 ч",
            callback_data=f"traffic_view:{campaign_id}:24",
        ),
        InlineKeyboardButton(
            text=f"{'🔘 ' if hours == 48 else ''}48 ч",
            callback_data=f"traffic_view:{campaign_id}:48",
        ),
        InlineKeyboardButton(
            text="🔄",
            callback_data=f"traffic_view:{campaign_id}:{hours}",
        ),
    )
    kb.row(
        InlineKeyboardButton(
            text="🗑 Удалить закуп",
            callback_data=f"traffic_delete_confirm:{campaign_id}",
        )
    )
    kb.row(
        InlineKeyboardButton(text="◀️ К списку закупов", callback_data="traffic_campaigns")
    )

    await call.message.edit_text(
        text,
        reply_markup=colorize_markup(kb.as_markup(), uniform="success"),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("traffic_delete_confirm:"))
async def traffic_delete_confirm_handler(call: CallbackQuery):
    """Ask confirmation before deleting campaign."""
    campaign_id = int(call.data.split(":")[1])
    campaign = await get_traffic_campaign_by_id(campaign_id)
    name = campaign["name"] if campaign else "закуп"

    text = f"❓ Вы действительно хотите удалить закуп <b>{esc(name)}</b> и всю статистику переходов по нему?"
    buttons = [
        ("❌ Да, удалить", f"traffic_delete_do:{campaign_id}"),
        ("◀️ Отмена", f"traffic_view:{campaign_id}:24"),
    ]
    await call.message.edit_text(
        text,
        reply_markup=simple_buttons(buttons, per_row=1),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("traffic_delete_do:"))
async def traffic_delete_execute_handler(call: CallbackQuery, state: FSMContext):
    """Execute deletion."""
    campaign_id = int(call.data.split(":")[1])
    await delete_traffic_campaign(campaign_id)
    await call.answer("✅ Закуп успешно удалён!", show_alert=True)
    await traffic_campaigns_main_handler(call, state)

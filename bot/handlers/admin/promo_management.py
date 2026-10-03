from datetime import datetime

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pydantic import ValidationError

from bot.database.models import Permission
from bot.database.methods.create import create_promo_code
from bot.database.methods.delete import delete_promo_code
from bot.database.methods.update import toggle_promo_code
from bot.database.methods.lazy_queries import query_promo_codes
from bot.database.methods.read import get_promo_code
from bot.database.methods.audit import log_audit
from bot.filters import HasPermissionFilter
from bot.keyboards.inline import back, simple_buttons, lazy_paginated_keyboard
from bot.keyboards.styles import colorize_markup
from bot.misc import LazyPaginator, PromoCodeRequest
from bot.misc.timezone import format_moscow_datetime, moscow_day_window
from bot.i18n import localize, esc
from bot.states import PromoFSM

router = Router()


# --- Promo list ---

def _promo_list_label(p: dict) -> str:
    """Render a balance-credit promo row."""
    state_icon = '✅' if p['is_active'] else '⛔'
    return f"{state_icon} {p['code']} ({p['current_uses']}/{p['max_uses'] or '∞'})"


@router.callback_query(F.data == "promo_mgmt", HasPermissionFilter(permission=Permission.PROMO_MANAGE))
async def promo_management_handler(call: CallbackQuery, state: FSMContext):
    paginator = LazyPaginator(query_promo_codes, per_page=10)
    page_items = await paginator.get_page(0)

    if not page_items:
        buttons = [
            (localize("admin.promo.create"), "promo_create"),
            (localize("btn.back"), "console"),
        ]
        await call.message.edit_text(
            localize("admin.promo.title") + "\n\n" + localize("admin.promo.list_empty"),
            reply_markup=simple_buttons(buttons),
        )
        await state.clear()
        return

    markup = await lazy_paginated_keyboard(
        paginator=paginator,
        item_text=_promo_list_label,
        item_callback=lambda p: f"promo_v_{p['id']}",
        page=0,
        back_cb=None,
        nav_cb_prefix="promos-page_",
    )

    # Rebuild: add Create, then Back (back always last)
    kb = InlineKeyboardBuilder()
    kb.attach(InlineKeyboardBuilder.from_markup(markup))
    kb.row(InlineKeyboardButton(text=localize("admin.promo.create"), callback_data="promo_create"))
    kb.row(InlineKeyboardButton(text=localize("btn.back"), callback_data="console"))
    markup = colorize_markup(kb.as_markup())

    await call.message.edit_text(localize("admin.promo.title"), reply_markup=markup)


@router.callback_query(F.data.startswith("promos-page_"), HasPermissionFilter(permission=Permission.PROMO_MANAGE))
async def navigate_promos(call: CallbackQuery, state: FSMContext):
    try:
        page = max(0, int(call.data.split("_", 1)[1]))
    except (ValueError, IndexError):
        await call.answer(localize("errors.pagination_invalid"))
        return
    paginator = LazyPaginator(query_promo_codes, per_page=10)

    markup = await lazy_paginated_keyboard(
        paginator=paginator,
        item_text=_promo_list_label,
        item_callback=lambda p: f"promo_v_{p['id']}",
        page=page,
        back_cb=None,
        nav_cb_prefix="promos-page_",
    )

    kb = InlineKeyboardBuilder()
    kb.attach(InlineKeyboardBuilder.from_markup(markup))
    kb.row(InlineKeyboardButton(text=localize("admin.promo.create"), callback_data="promo_create"))
    kb.row(InlineKeyboardButton(text=localize("btn.back"), callback_data="console"))
    markup = colorize_markup(kb.as_markup())

    await call.message.edit_text(localize("admin.promo.title"), reply_markup=markup)


# --- View / toggle / delete promo ---

async def _promo_binding_label(s, promo) -> str:
    """Human-readable description of what a promo applies to.

    A scoped promo whose bound category/item was deleted keeps its scope but
    loses the id (ON DELETE SET NULL). It rejects every product from then on, so
    say that plainly instead of letting it read as healthy.
    """
    from bot.database.models.main import Categories, Goods
    from sqlalchemy import select

    if promo.scope == "category":
        if promo.category_id is None:
            return localize("admin.promo.binding.dangling")
        name = (await s.execute(
            select(Categories.name).where(Categories.id == promo.category_id)
        )).scalar()
        return localize("admin.promo.binding.on_category", name=esc(name or "?"))

    if promo.scope == "item":
        if promo.item_id is None:
            return localize("admin.promo.binding.dangling")
        name = (await s.execute(
            select(Goods.name).where(Goods.id == promo.item_id)
        )).scalar()
        return localize("admin.promo.binding.on_item", name=esc(name or "?"))

    return localize("admin.promo.binding.none")


async def _show_promo_view(message, promo_id: int) -> bool:
    """Render the promo detail view on a Message. False if the promo is gone."""
    from bot.database.models.main import PromoCodes
    from bot.database import Database
    from sqlalchemy import select

    async with Database().session() as s:
        promo = (await s.execute(select(PromoCodes).where(PromoCodes.id == promo_id))).scalars().first()
        if not promo:
            return False

        if promo.discount_type == "balance":
            text = localize(
                "admin.promo.detail",
                code=esc(promo.code),
                discount_value=promo.discount_value,
                current_uses=promo.current_uses,
                max_uses=promo.max_uses or "∞",
                expires_at=format_moscow_datetime(promo.expires_at) if promo.expires_at else "—",
                is_active="✅" if promo.is_active else "⛔",
            )
        else:
            # Legacy purchase-discount records remain in storage for audit, but
            # they are not listed, redeemable, editable, or activatable.
            text = localize("admin.promo.purchase_disabled", code=esc(promo.code))

    buttons = []
    if promo.discount_type == "balance":
        toggle_text = localize(
            "admin.promo.btn.deactivate" if promo.is_active else "admin.promo.btn.activate"
        )
        buttons.append((toggle_text, f"promo_toggle_{promo_id}"))
    buttons.extend([
        (localize("admin.promo.btn.delete"), f"promo_d_{promo_id}"),
        (localize("btn.back"), "promo_mgmt"),
    ])
    await message.edit_text(text, reply_markup=simple_buttons(buttons))
    return True


def _promo_id_arg(data: str) -> int | None:
    """Parse the promo id out of promo_v_/promo_toggle_/promo_d_/promo_dc_ data."""
    try:
        return int(data.split("_")[2])
    except (ValueError, IndexError):
        return None


@router.callback_query(F.data.startswith("promo_v_"), HasPermissionFilter(permission=Permission.PROMO_MANAGE))
async def view_promo(call: CallbackQuery, state: FSMContext):
    promo_id = _promo_id_arg(call.data)
    if promo_id is None:
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return
    if not await _show_promo_view(call.message, promo_id):
        await call.answer(localize("promo.not_found"), show_alert=True)


@router.callback_query(F.data.startswith("promo_toggle_"), HasPermissionFilter(permission=Permission.PROMO_MANAGE))
async def toggle_promo(call: CallbackQuery, state: FSMContext):
    promo_id = _promo_id_arg(call.data)
    if promo_id is None:
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return
    new_state = await toggle_promo_code(promo_id)
    if new_state is None:
        await call.answer(localize("promo.not_found"), show_alert=True)
        return

    msg = localize("admin.promo.toggled_on") if new_state else localize("admin.promo.toggled_off")
    await call.answer(msg)
    await log_audit("promo_toggle", user_id=call.from_user.id, resource_type="PromoCode", resource_id=str(promo_id))
    await _show_promo_view(call.message, promo_id)


@router.callback_query(F.data.startswith("promo_d_"), HasPermissionFilter(permission=Permission.PROMO_MANAGE))
async def confirm_delete_promo(call: CallbackQuery, state: FSMContext):
    promo_id = _promo_id_arg(call.data)
    if promo_id is None:
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return

    from bot.database.models.main import PromoCodes
    from bot.database import Database
    from sqlalchemy import select

    async with Database().session() as s:
        promo = (await s.execute(select(PromoCodes).where(PromoCodes.id == promo_id))).scalars().first()
        code = promo.code if promo else "?"

    buttons = [
        (localize("btn.yes"), f"promo_dc_{promo_id}"),
        (localize("btn.no"), f"promo_v_{promo_id}"),
    ]
    await call.message.edit_text(
        localize("admin.promo.confirm_delete", code=esc(code)),
        reply_markup=simple_buttons(buttons),
    )


@router.callback_query(F.data.startswith("promo_dc_"), HasPermissionFilter(permission=Permission.PROMO_MANAGE))
async def delete_promo_confirmed(call: CallbackQuery, state: FSMContext):
    promo_id = _promo_id_arg(call.data)
    if promo_id is None:
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return
    await delete_promo_code(promo_id)
    await log_audit("promo_delete", user_id=call.from_user.id, resource_type="PromoCode", resource_id=str(promo_id))
    await call.answer(localize("admin.promo.deleted"))
    await promo_management_handler(call, state)


# --- Promo creation flow ---

@router.callback_query(F.data == "promo_create", HasPermissionFilter(permission=Permission.PROMO_MANAGE))
async def promo_create_start(call: CallbackQuery, state: FSMContext):
    await call.message.edit_text(localize("admin.promo.prompt.code"), reply_markup=back("promo_mgmt"))
    await state.set_state(PromoFSM.waiting_code)


@router.message(PromoFSM.waiting_code, F.text)
async def promo_receive_code(message: Message, state: FSMContext):
    try:
        code = PromoCodeRequest(code=message.text or "").code
    except ValidationError:
        await message.answer(localize("admin.promo.invalid_code"), reply_markup=back("promo_mgmt"))
        return

    existing = await get_promo_code(code)
    if existing:
        await message.answer(localize("admin.promo.code_exists"), reply_markup=back("promo_mgmt"))
        return

    await state.update_data(promo_code=code)
    buttons = [
        (localize("admin.promo.type.balance"), "promo_type_balance"),
        (localize("btn.back"), "promo_mgmt"),
    ]
    await message.answer(localize("admin.promo.prompt.type"), reply_markup=simple_buttons(buttons))
    await state.set_state(PromoFSM.waiting_type)


@router.callback_query(F.data.startswith("promo_type_"), PromoFSM.waiting_type)
async def promo_receive_type(call: CallbackQuery, state: FSMContext):
    dtype = call.data.split("_")[2]
    if dtype != "balance":
        await call.answer(localize("admin.promo.invalid_value"), show_alert=True)
        return
    await state.update_data(promo_type=dtype)
    type_label = localize("admin.promo.type.balance")
    await call.message.edit_text(
        localize("admin.promo.prompt.value", type=type_label),
        reply_markup=back("promo_mgmt"),
    )
    await state.set_state(PromoFSM.waiting_value)


@router.message(PromoFSM.waiting_value, F.text)
async def promo_receive_value(message: Message, state: FSMContext):
    try:
        value = float(message.text.strip())
        if value <= 0:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer(localize("admin.promo.invalid_value"), reply_markup=back("promo_mgmt"))
        return

    await state.update_data(promo_value=value)
    await message.answer(localize("admin.promo.prompt.max_uses"), reply_markup=back("promo_mgmt"))
    await state.set_state(PromoFSM.waiting_max_uses)


@router.message(PromoFSM.waiting_max_uses, F.text)
async def promo_receive_max_uses(message: Message, state: FSMContext):
    try:
        max_uses = int(message.text.strip())
        if max_uses < 0:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer(localize("admin.promo.invalid_value"), reply_markup=back("promo_mgmt"))
        return

    await state.update_data(promo_max_uses=max_uses)
    await message.answer(localize("admin.promo.prompt.expires"), reply_markup=back("promo_mgmt"))
    await state.set_state(PromoFSM.waiting_expires)


@router.message(PromoFSM.waiting_expires, F.text)
async def promo_receive_expires(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "0":
        expires_at = None
    else:
        try:
            _, expires_at = moscow_day_window(text)
        except ValueError:
            await message.answer(localize("admin.promo.invalid_date"), reply_markup=back("promo_mgmt"))
            return
        # The named Moscow day remains valid until the next Moscow midnight.

    await state.update_data(promo_expires=expires_at.isoformat() if expires_at else None)
    await _finalize_promo_creation(message, state, message.from_user.id)


async def _finalize_promo_creation(target, state: FSMContext, user_id: int):
    """Create promo code from accumulated state data. `target` is Message (has .answer)."""
    data = await state.get_data()
    expires_at = datetime.fromisoformat(data['promo_expires']) if data.get('promo_expires') else None

    # Balance promos credit real money: an unlimited one (max_uses=0) is a
    # money printer for anyone with a fresh Telegram account. Refuse it here.
    if data.get('promo_type') == 'balance' and not int(data.get('promo_max_uses') or 0) > 0:
        reply = target.answer if hasattr(target, 'answer') else target.edit_text
        await reply(
            localize("admin.promo.invalid_value"),
            reply_markup=back("promo_mgmt"),
        )
        await log_audit(
            "promo_create_blocked",
            user_id=user_id,
            resource_type="PromoCode",
            resource_id=str(data.get('promo_code')),
            details="balance promo with unlimited uses refused",
        )
        await state.clear()
        return

    promo_id = await create_promo_code(
        code=data['promo_code'],
        discount_type=data['promo_type'],
        discount_value=data['promo_value'],
        max_uses=data.get('promo_max_uses', 0),
        expires_at=expires_at,
    )

    reply = target.answer if hasattr(target, 'answer') else target.edit_text

    if promo_id:
        await reply(
            localize("admin.promo.created", code=esc(data['promo_code'])),
            reply_markup=back("promo_mgmt"),
        )
        await log_audit(
            "promo_create",
            user_id=user_id,
            resource_type="PromoCode",
            resource_id=data['promo_code'],
        )
    else:
        await reply(localize("admin.promo.code_exists"), reply_markup=back("promo_mgmt"))

    await state.clear()

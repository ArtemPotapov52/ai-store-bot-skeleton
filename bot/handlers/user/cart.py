from collections import Counter
from decimal import Decimal

from aiogram import Router, F
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest

from bot.database.methods.create import add_to_cart, CART_MAX_QTY_PER_ITEM
from bot.database.methods.read import get_cart_items
from bot.database.methods.update import set_cart_item_quantity
from bot.database.methods.delete import remove_from_cart, clear_cart
from bot.database.methods.transactions import checkout_cart_transaction
from bot.keyboards.inline import back, simple_buttons, cart_keyboard
from bot.misc import EnvKeys
from bot.misc.screens import edit_screen
from bot.i18n import localize, esc, format_dt
from bot.handlers.user.main import ensure_user

router = Router()

# A checkout can deliver hundreds of units; the receipt only ever shows this many, then defers to the paginated purchases list.
RECEIPT_MAX_BUTTONS = 10


async def _cart_view_data(user_id: int) -> tuple[list[dict], dict[str, dict], dict[int, dict], Decimal]:
    """Load everything a cart render or total needs in three queries total.

    Returns (items, info_map, line_data, total); cart lines use only current
    catalog prices. Legacy promo and sale metadata are ignored.
    Lines whose item no longer exists are absent from line_data.
    """
    items = await get_cart_items(user_id)
    if not items:
        return [], {}, {}, Decimal(0)

    from bot.database.methods.read import get_items_info
    from bot.database.methods import effective_price
    info_map = await get_items_info([item['item_name'] for item in items])
    line_data: dict[int, dict] = {}

    for item in items:
        info = info_map.get(item['item_name'])
        if not info:
            continue
        qty = item['quantity']
        base_price, _on_sale, _original = effective_price(info)
        line_total = (base_price * qty).quantize(Decimal("0.01"))
        line_data[item['id']] = {
            'line_total': line_total,
            'qty': qty,
            'unit_price': base_price,
        }

    total = sum((ld['line_total'] for ld in line_data.values()), Decimal("0"))
    return items, info_map, line_data, total


async def _show_cart(call: CallbackQuery):
    """Shared logic: render cart view."""
    user_id = call.from_user.id
    items, info_map, line_data, real_total = await _cart_view_data(user_id)

    if not items:
        await edit_screen(
            call,
            localize("cart.title") + "\n\n" + localize("cart.empty"),
            reply_markup=back("profile"),
            screen="cart",
        )
        return

    lines = [localize("cart.title"), ""]

    for item in items:
        qty = item['quantity']
        name = esc(item['item_name'])
        ld = line_data.get(item['id'])
        if ld is None:
            lines.append(localize(
                "cart.item", name=name, qty=qty,
                price='?', currency=EnvKeys.PAY_CURRENCY,
            ))
            continue

        lines.append(localize(
            "cart.item", name=name, qty=qty,
            price=ld['line_total'], currency=EnvKeys.PAY_CURRENCY,
        ))

    lines.append(localize("cart.total", total=real_total, currency=EnvKeys.PAY_CURRENCY))

    try:
        await edit_screen(
            call,
            "\n".join(lines),
            reply_markup=cart_keyboard(items),
            screen="cart",
        )
    except TelegramBadRequest as e:
        # Stepping quantity up then back down re-renders an identical message.
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data == "add_to_cart")
async def add_to_cart_handler(call: CallbackQuery, state: FSMContext):
    if await ensure_user(call.from_user.id) is None:
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return
    data = await state.get_data()
    item_name = data.get('csrf_item')
    if not item_name:
        await call.answer(localize("cart.item_not_found"), show_alert=True)
        return

    success, msg = await add_to_cart(call.from_user.id, item_name)
    if success:
        await call.answer(localize("cart.added", name=item_name))
    else:
        error_map = {
            "cart_full": localize("cart.full"),
            "item_not_found": localize("cart.item_not_found"),
            "cart_qty_max": localize("cart.qty_max", max=CART_MAX_QTY_PER_ITEM),
            "cart_qty_min": localize("cart.qty_min"),
            "cart_conflict": localize("errors.something_wrong"),
            "invalid_quantity": localize("errors.something_wrong"),
        }
        await call.answer(error_map.get(msg, msg), show_alert=True)


@router.callback_query(F.data.startswith("cart_qty:"))
async def cart_qty_handler(call: CallbackQuery, state: FSMContext):
    """Step a cart line's quantity up or down. Format: cart_qty:{id}:{delta}"""
    try:
        parts = call.data.split(":")
        cart_item_id = int(parts[1])
        delta = int(parts[2])
    except (ValueError, IndexError):
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return

    ok, code, _new_qty = await set_cart_item_quantity(cart_item_id, call.from_user.id, delta)
    if not ok:
        error_map = {
            "item_not_found": localize("cart.item_not_found"),
            "cart_qty_max": localize("cart.qty_max", max=CART_MAX_QTY_PER_ITEM),
            "cart_qty_min": localize("cart.qty_min"),
        }
        await call.answer(error_map.get(code, code), show_alert=True)
    elif code == "removed":
        await call.answer(localize("cart.removed"))
    else:
        await call.answer()

    await _show_cart(call)


@router.callback_query(F.data == "cart")
async def view_cart_handler(call: CallbackQuery, state: FSMContext):
    await _show_cart(call)


@router.callback_query(F.data.startswith("cart_remove:"))
async def remove_cart_item_handler(call: CallbackQuery, state: FSMContext):
    try:
        cart_item_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return
    removed = await remove_from_cart(cart_item_id, user_id=call.from_user.id)
    if removed:
        await call.answer(localize("cart.removed"))
    else:
        await call.answer(localize("cart.item_not_found"), show_alert=True)
    await _show_cart(call)


@router.callback_query(F.data == "cart_clear")
async def clear_cart_handler(call: CallbackQuery, state: FSMContext):
    await clear_cart(call.from_user.id)
    await call.answer(localize("cart.cleared"))
    await _show_cart(call)


def _receipt_keyboard(results: list[dict]) -> InlineKeyboardMarkup:
    """Build the receipt's per-unit buttons, capped.
    """
    per_name = Counter(r['item_name'] for r in results)
    shown = Counter()

    buttons = []
    for r in results[:RECEIPT_MAX_BUTTONS]:
        name = r['item_name']
        shown[name] += 1
        # Several units of one position would otherwise be indistinguishable.
        label = f"📦 {name}" if per_name[name] == 1 else f"📦 {name} ({shown[name]})"
        buttons.append((label, f"bought-item:{r['bought_id']}:cart_receipt"))

    if len(results) > RECEIPT_MAX_BUTTONS:
        buttons.append((localize("btn.cart_receipt_all"), "bought_items"))
    buttons.append((localize("btn.back"), "profile"))
    return simple_buttons(buttons)


def _slim_receipt(results: list[dict]) -> list[dict]:
    """Drop the delivered secret before the receipt goes into FSM storage.

    Only bought_id / item_name / bought_datetime are needed to re-render, and
    FSM state is serialised into Redis when it is enabled.
    """
    return [
        {
            "item_name": r["item_name"],
            "bought_id": r["bought_id"],
            "bought_datetime": r["bought_datetime"],
            "price": r["price"],
        }
        for r in results
    ]


def _receipt_total(results: list[dict]) -> Decimal:
    """Sum a checkout's per-unit prices back into the line total.
    """
    return sum(
        (Decimal(str(r['price'])) for r in results), Decimal(0)
    ).quantize(Decimal("0.01"))


@router.callback_query(F.data == "cart_checkout")
async def cart_checkout_handler(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    items, _info, _lines, total = await _cart_view_data(user_id)
    count = sum(item['quantity'] for item in items)

    # Remember the total the user is being asked to confirm, so checkout can
    # detect a catalog price change and refuse to charge a different amount.
    await state.update_data(cart_expected_total=str(total))

    buttons = [
        (localize("btn.yes"), "cart_checkout_confirm"),
        (localize("btn.no"), "cart"),
    ]
    await edit_screen(
        call,
        localize("cart.checkout_confirm", count=count, total=total, currency=EnvKeys.PAY_CURRENCY),
        reply_markup=simple_buttons(buttons),
        screen="cart",
    )


@router.callback_query(F.data == "cart_checkout_confirm")
async def cart_checkout_confirm_handler(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    if await ensure_user(user_id) is None:
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return
    await call.answer(localize("shop.purchase.processing"))

    data = await state.get_data()
    expected_raw = data.get("cart_expected_total")
    expected_total = Decimal(expected_raw) if expected_raw is not None else None

    success, msg, results = await checkout_cart_transaction(user_id, expected_total=expected_total)

    if not success:
        reason_map = {
            "user_not_found": "User not found",
            "cart_empty": localize("cart.empty"),
            "cart_items_unavailable": localize("cart.items_unavailable"),
            "out_of_stock": localize("cart.out_of_stock"),
            "invalid_quantity": localize("cart.qty_range"),
            "vpn_unconfigured": localize("shop.vpn_unavailable"),
            "insufficient_funds": localize("shop.insufficient_funds"),
            "transaction_error": localize("errors.something_wrong"),
            "price_changed": localize("cart.price_changed"),
            "invalid_price": localize("errors.something_wrong"),
        }
        await edit_screen(
            call,
            localize("cart.checkout_fail", reason=reason_map.get(msg, msg)),
            reply_markup=back("cart"),
            screen="cart",
        )
        return

    total = _receipt_total(results)
    dt = format_dt(results[0]['bought_datetime']) if results else ""

    # Save results in state for cart_receipt back navigation
    await state.update_data(
        cart_receipt_results=_slim_receipt(results),
        cart_receipt_total=str(total),
    )

    await edit_screen(
        call,
        localize(
            "cart.checkout_receipt",
            count=len(results),
            total=total,
            currency=EnvKeys.PAY_CURRENCY,
            datetime=dt,
        ),
        reply_markup=_receipt_keyboard(results),
        screen="cart",
    )

    from bot.database.methods.audit import log_audit_bg
    log_audit_bg(
        "cart_checkout",
        user_id=user_id,
        resource_type="Cart",
        details=f"items={len(results)}, total={total}",
    )


@router.callback_query(F.data == "cart_receipt")
async def cart_receipt_handler(call: CallbackQuery, state: FSMContext):
    """Re-render the cart checkout receipt (back from bought-item detail)."""
    data = await state.get_data()
    results = data.get("cart_receipt_results")
    total = data.get("cart_receipt_total") or (_receipt_total(results) if results else 0)

    if not results:
        await edit_screen(
            call,
            localize("cart.empty"),
            reply_markup=back("profile"),
            screen="cart",
        )
        return

    dt = format_dt(results[0].get("bought_datetime", ""))

    await edit_screen(
        call,
        localize(
            "cart.checkout_receipt",
            count=len(results),
            total=total,
            currency=EnvKeys.PAY_CURRENCY,
            datetime=dt,
        ),
        reply_markup=_receipt_keyboard(results),
        screen="cart",
    )

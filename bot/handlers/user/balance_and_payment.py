import hashlib
import json
import secrets
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_UP
from urllib.parse import quote

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery, SuccessfulPayment
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot.database.methods import (
    get_user_referral,
    buy_item_transaction,
    process_payment_with_referral,
    create_pending_payment,
    bind_pending_payment,
    mark_pending_payment_failed,
    check_user,
    get_item_info_cached,
    select_item_values_amount_cached,
    check_value_cached,
    get_category_name_by_id,
)
from bot.catalog_labels import category_intro
from bot.keyboards import (
    back, payment_menu, close, get_payment_choice, admin_payment_keyboard,
    purchase_keyboard, simple_buttons,
)
from bot.logger_mesh import logger
from bot.database.methods.audit import log_audit
from bot.database.methods.cache_utils import safe_create_task
from bot.misc import EnvKeys, ItemPurchaseRequest, validate_telegram_id, validate_money_amount, PaymentRequest
from bot.handlers.other import _any_payment_method_enabled, is_safe_item_name, caller_name
from bot.handlers.user.main import ensure_user
from bot.misc.metrics import get_metrics
from bot.misc.services import (
    CryptoPayAPI, CryptoPayAPIError, XRocketPayAPI, XRocketAPIError,
    send_fiat_invoice,
)
from bot.misc.services.platega import (
    PlategaAPI,
    PlategaAPIError,
    PlategaCallbackError,
    platega_event_from_status,
    process_platega_event,
)
from bot.misc.services.payment import _minor_units_for, payload_amount
from bot.database.methods.pricing import (
    MAX_PURCHASE_QUANTITY, effective_price, purchase_quantity_limits,
)
from bot.misc.screens import answer_screen, edit_screen
from bot.filters import ValidAmountFilter
from bot.i18n import localize, esc, format_dt
from bot.states import BalanceStates, ShopStates

router = Router()


def _purchase_delivery_text(items: list[dict]) -> str:
    """Keep an infinite/manual delivery instruction readable for bulk orders."""
    values = dict.fromkeys(
        str(item.get("value") or "") for item in items if str(item.get("value") or "")
    )
    return "\n\n".join(values)


async def _fail_payment_intent(provider: str, intent_id: str) -> None:
    """Best-effort state transition for an invoice that was not created."""
    try:
        await mark_pending_payment_failed(provider, intent_id)
    except Exception:
        logger.error("Could not mark payment intent %s/%s failed", provider, intent_id, exc_info=True)


def _balance_prompt_keyboard():
    """Navigation for the amount prompt, including a direct main-menu exit."""
    return simple_buttons([
        (localize("btn.back"), "profile"),
        (localize("btn.main_menu"), "back_to_menu"),
    ])


async def _topup_amount_for_item(
        user_id: int,
        item_name: str,
        quantity: int = 1,
) -> int | None:
    """Return the whole-unit amount needed to buy an item right now.

    The purchase transaction remains the source of truth.  This helper only
    prepares a convenient suggested top-up after that transaction reports an
    insufficient balance; it rounds up so the suggested payment can never be
    one kopeck short of the current price.
    """
    user = await check_user(user_id)
    item = await get_item_info_cached(item_name)
    if not user or not item:
        return None

    unit_price, _on_sale, _original = effective_price(item)
    quantity = max(1, min(int(quantity or 1), MAX_PURCHASE_QUANTITY))
    total = (unit_price * quantity).quantize(Decimal("0.01"))

    balance = Decimal(str(user.get("balance") or 0))
    missing = max(total - balance, Decimal("0.00"))
    if missing <= 0:
        return None

    suggested = int(missing.to_integral_value(rounding=ROUND_UP))
    return max(suggested, int(EnvKeys.MIN_AMOUNT))


async def _item_purchase_quote(
        user_id: int,
        item_name: str,
        quantity: int,
) -> dict | None:
    """Calculate the current server-side quote shown before purchase."""
    user = await check_user(user_id)
    item = await get_item_info_cached(item_name)
    if not user or not item:
        return None

    if isinstance(quantity, bool):
        return None
    try:
        min_quantity, max_quantity = purchase_quantity_limits(item)
        quantity = int(quantity)
    except (TypeError, ValueError):
        return None
    if not min_quantity <= quantity <= max_quantity:
        return None
    unit_price, _on_sale, _original = effective_price(item)
    base_total = (unit_price * quantity).quantize(Decimal("0.01"))
    total_price = base_total

    balance = Decimal(str(user.get("balance") or 0))
    return {
        "item_name": item_name,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_price": total_price,
        "balance": balance,
        "can_afford": balance >= total_price,
    }


async def _render_purchase_choice(
        call,
        state: FSMContext,
        *,
        quantity: int | None = None,
        message_input: bool = False,
):
    """Render the quantity/confirmation screen in the current message."""
    async def show(text: str, *, reply_markup=None, image_ref=None):
        render = answer_screen if message_input else edit_screen
        return await render(
            call,
            text,
            reply_markup=reply_markup,
            screen="product",
            image_ref=image_ref,
        )

    async def fail(key: str):
        text = localize(key)
        if message_input:
            await show(
                text,
                reply_markup=simple_buttons([
                    (localize("btn.main_menu"), "back_to_menu"),
                ]),
                image_ref=(await state.get_data()).get("item_image_ref"),
            )
        else:
            await call.answer(text, show_alert=True)
        return None

    data = await state.get_data()
    item_name = data.get("csrf_item")
    if not item_name:
        return await fail("middleware.security.invalid_csrf")

    item = await get_item_info_cached(item_name)
    if not item or not item.get("is_active", True):
        return await fail("shop.item.not_found")

    is_infinite = await check_value_cached(item_name)
    # select_item_values_amount already combines individual rows with the
    # simple counted stock (stock_quantity) — adding it again would double
    # the limit (e.g. 30 counted units shown as 60).
    available = await select_item_values_amount_cached(item_name)
    try:
        min_quantity, configured_max_quantity = purchase_quantity_limits(item)
    except ValueError:
        logger.error("Invalid quantity range configured for product %s", item_name)
        return await fail("errors.something_wrong")
    max_quantity = (
        configured_max_quantity if is_infinite
        else min(int(available or 0), configured_max_quantity)
    )
    if max_quantity < min_quantity:
        await show(
            localize("shop.out_of_stock"),
            reply_markup=simple_buttons([
                (localize("btn.back"), "back_to_item"),
                (localize("btn.main_menu"), "back_to_menu"),
            ]),
            image_ref=data.get("item_image_ref"),
        )
        return None

    quantity = quantity if quantity is not None else data.get("purchase_quantity", 1)
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        quantity = min_quantity
    quantity = max(min_quantity, min(quantity, max_quantity))
    quote = await _item_purchase_quote(
        call.from_user.id,
        item_name,
        quantity,
    )
    if quote is None:
        return await fail("errors.something_wrong")

    # Remove legacy promo state so an old session can never apply it.
    await state.update_data(applied_promo=None)

    await state.update_data(
        purchase_quantity=quantity,
        purchase_min_quantity=min_quantity,
        purchase_max_quantity=max_quantity,
        purchase_total=str(quote["total_price"]),
        purchase_unit_price=str(quote["unit_price"]),
    )
    await state.set_state(ShopStates.confirming_purchase)

    text = localize(
        "shop.purchase.choose_quantity" if quote["can_afford"] else "shop.purchase.insufficient",
        item_name=esc(item_name),
        quantity=quantity,
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        unit_price=quote["unit_price"],
        total_price=quote["total_price"],
        balance=quote["balance"],
        missing=max(quote["total_price"] - quote["balance"], Decimal("0.00")),
        currency=EnvKeys.PAY_CURRENCY,
    )
    category_name = None
    category_id = item.get("category_id")
    if category_id:
        category_name = await get_category_name_by_id(int(category_id))
    intro = category_intro(category_name)
    if intro:
        text = f"{intro}\n\n{text}"
    await show(
        text,
        reply_markup=purchase_keyboard(
            quantity,
            max_quantity,
            quote["can_afford"],
            min_quantity=min_quantity,
        ),
        image_ref=data.get("item_image_ref"),
    )
    return quote


def _payment_admin_username() -> str:
    """Return a safe Telegram username for manual top-up links."""
    raw = str(getattr(EnvKeys, "PAYMENT_ADMIN_USERNAME", "") or "")
    username = raw.strip().lstrip("@").strip()
    username = "".join(char for char in username if char.isalnum() or char == "_")
    return username or ""


def _payment_admin_link(amount: int | Decimal) -> tuple[str, str]:
    """Build a Telegram link and the exact message shown in its draft."""
    username = _payment_admin_username()
    message = localize(
        "payments.admin.prefilled",
        amount=amount,
        currency=EnvKeys.PAY_CURRENCY,
    )
    return (
        f"https://t.me/{username}?text={quote(message)}",
        username,
    )


async def _usd_hint(amount: int | Decimal) -> str:
    """Approximate USD value of a shop-currency amount for display purposes."""
    try:
        if str(EnvKeys.PAY_CURRENCY).upper() == "USD":
            return f"{Decimal(str(amount)):.2f}"
        if EnvKeys.XROCKET_PAY_TOKEN:
            xrocket = XRocketPayAPI()
            per_usdt = Decimal("1") / await xrocket.get_rate_to_asset(str(EnvKeys.PAY_CURRENCY))
            if per_usdt > 0:
                return f"{(Decimal(str(amount)) / per_usdt):.2f}"
    except Exception:
        logger.debug("USD hint unavailable", exc_info=True)
    return "—"


async def _notify_referrer_bonus(bot, user_id: int, amount: Decimal | int, payer_name: str, payer_id: int):
    """Send referral bonus notification to the referrer if applicable."""
    referral_id = await get_user_referral(user_id)
    if not referral_id or not EnvKeys.REFERRAL_PERCENT:
        return
    try:
        clamped_percent = min(max(EnvKeys.REFERRAL_PERCENT, 0), 99)
        bonus = (Decimal(clamped_percent) / Decimal(100) * Decimal(amount)).quantize(Decimal("0.01"))
        if bonus > 0:
            await bot.send_message(
                referral_id,
                localize('payments.referral.bonus',
                         amount=bonus, name=esc(payer_name),
                         id=payer_id, currency=EnvKeys.PAY_CURRENCY),
                reply_markup=close()
            )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.error(f"Failed to send referral notification to user {referral_id}: {e}")


@router.callback_query(F.data == "replenish_balance")
async def replenish_balance_callback_handler(call: CallbackQuery, state: FSMContext):
    """Ask user for the amount if at least one payment method is enabled."""
    if await ensure_user(call.from_user.id) is None:
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return
    if not _any_payment_method_enabled():
        await call.answer(localize("payments.not_configured"), show_alert=True)
        return

    await edit_screen(
        call,
        localize("payments.replenish_prompt", currency=EnvKeys.PAY_CURRENCY),
        reply_markup=_balance_prompt_keyboard(),
        screen="balance",
    )
    await state.set_state(BalanceStates.waiting_amount)


@router.message(BalanceStates.waiting_amount, ValidAmountFilter())
async def replenish_balance_amount(message: Message, state: FSMContext):
    """Store amount and show payment methods."""
    try:
        # Validate amount using Pydantic
        amount = validate_money_amount(
            message.text,
            min_amount=Decimal(EnvKeys.MIN_AMOUNT),
            max_amount=Decimal(EnvKeys.MAX_AMOUNT)
        )

        await state.update_data(
            amount=int(amount),
            test_payment_id=secrets.token_urlsafe(24),
        )

        await answer_screen(
            message,
            localize("payments.method_choose"),
            reply_markup=get_payment_choice(),
            screen="balance",
        )
        await state.set_state(BalanceStates.waiting_payment)

    except ValueError:
        await answer_screen(
            message,
            localize("payments.replenish_invalid",
                     min_amount=EnvKeys.MIN_AMOUNT,
                     max_amount=EnvKeys.MAX_AMOUNT,
                     currency=EnvKeys.PAY_CURRENCY),
            reply_markup=simple_buttons([
                (localize("btn.back"), "replenish_balance"),
                (localize("btn.main_menu"), "back_to_menu"),
            ]),
            screen="balance",
        )


@router.message(BalanceStates.waiting_amount)
async def invalid_amount(message: Message, state: FSMContext):
    """
    Tell user the amount is invalid.
    """
    await answer_screen(
        message,
        localize("payments.replenish_invalid",
                 min_amount=EnvKeys.MIN_AMOUNT,
                 max_amount=EnvKeys.MAX_AMOUNT,
                 currency=EnvKeys.PAY_CURRENCY),
        reply_markup=simple_buttons([
            (localize("btn.back"), "replenish_balance"),
            (localize("btn.main_menu"), "back_to_menu"),
        ]),
        screen="balance",
    )


@router.callback_query(F.data == "pay_stars")
async def retired_stars_payment_callback(call: CallbackQuery):
    """Tell users holding an old keyboard that Stars payments were removed."""
    await call.answer(localize("payments.method_unavailable"), show_alert=True)


@router.callback_query(
    BalanceStates.waiting_payment,
    F.data.in_([
        "pay_cryptopay",
        "pay_xrocket",
        "pay_manual_crypto",
        "pay_sbp_card",
        "pay_admin",
        "pay_fiat",
        "pay_test",
    ])
)
async def process_replenish_balance(call: CallbackQuery, state: FSMContext):
    """Create an invoice or show instructions for the chosen payment method."""
    data = await state.get_data()
    amount = data.get('amount')

    if amount is None:
        await call.answer(localize("payments.session_expired"), show_alert=True)
        from bot.database.methods import check_role_cached
        from bot.handlers.other import _parse_channel_username
        from bot.handlers.user.main import _main_markup, _main_text, ensure_user
        user_id = call.from_user.id
        await ensure_user(user_id)
        role = await check_role_cached(user_id) or 0
        await edit_screen(
            call,
            _main_text(call.from_user),
            reply_markup=_main_markup(role, _parse_channel_username()),
            screen="main-menu",
        )
        await state.clear()
        return

    # Map callback data to provider
    provider_map = {
        "pay_cryptopay": "cryptopay",
        "pay_xrocket": "xrocket",
        "pay_manual_crypto": "fiat",
        "pay_sbp_card": "platega",
        "pay_admin": "fiat",
        "pay_fiat": "fiat",
        "pay_test": "test",
    }
    provider = provider_map.get(call.data)

    try:
        # Validate payment request
        payment_request = PaymentRequest(
            amount=Decimal(amount),
            currency=EnvKeys.PAY_CURRENCY,
            provider=provider
        )

        amount_dec = payment_request.amount
        ttl_seconds = int(EnvKeys.PAYMENT_TIME)

        if call.data == "pay_sbp_card":
            merchant_id = str(getattr(EnvKeys, "PLATEGA_MERCHANT_ID", "") or "").strip()
            api_key = str(getattr(EnvKeys, "PLATEGA_API_KEY", "") or "").strip()
            if not merchant_id or not api_key:
                await edit_screen(
                    call,
                    localize("payments.platega.setup"),
                    reply_markup=back("replenish_balance"),
                    screen="balance",
                )
                return
            if payment_request.currency != "RUB":
                await edit_screen(
                    call,
                    localize("payments.platega.currency_unavailable"),
                    reply_markup=back("replenish_balance"),
                    screen="balance",
                )
                return

            intent_id = f"intent:platega:{secrets.token_urlsafe(24)}"
            await create_pending_payment(
                provider="platega",
                external_id=intent_id,
                user_id=call.from_user.id,
                amount=amount_dec,
                currency=payment_request.currency,
            )
            try:
                bot_info = await call.bot.me()
                if not bot_info.username:
                    raise PlategaAPIError(None, "Telegram bot username is unavailable")
                bot_url = f"https://t.me/{bot_info.username}"
                api = PlategaAPI()
                invoice = await api.create_sbp_transaction(
                    amount=amount_dec,
                    currency=payment_request.currency,
                    intent_id=intent_id,
                    user_id=call.from_user.id,
                    user_name=(
                        f"@{call.from_user.username}"
                        if call.from_user.username
                        else str(call.from_user.id)
                    ),
                    return_url=bot_url,
                    failed_url=bot_url,
                )
            except PlategaAPIError as exc:
                await _fail_payment_intent("platega", intent_id)
                await log_audit(
                    "platega_invoice_fail",
                    level="ERROR",
                    user_id=call.from_user.id,
                    resource_type="Payment",
                    details=f"http_status={exc.status_code}",
                )
                await call.answer(localize("payments.platega.create_fail"), show_alert=True)
                return
            except Exception:
                await _fail_payment_intent("platega", intent_id)
                await log_audit(
                    "platega_invoice_fail",
                    level="ERROR",
                    user_id=call.from_user.id,
                    resource_type="Payment",
                    details="unexpected transaction creation error",
                )
                await call.answer(localize("payments.platega.create_fail"), show_alert=True)
                return

            transaction_id = invoice["transactionId"]
            try:
                bound = await bind_pending_payment("platega", intent_id, transaction_id)
            except Exception:
                bound = False
            if not bound:
                await _fail_payment_intent("platega", intent_id)
                await log_audit(
                    "platega_invoice_link_fail",
                    level="ERROR",
                    user_id=call.from_user.id,
                    resource_type="Payment",
                    details="payment record could not be linked",
                )
                await call.answer(localize("payments.platega.create_fail"), show_alert=True)
                return

            provider_ttl = ttl_seconds
            try:
                hours, minutes, seconds = map(int, invoice.get("expiresIn", "").split(":"))
                provider_ttl = hours * 3600 + minutes * 60 + seconds
            except (TypeError, ValueError):
                pass
            await state.update_data(
                invoice_id=transaction_id,
                payment_type="platega",
                payment_intent_id=intent_id,
                payment_currency=payment_request.currency,
            )
            await edit_screen(
                call,
                localize(
                    "payments.invoice.summary",
                    amount=amount_dec,
                    minutes=max(1, (provider_ttl + 59) // 60),
                    button=localize("btn.check_payment"),
                    currency=payment_request.currency,
                ),
                reply_markup=payment_menu(invoice["redirect"]),
                screen="balance",
            )
            return

        if call.data == "pay_admin":
            payment_link, username = _payment_admin_link(int(amount_dec))
            prepared_message = localize(
                "payments.admin.prefilled",
                amount=int(amount_dec),
                currency=EnvKeys.PAY_CURRENCY,
            )
            await edit_screen(
                call,
                localize(
                    "payments.admin.info",
                    amount=int(amount_dec),
                    currency=EnvKeys.PAY_CURRENCY,
                    username=username,
                    message=esc(prepared_message),
                ),
                reply_markup=admin_payment_keyboard(payment_link, username),
                screen="balance",
            )
            return

        if call.data == "pay_cryptopay":
            if not EnvKeys.CRYPTO_PAY_TOKEN:
                await edit_screen(
                    call,
                    localize("payments.crypto.setup"),
                    reply_markup=back("replenish_balance"),
                    screen="balance",
                )
                return

            intent_id = f"intent:{secrets.token_urlsafe(24)}"
            await create_pending_payment(
                provider="cryptopay",
                external_id=intent_id,
                user_id=call.from_user.id,
                amount=int(amount_dec),
                currency=payment_request.currency,
            )

            try:
                crypto = CryptoPayAPI()
                invoice = await crypto.create_invoice(
                    amount=float(amount_dec),
                    expires_in=ttl_seconds,
                    currency=payment_request.currency,
                    accepted_assets="TON,USDT,BTC,ETH",
                    payload=intent_id,
                )
            except CryptoPayAPIError as e:
                await _fail_payment_intent("cryptopay", intent_id)
                await log_audit("cryptopay_error", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=f"[{e.code}] {e.name}")
                await call.answer(localize("payments.crypto.api_error", error=e.name), show_alert=True)
                return
            except Exception as e:
                await _fail_payment_intent("cryptopay", intent_id)
                await log_audit("cryptopay_invoice_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=str(e))
                await call.answer(localize("payments.crypto.create_fail", error=str(e)), show_alert=True)
                return

            pay_url = invoice.get("mini_app_invoice_url")
            invoice_id = invoice.get("invoice_id")

            if not pay_url or not invoice_id:
                await _fail_payment_intent("cryptopay", intent_id)
                await call.answer(localize("payments.crypto.create_fail", error="empty response"), show_alert=True)
                return
            try:
                bound = await bind_pending_payment("cryptopay", intent_id, str(invoice_id))
            except Exception:
                bound = False
            if not bound:
                await _fail_payment_intent("cryptopay", intent_id)
                await log_audit("cryptopay_invoice_link_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=f"intent={intent_id}")
                await call.answer(localize("payments.crypto.create_fail", error="payment record unavailable"), show_alert=True)
                return

            await state.update_data(
                invoice_id=str(invoice_id),
                payment_type="cryptopay",
                payment_intent_id=intent_id,
                payment_currency=payment_request.currency,
            )

            await edit_screen(
                call,
                localize("payments.invoice.summary",
                         amount=int(amount_dec),
                         minutes=int(ttl_seconds / 60),
                         button=localize("btn.check_payment"),
                         currency=payment_request.currency),
                reply_markup=payment_menu(pay_url),
                screen="balance",
            )

        elif call.data == "pay_xrocket":
            if not EnvKeys.XROCKET_PAY_TOKEN:
                await edit_screen(
                    call,
                    localize("payments.xrocket.setup"),
                    reply_markup=back("replenish_balance"),
                    screen="balance",
                )
                return

            intent_id = f"intent:{secrets.token_urlsafe(24)}"
            await create_pending_payment(
                provider="xrocket",
                external_id=intent_id,
                user_id=call.from_user.id,
                amount=int(amount_dec),
                currency=payment_request.currency,
            )

            try:
                xrocket = XRocketPayAPI()
                asset_amount = await xrocket.fiat_to_asset_amount(
                    amount_dec, payment_request.currency
                )
                invoice = await xrocket.create_invoice(
                    amount=amount_dec,
                    expires_in=ttl_seconds,
                    currency=payment_request.currency,
                    description=f"My Store: +{int(amount_dec)} {payment_request.currency}",
                    client_invoice_id=intent_id,
                    telegram_id=call.from_user.id,
                )
            except XRocketAPIError as e:
                await _fail_payment_intent("xrocket", intent_id)
                await log_audit("xrocket_error", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=f"[{e.status}] {e.detail}")
                await call.answer(localize("payments.crypto.api_error", error=e.detail[:120]), show_alert=True)
                return
            except Exception as e:
                await _fail_payment_intent("xrocket", intent_id)
                await log_audit("xrocket_invoice_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=str(e))
                await call.answer(localize("payments.crypto.create_fail", error=str(e)[:120]), show_alert=True)
                return

            links = invoice.get("links") or {}
            pay_url = links.get("telegramBotLink") or links.get("telegramMiniAppLink") or links.get("webLink")
            invoice_id = invoice.get("id")
            if not pay_url or not invoice_id:
                await _fail_payment_intent("xrocket", intent_id)
                await log_audit("xrocket_invoice_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details="missing links/id")
                await call.answer(localize("payments.crypto.create_fail", error="empty response"), show_alert=True)
                return

            try:
                bound = await bind_pending_payment("xrocket", intent_id, str(invoice_id))
            except Exception:
                bound = False
            if not bound:
                await _fail_payment_intent("xrocket", intent_id)
                await log_audit("xrocket_invoice_link_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=f"intent={intent_id}")
                await call.answer(localize("payments.crypto.create_fail", error="payment record unavailable"), show_alert=True)
                return

            await state.update_data(
                invoice_id=str(invoice_id),
                payment_type="xrocket",
                payment_intent_id=intent_id,
                payment_currency=payment_request.currency,
            )

            await edit_screen(
                call,
                localize("payments.xrocket.invoice",
                         asset_amount=asset_amount,
                         asset=xrocket.asset,
                         fiat_amount=int(amount_dec),
                         minutes=int(ttl_seconds / 60),
                         button=localize("btn.check_payment"),
                         currency=payment_request.currency),
                reply_markup=payment_menu(pay_url),
                screen="balance",
            )
            return

        elif call.data == "pay_manual_crypto":
            usdt_address = str(getattr(EnvKeys, "MANUAL_USDT_BEP20", "") or "").strip()
            if not usdt_address:
                await call.answer(localize("payments.not_configured"), show_alert=True)
                return

            username = _payment_admin_username()
            prepared_message = localize(
                "payments.manual_crypto.prefilled",
                amount=int(amount_dec),
                currency=str(data.get("payment_currency") or EnvKeys.PAY_CURRENCY).upper(),
            )
            payment_link = f"https://t.me/{username}?text={quote(prepared_message)}"
            await edit_screen(
                call,
                localize(
                    "payments.manual_crypto.info",
                    amount=int(amount_dec),
                    currency=EnvKeys.PAY_CURRENCY,
                    usd=await _usd_hint(amount_dec),
                    usdt=usdt_address,
                    username=username,
                ),
                reply_markup=admin_payment_keyboard(payment_link, username),
                screen="balance",
            )
            return

        elif call.data == "pay_fiat":
            if not EnvKeys.TELEGRAM_PROVIDER_TOKEN:
                await call.answer(localize("payments.not_configured"), show_alert=True)
                return

            try:
                await send_fiat_invoice(
                    bot=call.message.bot,
                    chat_id=call.from_user.id,
                    amount=int(amount_dec),
                )
            except Exception as e:
                await log_audit("fiat_invoice_fail", level="ERROR", user_id=call.from_user.id, resource_type="Payment", details=str(e))
                await call.answer(localize("payments.fiat.create_fail", error=str(e)), show_alert=True)
                return
            await state.clear()

        elif call.data == "pay_test":
            if EnvKeys.TEST_PAYMENT_ENABLED != "1" or EnvKeys.DEBUG != "1":
                await call.answer(localize("payments.test.disabled"), show_alert=True)
                return

            external_id = data.get("test_payment_id")
            if not external_id:
                await call.answer(localize("payments.session_expired"), show_alert=True)
                await state.clear()
                return

            success, error_msg = await process_payment_with_referral(
                user_id=call.from_user.id,
                amount=amount_dec,
                provider="test",
                external_id=external_id,
                referral_percent=EnvKeys.REFERRAL_PERCENT,
            )
            if not success:
                key = "payments.already_processed" if error_msg == "already_processed" else "payments.processing_error"
                await call.answer(localize(key), show_alert=True)
                return

            metrics = get_metrics()
            if metrics:
                metrics.track_event(
                    "payment", call.from_user.id,
                    {"amount": amount_dec, "provider": "test"},
                )
            await _notify_referrer_bonus(
                call.bot,
                call.from_user.id,
                amount_dec,
                call.from_user.first_name,
                call.from_user.id,
            )
            await edit_screen(
                call,
                localize(
                    "payments.test.success",
                    amount=amount_dec,
                    currency=EnvKeys.PAY_CURRENCY,
                ),
                reply_markup=back("profile"),
                screen="balance",
            )
            safe_create_task(log_audit(
                "test_balance_replenish",
                user_id=call.from_user.id,
                resource_type="Payment",
                resource_id=external_id,
                details=f"amount={amount_dec} {EnvKeys.PAY_CURRENCY}",
            ))
            await state.clear()

    except Exception as e:
        logger.error(f"Payment processing error: {e}")
        await state.clear()
        await call.answer(localize("errors.something_wrong"), show_alert=True)


@router.callback_query(F.data == "check")
async def checking_payment(call: CallbackQuery, state: FSMContext):
    """
    Check CryptoPay/xRocket invoice status and credit balance if paid.
    """
    user_id = call.from_user.id
    data = await state.get_data()
    payment_type = data.get("payment_type")

    if not payment_type:
        await call.answer(localize("payments.no_active_invoice"), show_alert=True)
        return

    if payment_type == "cryptopay":
        invoice_id = data.get("invoice_id")
        if not invoice_id:
            await call.answer(localize("payments.invoice_not_found"), show_alert=True)
            await state.clear()
            return

        try:
            crypto = CryptoPayAPI()
            info = await crypto.get_invoice(invoice_id)
        except CryptoPayAPIError as e:
            await log_audit("cryptopay_check_error", level="ERROR", user_id=user_id, resource_type="Payment", details=f"[{e.code}] {e.name}")
            await call.answer(localize("payments.crypto.api_error", error=e.name), show_alert=True)
            return
        except Exception as e:
            await log_audit("cryptopay_get_fail", level="ERROR", user_id=user_id, resource_type="Payment", details=str(e))
            await call.answer(localize("payments.crypto.check_fail", error=str(e)), show_alert=True)
            return

        status = info.get("status")
        if status == "paid":
            balance_amount = Decimal(str(info.get("amount", "0"))).quantize(Decimal("0.01"))

            if balance_amount <= 0:
                await call.answer(localize("payments.unable_determine_amount"), show_alert=True)
                return

            # Use transactional payment processing
            success, error_msg = await process_payment_with_referral(
                user_id=user_id,
                amount=balance_amount,
                provider="cryptopay",
                external_id=str(invoice_id),
                referral_percent=EnvKeys.REFERRAL_PERCENT,
                currency=str(data.get("payment_currency") or EnvKeys.PAY_CURRENCY).upper(),
            )

            if not success:
                if error_msg == "already_processed":
                    await call.answer(localize("payments.already_processed"), show_alert=True)
                else:
                    await call.answer(localize("errors.general_error", e=error_msg), show_alert=True)
                return

            metrics = get_metrics()
            if metrics:
                metrics.track_event("payment", user_id, {"amount": balance_amount, "provider": "cryptopay"})

            # Send a notification to the referrer
            await _notify_referrer_bonus(call.bot, user_id, balance_amount, call.from_user.first_name, call.from_user.id)

            await edit_screen(
                call,
                localize("payments.topped_simple",
                         amount=balance_amount,
                         currency=EnvKeys.PAY_CURRENCY),
                reply_markup=back('profile'),
                screen="balance",
            )
            await state.clear()

            safe_create_task(log_audit(
                "balance_replenish",
                user_id=user_id,
                resource_type="Payment",
                details=f"name={caller_name(call)}, amount={balance_amount} {EnvKeys.PAY_CURRENCY}, provider=cryptopay",
            ))

        elif status == "active":
            await call.answer(localize("payments.not_paid_yet"))
        else:
            await call.answer(localize("payments.expired"), show_alert=True)

    elif payment_type == "platega":
        transaction_id = data.get("invoice_id")
        if not transaction_id:
            await call.answer(localize("payments.invoice_not_found"), show_alert=True)
            await state.clear()
            return
        try:
            api = PlategaAPI()
            info = await api.get_transaction(str(transaction_id))
        except PlategaAPIError as exc:
            logger.warning(
                "Platega status lookup failed (http_status=%s)", exc.status_code
            )
            await call.answer(localize("payments.platega.check_fail"), show_alert=True)
            return
        except ValueError:
            logger.warning("Platega status lookup rejected an invalid transaction ID")
            await call.answer(localize("payments.platega.check_fail"), show_alert=True)
            return
        except Exception:
            logger.exception("Unexpected Platega payment status request failure")
            await call.answer(localize("payments.platega.check_fail"), show_alert=True)
            return

        try:
            event = platega_event_from_status(
                info,
                fallback_payload=str(data.get("payment_intent_id") or ""),
            )
            result = await process_platega_event(event)
        except PlategaCallbackError as exc:
            logger.warning("Platega status verification rejected: %s", exc)
            await call.answer(localize("payments.platega.check_fail"), show_alert=True)
            return

        if result.outcome in {"credited", "duplicate"}:
            if result.credited:
                metrics = get_metrics()
                if metrics:
                    metrics.track_event(
                        "payment", user_id,
                        {"amount": result.amount, "provider": "platega"},
                    )
                await _notify_referrer_bonus(
                    call.bot, user_id, result.amount,
                    call.from_user.first_name, call.from_user.id,
                )
            await edit_screen(
                call,
                localize(
                    "payments.topped_simple",
                    amount=result.amount,
                    currency=result.currency,
                ),
                reply_markup=back("profile"),
                screen="balance",
            )
            await state.clear()
        elif result.outcome == "pending":
            await call.answer(localize("payments.not_paid_yet"))
        elif result.outcome == "canceled":
            await call.answer(localize("payments.expired"), show_alert=True)
            await state.clear()
        else:
            await call.answer(localize("payments.platega.check_fail"), show_alert=True)

    elif payment_type == "xrocket":
        invoice_id = data.get("invoice_id")
        if not invoice_id:
            await call.answer(localize("payments.invoice_not_found"), show_alert=True)
            await state.clear()
            return

        try:
            xrocket = XRocketPayAPI()
            info = await xrocket.get_invoice(str(invoice_id))
        except XRocketAPIError as e:
            await log_audit("xrocket_check_error", level="ERROR", user_id=user_id, resource_type="Payment", details=f"[{e.status}] {e.detail}")
            await call.answer(localize("payments.crypto.api_error", error=e.detail[:120]), show_alert=True)
            return
        except Exception as e:
            await log_audit("xrocket_get_fail", level="ERROR", user_id=user_id, resource_type="Payment", details=str(e))
            await call.answer(localize("payments.crypto.check_fail", error=str(e)[:120]), show_alert=True)
            return

        status = info.get("status")
        if status == "paid":
            # The invoice is denominated in crypto; credit the fiat amount
            # the user originally requested (stored in the FSM + pending row).
            try:
                balance_amount = Decimal(str(data.get("amount") or 0)).quantize(Decimal("0.01"))
            except Exception:
                balance_amount = Decimal("0")

            if balance_amount <= 0:
                await call.answer(localize("payments.unable_determine_amount"), show_alert=True)
                return

            success, error_msg = await process_payment_with_referral(
                user_id=user_id,
                amount=balance_amount,
                provider="xrocket",
                external_id=str(invoice_id),
                referral_percent=EnvKeys.REFERRAL_PERCENT,
                currency=str(data.get("payment_currency") or EnvKeys.PAY_CURRENCY).upper(),
            )

            if not success:
                if error_msg == "already_processed":
                    await call.answer(localize("payments.already_processed"), show_alert=True)
                else:
                    await call.answer(localize("errors.general_error", e=error_msg), show_alert=True)
                return

            metrics = get_metrics()
            if metrics:
                metrics.track_event("payment", user_id, {"amount": balance_amount, "provider": "xrocket"})

            await _notify_referrer_bonus(call.bot, user_id, balance_amount, call.from_user.first_name, call.from_user.id)

            await edit_screen(
                call,
                localize("payments.topped_simple",
                         amount=balance_amount,
                         currency=EnvKeys.PAY_CURRENCY),
                reply_markup=back('profile'),
                screen="balance",
            )
            await state.clear()

            safe_create_task(log_audit(
                "balance_replenish",
                user_id=user_id,
                resource_type="Payment",
                details=f"name={caller_name(call)}, amount={balance_amount} {EnvKeys.PAY_CURRENCY}, provider=xrocket",
            ))

        elif status in ("active", "partially_paid"):
            await call.answer(localize("payments.not_paid_yet"))
        else:
            await call.answer(localize("payments.expired"), show_alert=True)


@router.pre_checkout_query()
async def pre_checkout_handler(query: PreCheckoutQuery):
    """Validate the payment before Telegram processes it."""
    if str(getattr(query, "currency", "") or "").upper() == "XTR":
        await query.answer(
            ok=False,
            error_message=localize("payments.method_unavailable"),
        )
        return

    try:
        payload = json.loads(query.invoice_payload or "{}")
    except Exception:
        await query.answer(ok=False, error_message="Invalid payload")
        return

    amount = payload_amount(payload)
    if amount <= 0:
        await query.answer(ok=False, error_message="Invalid amount")
        return

    if amount < int(EnvKeys.MIN_AMOUNT):
        await query.answer(ok=False, error_message="Amount below minimum")
        return

    if amount > int(EnvKeys.MAX_AMOUNT):
        await query.answer(ok=False, error_message="Amount exceeds maximum")
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    """
    Handle successful payment:
    - XTR (Stars): total_amount is ⭐. take CURRENCY from payload (amount) or convert ⭐ → CURRENCY.
    - Fiat: total_amount is minor units; divide by 100 (or 1 for JPY/KRW).
    """
    sp: SuccessfulPayment = message.successful_payment
    user_id = message.from_user.id

    payload = {}
    try:
        if sp.invoice_payload:
            payload = json.loads(sp.invoice_payload)
    except Exception:
        payload = {}

    amount = payload_amount(payload)

    # Cross-check what Telegram actually charged against our own invoice data.
    # A mismatch must never over-credit: fall back to the conservative figure.
    if sp.currency == "XTR":
        paid_stars = int(sp.total_amount or 0)
        billed_stars = payload.get("stars")
        try:
            billed_stars = int(billed_stars) if billed_stars is not None else None
        except (TypeError, ValueError):
            billed_stars = None
        if billed_stars is not None and paid_stars != billed_stars:
            safe_create_task(log_audit(
                "stars_amount_mismatch", level="WARNING", user_id=user_id,
                resource_type="Payment",
                details=f"billed={billed_stars} paid={paid_stars}",
            ))
            amount = 0  # redo below via the reverse conversion
    else:
        try:
            multiplier = _minor_units_for(sp.currency.upper())
            paid_major = int(Decimal(sp.total_amount) / Decimal(multiplier))
        except (InvalidOperation, TypeError, ValueError, ZeroDivisionError):
            paid_major = 0
        if amount > 0 and paid_major > 0 and paid_major != amount:
            safe_create_task(log_audit(
                "fiat_amount_mismatch", level="WARNING", user_id=user_id,
                resource_type="Payment",
                details=f"billed={amount} paid={paid_major} {sp.currency}",
            ))
            amount = min(amount, paid_major)

    if amount <= 0:
        if sp.currency == "XTR":
            # Stars, no usable payload: reverse the conversion as a last resort.
            amount = int(
                (Decimal(int(sp.total_amount)) / Decimal(str(EnvKeys.STARS_PER_VALUE)))
                .to_integral_value(rounding=ROUND_HALF_UP)
            )
        else:
            # Fiat: total_amount is exact in minor units, so this is lossless.
            currency = sp.currency.upper()
            multiplier = _minor_units_for(currency)
            amount = int(Decimal(sp.total_amount) / Decimal(multiplier))

    if amount <= 0:
        await answer_screen(
            message,
            localize("payments.unable_determine_amount"),
            reply_markup=close(),
            screen="balance",
        )
        return

    # Idempotence
    provider = "telegram" if sp.currency != "XTR" else "stars"
    external_id = sp.telegram_payment_charge_id or sp.provider_payment_charge_id
    if not external_id:
        digest = hashlib.sha256(
            f"{provider}|{user_id}|{sp.currency}|{sp.total_amount}|{sp.invoice_payload or ''}".encode()
        ).hexdigest()
        external_id = f"{provider}:fallback:{digest[:32]}"
        logger.warning(
            "successful_payment without a charge id for user %s (%s %s); "
            "falling back to a derived idempotency key %s",
            user_id, sp.total_amount, sp.currency, external_id,
        )

    success, error_msg = await process_payment_with_referral(
        user_id=user_id,
        amount=Decimal(amount),
        provider=provider,
        external_id=external_id,
        referral_percent=EnvKeys.REFERRAL_PERCENT,
        currency=sp.currency,
    )

    if not success:
        if error_msg == "already_processed":
            await answer_screen(
                message,
                localize("payments.already_processed"),
                reply_markup=close(),
                screen="balance",
            )
        else:
            await answer_screen(
                message,
                localize("payments.processing_error"),
                reply_markup=close(),
                screen="balance",
            )
        return

    # Sending notification to referrer
    await _notify_referrer_bonus(message.bot, user_id, amount, message.from_user.first_name, message.from_user.id)

    metrics = get_metrics()
    if metrics:
        metrics.track_event("payment", user_id, {"amount": amount, "provider": provider})

    suffix = localize("payments.success_suffix.stars") if sp.currency == "XTR" else localize(
        "payments.success_suffix.tg")
    await answer_screen(
        message,
        localize('payments.topped_with_suffix', amount=amount, suffix=suffix, currency=EnvKeys.PAY_CURRENCY),
        reply_markup=back('profile'),
        screen="balance",
    )

    safe_create_task(log_audit(
        "balance_replenish",
        user_id=user_id,
        resource_type="Payment",
        details=f"name={caller_name(message)}, amount={amount} {EnvKeys.PAY_CURRENCY}, provider={suffix}",
    ))


@router.callback_query(F.data == "buy_item")
async def buy_item_callback_handler(call: CallbackQuery, state: FSMContext):
    """Open the balance-only purchase confirmation screen.

    The old implementation charged one unit immediately and opened payment
    providers on an insufficient-balance error.  A purchase is now a two-step
    action: choose quantity, then explicitly confirm payment from the balance.
    """
    await _start_purchase(call, state, quantity=1)


async def _start_purchase(call: CallbackQuery, state: FSMContext, quantity: int):
    """Validate the selected product and show the purchase choice."""
    try:
        data = await state.get_data()
        raw_item_name = data.get('csrf_item')

        if not raw_item_name:
            await call.answer(localize("middleware.security.invalid_csrf"), show_alert=True)
            return

        purchase_request = ItemPurchaseRequest(
            item_name=raw_item_name,
            user_id=call.from_user.id
        )

        # Additional check for SQL injection
        if not is_safe_item_name(purchase_request.item_name):
            await call.answer(
                localize("errors.invalid_item_name"),
                show_alert=True
            )
            await log_audit("suspicious_item_name", level="WARNING", user_id=call.from_user.id, resource_type="Item", details=raw_item_name)
            return

        # User_id validation
        try:
            user_id = validate_telegram_id(call.from_user.id)
        except ValueError:
            await call.answer(localize("errors.invalid_user"), show_alert=True)
            return

        if await ensure_user(user_id) is None:
            await call.answer(localize("errors.something_wrong"), show_alert=True)
            return

        await call.answer()
        await state.update_data(purchase_quantity=quantity)
        await _render_purchase_choice(call, state, quantity=quantity)

    except Exception as e:
        logger.error(f"Critical error in purchase handler: {e}")
        await call.answer(
            localize("errors.something_wrong"),
            show_alert=True
        )


@router.callback_query(ShopStates.confirming_purchase, F.data == "buy_qty:input")
async def purchase_quantity_input_handler(call: CallbackQuery, state: FSMContext):
    """Ask the buyer to enter an exact quantity instead of using steppers."""
    data = await state.get_data()
    try:
        minimum = int(data["purchase_min_quantity"])
        maximum = int(data["purchase_max_quantity"])
    except (KeyError, TypeError, ValueError):
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return
    if minimum < 1 or maximum < minimum:
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return

    await state.set_state(ShopStates.entering_purchase_quantity)
    await call.answer()
    await edit_screen(
        call,
        localize(
            "shop.purchase.quantity_prompt",
            min_quantity=minimum,
            max_quantity=maximum,
        ),
        reply_markup=simple_buttons([
            (localize("btn.back"), "buy_qty:cancel"),
            (localize("btn.main_menu"), "back_to_menu"),
        ]),
        screen="product",
        image_ref=data.get("item_image_ref"),
    )


@router.message(ShopStates.entering_purchase_quantity, F.text & ~F.text.startswith("/"))
async def purchase_quantity_text_handler(message: Message, state: FSMContext):
    """Validate the entered integer and show the updated quote before purchase."""
    data = await state.get_data()
    try:
        minimum = int(data["purchase_min_quantity"])
        maximum = int(data["purchase_max_quantity"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await answer_screen(
            message,
            localize("errors.invalid_data"),
            reply_markup=simple_buttons([
                (localize("btn.main_menu"), "back_to_menu"),
            ]),
            screen="product",
        )
        return

    raw_quantity = (message.text or "").strip()
    quantity = int(raw_quantity) if raw_quantity.isdecimal() else None
    if quantity is None or not minimum <= quantity <= maximum:
        await answer_screen(
            message,
            localize(
                "shop.purchase.quantity_invalid",
                min_quantity=minimum,
                max_quantity=maximum,
            ),
            reply_markup=simple_buttons([
                (localize("btn.back"), "buy_qty:cancel"),
                (localize("btn.main_menu"), "back_to_menu"),
            ]),
            screen="product",
            image_ref=data.get("item_image_ref"),
        )
        return

    await _render_purchase_choice(
        message,
        state,
        quantity=quantity,
        message_input=True,
    )


@router.callback_query(ShopStates.entering_purchase_quantity, F.data == "buy_qty:cancel")
async def purchase_quantity_cancel_handler(call: CallbackQuery, state: FSMContext):
    """Restore the purchase summary without changing its selected quantity."""
    data = await state.get_data()
    quantity = data.get("purchase_quantity")
    await state.set_state(ShopStates.confirming_purchase)
    await call.answer()
    await _render_purchase_choice(call, state, quantity=quantity)


@router.callback_query(ShopStates.confirming_purchase, F.data.startswith("buy_qty:"))
async def purchase_quantity_handler(call: CallbackQuery, state: FSMContext):
    """Handle legacy quantity callbacks from screens sent before this update."""
    raw_delta = call.data.rsplit(":", 1)[-1]
    try:
        delta = int(raw_delta) if raw_delta != "max" else None
    except (TypeError, ValueError):
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return
    if delta == 0:
        await call.answer()
        return
    data = await state.get_data()
    current = int(data.get("purchase_quantity") or 1)
    minimum = int(data.get("purchase_min_quantity") or 1)
    maximum = int(data.get("purchase_max_quantity") or 1)
    await call.answer()
    await _render_purchase_choice(
        call,
        state,
        quantity=(
            maximum if delta is None
            else max(minimum, min(current + delta, maximum))
        ),
    )


@router.callback_query(ShopStates.confirming_purchase, F.data == "buy_topup")
async def purchase_topup_handler(call: CallbackQuery, state: FSMContext):
    """Move from the purchase summary to the separate top-up menu."""
    data = await state.get_data()
    item_name = data.get("csrf_item")
    quantity = int(data.get("purchase_quantity") or 1)
    if not item_name:
        await call.answer(localize("middleware.security.invalid_csrf"), show_alert=True)
        return

    quote = await _item_purchase_quote(call.from_user.id, item_name, quantity)
    if quote is None:
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return
    if quote["can_afford"]:
        await call.answer(localize("shop.purchase.processing"))
        await _render_purchase_choice(call, state, quantity=quantity)
        return

    topup_amount = await _topup_amount_for_item(
        call.from_user.id,
        item_name,
        quantity=quantity,
    )
    if topup_amount is None:
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return

    await state.update_data(
        amount=topup_amount,
        test_payment_id=secrets.token_urlsafe(24),
    )
    await state.set_state(BalanceStates.waiting_payment)
    await call.answer()
    await edit_screen(
        call,
        localize(
            "shop.insufficient_funds.topup",
            amount=topup_amount,
            currency=EnvKeys.PAY_CURRENCY,
        ),
        reply_markup=get_payment_choice(),
        screen="balance",
    )


@router.callback_query(ShopStates.confirming_purchase, F.data == "buy_confirm")
async def purchase_confirm_handler(call: CallbackQuery, state: FSMContext):
    """Atomically charge the selected quantity from the user's balance."""
    data = await state.get_data()
    raw_item_name = data.get("csrf_item")
    quantity = int(data.get("purchase_quantity") or 1)
    if not raw_item_name:
        await call.answer(localize("middleware.security.invalid_csrf"), show_alert=True)
        return

    try:
        request = ItemPurchaseRequest(
            item_name=raw_item_name,
            user_id=call.from_user.id,
            quantity=quantity,
        )
        user_id = validate_telegram_id(call.from_user.id)
    except (ValueError, TypeError):
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return
    if not is_safe_item_name(request.item_name):
        await call.answer(localize("errors.invalid_item_name"), show_alert=True)
        return
    if await ensure_user(user_id) is None:
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return

    quote = await _item_purchase_quote(user_id, request.item_name, quantity)
    if quote is None:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return
    if not quote["can_afford"]:
        await call.answer(localize("shop.insufficient_funds"), show_alert=True)
        await _render_purchase_choice(call, state, quantity=quantity)
        return

    await call.answer(localize("shop.purchase.processing"))
    try:
        expected_total = Decimal(str(data.get("purchase_total") or 0))
    except (InvalidOperation, TypeError, ValueError):
        expected_total = None
    success, message, purchase_data = await buy_item_transaction(
        user_id,
        request.item_name,
        quantity=request.quantity,
        expected_total=expected_total,
    )
    if not success and message == "price_changed":
        await _render_purchase_choice(call, state, quantity=quantity)
        return
    if not success:
        if message == "insufficient_funds":
            await _render_purchase_choice(call, state, quantity=quantity)
            return
        error_messages = {
            "user_not_found": "shop.purchase.fail.user_not_found",
            "item_not_found": "shop.item.not_found",
            "invalid_quantity": "errors.invalid_data",
            "out_of_stock": "shop.out_of_stock",
            "vpn_unconfigured": "shop.vpn_unavailable",
            "invalid_price": "shop.purchase.fail.general",
        }
        await edit_screen(
            call,
            localize(error_messages.get(message, "shop.purchase.fail.general"), message=message),
            reply_markup=simple_buttons([
                (localize("btn.back"), "back_to_item"),
                (localize("btn.main_menu"), "back_to_menu"),
            ]),
            screen="product",
            image_ref=data.get("item_image_ref"),
        )
        return

    metrics = get_metrics()
    if metrics:
        metrics.track_event("purchase", call.from_user.id, {
            "item": request.item_name,
            "price": purchase_data["total_price"],
            "quantity": quantity,
        })
        metrics.track_conversion("purchase_funnel", "purchase", call.from_user.id)

    items = purchase_data.get("items") or [purchase_data]
    safe_value = esc(_purchase_delivery_text(items))
    from bot.keyboards.inline import simple_buttons
    buttons = [
        (f"📦 {purchase_data['item_name']}", f"bought-item:{purchase_data['bought_id']}:back_to_item"),
        (localize("btn.back"), "back_to_item"),
        (localize("btn.main_menu"), "back_to_menu"),
    ]
    await edit_screen(
        call,
        localize(
            "shop.purchase.receipt",
            item_name=esc(purchase_data["item_name"]),
            price=purchase_data["price"],
            total=purchase_data["total_price"],
            quantity=quantity,
            unique_id=purchase_data["unique_id"],
            datetime=format_dt(purchase_data["bought_datetime"]),
            value=safe_value,
            currency=EnvKeys.PAY_CURRENCY,
        ),
        reply_markup=simple_buttons(buttons),
        screen="product",
        image_ref=data.get("item_image_ref"),
    )
    safe_create_task(log_audit(
        "purchase",
        user_id=user_id,
        resource_type="Item",
        resource_id=request.item_name[:100],
        details=(
            f"name={caller_name(call)[:50]}, quantity={quantity}, "
            f"price={purchase_data['total_price']} {EnvKeys.PAY_CURRENCY}, "
            f"unique_id={purchase_data['unique_id']}"
        ),
    ))

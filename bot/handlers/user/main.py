from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.enums.chat_type import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext

import asyncio
import datetime
import re
import time
from html import escape as _esc

from bot.database.methods import (
    select_max_role_id, create_user, check_role_cached, check_user,
    get_default_user_role_id,
    select_user_spent_total, select_user_items,
    check_user_referrals, set_user_locale, set_role,
    set_community_prompt_seen, set_user_currency, set_user_onboarded,
)
from bot.database.methods.read import get_cart_count, get_item_name_by_id, invalidate_user_cache
from bot.database.methods.lazy_queries import query_user_operations_history
from bot.handlers.other import _parse_channel_username
from bot.keyboards import main_menu, back, profile_keyboard, language_keyboard, legal_keyboard
from bot.keyboards.inline import (
    onboarding_language_keyboard,
    onboarding_currency_keyboard,
    language_and_currency_keyboard,
)
from bot.keyboards.styles import colorize_markup
from bot.misc import EnvKeys
from bot.misc.currency import (
    get_user_currency,
    set_user_currency_context,
    reset_user_currency_context,
    rub_to_usd,
)
from bot.misc.timezone import format_moscow_date
from bot.misc.screens import answer_screen, edit_screen
from bot.misc.metrics import get_metrics
from bot.i18n import format_dt, get_locale, localize, reset_locale, set_locale
from bot.logger_mesh import logger
from bot.middleware.subscription import (
    build_subscription_gate,
    clear_subscription_cache,
)

router = Router()

# Identical /start texts arriving within this window are double taps (or
# Telegram retries) — the first handler already renders the menu, so the
# second must not send it again. Check-and-set is atomic on the event loop
# (no await between get and set).
_START_DEBOUNCE_SECONDS = 2.0
_LAST_START: dict[int, tuple[str, float]] = {}


def _is_duplicate_start(user_id: int, text: str) -> bool:
    """True if the identical /start was already accepted within the window."""
    now = time.monotonic()
    if len(_LAST_START) > 10000:
        cutoff = now - _START_DEBOUNCE_SECONDS
        for uid, (_, at) in list(_LAST_START.items()):
            if at < cutoff:
                del _LAST_START[uid]
    previous = _LAST_START.get(user_id)
    _LAST_START[user_id] = (text or "", now)
    if previous is None:
        return False
    previous_text, previous_at = previous
    return previous_text == (text or "") and (now - previous_at) < _START_DEBOUNCE_SECONDS


def _env_text(name: str, default: str = "") -> str:
    value = getattr(EnvKeys, name, default)
    if not isinstance(value, str):
        return default
    if name == "FAQ":
        # systemd EnvironmentFile keeps ``\\n`` as two literal characters,
        # while Telegram needs real line breaks. Also accept FAQ text pasted
        # from Markdown-oriented editors and render its bold markers as HTML.
        value = value.replace(r"\&#x6E;", "\n").replace(r"\n", "\n")
        value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value, flags=re.DOTALL)
    return value


def _main_text(user) -> str:
    return localize(
        "menu.title",
        id=user.id,
        name=_esc(user.first_name or str(user.id)),
        shop_name=_esc(_env_text("SHOP_NAME", "My Store")),
    )


def _main_markup(role: int, channel_username: str | None):
    return main_menu(
        role=role,
        channel=channel_username,
        helper=_env_text("HELPER_ID"),
        support_url=_env_text("SUPPORT_URL"),
        support_username=_env_text("SUPPORT_USERNAME"),
    )


@router.message(Command("chatid"))
async def private_chat_id_to_owner(message: Message):
    """Return the current chat ID privately to the configured bot owner."""
    if message.from_user is None or message.from_user.id != EnvKeys.OWNER_ID:
        return

    response = f"ID этого чата: <code>{message.chat.id}</code>"
    if message.chat.type == ChatType.PRIVATE:
        await message.answer(response, parse_mode="HTML")
        return

    try:
        await message.bot.send_message(
            chat_id=EnvKeys.OWNER_ID,
            text=response,
            parse_mode="HTML",
        )
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer(
            "Не смог отправить ID в личку. Откройте бота в личных сообщениях, "
            "нажмите /start и повторите команду."
        )
        return
    await message.answer("ID чата отправил вам в личные сообщения.")


async def restart_from_stale_session(bot, user, state: FSMContext | None = None) -> None:
    """Render a fresh start screen after an old Telegram callback is rejected.

    The old transaction is never retried. This only restores navigation in a
    new bot message, so a restart does not leave the user on a dead keyboard.
    """
    if state is not None:
        await state.clear()

    user_row = await ensure_user(user.id)
    role = await check_role_cached(user.id) or 0
    channel_username = _parse_channel_username()

    gate = await build_subscription_gate(bot, user.id, user_row)
    if gate:
        await bot.send_message(
            chat_id=user.id,
            text=gate[0],
            reply_markup=gate[1],
            parse_mode="HTML",
        )
        return

    await bot.send_message(
        chat_id=user.id,
        text=_main_text(user),
        reply_markup=_main_markup(role, channel_username),
        parse_mode="HTML",
    )


def _product_id_from_start_payload(payload: str) -> int | None:
    """Parse the product-card deep link without affecting numeric referrals."""
    prefix = "item_"
    if not payload.startswith(prefix):
        return None
    raw_id = payload[len(prefix):]
    if not raw_id.isdigit():
        return None
    item_id = int(raw_id)
    return item_id if item_id > 0 else None


async def ensure_user(user_id: int) -> dict | None:
    """Return the user's row, registering them first if it is missing.

    A stale keyboard (or a wiped database) can hand a callback from someone
    with no row at all. Use the direct database read here: a cached positive
    result must never make a purchase proceed for a row that has since gone.

    Never raises for expected DB problems (missing roles, races, FK): it logs
    and returns None so handlers answer with ``errors.something_wrong``
    instead of leaving the callback hanging.
    """
    try:
        user = await check_user(user_id)
        if user:
            # Repair an account left by an old import or an interrupted migration.
            # Such a row exists but has no role, so it is treated inconsistently by
            # the menu and payment flows.
            if user.get("role_id") is None:
                role = (
                    await select_max_role_id()
                    if user_id == EnvKeys.OWNER_ID
                    else await get_default_user_role_id()
                )
                if role is not None:
                    try:
                        await set_role(user_id, role)
                    except Exception:
                        logger.exception("ensure_user: could not repair role for %s", user_id)
                        return await check_user(user_id)
                    await invalidate_user_cache(user_id)
                    from bot.middleware.security import invalidate_auth_caches
                    invalidate_auth_caches(user_id)
                else:
                    logger.error("ensure_user: no role available to repair user %s", user_id)
                return await check_user(user_id)
            return user

        role = (
            await select_max_role_id()
            if user_id == EnvKeys.OWNER_ID
            else await get_default_user_role_id()
        )
        if role is None:
            logger.error("ensure_user: no role available to register user %s", user_id)
            return None
        await create_user(
            telegram_id=user_id,
            registration_date=datetime.datetime.now(datetime.timezone.utc),
            referral_id=None,
            role=role,
        )
        await invalidate_user_cache(user_id)
        from bot.middleware.security import invalidate_auth_caches
        invalidate_auth_caches(user_id)
        return await check_user(user_id)
    except Exception:
        logger.exception("ensure_user failed for %s", user_id)
        return None


@router.message(F.text.startswith('/start'))
async def start(message: Message, state: FSMContext):
    """
    Handle /start:
    - Ensure user exists (register if new)
    - (Optional) Check channel subscription
    - Show the main menu
    """
    if message.chat.type != ChatType.PRIVATE:
        return

    user_id = message.from_user.id
    if _is_duplicate_start(user_id, message.text or ""):
        return
    await state.clear()
    parts = message.text.split(maxsplit=1)
    payload = parts[1].strip() if len(parts) > 1 else ""
    linked_item_id = _product_id_from_start_payload(payload)

    existing_user = await check_user(user_id)
    if existing_user is None:
        owner_max_role = await select_max_role_id()
        user_role = (
            owner_max_role
            if user_id == EnvKeys.OWNER_ID
            else await get_default_user_role_id()
        )

        referral_id = None
        source_campaign_id = None
        if linked_item_id is None and payload:
            # int() comparison: "000123" must not pass as someone else's id,
            # let alone your own (self-referral stat inflation).
            if payload.isdigit() and int(payload) != user_id:
                candidate = int(payload)
                if await check_user(candidate) is not None:
                    referral_id = candidate
            else:
                try:
                    from bot.database.methods.traffic import get_traffic_campaign_by_tag, record_traffic_click
                    campaign = await get_traffic_campaign_by_tag(payload)
                    if campaign:
                        source_campaign_id = campaign["id"]
                        await record_traffic_click(campaign["id"], user_id, is_new_user=True)
                except Exception:
                    logger.debug("Could not record traffic click for %s", payload, exc_info=True)

        # registration_date is DateTime
        if user_role is not None:
            try:
                await create_user(
                    telegram_id=int(user_id),
                    registration_date=datetime.datetime.now(datetime.timezone.utc),
                    referral_id=referral_id,
                    role=user_role,
                    source_campaign_id=source_campaign_id,
                )
            except Exception:
                logger.exception("start: could not register user %s", user_id)
        else:
            logger.error("start: no role available to register user %s", user_id)

        await invalidate_user_cache(user_id)
        from bot.middleware.security import invalidate_auth_caches
        invalidate_auth_caches(user_id)

        metrics = get_metrics()
        if metrics:
            metrics.track_event("registration", user_id)
    else:
        if linked_item_id is None and payload and not payload.isdigit():
            try:
                from bot.database.methods.traffic import get_traffic_campaign_by_tag, record_traffic_click
                campaign = await get_traffic_campaign_by_tag(payload)
                if campaign:
                    await record_traffic_click(campaign["id"], user_id, is_new_user=False)
            except Exception:
                logger.debug("Could not record returning traffic click for %s", payload, exc_info=True)
        await ensure_user(user_id)

    user_row = await check_user(user_id)

    # Check if this user needs initial onboarding (first start: language & currency selection)
    is_onboarded = bool(user_row.get("is_onboarded", True)) if user_row else True
    if existing_user is None or not is_onboarded:
        if linked_item_id is not None:
            await state.update_data(onboarding_item_id=linked_item_id)
        await answer_screen(
            message,
            localize("onboarding.lang.title"),
            reply_markup=onboarding_language_keyboard(),
            screen="language",
        )
        return

    if user_row:
        try:
            from bot.database.methods.bot_activity import record_bot_activity
            from bot.middleware.activity import moscow_today

            await record_bot_activity(
                user_id, activity_date=moscow_today(), start_click=True
            )
        except Exception:
            logger.exception("start: could not record /start click for %s", user_id)

    # Re-read after a potential registration/repair so the menu reflects the
    # current role instead of a stale callback-session value.
    role_data = await check_role_cached(user_id)

    channel_username = _parse_channel_username()

    gate = await build_subscription_gate(message.bot, user_id, user_row)
    if gate:
        await answer_screen(
            message,
            gate[0],
            reply_markup=gate[1],
            screen="main-menu",
        )
        return

    if user_row:
        try:
            from bot.database.methods.bot_activity import record_bot_activity
            from bot.middleware.activity import moscow_today

            await record_bot_activity(
                user_id, activity_date=moscow_today(), interaction=True
            )
        except Exception:
            logger.exception("start: could not record permitted interaction for %s", user_id)

    if linked_item_id is not None:
        item_name = await get_item_name_by_id(linked_item_id)
        if item_name:
            # Import locally to keep the core menu module independent of the
            # storefront renderer during application startup.
            from bot.handlers.user.shop_and_goods import _open_item

            await _open_item(
                message,
                state,
                item_name,
                back_data="back_to_menu",
            )
            return

    markup = _main_markup(role_data, channel_username)
    await answer_screen(
        message,
        _main_text(message.from_user),
        reply_markup=markup,
        screen="main-menu",
    )
    await state.clear()


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback_handler(call: CallbackQuery, state: FSMContext):
    """
    Return user to the main menu.
    """
    user_id = call.from_user.id
    await ensure_user(user_id)

    role = await check_role_cached(user_id) or 0

    channel_username = _parse_channel_username()

    markup = _main_markup(role, channel_username)
    await edit_screen(
        call,
        _main_text(call.from_user),
        reply_markup=markup,
        screen="main-menu",
    )
    await state.clear()


@router.callback_query(F.data == "rules")
async def rules_callback_handler(call: CallbackQuery, state: FSMContext):
    """
    Open the agreements menu (user agreement + privacy policy links).
    """
    await edit_screen(
        call,
        localize("legal.title"),
        reply_markup=legal_keyboard("back_to_menu"),
        screen="legal",
    )
    await state.clear()


@router.callback_query(F.data == "agreement")
async def agreement_callback_handler(call: CallbackQuery, state: FSMContext):
    await edit_screen(
        call,
        localize("legal.title"),
        reply_markup=legal_keyboard("profile"),
        screen="legal",
    )
    await state.clear()


@router.callback_query(F.data.startswith("legal-menu:"))
async def legal_menu_callback_handler(call: CallbackQuery, state: FSMContext):
    back_callback = call.data.split(":", 1)[1]
    await edit_screen(
        call,
        localize("legal.title"),
        reply_markup=legal_keyboard(back_callback),
        screen="legal",
    )
    await state.clear()


@router.callback_query(F.data == "faq")
async def faq_callback_handler(call: CallbackQuery, state: FSMContext):
    faq = _env_text("FAQ")
    if faq:
        await edit_screen(
            call,
            faq,
            reply_markup=back("back_to_menu"),
            screen="legal",
        )
    else:
        await call.answer(localize("faq.not_set"))
    await state.clear()


@router.callback_query(F.data == "support")
async def support_callback_handler(call: CallbackQuery):
    await call.answer(localize("support.not_set"), show_alert=True)


@router.callback_query(F.data.startswith("onboarding:lang:"))
async def onboarding_lang_callback_handler(call: CallbackQuery, state: FSMContext):
    locale = call.data.split(":", 2)[2].lower()
    user_id = call.from_user.id
    await ensure_user(user_id)
    await set_user_locale(user_id, locale)
    token = set_locale(locale)
    try:
        await edit_screen(
            call,
            localize("onboarding.curr.title"),
            reply_markup=onboarding_currency_keyboard(),
            screen="language",
        )
        await call.answer()
    finally:
        reset_locale(token)


@router.callback_query(F.data.startswith("onboarding:curr:"))
async def onboarding_curr_callback_handler(call: CallbackQuery, state: FSMContext):
    currency = call.data.split(":", 2)[2].upper()
    user_id = call.from_user.id
    await ensure_user(user_id)
    await set_user_currency(user_id, currency)
    await set_user_onboarded(user_id, True)

    token_curr = set_user_currency_context(currency)
    try:
        data = await state.get_data()
        linked_item_id = data.get("onboarding_item_id")
        user_row = await check_user(user_id)
        channel_username = _parse_channel_username()
        gate = await build_subscription_gate(call.bot, user_id, user_row)
        if gate:
            await edit_screen(
                call,
                gate[0],
                reply_markup=gate[1],
                screen="main-menu",
            )
            return

        if linked_item_id is not None:
            item_name = await get_item_name_by_id(linked_item_id)
            if item_name:
                from bot.handlers.user.shop_and_goods import _open_item
                await _open_item(
                    call.message,
                    state,
                    item_name,
                    back_data="back_to_menu",
                )
                return

        role = await check_role_cached(user_id) or 0
        markup = _main_markup(role, channel_username)
        await edit_screen(
            call,
            _main_text(call.from_user),
            reply_markup=markup,
            screen="main-menu",
        )
        await call.answer(localize("onboarding.done"))
    finally:
        reset_user_currency_context(token_curr)
    await state.clear()


@router.callback_query(F.data == "language")
async def language_callback_handler(call: CallbackQuery):
    user_row = await check_user(call.from_user.id)
    cur_lang = (user_row.get("locale") if user_row else None) or get_locale()
    cur_curr = (user_row.get("currency") if user_row else None) or get_user_currency()

    lang_names = {"ru": "Русский 🇷🇺", "en": "English 🇬🇧", "vi": "Tiếng Việt 🇻🇳"}
    curr_names = {"RUB": "RUB (₽)", "USD": "USD ($)"}

    text = localize(
        "settings.lang_curr.title",
        lang_name=lang_names.get(cur_lang, cur_lang),
        curr_name=curr_names.get(cur_curr, cur_curr),
    )
    await edit_screen(
        call,
        text,
        reply_markup=language_and_currency_keyboard(cur_lang, cur_curr),
        screen="language",
    )


@router.callback_query(F.data.startswith("set-language:"))
async def set_language_callback_handler(call: CallbackQuery, state: FSMContext):
    locale = call.data.split(":", 1)[1].lower()
    await ensure_user(call.from_user.id)
    if not await set_user_locale(call.from_user.id, locale):
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return

    token = set_locale(locale)
    try:
        user_row = await check_user(call.from_user.id)
        cur_curr = (user_row.get("currency") if user_row else None) or get_user_currency()
        lang_names = {"ru": "Русский 🇷🇺", "en": "English 🇬🇧", "vi": "Tiếng Việt 🇻🇳"}
        curr_names = {"RUB": "RUB (₽)", "USD": "USD ($)"}
        text = localize(
            "settings.lang_curr.title",
            lang_name=lang_names.get(locale, locale),
            curr_name=curr_names.get(cur_curr, cur_curr),
        )
        await edit_screen(
            call,
            text,
            reply_markup=language_and_currency_keyboard(locale, cur_curr),
            screen="language",
        )
        await call.answer(localize("language.changed"))
    finally:
        reset_locale(token)


@router.callback_query(F.data.startswith("set-currency:"))
async def set_currency_callback_handler(call: CallbackQuery, state: FSMContext):
    currency = call.data.split(":", 1)[1].upper()
    await ensure_user(call.from_user.id)
    if not await set_user_currency(call.from_user.id, currency):
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return

    token = set_user_currency_context(currency)
    try:
        user_row = await check_user(call.from_user.id)
        cur_lang = (user_row.get("locale") if user_row else None) or get_locale()
        lang_names = {"ru": "Русский 🇷🇺", "en": "English 🇬🇧", "vi": "Tiếng Việt 🇻🇳"}
        curr_names = {"RUB": "RUB (₽)", "USD": "USD ($)"}
        text = localize(
            "settings.lang_curr.title",
            lang_name=lang_names.get(cur_lang, cur_lang),
            curr_name=curr_names.get(currency, currency),
        )
        await edit_screen(
            call,
            text,
            reply_markup=language_and_currency_keyboard(cur_lang, currency),
            screen="language",
        )
        await call.answer(localize("currency.changed", currency=currency))
    finally:
        reset_user_currency_context(token)


@router.callback_query(F.data == "profile")
async def profile_callback_handler(call: CallbackQuery, state: FSMContext):
    """
    Send profile info (balance, purchases count, id, etc.).
    """
    user_id = call.from_user.id
    tg_user = call.from_user
    user_info = await ensure_user(user_id)
    if not user_info:
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return

    balance = user_info.get('balance')
    spent_total, items, cart_count, referrals_count = await asyncio.gather(
        select_user_spent_total(user_id),
        select_user_items(user_id),
        get_cart_count(user_id),
        check_user_referrals(user_id),
    )
    referral = EnvKeys.REFERRAL_PERCENT

    curr = get_user_currency()
    if curr == "USD":
        balance_disp = f"${rub_to_usd(balance):.2f}"
        spent_disp = f"${rub_to_usd(spent_total):.2f}"
        curr_disp = "USD"
    else:
        balance_disp = str(balance)
        spent_disp = str(spent_total)
        curr_disp = EnvKeys.PAY_CURRENCY

    markup = profile_keyboard(referral, items, cart_count=cart_count)
    text = (
        f"{localize('profile.caption')}\n"
        f"{localize('profile.id', id=user_id)}\n"
        f"{localize('profile.login', login='@' + _esc(tg_user.username) if tg_user.username else '—')}\n\n"
        f"{localize('profile.balance', amount=balance_disp, currency=curr_disp)}\n"
        f"{localize('profile.spent', amount=spent_disp, currency=curr_disp)}\n"
        f"{localize('profile.purchased_count', count=items)}\n\n"
        f"{localize('profile.referrals_count', count=referrals_count)}\n"
        f"{localize('profile.registration_date', dt=format_moscow_date(user_info['registration_date']))}"
    )
    try:
        await edit_screen(
            call,
            text,
            reply_markup=markup,
            screen="profile",
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await state.clear()


@router.callback_query(F.data == "sub_channel_done")
async def check_sub_to_channel(call: CallbackQuery, state: FSMContext):
    """
    Re-check channel subscription after user clicks "Check".
    """
    user_id = call.from_user.id
    channel_username = _parse_channel_username()
    helper = EnvKeys.HELPER_ID

    # Always recheck both chats; a negative middleware result may be stale.
    clear_subscription_cache(user_id)
    user_row = await ensure_user(user_id)
    gate = await build_subscription_gate(call.bot, user_id, user_row)
    if gate:
        try:
            if call.message is not None:
                await call.message.edit_text(gate[0], reply_markup=gate[1])
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.debug("Could not refresh subscription screen: %s", exc)
        await call.answer(localize("errors.not_subscribed"))
        return

    role = await check_role_cached(user_id) or 0
    markup = main_menu(
        role, channel_username, helper,
        support_url=_env_text("SUPPORT_URL"),
        support_username=_env_text("SUPPORT_USERNAME"),
    )
    await edit_screen(
        call,
        _main_text(call.from_user),
        reply_markup=markup,
        screen="main-menu",
    )
    await state.clear()


@router.callback_query(F.data == "subscription_continue_without_community")
async def continue_without_community(call: CallbackQuery, state: FSMContext):
    """Handle stale optional-invite buttons without bypassing the chat gate."""
    user_id = call.from_user.id
    user_row = await ensure_user(user_id)
    if user_row is None:
        await call.answer(localize("errors.something_wrong"), show_alert=True)
        return

    clear_subscription_cache(user_id)
    if not user_row.get("community_chat_required", False):
        try:
            await set_community_prompt_seen(user_id)
        except Exception:
            logger.exception("Could not persist community invite choice for user %s", user_id)
        user_row["community_prompt_seen"] = True

    gate = await build_subscription_gate(call.bot, user_id, user_row)
    if gate:
        try:
            if call.message is not None:
                await call.message.edit_text(gate[0], reply_markup=gate[1])
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.debug("Could not refresh subscription screen: %s", exc)
        await call.answer(localize("errors.not_subscribed"), show_alert=True)
        return

    role = await check_role_cached(user_id) or 0
    await edit_screen(
        call,
        _main_text(call.from_user),
        reply_markup=_main_markup(role, _parse_channel_username()),
        screen="main-menu",
    )
    await state.clear()


# --- Operation History ---

@router.callback_query(F.data == "operation_history")
async def operation_history_handler(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    await _show_operations_page(call, state, user_id, 0)


@router.callback_query(F.data.startswith("ops-page_"))
async def navigate_operations(call: CallbackQuery, state: FSMContext):
    try:
        page = max(0, int(call.data.split("_")[1]))
    except (ValueError, IndexError):
        await call.answer(localize("errors.pagination_invalid"))
        return
    await _show_operations_page(call, state, call.from_user.id, page)


async def _show_operations_page(call: CallbackQuery, state: FSMContext, user_id: int, page: int):
    from functools import partial
    from bot.misc import LazyPaginator

    paginator = LazyPaginator(partial(query_user_operations_history, user_id), per_page=10)
    items = await paginator.get_page(page)
    total_pages = await paginator.get_total_pages()

    if not items:
        await edit_screen(
            call,
            localize("history.title") + "\n\n" + localize("history.empty"),
            reply_markup=back("profile"),
            screen="orders",
        )
        return

    lines = [localize("history.title"), ""]
    for op in items:
        op_type = op['type']
        amount = op['amount']
        date_str = format_dt(op['date'])

        if op_type == 'topup':
            lines.append(localize("history.topup", amount=amount, currency=EnvKeys.PAY_CURRENCY))
        elif op_type == 'purchase':
            lines.append(localize("history.purchase", amount=amount, currency=EnvKeys.PAY_CURRENCY))
        elif op_type == 'referral':
            lines.append(localize("history.referral", amount=amount, currency=EnvKeys.PAY_CURRENCY))
        lines.append(localize("history.date", date=date_str))
        lines.append("")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    kb = InlineKeyboardBuilder()
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"ops-page_{page - 1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="dummy_button"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"ops-page_{page + 1}"))
    if nav_buttons:
        kb.row(*nav_buttons)
    kb.row(InlineKeyboardButton(text=localize("btn.back"), callback_data="profile"))

    await edit_screen(
        call,
        "\n".join(lines),
        reply_markup=colorize_markup(kb.as_markup()),
        screen="orders",
    )

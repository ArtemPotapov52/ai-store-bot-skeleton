from typing import Callable, Iterable, Tuple
from urllib.parse import quote

from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from bot.keyboards.icons import button_custom_emoji_id
from bot.keyboards.styles import colorize_markup
from bot.i18n import localize
from bot.database.models import Permission
from bot.misc import EnvKeys, LazyPaginator # noqa: F401


def support_username() -> str:
    """Sanitized support username (letters, digits, underscore) or ""."""
    raw = str(getattr(EnvKeys, "SUPPORT_USERNAME", "") or "")
    username = raw.strip().lstrip("@").strip()
    username = "".join(c for c in username if c.isalnum() or c == "_")
    return username


def support_prefilled_link(username: str | None = None) -> str | None:
    """t.me link opening support chat with a pre-filled message, or None."""
    username = username or support_username()
    if not username:
        return None
    raw_shop = str(getattr(EnvKeys, "SHOP_NAME", "") or "").strip() or "My Store"
    try:
        text = localize("support.prefilled", shop_name=raw_shop)
    except Exception:
        text = f"Привет! Я из {raw_shop}, у меня такая проблема: "
    # Backward-compat: if translation has no placeholder, old static text
    # would remain — replace generic wording with the shop name.
    if "{shop_name}" in text or "этого ТГ-бота" in text or "this Telegram bot" in text or "bot Telegram này" in text:
        text = text.replace("{shop_name}", raw_shop)
        text = text.replace("этого ТГ-бота", raw_shop)
        text = text.replace("this Telegram bot", raw_shop)
        text = text.replace("bot Telegram này", raw_shop)
    # quote (not quote_plus): Telegram renders "+" literally, "%20" as a space.
    return f"https://t.me/{username}?text={quote(text)}"


def main_menu(
    role: int,
    channel: str | None = None,
    helper: str | None = None,
    support_url: str | None = None,
    support_username: str | None = None,
) -> InlineKeyboardMarkup:
    """
    Main menu.
    """
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=localize("btn.shop"), callback_data="shop"),
        InlineKeyboardButton(text=localize("btn.profile"), callback_data="profile"),
    )
    support_link = (
        support_url
        if isinstance(support_url, str) and support_url.startswith(("https://", "tg://"))
        else None
    )
    if support_link:
        kb.row(InlineKeyboardButton(
            text=localize("btn.support"),
            url=support_link,
            icon_custom_emoji_id=button_custom_emoji_id("support"),
        ))
    elif (prefilled := support_prefilled_link(support_username)):
        kb.row(InlineKeyboardButton(
            text=localize("btn.support"),
            url=prefilled,
            icon_custom_emoji_id=button_custom_emoji_id("support"),
        ))
    elif helper:
        kb.row(InlineKeyboardButton(
            text=localize("btn.support"),
            url=f"tg://user?id={helper}",
            icon_custom_emoji_id=button_custom_emoji_id("support"),
        ))
    else:
        kb.row(InlineKeyboardButton(text=localize("btn.support"), callback_data="support"))
    kb.row(InlineKeyboardButton(text=localize("btn.language"), callback_data="language"))
    kb.row(
        InlineKeyboardButton(text=localize("btn.rules"), callback_data="rules"),
        InlineKeyboardButton(text=localize("btn.faq"), callback_data="faq"),
    )
    # The news-channel button was removed on purpose: subscription is enforced
    # by the gate (subscribe screen links the channel), so the menu stays
    # focused on shopping. The `channel` argument is kept for compatibility.
    _ = channel
    # Administrative tools remain available through the dedicated admin
    # entrypoint; the customer-facing main menu stays focused on shopping.
    return colorize_markup(kb.as_markup(), uniform="success")


def profile_keyboard(referral_percent: int, user_items: int = 0, cart_count: int = 0) -> InlineKeyboardMarkup:
    """
    Compact profile keyboard matching the storefront's primary flow.
    """
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text=localize("btn.replenish"), callback_data="replenish_balance"),
        InlineKeyboardButton(text=localize("btn.orders"), callback_data="bought_items"),
    )
    if referral_percent != 0:
        kb.row(
            InlineKeyboardButton(text=localize("btn.referral"), callback_data="referral_system"),
            InlineKeyboardButton(text=localize("btn.agreement"), callback_data="agreement"),
        )
    else:
        kb.row(InlineKeyboardButton(text=localize("btn.agreement"), callback_data="agreement"))
    kb.row(InlineKeyboardButton(text=localize("btn.cart", count=cart_count), callback_data="cart"))
    kb.row(InlineKeyboardButton(text=localize("btn.redeem_promo"), callback_data="redeem_promo"))
    kb.row(InlineKeyboardButton(text=localize("btn.main_menu"), callback_data="back_to_menu"))
    return colorize_markup(kb.as_markup(), uniform="success")


def legal_keyboard(back_callback: str) -> InlineKeyboardMarkup:
    """Shop's public documents: user agreement + privacy policy links."""
    kb = InlineKeyboardBuilder()
    agreement_url = str(getattr(EnvKeys, "LEGAL_AGREEMENT_URL", "") or "")
    privacy_url = str(getattr(EnvKeys, "LEGAL_PRIVACY_URL", "") or "")
    if agreement_url.startswith("https://"):
        kb.button(text=localize("btn.terms"), url=agreement_url)
    if privacy_url.startswith("https://"):
        kb.button(text=localize("btn.privacy"), url=privacy_url)
    kb.button(text=localize("btn.back"), callback_data=back_callback)
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(1)
    return colorize_markup(kb.as_markup(), uniform="success")


def language_keyboard() -> InlineKeyboardMarkup:
    return simple_buttons(
        [
            ("🇷🇺 Русский", "set-language:ru"),
            ("🇬🇧 English", "set-language:en"),
            ("🇻🇳 Tiếng Việt", "set-language:vi"),
            (localize("btn.main_menu"), "back_to_menu"),
        ]
    )


def admin_console_keyboard(maintenance_mode: bool = False, role: int = 127) -> InlineKeyboardMarkup:
    """
    Admin panel — shows only buttons the user has permissions for.
    """
    kb = InlineKeyboardBuilder()
    if role & Permission.CATALOG_MANAGE:
        kb.button(text=localize("admin.menu.shop"), callback_data="shop_management")
        kb.button(text=localize("admin.menu.goods"), callback_data="goods_management")
        kb.button(text=localize("admin.menu.categories"), callback_data="categories_management")
    if role & Permission.PROMO_MANAGE:
        kb.button(text=localize("admin.menu.promo"), callback_data="promo_mgmt")
    if role & Permission.USERS_MANAGE:
        kb.button(text=localize("admin.menu.users"), callback_data="user_management")
    if role & Permission.ADMINS_MANAGE:
        kb.button(text=localize("admin.menu.roles"), callback_data="role_mgmt")
    if role & Permission.BROADCAST:
        kb.button(text=localize("admin.menu.broadcast"), callback_data="send_message")
    if role & Permission.SETTINGS_MANAGE:
        maintenance_key = "admin.menu.maintenance_on" if maintenance_mode else "admin.menu.maintenance_off"
        kb.button(text=localize(maintenance_key), callback_data="toggle_maintenance")
    if role & Permission.USERS_MANAGE:
        kb.button(text=localize("admin.menu.user_search"), callback_data="user_search")
    if role & Permission.BALANCE_MANAGE:
        kb.button(text=localize("admin.menu.balance_topup"), callback_data="admin_replenish_balance")
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(1)
    return colorize_markup(kb.as_markup(), uniform="success")


def simple_buttons(buttons: Iterable[Tuple[str, str]], per_row: int = 1) -> InlineKeyboardMarkup:
    """
    Universal button assembly from (text, callback_data)
    """
    kb = InlineKeyboardBuilder()
    for text, cb in buttons:
        kb.button(text=text, callback_data=cb)
    kb.adjust(per_row)
    return colorize_markup(kb.as_markup(), uniform="success")


def back(cb: str = "menu", text: str | None = None) -> InlineKeyboardMarkup:
    """One back button; screens that need a home escape add it explicitly."""
    return simple_buttons([(text or localize("btn.back"), cb)])


def close() -> InlineKeyboardMarkup:
    """
    One button 'Close'.
    """
    return simple_buttons([(localize("btn.close"), "close")])


async def lazy_paginated_keyboard(
        paginator: 'LazyPaginator',
        item_text: Callable[[object], str],
        item_callback: Callable[[object], str],
        item_style: Callable[[object], str | None] | None = None,
        item_icon_custom_emoji_id: Callable[[object], str | None] | None = None,
        page: int = 0,
        back_cb: str | None = None,
        nav_cb_prefix: str = "",
        back_text: str | None = None,
        extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    """
    Lazy pagination keyboard with data loading on demand.

    `extra_rows` are inserted between the item buttons and the navigation row.
    """
    kb = InlineKeyboardBuilder()

    # Get items for current page
    items = await paginator.get_page(page)

    for item in items:
        button_kwargs = {
            "text": item_text(item),
            "callback_data": item_callback(item),
            "style": item_style(item) if item_style else "success",
        }
        if item_icon_custom_emoji_id:
            icon_id = item_icon_custom_emoji_id(item)
            if icon_id:
                button_kwargs["icon_custom_emoji_id"] = icon_id
        kb.button(**button_kwargs)
    kb.adjust(1)

    for row in (extra_rows or []):
        for button in row:
            if button.style is None:
                button.style = "success"
        kb.row(*row)

    # Navigation
    total_pages = await paginator.get_total_pages()
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(
                text="◀️", callback_data=f"{nav_cb_prefix}{page - 1}", style="success"
            ))
        nav_buttons.append(InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}", callback_data="dummy_button", style="success"
        ))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(
                text="▶️", callback_data=f"{nav_cb_prefix}{page + 1}", style="success"
            ))
        kb.row(*nav_buttons)

    if back_cb:
        kb.row(InlineKeyboardButton(
            text=back_text or localize("btn.back"), callback_data=back_cb, style="success"
        ))

    return colorize_markup(kb.as_markup())


def item_info(
        back_data: str, avg_rating: float = None,
        review_count: int = 0, has_purchased: bool = False,
        reviews_enabled: bool = True, out_of_stock: bool = False,
        subscribed: bool = False,
) -> InlineKeyboardMarkup:
    """
    Product card with purchase and navigation buttons.

    When `out_of_stock`, offers a restock notification toggle instead of
    leaving the user at a dead end. Quantity is selected on the purchase
    confirmation screen.
    """
    kb = InlineKeyboardBuilder()
    if not out_of_stock:
        kb.button(text=localize("btn.buy_balance"), callback_data="buy_item")
        kb.button(text=localize("btn.add_to_cart"), callback_data="add_to_cart")
    if out_of_stock:
        if subscribed:
            kb.button(text=localize("btn.notify_stock_off"), callback_data="unsub_stock")
        else:
            kb.button(text=localize("btn.notify_stock"), callback_data="sub_stock")
    kb.button(text=localize("btn.back"), callback_data=back_data)
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(2)
    return colorize_markup(kb.as_markup(), uniform="success")


def purchase_keyboard(
        quantity: int,
        max_quantity: int,
        can_afford: bool,
        min_quantity: int = 1,
) -> InlineKeyboardMarkup:
    """Quantity selector and the balance-only purchase action.

    Payment provider buttons intentionally do not belong to this keyboard;
    they are shown only by the balance top-up screen after ``buy_topup``.
    """
    kb = InlineKeyboardBuilder()
    if max_quantity > min_quantity:
        kb.row(InlineKeyboardButton(
            text=localize("btn.enter_quantity"), callback_data="buy_qty:input",
        ))
    if can_afford:
        kb.row(InlineKeyboardButton(
            text=localize("btn.pay_balance"), callback_data="buy_confirm",
        ))
    else:
        kb.row(InlineKeyboardButton(
            text=localize("btn.topup_balance"), callback_data="buy_topup",
        ))
    kb.row(InlineKeyboardButton(text=localize("btn.back"), callback_data="back_to_item"))
    kb.row(InlineKeyboardButton(text=localize("btn.main_menu"), callback_data="back_to_menu"))
    return colorize_markup(kb.as_markup(), uniform="success")


def cart_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    """
    Cart view with quantity and remove controls; legacy promo data is ignored.
    """
    kb = InlineKeyboardBuilder()
    for item in items:
        kb.row(
            InlineKeyboardButton(text="➖", callback_data=f"cart_qty:{item['id']}:-1"),
            InlineKeyboardButton(
                text=f"{item['item_name']} ×{item['quantity']}",
                callback_data="dummy_button",
            ),
            InlineKeyboardButton(text="➕", callback_data=f"cart_qty:{item['id']}:1"),
        )
        kb.row(InlineKeyboardButton(
            text=localize("btn.cart_remove_item", name=item['item_name']),
            callback_data=f"cart_remove:{item['id']}",
        ))
    kb.row(InlineKeyboardButton(text=localize("btn.cart_checkout"), callback_data="cart_checkout"))
    kb.row(InlineKeyboardButton(text=localize("btn.cart_clear"), callback_data="cart_clear"))
    kb.row(InlineKeyboardButton(text=localize("btn.back"), callback_data="profile"))
    kb.row(InlineKeyboardButton(text=localize("btn.main_menu"), callback_data="back_to_menu"))
    return colorize_markup(kb.as_markup(), uniform="success")


def payment_menu(pay_url: str) -> InlineKeyboardMarkup:
    """
    Buttons under the invoice (CryptoPay, etc.).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=localize("btn.pay"), url=pay_url)
    kb.button(text=localize("btn.check_payment"), callback_data="check")
    kb.button(text=localize("btn.back"), callback_data="profile")
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(1)
    return colorize_markup(kb.as_markup())


def admin_payment_keyboard(pay_url: str, username: str) -> InlineKeyboardMarkup:
    """Open a prepared top-up request to the administrator."""
    kb = InlineKeyboardBuilder()
    kb.button(
        text=localize("btn.pay.admin.open", username=username),
        url=pay_url,
    )
    kb.button(text=localize("btn.back"), callback_data="replenish_balance")
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(1)
    return colorize_markup(kb.as_markup())


def get_payment_choice() -> InlineKeyboardMarkup:
    """
    Select a payment method.
    """
    methods = [
        (localize("btn.pay.sbp_card"), "pay_sbp_card"),
        # CryptoBot is shown even before its token is configured so the bot
        # can explain the one-time setup instead of hiding the option.
        (localize("btn.pay.crypto"), "pay_cryptopay"),
        # Same for xRocket: without a token the bot shows setup instructions.
        (localize("btn.pay.xrocket"), "pay_xrocket"),
        # Manual top-ups are available without an external payment service.
        (localize("btn.pay.admin"), "pay_admin"),
    ]
    # Manual wallet transfer is shown only when its single receiving address is configured.
    if getattr(EnvKeys, "MANUAL_USDT_BEP20", ""):
        methods.insert(3, (localize("btn.pay.manual_crypto"), "pay_manual_crypto"))
    if EnvKeys.TELEGRAM_PROVIDER_TOKEN:
        methods.append((localize("btn.pay.tg"), "pay_fiat"))
    if EnvKeys.TEST_PAYMENT_ENABLED == "1":
        methods.append((localize("btn.pay.test"), "pay_test"))
    methods.append((localize("btn.back"), "replenish_balance"))
    methods.append((localize("btn.main_menu"), "back_to_menu"))
    return simple_buttons(methods, per_row=1)


def question_buttons(question: str, back_data: str) -> InlineKeyboardMarkup:
    """
    Universal yes/no + Back.
    """
    kb = InlineKeyboardBuilder()
    kb.button(text=localize("btn.yes"), callback_data=f"{question}_yes")
    kb.button(text=localize("btn.no"), callback_data=f"{question}_no")
    kb.button(text=localize("btn.back"), callback_data=back_data)
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(2)
    return colorize_markup(kb.as_markup())


def check_sub(
    channel_username: str | None = None,
    channel_url: str | None = None,
    community_url: str | None = None,
    allow_skip_community: bool = False,
) -> InlineKeyboardMarkup:
    """Render required channel/chat links and the subscription re-check action.

    ``channel_username`` is kept for backwards compatibility.  Private
    channels can rotate their invite hash, so callers may pass the current
    URL resolved from Telegram instead of rebuilding a stale invite link.
    """
    kb = InlineKeyboardBuilder()
    if channel_username:
        channel_url = channel_url or f"https://t.me/{channel_username}"
        kb.button(text=localize("btn.channel"), url=channel_url)
    if isinstance(community_url, str) and community_url.startswith(("https://t.me/", "tg://")):
        kb.button(text=localize("btn.community"), url=community_url)
    kb.button(text=localize("btn.check_subscription"), callback_data="sub_channel_done")
    if allow_skip_community:
        kb.button(
            text=localize("btn.continue_without_community"),
            callback_data="subscription_continue_without_community",
        )
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(1)
    return colorize_markup(kb.as_markup())


def rating_keyboard() -> InlineKeyboardMarkup:
    """Rating selection keyboard (1-5 stars)."""
    kb = InlineKeyboardBuilder()
    for i in range(1, 6):
        kb.button(text="⭐" * i, callback_data=f"rating:{i}")
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(5)
    return colorize_markup(kb.as_markup())


def referral_system_keyboard(has_referrals: bool = False, has_earnings: bool = False) -> InlineKeyboardMarkup:
    """
    Referral system keyboard with additional buttons.
    """
    kb = InlineKeyboardBuilder()

    if has_referrals:
        kb.button(text=localize("btn.view_referrals"), callback_data="view_referrals")

    if has_earnings:
        kb.button(text=localize("btn.view_earnings"), callback_data="view_all_earnings")

    kb.button(text=localize("btn.back"), callback_data="profile")
    kb.button(text=localize("btn.main_menu"), callback_data="back_to_menu")
    kb.adjust(1)
    return colorize_markup(kb.as_markup())

import asyncio
from decimal import Decimal
from functools import partial

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from pydantic import ValidationError

from bot.database.methods import (
    get_bought_item_info, query_categories, query_user_bought_items, get_item_info_cached,
    select_item_values_amount_cached, effective_price, get_catalog_items_summary,
    check_category_cached,
)
from bot.database.methods.read import (
    has_purchased_item,
    get_user_review, invalidate_rating_cache, is_subscribed_to_stock,
    check_value_cached, get_category_name_by_id,
)
from bot.database.methods.pricing import purchase_quantity_limits
from bot.database.methods.create import create_review, subscribe_to_stock
from bot.database.methods.delete import unsubscribe_from_stock
from bot.database.methods.lazy_queries import (
    query_item_reviews,
    query_goods_search,
    query_items_in_category,
    query_subcategories,
)
from bot.database.methods.transactions import redeem_balance_promo
from bot.database.methods.audit import log_audit_bg
from bot.database.models import Permission
from bot.keyboards import item_info, back, lazy_paginated_keyboard
from bot.keyboards.inline import simple_buttons, rating_keyboard
from bot.keyboards.styles import colorize_markup
from aiogram.types import InlineKeyboardButton
from bot.i18n import localize, esc, format_dt
from bot.misc import EnvKeys, LazyPaginator, ReviewRequest
from bot.catalog_labels import (
    category_button_text,
    category_custom_emoji_id,
    category_heading_icon,
    category_intro,
    shop_heading_icon,
)
from bot.catalog_structure import (
    GAME_CURRENCY_CATEGORY,
    GAME_CURRENCY_VISIBLE_IN_STOREFRONT,
)
from bot.misc.screens import answer_screen, edit_screen
from bot.misc.metrics import get_metrics
from bot.states import ShopStates
from bot.states.review_state import ReviewFSM
from bot.states.promo_state import PromoFSM

router = Router()

# Keep review records and API support, but hide reviews from the bot UI.
REVIEWS_UI_VISIBLE = False


def _browsing_state_for(back_data: str):
    """The FSM state the item card's Back button needs, or None if it needs none."""
    if back_data.startswith('gp_'):
        return ShopStates.viewing_goods
    if back_data.startswith('sp_'):
        return ShopStates.viewing_search_results
    return None


def _page_arg(raw: str) -> int | None:
    """Parse a page number out of callback_data. None if it is not a valid page."""
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None
    return page if page >= 0 else None


def _split_description(description: str, chunk_size: int = 2600) -> list[str]:
    """Split an unusually long description without dropping or reordering text."""
    remaining = description
    chunks: list[str] = []
    while len(remaining) > chunk_size:
        cut = max(remaining.rfind("\n", 0, chunk_size + 1), remaining.rfind(" ", 0, chunk_size + 1))
        if cut <= 0:
            cut = chunk_size
        elif remaining[cut] == " ":
            cut += 1
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


# --- Shared helper: render item page ---

async def _render_item_page(target, state: FSMContext, item_name: str, back_data: str = None, user_id: int = None):
    """
    Render the item detail page at the catalog price.
    `target` can be CallbackQuery or Message.
    """
    data = await state.get_data()
    if not back_data:
        back_data = data.get('item_back_data', 'gp_0')

    item_info_data = await get_item_info_cached(item_name)
    if not item_info_data or not item_info_data.get("is_active", True):
        if isinstance(target, CallbackQuery):
            await target.answer(localize("shop.item.not_found"), show_alert=True)
        else:
            await answer_screen(
                target,
                localize("shop.item.not_found"),
                reply_markup=back("shop"),
                screen="catalog",
            )
        return

    # Product cards inherit the category artwork when a product has no own
    # image. This keeps the ChatGPT/Gemini/Claude/Grok card visible above the
    # description and also gives the screen layer a media message to replace.
    image_ref = item_info_data.get("image_ref")
    category_name = None
    category_info = None
    category_id = item_info_data.get("category_id")
    if category_id:
        category_name = await get_category_name_by_id(int(category_id))
        category_info = await check_category_cached(category_name) if category_name else None
    if not image_ref:
        image_ref = category_info.get("image_ref") if category_info else None

    # Reuse the same artwork on the quantity/confirmation screen.  This also
    # lets the screen editor keep a single photo message throughout the flow.
    await state.update_data(item_image_ref=image_ref)

    required_state = _browsing_state_for(back_data)
    if required_state is not None:
        await state.set_state(required_state)

    reviews_enabled = EnvKeys.REVIEWS_ENABLED == "1" and REVIEWS_UI_VISIBLE

    reads = [select_item_values_amount_cached(item_name), check_value_cached(item_name)]
    if reviews_enabled:
        reads.append(query_item_reviews(item_name, count_only=True))
        if user_id:
            reads.append(has_purchased_item(user_id, item_name))
    results = await asyncio.gather(*reads)

    quantity, is_infinite = results[0], results[1]
    review_count_val = results[2] if reviews_enabled else 0
    purchased = results[3] if (reviews_enabled and user_id) else False

    quantity_line = (
        localize("shop.item.quantity_unlimited")
        if is_infinite
        else localize("shop.item.quantity_left", count=quantity)
    )

    try:
        min_order_quantity, max_order_quantity = purchase_quantity_limits(item_info_data)
    except ValueError:
        min_order_quantity, max_order_quantity = 1, 99
    out_of_stock = (not is_infinite) and int(quantity or 0) < min_order_quantity
    subscribed = bool(
        out_of_stock and user_id and await is_subscribed_to_stock(user_id, item_name)
    )

    price, _on_sale, _original_price = effective_price(item_info_data)
    price_line = localize("shop.item.price", amount=price, currency=EnvKeys.PAY_CURRENCY)

    markup = item_info(
        back_data,
        review_count=review_count_val,
        has_purchased=purchased,
        reviews_enabled=reviews_enabled,
        out_of_stock=out_of_stock, subscribed=subscribed,
    )

    description = str(item_info_data.get("description") or "")

    header_lines = [
        category_intro(category_name),
        localize("shop.item.title", name=esc(item_name)),
    ]
    footer_lines = [price_line, quantity_line]
    if item_info_data.get("is_variable_pricing"):
        footer_lines.append(localize(
            "shop.item.variable_limits",
            min_quantity=min_order_quantity,
            max_quantity=max_order_quantity,
        ))
    if out_of_stock and item_info_data.get("availability_note"):
        footer_lines.append(localize(
            "shop.item.availability_note",
            note=esc(item_info_data["availability_note"]),
        ))
    def _compose_item_text(description_text: str) -> str:
        text_lines = [
            *header_lines,
            localize("shop.item.description", description=esc(description_text)),
            *footer_lines,
        ]
        return "\n\n".join(line for line in text_lines if line)

    text = _compose_item_text(description)
    text_parts = [text]
    # Telegram captions are limited to 1024 characters, while regular messages
    # allow 4096. Keep the whole description by moving it into text messages;
    # exceptionally long descriptions are split across consecutive messages.
    if len(text) > 4096:
        chunks = _split_description(description)
        text_parts = []
        for index, chunk in enumerate(chunks):
            lines = []
            if index == 0:
                lines.extend(header_lines)
            else:
                lines.append(localize("shop.item.description.continued"))
            lines.append(localize("shop.item.description", description=esc(chunk)))
            if index == len(chunks) - 1:
                lines.extend(footer_lines)
            text_parts.append("\n\n".join(line for line in lines if line))

    try:
        if hasattr(target, 'message') and hasattr(target.message, 'edit_text'):
            await edit_screen(
                target,
                text_parts if len(text_parts) > 1 else text,
                reply_markup=markup,
                screen="product",
                image_ref=image_ref,
            )
        else:
            await answer_screen(
                target,
                text_parts if len(text_parts) > 1 else text,
                reply_markup=markup,
                screen="product",
                image_ref=image_ref,
            )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# --- Shop / categories / items ---

async def _show_categories_page(call: CallbackQuery, state: FSMContext, page: int):
    """Render one page of the category list (shared by the shop entry + paginate handlers)."""
    paginator = LazyPaginator(query_categories, per_page=10)

    # Pre-fetch page items to build the index map used by the item_callback.
    page_items = await paginator.get_page(page)
    items_index = {cat: idx for idx, cat in enumerate(page_items)}

    markup = await lazy_paginated_keyboard(
        paginator=paginator,
        item_text=category_button_text,
        item_callback=lambda cat: f"cat:{items_index[cat]}:{page}",
        item_style=lambda _cat: "primary",
        item_icon_custom_emoji_id=category_custom_emoji_id,
        page=page,
        back_cb="back_to_menu",
        nav_cb_prefix="categories-page_",
        extra_rows=[[InlineKeyboardButton(
            text=localize("btn.search"), callback_data="shop_search", style="success",
        )]],
    )

    await edit_screen(
        call,
        localize("shop.categories.title", shop_icon=shop_heading_icon()),
        reply_markup=markup,
        screen="catalog",
    )
    await state.update_data(
        category_page_items=list(page_items),
        category_page_num=page,
    )


async def _show_subcategories_page(
    call: CallbackQuery,
    state: FSMContext,
    parent_category: str,
    parent_page: int,
    page: int,
):
    """Render children of a category, retaining the route back to its parent list."""
    paginator = LazyPaginator(partial(query_subcategories, parent_category), per_page=10)
    page_items = await paginator.get_page(page)
    items_index = {category: index for index, category in enumerate(page_items)}
    markup = await lazy_paginated_keyboard(
        paginator=paginator,
        item_text=category_button_text,
        item_callback=lambda category: f"subcategory:{items_index[category]}:{page}",
        item_style=lambda _category: "primary",
        item_icon_custom_emoji_id=category_custom_emoji_id,
        page=page,
        back_cb=f"categories-page_{parent_page}",
        nav_cb_prefix="subcategories-page_",
    )
    await edit_screen(
        call,
        localize(
            "shop.categories.subcategories",
            category=esc(parent_category),
            category_icon=category_heading_icon(parent_category),
        ),
        reply_markup=markup,
        screen="catalog",
    )
    await state.update_data(
        subcategory_page_items=list(page_items),
        subcategory_page_num=page,
        subcategory_parent_category=parent_category,
        subcategory_parent_page=parent_page,
    )
    await state.set_state(ShopStates.viewing_categories)


@router.callback_query(F.data == "shop")
async def shop_callback_handler(call: CallbackQuery, state: FSMContext):
    """Show list of shop categories with lazy loading."""
    metrics = get_metrics()
    if metrics:
        metrics.track_conversion("purchase_funnel", "view_shop", call.from_user.id)

    await _show_categories_page(call, state, 0)
    await state.set_state(ShopStates.viewing_categories)


@router.callback_query(F.data.startswith('categories-page_'))
async def navigate_categories(call: CallbackQuery, state: FSMContext):
    """Pagination across shop categories with cache."""
    parts = call.data.split('_', 1)
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    await _show_categories_page(call, state, page)


@router.callback_query(F.data.startswith('subcategories-page_'))
async def navigate_subcategories(call: CallbackQuery, state: FSMContext):
    """Paginate within the currently open category group."""
    page = _page_arg(call.data.removeprefix("subcategories-page_"))
    if page is None:
        await call.answer(localize("errors.pagination_invalid"), show_alert=True)
        return
    data = await state.get_data()
    parent_category = data.get("subcategory_parent_category")
    if not parent_category:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return
    await _show_subcategories_page(
        call,
        state,
        str(parent_category),
        int(data.get("subcategory_parent_page", 0)),
        page,
    )


async def _show_goods_page(call: CallbackQuery, state: FSMContext,
                           category_name: str, cat_page: int, page: int,
                           back_data: str | None = None):
    """Render one page of goods inside a category (shared by category-open + paginate)."""
    from bot.database.methods.lazy_queries import query_items_in_category

    if back_data is None:
        back_data = (await state.get_data()).get("goods_back_data") or f"categories-page_{cat_page}"
    paginator = LazyPaginator(partial(query_items_in_category, category_name), per_page=10)

    page_items = await paginator.get_page(page)
    items_index = {item: i for i, item in enumerate(page_items)}
    summaries = await get_catalog_items_summary(page_items)

    def _button_text(item_name: str) -> str:
        item = summaries.get(item_name, {"name": item_name, "price": "—", "quantity": 0})
        common = {
            "name": item_name,
            "price": item.get("price", "—"),
            "currency": EnvKeys.PAY_CURRENCY,
        }
        if item.get("is_infinite"):
            label = localize("shop.goods.button.unlimited", **common)
        elif item.get("quantity", 0) >= item.get("min_quantity", 1):
            label = localize(
                "shop.goods.button.in_stock", count=item["quantity"], **common
            )
        elif item.get("availability_note"):
            label = localize(
                "shop.goods.button.note", note=item["availability_note"], **common
            )
        else:
            label = localize("shop.goods.button.out", **common)
        return label if len(label) <= 64 else label[:61] + "…"

    def _button_style(item_name: str) -> str:
        item = summaries.get(item_name, {})
        if item.get("is_infinite") or item.get("quantity", 0) > 0:
            return "success"
        return "danger"

    markup = await lazy_paginated_keyboard(
        paginator=paginator,
        item_text=_button_text,
        item_callback=lambda item: f"itm:{items_index[item]}:{page}",
        item_style=_button_style,
        page=page,
        back_cb=back_data,
        nav_cb_prefix="gp_",
    )

    category_info = await check_category_cached(category_name)
    await edit_screen(
        call,
        localize(
            "shop.goods.choose",
            category=esc(category_name),
            category_icon=category_heading_icon(category_name),
        ),
        reply_markup=markup,
        screen="catalog",
        image_ref=category_info.get("image_ref") if category_info else None,
    )
    await state.update_data(
        current_category=category_name,
        goods_page_items=list(page_items),
        goods_page_num=page,
        categories_last_viewed_page=cat_page,
        goods_back_data=back_data,
    )
    await state.set_state(ShopStates.viewing_goods)


@router.callback_query(F.data.startswith('cat:'))
async def items_list_callback_handler(call: CallbackQuery, state: FSMContext):
    """
    Show items of selected category.
    Parse index and page from cat:{index}:{page}, look up category name from state.
    """
    try:
        parts = call.data.split(':')
        idx = int(parts[1])
        cat_page = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    category = await _page_item_from_state(state, 'category_page_items', 'category_page_num', cat_page, idx)
    if category is None:
        category = await _page_item_at(query_categories, cat_page, idx)
    if category is None:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    if (
        not GAME_CURRENCY_VISIBLE_IN_STOREFRONT
        and category == GAME_CURRENCY_CATEGORY
    ):
        await call.answer()
        return

    if await query_subcategories(category, count_only=True):
        await _show_subcategories_page(call, state, category, cat_page, 0)
        return

    await _show_goods_page(
        call, state, category, cat_page, 0,
        back_data=f"categories-page_{cat_page}",
    )


@router.callback_query(F.data.startswith('subcategory:'))
async def subcategory_list_callback_handler(call: CallbackQuery, state: FSMContext):
    """Open products inside a child category and return to its sibling list."""
    try:
        _prefix, idx_raw, page_raw = call.data.split(':', 2)
        idx, page = int(idx_raw), int(page_raw)
    except (ValueError, TypeError):
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    data = await state.get_data()
    parent_category = data.get("subcategory_parent_category")
    if not parent_category:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    category = await _page_item_from_state(
        state, "subcategory_page_items", "subcategory_page_num", page, idx
    )
    if category is None:
        category = await _page_item_at(
            partial(query_subcategories, str(parent_category)), page, idx
        )
    if category is None:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    await _show_goods_page(
        call,
        state,
        category,
        int(data.get("subcategory_parent_page", 0)),
        0,
        back_data=f"subcategories-page_{page}",
    )


@router.callback_query(F.data.startswith('gp_'), ShopStates.viewing_goods)
async def navigate_goods(call: CallbackQuery, state: FSMContext):
    """
    Pagination for items inside selected category.
    Format: gp_{page}
    """
    page = _page_arg(call.data[3:])
    if page is None:
        await call.answer(localize("errors.pagination_invalid"), show_alert=True)
        return
    data = await state.get_data()
    await _show_goods_page(
        call, state,
        data.get('current_category', ''),
        data.get('categories_last_viewed_page', 0),
        page,
    )


async def _page_item_at(query_func, page: int, idx: int):
    """Return the item at ``idx`` on ``page`` of ``query_func``, or None."""
    paginator = LazyPaginator(query_func, per_page=10)
    page_items = await paginator.get_page(page)
    if idx < 0 or idx >= len(page_items):
        return None
    return page_items[idx]


async def _page_item_from_state(state: FSMContext, list_key: str, page_key: str,
                                page: int, idx: int):
    """Resolve idx->name from the page list saved by the last render.

    Avoids re-running the list query the user just saw. Returns None when the
    state doesn't cover this page (restart, stale keyboard) — the caller then
    falls back to _page_item_at.
    """
    data = await state.get_data()
    if data.get(page_key) != page:
        return None
    items = data.get(list_key)
    if not items or idx < 0 or idx >= len(items):
        return None
    return items[idx]


async def _open_item(call: CallbackQuery, state: FSMContext, item_name: str, back_data: str):
    """Open an item card and record it for the on-screen (csrf) item context."""
    metrics = get_metrics()
    if metrics:
        metrics.track_conversion("purchase_funnel", "view_item", call.from_user.id)

    # Save item name and back_data in state
    updates = {"csrf_item": item_name, "item_back_data": back_data}
    await state.update_data(**updates)

    await _render_item_page(call, state, item_name, back_data, user_id=call.from_user.id)


@router.callback_query(F.data.startswith('itm:'))
async def item_info_callback_handler(call: CallbackQuery, state: FSMContext):
    """
    Show detailed information about the item.
    Format: itm:{index}:{page}
    """
    try:
        parts = call.data.split(':')
        idx = int(parts[1])
        goods_page = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    item_name = await _page_item_from_state(state, 'goods_page_items', 'goods_page_num', goods_page, idx)
    if not item_name:
        category = (await state.get_data()).get('current_category', '')
        item_name = await _page_item_at(partial(query_items_in_category, category), goods_page, idx)
    if not item_name:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return
    await _open_item(call, state, item_name, f"gp_{goods_page}")


# --- Catalog search ---

async def _show_search_page(target, state: FSMContext, query: str, page: int):
    """Render one page of search results. `target` is a CallbackQuery or Message."""
    paginator = LazyPaginator(
        partial(query_goods_search, query), per_page=10,
    )

    page_items = await paginator.get_page(page)
    safe_query = esc(query)

    async def _render(text, markup):
        if isinstance(target, CallbackQuery):
            await edit_screen(target, text, reply_markup=markup, screen="catalog")
        else:
            await answer_screen(target, text, reply_markup=markup, screen="catalog")

    if not page_items and page == 0:
        await _render(localize("shop.search.empty", query=safe_query), back("shop"))
        await state.set_state(None)
        return

    items_index = {item: i for i, item in enumerate(page_items)}
    markup = await lazy_paginated_keyboard(
        paginator=paginator,
        item_text=lambda item: item,
        item_callback=lambda item: f"sitm:{items_index[item]}:{page}",
        page=page,
        back_cb="shop",
        nav_cb_prefix="sp_",
    )

    total = await paginator.get_total_count()
    await _render(localize("shop.search.results", query=safe_query, count=total), markup)

    await state.update_data(
        search_query=query,
        search_page_items=list(page_items),
        search_page_num=page,
    )
    await state.set_state(ShopStates.viewing_search_results)


@router.callback_query(F.data == "shop_search")
async def shop_search_handler(call: CallbackQuery, state: FSMContext):
    """Prompt for a search query."""
    await edit_screen(
        call,
        localize("shop.search.prompt"),
        reply_markup=back("shop"),
        screen="catalog",
    )
    await state.set_state(ShopStates.waiting_search_query)


@router.message(ShopStates.waiting_search_query, F.text)
async def receive_search_query_handler(message: Message, state: FSMContext):
    query = (message.text or "").strip()

    if len(query) < 2 or len(query) > 64:
        # Stay in the state so the user can just retype.
        await answer_screen(
            message,
            localize("shop.search.too_short"),
            reply_markup=back("shop"),
            screen="catalog",
        )
        return

    await _show_search_page(message, state, query, 0)


@router.callback_query(F.data.startswith('sp_'), ShopStates.viewing_search_results)
async def navigate_search(call: CallbackQuery, state: FSMContext):
    """Pagination across search results. Format: sp_{page}"""
    page = _page_arg(call.data[3:])
    if page is None:
        await call.answer(localize("errors.pagination_invalid"), show_alert=True)
        return
    data = await state.get_data()
    await _show_search_page(call, state, data.get('search_query', ''), page)


@router.callback_query(F.data.startswith('sitm:'))
async def search_item_info_handler(call: CallbackQuery, state: FSMContext):
    """
    Open an item from the search results.
    Format: sitm:{index}:{page}

    A separate namespace from itm:/gp_ because navigate_goods re-derives its page
    from current_category, which search results do not have.
    """
    try:
        parts = call.data.split(':')
        idx = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    item_name = await _page_item_from_state(state, 'search_page_items', 'search_page_num', page, idx)
    if not item_name:
        query = (await state.get_data()).get('search_query', '')
        item_name = await _page_item_at(partial(query_goods_search, query), page, idx)
    if not item_name:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return
    await _open_item(call, state, item_name, f"sp_{page}")


# --- Restock notifications ---

@router.callback_query(F.data == "sub_stock")
async def subscribe_stock_handler(call: CallbackQuery, state: FSMContext):
    """Subscribe to the restock notification for the item on screen."""
    item_name = (await state.get_data()).get('csrf_item')
    if not item_name:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    ok, _code = await subscribe_to_stock(call.from_user.id, item_name)
    await call.answer(localize("stock.subscribed" if ok else "errors.something_wrong"))
    await _render_item_page(call, state, item_name, user_id=call.from_user.id)


@router.callback_query(F.data == "unsub_stock")
async def unsubscribe_stock_handler(call: CallbackQuery, state: FSMContext):
    """Cancel the restock notification for the item on screen."""
    item_name = (await state.get_data()).get('csrf_item')
    if not item_name:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    await unsubscribe_from_stock(call.from_user.id, item_name)
    await call.answer(localize("stock.unsubscribed"))
    await _render_item_page(call, state, item_name, user_id=call.from_user.id)


@router.callback_query(F.data == "back_to_item")
async def back_to_item_handler(call: CallbackQuery, state: FSMContext):
    """Return to the product page from the quantity picker."""
    data = await state.get_data()
    item_name = data.get('csrf_item')
    if not item_name:
        # Fallback
        await edit_screen(
            call,
            localize("shop.item.not_found"),
            reply_markup=back("back_to_menu"),
            screen="product",
        )
        return
    await _render_item_page(call, state, item_name, user_id=call.from_user.id)


# --- Balance Promo Redemption (from profile) ---

@router.callback_query(F.data == "redeem_promo")
async def redeem_promo_handler(call: CallbackQuery, state: FSMContext):
    await edit_screen(
        call,
        localize("promo.enter_redeem_code"),
        reply_markup=back("profile"),
        screen="profile",
    )
    await state.set_state(PromoFSM.waiting_redeem_code)


@router.message(PromoFSM.waiting_redeem_code, F.text)
async def redeem_promo_code_handler(message: Message, state: FSMContext):
    from bot.handlers.user.main import ensure_user
    if await ensure_user(message.from_user.id) is None:
        await answer_screen(
            message,
            localize("errors.something_wrong"),
            reply_markup=back("profile"),
            screen="profile",
        )
        return
    code = (message.text or "").strip().upper()
    success, error_key, amount = await redeem_balance_promo(code, message.from_user.id)

    if success:
        await answer_screen(
            message,
            localize("promo.balance_redeemed", code=code, amount=amount, currency=EnvKeys.PAY_CURRENCY),
            reply_markup=back("profile"),
            screen="profile",
        )
        log_audit_bg(
            "promo_redeem", user_id=message.from_user.id,
            resource_type="PromoCode", resource_id=code,
        )
    else:
        await answer_screen(
            message,
            localize(error_key),
            reply_markup=back("profile"),
            screen="profile",
        )

    await state.clear()


# --- Review Handlers ---

@router.callback_query(F.data == "review")
async def start_review_handler(call: CallbackQuery, state: FSMContext):
    if not REVIEWS_UI_VISIBLE or EnvKeys.REVIEWS_ENABLED != "1":
        await call.answer()
        return

    item_name = (await state.get_data()).get('csrf_item')
    if not item_name:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    # Check if user purchased the item
    purchased = await has_purchased_item(call.from_user.id, item_name)
    if not purchased:
        await call.answer(localize("review.not_purchased"), show_alert=True)
        return

    # Check if already reviewed
    existing = await get_user_review(call.from_user.id, item_name)
    if existing:
        await call.answer(localize("review.already_exists"), show_alert=True)
        return

    await state.update_data(review_item_name=item_name)
    await edit_screen(
        call,
        localize("review.prompt_rating", name=esc(item_name)),
        reply_markup=rating_keyboard(),
        screen="product",
    )
    await state.set_state(ReviewFSM.waiting_rating)


@router.callback_query(F.data.startswith("rating:"), ReviewFSM.waiting_rating)
async def receive_rating_handler(call: CallbackQuery, state: FSMContext):
    try:
        rating = ReviewRequest(rating=int(call.data.split(":")[1])).rating
    except (ValueError, IndexError, ValidationError):
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return

    await state.update_data(review_rating=rating)

    buttons = [
        (localize("btn.skip_review_text"), "skip_review_text"),
        (localize("btn.back"), "back_to_menu"),
    ]
    await edit_screen(
        call,
        localize("review.prompt_text"),
        reply_markup=simple_buttons(buttons),
        screen="product",
    )
    await state.set_state(ReviewFSM.waiting_text)


async def _submit_review(user_id: int, state: FSMContext, text: str | None) -> bool:
    """Persist the review accumulated in the FSM. False if it could not be saved.

    The item name and rating come out of state, which an expired session can
    leave empty — create_review also re-validates, so a bad pair is refused
    rather than raised.
    """
    data = await state.get_data()
    item_name = data.get('review_item_name')
    rating = data.get('review_rating')
    if not item_name or rating is None:
        return False

    if await create_review(user_id, item_name, rating, text) is None:
        return False

    await invalidate_rating_cache(item_name)
    return True


@router.callback_query(F.data == "skip_review_text", ReviewFSM.waiting_text)
async def skip_review_text_handler(call: CallbackQuery, state: FSMContext):
    ok = await _submit_review(call.from_user.id, state, None)
    await edit_screen(
        call,
        localize("review.created" if ok else "errors.something_wrong"),
        reply_markup=back("back_to_menu"),
        screen="product",
    )
    await state.clear()


@router.message(ReviewFSM.waiting_text, F.text)
async def receive_review_text_handler(message: Message, state: FSMContext):
    text = (message.text or "")[:500].strip()

    ok = await _submit_review(message.from_user.id, state, text)
    await answer_screen(
        message,
        localize("review.created" if ok else "errors.something_wrong"),
        reply_markup=back("back_to_menu"),
        screen="product",
    )
    await state.clear()


# --- View Reviews ---

@router.callback_query(F.data.startswith("reviews:"))
async def view_reviews_handler(call: CallbackQuery, state: FSMContext):
    """List an item's reviews. Format: reviews:{page}"""
    if not REVIEWS_UI_VISIBLE or EnvKeys.REVIEWS_ENABLED != "1":
        await call.answer()
        return

    try:
        page = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0

    item_name = (await state.get_data()).get('csrf_item')
    if not item_name:
        await call.answer(localize("shop.item.not_found"), show_alert=True)
        return

    paginator = LazyPaginator(partial(query_item_reviews, item_name), per_page=5)

    reviews = await paginator.get_page(page)
    total_pages = await paginator.get_total_pages()

    if not reviews:
        await edit_screen(
            call,
            localize("review.list_empty"),
            reply_markup=back("back_to_item"),
            screen="product",
        )
        return

    lines = [localize("review.list_title", name=esc(item_name)), ""]
    for r in reviews:
        if r.get('text'):
            lines.append(localize(
                "review.item", rating=r['rating'],
                text=esc(r['text'][:100]),
            ))
        else:
            lines.append(localize("review.item_no_text", rating=r['rating']))

    # Navigation
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    kb = InlineKeyboardBuilder()
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="◀️", callback_data=f"reviews:{page - 1}"))
    if total_pages > 1:
        nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="dummy_button"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="▶️", callback_data=f"reviews:{page + 1}"))
    if nav_buttons:
        kb.row(*nav_buttons)
    kb.row(InlineKeyboardButton(text=localize("btn.back"), callback_data="back_to_item"))

    await edit_screen(
        call,
        "\n".join(lines),
        reply_markup=colorize_markup(kb.as_markup()),
        screen="product",
    )


# --- Bought items ---

@router.callback_query(F.data == "bought_items")
async def bought_items_callback_handler(call: CallbackQuery, state: FSMContext):
    """
    Show list of user's purchased items with lazy loading.
    """
    user_id = call.from_user.id

    await _show_bought_items_page(call, user_id=user_id, page=0, data_type="user")

    # Save paginator state
@router.callback_query(F.data.startswith('bought-goods-page_'))
async def navigate_bought_items(call: CallbackQuery, state: FSMContext):
    """
    Pagination for user's purchased items with lazy loading.
    Format: 'bought-goods-page_{data}_{page}', where data = 'user' or user_id.
    """
    parts = call.data.split('_')
    if len(parts) < 3:
        await call.answer(localize("purchases.pagination.invalid"))
        return

    data_type = parts[1]
    try:
        current_index = int(parts[2])
    except ValueError:
        current_index = 0

    if data_type == 'user':
        user_id = call.from_user.id
        back_cb = 'profile'
        pre_back = f'bought-goods-page_user_{current_index}'
    else:
        # Admin path: viewing another user's purchases. Gate on USERS_MANAGE — this callback prefix is not covered by the auth middleware.
        from bot.database.methods import check_role_cached
        caller_perms = await check_role_cached(call.from_user.id) or 0
        if not Permission.granted(caller_perms, Permission.USERS_MANAGE):
            await call.answer(localize("middleware.security.not_admin"), show_alert=True)
            return
        try:
            user_id = int(data_type)
        except ValueError:
            await call.answer(localize("purchases.pagination.invalid"))
            return
        back_cb = f'check-user_{data_type}'
        pre_back = f'bought-goods-page_{data_type}_{current_index}'

    await _show_bought_items_page(
        call,
        user_id=user_id,
        page=current_index,
        data_type=data_type,
        back_cb=back_cb,
        pre_back=pre_back,
    )


async def _show_bought_items_page(
    call: CallbackQuery,
    *,
    user_id: int,
    page: int,
    data_type: str,
    back_cb: str = "profile",
    pre_back: str | None = None,
) -> None:
    paginator = LazyPaginator(partial(query_user_bought_items, user_id), per_page=7)
    items = await paginator.get_page(page)
    pages = max(await paginator.get_total_pages(), 1)
    page = min(max(page, 0), pages - 1)

    title = localize("purchases.title", page=page + 1, pages=pages)
    if not items:
        await edit_screen(
            call,
            title + "\n\n" + localize("purchases.empty"),
            reply_markup=back(back_cb),
            screen="orders",
        )
        return

    target = pre_back or f"bought-goods-page_{data_type}_{page}"

    def _label(item) -> str:
        text = localize(
            "purchases.button",
            id=item.id,
            name=item.item_name,
            price=item.price,
            currency=EnvKeys.PAY_CURRENCY,
        )
        return text if len(text) <= 64 else text[:61] + "…"

    markup = await lazy_paginated_keyboard(
        paginator=paginator,
        item_text=_label,
        item_callback=lambda item: f"bought-item:{item.id}:{target}",
        page=page,
        back_cb=back_cb,
        nav_cb_prefix=f"bought-goods-page_{data_type}_",
    )
    await edit_screen(call, title, reply_markup=markup, screen="orders")


@router.callback_query(F.data.startswith('bought-item:'))
async def bought_item_info_callback_handler(call: CallbackQuery):
    """
    Show details for a purchased item.

    Scoped to the caller's own purchases; an admin with USERS_MANAGE may view
    any buyer's row (falls back to an unscoped lookup only after the permission
    check).
    """
    try:
        _prefix, item_id_str, back_data = call.data.split(':', 2)
        item_id = int(item_id_str)
    except ValueError:
        await call.answer(localize("errors.invalid_data"), show_alert=True)
        return

    item = await get_bought_item_info(item_id, buyer_id=call.from_user.id)
    if not item:
        from bot.database.methods import check_role_cached
        caller_perms = await check_role_cached(call.from_user.id) or 0
        if Permission.granted(caller_perms, Permission.USERS_MANAGE):
            item = await get_bought_item_info(item_id)
    if not item:
        await call.answer(localize("purchases.item.not_found"), show_alert=True)
        return

    text = "\n".join([
        localize("purchases.item.name", name=esc(item["item_name"])),
        localize("purchases.item.price", amount=item["price"], currency=EnvKeys.PAY_CURRENCY),
        localize("purchases.item.datetime", dt=format_dt(item["bought_datetime"])),
        localize("purchases.item.unique_id", uid=item["unique_id"]),
        localize("purchases.item.value", value=esc(item["value"])),
    ])
    await edit_screen(
        call,
        text,
        reply_markup=back(back_data),
        screen="orders",
    )

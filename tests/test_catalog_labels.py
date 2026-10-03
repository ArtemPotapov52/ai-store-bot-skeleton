from bot.catalog_labels import (
    AI_EMOJIS,
    SHOP_CUSTOM_EMOJI_ID,
    category_button_text,
    category_custom_emoji_id,
    category_heading_icon,
    category_intro,
    shop_heading_icon,
)
from bot.keyboards.inline import lazy_paginated_keyboard
from bot.misc import LazyPaginator
from bot.i18n import reset_locale, set_locale


def test_known_category_button_uses_plain_label_with_premium_icon_field():
    assert category_button_text("ChatGPT") == "ChatGPT"
    assert category_custom_emoji_id("ChatGPT") == AI_EMOJIS["chatgpt"]
    assert category_custom_emoji_id("Gemini") == AI_EMOJIS["gemini"]


def test_capcut_category_uses_requested_premium_emoji():
    assert category_custom_emoji_id("CapCut") == "6131794507781382239"
    assert 'emoji-id="6131794507781382239"' in category_heading_icon("CapCut")


def test_category_name_is_not_mutated_when_it_has_existing_text():
    assert category_button_text("🧠 Claude") == "🧠 Claude"


def test_unknown_category_keeps_existing_label():
    assert category_button_text("Other") == "Other"
    assert category_custom_emoji_id("Other") is None
    assert category_heading_icon("Other") == shop_heading_icon()
    assert f'emoji-id="{SHOP_CUSTOM_EMOJI_ID}"' in category_heading_icon("Other")
    assert category_intro("Other") == ""


def test_products_heading_uses_the_shop_premium_emoji():
    heading_icon = shop_heading_icon()
    assert f'emoji-id="{SHOP_CUSTOM_EMOJI_ID}"' in heading_icon
    assert "🛍" in heading_icon


def test_product_intro_uses_the_configured_custom_emoji_id():
    intro = category_intro("ChatGPT")
    assert f'emoji-id="{AI_EMOJIS["chatgpt"]}"' in intro
    assert "Подберите подходящий доступ к ChatGPT" in intro


def test_goods_heading_uses_the_configured_custom_emoji_id():
    heading_icon = category_heading_icon("ChatGPT")
    assert f'emoji-id="{AI_EMOJIS["chatgpt"]}"' in heading_icon
    assert "🤖" in heading_icon


def test_product_intro_follows_current_locale():
    token = set_locale("en")
    try:
        assert "Choose the ChatGPT access option" in category_intro("ChatGPT")
    finally:
        reset_locale(token)


async def test_category_button_serializes_premium_icon_id():
    async def query(offset=0, limit=10, count_only=False):
        return 1 if count_only else ["ChatGPT"]

    markup = await lazy_paginated_keyboard(
        paginator=LazyPaginator(query, per_page=10),
        item_text=category_button_text,
        item_callback=lambda value: value,
        item_icon_custom_emoji_id=category_custom_emoji_id,
    )

    button = markup.inline_keyboard[0][0]
    assert button.text == "ChatGPT"
    assert button.icon_custom_emoji_id == AI_EMOJIS["chatgpt"]

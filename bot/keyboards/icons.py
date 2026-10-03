"""Premium custom-emoji icons for customer-facing inline buttons."""

from __future__ import annotations


# Telegram renders this field before the button label.  Keep the mapping by
# callback data so every keyboard that reuses a customer action gets the same
# icon, including nested screens and the shared "main menu" escape button.
BUTTON_CUSTOM_EMOJI_IDS: dict[str, str] = {
    "shop": "5226513232549664618",
    "profile": "5257991477358763590",
    "support": "5258391025281408576",
    "language": "5258115571848846212",
    # The user supplied three entity IDs for the remaining main-menu items.
    # The explicit support ID above takes precedence, so the other two are
    # used for rules/agreement and FAQ respectively.
    "rules": "5258336354642697821",
    "faq": "5258165702707125574",
    "replenish_balance": "5258204546391351475",
    "bought_items": "5323761960829862762",
    "back_to_menu": "5257963315258204021",
    # Additional profile actions use a stable spread of the supplied premium
    # icons; these are intentionally not regenerated on every render.
    "referral_system": "5258115571848846212",
    "agreement": "5258336354642697821",
    "cart": "5258165702707125574",
    "redeem_promo": "5190533058855475557",
}


def button_custom_emoji_id(callback_data: str | None) -> str | None:
    """Return the configured premium icon for a callback action, if any."""

    value = str(callback_data or "")
    if value.startswith("categories-page_"):
        # The catalog's dynamic back callback still gets a premium icon while
        # the stable home callback keeps the dedicated house icon above.
        return "5258336354642697821"
    return BUTTON_CUSTOM_EMOJI_IDS.get(value)


__all__ = ["BUTTON_CUSTOM_EMOJI_IDS", "button_custom_emoji_id"]

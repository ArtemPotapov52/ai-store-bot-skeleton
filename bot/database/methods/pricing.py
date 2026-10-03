from decimal import Decimal, InvalidOperation
from typing import Any

MAX_PURCHASE_QUANTITY = 5000
MAX_FIXED_PURCHASE_QUANTITY = 99


def _get(goods: Any, key: str):
    """Read a field from either an ORM object or a plain dict."""
    if isinstance(goods, dict):
        return goods.get(key)
    return getattr(goods, key, None)


def purchase_quantity_limits(goods: Any) -> tuple[int, int]:
    """Return the allowed per-order quantity range for a product.

    Existing fixed-price products keep their historical 1–99 range. Variable-pricing
    products must carry a valid, explicit range; rejecting malformed rows is
    safer than silently treating them as fixed-price products.
    """
    if not _get(goods, "is_variable_pricing"):
        return 1, MAX_FIXED_PURCHASE_QUANTITY
    minimum = _get(goods, "min_quantity")
    maximum = _get(goods, "max_quantity")
    if (
        isinstance(minimum, bool) or isinstance(maximum, bool)
        or not isinstance(minimum, int) or not isinstance(maximum, int)
        or not 1 <= minimum <= maximum <= MAX_PURCHASE_QUANTITY
    ):
        raise ValueError("invalid product quantity range")
    return minimum, maximum


def effective_price(goods: Any) -> tuple[Decimal, bool, Decimal]:
    """Return the catalog price; legacy sale metadata is intentionally ignored."""
    try:
        original = Decimal(str(_get(goods, 'price')))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid price") from exc
    if not original.is_finite() or original <= 0:
        raise ValueError("invalid price")
    original = original.quantize(Decimal("0.01"))
    if original <= 0:
        raise ValueError("invalid price")

    return original, False, original

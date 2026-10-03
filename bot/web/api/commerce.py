"""Account-scoped catalog quotes, carts, purchases, and product reviews."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request
from starlette.responses import JSONResponse

from bot.database import Database
from bot.database.methods.create import CART_MAX_ITEMS, CART_MAX_QTY_PER_ITEM, subscribe_to_stock
from bot.database.methods.delete import clear_cart, remove_from_cart
from bot.database.methods.pricing import (
    MAX_PURCHASE_QUANTITY, effective_price, purchase_quantity_limits,
)
from bot.database.methods.read import get_cart_items
from bot.database.methods.transactions import buy_item_transaction, checkout_cart_transaction
from bot.database.models import BoughtGoods, CartItems, Goods, Reviews, User
from bot.misc import EnvKeys
from bot.misc.timezone import moscow_isoformat
from bot.web.api.common import (
    ApiError, api_route, json_response, parse_pagination, read_json_object,
    reject_unknown_fields,
)
from bot.web.api.idempotency import begin_idempotency, finish_idempotency


def _money(value) -> str:
    return format(Decimal(str(value or 0)), ".2f")


def _decimal(value, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_amount", f"{field} must be a valid decimal amount.") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ApiError(400, "invalid_amount", f"{field} is outside the allowed range.")
    if result.as_tuple().exponent < -2:
        raise ApiError(400, "invalid_amount", f"{field} must have at most two decimal places.")
    return result.quantize(Decimal("0.01"))


async def _active_product(product_id: int) -> Goods:
    if product_id <= 0:
        raise ApiError(400, "invalid_product_id", "product_id must be a positive integer.")
    async with Database().session() as session:
        goods = (await session.execute(
            select(Goods).where(Goods.id == product_id, Goods.is_active.is_(True))
        )).scalar_one_or_none()
        if goods is None:
            raise ApiError(404, "product_not_found", "The product was not found.")
        # Detach only scalar data required after session exit.
        return goods


async def _purchase_receipt(user_id: int, saved: dict) -> dict:
    ids = [int(value) for value in saved.get("purchase_ids", [])]
    if not ids or len(ids) > MAX_PURCHASE_QUANTITY * CART_MAX_ITEMS:
        raise ApiError(409, "receipt_unavailable", "The purchase receipt is not available.")
    async with Database().session() as session:
        rows = (await session.execute(
            select(BoughtGoods).where(
                BoughtGoods.id.in_(ids), BoughtGoods.buyer_id == user_id,
            )
        )).scalars().all()
    by_id = {int(row.id): row for row in rows}
    if len(by_id) != len(ids):
        raise ApiError(409, "receipt_unavailable", "The purchase receipt is not available.")
    return {
        "items": [
            {
                "purchase_id": purchase_id,
                "product_name": str(by_id[purchase_id].item_name),
                "delivery": str(by_id[purchase_id].value),
                "price": _money(by_id[purchase_id].price),
                "purchased_at": moscow_isoformat(by_id[purchase_id].bought_datetime),
            }
            for purchase_id in ids
        ],
        "total": _money(saved.get("total")),
        "balance": _money(saved.get("balance")),
        "quantity": int(saved.get("quantity", len(ids))),
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
    }


_PURCHASE_ERRORS = {
    "user_not_found": (404, "account_unavailable", "The account was not found."),
    "item_not_found": (404, "product_not_found", "The product was not found."),
    "invalid_quantity": (400, "invalid_quantity", "quantity is outside the product's allowed range."),
    "invalid_price": (409, "price_unavailable", "The current product price is unavailable."),
    "price_changed": (409, "price_changed", "The total changed; request a fresh quote before purchasing."),
    "insufficient_funds": (402, "insufficient_funds", "The account balance is too low."),
    "out_of_stock": (409, "out_of_stock", "There is not enough stock for this purchase."),
    "vpn_unconfigured": (503, "vpn_proxy_unconfigured", "VPN delivery is temporarily unavailable; no balance was charged."),
    "cart_empty": (409, "cart_empty", "The cart is empty."),
    "cart_items_unavailable": (409, "cart_items_unavailable", "No cart items are currently available."),
}


def _purchase_error(code: str) -> ApiError:
    status, error_code, message = _PURCHASE_ERRORS.get(
        code, (503, "purchase_failed", "The purchase could not be safely completed.")
    )
    return ApiError(status, error_code, message)


async def quote_order(request: Request) -> JSONResponse:
    body = await read_json_object(request)
    reject_unknown_fields(body, {"product_id", "quantity", "promo_code"})
    product_id = body.get("product_id")
    quantity = body.get("quantity", 1)
    if isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0:
        raise ApiError(400, "invalid_order", "product_id must be a positive integer.")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_PURCHASE_QUANTITY:
        raise ApiError(400, "invalid_quantity", f"quantity must be an integer from 1 to {MAX_PURCHASE_QUANTITY}.")
    promo_value = body.get("promo_code")
    if promo_value is not None and not isinstance(promo_value, str):
        raise ApiError(400, "invalid_promo", "promo_code must be a string.")
    if str(promo_value or "").strip():
        raise ApiError(400, "discounts_disabled", "Purchase discount codes are disabled.")
    product = await _active_product(product_id)
    try:
        min_quantity, max_quantity = purchase_quantity_limits(product)
    except ValueError as exc:
        raise ApiError(409, "quantity_unavailable", "The product quantity range is unavailable.") from exc
    if not min_quantity <= quantity <= max_quantity:
        raise ApiError(
            400,
            "invalid_quantity",
            f"quantity must be from {min_quantity} to {max_quantity} for this product.",
        )
    try:
        unit_price, _on_sale, _original = effective_price(product)
    except ValueError as exc:
        raise ApiError(409, "price_unavailable", "The current product price is unavailable.") from exc
    subtotal = (unit_price * quantity).quantize(Decimal("0.01"))
    total = subtotal
    return json_response({
        "data": {
            "product_id": product_id,
            "product_name": product.name,
            "quantity": quantity,
            "min_quantity": min_quantity,
            "max_quantity": max_quantity,
            "unit_price": _money(unit_price),
            "subtotal": _money(subtotal),
            "total": _money(total),
            "currency": str(EnvKeys.PAY_CURRENCY).upper(),
        }
    })


async def get_cart(request: Request) -> JSONResponse:
    from bot.handlers.user.cart import _cart_view_data

    user_id = int(request.state.api_user_id)
    items, _info, line_data, total = await _cart_view_data(user_id)
    return json_response({
        "data": [
            {
                "cart_item_id": int(item["id"]),
                "product_id": int(item["item_id"]),
                "product_name": str(item["item_name"]),
                "quantity": int(item["quantity"]),
                "promo_code": None,
                "line_total": _money(line_data[item["id"]]["line_total"]) if item["id"] in line_data else None,
                "currency": str(EnvKeys.PAY_CURRENCY).upper(),
            }
            for item in items
        ],
        "total": _money(total),
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
    })


async def set_cart_item(request: Request) -> JSONResponse:
    body = await read_json_object(request)
    reject_unknown_fields(body, {"quantity", "promo_code"})
    try:
        product_id = int(request.path_params["product_id"])
        quantity = body.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_cart_item", "product_id and integer quantity are required.") from exc
    if not 1 <= quantity <= CART_MAX_QTY_PER_ITEM:
        raise ApiError(400, "invalid_quantity", f"quantity must be from 1 to {CART_MAX_QTY_PER_ITEM}.")
    product = await _active_product(product_id)
    try:
        min_quantity, max_quantity = purchase_quantity_limits(product)
    except ValueError as exc:
        raise ApiError(409, "quantity_unavailable", "The product quantity range is unavailable.") from exc
    if not min_quantity <= quantity <= max_quantity:
        raise ApiError(
            400,
            "invalid_quantity",
            f"quantity must be from {min_quantity} to {max_quantity} for this product.",
        )
    promo_value = body.get("promo_code")
    if promo_value is not None:
        if not isinstance(promo_value, str):
            raise ApiError(400, "invalid_promo", "promo_code must be a string.")
        if promo_value.strip():
            raise ApiError(400, "discounts_disabled", "Purchase discount codes are disabled.")
    promo_code = None

    user_id = int(request.state.api_user_id)
    async with Database().session() as session:
        line = (await session.execute(
            select(CartItems).where(
                CartItems.user_id == user_id,
                CartItems.item_id == product_id,
            ).with_for_update()
        )).scalar_one_or_none()
        if line is None:
            count = int((await session.execute(
                select(func.count(CartItems.id)).where(CartItems.user_id == user_id)
            )).scalar_one() or 0)
            if count >= CART_MAX_ITEMS:
                raise ApiError(409, "cart_full", "The cart has reached its line limit.")
            line = CartItems(user_id=user_id, item_id=product_id, quantity=quantity, promo_code=promo_code)
            session.add(line)
        else:
            line.quantity = quantity
            line.promo_code = promo_code
        await session.flush()
        result = {"cart_item_id": int(line.id), "product_id": product_id, "quantity": quantity}
    return json_response({"data": result})


async def delete_cart_item(request: Request) -> JSONResponse:
    try:
        product_id = int(request.path_params["product_id"])
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_product_id", "product_id must be an integer.") from exc
    if product_id <= 0:
        raise ApiError(400, "invalid_product_id", "product_id must be positive.")
    async with Database().session() as session:
        result = await session.execute(
            CartItems.__table__.delete().where(
                CartItems.user_id == int(request.state.api_user_id),
                CartItems.item_id == product_id,
            )
        )
    if not result.rowcount:
        raise ApiError(404, "cart_item_not_found", "The cart item was not found.")
    return json_response({"deleted": True})


async def clear_user_cart(request: Request) -> JSONResponse:
    await clear_cart(int(request.state.api_user_id))
    return json_response({"cleared": True})


async def _purchase_route(request: Request, *, checkout_cart: bool) -> JSONResponse:
    body = await read_json_object(request)
    operation = "cart.checkout" if checkout_cart else "order.purchase"
    fields = {"expected_total"} if checkout_cart else {
        "product_id", "quantity", "promo_code", "expected_total",
    }
    reject_unknown_fields(body, fields)
    raw_total = body.get("expected_total")
    if not isinstance(raw_total, str):
        raise ApiError(400, "invalid_amount", "expected_total must be a decimal string.")
    expected_total = _decimal(raw_total, "expected_total", positive=True)
    product = None
    quantity = None
    if not checkout_cart:
        product_id = body.get("product_id")
        quantity = body.get("quantity", 1)
        if (
            isinstance(product_id, bool) or not isinstance(product_id, int) or product_id <= 0
            or isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_PURCHASE_QUANTITY
        ):
            raise ApiError(400, "invalid_order", "product_id and quantity are invalid.")
        promo_value = body.get("promo_code")
        if promo_value is not None and not isinstance(promo_value, str):
            raise ApiError(400, "invalid_promo", "promo_code must be a string.")
        if (promo_value or "").strip():
            raise ApiError(400, "discounts_disabled", "Purchase discount codes are disabled.")
    row_id, replay = await begin_idempotency(request, operation, body)
    if replay is not None:
        return json_response({"data": await _purchase_receipt(int(request.state.api_user_id), replay)})

    if not checkout_cart:
        try:
            product = await _active_product(product_id)
        except ApiError as error:
            await finish_idempotency(row_id, status="failed", result={
                "status_code": error.status_code,
                "code": error.code,
                "message": error.message,
            })
            raise

    if checkout_cart:
        success, code, result = await checkout_cart_transaction(
            int(request.state.api_user_id),
            expected_total=expected_total,
            api_idempotency_id=row_id,
        )
    else:
        success, code, result = await buy_item_transaction(
            int(request.state.api_user_id),
            product.name,
            quantity=quantity,
            expected_total=expected_total,
            api_idempotency_id=row_id,
        )

    if not success:
        error = _purchase_error(code)
        await finish_idempotency(row_id, status="failed", result={
            "status_code": error.status_code,
            "code": error.code,
            "message": error.message,
        })
        raise error
    if code == "idempotent_replay":
        saved = result or {}
    elif checkout_cart:
        saved = {
            "purchase_ids": [int(row["bought_id"]) for row in result],
            "total": _money(sum((Decimal(str(row["price"])) for row in result), Decimal(0))),
            "balance": "0.00",  # Replaced with the committed account value below.
            "quantity": len(result),
        }
    else:
        saved = {
            "purchase_ids": [int(row["bought_id"]) for row in result["items"]],
            "total": _money(result["total_price"]),
            "balance": _money(result["new_balance"]),
            "quantity": int(result["quantity"]),
        }
    if checkout_cart and code != "idempotent_replay":
        async with Database().session() as session:
            user = (await session.execute(
                select(User.balance).where(User.telegram_id == int(request.state.api_user_id))
            )).scalar_one()
        saved["balance"] = _money(user)
    if code != "idempotent_replay" and checkout_cart:
        await finish_idempotency(row_id, status="completed", result=saved)
    return json_response(
        {"data": await _purchase_receipt(int(request.state.api_user_id), saved)},
        status_code=201 if (not checkout_cart and code != "idempotent_replay") else 200,
    )


async def create_order(request: Request) -> JSONResponse:
    return await _purchase_route(request, checkout_cart=False)


async def checkout(request: Request) -> JSONResponse:
    return await _purchase_route(request, checkout_cart=True)


async def list_reviews(request: Request) -> JSONResponse:
    if str(EnvKeys.REVIEWS_ENABLED) != "1":
        raise ApiError(404, "reviews_disabled", "Product reviews are unavailable.")
    product_id = int(request.path_params["product_id"])
    product = await _active_product(product_id)
    limit, offset = parse_pagination(request)
    async with Database().session() as session:
        total, average = (await session.execute(
            select(func.count(Reviews.id), func.avg(Reviews.rating)).where(Reviews.item_id == product_id)
        )).one()
        rows = (await session.execute(
            select(Reviews).where(Reviews.item_id == product_id)
            .order_by(Reviews.created_at.desc(), Reviews.id.desc())
            .limit(limit + 1).offset(offset)
        )).scalars().all()
    return json_response({
        "product_id": product_id,
        "product_name": product.name,
        "average_rating": round(float(average), 2) if average is not None else None,
        "review_count": int(total or 0),
        "data": [
            {"id": int(row.id), "rating": int(row.rating), "text": row.text, "created_at": moscow_isoformat(row.created_at)}
            for row in rows[:limit]
        ],
        "pagination": {"limit": limit, "offset": offset, "has_more": len(rows) > limit},
    })


async def create_review(request: Request) -> JSONResponse:
    if str(EnvKeys.REVIEWS_ENABLED) != "1":
        raise ApiError(404, "reviews_disabled", "Product reviews are unavailable.")
    body = await read_json_object(request)
    reject_unknown_fields(body, {"rating", "text"})
    product = await _active_product(int(request.path_params["product_id"]))
    rating = body.get("rating")
    text = body.get("text")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ApiError(400, "invalid_rating", "rating must be an integer from 1 to 5.")
    if text is not None and (not isinstance(text, str) or len(text) > 2000):
        raise ApiError(400, "invalid_review_text", "text must be a string of at most 2000 characters.")
    user_id = int(request.state.api_user_id)
    async with Database().session() as session:
        purchased = await session.scalar(select(func.count(BoughtGoods.id)).where(
            BoughtGoods.buyer_id == user_id,
            BoughtGoods.item_name == product.name,
        ))
        if not purchased:
            raise ApiError(403, "purchase_required", "Only a verified buyer can review this product.")
        review = Reviews(user_id=user_id, item_id=product.id, rating=rating, text=(text or "").strip() or None)
        session.add(review)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise ApiError(409, "review_exists", "A review for this product already exists on this account.") from exc
        result = {"id": int(review.id), "rating": int(review.rating), "text": review.text, "created_at": moscow_isoformat(review.created_at)}
    return json_response({"data": result}, status_code=201)


async def stock_alert(request: Request) -> JSONResponse:
    product = await _active_product(int(request.path_params["product_id"]))
    success, code = await subscribe_to_stock(int(request.state.api_user_id), product.name)
    if not success:
        raise ApiError(404, "product_not_found", "The product was not found.")
    return json_response({"subscribed": True, "already_subscribed": code == "already_subscribed"}, status_code=201)


def routes():
    return [
        api_route("/v1/orders/quote", quote_order, methods=["POST"], write=True, name="api_order_quote"),
        api_route("/v1/orders", create_order, methods=["POST"], write=True, name="api_order_create"),
        api_route("/v1/cart", get_cart, name="api_cart"),
        api_route("/v1/cart", clear_user_cart, methods=["DELETE"], write=True, name="api_cart_clear"),
        api_route("/v1/cart/items/{product_id:int}", set_cart_item, methods=["PUT"], write=True, name="api_cart_set"),
        api_route("/v1/cart/items/{product_id:int}", delete_cart_item, methods=["DELETE"], write=True, name="api_cart_delete"),
        api_route("/v1/cart/checkout", checkout, methods=["POST"], write=True, name="api_cart_checkout"),
        api_route("/v1/products/{product_id:int}/reviews", list_reviews, name="api_reviews"),
        api_route("/v1/products/{product_id:int}/reviews", create_review, methods=["POST"], write=True, name="api_review_create"),
        api_route("/v1/products/{product_id:int}/stock-alert", stock_alert, methods=["POST"], write=True, name="api_stock_alert"),
    ]

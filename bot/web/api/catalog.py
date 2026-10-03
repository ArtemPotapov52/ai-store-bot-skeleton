"""Read-only catalog routes; stock credentials are never selected for display."""

from __future__ import annotations

from sqlalchemy import case, func, select
from starlette.requests import Request
from starlette.responses import JSONResponse

from bot.database import Database
from bot.database.models import Categories, Goods, ItemValues
from bot.database.methods.pricing import effective_price, purchase_quantity_limits
from bot.misc import EnvKeys
from bot.web.api.common import ApiError, api_route, json_response, parse_pagination


def _product_json(goods: Goods, category_name: str, quantity: int, is_infinite: bool) -> dict:
    price, _on_sale, _original_price = effective_price(goods)
    min_quantity, max_quantity = purchase_quantity_limits(goods)
    available = bool(is_infinite or quantity >= min_quantity)
    return {
        "id": int(goods.id),
        "name": str(goods.name),
        "category_id": int(goods.category_id),
        "category": category_name,
        "description": str(goods.description or ""),
        "price": format(price, ".2f"),
        "is_variable_pricing": bool(goods.is_variable_pricing),
        "min_quantity": min_quantity if goods.is_variable_pricing else None,
        "max_quantity": max_quantity if goods.is_variable_pricing else None,
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
        "available_quantity": None if is_infinite else max(0, int(quantity)),
        "unlimited": bool(is_infinite),
        "available": available,
        "availability_note": goods.availability_note,
    }


def _stock_expressions():
    finite = func.coalesce(
        func.sum(case((ItemValues.is_infinity.is_(False), 1), else_=0)), 0
    )
    infinite = func.coalesce(
        func.max(case((ItemValues.is_infinity.is_(True), 1), else_=0)), 0
    )
    return finite, infinite


async def list_categories(request: Request) -> JSONResponse:
    async with Database().session() as session:
        rows = (await session.execute(
            select(
                Categories.id,
                Categories.name,
                Categories.sort_order,
                func.count(Goods.id).label("product_count"),
            )
            .outerjoin(
                Goods,
                (Goods.category_id == Categories.id) & Goods.is_active.is_(True),
            )
            .where(Categories.is_active.is_(True))
            .group_by(Categories.id)
            .order_by(Categories.sort_order, Categories.id)
        )).all()
    return json_response({
        "data": [
            {
                "id": int(row.id),
                "name": str(row.name),
                "product_count": int(row.product_count or 0),
            }
            for row in rows
        ]
    })


async def _product_rows(request: Request, *, product_id: int | None = None):
    finite, infinite = _stock_expressions()
    query = (
        select(Goods, Categories.name.label("category_name"), finite.label("finite_count"), infinite.label("has_infinite"))
        .join(Categories, Categories.id == Goods.category_id)
        .outerjoin(ItemValues, ItemValues.item_id == Goods.id)
        .where(Goods.is_active.is_(True), Categories.is_active.is_(True))
    )

    if product_id is not None:
        query = query.where(Goods.id == product_id)
    else:
        raw_category = request.query_params.get("category_id")
        if raw_category is not None:
            try:
                category_id = int(raw_category)
            except ValueError as exc:
                raise ApiError(400, "invalid_category_id", "category_id must be a positive integer.") from exc
            if category_id <= 0:
                raise ApiError(400, "invalid_category_id", "category_id must be a positive integer.")
            query = query.where(Goods.category_id == category_id)

        search = request.query_params.get("search", "").strip()
        if len(search) > 100:
            raise ApiError(400, "search_too_long", "search must be at most 100 characters.")
        if search:
            query = query.where(Goods.name.ilike(f"%{search}%"))

    query = query.group_by(Goods.id, Categories.name)
    if product_id is None:
        limit, offset = parse_pagination(request)
        query = query.order_by(Goods.sort_order, Goods.id).limit(limit + 1).offset(offset)
    else:
        limit, offset = 1, 0
        query = query.limit(1)

    async with Database().session() as session:
        rows = (await session.execute(query)).all()
    return rows, limit, offset


async def list_products(request: Request) -> JSONResponse:
    rows, limit, offset = await _product_rows(request)
    has_more = len(rows) > limit
    rows = rows[:limit]
    data = [
        _product_json(
            row[0],
            row.category_name,
            int(row.finite_count or 0) + int(row[0].stock_quantity or 0),
            bool(row.has_infinite),
        )
        for row in rows
    ]
    return json_response({
        "data": data,
        "pagination": {"limit": limit, "offset": offset, "has_more": has_more},
    })


async def get_product(request: Request) -> JSONResponse:
    try:
        product_id = int(request.path_params["product_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_product_id", "product_id must be a positive integer.") from exc
    if product_id <= 0:
        raise ApiError(400, "invalid_product_id", "product_id must be a positive integer.")

    rows, _limit, _offset = await _product_rows(request, product_id=product_id)
    if not rows:
        raise ApiError(404, "product_not_found", "The product was not found.")
    row = rows[0]
    data = _product_json(
        row[0],
        row.category_name,
        int(row.finite_count or 0) + int(row[0].stock_quantity or 0),
        bool(row.has_infinite),
    )
    return json_response({"data": data})


def routes():
    return [
        api_route("/v1/categories", list_categories, name="api_categories"),
        api_route("/v1/products", list_products, name="api_products"),
        api_route("/v1/products/{product_id:int}", get_product, name="api_product"),
    ]

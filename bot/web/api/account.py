"""Account-scoped balance, receipt, referral, and shop-information routes."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import desc, select
from starlette.requests import Request
from starlette.responses import JSONResponse

from bot.database import Database
from bot.database.methods.lazy_queries import (
    query_user_bought_items,
    query_user_operations_history,
)
from bot.database.methods.read import check_user_referrals, get_referral_earnings_stats
from bot.database.models import BoughtGoods, ReferralEarnings, User
from bot.misc import EnvKeys
from bot.misc.timezone import moscow_isoformat
from bot.web.api.common import ApiError, api_route, json_response, parse_pagination


def _money(value) -> str:
    return format(Decimal(str(value or 0)), ".2f")


async def get_balance(request: Request) -> JSONResponse:
    user_id = int(request.state.api_user_id)
    async with Database().session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == user_id)
        )).scalar_one_or_none()
    if user is None or user.is_blocked:
        raise ApiError(403, "account_unavailable", "This account cannot use the API.")
    return json_response({
        "balance": _money(user.balance),
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
    })


async def get_operations(request: Request) -> JSONResponse:
    user_id = int(request.state.api_user_id)
    limit, offset = parse_pagination(request)
    rows = await query_user_operations_history(user_id, offset=offset, limit=limit + 1)
    has_more = len(rows) > limit
    return json_response({
        "data": [
            {
                "id": int(row["id"]),
                "type": str(row["type"]),
                "amount": _money(row["amount"]),
                "currency": str(EnvKeys.PAY_CURRENCY).upper(),
                "created_at": moscow_isoformat(row["date"]),
            }
            for row in rows[:limit]
        ],
        "pagination": {"limit": limit, "offset": offset, "has_more": has_more},
    })


async def get_purchases(request: Request) -> JSONResponse:
    user_id = int(request.state.api_user_id)
    limit, offset = parse_pagination(request)
    rows = await query_user_bought_items(user_id, offset=offset, limit=limit + 1)
    has_more = len(rows) > limit
    return json_response({
        "data": [
            {
                "id": int(row.id),
                "product_name": str(row.item_name),
                "price": _money(row.price),
                "currency": str(EnvKeys.PAY_CURRENCY).upper(),
                "purchased_at": moscow_isoformat(row.bought_datetime),
            }
            for row in rows[:limit]
        ],
        "pagination": {"limit": limit, "offset": offset, "has_more": has_more},
    })


async def get_purchase(request: Request) -> JSONResponse:
    try:
        purchase_id = int(request.path_params["purchase_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_purchase_id", "purchase_id must be a positive integer.") from exc
    if purchase_id <= 0:
        raise ApiError(400, "invalid_purchase_id", "purchase_id must be a positive integer.")

    async with Database().session() as session:
        purchase = (await session.execute(
            select(BoughtGoods).where(
                BoughtGoods.id == purchase_id,
                BoughtGoods.buyer_id == int(request.state.api_user_id),
            )
        )).scalar_one_or_none()
    if purchase is None:
        # Return the same result for absent and foreign receipts.
        raise ApiError(404, "purchase_not_found", "The purchase was not found.")
    return json_response({
        "data": {
            "id": int(purchase.id),
            "product_name": str(purchase.item_name),
            "delivery": str(purchase.value),
            "price": _money(purchase.price),
            "currency": str(EnvKeys.PAY_CURRENCY).upper(),
            "purchased_at": moscow_isoformat(purchase.bought_datetime),
        }
    })


async def get_referrals(request: Request) -> JSONResponse:
    user_id = int(request.state.api_user_id)
    count, stats = await asyncio.gather(
        check_user_referrals(user_id),
        get_referral_earnings_stats(user_id),
    )
    return json_response({
        "referral_count": int(count),
        "referral_percent": int(EnvKeys.REFERRAL_PERCENT),
        "earnings_total": _money(stats["total_amount"]),
        "earnings_count": int(stats["total_earnings_count"]),
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
        "referral_code": str(user_id),
    })


async def get_referral_earnings(request: Request) -> JSONResponse:
    user_id = int(request.state.api_user_id)
    limit, offset = parse_pagination(request)
    async with Database().session() as session:
        rows = (await session.execute(
            select(ReferralEarnings)
            .where(ReferralEarnings.referrer_id == user_id)
            .order_by(desc(ReferralEarnings.created_at), desc(ReferralEarnings.id))
            .limit(limit + 1)
            .offset(offset)
        )).scalars().all()
    has_more = len(rows) > limit
    return json_response({
        "data": [
            {
                "id": int(row.id),
                "amount": _money(row.amount),
                "original_top_up": _money(row.original_amount),
                "currency": str(EnvKeys.PAY_CURRENCY).upper(),
                "created_at": moscow_isoformat(row.created_at),
            }
            for row in rows[:limit]
        ],
        "pagination": {"limit": limit, "offset": offset, "has_more": has_more},
    })


async def get_info(request: Request) -> JSONResponse:
    return json_response({
        "shop_name": str(EnvKeys.SHOP_NAME or "My Store"),
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
        "faq": str(EnvKeys.FAQ or ""),
        "rules": str(EnvKeys.RULES or ""),
        "agreement": str(EnvKeys.AGREEMENT or ""),
        "legal_agreement_url": str(EnvKeys.LEGAL_AGREEMENT_URL or ""),
        "legal_privacy_url": str(EnvKeys.LEGAL_PRIVACY_URL or ""),
        "support_url": str(EnvKeys.SUPPORT_URL or ""),
        "support_username": str(EnvKeys.SUPPORT_USERNAME or ""),
    })


def routes():
    return [
        api_route("/v1/me/balance", get_balance, name="api_balance"),
        api_route("/v1/me/operations", get_operations, name="api_operations"),
        api_route("/v1/me/purchases", get_purchases, name="api_purchases"),
        api_route("/v1/me/purchases/{purchase_id:int}", get_purchase, name="api_purchase"),
        api_route("/v1/referrals", get_referrals, name="api_referrals"),
        api_route("/v1/referrals/earnings", get_referral_earnings, name="api_referral_earnings"),
        api_route("/v1/info", get_info, name="api_info"),
    ]

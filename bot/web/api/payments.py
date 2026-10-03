"""Partner top-ups through configured real providers and account-scoped status."""

from __future__ import annotations

import secrets
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import JSONResponse

from bot.database import Database
from bot.database.methods.create import (
    bind_pending_payment,
    create_pending_payment,
)
from bot.database.methods.delete import unsubscribe_from_stock
from bot.database.methods.transactions import process_payment_with_referral, redeem_balance_promo
from bot.database.models import ApiIdempotency, Goods, Payments, User
from bot.misc import EnvKeys
from bot.misc.timezone import moscow_isoformat
from bot.misc.services import CryptoPayAPI, XRocketPayAPI, send_fiat_invoice
from bot.misc.services.platega import (
    PlategaAPI,
    platega_event_from_status,
    process_platega_event,
)
from bot.web.api.common import (
    ApiError, api_route, json_response, read_json_object, reject_unknown_fields,
)
from bot.web.api.idempotency import begin_idempotency, finish_idempotency


def _providers(bot_available: bool) -> list[dict]:
    currency = str(EnvKeys.PAY_CURRENCY).upper()
    methods = []
    if currency == "RUB" and EnvKeys.PLATEGA_MERCHANT_ID and EnvKeys.PLATEGA_API_KEY:
        methods.append({"id": "platega", "name": "СБП / карта", "kind": "checkout_url", "currency": currency})
    if EnvKeys.CRYPTO_PAY_TOKEN:
        methods.append({"id": "cryptopay", "name": "Crypto Pay", "kind": "checkout_url", "currency": currency})
    if EnvKeys.XROCKET_PAY_TOKEN:
        methods.append({"id": "xrocket", "name": "xRocket", "kind": "checkout_url", "currency": currency})
    if bot_available and EnvKeys.TELEGRAM_PROVIDER_TOKEN:
        methods.append({"id": "telegram", "name": "Telegram Payments", "kind": "telegram_invoice", "currency": currency})
    return methods


def _validate_checkout_url(value, provider: str) -> str:
    url = str(value or "")
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    allowed = {
        "platega": host == "platega.io" or host.endswith(".platega.io"),
        "cryptopay": host in {"pay.crypt.bot", "t.me"} or host.endswith(".crypt.bot"),
        "xrocket": host == "t.me" or host == "xrocket.exchange" or host.endswith(".xrocket.exchange"),
    }
    if parsed.scheme != "https" or not host or not allowed.get(provider, False):
        raise ApiError(502, "invalid_provider_link", "The payment provider returned an unsafe checkout link.")
    return url


async def payment_methods(request: Request) -> JSONResponse:
    return json_response({
        "data": _providers(getattr(request.app.state, "bot", None) is not None),
        "minimum": int(EnvKeys.MIN_AMOUNT),
        "maximum": int(EnvKeys.MAX_AMOUNT),
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
    })


async def list_payments(request: Request) -> JSONResponse:
    from bot.web.api.common import parse_pagination

    limit, offset = parse_pagination(request)
    async with Database().session() as session:
        rows = (await session.execute(
            select(Payments)
            .where(Payments.user_id == int(request.state.api_user_id))
            .order_by(Payments.created_at.desc(), Payments.id.desc())
            .limit(limit + 1).offset(offset)
        )).scalars().all()
    return json_response({
        "data": [
            {
                "payment_id": int(row.id),
                "provider": str(row.provider),
                "status": str(row.status).lower(),
                "amount": f"{Decimal(str(row.amount)):.2f}",
                "currency": str(row.currency).upper(),
                "created_at": moscow_isoformat(row.created_at),
            }
            for row in rows[:limit]
        ],
        "pagination": {"limit": limit, "offset": offset, "has_more": len(rows) > limit},
    })


async def create_top_up(request: Request) -> JSONResponse:
    body = await read_json_object(request)
    reject_unknown_fields(body, {"provider", "amount"})
    raw_provider = body.get("provider")
    if not isinstance(raw_provider, str):
        raise ApiError(400, "invalid_provider", "provider must be a string.")
    provider = raw_provider.strip().lower()
    amount = body.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ApiError(400, "invalid_amount", "amount must be a whole number in the shop currency.")
    if not int(EnvKeys.MIN_AMOUNT) <= amount <= int(EnvKeys.MAX_AMOUNT):
        raise ApiError(400, "invalid_amount", f"amount must be between {EnvKeys.MIN_AMOUNT} and {EnvKeys.MAX_AMOUNT} {EnvKeys.PAY_CURRENCY}.")
    available = {method["id"] for method in _providers(getattr(request.app.state, "bot", None) is not None)}
    if provider not in available:
        raise ApiError(400, "payment_method_unavailable", "This real payment method is not currently configured.")

    operation = "balance.topup"
    row_id, replay = await begin_idempotency(request, operation, body)
    if replay is not None:
        return json_response({"data": replay})

    user_id = int(request.state.api_user_id)
    currency = str(EnvKeys.PAY_CURRENCY).upper()
    result = {
        "provider": provider,
        "amount": f"{amount:.2f}",
        "currency": currency,
        "checkout_url": None,
        "sent_to_telegram": False,
        "payment_id": None,
    }
    api_bot = getattr(request.app.state, "bot", None)

    if provider in {"platega", "cryptopay", "xrocket"}:
        intent_id = f"intent:api:{secrets.token_urlsafe(24)}"
        await create_pending_payment(provider, intent_id, user_id, amount, currency)
        # Persist the provider intent alongside the processing state for safe
        # diagnostics; a repeated request still cannot create a second invoice.
        async with Database().session() as session:
            idem = (await session.execute(
                select(ApiIdempotency).where(ApiIdempotency.id == row_id).with_for_update()
            )).scalar_one_or_none()
            if idem is not None and idem.status == "processing":
                idem.result_json = {"provider": provider, "intent_id": intent_id, "state": "creating"}

        try:
            if provider == "platega":
                telegram_username = None
                if api_bot is not None:
                    me = await api_bot.me()
                    telegram_username = getattr(me, "username", None)
                return_url = f"https://t.me/{telegram_username}" if telegram_username else "https://api.example.com/docs"
                invoice = await PlategaAPI().create_sbp_transaction(
                    amount=Decimal(amount), currency=currency, intent_id=intent_id,
                    user_id=user_id, user_name=str(user_id),
                    return_url=return_url, failed_url=return_url,
                )
                external_id = str(invoice["transactionId"])
                url = _validate_checkout_url(invoice.get("redirect"), provider)
            elif provider == "cryptopay":
                invoice = await CryptoPayAPI().create_invoice(
                    amount=amount, expires_in=int(EnvKeys.PAYMENT_TIME), currency=currency,
                    accepted_assets="TON,USDT,BTC,ETH", payload=intent_id,
                    description=f"{EnvKeys.SHOP_NAME}: balance top-up {amount} {currency}",
                )
                external_id = str(invoice.get("invoice_id") or "")
                url = _validate_checkout_url(
                    invoice.get("mini_app_invoice_url") or invoice.get("bot_invoice_url") or invoice.get("pay_url"),
                    provider,
                )
                if not external_id:
                    raise ApiError(502, "provider_invoice_invalid", "The payment provider returned an invalid invoice.")
            else:
                invoice = await XRocketPayAPI().create_invoice(
                    amount=amount, expires_in=int(EnvKeys.PAYMENT_TIME), currency=currency,
                    description=f"{EnvKeys.SHOP_NAME}: +{amount} {currency}",
                    client_invoice_id=intent_id, telegram_id=user_id,
                )
                external_id = str(invoice.get("id") or "")
                links = invoice.get("links") or {}
                url = _validate_checkout_url(
                    links.get("telegramBotLink") or links.get("telegramMiniAppLink") or links.get("webLink"),
                    provider,
                )
                if not external_id:
                    raise ApiError(502, "provider_invoice_invalid", "The payment provider returned an invalid invoice.")

            if not await bind_pending_payment(provider, intent_id, external_id):
                raise RuntimeError("payment intent could not be bound")
            async with Database().session() as session:
                payment = (await session.execute(
                    select(Payments).where(
                        Payments.provider == provider,
                        Payments.external_id == external_id,
                        Payments.user_id == user_id,
                    )
                )).scalar_one_or_none()
                if payment is None:
                    raise RuntimeError("bound payment not found")
                result.update(payment_id=int(payment.id), checkout_url=url)
        except ApiError:
            # Provider response validation failed after the external invoice may
            # have been created. Leave the durable idempotency state in progress.
            raise
        except Exception as exc:
            # A timeout can occur after the provider accepted the request. Keep the
            # operation closed to duplicate invoice creation; never rotate to a new
            # idempotency key automatically.
            from bot.logger_mesh import logger
            logger.warning("Partner top-up creation indeterminate for key row %s (%s)", row_id, provider)
            raise ApiError(503, "payment_creation_indeterminate", "The provider result is uncertain. Retry only with the same Idempotency-Key; do not create a second top-up.") from exc

    else:  # telegram card invoice
        if api_bot is None:
            raise ApiError(503, "telegram_unavailable", "Telegram invoice delivery is unavailable.")
        try:
            await send_fiat_invoice(bot=api_bot, chat_id=user_id, amount=amount)
        except Exception as exc:
            from bot.logger_mesh import logger
            logger.warning("Partner fiat invoice delivery failed for account %s", user_id)
            raise ApiError(503, "invoice_delivery_uncertain", "Telegram invoice delivery could not be confirmed; retry only with the same Idempotency-Key.") from exc
        result["sent_to_telegram"] = True

    await finish_idempotency(row_id, status="completed", result=result)
    return json_response({"data": result}, status_code=201)


async def redeem_balance_code(request: Request) -> JSONResponse:
    body = await read_json_object(request)
    reject_unknown_fields(body, {"code"})
    raw_code = body.get("code")
    if not isinstance(raw_code, str):
        raise ApiError(400, "invalid_promo", "code must be a string.")
    code = raw_code.strip().upper()
    if not code or len(code) > 50:
        raise ApiError(400, "invalid_promo", "code must contain 1–50 characters.")
    row_id, replay = await begin_idempotency(request, "balance.promo.redeem", {"code": code})
    if replay is not None:
        return json_response({"data": replay})
    user_id = int(request.state.api_user_id)
    success, error_code, amount = await redeem_balance_promo(
        code, user_id, api_idempotency_id=row_id,
    )
    if not success:
        known = {
            "promo.not_found": "promo_not_found",
            "promo.expired": "promo_expired",
            "promo.max_uses_reached": "promo_unavailable",
            "promo.already_used": "promo_already_used",
            "promo.not_balance_type": "promo_wrong_type",
        }
        code_out = known.get(error_code, "promo_redeem_failed")
        message = "The balance promo could not be redeemed."
        await finish_idempotency(row_id, status="failed", result={
            "status_code": 400,
            "code": code_out,
            "message": message,
        })
        raise ApiError(400, code_out, message)
    async with Database().session() as session:
        balance = await session.scalar(select(User.balance).where(User.telegram_id == user_id))
        idem = await session.scalar(select(ApiIdempotency.result_json).where(ApiIdempotency.id == row_id))
    result = {
        "amount_added": f"{Decimal(str(amount or 0)):.2f}",
        "balance": f"{Decimal(str(balance or 0)):.2f}",
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
    }
    # The transaction stored balance and amount together with its credit.
    if isinstance(idem, dict):
        result.update(idem)
    return json_response({"data": result}, status_code=200 if error_code == "idempotent_replay" else 201)


async def get_payment_status(request: Request) -> JSONResponse:
    try:
        payment_id = int(request.path_params["payment_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_payment_id", "payment_id must be a positive integer.") from exc
    if payment_id <= 0:
        raise ApiError(400, "invalid_payment_id", "payment_id must be positive.")
    user_id = int(request.state.api_user_id)
    async with Database().session() as session:
        payment = (await session.execute(select(Payments).where(
            Payments.id == payment_id, Payments.user_id == user_id,
        ))).scalar_one_or_none()
        if payment is None:
            raise ApiError(404, "payment_not_found", "The payment was not found.")
        provider = str(payment.provider)
        external_id = str(payment.external_id)
        amount = Decimal(str(payment.amount))
        currency = str(payment.currency).upper()
        status = str(payment.status).lower()

    if status == "pending" and provider in {"platega", "cryptopay", "xrocket"}:
        try:
            if provider == "platega":
                invoice = await PlategaAPI().get_transaction(external_id)
                event = platega_event_from_status(invoice, fallback_payload=external_id)
                processed = await process_platega_event(event)
                status = "succeeded" if processed.outcome in {"credited", "duplicate"} else processed.outcome
            elif provider == "cryptopay":
                invoice = await CryptoPayAPI().get_invoice(external_id)
                if str(invoice.get("status", "")).lower() == "paid":
                    paid_amount = Decimal(str(invoice.get("amount") or 0)).quantize(Decimal("0.01"))
                    paid_currency = str(invoice.get("fiat") or invoice.get("currency") or currency).upper()
                    ok, error = await process_payment_with_referral(
                        user_id=user_id, amount=paid_amount, provider=provider,
                        external_id=external_id, referral_percent=EnvKeys.REFERRAL_PERCENT,
                        currency=paid_currency,
                    )
                    if ok or error == "already_processed":
                        status = "succeeded"
                elif str(invoice.get("status", "")).lower() not in {"active", "paid"}:
                    status = "failed"
            else:
                invoice = await XRocketPayAPI().get_invoice(external_id)
                if str(invoice.get("status", "")).lower() == "paid":
                    ok, error = await process_payment_with_referral(
                        user_id=user_id, amount=amount, provider=provider,
                        external_id=external_id, referral_percent=EnvKeys.REFERRAL_PERCENT,
                        currency=currency,
                    )
                    if ok or error == "already_processed":
                        status = "succeeded"
                elif str(invoice.get("status", "")).lower() not in {"active", "partially_paid", "pending"}:
                    status = "failed"
        except Exception as exc:
            from bot.logger_mesh import logger
            logger.warning("Partner payment status refresh failed for payment row %s", payment_id)
            raise ApiError(503, "payment_status_unavailable", "The provider status could not be checked; retry shortly.") from exc

    async with Database().session() as session:
        fresh_status = await session.scalar(select(Payments.status).where(Payments.id == payment_id))
        balance = await session.scalar(select(User.balance).where(User.telegram_id == user_id))
    if status == "succeeded" and fresh_status:
        status = str(fresh_status).lower()
    return json_response({
        "data": {
            "payment_id": payment_id,
            "provider": provider,
            "status": status,
            "amount": f"{amount:.2f}",
            "currency": currency,
            "balance": f"{Decimal(str(balance or 0)):.2f}",
        }
    })


async def remove_stock_alert(request: Request) -> JSONResponse:
    try:
        product_id = int(request.path_params["product_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_product_id", "product_id must be a positive integer.") from exc
    async with Database().session() as session:
        product = (await session.execute(select(Goods).where(
            Goods.id == product_id, Goods.is_active.is_(True),
        ))).scalar_one_or_none()
        if product is None:
            raise ApiError(404, "product_not_found", "The product was not found.")
        name = str(product.name)
    removed = await unsubscribe_from_stock(int(request.state.api_user_id), name)
    return json_response({"subscribed": False, "removed": bool(removed)})


def routes():
    return [
        api_route("/v1/balance/payment-methods", payment_methods, name="api_payment_methods"),
        api_route("/v1/balance/payments", list_payments, name="api_payments"),
        api_route("/v1/balance/promos/redeem", redeem_balance_code, methods=["POST"], write=True, name="api_balance_promo"),
        api_route("/v1/balance/top-ups", create_top_up, methods=["POST"], write=True, name="api_top_up"),
        api_route("/v1/balance/payments/{payment_id:int}", get_payment_status, name="api_payment_status"),
        api_route("/v1/products/{product_id:int}/stock-alert", remove_stock_alert, methods=["DELETE"], write=True, name="api_stock_alert_remove"),
    ]

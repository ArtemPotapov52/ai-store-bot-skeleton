"""Public, credential-authenticated Platega callback endpoint."""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from bot.i18n import localize
from bot.keyboards import close
from bot.misc import EnvKeys
from bot.misc.services.platega import (
    PlategaCallbackError,
    process_platega_callback,
)

logger = logging.getLogger(__name__)
MAX_CALLBACK_BODY_BYTES = 16 * 1024


async def platega_callback_endpoint(request: Request) -> JSONResponse:
    """Verify and acknowledge Platega's transaction-status notification."""
    if request.method != "POST":
        return JSONResponse({"ok": False}, status_code=405)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse({"ok": False}, status_code=415)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_CALLBACK_BODY_BYTES:
                return JSONResponse({"ok": False}, status_code=413)
        except ValueError:
            return JSONResponse({"ok": False}, status_code=400)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_CALLBACK_BODY_BYTES:
            return JSONResponse({"ok": False}, status_code=413)
    try:
        event = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return JSONResponse({"ok": False}, status_code=400)
    if not isinstance(event, dict):
        return JSONResponse({"ok": False}, status_code=400)

    try:
        result = await process_platega_callback(request.headers, event)
    except PlategaCallbackError as exc:
        if exc.status_code >= 500:
            logger.error("Platega callback could not be processed: %s", exc)
        return JSONResponse({"ok": False}, status_code=exc.status_code)
    except Exception:
        logger.exception("Unexpected Platega callback processing failure")
        return JSONResponse({"ok": False}, status_code=500)

    bot = getattr(request.app.state, "bot", None)
    if result.credited and bot is not None:
        try:
            await bot.send_message(
                chat_id=result.user_id,
                text=localize(
                    "payments.topped_simple",
                    amount=result.amount,
                    currency=result.currency,
                ),
                reply_markup=close(),
            )
        except Exception:
            logger.warning(
                "Balance credited but Platega payment notification failed for user %s",
                result.user_id,
                exc_info=True,
            )
    elif result.outcome == "chargebacked" and bot is not None:
        try:
            await bot.send_message(
                chat_id=EnvKeys.OWNER_ID,
                text=localize(
                    "payments.platega.chargeback_alert",
                    id=result.transaction_id,
                    amount=result.amount,
                    currency=result.currency,
                    user_id=result.user_id,
                ),
                reply_markup=close(),
            )
        except Exception:
            logger.warning("Platega chargeback alert could not be delivered", exc_info=True)

    return JSONResponse({"ok": True}, status_code=200)

"""Platega.io SBP transaction client and authenticated payment callback flow."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp

from bot.database.methods import (
    bind_pending_payment,
    get_payment_record,
    log_audit,
    mark_payment_chargebacked,
    mark_pending_payment_failed,
    process_payment_with_referral,
    reopen_failed_payment,
)
from bot.misc import EnvKeys

logger = logging.getLogger(__name__)


class PlategaAPIError(RuntimeError):
    """Provider failure without including credentials or raw response bodies."""

    def __init__(self, status_code: int | None, message: str = "Platega request failed"):
        self.status_code = status_code
        super().__init__(message)


class PlategaCallbackError(RuntimeError):
    """A callback that is unauthenticated or does not match a stored intent."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class PlategaCallbackResult:
    outcome: str
    transaction_id: str
    user_id: int
    amount: Decimal
    currency: str

    @property
    def credited(self) -> bool:
        return self.outcome == "credited"


class PlategaAPI:
    """Small async client for Platega's documented JSON API."""

    base_url = "https://app.platega.io"
    timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_read=15)

    def __init__(self, merchant_id: str | None = None, api_key: str | None = None):
        self.merchant_id = str(
            EnvKeys.PLATEGA_MERCHANT_ID if merchant_id is None else merchant_id
        ).strip()
        self.api_key = str(
            EnvKeys.PLATEGA_API_KEY if api_key is None else api_key
        ).strip()
        if not self.merchant_id or not self.api_key:
            raise PlategaAPIError(None, "Platega is not configured")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
    ) -> dict:
        """Make one bounded HTTPS request; never follow provider redirects."""
        headers = {
            "X-MerchantId": self.merchant_id,
            "X-Secret": self.api_key,
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json_body,
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    try:
                        data = await response.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        data = None
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise PlategaAPIError(None, "Platega is temporarily unavailable") from exc

        if status < 200 or status >= 300:
            raise PlategaAPIError(status)
        if not isinstance(data, dict):
            raise PlategaAPIError(status, "Platega returned an invalid response")
        return data

    @staticmethod
    def _safe_return_url(value: str) -> str:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Platega return URLs must use HTTPS")
        return value

    async def create_sbp_transaction(
        self,
        *,
        amount: Decimal,
        currency: str,
        intent_id: str,
        user_id: int,
        user_name: str,
        return_url: str,
        failed_url: str,
    ) -> dict:
        """Create a method-2 (SBP QR) transaction and validate its pay link."""
        try:
            value = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Invalid payment amount") from exc
        currency = str(currency or "").strip().upper()
        if not value.is_finite() or value <= 0:
            raise ValueError("Payment amount must be positive")
        if currency != "RUB":
            raise ValueError("Platega SBP requires RUB")
        if not intent_id or len(intent_id) > 128:
            raise ValueError("Invalid payment intent")

        numeric_amount: int | float = (
            int(value) if value == value.to_integral_value() else float(value)
        )
        body = {
            "paymentMethod": 2,
            "paymentDetails": {"amount": numeric_amount, "currency": currency},
            "description": f"Пополнение баланса на {value:f} {currency}",
            "return": self._safe_return_url(return_url),
            "failedUrl": self._safe_return_url(failed_url),
            "payload": intent_id,
            "orderId": intent_id,
            "metadata": {
                "userId": str(int(user_id)),
                "userName": (str(user_name or "").strip() or str(int(user_id)))[:128],
            },
        }
        response = await self._request("POST", "/transaction/process", json_body=body)

        try:
            transaction_id = str(UUID(str(response.get("transactionId") or "")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise PlategaAPIError(502, "Platega returned an invalid transaction ID") from exc

        redirect = str(response.get("redirect") or "")
        parsed = urlsplit(redirect)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not host
            or not (host == "platega.io" or host.endswith(".platega.io"))
        ):
            raise ValueError("Platega returned an invalid payment URL")

        return {
            "transactionId": transaction_id,
            "redirect": redirect,
            "status": str(response.get("status") or "PENDING").upper(),
            "expiresIn": str(response.get("expiresIn") or ""),
        }

    async def get_transaction(self, transaction_id: str) -> dict:
        """Fetch an invoice by its UUID for the user's explicit status check."""
        try:
            normalized_id = str(UUID(str(transaction_id)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("Invalid Platega transaction ID") from exc
        data = await self._request("GET", f"/transaction/{normalized_id}")
        try:
            response_id = str(UUID(str(data.get("id") or "")))
        except (TypeError, ValueError, AttributeError) as exc:
            raise PlategaAPIError(502, "Platega returned an invalid transaction status") from exc
        if response_id != normalized_id:
            raise PlategaAPIError(502, "Platega returned a different transaction")
        return data


def _field(data: Mapping, *names: str):
    for name in names:
        if name in data:
            return data[name]
    lowered = {str(key).lower(): value for key, value in data.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _callback_amount(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PlategaCallbackError(400, "Invalid callback amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise PlategaCallbackError(400, "Invalid callback amount")
    return amount


def _normalize_sbp_method(value) -> str | None:
    """Accept Platega's numeric ID and documented/localized SBP labels only."""
    normalized = "".join(char for char in str(value or "").casefold() if char.isalnum())
    if normalized in {"2", "sbp", "sbpqr", "sbpqrcode", "сбп", "сбпqr", "сбпqrкод"}:
        return "SBPQR"
    return None


def _normalize_status(value) -> str:
    status = str(value or "").strip().upper()
    return "CHARGEBACKED" if status == "CHARGEBACK" else status


def platega_event_from_status(
    status_data: Mapping,
    *,
    fallback_payload: str | None = None,
) -> dict:
    """Normalize an authenticated Platega status response for balance credit.

    Platega's status response reports the payer's gross amount in
    ``paymentDetails.amount`` and the fee separately in ``comission``. Store
    and credit the net invoice amount, while retaining gross fields privately
    so the webhook can verify that its payload agrees with the status API.
    """
    if not isinstance(status_data, Mapping):
        raise PlategaCallbackError(502, "Invalid Platega transaction status")
    try:
        transaction_id = str(UUID(str(_field(status_data, "id", "transactionId"))))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PlategaCallbackError(502, "Invalid Platega transaction status") from exc

    details = _field(status_data, "paymentDetails")
    if not isinstance(details, Mapping):
        raise PlategaCallbackError(502, "Invalid Platega payment details")
    gross_amount = _callback_amount(_field(details, "amount"))
    raw_commission = _field(status_data, "comission", "commission")
    try:
        commission = Decimal("0") if raw_commission in (None, "") else Decimal(str(raw_commission))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PlategaCallbackError(502, "Invalid Platega commission") from exc
    if not commission.is_finite() or commission < 0 or commission >= gross_amount:
        raise PlategaCallbackError(502, "Invalid Platega commission")

    currency = str(_field(details, "currency") or "").strip().upper()
    if not currency or len(currency) > 8:
        raise PlategaCallbackError(502, "Invalid Platega payment currency")
    payment_method = _field(status_data, "paymentMethod")
    if _normalize_sbp_method(payment_method) is None:
        raise PlategaCallbackError(400, "Unexpected payment method")

    payload = _field(status_data, "payload") or fallback_payload or ""
    return {
        "id": transaction_id,
        "amount": gross_amount - commission,
        "currency": currency,
        "status": _normalize_status(_field(status_data, "status")),
        "paymentMethod": payment_method,
        "payload": str(payload),
        "_gross_amount": gross_amount,
        "_commission": commission,
    }


def _verify_callback_headers(headers: Mapping[str, str]) -> None:
    merchant_id = str(getattr(EnvKeys, "PLATEGA_MERCHANT_ID", "") or "").strip()
    api_key = str(getattr(EnvKeys, "PLATEGA_API_KEY", "") or "").strip()
    if not merchant_id or not api_key:
        raise PlategaCallbackError(503, "Platega callback is not configured")

    normalized = {str(key).lower(): str(value) for key, value in headers.items()}
    received_merchant = normalized.get("x-merchantid", "")
    received_secret = normalized.get("x-secret", "")
    if not (
        hmac.compare_digest(received_merchant.encode(), merchant_id.encode())
        and hmac.compare_digest(received_secret.encode(), api_key.encode())
    ):
        raise PlategaCallbackError(401, "Invalid Platega callback credentials")


async def process_platega_event(event: Mapping) -> PlategaCallbackResult:
    """Validate an authenticated provider event and apply its idempotent state."""
    try:
        transaction_id = str(UUID(str(_field(event, "id", "transactionId"))))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PlategaCallbackError(400, "Invalid transaction ID") from exc

    intent_id = str(_field(event, "payload", "orderId") or "").strip()
    if len(intent_id) > 128:
        raise PlategaCallbackError(400, "Invalid payment intent")
    amount = _callback_amount(_field(event, "amount"))
    currency = str(_field(event, "currency") or "").strip().upper()
    if not currency or len(currency) > 8:
        raise PlategaCallbackError(400, "Invalid payment currency")
    payment_method = _normalize_sbp_method(_field(event, "paymentMethod"))
    if payment_method is None:
        raise PlategaCallbackError(400, "Unexpected payment method")

    status = _normalize_status(_field(event, "status"))
    if status not in {"PENDING", "CONFIRMED", "CANCELED", "CHARGEBACK", "CHARGEBACKED"}:
        raise PlategaCallbackError(400, "Unknown payment status")

    payment = await get_payment_record("platega", transaction_id)
    if payment is None:
        # Status notifications normally identify only the provider transaction
        # ID. `payload`/`orderId` are optional here, despite being accepted on
        # transaction creation; use one only to recover an as-yet-unbound intent.
        if not intent_id:
            raise PlategaCallbackError(404, "Payment transaction not found")
        try:
            bound = await bind_pending_payment("platega", intent_id, transaction_id)
        except ValueError as exc:
            raise PlategaCallbackError(409, "Payment transaction is already linked") from exc
        if not bound:
            raise PlategaCallbackError(404, "Payment intent not found")
        payment = await get_payment_record("platega", transaction_id)
    if not payment or payment.get("user_id") is None:
        raise PlategaCallbackError(404, "Payment intent not found")
    if payment["amount"] != amount or payment["currency"] != currency:
        try:
            await log_audit(
                "platega_callback_mismatch",
                level="ERROR",
                user_id=payment["user_id"],
                resource_type="Payment",
                resource_id=transaction_id,
                details=(
                    f"stored={payment['amount']} {payment['currency']}; "
                    f"callback={amount} {currency}"
                ),
            )
        except Exception:
            logger.warning("Could not audit a mismatched Platega callback", exc_info=True)
        raise PlategaCallbackError(400, "Payment amount or currency mismatch")

    result = PlategaCallbackResult(
        outcome="pending",
        transaction_id=transaction_id,
        user_id=int(payment["user_id"]),
        amount=amount,
        currency=currency,
    )
    if status == "PENDING":
        return result

    if status == "CANCELED":
        if payment["status"] == "pending":
            await mark_pending_payment_failed("platega", transaction_id)
        return PlategaCallbackResult("canceled", transaction_id, result.user_id, amount, currency)

    if status in {"CHARGEBACK", "CHARGEBACKED"}:
        await mark_payment_chargebacked("platega", transaction_id)
        try:
            await log_audit(
                "platega_chargeback",
                level="WARNING",
                user_id=result.user_id,
                resource_type="Payment",
                resource_id=transaction_id,
                details=f"amount={amount} {currency}; manual balance review required",
            )
        except Exception:
            logger.warning("Could not audit a Platega chargeback", exc_info=True)
        return PlategaCallbackResult("chargebacked", transaction_id, result.user_id, amount, currency)

    if payment["status"] == "chargebacked":
        return PlategaCallbackResult("chargebacked", transaction_id, result.user_id, amount, currency)
    if payment["status"] == "failed":
        await reopen_failed_payment("platega", transaction_id)

    success, error = await process_payment_with_referral(
        user_id=result.user_id,
        amount=amount,
        provider="platega",
        external_id=transaction_id,
        referral_percent=int(getattr(EnvKeys, "REFERRAL_PERCENT", 0) or 0),
        currency=currency,
    )
    if success:
        try:
            await log_audit(
                "balance_replenish",
                user_id=result.user_id,
                resource_type="Payment",
                resource_id=transaction_id,
                details=f"provider=platega, amount={amount} {currency}",
            )
        except Exception:
            logger.warning("Could not audit a credited Platega transaction", exc_info=True)
        return PlategaCallbackResult("credited", transaction_id, result.user_id, amount, currency)
    if error == "already_processed":
        return PlategaCallbackResult("duplicate", transaction_id, result.user_id, amount, currency)
    if error == "payment_mismatch":
        raise PlategaCallbackError(409, "Payment is not eligible for credit")
    raise PlategaCallbackError(500, "Could not apply payment")


async def process_platega_callback(
    headers: Mapping[str, str],
    event: Mapping,
) -> PlategaCallbackResult:
    """Authenticate a callback, verify it against Platega, and process net value."""
    _verify_callback_headers(headers)
    try:
        callback_id = str(UUID(str(_field(event, "id", "transactionId"))))
    except (TypeError, ValueError, AttributeError) as exc:
        raise PlategaCallbackError(400, "Invalid transaction ID") from exc

    try:
        status_data = await PlategaAPI().get_transaction(callback_id)
    except (PlategaAPIError, ValueError) as exc:
        logger.warning(
            "Could not verify Platega callback with status API (http_status=%s)",
            getattr(exc, "status_code", None),
        )
        raise PlategaCallbackError(503, "Could not verify Platega transaction") from exc

    verified_event = platega_event_from_status(
        status_data,
        fallback_payload=str(_field(event, "payload") or ""),
    )
    callback_amount = _callback_amount(_field(event, "amount"))
    callback_currency = str(_field(event, "currency") or "").strip().upper()
    callback_status = _normalize_status(_field(event, "status"))
    callback_method = _field(event, "paymentMethod")
    if (
        callback_id != verified_event["id"]
        or callback_amount != verified_event["_gross_amount"]
        or callback_currency != verified_event["currency"]
        or callback_status != verified_event["status"]
        or (callback_method is not None and _normalize_sbp_method(callback_method) is None)
    ):
        raise PlategaCallbackError(400, "Callback does not match Platega transaction")

    return await process_platega_event(verified_event)

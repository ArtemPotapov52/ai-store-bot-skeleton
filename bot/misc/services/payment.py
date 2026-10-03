import aiohttp
import json
from decimal import Decimal, InvalidOperation
from typing import Optional

from aiogram import Bot
from aiogram.types import LabeledPrice

from bot.misc import EnvKeys
from bot.i18n import localize

# Currencies without minor units (no cents)
ZERO_DEC_CURRENCIES = {"JPY", "KRW"}


def _format_invoice_amount(amount: int | float | Decimal) -> str:
    """Serialize a fiat amount without binary-float artefacts.

    Crypto Pay accepts a decimal string.  Product and balance amounts are
    stored with two decimal places, so keeping the decimal representation here
    makes ``400`` go to the provider as exactly ``"400"`` instead of
    ``"400.0"`` or a long float expansion.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Amount must be a valid number") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("Amount must be greater than zero")
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _minor_units_for(currency: str) -> int:
    """
    Return multiplier to convert major units to minor units.
    """
    return 1 if currency.upper() in ZERO_DEC_CURRENCIES else 100


def payload_amount(payload: dict) -> int:
    """Read the requested top-up amount (major units) out of an invoice payload.

    Returns 0 when the payload carries no usable amount.
    """
    for key in ("amount", "amount_rub"):
        if key in payload:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                return 0
    return 0


async def send_fiat_invoice(
        *,
        bot: Bot,
        chat_id: int,
        amount: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
):
    """
    Send invoice via Telegram Payments (fiat provider).
    `amount` is given in major units (e.g., RUB, USD).
    """
    provider_token = EnvKeys.TELEGRAM_PROVIDER_TOKEN
    if not provider_token:
        raise RuntimeError("TELEGRAM_PROVIDER_TOKEN is not set")

    currency = (getattr(EnvKeys, "PAY_CURRENCY", None) or "RUB").upper()
    multiplier = _minor_units_for(currency)
    amount_minor = int(amount) * multiplier

    prices = [
        LabeledPrice(
            label=localize("payments.invoice.label.fiat", amount=int(amount), currency=currency),
            amount=amount_minor,
        )
    ]
    payload = json.dumps({"type": "balance_topup", "amount": int(amount)})

    await bot.send_invoice(
        chat_id=chat_id,
        title=title or localize("payments.invoice.title.topup"),
        description=description or localize("payments.invoice.desc.topup.fiat"),
        payload=payload,
        provider_token=provider_token,
        currency=currency,
        prices=prices,
        request_timeout=60,
    )


class CryptoPayAPIError(Exception):
    """Exception raised when CryptoPay API returns an error."""

    def __init__(self, code: int, name: str, message: str = None):
        self.code = code
        self.name = name
        self.message = message or name
        super().__init__(f"CryptoPay API Error [{code}]: {name}")


class CircuitBreaker:
    """Simple circuit breaker for external API calls."""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failure_count = 0
        self._last_failure_time: float = 0
        self._state = "closed"  # closed, open

    @property
    def is_open(self) -> bool:
        if self._state == "open":
            import time
            if time.time() - self._last_failure_time > self.recovery_timeout:
                self._state = "closed"
                self._failure_count = 0
                return False
            return True
        return False

    def record_success(self):
        self._failure_count = 0
        self._state = "closed"

    def record_failure(self):
        import time
        self._failure_count += 1
        self._last_failure_time = time.time()
        if self._failure_count >= self.failure_threshold:
            self._state = "open"


# Shared circuit breaker instance for CryptoPay API
_crypto_circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)


class CryptoPayAPI:
    """
    Minimal async client for Crypto Bot API used to create and fetch invoices.
    """

    _timeout = aiohttp.ClientTimeout(total=30)
    _session: Optional[aiohttp.ClientSession] = None

    def __init__(self):
        self.token = EnvKeys.CRYPTO_PAY_TOKEN
        self.base_url = "https://pay.crypt.bot/api"
        self.circuit_breaker = _crypto_circuit_breaker

    @classmethod
    def _get_session(cls) -> aiohttp.ClientSession:
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession(timeout=cls._timeout)
        return cls._session

    @classmethod
    async def close_session(cls):
        if cls._session and not cls._session.closed:
            await cls._session.close()
            cls._session = None

    async def _request(self, method: str, params: dict) -> dict:
        if self.circuit_breaker.is_open:
            raise CryptoPayAPIError(
                code=503,
                name="SERVICE_UNAVAILABLE",
                message="CryptoPay API temporarily unavailable, please try again later"
            )

        headers = {
            "Crypto-Pay-API-Token": self.token,
            "User-Agent": "MyStore/1.0",
        }
        url = f"{self.base_url}/{method}"
        session = self._get_session()

        try:
            if method.startswith("get"):
                async with session.get(url, params=params, headers=headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            else:
                async with session.post(url, json=params, headers=headers) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except CryptoPayAPIError:
            raise
        except Exception:
            self.circuit_breaker.record_failure()
            raise

        # Check for API-level errors (HTTP 200 but ok=false)
        if not data.get("ok", False):
            error = data.get("error", {})
            raise CryptoPayAPIError(
                code=error.get("code", 0),
                name=error.get("name", "UNKNOWN_ERROR")
            )

        self.circuit_breaker.record_success()
        return data

    async def create_invoice(
            self,
            amount: int | float | Decimal,
            expires_in: int,
            currency: Optional[str] = None,
            accepted_assets: str = "TON,USDT",
            payload: Optional[str] = None,
            description: Optional[str] = None,
            hidden_message: Optional[str] = None,
    ) -> dict:
        """
        Create a Crypto Pay invoice for given fiat amount/currency.
        """
        normalized_currency = str(
            currency or getattr(EnvKeys, "PAY_CURRENCY", None) or "RUB"
        ).strip().upper()
        if not normalized_currency:
            normalized_currency = "RUB"

        params = {
            "currency_type": "fiat",
            "fiat": normalized_currency,
            "amount": _format_invoice_amount(amount),
            "accepted_assets": accepted_assets,
            "expires_in": expires_in,
        }
        if payload:
            params["payload"] = payload
        if description:
            params["description"] = description
        if hidden_message:
            params["hidden_message"] = hidden_message

        response = await self._request("createInvoice", params)
        return response.get("result") or {}

    async def get_invoice(self, invoice_id: str) -> dict:
        """
        Fetch a single invoice by id.
        """
        params = {"invoice_ids": invoice_id}
        res = await self._request("getInvoices", params)
        items = res.get("result", {}).get("items")
        return items[0] if items else {}


class XRocketAPIError(Exception):
    """Exception raised when xRocket Pay API returns an error."""

    def __init__(self, status: int, problem_type: str = "", detail: str = ""):
        self.status = status
        self.problem_type = problem_type
        self.detail = detail or problem_type or f"HTTP {status}"
        super().__init__(f"xRocket API Error [{status}]: {self.detail}")


_xrocket_circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)

# Browsers-like UA: the Pay API edge (Cloudflare) rejects bare library UAs.
_XROCKET_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 MyStore/1.0"
)


class XRocketPayAPI:
    """Minimal async client for xRocket Pay API invoices.

    Docs: https://docs.xrocket.exchange/api/pay/pay-api-overview
    The Pay API does NOT convert fiat<->crypto (``priceCurrency`` must equal
    ``payCurrencies[0]``), so fiat shop amounts are converted to the payout
    asset via the public ``/api/v1/rates`` endpoint before invoice creation.
    """

    _timeout = aiohttp.ClientTimeout(total=30)
    _session: Optional[aiohttp.ClientSession] = None
    base_url = "https://pay.api.xrocket.exchange"

    def __init__(self):
        self.token = EnvKeys.XROCKET_PAY_TOKEN
        self.asset = (getattr(EnvKeys, "XROCKET_PAY_ASSET", None) or "USDT").upper()
        self.circuit_breaker = _xrocket_circuit_breaker

    @classmethod
    def _get_session(cls) -> aiohttp.ClientSession:
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession(timeout=cls._timeout)
        return cls._session

    @classmethod
    async def close_session(cls):
        if cls._session and not cls._session.closed:
            await cls._session.close()
            cls._session = None

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _XROCKET_USER_AGENT,
        }

    async def _api(self, method: str, path: str, body: dict | None = None, params: dict | None = None) -> dict:
        if self.circuit_breaker.is_open:
            raise XRocketAPIError(503, "service_unavailable", "xRocket API temporarily unavailable")
        session = self._get_session()
        try:
            async with session.request(
                method, f"{self.base_url}{path}",
                json=body, params=params, headers=self._headers(),
            ) as resp:
                if resp.status in (200, 201, 204):
                    self.circuit_breaker.record_success()
                    text = await resp.text()
                    if not text.strip():
                        return {}
                    try:
                        return json.loads(text)
                    except ValueError:
                        return {}
                try:
                    problem = await resp.json()
                except Exception:
                    problem = {}
                raise XRocketAPIError(
                    resp.status,
                    str(problem.get("type", "")),
                    str(problem.get("detail", "")),
                )
        except XRocketAPIError:
            raise
        except Exception:
            self.circuit_breaker.record_failure()
            raise

    async def get_rate_to_asset(self, fiat: str) -> Decimal:
        """How much 1 unit of ``fiat`` costs in the payout asset (e.g. USDT per 1 RUB)."""
        fiat = (fiat or "RUB").upper()
        if fiat == self.asset:
            return Decimal("1")
        data = await self._api("GET", "/api/v1/rates", params={"base": fiat})
        for row in data if isinstance(data, list) else []:
            if str(row.get("currency", "")).upper() == self.asset:
                # /rates returns fiat-per-asset (e.g. 84.5 RUB per USDT).
                per_asset = Decimal(str(row["rate"]))
                if per_asset > 0:
                    return (Decimal("1") / per_asset).quantize(Decimal("0.00000001"))
        raise XRocketAPIError(502, "rate_unavailable", f"No {fiat}->{self.asset} rate")

    async def fiat_to_asset_amount(self, amount: int | float | Decimal, fiat: str | None = None) -> str:
        """Convert a fiat shop amount to an asset amount string for the invoice."""
        rate = await self.get_rate_to_asset((fiat or getattr(EnvKeys, "PAY_CURRENCY", None) or "RUB"))
        value = (Decimal(str(amount)) * rate).quantize(Decimal("0.00000001"))
        if value <= 0:
            raise ValueError("Amount must be greater than zero")
        rendered = format(value.normalize(), "f")
        return rendered

    async def create_invoice(
        self,
        amount: int | float | Decimal,
        expires_in: int,
        currency: Optional[str] = None,
        description: Optional[str] = None,
        client_invoice_id: Optional[str] = None,
        telegram_id: int | None = None,
    ) -> dict:
        """Create a one-time invoice; returns the InvoiceDto dict (with ``links``)."""
        asset_amount = await self.fiat_to_asset_amount(amount, currency)
        body: dict = {
            "priceAmount": asset_amount,
            "priceCurrency": self.asset,
            "payCurrencies": [self.asset],
            "expiresIn": max(int(expires_in) * 1000, 60000),
        }
        if description:
            body["description"] = description[:1000]
        if client_invoice_id:
            body["clientInvoiceId"] = client_invoice_id[:100]
        if telegram_id:
            body["customer"] = {"telegramId": str(telegram_id)}
        return await self._api("POST", "/api/v1/invoices", body=body)

    async def get_invoice(self, invoice_id: str) -> dict:
        """Fetch invoice status by xRocket id."""
        return await self._api("GET", "/api/v1/invoice", params={"invoiceId": invoice_id})

    async def delete_invoice(self, invoice_id: str) -> bool:
        """Delete an invoice; True when it is gone."""
        try:
            await self._api("DELETE", "/api/v1/invoice", params={"invoiceId": invoice_id})
            return True
        except XRocketAPIError as e:
            if e.status == 404:
                return True
            raise

import logging
import os
from abc import ABC
from typing import Final
from urllib.parse import quote_plus, urlparse

_env_logger = logging.getLogger(__name__)

_DEFAULT_ADMIN_PASSWORD = "admin"
_DEFAULT_SECRET_KEY = "change-me-in-production"

# An empty host means "not bound anywhere reachable" as far as this check cares.
_LOOPBACK_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "[::1]"})

# Fiat codes supported by Crypto Pay invoices and commonly accepted by
# Telegram payment providers.  Keeping the list explicit prevents a typo in
# the admin panel from creating invoices that the provider cannot process.
PAY_CURRENCY_CHOICES = (
    ("RUB", "Российский рубль (RUB)"),
    ("USD", "Доллар США (USD)"),
    ("EUR", "Евро (EUR)"),
    ("GBP", "Фунт стерлингов (GBP)"),
    ("CNY", "Китайский юань (CNY)"),
    ("KZT", "Казахстанский тенге (KZT)"),
    ("UAH", "Украинская гривна (UAH)"),
    ("BYN", "Белорусский рубль (BYN)"),
)
PAY_CURRENCY_CODES = frozenset(code for code, _label in PAY_CURRENCY_CHOICES)


class EnvKeys(ABC):
    """Secure environment configuration with validation"""

    @staticmethod
    def _get_required(key: str) -> str:
        val = os.getenv(key)
        if not val:
            raise ValueError(f"Missing required environment variable: {key}")
        return val

    @staticmethod
    def _get_optional(key: str, default: str = "") -> str:
        return os.getenv(key, default)

    # Telegram
    TOKEN: Final = _get_required('TOKEN')
    OWNER_ID: Final = int(_get_required('OWNER_ID'))

    # Database
    POSTGRES_DB: Final = _get_required("POSTGRES_DB")
    POSTGRES_USER: Final = _get_required("POSTGRES_USER")
    POSTGRES_PASSWORD: Final = _get_required("POSTGRES_PASSWORD")
    DB_PORT: Final = int(_get_optional("DB_PORT", "5432"))
    POSTGRES_HOST: Final = _get_optional("POSTGRES_HOST", "localhost")
    DB_POOL_SIZE: Final = int(_get_optional("DB_POOL_SIZE", "10"))
    DB_MAX_OVERFLOW: Final = int(_get_optional("DB_MAX_OVERFLOW", "20"))

    # Redis
    REDIS_ENABLED: Final = _get_optional("REDIS_ENABLED", "1")
    REDIS_HOST: Final = _get_optional("REDIS_HOST", "localhost")
    REDIS_PORT: Final = int(_get_optional("REDIS_PORT", "6379"))
    REDIS_DB: Final = int(_get_optional("REDIS_DB", "0"))
    REDIS_PASSWORD: Final = _get_optional("REDIS_PASSWORD", "")

    # Payments
    TELEGRAM_PROVIDER_TOKEN: Final = _get_optional("TELEGRAM_PROVIDER_TOKEN", "")
    CRYPTO_PAY_TOKEN: Final = _get_optional("CRYPTO_PAY_TOKEN", "")
    PLATEGA_MERCHANT_ID: Final = _get_optional("PLATEGA_MERCHANT_ID", "")
    PLATEGA_API_KEY: Final = _get_optional("PLATEGA_API_KEY", "")
    # Legacy XTR receipt fallback only. Stars are no longer offered for new payments.
    STARS_PER_VALUE: Final = float(_get_optional("STARS_PER_VALUE", "0.5208333333"))
    REFERRAL_PERCENT: Final = int(_get_optional("REFERRAL_PERCENT", "0"))
    PAY_CURRENCY: str = (_get_optional("PAY_CURRENCY", "RUB") or "RUB").strip().upper()
    PAYMENT_TIME: Final = int(_get_optional("PAYMENT_TIME", "1800"))
    MIN_AMOUNT: Final = int(_get_optional("MIN_AMOUNT", "20"))
    MAX_AMOUNT: Final = int(_get_optional("MAX_AMOUNT", "10000"))
    TEST_PAYMENT_ENABLED: Final = _get_optional("TEST_PAYMENT_ENABLED", "0")
    # Username shown for manual balance top-ups. It is used only to build a
    # Telegram link with a pre-filled request; no credentials are stored here.
    PAYMENT_ADMIN_USERNAME: Final = _get_optional("PAYMENT_ADMIN_USERNAME", "")
    # xRocket Pay API (https://docs.xrocket.exchange/api/pay/pay-api-overview).
    # Bearer token of the "My Store" app. Invoices are created in crypto
    # (USDT) because the Pay API does not convert fiat<->crypto itself.
    XROCKET_PAY_TOKEN: Final = _get_optional("XROCKET_PAY_TOKEN", "")
    XROCKET_PAY_ASSET: Final = (_get_optional("XROCKET_PAY_ASSET", "USDT") or "USDT").strip().upper()
    # Public receiving address for manual USDT top-ups on BEP20 (set it in .env).
    MANUAL_USDT_BEP20: Final = _get_optional("MANUAL_USDT_BEP20", "")

    @classmethod
    def set_pay_currency(cls, currency: str) -> str:
        """Apply a validated fiat code to the running bot configuration.

        The setting is intentionally kept in the process environment as well
        as on ``EnvKeys`` so newly imported modules observe the same value.
        The web admin persists it to ``.env`` when that file is available.
        """
        normalized = str(currency or "").strip().upper()
        if normalized not in PAY_CURRENCY_CODES:
            raise ValueError("Unsupported payment currency")
        cls.PAY_CURRENCY = normalized
        os.environ["PAY_CURRENCY"] = normalized
        return normalized

    # Links / UI
    CHANNEL_URL: Final = _get_optional("CHANNEL_URL", "")
    CHANNEL_ID: Final = _get_optional("CHANNEL_ID", "")
    COMMUNITY_CHAT_URL: Final = _get_optional("COMMUNITY_CHAT_URL", "")
    COMMUNITY_CHAT_ID: Final = _get_optional("COMMUNITY_CHAT_ID", "")
    HELPER_ID: Final = _get_optional("HELPER_ID", "")
    SUPPORT_URL: Final = _get_optional("SUPPORT_URL", "")
    SUPPORT_USERNAME: Final = _get_optional("SUPPORT_USERNAME", "")
    SHOP_NAME: Final = _get_optional("SHOP_NAME", "My Store")
    RULES: Final = _get_optional("RULES", "")
    AGREEMENT: Final = _get_optional("AGREEMENT", "")
    # Public legal documents (Telegraph pages) shown in the agreements menu.
    LEGAL_AGREEMENT_URL: Final = _get_optional(
        "LEGAL_AGREEMENT_URL", ""
    )
    LEGAL_PRIVACY_URL: Final = _get_optional(
        "LEGAL_PRIVACY_URL", ""
    )
    FAQ: Final = _get_optional("FAQ", "")
    LEGAL_SELLER_DETAILS: Final = _get_optional("LEGAL_SELLER_DETAILS", "")
    LEGAL_PRIVACY_CONTACT: Final = _get_optional("LEGAL_PRIVACY_CONTACT", "")
    UI_ASSETS_DIR: Final = _get_optional("UI_ASSETS_DIR", "assets/ui")

    # Locale & logs
    BOT_LOCALE: Final = _get_optional("BOT_LOCALE", "ru")
    BOT_LOGFILE: Final = _get_optional("BOT_LOGFILE", "logs/bot.log")
    BOT_AUDITFILE: Final = _get_optional("BOT_AUDITFILE", "logs/audit.log")
    LOG_TO_STDOUT: Final = _get_optional("LOG_TO_STDOUT", "1")
    LOG_TO_FILE: Final = _get_optional("LOG_TO_FILE", "1")
    DEBUG: Final = _get_optional("DEBUG", "0")
    REVIEWS_ENABLED: Final = _get_optional("REVIEWS_ENABLED", "1")

    # Web admin panel
    ADMIN_HOST: Final = _get_optional("ADMIN_HOST", _get_optional("MONITORING_HOST", "localhost"))
    ADMIN_PORT: Final = int(_get_optional("ADMIN_PORT", _get_optional("PORT", _get_optional("MONITORING_PORT", "9090"))))
    # The partner API is a separate ASGI app on a loopback-only listener. Caddy
    # (or another local TLS proxy) is responsible for the public API hostname.
    PARTNER_API_ENABLED: Final = _get_optional("PARTNER_API_ENABLED", "0")
    PARTNER_API_HOST: Final = _get_optional("PARTNER_API_HOST", "127.0.0.1")
    PARTNER_API_PORT: Final = int(_get_optional("PARTNER_API_PORT", "9091"))
    # Private upstream subscription URL: do not log, return from APIs, or commit.
    VPN_UPSTREAM_SUBSCRIPTION_URL: Final = _get_optional("VPN_UPSTREAM_SUBSCRIPTION_URL", "")
    VPN_PUBLIC_BASE_URL: Final = _get_optional(
        "VPN_PUBLIC_BASE_URL", "https://api.example.com"
    )
    ADMIN_USERNAME: Final = _get_optional("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: Final = _get_optional("ADMIN_PASSWORD", _DEFAULT_ADMIN_PASSWORD)
    # Optional separate password for the Telegram owner panel.  The handler
    # falls back to ADMIN_PASSWORD when this is empty for backwards
    # compatibility with existing deployments.
    BOT_ADMIN_PASSWORD: Final = _get_optional("BOT_ADMIN_PASSWORD", "")
    SECRET_KEY: Final = _get_optional("SECRET_KEY", _DEFAULT_SECRET_KEY)
    ADMIN_COOKIE_SECURE: Final = _get_optional("ADMIN_COOKIE_SECURE", "auto")

    # aiogram otherwise creates one task per Telegram update with no upper
    # bound, which can exhaust memory and database connections.
    POLLING_TASKS_CONCURRENCY: Final = int(_get_optional("POLLING_TASKS_CONCURRENCY", "32"))

    # Webhook
    WEBHOOK_ENABLED: Final = _get_optional("WEBHOOK_ENABLED", "0")
    WEBHOOK_URL: Final = _get_optional("WEBHOOK_URL", "")
    WEBHOOK_PATH: Final = _get_optional("WEBHOOK_PATH", "/webhook")
    WEBHOOK_SECRET: Final = _get_optional("WEBHOOK_SECRET", "")
    WEBHOOK_HOST: Final = _get_optional("WEBHOOK_HOST", "0.0.0.0")
    WEBHOOK_PORT: Final = int(_get_optional("WEBHOOK_PORT", "8080"))

    # Cleanup
    AUDIT_RETENTION_DAYS: Final = int(_get_optional("AUDIT_RETENTION_DAYS", "90"))
    PAYMENTS_RETENTION_DAYS: Final = int(_get_optional("PAYMENTS_RETENTION_DAYS", "90"))

    DATABASE_URL: Final = f"postgresql+asyncpg://{POSTGRES_USER}:{quote_plus(POSTGRES_PASSWORD)}@{POSTGRES_HOST}:{DB_PORT}/{POSTGRES_DB}"

    @classmethod
    def panel_is_exposed(cls) -> bool:
        """Whether the admin panel is bound somewhere off-host.

        Webhook mode counts as exposed regardless of the bind address: it is by
        definition a public production deployment, and the placeholder
        credentials have no place in one.
        """
        return (
            cls.ADMIN_HOST.strip().lower() not in _LOOPBACK_HOSTS
            or cls.WEBHOOK_ENABLED == "1"
        )

    @classmethod
    def session_cookie_secure(cls) -> bool:
        """Whether the admin session cookie should be marked Secure."""
        setting = cls.ADMIN_COOKIE_SECURE.strip().lower()
        if setting in ("1", "true", "yes"):
            return True
        if setting in ("0", "false", "no"):
            return False
        return cls.panel_is_exposed()

    @classmethod
    def validate(cls) -> None:
        """Check configuration: fatal on unsafe defaults, warnings otherwise."""
        insecure = []
        if cls.SECRET_KEY == _DEFAULT_SECRET_KEY:
            insecure.append(
                "SECRET_KEY is the shipped default — anyone who can reach the panel "
                "can forge an admin session. Generate one with: "
                'python -c "import secrets; print(secrets.token_hex(32))"'
            )
        if cls.ADMIN_PASSWORD == _DEFAULT_ADMIN_PASSWORD:
            insecure.append(
                "ADMIN_PASSWORD is the shipped default 'admin'. Set a strong password."
            )

        if insecure:
            if cls.panel_is_exposed():
                raise RuntimeError(
                    "Refusing to start: the admin panel is reachable "
                    f"(ADMIN_HOST={cls.ADMIN_HOST!r}, WEBHOOK_ENABLED={cls.WEBHOOK_ENABLED!r}) "
                    "with insecure default credentials.\n  - " + "\n  - ".join(insecure)
                )
            for problem in insecure:
                _env_logger.warning("SECURITY: %s", problem)

        if int(cls.MIN_AMOUNT) >= int(cls.MAX_AMOUNT):
            raise RuntimeError(
                f"Refusing to start: MIN_AMOUNT ({cls.MIN_AMOUNT}) >= MAX_AMOUNT "
                f"({cls.MAX_AMOUNT}). Payment amounts would always be rejected."
            )
        if int(cls.REFERRAL_PERCENT) < 0 or int(cls.REFERRAL_PERCENT) > 99:
            raise RuntimeError(
                f"Refusing to start: REFERRAL_PERCENT={cls.REFERRAL_PERCENT} is outside "
                "the valid range [0, 99]. 100 would pay out the full top-up as a bonus."
            )
        if int(cls.POLLING_TASKS_CONCURRENCY) < 1 or int(cls.POLLING_TASKS_CONCURRENCY) > 1000:
            raise RuntimeError(
                "Refusing to start: POLLING_TASKS_CONCURRENCY must be within [1, 1000]."
            )
        if cls.PARTNER_API_ENABLED not in {"0", "1"}:
            raise RuntimeError("Refusing to start: PARTNER_API_ENABLED must be 0 or 1.")
        if cls.PARTNER_API_ENABLED == "1":
            if not 1 <= cls.PARTNER_API_PORT <= 65535:
                raise RuntimeError("Refusing to start: PARTNER_API_PORT must be within [1, 65535].")
            if cls.PARTNER_API_HOST.strip().lower() not in _LOOPBACK_HOSTS:
                raise RuntimeError(
                    "Refusing to start: PARTNER_API_HOST must be loopback-only; "
                    "terminate public HTTPS at the reverse proxy."
                )
            if cls.PARTNER_API_PORT == cls.ADMIN_PORT:
                raise RuntimeError(
                    "Refusing to start: PARTNER_API_PORT must differ from ADMIN_PORT."
                )
            if (
                cls.WEBHOOK_ENABLED == "1"
                and cls.PARTNER_API_HOST == cls.WEBHOOK_HOST
                and cls.PARTNER_API_PORT == cls.WEBHOOK_PORT
            ):
                raise RuntimeError(
                    "Refusing to start: partner API and Telegram webhook cannot share a listener."
                )

        if cls.TEST_PAYMENT_ENABLED == "1" and cls.DEBUG != "1":
            raise RuntimeError(
                "TEST_PAYMENT_ENABLED is only allowed with DEBUG=1. "
                "Disable the demo provider before production deployment."
            )
        if cls.TEST_PAYMENT_ENABLED == "1" and cls.panel_is_exposed():
            raise RuntimeError(
                "Refusing to start: TEST_PAYMENT_ENABLED mints free balance and must "
                "never be on in a reachable deployment."
            )

        # Placeholder secrets from .env.example must never boot in production.
        # (Empty values are still rejected earlier by _get_required.)
        placeholders = {
            "POSTGRES_PASSWORD": "change_me_to_strong_password",
            "REDIS_PASSWORD": "changeme_redis_pass",
            "TOKEN": "your_bot_token_here",
        }
        for key, bad in placeholders.items():
            if str(getattr(cls, key, "") or "") == bad:
                raise RuntimeError(
                    f"Refusing to start: {key} still holds the .env.example "
                    "placeholder. Set a real value."
                )

        if cls.WEBHOOK_ENABLED == "1":
            if len(str(cls.WEBHOOK_SECRET or "")) < 32:
                raise RuntimeError(
                    "Refusing to start: WEBHOOK_ENABLED=1 requires WEBHOOK_SECRET "
                    "of at least 32 characters, otherwise anyone can forge "
                    "Telegram updates (including fake payments)."
                )
            parsed_webhook_url = urlparse(str(cls.WEBHOOK_URL or "").strip())
            if parsed_webhook_url.scheme != "https" or not parsed_webhook_url.netloc:
                raise RuntimeError(
                    "Refusing to start: WEBHOOK_URL must be a complete https:// URL."
                )
            webhook_path = str(cls.WEBHOOK_PATH or "")
            if not webhook_path.startswith("/") or "\n" in webhook_path or "\r" in webhook_path:
                raise RuntimeError(
                    "Refusing to start: WEBHOOK_PATH must be an absolute URL path."
                )

        # Behind-proxy deployments look like loopback to panel_is_exposed(),
        # but ADMIN_COOKIE_SECURE=1 proves the operator publishes the panel.
        if cls.session_cookie_secure() and insecure:
            raise RuntimeError(
                "Refusing to start: the admin panel is published "
                "(ADMIN_COOKIE_SECURE=1) with insecure default credentials.\n  - "
                + "\n  - ".join(insecure)
            )

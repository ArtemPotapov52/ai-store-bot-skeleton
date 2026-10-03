import asyncio
import hmac
import logging
import os
import secrets
import stat
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqladmin import Admin, BaseView, ModelView, expose
from sqladmin.authentication import AuthenticationBackend
from sqladmin.fields import DateTimeField as SQLAdminDateTimeField
from sqladmin.helpers import get_object_identifier
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import Route
from sqlalchemy import case, exists as sa_exists, func, text, update as sa_update
from sqlalchemy.exc import IntegrityError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from markupsafe import Markup
from pydantic import ValidationError
from wtforms import SelectField
from sqlalchemy import select as sa_select

from bot.misc import BroadcastMessage, EnvKeys, sanitize_html
from bot.misc.timezone import (
    as_moscow_datetime,
    format_moscow_datetime,
    moscow_input_to_utc,
    moscow_today,
)
from bot.misc.env import PAY_CURRENCY_CHOICES, PAY_CURRENCY_CODES
from bot.database.methods.audit import log_audit
from bot.database.methods.create import add_values_bulk
from bot.web.platega import platega_callback_endpoint

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """Resolve the real client IP, trusting X-Forwarded-For only from loopback.

    When a reverse proxy on the same host fronts the panel, request.client.host
    is 127.0.0.1; the original client is then the first hop of X-Forwarded-For.
    That header ONLY when the socket peer is loopback, so an external
    client cannot spoof its IP (which would otherwise defeat the default-cred
    guard and the login rate limiter).
    """
    peer = request.client.host if request.client else ""
    if peer in ("127.0.0.1", "::1"):
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return peer


class LoginRateLimiter:
    """In-memory rate limiter for login attempts by IP."""

    def __init__(self, max_attempts: int = 5, lockout_seconds: int = 900):
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._attempts: dict[str, list[float]] = {}
        self._last_cleanup: float = time.time()

    def is_blocked(self, ip: str) -> bool:
        if ip not in self._attempts:
            return False
        now = time.time()
        self._attempts[ip] = [t for t in self._attempts[ip] if now - t < self.lockout_seconds]
        return len(self._attempts[ip]) >= self.max_attempts

    def record_failure(self, ip: str) -> None:
        now = time.time()
        if now - self._last_cleanup > 600:
            self._attempts = {
                k: [t for t in v if now - t < self.lockout_seconds]
                for k, v in self._attempts.items()
                if any(now - t < self.lockout_seconds for t in v)
            }
            self._last_cleanup = now
        if ip not in self._attempts:
            self._attempts[ip] = []
        self._attempts[ip].append(now)

    def reset(self, ip: str) -> None:
        self._attempts.pop(ip, None)


_login_limiter = LoginRateLimiter()

# Keep an active admin in the panel without turning the session into an
# effectively permanent credential.  SessionMiddleware signs the cookie and
# rejects it after ``max_age``; it only emits a new expiry when the session is
# modified, so AdminAuth refreshes this marker periodically on authenticated
# requests.  An active operator therefore stays signed in, while an abandoned
# browser still requires a login after 30 days.
ADMIN_SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
ADMIN_SESSION_REFRESH_INTERVAL_SECONDS = 5 * 60
_ADMIN_SESSION_TOUCH_KEY = "_admin_session_touch"


def _refresh_admin_session(request: Request) -> None:
    """Slide the admin cookie expiry forward while the session is in use."""
    now = int(time.time())
    try:
        last_touch = int(request.session.get(_ADMIN_SESSION_TOUCH_KEY, 0))
    except (TypeError, ValueError):
        last_touch = 0
    if now - last_touch >= ADMIN_SESSION_REFRESH_INTERVAL_SECONDS:
        request.session[_ADMIN_SESSION_TOUCH_KEY] = now

# PBKDF2 is deliberately expensive. Keep a small process-wide admission
# limit in addition to the per-IP failure window, so a distributed password
# spray cannot occupy every worker thread and starve the bot event loop.
_PASSWORD_VERIFY_SLOTS = threading.BoundedSemaphore(4)


def _verify_password_limited(password: str, stored: str) -> bool:
    if not _PASSWORD_VERIFY_SLOTS.acquire(blocking=False):
        return False
    try:
        return verify_password(password, stored)
    finally:
        _PASSWORD_VERIFY_SLOTS.release()


_HEALTH_CACHE_TTL_SECONDS = 5.0
_HEALTH_PROBE_TIMEOUT_SECONDS = 2.0
_health_cache: tuple[bool, float] | None = None
_health_probe_lock = asyncio.Lock()


async def _database_ready() -> bool:
    """Probe PostgreSQL at most once per short interval."""
    global _health_cache
    now = time.monotonic()
    if _health_cache is not None and _health_cache[1] > now:
        return _health_cache[0]

    async with _health_probe_lock:
        now = time.monotonic()
        if _health_cache is not None and _health_cache[1] > now:
            return _health_cache[0]
        try:
            async def probe() -> None:
                async with Database().session() as session:
                    await session.execute(text("SELECT 1"))

            await asyncio.wait_for(probe(), timeout=_HEALTH_PROBE_TIMEOUT_SECONDS)
            db_ok = True
        except Exception as exc:
            logger.error("Health readiness database error: %s", exc)
            db_ok = False
        _health_cache = (db_ok, time.monotonic() + _HEALTH_CACHE_TTL_SECONDS)
        return db_ok


from bot.database.main import Database
from bot.database.models.main import (
    User, Role, Categories, Goods, ItemValues,
    BoughtGoods, Operations, Payments, ManualRevenue, ProductExpense, FinanceReceipt,
    ProcurementPlan, ProcurementPlanItem, ReferralEarnings,
    AuditLog, PromoCodes, CartItems, Reviews,
    Permission,
)
from bot.web.access import (
    AdminLoginBodyLimitMiddleware,
    HealthRateLimitMiddleware,
    LOGIN_BODY_MAX_BYTES,
    WebAccessMixin,
    has_web_perm,
    resolve_web_perms,
    role_permissions,
    verify_password,
)
from bot.misc.metrics import get_metrics
from bot.misc.caching import get_cache_manager
from bot.database.methods.read import (
    invalidate_user_cache, invalidate_item_cache, invalidate_rating_cache,
    invalidate_category_cache, invalidate_stats_cache,
    get_item_name_by_id, get_category_name_by_id,
    get_all_users,
)
from bot.database.methods.pricing import effective_price
from bot.database.methods.cache_utils import safe_create_task
from bot.misc.services.broadcast_system import BroadcastManager
from bot.misc.services.catalog_notifications import (
    announce_catalog_arrival,
    notify_owner_stock_added,
    product_purchase_link,
)
from bot.misc.services.restock_notifier import notify_restock
from bot.keyboards.inline import close
from bot.middleware.security import invalidate_auth_caches, flush_all_role_caches
from bot.web.catalog_import import CatalogImportError, parse_catalog_import
from bot.web.stock_bulk import SEPARATORS, StockBulkError, parse_bulk_values
from bot.web.revenue import (
    MOSCOW_TZ,
    REVENUE_DAYS,
    REVENUE_PERIOD_OPTIONS,
    RevenueInputError,
    build_revenue_report,
    is_excluded_revenue_entry_date,
    parse_manual_revenue_fields,
    parse_revenue_period,
    revenue_window,
)
from bot.web.expenses import (
    ExpenseInputError,
    build_expense_report,
    build_inventory_forecast,
    parse_expense_fields,
)
from bot.web.finance import (
    FINANCE_PERIOD_OPTIONS,
    FinanceInputError,
    aggregate_daily_cash_events,
    finance_window,
    parse_finance_period,
    parse_finance_receipt_fields,
    validate_receipt_allocation,
)
from bot.web.procurement import (
    MAX_AMOUNT,
    ProcurementInputError,
    build_daily_procurement_report,
    calculate_forecast,
    parse_plan_metadata,
    parse_procurement_form,
)
from bot.web.product_reminder import (
    ProductReminderError,
    build_reminder_text,
    format_reminder_money,
    parse_reminder_price,
)
from bot.web.bot_statistics import (
    BOT_STATS_PERIOD_OPTIONS,
    load_bot_statistics,
    parse_bot_stats_period,
)


# Authentication
class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request) -> bool:
        ip = _client_ip(request)

        if _login_limiter.is_blocked(ip):
            await log_audit("web_login_blocked", level="WARNING", details=f"ip={ip}", ip_address=ip)
            return False

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > LOGIN_BODY_MAX_BYTES:
                    await log_audit(
                        "web_login_body_too_large",
                        level="WARNING",
                        details=f"ip={ip}",
                        ip_address=ip,
                    )
                    return False
            except ValueError:
                return False

        form = await request.form(
            max_files=4,
            max_fields=8,
            max_part_size=LOGIN_BODY_MAX_BYTES,
        )
        username = form.get("username")
        password = form.get("password")

        # Constant-time comparison to avoid leaking credential length/content via
        # response timing. str() guards against a missing form field (None).
        creds_ok = (
            hmac.compare_digest(str(username), str(EnvKeys.ADMIN_USERNAME))
            and hmac.compare_digest(str(password), str(EnvKeys.ADMIN_PASSWORD))
        )
        if creds_ok:
            if (
                username == "admin" and password == "admin"
                and ip not in ("127.0.0.1", "::1", "localhost")
            ):
                await log_audit("web_login_blocked_default_creds", level="WARNING", details=f"ip={ip}", ip_address=ip)
                return False
            from bot.web.access import _FULL_PERMS

            request.session.clear()
            request.session.update(
                {"authenticated": True, "web_owner": True,
                 "web_login": str(username), "web_perms": _FULL_PERMS}
            )
            _login_limiter.reset(ip)
            await log_audit("web_login", user_id=None, details=f"user={username} (owner)", ip_address=ip)
            return True

        # Personal web logins (see bot/web/access.py + scripts/manage_web_admin.py).
        from bot.web.access import find_web_admin
        from bot.database.models.main import Permission as _Perm

        web_admin = await find_web_admin(str(username or ""))
        if web_admin is not None and await asyncio.to_thread(
            _verify_password_limited,
            str(password or ""),
            web_admin.password_hash,
        ):
            perms = await role_permissions(web_admin.role_id)
            if not _Perm.has_any_admin_perm(perms):
                await log_audit("web_login_blocked_no_perms", level="WARNING",
                                details=f"user={username}", ip_address=ip)
                return False
            request.session.clear()
            request.session.update(
                {"authenticated": True, "web_admin_id": web_admin.id,
                 "web_login": str(username), "web_perms": perms}
            )
            _login_limiter.reset(ip)
            await log_audit("web_login", user_id=None, details=f"user={username}", ip_address=ip)
            return True

        _login_limiter.record_failure(ip)
        await log_audit("web_login_failed", level="WARNING", details=f"user={username}", ip_address=ip)
        return False

    async def logout(self, request: Request) -> bool:
        await log_audit("web_logout", ip_address=_client_ip(request))
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        from bot.web.access import web_session_active

        # A disabled login or a wiped row loses access immediately.
        active = await web_session_active(request)
        if active:
            _refresh_admin_session(request)
        return active


def _safe_model_repr(model: Any, max_len: int = 500) -> str:
    """Return a truncated repr that excludes sensitive fields."""
    _sensitive = {"balance", "password", "secret", "token", "value"}
    parts = []
    for col in getattr(model, "__table__", None).columns if hasattr(model, "__table__") else ():
        if col.name in _sensitive:
            continue
        val = getattr(model, col.name, None)
        parts.append(f"{col.name}={val!r}")
    result = f"{type(model).__name__}({', '.join(parts)})"
    return result[:max_len]


_notifier_bot: Any = None
_web_broadcast_running = False
def _format_moscow_datetime(model: Any, name: str) -> str:
    """Format an admin datetime in Moscow time without changing stored data."""
    return format_moscow_datetime(getattr(model, name, None))


class MoscowDateTimeField(SQLAdminDateTimeField):
    """Render stored UTC timestamps as Moscow-local values in admin forms."""

    def _value(self) -> str:
        if self.raw_data:
            return " ".join(self.raw_data)
        moment = as_moscow_datetime(self.data)
        return moment.strftime(self.format[0]) if moment else ""


def set_notifier_bot(bot: Any) -> None:
    global _notifier_bot
    _notifier_bot = bot


def _parse_telegram_id(raw_value: Any) -> int | None:
    """Return a positive Telegram ID from a bounded admin form value."""
    value = str(raw_value or "").strip()
    if not value or len(value) > 32 or not value.isdigit():
        return None
    try:
        telegram_id = int(value)
    except ValueError:
        return None
    return telegram_id if 0 < telegram_id <= 2**63 - 1 else None


async def _telegram_profile(telegram_id: int) -> dict[str, str | None]:
    """Resolve a display name/username while keeping an ID deep link as fallback."""
    profile: dict[str, str | None] = {
        "display_name": None,
        "username": None,
        "profile_url": f"tg://user?id={telegram_id}",
    }
    if _notifier_bot is None:
        return profile

    try:
        chat = await _notifier_bot.get_chat(telegram_id)
    except Exception:
        logger.info("Telegram profile lookup failed for user %s", telegram_id, exc_info=True)
        return profile

    first_name = str(getattr(chat, "first_name", "") or "").strip()
    last_name = str(getattr(chat, "last_name", "") or "").strip()
    display_name = " ".join(part for part in (first_name, last_name) if part)
    username = str(getattr(chat, "username", "") or "").strip().lstrip("@")
    profile["display_name"] = display_name or None
    profile["username"] = f"@{username}" if username else None
    if username:
        profile["profile_url"] = f"https://t.me/{quote(username, safe='')}"
    return profile


async def _arrival_announcement_requested(request: Request) -> bool:
    """Read the non-model checkbox added to stock forms.

    SQLAdmin only passes mapped fields into ``on_model_change``. Keeping this
    checkbox outside the model avoids a migration for a one-time operator
    choice, while Starlette's cached form body keeps it safe to read here.
    """
    try:
        form = await request.form()
    except Exception:
        return False
    return str(form.get("announce_arrival") or "").lower() in {"1", "true", "on", "yes"}


async def _notify_all_requested(request: Request) -> bool:
    """Read the stock-form switch that opts this arrival into a full broadcast."""
    try:
        form = await request.form()
    except Exception:
        return False
    return str(form.get("notify_all") or "").lower() in {"1", "true", "on", "yes"}


async def _run_web_broadcast(bot: Any, text: str, operator_ip: str) -> None:
    """Deliver a web-admin message to all users and retain its result in the audit log."""
    global _web_broadcast_running
    try:
        user_ids = [int(row[0]) for row in await get_all_users()]
        stats = await BroadcastManager(bot).broadcast(
            user_ids=user_ids,
            text=text,
            reply_markup=close(),
            parse_mode="HTML",
            disable_notification=False,
        )
        await log_audit(
            "web_broadcast_finished",
            resource_type="Рассылка",
            details=(
                f"total={stats.total}, sent={stats.sent}, "
                f"failed={stats.failed}, blocked={stats.blocked}"
            ),
            ip_address=operator_ip,
        )
    finally:
        _web_broadcast_running = False


# Audited base view for mutable models
class AuditModelView(WebAccessMixin, ModelView):
    """Model views with audit logging and role-based access (see required_perm)."""

    required_perm: int | None = None

    async def after_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        action = f"sqladmin_{'create' if is_created else 'update'}"
        await log_audit(
            action,
            resource_type=self.name,
            resource_id=str(getattr(model, 'id', getattr(model, 'name', None))),
            details=_safe_model_repr(model),
            ip_address=_client_ip(request),
        )

    async def after_model_delete(self, model: Any, request: Request) -> None:
        await log_audit(
            "sqladmin_delete",
            resource_type=self.name,
            resource_id=str(getattr(model, 'id', getattr(model, 'name', None))),
            details=_safe_model_repr(model),
            ip_address=_client_ip(request),
        )


class RussianAdmin(Admin):
    """Keep SQLAdmin's save routing compatible with the Russian form buttons."""

    @staticmethod
    def get_save_redirect_url(request, form, obj, model_view):
        action = form.get("save")
        identity = request.path_params["identity"]
        identifier = get_object_identifier(obj)

        if action in ("Save", "Сохранить"):
            return request.url_for("admin:list", identity=identity)
        if action in ("Save and continue editing", "Продолжить редактирование") or (
            action in ("Save as new", "Сохранить как новый")
            and model_view.save_as_continue
        ):
            return request.url_for("admin:edit", identity=identity, pk=identifier)
        return request.url_for("admin:create", identity=identity)


class CatalogImportView(WebAccessMixin, BaseView):
    identity = "catalog-import"
    required_perm = Permission.CATALOG_MANAGE
    """Create many simple counted-stock products from one UTF-8 text file."""

    name = "Импорт товаров"
    icon = "fa-solid fa-file-import"

    @expose("/catalog-import", methods=["GET", "POST"], identity="catalog-import")
    async def catalog_import(self, request: Request):
        error: str | None = None
        result: str | None = None

        async with Database().session() as session:
            categories = (await session.execute(
                sa_select(Categories.id, Categories.name)
                .where(Categories.is_active.is_(True), ~Categories.children.any())
                .order_by(Categories.name)
            )).all()

        if request.method == "POST":
            form = await request.form()
            raw_category_id = form.get("category_id")
            uploaded = form.get("catalog_file")
            try:
                category_id = int(str(raw_category_id))
                if not hasattr(uploaded, "read"):
                    raise CatalogImportError("Выберите текстовый файл с товарами.")
                raw_content = await uploaded.read(1_048_577)
                if len(raw_content) > 1_048_576:
                    raise CatalogImportError("Файл слишком большой: максимум 1 МБ.")
                try:
                    products = parse_catalog_import(raw_content.decode("utf-8-sig"))
                except UnicodeDecodeError as exc:
                    raise CatalogImportError("Файл должен быть сохранён в кодировке UTF-8.") from exc

                async with Database().session() as session:
                    category = (await session.execute(
                        sa_select(Categories).where(
                            Categories.id == category_id,
                            Categories.is_active.is_(True),
                            ~Categories.children.any(),
                        )
                    )).scalars().one_or_none()
                    if category is None:
                        raise CatalogImportError("Выберите активную категорию без подразделов.")
                    category_name = category.name

                    existing_names = set((await session.execute(
                        sa_select(Goods.name).where(Goods.name.in_([p.name for p in products]))
                    )).scalars())
                    new_products = [product for product in products if product.name not in existing_names]
                    for product in new_products:
                        session.add(Goods(
                            name=product.name,
                            price=product.price,
                            description=product.description,
                            category_id=category.id,
                            stock_quantity=product.stock_quantity,
                            delivery_text=product.delivery_text,
                        ))

                for product in new_products:
                    safe_create_task(invalidate_item_cache(product.name, category_name))
                safe_create_task(invalidate_category_cache(category_name))
                safe_create_task(invalidate_stats_cache())
                await log_audit(
                    "catalog_bulk_import",
                    resource_type="Категория",
                    resource_id=str(category_id),
                    details=f"added={len(new_products)}, skipped_existing={len(existing_names)}",
                    ip_address=_client_ip(request),
                )
                result = (
                    f"Добавлено товаров: {len(new_products)}. "
                    f"Пропущено с уже существующим названием: {len(existing_names)}."
                )
            except (CatalogImportError, ValueError) as exc:
                error = str(exc)

        return await self.templates.TemplateResponse(
            request,
            "catalog_import.html",
            {"categories": categories, "error": error, "result": result},
        )


class WebBroadcastView(WebAccessMixin, BaseView):
    identity = "broadcast"
    required_perm = Permission.BROADCAST
    """A simple, auditable all-user message composer inside the web admin."""

    name = "Рассылка"
    icon = "fa-solid fa-bullhorn"

    @expose("/broadcast", methods=["GET", "POST"], identity="broadcast")
    async def broadcast(self, request: Request):
        global _web_broadcast_running
        error: str | None = None
        result: str | None = None
        draft = ""

        if request.method == "POST":
            form = await request.form()
            draft = str(form.get("message") or "").strip()
            if _web_broadcast_running:
                error = "Другая рассылка уже выполняется. Дождитесь её завершения."
            elif _notifier_bot is None:
                error = "Бот не подключён к веб-админке. Перезапустите приложение."
            else:
                try:
                    message = BroadcastMessage(text=draft, parse_mode="HTML")
                except ValidationError as exc:
                    error = f"Проверьте текст сообщения: {exc.errors()[0]['msg']}"
                else:
                    _web_broadcast_running = True
                    safe_text = sanitize_html(message.text)
                    safe_create_task(_run_web_broadcast(
                        _notifier_bot,
                        safe_text,
                        _client_ip(request),
                    ))
                    await log_audit(
                        "web_broadcast_started",
                        resource_type="Рассылка",
                        details=f"characters={len(message.text)}",
                        ip_address=_client_ip(request),
                    )
                    result = (
                        "Рассылка запущена. Её итог появится в «Журнале действий»; "
                        "во время отправки второй запуск недоступен."
                    )

        return await self.templates.TemplateResponse(
            request,
            "web_broadcast.html",
            {"error": error, "result": result, "draft": draft, "is_running": _web_broadcast_running},
        )


class StockBulkView(WebAccessMixin, BaseView):
    """Add many independent auto-delivery lots from one text field."""

    identity = "stock-bulk"
    required_perm = Permission.CATALOG_MANAGE
    name = "Добавить много"
    icon = "fa-solid fa-layer-group"

    @expose("/stock-bulk", methods=["GET", "POST"], identity="stock-bulk")
    async def stock_bulk(self, request: Request):
        error: str | None = None
        result: str | None = None
        selected_item_id = ""
        separator = "newline"
        bulk_text = ""
        notify_all = False
        announce_arrival = False

        async with Database().session() as session:
            item_rows = (await session.execute(
                sa_select(Goods.id, Goods.name, Goods.price).order_by(Goods.name)
            )).all()
        items = [
            {"id": int(row.id), "name": str(row.name), "price": row.price}
            for row in item_rows
        ]

        if request.method == "POST":
            form = await request.form()
            selected_item_id = str(form.get("item_id") or "").strip()
            separator = str(form.get("separator") or "").strip()
            bulk_text = str(form.get("bulk_values") or "")
            notify_all = str(form.get("notify_all") or "").lower() in {
                "1", "true", "on", "yes"
            }
            announce_arrival = str(form.get("announce_arrival") or "").lower() in {
                "1", "true", "on", "yes"
            }

            try:
                try:
                    item_id = int(selected_item_id)
                except (TypeError, ValueError) as exc:
                    raise StockBulkError("Выберите существующий товар.") from exc
                item = next((entry for entry in items if entry["id"] == item_id), None)
                if item is None:
                    raise StockBulkError("Выберите существующий товар.")

                values = parse_bulk_values(bulk_text, separator)
                async with Database().session() as session:
                    has_infinite_stock = (await session.execute(
                        sa_select(ItemValues.id).where(
                            ItemValues.item_id == item_id,
                            ItemValues.is_infinity.is_(True),
                        ).limit(1)
                    )).scalar_one_or_none() is not None
                if has_infinite_stock:
                    raise StockBulkError(
                        "У этого товара уже включена выдача без ограничения. "
                        "Сначала удалите бесконечную единицу или выберите другой товар."
                    )

                added, skipped_db_dup, skipped_batch_dup, skipped_invalid = await add_values_bulk(
                    item["name"], values, is_infinity=False
                )
                if added:
                    if _notifier_bot is not None:
                        safe_create_task(notify_restock(
                            _notifier_bot,
                            item["name"],
                            notify_all=notify_all,
                            added_count=added,
                            price=item["price"],
                            item_id=item_id,
                        ))
                        safe_create_task(notify_owner_stock_added(
                            _notifier_bot,
                            item_name=item["name"],
                            price=item["price"],
                            count=added,
                            is_infinity=False,
                            item_id=item_id,
                        ))
                        if announce_arrival:
                            safe_create_task(announce_catalog_arrival(
                                _notifier_bot,
                                item_name=item["name"],
                                price=item["price"],
                                count=added,
                                item_id=item_id,
                                is_infinity=False,
                            ))

                details = (
                    f"added={added}, skipped_db_dup={skipped_db_dup}, "
                    f"skipped_batch_dup={skipped_batch_dup}, skipped_invalid={skipped_invalid}, "
                    f"separator={separator}"
                )
                await log_audit(
                    "catalog_bulk_stock",
                    resource_type="Склад и автовыдача",
                    resource_id=str(item["name"]),
                    details=details,
                    ip_address=_client_ip(request),
                )

                result_parts = [f"Добавлено аккаунтов: {added}."]
                if skipped_db_dup:
                    result_parts.append(f"Уже были на складе: {skipped_db_dup}.")
                if skipped_batch_dup:
                    result_parts.append(f"Повторились в этом файле: {skipped_batch_dup}.")
                if skipped_invalid:
                    result_parts.append(f"Пустых строк пропущено: {skipped_invalid}.")
                result = " ".join(result_parts)
                bulk_text = ""
            except (StockBulkError, ValueError) as exc:
                error = str(exc)

        return await self.templates.TemplateResponse(
            request,
            "stock_item_bulk.html",
            {
                "items": items,
                "separators": SEPARATORS,
                "selected_item_id": selected_item_id,
                "separator": separator,
                "bulk_text": bulk_text,
                "notify_all": notify_all,
                "announce_arrival": announce_arrival,
                "error": error,
                "result": result,
            },
        )


async def _run_product_reminder_broadcast(
    bot: Any,
    user_ids: list[int],
    text: str,
    reply_markup: InlineKeyboardMarkup,
    operator_ip: str,
    item_name: str,
) -> None:
    """Send a confirmed reminder with the shared rate-limited broadcast worker."""
    global _web_broadcast_running
    try:
        stats = await BroadcastManager(bot).broadcast(
            user_ids=user_ids,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_notification=False,
        )
        await log_audit(
            "product_reminder_finished",
            resource_type="Напоминание о товаре",
            resource_id=item_name,
            details=(
                f"total={stats.total}, sent={stats.sent}, "
                f"failed={stats.failed}, blocked={stats.blocked}"
            ),
            ip_address=operator_ip,
        )
    except Exception:
        logger.exception("Product reminder broadcast failed for %r", item_name)
    finally:
        _web_broadcast_running = False


class ProductReminderView(WebAccessMixin, BaseView):
    """Preview and explicitly confirm an all-customer product reminder."""

    identity = "product-reminder"
    required_perm = Permission.CATALOG_MANAGE
    name = "Напомнить о товаре"
    icon = "fa-solid fa-bell"
    _session_key = "product_reminder_confirmation"

    async def _load_items(self) -> list[dict[str, Any]]:
        limited_count = func.sum(case((ItemValues.is_infinity.is_(False), 1), else_=0))
        infinite_count = func.max(case((ItemValues.is_infinity.is_(True), 1), else_=0))
        async with Database().session() as session:
            rows = (await session.execute(
                sa_select(
                    Goods,
                    (func.coalesce(limited_count, 0) + Goods.stock_quantity).label("quantity"),
                    infinite_count.label("is_infinite"),
                )
                .outerjoin(ItemValues, ItemValues.item_id == Goods.id)
                .where(Goods.is_active.is_(True))
                .group_by(Goods.id)
                .order_by(Goods.name)
            )).all()

        items = []
        for row in rows:
            goods = row[0]
            try:
                current_price, _on_sale, _original_price = effective_price(goods)
            except ValueError:
                logger.warning("Skipping product with invalid price in reminder: %s", goods.id)
                continue
            items.append({
                "id": int(goods.id),
                "name": str(goods.name),
                "price": current_price,
                "base_price": Decimal(str(goods.price)),
                "quantity": max(0, int(row.quantity or 0)),
                "is_infinite": bool(row.is_infinite),
            })
        return items

    @staticmethod
    def _preview(item: dict[str, Any], new_price: Decimal, price_was_entered: bool, token: str):
        return {
            "token": token,
            "item_id": item["id"],
            "name": item["name"],
            "quantity": "∞" if item["is_infinite"] else f'{item["quantity"]} шт.',
            "old_price": format_reminder_money(item["price"]),
            "new_price": format_reminder_money(new_price),
            "price_changed": price_was_entered and new_price != item["price"],
        }

    @expose("/product-reminder", methods=["GET", "POST"], identity="product-reminder")
    async def product_reminder(self, request: Request):
        global _web_broadcast_running
        error: str | None = None
        result: str | None = None
        preview: dict[str, Any] | None = None
        selected_item_id = ""
        price_raw = ""
        items = await self._load_items()

        if request.method == "POST":
            form = await request.form()
            action = str(form.get("action") or "")
            if action == "preview":
                selected_item_id = str(form.get("item_id") or "").strip()
                price_raw = str(form.get("price") or "").strip()
                try:
                    if not selected_item_id.isdigit():
                        raise ProductReminderError("Выберите существующий активный товар.")
                    item_id = int(selected_item_id)
                    item = next((entry for entry in items if entry["id"] == item_id), None)
                    if item is None:
                        raise ProductReminderError("Выберите существующий активный товар.")
                    if item["quantity"] <= 0 and not item["is_infinite"]:
                        raise ProductReminderError("Товар сейчас отсутствует на складе.")
                    new_price, price_was_entered = parse_reminder_price(price_raw, item["price"])
                    token = secrets.token_urlsafe(24)
                    request.session[self._session_key] = {
                        "token": token,
                        "item_id": item["id"],
                        "old_price": str(item["price"]),
                        "new_price": str(new_price),
                        "price_was_entered": price_was_entered,
                    }
                    preview = self._preview(item, new_price, price_was_entered, token)
                except (ProductReminderError, TypeError, ValueError) as exc:
                    error = str(exc) or "Выберите существующий товар."

            elif action == "send":
                confirmation = request.session.get(self._session_key) or {}
                submitted_token = str(form.get("confirmation") or "")
                if not hmac.compare_digest(str(confirmation.get("token") or ""), submitted_token):
                    error = "Предпросмотр устарел. Выберите товар и сформируйте его заново."
                else:
                    request.session.pop(self._session_key, None)
                    item = next(
                        (entry for entry in items if entry["id"] == confirmation.get("item_id")),
                        None,
                    )
                    if item is None or (item["quantity"] <= 0 and not item["is_infinite"]):
                        error = "Товар больше не доступен. Обновите страницу и выберите другой."
                    elif item["price"] != Decimal(str(confirmation.get("old_price"))):
                        error = "Цена товара изменилась после предпросмотра. Сформируйте напоминание заново."
                    elif _notifier_bot is None:
                        error = "Бот не подключён к веб-админке. Перезапустите приложение."
                    elif _web_broadcast_running:
                        error = "Другая рассылка уже выполняется. Дождитесь её завершения."
                    else:
                        # Reserve synchronously before the first await so two
                        # simultaneous confirmations cannot both launch broadcasts.
                        _web_broadcast_running = True
                        broadcast_scheduled = False
                        try:
                            user_ids = list(dict.fromkeys(
                                int(row[0]) for row in await get_all_users()
                            ))
                            purchase_url = await product_purchase_link(_notifier_bot, item["id"])
                            if not user_ids:
                                error = "Пока нет пользователей для рассылки. Цена не изменена."
                            elif not purchase_url:
                                error = "Не удалось получить ссылку на товар. Цена не изменена."
                            else:
                                old_price = item["price"]
                                new_price = Decimal(str(confirmation["new_price"]))
                                price_changed = bool(confirmation.get("price_was_entered")) and new_price != old_price
                                if price_changed:
                                    async with Database().session() as session:
                                        update_result = await session.execute(
                                            sa_update(Goods)
                                            .where(
                                                Goods.id == item["id"],
                                                Goods.is_active.is_(True),
                                                Goods.price == item["base_price"],
                                            )
                                            .values(price=new_price)
                                        )
                                        if update_result.rowcount != 1:
                                            error = "Не удалось обновить цену: товар уже изменён. Проверьте его и повторите."
                                    if error is None:
                                        safe_create_task(invalidate_item_cache(item["name"]))
                                        safe_create_task(invalidate_stats_cache())
                                        await log_audit(
                                            "product_reminder_price_updated",
                                            resource_type="Товар",
                                            resource_id=str(item["id"]),
                                            details=(
                                                f"name={item['name']}, old_price={old_price}, "
                                                f"new_price={new_price}"
                                            ),
                                            ip_address=_client_ip(request),
                                        )

                                if error is None:
                                    text = build_reminder_text(
                                        item_name=item["name"],
                                        quantity=item["quantity"],
                                        is_infinite=item["is_infinite"],
                                        old_price=old_price,
                                        new_price=new_price,
                                        currency=str(getattr(EnvKeys, "PAY_CURRENCY", "RUB") or "RUB"),
                                    )
                                    markup = InlineKeyboardMarkup(inline_keyboard=[[
                                        InlineKeyboardButton(
                                            text="➡️ Перейти к товару",
                                            url=purchase_url,
                                        )
                                    ]])
                                    safe_create_task(_run_product_reminder_broadcast(
                                        _notifier_bot,
                                        user_ids,
                                        text,
                                        markup,
                                        _client_ip(request),
                                        item["name"],
                                    ))
                                    broadcast_scheduled = True
                                    await log_audit(
                                        "product_reminder_started",
                                        resource_type="Напоминание о товаре",
                                        resource_id=str(item["id"]),
                                        details=(
                                            f"name={item['name']}, recipients={len(user_ids)}, "
                                            f"price={new_price}, price_changed={price_changed}"
                                        ),
                                        ip_address=_client_ip(request),
                                    )
                                    result = (
                                        f"Напоминание о «{item['name']}» поставлено в рассылку "
                                        f"для {len(user_ids)} пользователей."
                                    )
                        finally:
                            if not broadcast_scheduled:
                                _web_broadcast_running = False
            else:
                error = "Неизвестное действие. Обновите страницу и попробуйте снова."

        rendered_items = [
            {
                **item,
                "price_display": format_reminder_money(item["price"]),
                "stock_display": "∞" if item["is_infinite"] else f'{item["quantity"]} шт.',
            }
            for item in items
        ]
        return await self.templates.TemplateResponse(
            request,
            "product_reminder.html",
            {
                "items": rendered_items,
                "selected_item_id": selected_item_id,
                "price": price_raw,
                "preview": preview,
                "error": error,
                "result": result,
                "currency": str(getattr(EnvKeys, "PAY_CURRENCY", "RUB") or "RUB"),
            },
        )


def _persist_pay_currency(currency: str) -> bool:
    """Update only ``PAY_CURRENCY`` in a local dotenv file when present.

    Render and other managed hosts normally provide environment variables from
    their dashboard, so the runtime value is always updated independently. A
    local ``.env`` is persisted atomically for convenient development restarts;
    no other setting (especially no secret) is read or rewritten.
    """
    env_path = Path(os.getenv("ENV_FILE", ".env"))
    if not env_path.is_file():
        return False

    temporary: Path | None = None
    try:
        original = env_path.read_text(encoding="utf-8")
        lines = original.splitlines(keepends=True)
        replaced = False
        updated_lines: list[str] = []
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("PAY_CURRENCY=") and not stripped.startswith("#"):
                newline = "\n" if line.endswith("\n") else ""
                updated_lines.append(f"PAY_CURRENCY={currency}{newline}")
                replaced = True
            else:
                updated_lines.append(line)
        if not replaced:
            if updated_lines and not updated_lines[-1].endswith("\n"):
                updated_lines[-1] += "\n"
            updated_lines.append(f"PAY_CURRENCY={currency}\n")

        temporary = env_path.with_name(f".{env_path.name}.{os.getpid()}.tmp")
        temporary.write_text("".join(updated_lines), encoding="utf-8")
        os.chmod(temporary, stat.S_IMODE(env_path.stat().st_mode))
        os.replace(temporary, env_path)
        return True
    except OSError:
        logger.warning("Unable to persist PAY_CURRENCY to %s", env_path, exc_info=True)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return False


class BotSettingsView(WebAccessMixin, BaseView):
    identity = "settings"
    required_perm = Permission.SETTINGS_MANAGE
    """Runtime settings that affect all newly rendered prices and invoices."""

    name = "Настройки бота"
    icon = "fa-solid fa-gear"

    @expose("/settings", methods=["GET", "POST"], identity="settings")
    async def settings(self, request: Request):
        error: str | None = None
        result: str | None = None
        current = str(getattr(EnvKeys, "PAY_CURRENCY", "RUB") or "RUB").upper()

        if request.method == "POST":
            form = await request.form()
            selected = str(form.get("pay_currency") or "").strip().upper()
            if selected not in PAY_CURRENCY_CODES:
                error = "Выберите валюту из списка."
            else:
                try:
                    EnvKeys.set_pay_currency(selected)
                except ValueError:
                    error = "Выберите валюту из списка."
                else:
                    current = selected
                    persisted = _persist_pay_currency(selected)
                    result = (
                        f"Валюта магазина изменена на {selected}. "
                        "Новые цены, балансы и платежи будут показываться в этой валюте. "
                        "CryptoBot получит сумму как фиатную и сам рассчитает эквивалент в криптовалюте."
                    )
                    if persisted:
                        result += " Значение сохранено в локальном .env."
                    else:
                        result += " На Render задайте PAY_CURRENCY в настройках сервиса и перезапустите бота."
                    await log_audit(
                        "settings_currency_changed",
                        resource_type="Настройки бота",
                        details=f"currency={selected}",
                        ip_address=_client_ip(request),
                    )

        return await self.templates.TemplateResponse(
            request,
            "settings.html",
            {
                "choices": PAY_CURRENCY_CHOICES,
                "current": current,
                "error": error,
                "result": result,
            },
        )


class UserSearchView(WebAccessMixin, BaseView):
    identity = "user-search"
    required_perm = Permission.USERS_MANAGE
    """Find a bot user by Telegram ID and show a clickable profile link."""

    name = "Поиск пользователя"
    icon = "fa-solid fa-magnifying-glass"

    @expose("/user-search", methods=["GET", "POST"], identity="user-search")
    async def user_search(self, request: Request):
        raw_id = request.query_params.get("telegram_id", "")
        if request.method == "POST":
            form = await request.form()
            raw_id = str(form.get("telegram_id") or "")

        raw_id = str(raw_id).strip()
        error: str | None = None
        user_result: dict[str, Any] | None = None
        telegram_id = _parse_telegram_id(raw_id) if raw_id else None

        if raw_id and telegram_id is None:
            error = "Введите положительный числовой Telegram ID."
        elif telegram_id is not None:
            async with Database().session() as session:
                user = (await session.execute(
                    sa_select(User).where(User.telegram_id == telegram_id)
                )).scalars().one_or_none()

            if user is None:
                error = "Пользователь с таким Telegram ID не найден в базе бота."
            else:
                profile = await _telegram_profile(telegram_id)
                user_result = {
                    "telegram_id": telegram_id,
                    "display_name": profile["display_name"],
                    "username": profile["username"],
                    "profile_url": profile["profile_url"],
                    "balance": getattr(user, "balance", None),
                    "is_blocked": bool(getattr(user, "is_blocked", False)),
                }
                await log_audit(
                    "web_user_search",
                    user_id=telegram_id,
                    resource_type="User",
                    details=f"found={user_result['username'] or user_result['display_name'] or 'profile-link'}",
                    ip_address=_client_ip(request),
                )

        return await self.templates.TemplateResponse(
            request,
            "user_search.html",
            {"telegram_id": raw_id, "error": error, "user_result": user_result},
        )


def _format_revenue_money(value: Any) -> str:
    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        amount = Decimal("0")
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


class RevenueView(WebAccessMixin, BaseView):
    identity = "revenue"
    required_perm = Permission.STATS_VIEW
    """Revenue dashboard with real purchases and separately marked manual entries."""

    name = "Доходы"
    icon = "fa-solid fa-chart-line"

    async def _load_report(self, period_days: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[Any], date]:
        today = moscow_today()
        period_days = parse_revenue_period(period_days)
        period_start, query_end = revenue_window(today, period_days)
        query_start = period_start
        utc_start = datetime.combine(query_start, datetime.min.time(), tzinfo=MOSCOW_TZ).astimezone(timezone.utc)
        utc_end = datetime.combine(query_end, datetime.min.time(), tzinfo=MOSCOW_TZ).astimezone(timezone.utc)

        async with Database().session() as session:
            actual_rows = (await session.execute(
                sa_select(
                    BoughtGoods.item_name,
                    BoughtGoods.price,
                    BoughtGoods.buyer_id,
                    BoughtGoods.bought_datetime,
                ).where(
                    BoughtGoods.bought_datetime >= utc_start,
                    BoughtGoods.bought_datetime < utc_end,
                )
            )).all()
            manual_rows = (await session.execute(
                sa_select(
                    ManualRevenue.category_name,
                    ManualRevenue.quantity,
                    ManualRevenue.unit_price,
                    ManualRevenue.created_at,
                ).where(
                    ManualRevenue.created_at >= utc_start,
                    ManualRevenue.created_at < utc_end,
                )
            )).all()
            category_rows = (await session.execute(
                sa_select(Goods.name, Categories.name).join(
                    Categories, Categories.id == Goods.category_id
                )
            )).all()
            manual_history = (await session.execute(
                sa_select(ManualRevenue)
                .where(
                    ManualRevenue.created_at >= utc_start,
                    ManualRevenue.created_at < utc_end,
                )
                .order_by(ManualRevenue.created_at.desc(), ManualRevenue.id.desc())
                .limit(20)
            )).scalars().all()

        manual_history = [
            row for row in manual_history
            if not is_excluded_revenue_entry_date(row.created_at)
        ]

        category_by_item: dict[str, str] = {}
        for item_name, category_name in category_rows:
            category_by_item[str(item_name).strip()] = str(category_name)

        report = build_revenue_report(
            actual_rows,
            manual_rows,
            category_by_item,
            period_start=period_start,
            days=period_days,
        )
        # There is no trustworthy pre-launch baseline after excluding the old
        # test rows, so never expose a fabricated percentage comparison.
        report["summary"]["change_percent"] = None
        return report, category_rows, manual_history, today

    @expose("/revenue", methods=["GET", "POST"], identity="revenue")
    async def revenue(self, request: Request):
        if request.query_params.get("section") == "bot_stats":
            return await self._bot_statistics(request)

        period_days = parse_revenue_period(request.query_params.get("days"))
        error: str | None = None
        result: str | None = None
        selected_category_id = ""
        quantity = ""
        unit_price = ""

        async with Database().session() as session:
            category_rows = (await session.execute(
                sa_select(Categories.id, Categories.name)
                .where(Categories.is_active.is_(True), ~Categories.children.any())
                .order_by(Categories.name)
            )).all()

        if request.method == "POST":
            form = await request.form()
            selected_category_id = str(form.get("category_id") or "").strip()
            quantity = str(form.get("quantity") or "").strip()
            unit_price = str(form.get("unit_price") or "").strip()
            period_days = parse_revenue_period(form.get("days"))
            caller_perms = await resolve_web_perms(request)
            if not Permission.granted(int(caller_perms or 0), Permission.BALANCE_MANAGE):
                error = "Для ручного добавления выручки нужно право управления балансом."
            else:
                try:
                    category_id, parsed_quantity, parsed_price = parse_manual_revenue_fields(
                        selected_category_id, quantity, unit_price
                    )
                    category_name = next(
                        (str(row.name) for row in category_rows if int(row.id) == category_id),
                        None,
                    )
                    if category_name is None:
                        raise RevenueInputError("Выберите существующую категорию.")
                    async with Database().session() as session:
                        session.add(ManualRevenue(
                            category_name=category_name,
                            quantity=parsed_quantity,
                            unit_price=parsed_price,
                            created_by=str(request.session.get("web_login") or "")[:128] or None,
                        ))
                    await log_audit(
                        "manual_revenue_added",
                        resource_type="Доходы",
                        details=(
                            f"category={category_name}, quantity={parsed_quantity}, "
                            f"unit_price={parsed_price}, total={parsed_quantity * parsed_price}"
                        ),
                        ip_address=_client_ip(request),
                    )
                    result = (
                        f"Добавлено: {parsed_quantity} шт. × {_format_revenue_money(parsed_price)} "
                        f"= {_format_revenue_money(parsed_quantity * parsed_price)} {EnvKeys.PAY_CURRENCY}."
                    )
                    selected_category_id = quantity = unit_price = ""
                except RevenueInputError as exc:
                    error = str(exc)

        report, _category_rows, manual_history, today = await self._load_report(period_days)
        max_daily = max((row["revenue"] for row in report["daily"]), default=Decimal("0"))
        summary = report["summary"]
        for row in report["daily"]:
            row["label"] = row["date"].strftime("%d.%m")
            row["revenue_display"] = _format_revenue_money(row["revenue"])
            row["units_display"] = f"{row['units']:,}".replace(",", " ")
            row["bar_width"] = int((row["revenue"] / max_daily) * 100) if max_daily else 0
        for row in report["categories"]:
            row["revenue_display"] = _format_revenue_money(row["revenue"])
            row["share"] = int((row["revenue"] / summary["revenue"]) * 100) if summary["revenue"] else 0
        for row in report["products"]:
            row["revenue_display"] = _format_revenue_money(row["revenue"])
        for row in manual_history:
            row.total_display = _format_revenue_money(row.quantity * row.unit_price)
            row.unit_price_display = _format_revenue_money(row.unit_price)
            row.created_display = _format_moscow_datetime(row, "created_at")

        change_text = f"За выбранный период: {REVENUE_PERIOD_OPTIONS[period_days].lower()}"
        change_class = "text-muted"

        categories = [
            {"id": int(row.id), "name": str(row.name)} for row in category_rows
        ]
        period_start, _query_end = revenue_window(today, period_days)
        return await self.templates.TemplateResponse(
            request,
            "revenue.html",
            {
                "period_days": period_days,
                "period_options": REVENUE_PERIOD_OPTIONS,
                "period_label": REVENUE_PERIOD_OPTIONS[period_days],
                "period_start": period_start.strftime("%d.%m.%Y"),
                "period_end": today.strftime("%d.%m.%Y"),
                "summary": summary,
                "summary_revenue": _format_revenue_money(summary["revenue"]),
                "summary_actual_revenue": _format_revenue_money(summary["actual_revenue"]),
                "summary_manual_revenue": _format_revenue_money(summary["manual_revenue"]),
                "summary_previous_revenue": _format_revenue_money(summary["previous_revenue"]),
                "change_text": change_text,
                "change_class": change_class,
                "daily": report["daily"],
                "categories_report": report["categories"][:10],
                "products_report": report["products"][:10],
                "manual_history": manual_history,
                "categories": categories,
                "selected_category_id": selected_category_id,
                "quantity": quantity,
                "unit_price": unit_price,
                "error": error,
                "result": result,
                "can_add_manual": Permission.granted(
                    int(await resolve_web_perms(request) or 0), Permission.BALANCE_MANAGE
                ),
                "currency": str(getattr(EnvKeys, "PAY_CURRENCY", "RUB") or "RUB"),
            },
        )

    async def _bot_statistics(self, request: Request):
        period_days = parse_bot_stats_period(request.query_params.get("days"))
        today = moscow_today()
        report = await load_bot_statistics(today=today, days=period_days)
        daily = report["daily"]
        for row in daily:
            row["label"] = row["date"].strftime("%d.%m")
        summary = report["summary"]
        return await self.templates.TemplateResponse(
            request,
            "bot_statistics.html",
            {
                "active_section": "bot_stats",
                "period_days": period_days,
                "period_options": BOT_STATS_PERIOD_OPTIONS,
                "period_start": report["period_start"].strftime("%d.%m.%Y"),
                "period_end": report["period_end"].strftime("%d.%m.%Y"),
                "summary": summary,
                "daily": daily,
            },
        )


# Expense Ledger
class ExpensesView(WebAccessMixin, BaseView):
    """Append-only expense ledger and per-period purchase cost statistics."""

    identity = "expenses"
    required_perm = Permission.STATS_VIEW
    name = "Расходы"
    icon = "fa-solid fa-money-bill-transfer"
    _flash_key = "expenses_flash"

    async def _load_report(
        self, period_days: int
    ) -> tuple[dict[str, Any], list[Any], list[Any], date, date]:
        today = moscow_today()
        period_days = parse_revenue_period(period_days)
        period_start, query_end = revenue_window(today, period_days)
        utc_start = datetime.combine(
            period_start, datetime.min.time(), tzinfo=MOSCOW_TZ
        ).astimezone(timezone.utc)
        utc_end = datetime.combine(
            query_end, datetime.min.time(), tzinfo=MOSCOW_TZ
        ).astimezone(timezone.utc)

        async with Database().session() as session:
            rows = (await session.execute(
                sa_select(ProductExpense)
                .where(
                    ProductExpense.created_at >= utc_start,
                    ProductExpense.created_at < utc_end,
                )
                .order_by(ProductExpense.created_at.desc(), ProductExpense.id.desc())
            )).scalars().all()
            products = (await session.execute(
                sa_select(Goods.id, Goods.name)
                .where(Goods.is_active.is_(True))
                .order_by(Goods.name, Goods.id)
            )).all()

        report = build_expense_report(
            rows, period_start=period_start, days=period_days
        )
        return report, list(rows[:20]), list(products), today, period_start

    async def _load_inventory_forecast(self, today: date) -> dict[str, Any]:
        finite_count = func.sum(case((ItemValues.is_infinity.is_(False), 1), else_=0))
        has_infinite = func.max(case((ItemValues.is_infinity.is_(True), 1), else_=0))
        utc_start = datetime.combine(
            today, datetime.min.time(), tzinfo=MOSCOW_TZ
        ).astimezone(timezone.utc)
        utc_end = datetime.combine(
            today + timedelta(days=1), datetime.min.time(), tzinfo=MOSCOW_TZ
        ).astimezone(timezone.utc)
        async with Database().session() as session:
            stock_rows = (await session.execute(
                sa_select(
                    Goods,
                    (func.coalesce(finite_count, 0) + Goods.stock_quantity).label("quantity"),
                    has_infinite.label("is_infinite"),
                )
                .join(Categories, Categories.id == Goods.category_id)
                .outerjoin(ItemValues, ItemValues.item_id == Goods.id)
                .where(Goods.is_active.is_(True), Categories.is_active.is_(True))
                .group_by(Goods.id)
                .order_by(Goods.name, Goods.id)
            )).all()
            expense_cost_rows = (await session.execute(
                sa_select(
                    ProductExpense.product_id,
                    func.sum(ProductExpense.quantity).label("quantity"),
                    func.sum(ProductExpense.total_cost).label("total_cost"),
                )
                .where(
                    ProductExpense.product_id.is_not(None),
                    ProductExpense.created_at >= utc_start,
                    ProductExpense.created_at < utc_end,
                )
                .group_by(ProductExpense.product_id)
            )).all()

        prepared_stock = []
        for row in stock_rows:
            goods = row[0]
            try:
                sale_price, _on_sale, _original_price = effective_price(goods)
            except ValueError:
                logger.warning("Skipping product with invalid price in expense forecast: %s", goods.id)
                continue
            prepared_stock.append({
                "id": int(goods.id),
                "name": str(goods.name),
                "quantity": int(row.quantity or 0),
                "is_infinite": bool(row.is_infinite),
                "sale_price": sale_price,
            })
        return build_inventory_forecast(prepared_stock, expense_cost_rows)

    @expose("/expenses", methods=["GET", "POST"], identity="expenses")
    async def expenses(self, request: Request):
        period_days = parse_revenue_period(request.query_params.get("days"))
        error: str | None = None
        result: str | None = None
        selected_product_id = ""
        quantity = ""
        amount = ""
        cost_mode = "unit"

        if request.method == "POST":
            form = await request.form()
            selected_product_id = str(form.get("product_id") or "").strip()
            quantity = str(form.get("quantity") or "").strip()
            amount = str(form.get("amount") or "").strip()
            cost_mode = str(form.get("cost_mode") or "unit").strip()
            period_days = parse_revenue_period(form.get("days"))

            caller_perms = await resolve_web_perms(request)
            if not Permission.granted(int(caller_perms or 0), Permission.CATALOG_MANAGE):
                error = "Для добавления расхода нужно право управления каталогом."
            else:
                try:
                    product_id, parsed_quantity, total_cost = parse_expense_fields(
                        selected_product_id, quantity, amount, cost_mode
                    )
                    async with Database().session() as session:
                        product = (await session.execute(
                            sa_select(Goods).where(
                                Goods.id == product_id,
                                Goods.is_active.is_(True),
                            )
                        )).scalars().first()
                        if product is None:
                            raise ExpenseInputError("Выберите существующий активный товар.")

                        expense = ProductExpense(
                            product_id=int(product.id),
                            product_name=str(product.name),
                            quantity=parsed_quantity,
                            total_cost=total_cost,
                            created_by=str(request.session.get("web_login") or "")[:128] or None,
                        )
                        session.add(expense)
                        await session.flush()
                        expense_id = int(expense.id)
                        product_name = str(product.name)

                    await log_audit(
                        "product_expense_added",
                        resource_type="Расход",
                        resource_id=str(expense_id),
                        details=(
                            f"product={product_name}, product_id={product_id}, "
                            f"quantity={parsed_quantity}, total_cost={total_cost}"
                        ),
                        ip_address=_client_ip(request),
                    )
                    unit_cost = (total_cost / parsed_quantity).quantize(Decimal("0.01"))
                    request.session[self._flash_key] = (
                        f"Расход записан: {product_name}, {parsed_quantity} шт. × "
                        f"{_format_revenue_money(unit_cost)} = "
                        f"{_format_revenue_money(total_cost)} {EnvKeys.PAY_CURRENCY}."
                    )
                    return RedirectResponse(
                        url=f"/admin/expenses?days={period_days}", status_code=303
                    )
                except ExpenseInputError as exc:
                    error = str(exc)
        else:
            result = request.session.pop(self._flash_key, None)

        report, history, product_rows, today, period_start = await self._load_report(period_days)
        inventory_forecast = await self._load_inventory_forecast(today)
        summary = report["summary"]
        max_daily = max((row["total_cost"] for row in report["daily"]), default=Decimal("0"))
        for row in report["daily"]:
            row["label"] = row["date"].strftime("%d.%m")
            row["total_cost_display"] = _format_revenue_money(row["total_cost"])
            row["quantity_display"] = f"{row['quantity']:,}".replace(",", " ")
            row["bar_width"] = int((row["total_cost"] / max_daily) * 100) if max_daily else 0
        for row in report["products"]:
            row["total_cost_display"] = _format_revenue_money(row["total_cost"])
            row["average_unit_cost_display"] = _format_revenue_money(row["average_unit_cost"])
        for row in history:
            row.total_cost_display = _format_revenue_money(row.total_cost)
            row.average_unit_cost_display = _format_revenue_money(
                Decimal(str(row.total_cost)) / row.quantity
            )
            row.created_display = _format_moscow_datetime(row, "created_at")

        inventory_summary = inventory_forecast["summary"]
        inventory_money_keys = (
            "potential_revenue", "estimated_cost", "estimated_revenue_with_cost",
            "estimated_profit", "unpriced_revenue",
        )
        for key in inventory_money_keys:
            inventory_summary[f"{key}_display"] = _format_revenue_money(
                inventory_summary[key]
            )
        for row in inventory_forecast["items"]:
            for key in ("sale_price", "potential_revenue", "average_unit_cost", "estimated_cost", "estimated_profit"):
                row[f"{key}_display"] = (
                    _format_revenue_money(row[key]) if row[key] is not None else None
                )

        return await self.templates.TemplateResponse(
            request,
            "expenses.html",
            {
                "period_days": period_days,
                "period_options": REVENUE_PERIOD_OPTIONS,
                "period_label": REVENUE_PERIOD_OPTIONS[period_days],
                "period_start": period_start.strftime("%d.%m.%Y"),
                "period_end": today.strftime("%d.%m.%Y"),
                "summary": summary,
                "summary_total_cost": _format_revenue_money(summary["total_cost"]),
                "summary_average_unit_cost": _format_revenue_money(summary["average_unit_cost"]),
                "daily": report["daily"],
                "products_report": report["products"][:20],
                "inventory_forecast": inventory_forecast,
                "inventory_summary": inventory_summary,
                "inventory_items": inventory_forecast["items"],
                "history": history,
                "products": [
                    {"id": int(row.id), "name": str(row.name)} for row in product_rows
                ],
                "selected_product_id": selected_product_id,
                "quantity": quantity,
                "amount": amount,
                "cost_mode": cost_mode,
                "error": error,
                "result": result,
                "can_add_expense": Permission.granted(
                    int(await resolve_web_perms(request) or 0), Permission.CATALOG_MANAGE
                ),
                "currency": str(getattr(EnvKeys, "PAY_CURRENCY", "RUB") or "RUB"),
            },
        )


class FinanceView(WebAccessMixin, BaseView):
    """A reconciled view of sales, top-ups, external cash, costs, and liabilities."""

    identity = "finance"
    required_perm = Permission.STATS_VIEW
    name = "Финансы"
    icon = "fa-solid fa-scale-balanced"
    _flash_key = "finance_flash"

    async def _load(self, days: int) -> dict[str, Any]:
        today = moscow_today()
        days = parse_finance_period(days)
        start_day, query_end = finance_window(today, days)
        utc_start = datetime.combine(start_day, datetime.min.time(), tzinfo=MOSCOW_TZ).astimezone(timezone.utc)
        utc_end = datetime.combine(query_end, datetime.min.time(), tzinfo=MOSCOW_TZ).astimezone(timezone.utc)

        async with Database().session() as session:
            sales = (await session.execute(sa_select(
                BoughtGoods.item_name, BoughtGoods.price, BoughtGoods.bought_datetime
            ).where(BoughtGoods.bought_datetime >= utc_start, BoughtGoods.bought_datetime < utc_end))).all()
            payments = (await session.execute(sa_select(
                Payments.provider, Payments.currency, Payments.amount, Payments.created_at
            ).where(
                Payments.status == "succeeded",
                Payments.created_at >= utc_start,
                Payments.created_at < utc_end,
            ))).all()
            receipts = (await session.execute(sa_select(FinanceReceipt).where(
                FinanceReceipt.received_at >= utc_start,
                FinanceReceipt.received_at < utc_end,
            ).order_by(FinanceReceipt.received_at.desc(), FinanceReceipt.id.desc()))).scalars().all()
            expenses = (await session.execute(sa_select(ProductExpense).where(
                ProductExpense.created_at >= utc_start,
                ProductExpense.created_at < utc_end,
            ).order_by(ProductExpense.created_at.desc(), ProductExpense.id.desc()))).scalars().all()
            allocated_rows = (await session.execute(sa_select(
                ProductExpense.finance_receipt_id, func.coalesce(func.sum(ProductExpense.total_cost), 0)
            ).where(ProductExpense.finance_receipt_id.is_not(None)).group_by(ProductExpense.finance_receipt_id))).all()
            known_cost_names = (await session.execute(sa_select(ProductExpense.product_name).distinct())).scalars().all()
            products = (await session.execute(sa_select(Goods.id, Goods.name).where(
                Goods.is_active.is_(True)
            ).order_by(Goods.name, Goods.id))).all()
            total_user_balance = await session.scalar(sa_select(func.coalesce(func.sum(User.balance), 0)))
            all_receipts_rub = await session.scalar(sa_select(func.coalesce(func.sum(FinanceReceipt.amount_rub), 0)))
            all_allocated_rub = await session.scalar(sa_select(func.coalesce(func.sum(ProductExpense.total_cost), 0)).where(
                ProductExpense.finance_receipt_id.is_not(None)
            ))
            recent_receipts = (await session.execute(sa_select(FinanceReceipt).order_by(
                FinanceReceipt.received_at.desc(), FinanceReceipt.id.desc()
            ).limit(40))).scalars().all()
            recent_expenses = (await session.execute(sa_select(ProductExpense).order_by(
                ProductExpense.created_at.desc(), ProductExpense.id.desc()
            ).limit(40))).scalars().all()

        receipt_allocated = {int(receipt_id): Decimal(str(total or 0)) for receipt_id, total in allocated_rows}
        receipt_total = sum((Decimal(str(row.amount_rub)) for row in receipts), Decimal("0"))
        expense_total = sum((Decimal(str(row.total_cost)) for row in expenses), Decimal("0"))
        sales_total = sum((Decimal(str(row.price)) for row in sales), Decimal("0"))
        payment_totals: dict[tuple[str, str], dict[str, Any]] = {}
        for provider, currency, amount, _moment in payments:
            key = (str(provider), str(currency).upper())
            group = payment_totals.setdefault(key, {"provider": key[0], "currency": key[1], "amount": Decimal("0"), "count": 0})
            group["amount"] += Decimal(str(amount))
            group["count"] += 1
        for group in payment_totals.values():
            group["amount_display"] = _format_revenue_money(group["amount"])

        receipt_events = [(row.received_at, row.amount_rub) for row in receipts]
        daily = aggregate_daily_cash_events(
            [(row.bought_datetime, row.price) for row in sales],
            receipt_events,
            [(row.created_at, row.total_cost) for row in expenses],
        )
        for row in daily:
            for key in ("sales", "receipts", "expenses", "cash_delta"):
                row[f"{key}_display"] = _format_revenue_money(row[key])
            row["date_display"] = row["date"].strftime("%d.%m.%Y")

        product_stats: dict[str, dict[str, Any]] = {}
        known_names = {str(name).strip().casefold() for name in known_cost_names}
        for row in sales:
            key = str(row.item_name).strip().casefold()
            product = product_stats.setdefault(key, {"name": str(row.item_name), "units": 0, "sales": Decimal("0")})
            product["units"] += 1
            product["sales"] += Decimal(str(row.price))
        for product in product_stats.values():
            product["sales_display"] = _format_revenue_money(product["sales"])
            product["cost_status"] = (
                "Есть записи закупок, но продажи не связаны с партией"
                if product["name"].strip().casefold() in known_names
                else "Себестоимость не записана"
            )
            product["cost_missing"] = product["name"].strip().casefold() not in known_names

        for product in product_stats.values():
            product["units_display"] = f"{product['units']:,}".replace(",", " ")

        for row in receipts:
            allocated = receipt_allocated.get(int(row.id), Decimal("0"))
            row.amount_display = f"{_format_revenue_money(row.amount)} {row.currency}"
            row.amount_rub_display = _format_revenue_money(row.amount_rub)
            row.allocated_display = _format_revenue_money(allocated)
            row.remaining = Decimal(str(row.amount_rub)) - allocated
            row.remaining_display = _format_revenue_money(row.remaining)
            row.received_display = _format_moscow_datetime(row, "received_at")
        for row in recent_receipts:
            allocated = receipt_allocated.get(int(row.id), Decimal("0"))
            row.amount_display = f"{_format_revenue_money(row.amount)} {row.currency}"
            row.amount_rub_display = _format_revenue_money(row.amount_rub)
            row.remaining = Decimal(str(row.amount_rub)) - allocated
            row.remaining_display = _format_revenue_money(row.remaining)
            row.received_display = _format_moscow_datetime(row, "received_at")
        for row in expenses:
            row.total_cost_display = _format_revenue_money(row.total_cost)
            row.created_display = _format_moscow_datetime(row, "created_at")
        for row in recent_expenses:
            row.total_cost_display = _format_revenue_money(row.total_cost)
            row.created_display = _format_moscow_datetime(row, "created_at")

        return {
            "days": days,
            "today": today,
            "start_day": start_day,
            "sales_total": sales_total,
            "sales_total_display": _format_revenue_money(sales_total),
            "sales_count": len(sales),
            "payment_groups": sorted(payment_totals.values(), key=lambda row: (row["provider"], row["currency"])),
            "receipt_total": receipt_total,
            "receipt_total_display": _format_revenue_money(receipt_total),
            "receipt_count": len(receipts),
            "expense_total": expense_total,
            "expense_total_display": _format_revenue_money(expense_total),
            "expense_count": len(expenses),
            "cash_delta": receipt_total - expense_total,
            "unallocated_total": max(
                Decimal("0"), Decimal(str(all_receipts_rub or 0)) - Decimal(str(all_allocated_rub or 0))
            ),
            "unallocated_total_display": _format_revenue_money(max(
                Decimal("0"), Decimal(str(all_receipts_rub or 0)) - Decimal(str(all_allocated_rub or 0))
            )),
            "unfunded_expenses_count": sum(1 for row in expenses if row.finance_receipt_id is None),
            "total_user_balance": Decimal(str(total_user_balance or 0)),
            "total_user_balance_display": _format_revenue_money(total_user_balance),
            "sales": sorted(product_stats.values(), key=lambda row: row["sales"], reverse=True),
            "daily": daily,
            "receipts": receipts,
            "receipt_options": [row for row in recent_receipts if row.remaining > 0],
            "expenses": expenses,
            "products": [{"id": int(row.id), "name": str(row.name)} for row in products],
        }

    @expose("/finance", methods=["GET", "POST"], identity="finance")
    async def finance(self, request: Request):
        days = parse_finance_period(request.query_params.get("days"))
        error = None
        result = request.session.pop(self._flash_key, None) if request.method == "GET" else None
        caller_perms = int(await resolve_web_perms(request) or 0)
        can_edit = Permission.granted(caller_perms, Permission.OWN)

        if request.method == "POST":
            form = await request.form()
            days = parse_finance_period(form.get("days"))
            action = str(form.get("action") or "").strip()
            if not can_edit:
                error = "Добавлять финансовые записи может только владелец."
            else:
                try:
                    async with Database().session() as session:
                        if action == "receipt":
                            fields = parse_finance_receipt_fields(
                                form.get("source"), form.get("user_id"), form.get("currency"),
                                form.get("amount"), form.get("amount_rub"), form.get("received_date"),
                                form.get("reference"), form.get("note"),
                            )
                            if fields["user_id"] is not None and await session.get(User, fields["user_id"]) is None:
                                raise FinanceInputError("Пользователь с таким Telegram ID не найден.")
                            if fields["reference"] and (await session.execute(sa_select(FinanceReceipt.id).where(
                                FinanceReceipt.source == fields["source"],
                                FinanceReceipt.reference == fields["reference"],
                            ))).first():
                                raise FinanceInputError("Поступление с таким источником и номером операции уже записано.")
                            entry = FinanceReceipt(
                                **fields,
                                created_by=str(request.session.get("web_login") or "")[:128] or None,
                            )
                            session.add(entry)
                            await session.flush()
                            resource_id = str(entry.id)
                            audit_action = "finance_receipt_added"
                            details = f"source={entry.source}, user_id={entry.user_id}, currency={entry.currency}, amount={entry.amount}, amount_rub={entry.amount_rub}, reference={entry.reference}"
                            result = "Поступление записано только в финансовый учёт; баланс пользователя и выручка не менялись."
                        elif action == "expense":
                            product_id, quantity, total_cost = parse_expense_fields(
                                form.get("product_id"), form.get("quantity"), form.get("amount"), form.get("cost_mode") or "unit"
                            )
                            product = (await session.execute(sa_select(Goods).where(
                                Goods.id == product_id, Goods.is_active.is_(True)
                            ))).scalars().first()
                            if product is None:
                                raise FinanceInputError("Выберите существующий активный товар.")
                            raw_receipt_id = str(form.get("receipt_id") or "").strip()
                            receipt = None
                            if raw_receipt_id:
                                try:
                                    receipt_id = int(raw_receipt_id)
                                except ValueError as exc:
                                    raise FinanceInputError("Выберите поступление из списка.") from exc
                                receipt = (await session.execute(sa_select(FinanceReceipt).where(
                                    FinanceReceipt.id == receipt_id
                                ).with_for_update())).scalars().first()
                                if receipt is None:
                                    raise FinanceInputError("Источник поступления не найден.")
                                allocated = await session.scalar(sa_select(func.coalesce(func.sum(ProductExpense.total_cost), 0)).where(
                                    ProductExpense.finance_receipt_id == receipt_id
                                ))
                                validate_receipt_allocation(total_cost, receipt.amount_rub, allocated or 0)
                            expense = ProductExpense(
                                product_id=int(product.id), product_name=str(product.name), quantity=quantity,
                                total_cost=total_cost,
                                finance_receipt_id=int(receipt.id) if receipt else None,
                                created_by=str(request.session.get("web_login") or "")[:128] or None,
                            )
                            session.add(expense)
                            await session.flush()
                            resource_id = str(expense.id)
                            audit_action = "finance_expense_added"
                            details = f"product={product.name}, quantity={quantity}, total_cost={total_cost}, receipt_id={receipt.id if receipt else None}"
                            result = "Расход записан. Склад, цена товара и баланс покупателя не изменялись."
                        else:
                            raise FinanceInputError("Неизвестный тип финансовой записи.")
                    await log_audit(
                        audit_action, resource_type="Финансы", resource_id=resource_id,
                        details=details, ip_address=_client_ip(request),
                    )
                    request.session[self._flash_key] = result
                    return RedirectResponse(
                        url=f"/admin/finance?days={days}", status_code=303
                    )
                except FinanceInputError as exc:
                    error = str(exc)
                except IntegrityError:
                    error = "Запись не сохранена: проверьте номер операции — возможно, он уже внесён."

        data = await self._load(days)
        return await self.templates.TemplateResponse(
            request, "finance.html", {**data, "period_options": FINANCE_PERIOD_OPTIONS,
            "other_days": days if days != 1 else REVENUE_DAYS,
            "currency": str(getattr(EnvKeys, "PAY_CURRENCY", "RUB") or "RUB"),
            "can_edit": can_edit, "error": error, "result": result},
        )


def _procurement_daily_query():
    """Aggregate plan totals and item counts separately to avoid join fan-out.

    Keeping each aggregate in its own subquery is portable to PostgreSQL's
    strict GROUP BY rules and prevents plan totals being multiplied by lines.
    """
    items_by_date = (
        sa_select(
            ProcurementPlan.plan_date.label("plan_date"),
            func.count(ProcurementPlanItem.id).label("item_count"),
        )
        .join(
            ProcurementPlanItem,
            ProcurementPlanItem.plan_id == ProcurementPlan.id,
        )
        .group_by(ProcurementPlan.plan_date)
        .subquery()
    )
    plans_by_date = (
        sa_select(
            ProcurementPlan.plan_date.label("plan_date"),
            func.count(ProcurementPlan.id).label("plan_count"),
            func.sum(ProcurementPlan.total_cost).label("total_cost"),
            func.sum(ProcurementPlan.expected_revenue).label("expected_revenue"),
            func.sum(ProcurementPlan.gross_profit).label("gross_profit"),
        )
        .group_by(ProcurementPlan.plan_date)
        .subquery()
    )
    return (
        sa_select(
            plans_by_date.c.plan_date,
            plans_by_date.c.plan_count,
            func.coalesce(items_by_date.c.item_count, 0).label("item_count"),
            plans_by_date.c.total_cost,
            plans_by_date.c.expected_revenue,
            plans_by_date.c.gross_profit,
        )
        .outerjoin(
            items_by_date,
            items_by_date.c.plan_date == plans_by_date.c.plan_date,
        )
        .order_by(plans_by_date.c.plan_date.desc())
        .limit(90)
    )


class ProcurementControlView(WebAccessMixin, BaseView):
    """Persisted profit forecasts that never alter inventory or real expenses."""

    identity = "procurement-control"
    required_perm = Permission.STATS_VIEW
    name = "Контроль закупок"
    icon = "fa-solid fa-cart-shopping"

    async def _catalog(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        async with Database().session() as session:
            categories = (await session.execute(
                sa_select(Categories)
                .where(Categories.is_active.is_(True))
                .order_by(Categories.sort_order, Categories.name, Categories.id)
            )).scalars().all()
            active_parent_ids = {int(category.parent_id) for category in categories if category.parent_id is not None}
            leaves = [category for category in categories if int(category.id) not in active_parent_ids]
            leaf_ids = {int(category.id) for category in leaves}
            leaves_by_id = {int(category.id): category for category in leaves}
            goods = (await session.execute(
                sa_select(Goods)
                .where(Goods.is_active.is_(True), Goods.category_id.in_(leaf_ids or {-1}))
                .order_by(Goods.sort_order, Goods.name, Goods.id)
            )).scalars().all()

            category_options = [
                {"id": int(category.id), "name": str(category.name)}
                for category in leaves
            ]
            products = []
            for goods_row in goods:
                price, _on_sale, _original_price = effective_price(goods_row)
                category = leaves_by_id.get(int(goods_row.category_id))
                if category is None:
                    continue
                products.append({
                    "id": int(goods_row.id),
                    "category_id": int(goods_row.category_id),
                    "category_name": str(category.name),
                    "name": str(goods_row.name),
                    "current_price": Decimal(str(price)).quantize(Decimal("0.01")),
                })
        return category_options, products

    async def _history(
        self, page: int
    ) -> tuple[list[Any], list[dict[str, Any]], int, int, int]:
        page_size = 50
        async with Database().session() as session:
            total_plans = int(await session.scalar(
                sa_select(func.count(ProcurementPlan.id))
            ) or 0)
            page_count = max(1, (total_plans + page_size - 1) // page_size)
            page = min(max(1, page), page_count)
            plans = (await session.execute(
                sa_select(ProcurementPlan)
                .order_by(ProcurementPlan.plan_date.desc(), ProcurementPlan.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )).scalars().all()
            plan_ids = [int(plan.id) for plan in plans]
            item_rows = []
            if plan_ids:
                item_rows = (await session.execute(
                    sa_select(ProcurementPlanItem)
                    .where(ProcurementPlanItem.plan_id.in_(plan_ids))
                    .order_by(ProcurementPlanItem.id)
                )).scalars().all()
            daily_rows = (await session.execute(_procurement_daily_query())).all()

        items_by_plan: dict[int, list[Any]] = {}
        for item in item_rows:
            items_by_plan.setdefault(int(item.plan_id), []).append(item)
        for plan in plans:
            plan.display_items = items_by_plan.get(int(plan.id), [])
            plan.created_display = _format_moscow_datetime(plan, "created_at")
            plan.date_display = plan.plan_date.strftime("%d.%m.%Y")
            for field in ("total_cost", "expected_revenue", "gross_profit"):
                setattr(plan, f"{field}_display", _format_revenue_money(getattr(plan, field)))
            for item in plan.display_items:
                item.total_cost_display = _format_revenue_money(item.total_cost)
                item.expected_revenue_display = _format_revenue_money(item.expected_revenue)
                item.unit_cost_display = _format_revenue_money(item.unit_cost)
                item.sale_price_display = _format_revenue_money(item.sale_price)

        daily_report = build_daily_procurement_report(daily_rows)
        daily = []
        max_profit = max(
            (abs(row["gross_profit"]) for row in daily_report),
            default=Decimal("0"),
        )
        for row in daily_report:
            gross_profit = row["gross_profit"]
            daily.append({
                "date": row["date"],
                "date_display": row["date"].strftime("%d.%m.%Y"),
                "plans": row["plans"],
                "items": row["items"],
                "total_cost_display": _format_revenue_money(row["total_cost"]),
                "expected_revenue_display": _format_revenue_money(row["expected_revenue"]),
                "gross_profit_display": _format_revenue_money(gross_profit),
                "profit_bar_width": int(abs(gross_profit) / max_profit * 100) if max_profit else 0,
                "is_loss": gross_profit < 0,
            })
        return list(plans), daily, page, page_count, total_plans

    def _draft_rows(self, form: Any | None) -> list[dict[str, str]]:
        names = ("category_id", "product_id", "quantity", "unit_cost", "sale_mode", "custom_sale_price")
        if form is None:
            return [{name: "" for name in names}]
        getter = getattr(form, "getlist", None)
        columns: dict[str, list[Any]] = {}
        for name in names:
            value = getter(name) if callable(getter) else form.get(name, [])
            if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
                value = [value]
            columns[name] = list(value)[:50]
        count = max((len(value) for value in columns.values()), default=0)
        return [
            {name: str(columns[name][index] or "") if index < len(columns[name]) else ""
             for name in names}
            for index in range(min(count, 50))
        ] or [{name: "" for name in names}]

    async def _render(
        self,
        request: Request,
        *,
        error: str | None,
        form: Any | None,
        can_save: bool,
        success: str | None = None,
    ):
        categories, products = await self._catalog()
        try:
            requested_page = max(1, int(request.query_params.get("page", "1")))
        except (TypeError, ValueError):
            requested_page = 1
        history, daily, history_page, history_pages, history_total = await self._history(requested_page)
        return await self.templates.TemplateResponse(
            request,
            "procurement.html",
            {
                "categories": categories,
                "products": products,
                "daily": daily,
                "history": history,
                "history_page": history_page,
                "history_pages": history_pages,
                "history_total": history_total,
                "draft_rows": self._draft_rows(form),
                "plan_date": str(form.get("plan_date") or moscow_today().isoformat()) if form else moscow_today().isoformat(),
                "title": str(form.get("title") or "") if form else "",
                "error": error,
                "success": success,
                "can_save": can_save,
                "currency": str(getattr(EnvKeys, "PAY_CURRENCY", "RUB") or "RUB"),
            },
        )

    @expose("/procurement-control", methods=["GET", "POST"], identity="procurement-control")
    async def procurement_control(self, request: Request):
        caller_perms = int(await resolve_web_perms(request) or 0)
        can_save = Permission.granted(caller_perms, Permission.CATALOG_MANAGE)
        if request.method != "POST":
            success = request.session.pop("procurement_flash", None)
            return await self._render(
                request, error=None, form=None, can_save=can_save, success=success
            )

        form = await request.form()
        if not can_save:
            return await self._render(
                request,
                error="Для сохранения плана нужно право управления каталогом.",
                form=form,
                can_save=False,
            )

        try:
            plan_date, title = parse_plan_metadata(form)
            categories, products = await self._catalog()
            categories_by_id = {category["id"]: category for category in categories}
            products_by_id = {product["id"]: product for product in products}
            current_prices = {
                product_id: product["current_price"]
                for product_id, product in products_by_id.items()
            }
            lines = parse_procurement_form(form, current_prices)

            for line in lines:
                category = categories_by_id.get(line.category_id)
                product = products_by_id.get(line.product_id)
                if category is None or product is None:
                    raise ProcurementInputError("Выберите активные категорию и товар из каталога.")
                if product["category_id"] != line.category_id:
                    raise ProcurementInputError("Выбранный товар не относится к указанной категории.")

            totals = calculate_forecast(lines)
            async with Database().session() as session:
                # Re-read submitted product and category IDs in the write transaction;
                # rendered data above is only a preview and can never authorize a write.
                category_ids = {line.category_id for line in lines}
                product_ids = {line.product_id for line in lines}
                db_categories = (await session.execute(
                    sa_select(Categories).where(
                        Categories.id.in_(category_ids),
                        Categories.is_active.is_(True),
                    )
                )).scalars().all()
                db_products = (await session.execute(
                    sa_select(Goods).where(
                        Goods.id.in_(product_ids),
                        Goods.is_active.is_(True),
                    )
                )).scalars().all()
                db_categories_by_id = {int(row.id): row for row in db_categories}
                db_products_by_id = {int(row.id): row for row in db_products}
                active_children = (await session.execute(
                    sa_select(Categories.parent_id).where(
                        Categories.parent_id.in_(category_ids),
                        Categories.is_active.is_(True),
                    )
                )).scalars().all()
                categories_with_children = {int(category_id) for category_id in active_children if category_id}

                plan_items = []
                for line in lines:
                    category = db_categories_by_id.get(line.category_id)
                    product = db_products_by_id.get(line.product_id)
                    if (
                        category is None or product is None
                        or line.category_id in categories_with_children
                        or int(product.category_id) != line.category_id
                    ):
                        raise ProcurementInputError(
                            "Каталог изменился. Обновите страницу и заново выберите активные товары."
                        )
                    # Current catalog price is resolved again within the same transaction.
                    current_price, _on_sale, _original = effective_price(product)
                    if line.sale_price_mode == "catalog":
                        sale_price = Decimal(str(current_price)).quantize(Decimal("0.01"))
                    else:
                        sale_price = line.sale_price
                    line_total_cost = (line.unit_cost * line.quantity).quantize(Decimal("0.01"))
                    line_revenue = (sale_price * line.quantity).quantize(Decimal("0.01"))
                    if line_total_cost > MAX_AMOUNT or line_revenue > MAX_AMOUNT:
                        raise ProcurementInputError("Итог по строке не должен превышать 9 999 999 999,99.")
                    plan_items.append(ProcurementPlanItem(
                        product_id=int(product.id),
                        category_id=int(category.id),
                        category_name=str(category.name),
                        product_name=str(product.name),
                        quantity=line.quantity,
                        unit_cost=line.unit_cost,
                        sale_price=sale_price,
                        sale_price_mode=line.sale_price_mode,
                        total_cost=line_total_cost,
                        expected_revenue=line_revenue,
                        gross_profit=(line_revenue - line_total_cost).quantize(Decimal("0.01")),
                    ))

                # Recalculate after authoritative catalog prices are re-read.
                totals = calculate_forecast(plan_items)
                plan = ProcurementPlan(
                    plan_date=plan_date,
                    title=title,
                    total_cost=totals["total_cost"],
                    expected_revenue=totals["expected_revenue"],
                    gross_profit=totals["gross_profit"],
                    gross_margin_percent=totals["gross_margin_percent"],
                    return_on_cost_percent=totals["return_on_cost_percent"],
                    created_by=str(request.session.get("web_login") or "")[:128] or None,
                    items=plan_items,
                )
                session.add(plan)
                await session.flush()
                await log_audit(
                    "procurement_plan_saved",
                    resource_type="Прогноз закупки",
                    resource_id=str(plan.id),
                    details=(
                        f"date={plan_date.isoformat()}, title={title or ''}, "
                        f"lines={totals['line_count']}, quantity={totals['quantity']}, "
                        f"cost={totals['total_cost']}, revenue={totals['expected_revenue']}, "
                        f"profit={totals['gross_profit']}"
                    ),
                    ip_address=_client_ip(request),
                    session=session,
                )
                saved_plan_id = int(plan.id)

            request.session["procurement_flash"] = (
                f"Прогноз #{saved_plan_id} сохранён: закупка "
                f"{_format_revenue_money(totals['total_cost'])} {EnvKeys.PAY_CURRENCY}, "
                f"прогноз прибыли {_format_revenue_money(totals['gross_profit'])} {EnvKeys.PAY_CURRENCY}. "
                "Склад и фактические расходы не менялись."
            )
            return RedirectResponse(url="/admin/procurement-control", status_code=303)
        except ProcurementInputError as exc:
            error = str(exc)
        except Exception:
            logger.exception("Failed to save procurement forecast")
            error = "Не удалось сохранить план. Проверьте введённые данные и попробуйте ещё раз."

        return await self._render(
            request, error=error, form=form, can_save=can_save
        )


# Model Views
class UserAdmin(AuditModelView, model=User):
    required_perm = Permission.USERS_MANAGE
    column_list = [User.telegram_id, User.balance, User.role_id, User.referral_id,
                   User.registration_date, User.locale, User.is_blocked]
    column_searchable_list = [User.telegram_id]
    column_sortable_list = [User.telegram_id, User.balance, User.registration_date]
    column_default_sort = (User.registration_date, True)
    column_formatters = {"registration_date": _format_moscow_datetime}
    column_formatters_detail = {"registration_date": _format_moscow_datetime}
    form_excluded_columns = [
        User.user_operations, User.user_goods,
        User.referral_earnings_received, User.referral_earnings_generated,
        User.community_chat_required, User.community_prompt_seen,
    ]
    column_labels = {
        "telegram_id": "ID в Telegram",
        "balance": "Баланс",
        "role_id": "Роль",
        "referral_id": "Пригласивший пользователь",
        "registration_date": "Дата регистрации",
        "locale": "Язык",
        "is_blocked": "Заблокирован",
    }
    name = "Пользователь"
    name_plural = "Пользователи"
    icon = "fa-solid fa-users"

    async def _invalidate(self, model: Any, *, blocked: bool | None = None) -> None:
        # A web edit of balance/role_id/is_blocked would otherwise be served stale
        # from Redis (user/role, up to 600s) and from the middleware's in-memory
        # role cache + blocked set (until restart). Clear both; the blocked set
        # is authoritative per-update, so pass the new block state explicitly.
        tid = getattr(model, "telegram_id", None)
        if tid is not None:
            safe_create_task(invalidate_user_cache(int(tid)))
            invalidate_auth_caches(int(tid), blocked=blocked)

    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().on_model_change(data, model, is_created, request)

        def _to_int(v):
            if v in (None, "", "None"):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        new_role = _to_int(data.get("role_id"))
        if is_created:
            current_role, old_balance = None, None
        else:
            current_role, old_balance = None, None
            tid = getattr(model, "telegram_id", None)
            if tid is not None:
                async with Database().session() as s:
                    row = (await s.execute(
                        sa_select(User.role_id, User.balance).where(
                            User.telegram_id == int(tid))
                    )).first()
                    if row is not None:
                        current_role, old_balance = row
        if new_role != current_role:
            elevated = await role_permissions(new_role)
            caller_perms = await resolve_web_perms(request)
            caller_perms = int(caller_perms or 0)

            if is_created:
                # A USERS_MANAGE operator may create ordinary users, but a
                # privileged Telegram account must be created by OWN only. Do
                # not treat an arbitrary custom role that happens to contain
                # only USE as the built-in USER role.
                role_name = None
                if new_role is not None:
                    async with Database().session() as s:
                        role_name = (await s.execute(
                            sa_select(Role.name).where(Role.id == new_role)
                        )).scalar_one_or_none()
                plain_user_role = (
                    str(role_name or "").strip().upper() == "USER"
                    and elevated == Permission.USE
                )
                if not plain_user_role and not Permission.granted(caller_perms, Permission.OWN):
                    raise ValueError(
                        "Создавать пользователя с ролью выше USER может только владелец.")
            elif not Permission.is_subset(elevated, caller_perms | Permission.USE):
                # A role change may never grant a bit the caller does not hold.
                raise ValueError(
                    "Целевая роль должна быть подмножеством прав вызывающего администратора.")
        # Money trail: web balance edits must record old -> new figures.
        if not is_created and old_balance is not None and "balance" in data:
            try:
                new_balance = Decimal(str(data.get("balance")))
            except (InvalidOperation, TypeError, ValueError):
                new_balance = None
            if new_balance is not None and new_balance != Decimal(str(old_balance)):
                safe_create_task(log_audit(
                    "web_balance_change",
                    user_id=int(getattr(model, "telegram_id", 0) or 0),
                    resource_type="User",
                    details=(f"balance {old_balance} -> {new_balance} "
                             f"{EnvKeys.PAY_CURRENCY} by {request.session.get('web_login', '?')}"),
                    ip_address=_client_ip(request),
                ))

    async def after_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().after_model_change(data, model, is_created, request)
        await self._invalidate(model, blocked=bool(getattr(model, "is_blocked", False)))

    async def after_model_delete(self, model: Any, request: Request) -> None:
        await super().after_model_delete(model, request)
        await self._invalidate(model, blocked=False)


_PERM_FLAGS = [
    (1,   "Использование"),
    (2,   "Рассылка"),
    (4,   "Настройки"),
    (8,   "Пользователи"),
    (16,  "Каталог"),
    (32,  "Администраторы"),
    (64,  "Владелец"),
    (128, "Статистика"),
    (256, "Баланс"),
    (512, "Промокоды"),
]


def _format_perms_html(model, name):
    perms = getattr(model, name, 0) or 0
    if not perms:
        return Markup('<span style="color:#999">\u2014</span>')
    badges = []
    for bit, label in _PERM_FLAGS:
        if perms & bit:
            badges.append(
                f'<span style="display:inline-block;background:#e2e8f0;padding:1px 6px;'
                f'border-radius:4px;margin:1px;font-size:12px">{label}</span>'
            )
    raw = f'<span style="color:#999;font-size:11px;margin-left:4px">({perms})</span>'
    return Markup(" ".join(badges) + raw)


class RoleAdmin(AuditModelView, model=Role):
    required_perm = Permission.ADMINS_MANAGE
    column_list = [Role.id, Role.name, Role.default, Role.permissions]
    column_details_exclude_list = ["users"]
    form_excluded_columns = [Role.users]
    column_sortable_list = [Role.id, Role.name]
    column_labels = {
        "id": "ID",
        "name": "Название роли",
        "default": "Системная роль",
        "permissions": "Права доступа",
    }
    name = "Роль"
    name_plural = "Роли и права"
    icon = "fa-solid fa-shield-halved"
    column_formatters = {"permissions": _format_perms_html}
    column_formatters_detail = {"permissions": _format_perms_html}
    form_args = {
        "permissions": {
            "description": (
                "Число из прав доступа. Для обычной работы с каталогом лучше "
                "использовать меню ролей прямо в Telegram, а не менять это поле вручную."
            ),
        },
    }

    @staticmethod
    async def _flush_role_caches() -> None:
        # A Role's permission bitmask affects every user holding that role, so
        # invalidation cannot be scoped to one id: flush all role caches.
        await flush_all_role_caches()

    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        """Keep role bitmasks and built-in roles fail-closed in the web panel."""
        await super().on_model_change(data, model, is_created, request)

        known_mask = 0
        for bit, _label in _PERM_FLAGS:
            known_mask |= bit

        raw_permissions = data.get("permissions", getattr(model, "permissions", None))
        if isinstance(raw_permissions, bool):
            raise ValueError("Права должны быть целым числом от 0 до 1023.")
        try:
            permissions = int(raw_permissions)
        except (TypeError, ValueError):
            raise ValueError("Права должны быть целым числом от 0 до 1023.")
        if permissions < 0 or permissions & ~known_mask:
            raise ValueError("Права должны быть целым числом от 0 до 1023.")

        name = str(data.get("name", getattr(model, "name", "")) or "").strip().upper()
        if not name:
            raise ValueError("Укажите название роли.")
        system_names = {"USER", "ADMIN", "OWNER"}

        previous_name = getattr(model, "name", None)
        previous_default = bool(getattr(model, "default", False))
        if not is_created and getattr(model, "id", None) is not None:
            async with Database().session() as s:
                previous = (await s.execute(
                    sa_select(Role.name, Role.default).where(Role.id == int(model.id))
                )).one_or_none()
            if previous is not None:
                previous_name, previous_default = previous

        if (
            name in system_names
            or str(previous_name or "").strip().upper() in system_names
            or previous_default
        ):
            raise ValueError("Системные роли USER, ADMIN и OWNER нельзя изменять через веб-панель.")

        caller_perms = await resolve_web_perms(request)
        caller_perms = int(caller_perms or 0)
        if not Permission.is_subset(permissions, caller_perms):
            raise ValueError("Новые права должны быть подмножеством прав вызывающего администратора.")
        if permissions & Permission.OWN and not Permission.granted(caller_perms, Permission.OWN):
            raise ValueError("Право владельца может назначать только владелец.")

        if "default" in data and bool(data.get("default")) != previous_default:
            if not Permission.granted(caller_perms, Permission.OWN):
                raise ValueError("Роль по умолчанию может менять только владелец.")

    async def on_model_delete(self, model: Any, request: Request) -> None:
        """Block deletions that would break registration.

        The bot's own role menu refuses these; the web panel previously did
        not, which is how the built-in USER/ADMIN/OWNER rows were once removed:
        afterwards every new user failed with ``errors.something_wrong`` and
        never appeared in admin. Raising here aborts the SQLAdmin delete.
        """
        from sqlalchemy import func as _func

        name = getattr(model, "name", None)
        if name in ("USER", "ADMIN", "OWNER"):
            raise ValueError(f"Нельзя удалить системную роль {name!r}: она нужна для регистрации новых пользователей.")
        if getattr(model, "default", False):
            raise ValueError("Нельзя удалить роль по умолчанию: сначала назначьте другую роль по умолчанию.")
        async with Database().session() as s:
            user_count = (await s.execute(
                sa_select(_func.count(User.telegram_id)).where(User.role_id == getattr(model, "id", None))
            )).scalar() or 0
        if user_count:
            raise ValueError(f"Нельзя удалить роль: к ней привязано пользователей: {user_count}.")

    async def after_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().after_model_change(data, model, is_created, request)
        await self._flush_role_caches()

    async def after_model_delete(self, model: Any, request: Request) -> None:
        await super().after_model_delete(model, request)
        await self._flush_role_caches()


class CategoryAdmin(AuditModelView, model=Categories):
    required_perm = Permission.CATALOG_MANAGE
    column_list = [Categories.name, Categories.parent_id, Categories.sort_order, Categories.is_active,
                   Categories.image_ref]
    column_searchable_list = [Categories.name]
    form_columns = [Categories.name, Categories.parent, Categories.sort_order, Categories.is_active,
                    Categories.image_ref]
    column_labels = {
        "name": "Название категории",
        "parent_id": "Родительский раздел (ID)",
        "parent": "Родительский раздел",
        "sort_order": "Порядок в каталоге",
        "is_active": "Показывать в каталоге",
        "image_ref": "Изображение категории",
    }
    form_args = {
        "name": {
            "description": "Например: ChatGPT, Gemini или Claude.",
        },
        "sort_order": {
            "description": "Меньшее число показывает категорию выше среди её соседей. Можно оставить 0.",
        },
        "parent": {
            "description": "Необязательно. Выбирайте только для подраздела; вложенность ограничена одним уровнем.",
        },
        "image_ref": {
            "description": "Необязательно: HTTPS-ссылка, Telegram file_id или путь из assets/ui.",
        },
    }
    name = "Категория"
    name_plural = "Категории"
    icon = "fa-solid fa-folder"

    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().on_model_change(data, model, is_created, request)
        parent_id = getattr(model, "parent_id", None)
        if "parent" in data:
            selected_parent = data.get("parent")
            if selected_parent in (None, "", "None"):
                parent_id = None
            elif isinstance(selected_parent, Categories):
                parent_id = selected_parent.id
            else:
                try:
                    parent_id = int(selected_parent)
                except (TypeError, ValueError):
                    raise ValueError("Выберите существующий родительский раздел.")

        if parent_id is not None:
            async with Database().session() as session:
                parent = (await session.execute(
                    sa_select(Categories.id, Categories.parent_id)
                    .where(Categories.id == int(parent_id))
                )).one_or_none()
                if parent is None:
                    raise ValueError("Родительский раздел не найден.")
                if model.id is not None and int(model.id) == int(parent.id):
                    raise ValueError("Категория не может быть родителем самой себе.")
                if parent.parent_id is not None:
                    raise ValueError("Подраздел нельзя выбрать родительским разделом.")
                if model.id is not None and await session.scalar(
                    sa_select(sa_exists().where(Categories.parent_id == int(model.id)))
                ):
                    raise ValueError("Раздел с подразделами нельзя вложить в другой раздел.")
            model.parent_id = int(parent_id)
        else:
            model.parent_id = None

    async def after_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().after_model_change(data, model, is_created, request)
        safe_create_task(invalidate_category_cache(model.name))
        if model.parent_id is not None:
            parent_name = await get_category_name_by_id(int(model.parent_id))
            if parent_name:
                safe_create_task(invalidate_category_cache(parent_name))

    async def after_model_delete(self, model: Any, request: Request) -> None:
        await super().after_model_delete(model, request)
        safe_create_task(invalidate_category_cache(model.name))
        if model.parent_id is not None:
            parent_name = await get_category_name_by_id(int(model.parent_id))
            if parent_name:
                safe_create_task(invalidate_category_cache(parent_name))


class GoodsAdmin(AuditModelView, model=Goods):
    required_perm = Permission.CATALOG_MANAGE
    column_list = [Goods.id, Goods.name, Goods.price,
                   Goods.is_vpn_subscription, Goods.sort_order, Goods.is_active,
                   Goods.is_variable_pricing, Goods.min_quantity, Goods.max_quantity,
                   Goods.availability_note, Goods.image_ref,
                   Goods.stock_quantity, Goods.description, Goods.category_id]
    column_searchable_list = [Goods.name]
    column_sortable_list = [Goods.id, Goods.name, Goods.price]
    # NOTE: no delivery_text / stock_quantity here on purpose: the balance
    # of every lot lives only in «Склад и автовыдача» (ItemValues rows).
    form_columns = [
        Goods.category,
        Goods.name,
        Goods.price,
        Goods.description,
        Goods.sort_order,
        Goods.is_active,
        Goods.is_vpn_subscription,
        Goods.is_variable_pricing,
        Goods.min_quantity,
        Goods.max_quantity,
        Goods.image_ref,
        Goods.availability_note,
    ]
    column_labels = {
        "id": "ID",
        "category": "Категория",
        "category_id": "Категория",
        "name": "Название товара",
        "price": "Цена",
        "description": "Описание для покупателя",
        "sort_order": "Порядок в категории",
        "is_active": "Показывать товар в каталоге",
        "is_vpn_subscription": "Персональная VPN-подписка через прокси",
        "is_variable_pricing": "Цена зависит от количества",
        "min_quantity": "Минимальное количество за заказ",
        "max_quantity": "Максимальное количество за заказ",
        "image_ref": "Изображение товара",
        "availability_note": "Статус наличия",
    }
    create_template = "product_create.html"
    edit_template = "product_edit.html"
    name = "Товар"
    name_plural = "Товары"
    icon = "fa-solid fa-box"
    form_args = {
        "category": {
            "description": "Выберите раздел, в котором покупатель увидит товар.",
        },
        "name": {
            "description": "Например: ChatGPT Plus на 1 месяц.",
        },
        "price": {
            "description": (
                "Например, 599. В обычном режиме это цена товара; если включён режим "
                "цены по количеству — стоимость одной единицы."
            ),
        },
        "description": {
            "description": "Текст, который увидит покупатель: что входит, срок и условия выдачи.",
        },
        "sort_order": {
            "description": "Это только позиция товара в витрине: меньшее число выводит его выше. Это не количество.",
        },
        "is_active": {
            "description": "Включите, когда товар и остаток уже готовы к продаже.",
        },
        "is_variable_pricing": {
            "description": (
                "Включите, чтобы покупатель выбирал количество. Цена в поле «Цена» "
                "будет стоимостью одной единицы; итог = цена × количество."
            ),
        },
        "min_quantity": {
            "description": "Минимум единиц в одном заказе (от 1 до 5000).",
        },
        "max_quantity": {
            "description": "Максимум единиц в одном заказе (до 5000); фактическая покупка также ограничена складом.",
        },
        "image_ref": {
            "description": (
                "Необязательно: HTTPS-ссылка, Telegram file_id или путь из assets/ui "
                "(например products/chatgpt-plus.jpg)."
            ),
        },
        "availability_note": {
            "description": (
                "Необязательно: например, «предзаказ ⏳». Оставьте пустым, "
                "чтобы бот сам показывал наличие по складу."
            ),
        },
    }
    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().on_model_change(data, model, is_created, request)
        raw_category = data.get("category")
        category_id = data.get("category_id", getattr(model, "category_id", None))
        if category_id in (None, "", "None") and raw_category not in (None, "", "None"):
            category_id = getattr(raw_category, "id", raw_category)
        try:
            category_id = int(category_id) if category_id not in (None, "", "None") else None
        except (TypeError, ValueError):
            category_id = None
        if category_id is not None:
            async with Database().session() as session:
                is_group = await session.scalar(
                    sa_select(sa_exists().where(Categories.parent_id == category_id))
                )
            if is_group:
                raise ValueError("Выберите конечную категорию, а не родительский раздел.")
        raw_name = data.get("name", getattr(model, "name", None))
        normalized_name = str(raw_name or "").strip()
        if not normalized_name:
            raise ValueError("Название товара не может быть пустым.")
        data["name"] = normalized_name
        try:
            price = Decimal(str(data.get("price", getattr(model, "price", None))))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Цена должна быть конечным числом.") from exc
        if not price.is_finite() or price <= 0:
            raise ValueError("Цена должна быть положительной конечной суммой.")

        raw_variable_pricing = data.get(
            "is_variable_pricing", getattr(model, "is_variable_pricing", False)
        )
        if isinstance(raw_variable_pricing, str):
            normalized_flag = raw_variable_pricing.strip().lower()
            if normalized_flag in {"true", "1", "on", "yes", "y"}:
                variable_pricing = True
            elif normalized_flag in {"", "false", "0", "off", "no", "none"}:
                variable_pricing = False
            else:
                raise ValueError("Проверьте настройку цены по количеству.")
        else:
            variable_pricing = bool(raw_variable_pricing)
        data["is_variable_pricing"] = variable_pricing
        if variable_pricing:
            try:
                minimum = int(data.get("min_quantity"))
                maximum = int(data.get("max_quantity"))
            except (TypeError, ValueError) as exc:
                raise ValueError("Укажите минимальное и максимальное количество.") from exc
            if not 1 <= minimum <= maximum <= 5000:
                raise ValueError("Количество должно быть в диапазоне от 1 до 5000; максимум не меньше минимума.")
            data["min_quantity"] = minimum
            data["max_quantity"] = maximum
        else:
            data["min_quantity"] = None
            data["max_quantity"] = None

        # NOTE: stock balance and delivery contents are managed exclusively
        # in «Склад и автовыдача» (ItemValues rows) — the position form
        # intentionally has no stock_quantity / delivery_text fields.

    async def _invalidate(self, model: Any) -> None:
        name = getattr(model, "name", None)
        if name:
            category_id = getattr(model, "category_id", None)
            category_name = (
                await get_category_name_by_id(int(category_id))
                if category_id is not None else None
            )
            safe_create_task(invalidate_item_cache(name, category_name))

    async def after_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().after_model_change(data, model, is_created, request)
        await self._invalidate(model)
        if is_created and _notifier_bot is not None:
            safe_create_task(notify_owner_stock_added(
                _notifier_bot,
                item_name=str(model.name),
                price=model.price,
                count=int(model.stock_quantity or 0),
                item_id=int(model.id),
            ))

    async def after_model_delete(self, model: Any, request: Request) -> None:
        await super().after_model_delete(model, request)
        await self._invalidate(model)


class ItemValuesAdmin(AuditModelView, model=ItemValues):
    required_perm = Permission.CATALOG_MANAGE
    column_list = [
        ItemValues.id,
        ItemValues.item,
        ItemValues.item_id,
        ItemValues.value,
        ItemValues.is_infinity,
    ]
    column_searchable_list = [ItemValues.value]
    column_sortable_list = [ItemValues.id, ItemValues.item_id]
    form_columns = [ItemValues.item, ItemValues.value, ItemValues.is_infinity]
    column_labels = {
        "id": "ID",
        "item": "Тип товара",
        "item_id": "ID товара",
        "value": "Текст, который получит покупатель",
        "is_infinity": "Выдавать без ограничения",
    }
    create_template = "stock_item_create.html"
    edit_template = "stock_item_edit.html"
    name = "Единица склада"
    name_plural = "Склад и автовыдача"
    icon = "fa-solid fa-warehouse"
    form_args = {
        "item": {
            "description": "Выберите товар, для которого добавляете остаток.",
        },
        "value": {
            "description": (
                "Одно большое поле для результата одной покупки. Вставьте сюда "
                "логин, пароль, ключ, ссылку, инструкцию или любой другой текст."
            ),
        },
        "is_infinity": {
            "description": (
                "Включайте только для одной общей инструкции или ссылки, которую можно "
                "выдавать неограниченно. Для аккаунтов и ключей оставьте выключенным."
            ),
        },
    }
    form_widget_args = {
        "value": {
            "rows": 12,
            "placeholder": "Логин: example@gmail.com\nПароль: ваш-пароль\n\nИнструкция для покупателя: ...",
        },
    }

    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().on_model_change(data, model, is_created, request)
        value = str(data.get("value", getattr(model, "value", "")) or "").strip()
        if not value:
            raise ValueError("Значение склада не может быть пустым.")
        data["value"] = value
        request.state.catalog_arrival_announcement = bool(
            is_created and await _arrival_announcement_requested(request)
        )
        request.state.catalog_notify_all = bool(
            is_created and await _notify_all_requested(request)
        )

    async def _item_name(self, model: Any) -> str | None:
        item_id = getattr(model, "item_id", None)
        return await get_item_name_by_id(int(item_id)) if item_id is not None else None

    async def _item_notification_data(self, model: Any) -> tuple[int, str, Decimal] | None:
        """Load the immutable product details needed for the owner alert."""
        item_id = getattr(model, "item_id", None)
        if item_id is None:
            return None

        async with Database().session() as session:
            row = (await session.execute(
                sa_select(Goods.name, Goods.price).where(Goods.id == int(item_id))
            )).one_or_none()

        if row is None:
            return None
        return int(item_id), str(row.name), row.price

    async def _invalidate(self, name: str) -> None:
        safe_create_task(invalidate_item_cache(name))

    async def after_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().after_model_change(data, model, is_created, request)
        name = await self._item_name(model)
        if not name:
            return
        await self._invalidate(name)

        if is_created and _notifier_bot is not None:
            notification_data = await self._item_notification_data(model)
            safe_create_task(notify_restock(
                _notifier_bot,
                name,
                notify_all=getattr(request.state, "catalog_notify_all", False),
                item_id=notification_data[0] if notification_data else None,
                price=notification_data[2] if notification_data else None,
            ))
            if notification_data is not None:
                item_id, item_name, price = notification_data
                safe_create_task(notify_owner_stock_added(
                    _notifier_bot,
                    item_name=item_name,
                    price=price,
                    count=1,
                    is_infinity=bool(getattr(model, "is_infinity", False)),
                    item_id=item_id,
                ))
                if getattr(request.state, "catalog_arrival_announcement", False) is True:
                    safe_create_task(announce_catalog_arrival(
                        _notifier_bot,
                        item_name=item_name,
                        price=price,
                        count=1,
                        item_id=item_id,
                        is_infinity=bool(getattr(model, "is_infinity", False)),
                    ))

    async def after_model_delete(self, model: Any, request: Request) -> None:
        await super().after_model_delete(model, request)
        name = await self._item_name(model)
        if name:
            await self._invalidate(name)


class BoughtGoodsAdmin(WebAccessMixin, ModelView, model=BoughtGoods):
    required_perm = Permission.STATS_VIEW
    column_list = [BoughtGoods.id, BoughtGoods.item_name, BoughtGoods.value,
                   BoughtGoods.price, BoughtGoods.buyer_id, BoughtGoods.bought_datetime,
                   BoughtGoods.unique_id]
    column_searchable_list = [BoughtGoods.item_name, BoughtGoods.buyer_id, BoughtGoods.unique_id]
    column_sortable_list = [BoughtGoods.id, BoughtGoods.bought_datetime, BoughtGoods.price]
    column_default_sort = (BoughtGoods.id, True)
    can_create = False
    can_edit = False
    can_delete = False
    column_formatters = {"bought_datetime": _format_moscow_datetime}
    column_formatters_detail = {"bought_datetime": _format_moscow_datetime}
    column_labels = {
        "id": "ID",
        "item_name": "Товар",
        "value": "Выданный текст",
        "price": "Цена покупки",
        "buyer_id": "ID покупателя в Telegram",
        "bought_datetime": "Дата покупки",
        "unique_id": "Уникальный номер покупки",
    }
    name = "Покупка"
    name_plural = "Покупки"
    icon = "fa-solid fa-cart-shopping"


class OperationsAdmin(WebAccessMixin, ModelView, model=Operations):
    required_perm = Permission.STATS_VIEW
    column_list = [Operations.id, Operations.user_id, Operations.operation_value,
                   Operations.operation_time]
    column_searchable_list = [Operations.user_id]
    column_sortable_list = [Operations.id, Operations.operation_time, Operations.operation_value]
    column_default_sort = (Operations.id, True)
    can_create = False
    can_edit = False
    can_delete = False
    column_formatters = {"operation_time": _format_moscow_datetime}
    column_formatters_detail = {"operation_time": _format_moscow_datetime}
    column_labels = {
        "id": "ID",
        "user_id": "ID пользователя в Telegram",
        "operation_value": "Сумма",
        "operation_time": "Дата операции",
    }
    name = "Операция"
    name_plural = "Операции с балансом"
    icon = "fa-solid fa-money-bill-transfer"


class PaymentsAdmin(AuditModelView, model=Payments):
    required_perm = Permission.STATS_VIEW
    column_list = [Payments.id, Payments.provider, Payments.external_id, Payments.user_id,
                   Payments.amount, Payments.currency, Payments.status, Payments.created_at]
    column_searchable_list = [Payments.user_id, Payments.external_id, Payments.provider]
    column_sortable_list = [Payments.id, Payments.created_at, Payments.amount, Payments.status]
    column_default_sort = (Payments.id, True)
    can_create = False
    can_edit = False
    # Deleting a payment record is deliberately more privileged than viewing
    # it: a successful payment is part of the idempotency guard and removing it
    # must be an explicit operator action.  The standard SQLAdmin delete modal
    # still asks for confirmation, and AuditModelView records the deletion.
    can_delete = True
    column_formatters = {"created_at": _format_moscow_datetime}
    column_formatters_detail = {"created_at": _format_moscow_datetime}

    async def check_can_delete(self, request: Request, model: Any) -> bool:
        """Allow only safe test/failed rows to be physically removed.

        A pending or succeeded real payment must remain in the idempotency
        table: a provider callback can arrive again and recreate the row,
        which could credit the same payment twice after a hard delete.
        """
        if not await has_web_perm(request, Permission.BALANCE_MANAGE):
            return False
        provider = str(getattr(model, "provider", "") or "").strip().lower()
        status = str(getattr(model, "status", "") or "").strip().lower()
        return provider == "test" or status == "failed"

    column_labels = {
        "id": "ID",
        "provider": "Платёжный сервис",
        "external_id": "Номер платежа",
        "user_id": "ID пользователя в Telegram",
        "amount": "Сумма",
        "currency": "Валюта",
        "status": "Статус",
        "created_at": "Дата создания",
    }
    name = "Платёж"
    name_plural = "Платежи"
    icon = "fa-solid fa-credit-card"


class ManualRevenueAdmin(AuditModelView, model=ManualRevenue):
    required_perm = Permission.BALANCE_MANAGE
    column_list = [
        ManualRevenue.id,
        ManualRevenue.category_name,
        ManualRevenue.quantity,
        ManualRevenue.unit_price,
        ManualRevenue.created_at,
        ManualRevenue.created_by,
    ]
    column_searchable_list = [ManualRevenue.category_name, ManualRevenue.created_by]
    column_sortable_list = [ManualRevenue.id, ManualRevenue.created_at, ManualRevenue.unit_price]
    column_default_sort = (ManualRevenue.created_at, True)
    column_formatters = {"created_at": _format_moscow_datetime}
    column_formatters_detail = {"created_at": _format_moscow_datetime}
    can_create = False
    can_edit = False
    column_labels = {
        "id": "ID",
        "category_name": "Категория",
        "quantity": "Количество",
        "unit_price": "Цена за единицу",
        "created_at": "Дата внесения",
        "created_by": "Кто внёс",
    }
    name = "Ручная выручка"
    name_plural = "Ручная выручка"
    icon = "fa-solid fa-file-invoice-dollar"


class ReferralEarningsAdmin(WebAccessMixin, ModelView, model=ReferralEarnings):
    required_perm = Permission.STATS_VIEW
    column_list = [ReferralEarnings.id, ReferralEarnings.referrer_id,
                   ReferralEarnings.referral_id, ReferralEarnings.amount,
                   ReferralEarnings.original_amount, ReferralEarnings.created_at]
    column_searchable_list = [ReferralEarnings.referrer_id, ReferralEarnings.referral_id]
    column_sortable_list = [ReferralEarnings.id, ReferralEarnings.created_at, ReferralEarnings.amount]
    column_default_sort = (ReferralEarnings.id, True)
    can_create = False
    can_edit = False
    can_delete = False
    column_formatters = {"created_at": _format_moscow_datetime}
    column_formatters_detail = {"created_at": _format_moscow_datetime}
    column_labels = {
        "id": "ID",
        "referrer_id": "ID пригласившего в Telegram",
        "referral_id": "ID приглашённого в Telegram",
        "amount": "Начисление",
        "original_amount": "Сумма исходной покупки",
        "created_at": "Дата начисления",
    }
    name = "Реферальное начисление"
    name_plural = "Реферальные начисления"
    icon = "fa-solid fa-handshake"


class AuditLogAdmin(WebAccessMixin, ModelView, model=AuditLog):
    required_perm = Permission.STATS_VIEW
    column_list = [AuditLog.id, AuditLog.timestamp, AuditLog.level, AuditLog.user_id,
                   AuditLog.action, AuditLog.resource_type, AuditLog.resource_id,
                   AuditLog.details, AuditLog.ip_address]
    column_searchable_list = [AuditLog.action, AuditLog.resource_type, AuditLog.details]
    column_sortable_list = [AuditLog.id, AuditLog.timestamp, AuditLog.level, AuditLog.action]
    column_default_sort = (AuditLog.id, True)
    can_create = False
    can_edit = False
    can_delete = False
    column_formatters = {"timestamp": _format_moscow_datetime}
    column_formatters_detail = {"timestamp": _format_moscow_datetime}
    column_labels = {
        "id": "ID",
        "timestamp": "Дата и время",
        "level": "Уровень",
        "user_id": "ID пользователя в Telegram",
        "action": "Действие",
        "resource_type": "Раздел",
        "resource_id": "ID записи",
        "details": "Подробности",
        "ip_address": "IP-адрес",
    }
    name = "Запись журнала"
    name_plural = "Журнал действий"
    icon = "fa-solid fa-clipboard-list"


class PromoCodeAdmin(AuditModelView, model=PromoCodes):
    required_perm = Permission.PROMO_MANAGE
    column_list = [PromoCodes.id, PromoCodes.code, PromoCodes.discount_type,
                   PromoCodes.discount_value, PromoCodes.max_uses, PromoCodes.current_uses,
                   PromoCodes.is_active, PromoCodes.expires_at, PromoCodes.created_at]
    column_searchable_list = [PromoCodes.code]
    column_sortable_list = [PromoCodes.id, PromoCodes.code, PromoCodes.created_at]
    column_default_sort = (PromoCodes.id, True)
    form_columns = [PromoCodes.code, PromoCodes.discount_type, PromoCodes.discount_value,
                    PromoCodes.max_uses, PromoCodes.expires_at, PromoCodes.is_active]
    form_overrides = {
        "discount_type": SelectField,
        "expires_at": MoscowDateTimeField,
    }
    form_args = {
        "discount_type": {
            "choices": [
                ("balance", "Пополнение баланса"),
            ],
            "description": "Промокод только зачисляет указанную сумму на баланс.",
        },
        "expires_at": {
            "description": "Дата и время окончания по Москве (МСК). Пустое значение означает отсутствие срока.",
        },
    }
    column_labels = {
        "id": "ID",
        "code": "Промокод",
        "discount_type": "Тип промокода",
        "discount_value": "Сумма пополнения баланса",
        "max_uses": "Лимит использований",
        "current_uses": "Уже использован",
        "is_active": "Активен",
        "expires_at": "Действует до (МСК)",
        "created_at": "Дата создания",
    }
    column_formatters = {
        "expires_at": _format_moscow_datetime,
        "created_at": _format_moscow_datetime,
    }
    column_formatters_detail = {
        "expires_at": _format_moscow_datetime,
        "created_at": _format_moscow_datetime,
    }
    name = "Промокод"
    name_plural = "Промокоды"
    icon = "fa-solid fa-tag"

    def list_query(self, request: Request):
        return sa_select(PromoCodes).where(PromoCodes.discount_type == "balance")

    def count_query(self, request: Request):
        return sa_select(func.count(PromoCodes.id)).where(
            PromoCodes.discount_type == "balance"
        )

    async def on_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        """Validate/normalize a promo before persisting."""
        raw_expires_at = data.get("expires_at", getattr(model, "expires_at", None))
        if raw_expires_at in ("", "None"):
            data["expires_at"] = None
        elif raw_expires_at is not None:
            expires_at_utc = moscow_input_to_utc(raw_expires_at)
            if expires_at_utc is None:
                raise ValueError("Дата окончания промокода должна быть в формате даты и времени.")
            data["expires_at"] = expires_at_utc

        code = (data.get("code") or "").strip().upper()
        if not code:
            raise ValueError("Введите промокод.")
        data["code"] = code

        dtype = data.get("discount_type")
        if dtype != "balance":
            raise ValueError("Разрешены только промокоды на пополнение баланса.")
        if not is_created and getattr(model, "discount_type", None) != "balance":
            raise ValueError("Старые промокоды на скидку отключены и не редактируются.")

        try:
            dval = Decimal(str(data.get("discount_value")))
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("Значение скидки должно быть конечным числом.")
        if not dval.is_finite() or dval <= 0:
            raise ValueError("Сумма пополнения должна быть положительным конечным числом.")

        data["scope"] = "global"
        data["category_id"] = None
        data["item_id"] = None


class CartItemsAdmin(WebAccessMixin, ModelView, model=CartItems):
    required_perm = Permission.STATS_VIEW
    column_list = [CartItems.id, CartItems.user_id, CartItems.item_id, CartItems.added_at]
    column_searchable_list = [CartItems.user_id, CartItems.item_id]
    column_sortable_list = [CartItems.id, CartItems.added_at]
    column_default_sort = (CartItems.id, True)
    can_create = False
    can_edit = False
    can_delete = False
    column_formatters = {"added_at": _format_moscow_datetime}
    column_formatters_detail = {"added_at": _format_moscow_datetime}
    column_labels = {
        "id": "ID",
        "user_id": "ID пользователя в Telegram",
        "item_id": "Товар",
        "added_at": "Добавлено в корзину",
    }
    name = "Позиция корзины"
    name_plural = "Корзины пользователей"
    icon = "fa-solid fa-cart-plus"



class ReviewsAdmin(AuditModelView, model=Reviews):
    required_perm = Permission.CATALOG_MANAGE
    column_list = [Reviews.id, Reviews.user_id, Reviews.item_id,
                   Reviews.rating, Reviews.text, Reviews.created_at]
    column_searchable_list = [Reviews.user_id, Reviews.item_id]
    column_sortable_list = [Reviews.id, Reviews.rating, Reviews.created_at]
    column_default_sort = (Reviews.id, True)
    column_formatters = {"created_at": _format_moscow_datetime}
    column_formatters_detail = {"created_at": _format_moscow_datetime}
    column_labels = {
        "id": "ID",
        "user_id": "ID пользователя в Telegram",
        "item_id": "Товар",
        "rating": "Оценка",
        "text": "Текст отзыва",
        "created_at": "Дата создания",
    }
    name = "Отзыв"
    name_plural = "Отзывы"
    icon = "fa-solid fa-star"

    async def _invalidate(self, model: Any) -> None:
        # avg_rating is cached for 600s and keyed by product name, so editing a rating here would otherwise not show up in the bot until it expires.
        item_id = getattr(model, "item_id", None)
        if item_id is None:
            return
        name = await get_item_name_by_id(int(item_id))
        if name:
            safe_create_task(invalidate_rating_cache(name))

    async def after_model_change(self, data: dict, model: Any, is_created: bool, request: Request) -> None:
        await super().after_model_change(data, model, is_created, request)
        await self._invalidate(model)

    async def after_model_delete(self, model: Any, request: Request) -> None:
        await super().after_model_delete(model, request)
        await self._invalidate(model)


# Health & Metrics Endpoints
async def health_check(request: Request) -> JSONResponse:
    """Cheap process liveness probe; it never opens a database session."""
    return JSONResponse(
        {"status": "healthy"},
        status_code=200,
        headers={"Cache-Control": "no-store"},
    )


async def health_ready(request: Request) -> JSONResponse:
    """Authenticated DB/Redis readiness diagnostics.

    PostgreSQL is checked through a short single-flight cache, so a monitoring
    retry storm cannot consume the SQLAlchemy pool.  Public callers only get a
    401 and never trigger a database query.
    """
    if not request.session.get("authenticated"):
        return JSONResponse(
            {"status": "unauthorized"},
            status_code=401,
            headers={"Cache-Control": "no-store"},
        )

    db_ok = await _database_ready()
    status_code = 200 if db_ok else 503

    # Authenticated operators get the full diagnostic view.
    health_status = {
        "status": "healthy" if db_ok else "unhealthy",
        "checks": {"database": "ok" if db_ok else "error"},
    }

    cache = get_cache_manager()
    if cache:
        health_status["checks"]["redis"] = "ok" if cache._healthy else "degraded"
    else:
        health_status["checks"]["redis"] = "not configured"

    metrics = get_metrics()
    if metrics:
        health_status["checks"]["metrics"] = "ok"
        health_status["uptime"] = metrics.get_metrics_summary()["uptime_seconds"]

    return JSONResponse(
        health_status,
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


async def prometheus_metrics(request: Request) -> PlainTextResponse:
    if not request.session.get("authenticated"):
        return PlainTextResponse("Unauthorized", status_code=401)
    metrics = get_metrics()
    if not metrics:
        return PlainTextResponse("# Metrics not initialized\n", status_code=503)
    return PlainTextResponse(metrics.export_to_prometheus(), media_type="text/plain")


async def metrics_json(request: Request) -> JSONResponse:
    if not request.session.get("authenticated"):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    metrics = get_metrics()
    if not metrics:
        return JSONResponse({"error": "Metrics not initialized"}, status_code=503)
    return JSONResponse(metrics.get_metrics_summary(), status_code=200)


# App Factory
def create_admin_app(bot: Any = None) -> Starlette:
    """Build the admin panel app."""
    set_notifier_bot(bot)

    from bot.web.export import export_routes

    async def root_redirect(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/admin")

    routes = [
        Route("/", root_redirect),
        Route("/health", health_check),
        Route("/health/ready", health_ready),
        Route("/metrics", metrics_json),
        Route("/metrics/prometheus", prometheus_metrics),
        Route("/payments/platega/callback", platega_callback_endpoint, methods=["POST"]),
    ] + export_routes

    app = Starlette(routes=routes)
    app.state.bot = bot
    app.add_middleware(
        SessionMiddleware,
        secret_key=EnvKeys.SECRET_KEY,
        max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
        https_only=EnvKeys.session_cookie_secure(),
        same_site="strict",
    )
    app.add_middleware(HealthRateLimitMiddleware)
    app.add_middleware(AdminLoginBodyLimitMiddleware)

    auth_backend = AdminAuth(secret_key=EnvKeys.SECRET_KEY)
    admin = RussianAdmin(
        app,
        engine=Database().engine,
        authentication_backend=auth_backend,
        title="Панель Управления",
        # Override the (blank) SQLAdmin index page with our help/cheat-sheet.
        templates_dir=os.path.join(os.path.dirname(__file__), "templates"),
    )

    admin.add_view(UserAdmin)
    admin.add_view(RoleAdmin)
    admin.add_view(CategoryAdmin)
    admin.add_view(GoodsAdmin)
    admin.add_view(ItemValuesAdmin)
    admin.add_view(ProductReminderView)
    admin.add_view(BoughtGoodsAdmin)
    admin.add_view(OperationsAdmin)
    admin.add_view(PaymentsAdmin)
    admin.add_view(ManualRevenueAdmin)
    admin.add_view(ReferralEarningsAdmin)
    admin.add_view(AuditLogAdmin)
    admin.add_view(PromoCodeAdmin)
    admin.add_view(CartItemsAdmin)
    admin.add_view(CatalogImportView)
    admin.add_view(WebBroadcastView)
    admin.add_view(StockBulkView)
    admin.add_view(BotSettingsView)
    admin.add_view(UserSearchView)
    admin.add_view(RevenueView)
    admin.add_view(ExpensesView)
    admin.add_view(FinanceView)
    admin.add_view(ProcurementControlView)

    # Async RBAC enforcement (sqladmin calls is_accessible WITHOUT await, so
    # the mixin above is menu cosmetics only — this middleware is the lock).
    # Added AFTER SessionMiddleware so scope["session"] is already populated.
    from bot.web.access import WebRBACMiddleware

    perm_map: dict[str, int | None] = {
        view.identity: getattr(view, "required_perm", None) for view in admin.views
    }
    perm_map.update({
        "/export/users": Permission.USERS_MANAGE,
        "/export/purchases": Permission.STATS_VIEW,
        "/export/operations": Permission.STATS_VIEW,
        "/export/payments": Permission.STATS_VIEW,
    })
    app.add_middleware(WebRBACMiddleware, perm_map=perm_map)

    return app

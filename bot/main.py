import asyncio
import hmac
import logging
import json
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from bot.database.methods import check_category_cached
from bot.database.methods.audit import start_audit_buffer, stop_audit_buffer
from bot.handlers.admin.shop_management import init_stats_cache
from bot.misc import EnvKeys
from bot.handlers import register_all_handlers
from bot.database.models import register_models
from bot.logger_mesh import configure_logging
from bot.middleware import setup_rate_limiting, RateLimitConfig, LocaleMiddleware
from bot.middleware.security import SecurityMiddleware, AuthenticationMiddleware, set_auth_middleware
from bot.misc.caching import init_cache_manager, get_cache_manager
from bot.misc.caching import CacheScheduler
from bot.misc.caching import get_redis_storage
from bot.misc.services import RecoveryManager, CleanupManager
from bot.misc.metrics import init_metrics, get_metrics, AnalyticsMiddleware
from bot.database.main import Database as _Database


@dataclass
class AppContext:
    """Holds the lifecycle-managed components for one bot run.

    Replaces the old module-level globals so startup, the run loop and shutdown
    pass state explicitly instead of reaching into ambient globals.
    """
    recovery_manager: Optional[RecoveryManager] = None
    cleanup_manager: Optional[CleanupManager] = None
    cache_scheduler: Optional[CacheScheduler] = None
    admin_server: Optional["object"] = None  # uvicorn.Server, imported lazily
    admin_server_task: Optional[asyncio.Task] = None
    partner_api_server: Optional["object"] = None
    partner_api_server_task: Optional[asyncio.Task] = None
    webhook_server: Optional["object"] = None  # uvicorn.Server for the webhook listener
    webhook_server_task: Optional[asyncio.Task] = None
    webhook_active: bool = False
    stopping: bool = False


# ---------------------------------------------------------------------------
# Startup steps (each does one thing; called in order by `_startup`)
# ---------------------------------------------------------------------------

def _setup_rate_limiting(dp: Dispatcher, auth_middleware: AuthenticationMiddleware):
    """Register the rate-limit middleware (shares the auth role cache)."""
    return setup_rate_limiting(dp, RateLimitConfig(), auth_middleware=auth_middleware)


def _register_middlewares(
        dp: Dispatcher,
        analytics_middleware: AnalyticsMiddleware,
        auth_middleware: AuthenticationMiddleware,
        security_middleware: SecurityMiddleware,
) -> None:
    """Register non-rate-limit middlewares."""
    locale_middleware = LocaleMiddleware()
    dp.message.middleware(locale_middleware)
    dp.callback_query.middleware(locale_middleware)

    dp.message.middleware(analytics_middleware)
    dp.callback_query.middleware(analytics_middleware)

    dp.message.middleware(auth_middleware)
    dp.callback_query.middleware(auth_middleware)

    dp.message.middleware(security_middleware)
    dp.callback_query.middleware(security_middleware)

    from bot.middleware.subscription import SubscriptionMiddleware
    from bot.middleware.activity import BotActivityMiddleware
    subscription_middleware = SubscriptionMiddleware()
    dp.message.middleware(subscription_middleware)
    dp.callback_query.middleware(subscription_middleware)

    # Registered after the gate, so only events allowed through it count as
    # active interactions. /start is counted explicitly in its handler.
    activity_middleware = BotActivityMiddleware()
    dp.message.middleware(activity_middleware)
    dp.callback_query.middleware(activity_middleware)

    logging.info("Security middleware initialized")


async def _setup_caching(storage) -> Optional[CacheScheduler]:
    """Initialize the Redis cache manager, warm critical caches and start the
    cache scheduler. Returns the started scheduler, or None when Redis is off.

    Reuses the dispatcher's storage rather than opening a second connection, so
    the cache and the FSM agree on whether Redis is actually available.
    """
    if not isinstance(storage, RedisStorage):
        logging.warning("Redis not available - caching disabled")
        return None

    # Use the same Redis for caching
    await init_cache_manager(storage.redis)

    # Initialize the statistics cache
    init_stats_cache()

    # Warm up critical caches at startup
    await warm_up_critical_caches()

    logging.info("Cache system initialized and warmed up")

    scheduler = CacheScheduler()
    await scheduler.start()
    return scheduler


async def _start_admin_server(bot: Bot):
    """Create and start the admin web server as a background task; return it."""
    import uvicorn
    from bot.web import create_admin_app

    # The bot goes in so panel edits can reach users (e.g. restock notifications).
    admin_app = create_admin_app(bot)
    config = uvicorn.Config(
        admin_app,
        host=EnvKeys.ADMIN_HOST,
        port=EnvKeys.ADMIN_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    # Keep a strong reference: the loop only holds a weak one, so a task nobody references can be garbage-collected mid-run.
    task = asyncio.create_task(server.serve())
    return server, task


async def _start_partner_api_server(bot: Bot):
    """Start the separate loopback-only partner API when explicitly enabled."""
    if EnvKeys.PARTNER_API_ENABLED != "1":
        return None, None
    import uvicorn
    from bot.web.api import create_partner_api_app

    app = create_partner_api_app(bot)
    config = uvicorn.Config(
        app,
        host=EnvKeys.PARTNER_API_HOST,
        port=EnvKeys.PARTNER_API_PORT,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        if task.done():
            try:
                task.result()
            except Exception as exc:
                raise RuntimeError("Partner API failed to start") from exc
            raise RuntimeError("Partner API stopped before becoming ready")
        await asyncio.sleep(0.1)
    else:
        server.should_exit = True
        await asyncio.gather(task, return_exceptions=True)
        raise TimeoutError("Partner API did not become ready within five seconds")
    return server, task


async def _startup(dp: Dispatcher, bot: Bot, ctx: AppContext, storage) -> None:
    """Wire the application together, mutating `ctx` with started components."""
    # Registration of handlers and models
    register_all_handlers(dp)
    await register_models()

    # Security & authentication middleware
    security_middleware = SecurityMiddleware()
    auth_middleware = AuthenticationMiddleware()
    set_auth_middleware(auth_middleware)
    await auth_middleware.load_blocked_users()

    # Rate limiting (shares auth_middleware's role cache)
    _setup_rate_limiting(dp, auth_middleware)

    # Metrics + analytics middleware
    metrics = init_metrics()
    analytics_middleware = AnalyticsMiddleware(metrics)

    _register_middlewares(dp, analytics_middleware, auth_middleware, security_middleware)

    # Batch audit
    await start_audit_buffer()

    # Caching (optional Redis) and background services
    ctx.cache_scheduler = await _setup_caching(storage)

    ctx.recovery_manager = RecoveryManager(bot)
    await ctx.recovery_manager.start()

    ctx.cleanup_manager = CleanupManager()
    await ctx.cleanup_manager.start()

    ctx.admin_server, ctx.admin_server_task = await _start_admin_server(bot)
    ctx.partner_api_server, ctx.partner_api_server_task = await _start_partner_api_server(bot)

    logging.info(f"Recovery and admin panel initialized on {EnvKeys.ADMIN_HOST}:{EnvKeys.ADMIN_PORT}")
    if ctx.partner_api_server_task:
        logging.info(
            "Partner API initialized on %s:%s",
            EnvKeys.PARTNER_API_HOST, EnvKeys.PARTNER_API_PORT,
        )


async def warm_up_critical_caches():
    """Warming of critical caches at startup"""
    from bot.database.methods.read import (
        get_user_count_cached,
        select_admins_cached
    )

    cache_manager = get_cache_manager()
    if not cache_manager:
        return

    try:
        from bot.database.methods import query_categories

        # Independent warm-ups
        _counts, categories = await asyncio.gather(
            asyncio.gather(get_user_count_cached(), select_admins_cached()),
            query_categories(limit=5),
        )
        await asyncio.gather(*(check_category_cached(c) for c in categories))

        logging.info("Critical caches warmed up successfully")
    except Exception as e:
        logging.error(f"Failed to warm up caches: {e}")


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

async def _shutdown(ctx: AppContext, bot: Bot) -> None:
    """Graceful shutdown: persist metrics, stop services, close connections."""
    logging.info("Starting shutdown...")
    ctx.stopping = True

    # Create a data directory if it does not exist
    Path("data").mkdir(exist_ok=True)

    # Saving metrics
    metrics = get_metrics()
    if metrics:
        summary = metrics.get_metrics_summary()
        with open("data/final_metrics.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    if ctx.recovery_manager:
        await ctx.recovery_manager.stop()

    if ctx.cleanup_manager:
        await ctx.cleanup_manager.stop()

    if ctx.cache_scheduler:
        await ctx.cache_scheduler.stop()

    # Delete webhook if it was active
    if ctx.webhook_active:
        try:
            await bot.delete_webhook()
        except Exception as e:
            logging.error(f"Failed to delete webhook: {e}")

    # Admin, partner API, and webhook servers stop
    if ctx.admin_server:
        ctx.admin_server.should_exit = True
    if ctx.partner_api_server:
        ctx.partner_api_server.should_exit = True
    if ctx.webhook_server:
        ctx.webhook_server.should_exit = True
    server_tasks = [
        task for task in (
            ctx.admin_server_task,
            ctx.partner_api_server_task,
            ctx.webhook_server_task,
        ) if task and not task.done()
    ]
    if server_tasks:
        await asyncio.gather(*server_tasks, return_exceptions=True)

    # Close CryptoPay shared HTTP session
    from bot.misc.services.payment import CryptoPayAPI
    await CryptoPayAPI.close_session()

    # Let fire-and-forget invalidations and audit rows land while the engine and Redis are both still open.
    from bot.database.methods.cache_utils import drain_background_tasks
    await drain_background_tasks()

    # Drain buffered audit rows while the engine is still open.
    await stop_audit_buffer()

    # Close database engine
    await _Database().dispose()

    logging.info("Shutdown completed")

    # Flush queued file-log records (also registered via atexit as a fallback).
    from bot.logger_mesh import shutdown_logging
    shutdown_logging()


# ---------------------------------------------------------------------------
# Run loop
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    configure_logging(
        console=EnvKeys.LOG_TO_STDOUT == "1",
        debug=EnvKeys.DEBUG == "1"
    )


_ALLOWED_UPDATES = [
    "message",
    "callback_query",
    "pre_checkout_query",
    "successful_payment",
]


def _install_shutdown_signals(callback) -> None:
    """Route SIGINT/SIGTERM to `callback`.

    aiogram installs these itself for polling (handle_signals=True); webhook mode
    has to do it, or SIGTERM from `docker stop` kills the process outright and
    the graceful shutdown never runs.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, AttributeError, ValueError, RuntimeError):
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(callback))
            except (ValueError, OSError):
                logging.warning("Could not install a shutdown handler for %s", sig)


async def _run_webhook(dp: Dispatcher, bot: Bot, ctx: AppContext) -> None:
    """Run in webhook mode on a dedicated app and port.

    The webhook endpoint has to be reachable from Telegram; the admin panel must
    not be.
    """
    import uvicorn
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response
    from starlette.routing import Route
    from aiogram.types import Update

    webhook_path = EnvKeys.WEBHOOK_PATH or "/webhook"
    webhook_url = f"{EnvKeys.WEBHOOK_URL}{webhook_path}"

    await bot.set_webhook(
        url=webhook_url,
        secret_token=EnvKeys.WEBHOOK_SECRET or None,
        allowed_updates=_ALLOWED_UPDATES,
    )
    ctx.webhook_active = True
    logging.info(f"Webhook set: {webhook_url}")

    max_body_bytes = 256 * 1024
    max_in_flight = 100
    update_semaphore = asyncio.Semaphore(max_in_flight)

    # Strong references to in-flight update tasks (the loop keeps only weak ones).
    pending: set[asyncio.Task] = set()

    def _update_done(task: asyncio.Task) -> None:
        pending.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logging.error(
                "Webhook update task failed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _feed_update(update: Update) -> None:
        async with update_semaphore:
            await dp.feed_update(bot=bot, update=update)

    async def webhook_handler(request: Request) -> Response:
        """Process incoming webhook updates"""
        # Verify secret token
        if EnvKeys.WEBHOOK_SECRET:
            token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(str(token), str(EnvKeys.WEBHOOK_SECRET)):
                return Response(status_code=403)

        raw_content_length = request.headers.get("content-length")
        if raw_content_length:
            try:
                if int(raw_content_length) > max_body_bytes:
                    return Response(status_code=413)
            except ValueError:
                return Response(status_code=400)

        chunks: list[bytes] = []
        body_size = 0
        async for chunk in request.stream():
            body_size += len(chunk)
            if body_size > max_body_bytes:
                return Response(status_code=413)
            chunks.append(chunk)
        body = b"".join(chunks)
        try:
            update = Update.model_validate_raw(body)
        except Exception as e:
            # A malformed body is not worth a 500 — Telegram would retry it.
            logging.warning(f"Discarding unparseable webhook update: {e}")
            return Response(status_code=200)

        if len(pending) >= max_in_flight:
            return Response(status_code=429, headers={"Retry-After": "1"})
        task = asyncio.create_task(_feed_update(update))
        pending.add(task)
        task.add_done_callback(_update_done)
        return Response(status_code=200)

    webhook_app = Starlette(routes=[Route(webhook_path, webhook_handler, methods=["POST"])])
    config = uvicorn.Config(
        webhook_app,
        host=EnvKeys.WEBHOOK_HOST,
        port=EnvKeys.WEBHOOK_PORT,
        log_level="warning",
    )
    ctx.webhook_server = uvicorn.Server(config)
    ctx.webhook_server_task = asyncio.create_task(ctx.webhook_server.serve())
    logging.info(
        "Webhook listener on %s:%s%s",
        EnvKeys.WEBHOOK_HOST, EnvKeys.WEBHOOK_PORT, webhook_path,
    )

    stop = asyncio.Event()
    _install_shutdown_signals(stop.set)

    stop_waiter = asyncio.create_task(stop.wait())
    watched = {stop_waiter, ctx.webhook_server_task}
    if ctx.partner_api_server_task is not None:
        watched.add(ctx.partner_api_server_task)
    try:
        done, _pending_waiters = await asyncio.wait(
            watched,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ctx.partner_api_server_task is not None and ctx.partner_api_server_task in done and not stop.is_set():
            try:
                error = ctx.partner_api_server_task.exception()
            except asyncio.CancelledError as exc:
                raise RuntimeError("Partner API listener was cancelled unexpectedly") from exc
            if error is not None:
                raise RuntimeError("Partner API listener failed") from error
            raise RuntimeError("Partner API listener stopped unexpectedly")
        if ctx.webhook_server_task in done and not stop.is_set():
            try:
                error = ctx.webhook_server_task.exception()
            except asyncio.CancelledError as exc:
                raise RuntimeError("Webhook listener was cancelled unexpectedly") from exc
            if error is not None:
                raise RuntimeError("Webhook listener failed") from error
            raise RuntimeError("Webhook listener stopped unexpectedly")
    finally:
        stop_waiter.cancel()
        await asyncio.gather(stop_waiter, return_exceptions=True)

    # Stop accepting new updates, then let the in-flight ones finish.
    ctx.webhook_server.should_exit = True
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_polling_with_admin_watch(dp: Dispatcher, bot: Bot, ctx: AppContext) -> None:
    """Treat an unexpected admin listener exit as a process-fatal failure."""
    polling_task = asyncio.create_task(dp.start_polling(
        bot,
        allowed_updates=_ALLOWED_UPDATES,
        handle_signals=True,
        tasks_concurrency_limit=EnvKeys.POLLING_TASKS_CONCURRENCY,
    ))
    watched = {polling_task}
    if ctx.admin_server_task is not None:
        watched.add(ctx.admin_server_task)
    if ctx.partner_api_server_task is not None:
        watched.add(ctx.partner_api_server_task)
    try:
        done, _pending = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
        if ctx.partner_api_server_task is not None and ctx.partner_api_server_task in done and not ctx.stopping:
            try:
                error = ctx.partner_api_server_task.exception()
            except asyncio.CancelledError as exc:
                raise RuntimeError("Partner API listener was cancelled unexpectedly") from exc
            if error is not None:
                raise RuntimeError("Partner API listener failed") from error
            raise RuntimeError("Partner API listener stopped unexpectedly")
        if ctx.admin_server_task is not None and ctx.admin_server_task in done and not ctx.stopping:
            try:
                error = ctx.admin_server_task.exception()
            except asyncio.CancelledError as exc:
                raise RuntimeError("Admin listener was cancelled unexpectedly") from exc
            if error is not None:
                raise RuntimeError("Admin listener failed") from error
            raise RuntimeError("Admin listener stopped unexpectedly")
        await polling_task
    finally:
        if not polling_task.done():
            polling_task.cancel()
        await asyncio.gather(polling_task, return_exceptions=True)
        if ctx.admin_server_task and not ctx.admin_server_task.done():
            ctx.admin_server_task.cancel()
            await asyncio.gather(ctx.admin_server_task, return_exceptions=True)


async def start_bot() -> None:
    """Start the bot with enhanced security and monitoring"""

    _configure_logging()
    EnvKeys.validate()

    # Retrieve storage (Redis or Memory). get_redis_storage pings first, so a None here means Redis is genuinely unreachable, not merely unconfigured.
    storage = await get_redis_storage() or MemoryStorage()
    if isinstance(storage, MemoryStorage):
        logging.warning(
            "Using MemoryStorage - FSM states will be lost on restart! "
            "Consider setting up Redis for production."
        )

    dp = Dispatcher(storage=storage)
    ctx = AppContext()

    async with Bot(
            token=EnvKeys.TOKEN,
            default=DefaultBotProperties(
                parse_mode="HTML",
                link_preview_is_disabled=False,
                protect_content=False,
            ),
    ) as bot:
        bot_info = await bot.get_me()
        logging.info(f"Starting bot: @{bot_info.username} (ID: {bot_info.id})")

        await _startup(dp, bot, ctx, storage)

        try:
            if EnvKeys.WEBHOOK_ENABLED == "1":
                await _run_webhook(dp, bot, ctx)
            else:
                await _run_polling_with_admin_watch(dp, bot, ctx)
        except Exception as e:
            logging.error(f"Bot error: {e}")
            raise
        finally:
            # Correctly closing connections (called once, whether normal or abnormal exit)
            await _shutdown(ctx, bot)

            if isinstance(storage, RedisStorage):
                await storage.close()
                logging.info("Redis connection closed")

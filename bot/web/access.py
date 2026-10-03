"""Personal web panel logins with role-based view access.

The owner always uses ADMIN_USERNAME/ADMIN_PASSWORD from the env (full access).
Extra people get rows in ``web_admins`` (see scripts/manage_web_admin.py):
the linked bot role's permission bits decide which views they may open.
Roles themselves stay owner-only, so nobody can escalate their own rights.
Passwords are PBKDF2-HMAC-SHA256 hashes (stdlib, no extra dependency).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from typing import Any

from sqlalchemy import select

from bot.database.main import Database
from bot.database.models.main import Permission, Role, WebAdmin
from bot.logger_mesh import logger

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    """Hash a password for storage: pbkdf2_sha256$iters$salt$hex."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password check; False on any malformed hash."""
    try:
        algo, iters, salt, digest = str(stored).split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iters)
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


LOGIN_BODY_MAX_BYTES = 16 * 1024


class AdminLoginBodyLimitMiddleware:
    """Reject oversized admin login bodies before form parsing.

    Starlette's multipart limits do not cap an URL-encoded request as a whole,
    and a request without ``Content-Length`` can otherwise stream indefinitely.
    The login body is tiny by design, so buffering at most 16 KiB gives the
    parser a bounded receive channel without affecting any other admin route.
    """

    def __init__(
        self,
        app: Any,
        max_bytes: int = LOGIN_BODY_MAX_BYTES,
        max_concurrent: int = 16,
    ):
        self.app = app
        self.max_bytes = int(max_bytes)
        self._concurrency = asyncio.Semaphore(int(max_concurrent))

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("path") != "/admin/login"
            or scope.get("method", "").upper() != "POST"
        ):
            await self.app(scope, receive, send)
            return

        from starlette.responses import PlainTextResponse

        async with self._concurrency:
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            content_length = headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    declared_length = self.max_bytes + 1
                if declared_length > self.max_bytes:
                    response = PlainTextResponse("Request body too large", status_code=413)
                    await response(scope, receive, send)
                    return

            messages: list[dict] = []
            total = 0
            while True:
                message = await receive()
                if message.get("type") == "http.disconnect":
                    messages.append(message)
                    break
                if message.get("type") != "http.request":
                    messages.append(message)
                    continue

                body = message.get("body", b"")
                total += len(body)
                if total > self.max_bytes:
                    response = PlainTextResponse("Request body too large", status_code=413)
                    await response(scope, receive, send)
                    return
                messages.append(message)
                if not message.get("more_body", False):
                    break

            position = 0

            async def replay_receive() -> dict:
                nonlocal position
                if position < len(messages):
                    message = messages[position]
                    position += 1
                    return message
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_receive, send)


class HealthRateLimitMiddleware:
    """Keep public liveness probes from consuming unbounded request work."""

    def __init__(
        self,
        app: Any,
        max_requests: int = 120,
        window_seconds: int = 60,
        max_clients: int = 10_000,
    ):
        self.app = app
        self.max_requests = int(max_requests)
        self.window_seconds = int(window_seconds)
        self.max_clients = int(max_clients)
        self._hits: dict[str, list[float]] = {}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/health":
            await self.app(scope, receive, send)
            return

        from starlette.responses import JSONResponse

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        ip = client_ip_from_scope(scope, headers)
        now = time.monotonic()
        hits = [timestamp for timestamp in self._hits.get(ip, []) if now - timestamp < self.window_seconds]
        if len(hits) >= self.max_requests:
            response = JSONResponse(
                {"status": "rate_limited"},
                status_code=429,
                headers={"Retry-After": str(self.window_seconds), "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        if ip not in self._hits and len(self._hits) >= self.max_clients:
            oldest_ip = min(self._hits, key=lambda value: self._hits[value][-1])
            self._hits.pop(oldest_ip, None)
        hits.append(now)
        self._hits[ip] = hits
        await self.app(scope, receive, send)


async def find_web_admin(login: str) -> WebAdmin | None:
    """Active web admin by login (case-sensitive), or None."""
    name = (login or "").strip()
    if not name:
        return None
    async with Database().session() as s:
        return (await s.execute(
            select(WebAdmin).where(WebAdmin.login == name, WebAdmin.is_active.is_(True))
        )).scalars().first()


async def role_permissions(role_id: int | None) -> int:
    """Permission bits of a role, 0 when the role is missing."""
    if not role_id:
        return 0
    async with Database().session() as s:
        perms = (await s.execute(
            select(Role.permissions).where(Role.id == int(role_id))
        )).scalar()
    try:
        return int(perms or 0)
    except (TypeError, ValueError):
        return 0


async def resolve_web_perms(request: Any) -> int | None:
    """Permission bits for this web session.

    None = not authenticated at all. Owner sessions get every bit.
    Result is cached on request.state for the rest of the request.
    """
    session = getattr(request, "session", {}) or {}
    if not session.get("authenticated"):
        return None
    state = getattr(request, "state", None)
    if state is not None and hasattr(state, "web_perms"):
        return state.web_perms
    if session.get("web_owner"):
        if state is not None:
            state.web_perms = _FULL_PERMS
        return _FULL_PERMS
    web_admin_id = session.get("web_admin_id")
    perms = 0
    if web_admin_id:
        async with Database().session() as s:
            row = (await s.execute(
                select(WebAdmin.role_id).where(
                    WebAdmin.id == int(web_admin_id),
                    WebAdmin.is_active.is_(True),
                )
            )).first()
        if row is not None:
            perms = await role_permissions(row[0])
    if state is not None:
        state.web_perms = perms
    return perms


async def web_session_active(request: Any) -> bool:
    """Whether this session still maps to the owner or a live web login."""
    session = getattr(request, "session", {}) or {}
    if not session.get("authenticated"):
        return False
    if session.get("web_owner"):
        return True
    web_admin_id = session.get("web_admin_id")
    if not web_admin_id:
        return False
    async with Database().session() as s:
        row = (await s.execute(
            select(WebAdmin.id).where(
                WebAdmin.id == int(web_admin_id),
                WebAdmin.is_active.is_(True),
            )
        )).first()
    return row is not None


async def has_web_perm(request: Any, bit: int) -> bool:
    """Whether this web session is authenticated and holds ``bit``."""
    perms = await resolve_web_perms(request)
    if perms is None:
        return False
    return Permission.granted(perms, bit)


def session_snapshot_perms(session: dict) -> int | None:
    """Permission bits stored in the session cookie at login (may be stale).

    Used ONLY for sync menu cosmetics — real enforcement re-resolves from DB.
    """
    session = session or {}
    if not session.get("authenticated"):
        return None
    if session.get("web_owner"):
        return _FULL_PERMS
    perms = session.get("web_perms")
    try:
        return int(perms) if perms is not None else 0
    except (TypeError, ValueError):
        return 0


_FULL_PERMS = (
    Permission.USE | Permission.BROADCAST | Permission.SETTINGS_MANAGE
    | Permission.USERS_MANAGE | Permission.CATALOG_MANAGE
    | Permission.ADMINS_MANAGE | Permission.OWN | Permission.STATS_VIEW
    | Permission.BALANCE_MANAGE | Permission.PROMO_MANAGE
)


class WebAccessMixin:
    """Role gate for admin views. Set ``required_perm`` on the view class.

    NOTE: sqladmin calls is_accessible/is_visible WITHOUT await, so these
    MUST stay sync and read only the session snapshot. Real enforcement is
    WebRBACMiddleware below, which re-resolves fresh permissions from DB.
    """

    required_perm: int | None = None

    def is_accessible(self, request: Any) -> bool:
        perms = session_snapshot_perms(getattr(request, "session", {}) or {})
        if perms is None:
            return False
        if self.required_perm is None:
            return True
        return Permission.granted(perms, self.required_perm)

    def is_visible(self, request: Any) -> bool:
        return self.is_accessible(request)


def client_ip_from_scope(scope: dict, headers: dict) -> str:
    """Real client IP, trusting X-Forwarded-For only from a loopback peer."""
    peer = ""
    client = scope.get("client")
    if client:
        peer = client[0] if isinstance(client, (list, tuple)) else str(client)
    if peer in ("127.0.0.1", "::1"):
        fwd = headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip() or peer
    return peer or "unknown"


class WebRBACMiddleware:
    """Async enforcement for admin routes (the part sqladmin can't await).

    Must be added to the Starlette app AFTER SessionMiddleware so
    scope["session"] is populated. ``perm_map`` maps sqladmin view identity
    (first /admin/<identity>/ path segment, or the /export/<name> route)
    to the required Permission bit (None = any authenticated session).
    Unknown /admin/* identities are denied for non-owners (fail closed).
    """

    def __init__(self, app: Any, perm_map: dict[str, int | None]):
        self.app = app
        self.perm_map = dict(perm_map)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        from starlette.responses import PlainTextResponse

        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "") or ""
        if path in (
            "/",
            "/health",
            "/health/ready",
            "/metrics",
            "/metrics/prometheus",
            # The callback authenticates each request with Platega's merchant
            # ID and API secret inside its endpoint handler.
            "/payments/platega/callback",
        ):
            await self.app(scope, receive, send)
            return
        if path == "/admin/login" or path.startswith(("/admin/logout", "/admin/statics", "/static")):
            await self.app(scope, receive, send)
            return

        bit: int | None | str | None = "unknown"
        if path == "/admin" or path == "/admin/":
            bit = None  # index page; menu items hide individually
        elif path.startswith("/admin/"):
            ident = path[len("/admin/"):].split("/", 1)[0]
            bit = self.perm_map.get(ident, "unknown")
        elif path.startswith("/export/"):
            bit = self.perm_map.get(path, "unknown")

        session = scope.get("session") or {}
        if bit == "unknown":
            # Not our map (404 later) — but never let a non-owner probe further.
            if not session.get("authenticated"):
                await self.app(scope, receive, send)
                return
            if not session.get("web_owner"):
                await self._deny(send)
                return
            await self.app(scope, receive, send)
            return

        if not session.get("authenticated"):
            await self.app(scope, receive, send)
            return
        if session.get("web_owner"):
            await self.app(scope, receive, send)
            return

        from starlette.requests import Request

        request = Request(scope, receive)
        if bit is None:
            if await web_session_active(request):
                await self.app(scope, receive, send)
                return
            await self._deny(send)
            return
        if await has_web_perm(request, bit):
            await self.app(scope, receive, send)
            return
        logger.warning("RBAC deny: path=%s session=%s", path, session.get("web_admin_id"))
        await self._deny(send)

    async def _deny(self, send: Any) -> None:
        from starlette.responses import PlainTextResponse

        response = PlainTextResponse("Forbidden", status_code=403)

        async def _receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        await response({"type": "http", "method": "GET", "headers": []}, _receive, send)

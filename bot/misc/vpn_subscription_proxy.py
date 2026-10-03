"""Opaque per-customer HTTPS links for one private VPN subscription."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from urllib.parse import urlsplit

import aiohttp
from sqlalchemy import select, update
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from bot.database import Database
from bot.database.models import User
from bot.database.models.main import VpnSubscriptionLink
from bot.misc import EnvKeys

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
MAX_SUBSCRIPTION_BYTES = 2 * 1024 * 1024
MAX_REQUESTS_PER_MINUTE = 120
_WINDOW_SECONDS = 60.0
_MAX_RATE_LIMIT_KEYS = 4096
_REQUESTS: OrderedDict[str, deque[float]] = OrderedDict()

_FORWARDED_REQUEST_HEADERS = (
    "accept",
    "accept-encoding",
    "if-modified-since",
    "if-none-match",
    "if-range",
    "range",
    "user-agent",
)

# Keep standard config/profile headers understood by HAPP and other clients.
# In particular, do not forward Set-Cookie, Location, Server, or arbitrary
# provider headers. URL-migration metadata is intentionally omitted so clients
# cannot be redirected around this per-user proxy to the private upstream URL.
_FORWARDED_RESPONSE_HEADERS = frozenset({
    "accept-ranges",
    "announce",
    "cache-control",
    "content-disposition",
    "content-encoding",
    "content-length",
    "content-range",
    "content-type",
    "custom-tunnel-config",
    "dns",
    "etag",
    "expires",
    "http-auth-mode",
    "http-auth-password",
    "http-auth-user",
    "last-modified",
    "profile-title",
    "profile-update-interval",
    "profile-web-page-url",
    "routing-enable",
    "socks-auth-mode",
    "socks-auth-password",
    "socks-auth-user",
    "sub-expire",
    "sub-expire-button-link",
    "sub-info-button-link",
    "sub-info-button-text",
    "sub-info-color",
    "sub-info-text",
    "subscription-userinfo",
    "support-url",
    "vary",
})


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _valid_public_base_url() -> str | None:
    raw = str(getattr(EnvKeys, "VPN_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if not raw or len(raw) > 500:
        return None
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            return None
        _ = parsed.port  # Reject malformed/out-of-range ports.
    except ValueError:
        return None
    return raw


def configured_upstream_url() -> str | None:
    """Return only the configured provider URL; never accept a request URL."""
    raw = str(getattr(EnvKeys, "VPN_UPSTREAM_SUBSCRIPTION_URL", "") or "").strip()
    if not raw or len(raw) > 2048:
        return None
    try:
        parsed = urlsplit(raw)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith("/")
            or parsed.fragment
        ):
            return None
        port = parsed.port
    except ValueError:
        return None

    if parsed.scheme == "https":
        if hostname != "oversub.cloud" and not hostname.endswith(".oversub.cloud"):
            return None
        if port not in (None, 443):
            return None
    elif parsed.scheme == "http":
        # Plain HTTP is permitted only for an explicit loopback development
        # fixture. Production provider traffic must use HTTPS.
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = hostname == "localhost"
        if not is_loopback or port is None:
            return None
    else:
        return None
    return raw


def vpn_proxy_is_configured() -> bool:
    return configured_upstream_url() is not None and _valid_public_base_url() is not None


def _build_public_link(token: str) -> str | None:
    base_url = _valid_public_base_url()
    if base_url is None:
        return None
    return f"{base_url}/sub/{token}"


async def issue_vpn_subscription_link(user_id: int) -> str | None:
    """Create a fresh link for an existing, unblocked Telegram account.

    Only a SHA-256 digest is stored. The one-time plaintext is returned to the
    buyer/admin and also saved in the buyer-scoped purchase receipt.
    """
    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError):
        return None
    if normalized_user_id <= 0:
        return None

    async with Database().session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == normalized_user_id).with_for_update()
        )).scalar_one_or_none()
        if user is None or bool(user.is_blocked):
            return None
        return await create_vpn_subscription_link_in_session(session, normalized_user_id)


async def create_vpn_subscription_link_in_session(session, user_id: int) -> str | None:
    """Create a link inside the caller's purchase transaction."""
    token = secrets.token_urlsafe(32)
    link = _build_public_link(token)
    if link is None:
        return None
    session.add(VpnSubscriptionLink(
        user_id=int(user_id),
        token_hash=_token_digest(token),
        created_at=datetime.now(timezone.utc),
    ))
    await session.flush()
    return link


async def revoke_vpn_subscription_links(user_id: int) -> bool:
    """Revoke every active VPN link for one customer, without affecting others."""
    try:
        normalized_user_id = int(user_id)
    except (TypeError, ValueError):
        return False
    if normalized_user_id <= 0:
        return False

    async with Database().session() as session:
        result = await session.execute(
            update(VpnSubscriptionLink)
            .where(
                VpnSubscriptionLink.user_id == normalized_user_id,
                VpnSubscriptionLink.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
    return bool(result.rowcount)


async def _link_is_active(token: str) -> bool:
    digest = _token_digest(token)
    async with Database().session() as session:
        found = (await session.execute(
            select(VpnSubscriptionLink.id)
            .join(User, User.telegram_id == VpnSubscriptionLink.user_id)
            .where(
                VpnSubscriptionLink.token_hash == digest,
                VpnSubscriptionLink.revoked_at.is_(None),
                User.is_blocked.is_not(True),
            )
            .limit(1)
        )).scalar_one_or_none()
    return found is not None


def _is_rate_limited(token: str) -> bool:
    now = time.monotonic()
    bucket_key = _token_digest(token)
    entries = _REQUESTS.get(bucket_key)
    if entries is None:
        if len(_REQUESTS) >= _MAX_RATE_LIMIT_KEYS:
            _REQUESTS.popitem(last=False)
        entries = deque()
        _REQUESTS[bucket_key] = entries
    else:
        _REQUESTS.move_to_end(bucket_key)

    cutoff = now - _WINDOW_SECONDS
    while entries and entries[0] <= cutoff:
        entries.popleft()
    if len(entries) >= MAX_REQUESTS_PER_MINUTE:
        return True
    entries.append(now)
    return False


def _response_headers(upstream_headers) -> dict[str, str]:
    return {
        name.lower(): value
        for name, value in upstream_headers.items()
        if name.lower() in _FORWARDED_RESPONSE_HEADERS
    }


def _upstream_subscription_expired(headers) -> bool:
    """Detect an absolute expiry advertised by the shared subscription."""
    userinfo = headers.get("subscription-userinfo", "")
    for part in userinfo.split(";"):
        key, separator, value = part.strip().partition("=")
        if not separator or key.strip().lower() != "expire":
            continue
        try:
            return int(value.strip()) > 0 and int(value.strip()) <= int(time.time())
        except (TypeError, ValueError):
            return False
    return False


def _unavailable(status_code: int = 502) -> Response:
    return Response(
        "Subscription temporarily unavailable",
        status_code=status_code,
        media_type="text/plain",
        headers={"Cache-Control": "no-store"},
    )


async def proxy_subscription(request: Request) -> Response:
    token = str(request.path_params.get("token") or "")
    if not TOKEN_PATTERN.fullmatch(token) or not await _link_is_active(token):
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    if _is_rate_limited(token):
        return Response(
            "Too many requests",
            status_code=429,
            headers={"Cache-Control": "no-store", "Retry-After": "60"},
            media_type="text/plain",
        )

    upstream_url = configured_upstream_url()
    if upstream_url is None:
        return _unavailable(503)

    forwarded_headers = {
        name: request.headers[name]
        for name in _FORWARDED_REQUEST_HEADERS
        if name in request.headers
    }
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    try:
        async with aiohttp.ClientSession(
            timeout=timeout,
            auto_decompress=False,
            trust_env=False,
        ) as client:
            upstream_response = await client.request(
                request.method,
                upstream_url,
                headers=forwarded_headers,
                allow_redirects=False,
            )
            if request.method == "HEAD" and upstream_response.status in (405, 501):
                upstream_response.release()
                upstream_response = await client.get(
                    upstream_url,
                    headers=forwarded_headers,
                    allow_redirects=False,
            )

            status_code = int(upstream_response.status)
            if status_code in (401, 403, 404, 410) or _upstream_subscription_expired(
                upstream_response.headers
            ):
                upstream_response.release()
                return Response(status_code=410, headers={"Cache-Control": "no-store"})
            if 300 <= status_code < 400 or status_code >= 500:
                upstream_response.release()
                return _unavailable()

            content_length = upstream_response.content_length
            if content_length is not None and content_length > MAX_SUBSCRIPTION_BYTES:
                upstream_response.release()
                return _unavailable()

            body = bytearray()
            if request.method != "HEAD" and status_code not in (204, 304):
                async for chunk in upstream_response.content.iter_chunked(64 * 1024):
                    if len(body) + len(chunk) > MAX_SUBSCRIPTION_BYTES:
                        upstream_response.release()
                        return _unavailable()
                    body.extend(chunk)

            headers = _response_headers(upstream_response.headers)
            upstream_response.release()
            return Response(
                content=bytes(body),
                status_code=status_code,
                headers=headers,
            )
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return _unavailable()


routes = [
    Route("/sub/{token}", proxy_subscription, methods=["GET", "HEAD"], name="vpn_subscription_proxy"),
]


__all__ = [
    "MAX_REQUESTS_PER_MINUTE",
    "MAX_SUBSCRIPTION_BYTES",
    "configured_upstream_url",
    "create_vpn_subscription_link_in_session",
    "issue_vpn_subscription_link",
    "proxy_subscription",
    "revoke_vpn_subscription_links",
    "routes",
    "vpn_proxy_is_configured",
]

"""Shared transport, validation, authentication, and response helpers."""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict, deque
from typing import Awaitable, Callable
from uuid import uuid4

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from bot.web.api.auth import ApiPrincipal, authenticate_api_key

MAX_JSON_BODY_BYTES = 64 * 1024
_READ_LIMIT_PER_MINUTE = 120
_WRITE_LIMIT_PER_MINUTE = 20
_AUTH_FAILURE_LIMIT_PER_MINUTE = 30
_MAX_AUTH_CLIENTS = 4096
_WINDOW_SECONDS = 60.0
_REQUESTS: dict[tuple[int, str], deque[float]] = {}
_AUTH_FAILURES: OrderedDict[str, deque[float]] = OrderedDict()
_logger = logging.getLogger(__name__)


class SecurityHeadersMiddleware:
    """Apply browser hardening headers to every API response, including 404s."""

    _HEADERS = (
        (b"cache-control", b"no-store"),
        (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        (b"referrer-policy", b"no-referrer"),
        (b"strict-transport-security", b"max-age=31536000"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"x-xss-protection", b"0"),
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in headers}
                for name, value in self._HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, secure_send)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(code)


def json_response(data: dict, status_code: int = 200, *, request_id: str | None = None) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if request_id:
        headers["X-Request-ID"] = request_id
    return JSONResponse(data, status_code=status_code, headers=headers)


def error_response(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
    if status_code == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id}},
        status_code=status_code,
        headers=headers,
    )


def _allow_request(principal: ApiPrincipal, *, write: bool) -> bool:
    now = time.monotonic()
    bucket = "write" if write else "read"
    limit = _WRITE_LIMIT_PER_MINUTE if write else _READ_LIMIT_PER_MINUTE
    key = (principal.api_key_id, bucket)
    requests = _REQUESTS.setdefault(key, deque())
    cutoff = now - _WINDOW_SECONDS
    while requests and requests[0] <= cutoff:
        requests.popleft()
    if len(requests) >= limit:
        return False
    requests.append(now)

    if len(_REQUESTS) > 10000:
        for bucket_key, entries in list(_REQUESTS.items()):
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if not entries:
                _REQUESTS.pop(bucket_key, None)
    return True


def _auth_client_key(request: Request) -> str:
    client = request.client
    return str(client.host) if client and client.host else "unknown"


def _auth_client_is_limited(client_key: str) -> bool:
    """Check failed-auth rate by reverse-proxy-validated client address."""
    now = time.monotonic()
    cutoff = now - _WINDOW_SECONDS
    failures = _AUTH_FAILURES.get(client_key)
    if failures is not None:
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            _AUTH_FAILURES.pop(client_key, None)
            return False
        _AUTH_FAILURES.move_to_end(client_key)
        return len(failures) >= _AUTH_FAILURE_LIMIT_PER_MINUTE
    if len(_AUTH_FAILURES) >= _MAX_AUTH_CLIENTS:
        for stale_key, stale_failures in list(_AUTH_FAILURES.items()):
            while stale_failures and stale_failures[0] <= cutoff:
                stale_failures.popleft()
            if not stale_failures:
                _AUTH_FAILURES.pop(stale_key, None)
        while len(_AUTH_FAILURES) >= _MAX_AUTH_CLIENTS:
            _AUTH_FAILURES.popitem(last=False)
    return False


def _record_auth_failure(client_key: str) -> None:
    now = time.monotonic()
    failures = _AUTH_FAILURES.setdefault(client_key, deque())
    failures.append(now)
    _AUTH_FAILURES.move_to_end(client_key)


def _authentication_error(
    request_id: str, client_key: str, code: str, message: str,
) -> JSONResponse:
    _record_auth_failure(client_key)
    return error_response(401, code, message, request_id)


Endpoint = Callable[[Request], Awaitable[Response]]


def api_route(
    path: str,
    endpoint: Endpoint,
    *,
    methods: list[str] | None = None,
    protected: bool = True,
    write: bool = False,
    name: str | None = None,
) -> Route:
    """Wrap one endpoint consistently; protected routes derive identity from a key."""

    async def dispatch(request: Request) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        if protected:
            auth_client_key = _auth_client_key(request)
            if _auth_client_is_limited(auth_client_key):
                response = error_response(
                    429, "auth_rate_limited",
                    "Too many failed authentication attempts; retry later.", request_id,
                )
                response.headers["Retry-After"] = "60"
                return response
            authorization = request.headers.get("authorization", "")
            scheme, separator, token = authorization.partition(" ")
            if not separator or scheme.casefold() != "bearer" or not token or " " in token:
                return _authentication_error(
                    request_id, auth_client_key, "authentication_required",
                    "A valid bearer API key is required.",
                )

            principal = await authenticate_api_key(token)
            if principal is None:
                return _authentication_error(
                    request_id, auth_client_key, "invalid_api_key",
                    "The API key is invalid or revoked.",
                )
            if principal.is_blocked:
                return error_response(403, "account_blocked", "This account cannot use the API.", request_id)
            if not _allow_request(principal, write=write):
                response = error_response(429, "rate_limited", "Too many requests; retry later.", request_id)
                response.headers["Retry-After"] = "60"
                return response

            request.state.api_principal = principal
            request.state.api_user_id = principal.user_id
            request.state.api_key_id = principal.api_key_id

        try:
            response = await endpoint(request)
        except ApiError as exc:
            return error_response(exc.status_code, exc.code, exc.message, request_id)
        except Exception as exc:
            # Details and payment/provider responses must never cross the API boundary.
            _logger.error(
                "Partner API endpoint failed request_id=%s endpoint=%s error_type=%s",
                request_id,
                name or getattr(endpoint, "__name__", "unknown"),
                type(exc).__name__,
            )
            return error_response(500, "internal_error", "The request could not be processed.", request_id)

        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Request-ID", request_id)
        return response

    dispatch.__name__ = name or getattr(endpoint, "__name__", "api_endpoint")
    return Route(path, dispatch, methods=methods or ["GET"], name=name)


async def read_json_object(request: Request) -> dict:
    """Read a bounded JSON object, including chunked requests without a length."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ApiError(415, "unsupported_media_type", "Send a JSON request body.")

    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            if int(raw_length) > MAX_JSON_BODY_BYTES:
                raise ApiError(413, "payload_too_large", "The JSON body is too large.")
        except ValueError as exc:
            raise ApiError(400, "invalid_content_length", "The content length is invalid.") from exc

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_JSON_BODY_BYTES:
            raise ApiError(413, "payload_too_large", "The JSON body is too large.")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ApiError(400, "invalid_json", "The request body must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_json_object", "The JSON body must be an object.")
    return value


def parse_pagination(request: Request, *, default_limit: int = 20, max_limit: int = 100) -> tuple[int, int]:
    try:
        limit = int(request.query_params.get("limit", default_limit))
        offset = int(request.query_params.get("offset", 0))
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "invalid_pagination", "limit and offset must be integers.") from exc
    if not 1 <= limit <= max_limit or not 0 <= offset <= 1_000_000:
        raise ApiError(400, "invalid_pagination", f"limit must be 1–{max_limit} and offset must be non-negative.")
    return limit, offset


def reject_unknown_fields(body: dict, allowed: set[str]) -> None:
    """Fail closed when a JSON body contains fields outside its contract."""
    if set(body) - allowed:
        raise ApiError(400, "unknown_fields", "The request contains unsupported fields.")

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _scope(path: str, *, method="GET", headers=None, client=("1.2.3.4", 1234)) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": headers or [],
        "client": client,
    }


async def _run_middleware(middleware, scope, messages):
    sent = []
    app_called = False

    async def app(_scope, receive, _send):
        nonlocal app_called
        app_called = True
        while True:
            message = await receive()
            if message.get("type") != "http.request" or not message.get("more_body"):
                break

    iterator = iter(messages)

    async def receive():
        try:
            return next(iterator)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware.app = app
    await middleware(scope, receive, send)
    return app_called, sent


@pytest.mark.asyncio
async def test_public_health_is_liveness_only():
    from bot.web.admin import health_check

    request = SimpleNamespace(session={})
    with patch("bot.web.admin.Database") as database:
        response = await health_check(request)

    assert response.status_code == 200
    assert json.loads(response.body) == {"status": "healthy"}
    database.assert_not_called()


@pytest.mark.asyncio
async def test_public_readiness_requires_auth_without_touching_database():
    from bot.web.admin import health_ready

    request = SimpleNamespace(session={})
    with patch("bot.web.admin.Database") as database:
        response = await health_ready(request)

    assert response.status_code == 401
    database.assert_not_called()


@pytest.mark.asyncio
async def test_readiness_probe_is_single_flight_and_cached():
    from bot.web import admin

    execute = AsyncMock()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def execute(self, query):
            await execute(query)

    class FakeDatabase:
        def session(self):
            return FakeSession()

    request = SimpleNamespace(session={"authenticated": True})
    with patch.object(admin, "_health_cache", None), \
            patch.object(admin, "Database", return_value=FakeDatabase()), \
            patch.object(admin, "get_cache_manager", return_value=None), \
            patch.object(admin, "get_metrics", return_value=None), \
            patch.object(admin, "_HEALTH_CACHE_TTL_SECONDS", 60):
        responses = await asyncio.gather(
            admin.health_ready(request),
            admin.health_ready(request),
            admin.health_ready(request),
        )

    assert all(response.status_code == 200 for response in responses)
    assert execute.await_count == 1


@pytest.mark.asyncio
async def test_health_rate_limit_blocks_burst_per_client():
    from bot.web.access import HealthRateLimitMiddleware

    calls = 0

    async def app(_scope, _receive, _send):
        nonlocal calls
        calls += 1

    middleware = HealthRateLimitMiddleware(app, max_requests=2, window_seconds=60)
    scope = _scope("/health")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    responses = []

    async def send(message):
        responses.append(message)

    for _ in range(3):
        await middleware(scope, receive, send)

    statuses = [message["status"] for message in responses if message["type"] == "http.response.start"]
    assert calls == 2
    assert statuses[-1] == 429


@pytest.mark.asyncio
async def test_login_body_limit_replays_small_body():
    from bot.web.access import AdminLoginBodyLimitMiddleware

    body = b"username=admin&password=secret"
    scope = _scope(
        "/admin/login",
        method="POST",
        headers=[(b"content-length", str(len(body)).encode())],
    )
    called, _sent = await _run_middleware(
        AdminLoginBodyLimitMiddleware(lambda *_args: None),
        scope,
        [{"type": "http.request", "body": body, "more_body": False}],
    )
    assert called is True


@pytest.mark.asyncio
async def test_login_body_limit_rejects_chunked_oversize_body():
    from bot.web.access import AdminLoginBodyLimitMiddleware, LOGIN_BODY_MAX_BYTES

    scope = _scope("/admin/login", method="POST")
    called, sent = await _run_middleware(
        AdminLoginBodyLimitMiddleware(lambda *_args: None),
        scope,
        [
            {"type": "http.request", "body": b"x" * LOGIN_BODY_MAX_BYTES, "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ],
    )
    assert called is False
    assert any(
        message.get("type") == "http.response.start" and message.get("status") == 413
        for message in sent
    )

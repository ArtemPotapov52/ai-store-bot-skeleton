from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from starlette.requests import Request

from bot.database.main import Database
from bot.database.models.main import Permission, Role, WebAdmin
from bot.web.access import (
    WebAccessMixin,
    hash_password,
    has_web_perm,
    resolve_web_perms,
    verify_password,
    web_session_active,
)


def _login_request(username: str, password: str, ip: str = "1.2.3.4") -> Request:
    body = f"username={username}&password={password}".encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/admin/login",
        "query_string": b"",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "client": (ip, 1234),
        "session": {},
    }
    return Request(scope, receive)


def _authed_request(session: dict) -> SimpleNamespace:
    return SimpleNamespace(session=dict(session), state=SimpleNamespace())


async def _role_perms(role_name: str) -> int:
    async with Database().session() as s:
        return int((await s.execute(
            select(Role.permissions).where(Role.name == role_name)
        )).scalar() or 0)


def _role_session(admin_id: int, perms: int) -> dict:
    return {"authenticated": True, "web_admin_id": admin_id, "web_perms": perms}


async def _make_web_admin(login: str, password: str, role_name: str) -> int:
    async with Database().session() as s:
        role_id = (await s.execute(
            select(Role.id).where(Role.name == role_name)
        )).scalar_one()
        s.add(WebAdmin(login=login, password_hash=hash_password(password),
                       role_id=role_id))
        await s.flush()
        row_id = (await s.execute(
            select(WebAdmin.id).where(WebAdmin.login == login)
        )).scalar_one()
    return row_id


class TestPasswordHashing:

    def test_roundtrip(self):
        hashed = hash_password("s3cret-pw")
        assert hashed != "s3cret-pw"
        assert verify_password("s3cret-pw", hashed) is True

    def test_wrong_password_rejected(self):
        assert verify_password("nope", hash_password("s3cret-pw")) is False

    def test_malformed_hash_rejected(self):
        assert verify_password("x", "garbage") is False
        assert verify_password("x", "") is False


class TestWebLogin:

    async def test_owner_env_login_still_works(self):
        from bot.web.admin import AdminAuth

        request = _login_request("boss", "boss-pass")
        with patch("bot.web.admin.EnvKeys") as env:
            env.ADMIN_USERNAME = "boss"
            env.ADMIN_PASSWORD = "boss-pass"
            assert await AdminAuth(secret_key="test-secret").login(request) is True
        assert request.session["authenticated"] is True
        assert request.session["web_owner"] is True
        assert request.session["web_perms"] & 32  # ADMINS_MANAGE bit present

    async def test_personal_login_grants_scoped_session(self):
        from bot.web.admin import AdminAuth

        await _make_web_admin("manager", "manager-pass", "ADMIN")
        request = _login_request("manager", "manager-pass")
        with patch("bot.web.admin.EnvKeys") as env:
            env.ADMIN_USERNAME = "boss"
            env.ADMIN_PASSWORD = "boss-pass"
            assert await AdminAuth(secret_key="test-secret").login(request) is True
        assert request.session["authenticated"] is True
        assert request.session.get("web_owner") is None
        assert isinstance(request.session["web_admin_id"], int)

    async def test_wrong_password_rejected(self):
        from bot.web.admin import AdminAuth

        await _make_web_admin("manager2", "manager-pass", "ADMIN")
        request = _login_request("manager2", "wrong-pass")
        with patch("bot.web.admin.EnvKeys") as env:
            env.ADMIN_USERNAME = "boss"
            env.ADMIN_PASSWORD = "boss-pass"
            assert await AdminAuth(secret_key="test-secret").login(request) is False
        assert request.session.get("authenticated") is None

    async def test_disabled_login_rejected(self):
        from bot.web.admin import AdminAuth

        await _make_web_admin("fired", "fired-pass", "ADMIN")
        async with Database().session() as s:
            row = (await s.execute(
                select(WebAdmin).where(WebAdmin.login == "fired")
            )).scalars().one()
            row.is_active = False
        request = _login_request("fired", "fired-pass")
        with patch("bot.web.admin.EnvKeys") as env:
            env.ADMIN_USERNAME = "boss"
            env.ADMIN_PASSWORD = "boss-pass"
            assert await AdminAuth(secret_key="test-secret").login(request) is False

    async def test_role_without_admin_perms_rejected(self):
        from bot.web.admin import AdminAuth

        async with Database().session() as s:
            s.add(Role(name="PLAIN", permissions=Permission.USE))
        await _make_web_admin("plain", "plain-pass", "PLAIN")
        request = _login_request("plain", "plain-pass")
        with patch("bot.web.admin.EnvKeys") as env:
            env.ADMIN_USERNAME = "boss"
            env.ADMIN_PASSWORD = "boss-pass"
            assert await AdminAuth(secret_key="test-secret").login(request) is False


class TestAdminSessionLifecycle:

    async def test_active_owner_session_is_refreshed_without_changing_auth(self):
        from bot.web.admin import (
            ADMIN_SESSION_MAX_AGE_SECONDS,
            ADMIN_SESSION_REFRESH_INTERVAL_SECONDS,
            AdminAuth,
        )

        assert ADMIN_SESSION_MAX_AGE_SECONDS == 30 * 24 * 60 * 60
        request = _authed_request({"authenticated": True, "web_owner": True})
        auth = AdminAuth(secret_key="test-secret")

        with patch("bot.web.admin.time.time", return_value=1_000_000):
            assert await auth.authenticate(request) is True
        first_touch = request.session["_admin_session_touch"]

        # Regular page loads within the refresh interval do not rewrite the
        # cookie on every request.
        with patch("bot.web.admin.time.time", return_value=1_000_001):
            assert await auth.authenticate(request) is True
        assert request.session["_admin_session_touch"] == first_touch

        with patch(
            "bot.web.admin.time.time",
            return_value=1_000_000 + ADMIN_SESSION_REFRESH_INTERVAL_SECONDS,
        ):
            assert await auth.authenticate(request) is True
        assert request.session["_admin_session_touch"] == (
            1_000_000 + ADMIN_SESSION_REFRESH_INTERVAL_SECONDS
        )

    async def test_expired_or_missing_session_is_not_refreshed(self):
        from bot.web.admin import AdminAuth

        request = _authed_request({})
        assert await AdminAuth(secret_key="test-secret").authenticate(request) is False
        assert "_admin_session_touch" not in request.session


class TestViewGating:

    async def test_payment_deletion_requires_balance_management(self):
        from bot.web.admin import PaymentsAdmin

        request = _authed_request(_role_session(42, Permission.STATS_VIEW))
        view = PaymentsAdmin()
        assert view.can_delete is True

        with patch("bot.web.admin.has_web_perm", new=AsyncMock(return_value=False)):
            assert await view.check_can_delete(request, SimpleNamespace()) is False

        with patch("bot.web.admin.has_web_perm", new=AsyncMock(return_value=True)):
            assert await view.check_can_delete(
                request, SimpleNamespace(provider="test", status="succeeded")
            ) is True

    async def test_payment_deletion_is_audited(self):
        from bot.database.models.main import Payments
        from bot.web.admin import PaymentsAdmin

        request = SimpleNamespace(
            session={"authenticated": True, "web_owner": True},
            state=SimpleNamespace(),
            client=None,
        )
        payment = Payments(
            id=42,
            provider="test-provider",
            external_id="test-delete-payment",
            user_id=None,
            amount=Decimal("55.00"),
            currency="RUB",
            status="failed",
        )

        with patch("bot.web.admin.log_audit", new=AsyncMock()) as audit:
            await PaymentsAdmin().after_model_delete(payment, request)

        audit.assert_awaited_once()
        assert audit.await_args.args[0] == "sqladmin_delete"
        assert audit.await_args.kwargs["resource_type"] == "Платёж"
        assert audit.await_args.kwargs["resource_id"] == "42"

    @pytest.mark.parametrize(("provider", "status", "allowed"), [
        ("test", "succeeded", True),
        ("cryptopay", "failed", True),
        ("cryptopay", "pending", False),
        ("cryptopay", "succeeded", False),
    ])
    async def test_payment_deletion_protects_live_real_payments(
        self, provider, status, allowed
    ):
        from bot.database.models.main import Payments
        from bot.web.admin import PaymentsAdmin

        request = _authed_request({"authenticated": True, "web_owner": True})
        payment = Payments(provider=provider, status=status)
        view = PaymentsAdmin()

        with patch("bot.web.admin.has_web_perm", new=AsyncMock(return_value=True)):
            assert await view.check_can_delete(request, payment) is allowed

    async def test_owner_sees_everything(self):
        from bot.web.admin import GoodsAdmin, RoleAdmin

        request = _authed_request({"authenticated": True, "web_owner": True})
        assert GoodsAdmin().is_accessible(request) is True
        assert RoleAdmin().is_accessible(request) is True

    async def test_admin_role_cannot_open_roles(self):
        from bot.web.admin import GoodsAdmin, PromoCodeAdmin, RoleAdmin, UserAdmin

        await _make_web_admin("rater", "pw", "ADMIN")
        async with Database().session() as s:
            admin_id = (await s.execute(
                select(WebAdmin.id).where(WebAdmin.login == "rater")
            )).scalar_one()
        perms = await _role_perms("ADMIN")
        request = _authed_request(_role_session(admin_id, perms))
        assert GoodsAdmin().is_accessible(request) is True
        assert UserAdmin().is_accessible(request) is True
        assert PromoCodeAdmin().is_accessible(request) is True
        assert RoleAdmin().is_accessible(request) is False
        assert RoleAdmin().is_visible(request) is False

    async def test_catalog_only_role(self):
        from bot.web.admin import GoodsAdmin, RoleAdmin, UserAdmin

        async with Database().session() as s:
            s.add(Role(name="STOCK", permissions=Permission.USE | Permission.CATALOG_MANAGE))
        await _make_web_admin("stocker", "pw", "STOCK")
        async with Database().session() as s:
            admin_id = (await s.execute(
                select(WebAdmin.id).where(WebAdmin.login == "stocker")
            )).scalar_one()
        perms = await _role_perms("STOCK")
        request = _authed_request(_role_session(admin_id, perms))
        assert GoodsAdmin().is_accessible(request) is True
        assert UserAdmin().is_accessible(request) is False
        assert RoleAdmin().is_accessible(request) is False

    async def test_unauthenticated_denied(self):
        from bot.web.admin import GoodsAdmin

        assert GoodsAdmin().is_accessible(_authed_request({})) is False
        assert await has_web_perm(_authed_request({}), Permission.CATALOG_MANAGE) is False

    async def test_authenticate_drops_dead_sessions(self):
        from bot.web.admin import AdminAuth

        await _make_web_admin("ghost", "pw", "ADMIN")
        async with Database().session() as s:
            admin_id = (await s.execute(
                select(WebAdmin.id).where(WebAdmin.login == "ghost")
            )).scalar_one()
        request = _authed_request({"authenticated": True, "web_admin_id": admin_id})
        assert await AdminAuth(secret_key="test-secret").authenticate(request) is True
        assert await web_session_active(request) is True

        async with Database().session() as s:
            row = (await s.execute(
                select(WebAdmin).where(WebAdmin.id == admin_id)
            )).scalars().one()
            row.is_active = False
        assert await AdminAuth(secret_key="test-secret").authenticate(request) is False

    async def test_resolve_perms_cached_on_request(self):
        request = _authed_request({"authenticated": True, "web_owner": True})
        first = await resolve_web_perms(request)
        assert first is not None and first & Permission.ADMINS_MANAGE
        assert request.state.web_perms == first


class TestAdminDateFormatting:

    def test_registration_date_is_rendered_in_moscow_time(self):
        from bot.web.admin import UserAdmin

        view = UserAdmin()
        formatter = view.column_formatters["registration_date"]
        model = SimpleNamespace(
            registration_date=datetime(2026, 1, 15, 21, 30, tzinfo=timezone.utc)
        )

        assert formatter(model, "registration_date") == "16.01.2026 00:30"


    def test_naive_registration_date_is_treated_as_utc(self):
        from bot.web.admin import UserAdmin

        formatter = UserAdmin().column_formatters["registration_date"]
        model = SimpleNamespace(registration_date=datetime(2026, 1, 15, 21, 30))

        assert formatter(model, "registration_date") == "16.01.2026 00:30"

    def test_iso_registration_date_has_no_seconds_or_timezone_suffix(self):
        from bot.web.admin import UserAdmin

        formatter = UserAdmin().column_formatters["registration_date"]
        model = SimpleNamespace(
            registration_date="2026-01-15T21:30:42.123456+00:00"
        )

        formatted = formatter(model, "registration_date")
        assert formatted == "16.01.2026 00:30"
        assert ":" in formatted and formatted.count(":") == 1
        assert "+00:00" not in formatted


class TestUserSearch:

    def test_parse_telegram_id_is_strict_and_bounded(self):
        from bot.web.admin import _parse_telegram_id

        assert _parse_telegram_id("123456789") == 123456789
        assert _parse_telegram_id(" 123456789 ") == 123456789
        assert _parse_telegram_id("@123456789") is None
        assert _parse_telegram_id("-123") is None
        assert _parse_telegram_id("x" * 33) is None

    async def test_view_is_limited_to_user_management(self):
        from bot.web.admin import UserSearchView

        assert UserSearchView().is_accessible(_authed_request({
            "authenticated": True,
            "web_perms": Permission.USERS_MANAGE,
        })) is True
        assert UserSearchView().is_accessible(_authed_request({
            "authenticated": True,
            "web_perms": Permission.CATALOG_MANAGE,
        })) is False


class TestBulkStockTemplate:

    def test_mobile_submit_is_not_silently_blocked_by_browser_validation(self):
        template = Path("bot/web/templates/stock_item_bulk.html").read_text(encoding="utf-8")

        assert '<form method="POST" class="mt-3" novalidate>' in template
        assert '<button class="btn btn-primary w-100" type="submit">' in template


class TestAdminMutationGuards:

    @staticmethod
    def _request():
        return _authed_request({"authenticated": True, "web_perms": Permission.USERS_MANAGE})

    async def test_users_manager_cannot_create_admin_telegram_user(self):
        from bot.database.models.main import User
        from bot.web.admin import UserAdmin

        request = self._request()
        with patch("bot.web.admin.resolve_web_perms", new=AsyncMock(return_value=Permission.USERS_MANAGE)), \
                patch("bot.web.admin.role_permissions", new=AsyncMock(return_value=Permission.USE | Permission.USERS_MANAGE)):
            with pytest.raises(ValueError, match="роль"):
                await UserAdmin().on_model_change(
                    {"telegram_id": 777001, "role_id": 2},
                    User(telegram_id=777001),
                    True,
                    request,
                )

    async def test_users_manager_can_create_plain_user(self):
        from bot.database.models.main import User
        from bot.web.admin import UserAdmin

        request = self._request()
        with patch("bot.web.admin.resolve_web_perms", new=AsyncMock(return_value=Permission.USERS_MANAGE)), \
                patch("bot.web.admin.role_permissions", new=AsyncMock(return_value=Permission.USE)):
            await UserAdmin().on_model_change(
                {"telegram_id": 777002, "role_id": 1},
                User(telegram_id=777002),
                True,
                request,
            )

    async def test_users_manager_cannot_use_custom_user_like_role_on_create(self):
        from bot.database.models.main import User
        from bot.database.methods.create import create_role
        from bot.web.admin import UserAdmin

        request = self._request()
        custom_role_id = await create_role("CUSTOM_USER_LIKE", Permission.USE)
        with patch("bot.web.admin.resolve_web_perms", new=AsyncMock(return_value=Permission.USERS_MANAGE)), \
                patch("bot.web.admin.role_permissions", new=AsyncMock(return_value=Permission.USE)):
            # The role lookup must not be bypassed by a custom role carrying
            # the same single USE bit as the built-in USER role.
            with pytest.raises(ValueError, match="роль"):
                await UserAdmin().on_model_change(
                    {"telegram_id": 777003, "role_id": custom_role_id},
                    User(telegram_id=777003),
                    True,
                    request,
                )

    async def test_role_admin_rejects_own_and_unknown_bits_for_non_owner(self):
        from bot.database.models.main import Role
        from bot.web.admin import RoleAdmin

        request = _authed_request({"authenticated": True, "web_perms": Permission.ADMINS_MANAGE})
        with patch("bot.web.admin.resolve_web_perms", new=AsyncMock(return_value=Permission.ADMINS_MANAGE)):
            with pytest.raises(ValueError, match="подмножество|владелец"):
                await RoleAdmin().on_model_change(
                    {"name": "ESCALATED", "permissions": Permission.ADMINS_MANAGE | Permission.OWN},
                    Role(name="ESCALATED", permissions=Permission.ADMINS_MANAGE | Permission.OWN),
                    True,
                    request,
                )
            with pytest.raises(ValueError, match="неизвестн|0.*1023"):
                await RoleAdmin().on_model_change(
                    {"name": "UNKNOWN", "permissions": 1024},
                    Role(name="UNKNOWN", permissions=1024),
                    True,
                    request,
                )

    async def test_system_role_cannot_be_modified_even_by_role_manager(self):
        from bot.database.models.main import Role
        from bot.web.admin import RoleAdmin

        request = _authed_request({"authenticated": True, "web_perms": Permission.ADMINS_MANAGE})
        role = Role(id=1, name="ADMIN", permissions=Permission.ADMINS_MANAGE, default=False)
        with patch("bot.web.admin.resolve_web_perms", new=AsyncMock(return_value=Permission.ADMINS_MANAGE)):
            with pytest.raises(ValueError, match="(?i)системн"):
                await RoleAdmin().on_model_change(
                    {"name": "ADMIN", "permissions": Permission.USE},
                    role,
                    False,
                    request,
                )

    async def test_goods_admin_rejects_non_positive_or_non_finite_price(self):
        from bot.database.models.main import Goods
        from bot.web.admin import GoodsAdmin

        request = _authed_request({"authenticated": True, "web_perms": Permission.CATALOG_MANAGE})
        for value in ("0", "-1", "NaN", "Infinity"):
            with pytest.raises(ValueError, match="(?i)цен"):
                await GoodsAdmin().on_model_change(
                    {"price": value, "sale_percent": None},
                    Goods(name="bad"),
                    True,
                    request,
                )

    async def test_goods_admin_rejects_sale_outside_range(self):
        from bot.database.models.main import Goods
        from bot.web.admin import GoodsAdmin

        request = _authed_request({"authenticated": True, "web_perms": Permission.CATALOG_MANAGE})
        for value in ("-1", "101", "NaN"):
            with pytest.raises(ValueError, match="(?i)скидк"):
                await GoodsAdmin().on_model_change(
                    {"price": "100", "sale_percent": value},
                    Goods(name="bad"),
                    True,
                    request,
                )


class TestMixinDefaults:

    def test_view_without_perm_needs_login_only(self):
        class OpenView(WebAccessMixin):
            pass

        assert OpenView().is_accessible(_authed_request({"authenticated": True})) is True
        assert OpenView().is_accessible(_authed_request({})) is False

    def test_sync_contract_no_coroutine(self):
        import inspect

        assert not inspect.iscoroutinefunction(WebAccessMixin.is_accessible)
        assert not inspect.iscoroutinefunction(WebAccessMixin.is_visible)


class TestRBACMiddleware:
    """HTTP-level: the middleware (not the menu) is the real lock."""

    def _scope(self, path, session):
        return {
            "type": "http", "method": "GET", "path": path,
            "query_string": b"", "headers": [],
            "client": ("9.9.9.9", 1), "session": dict(session),
        }

    async def _run(self, path, session, perm_map):
        from bot.web.access import WebRBACMiddleware

        seen = {}

        async def app(scope, receive, send):
            seen["passed"] = True

        mw = WebRBACMiddleware(app, perm_map)
        sent = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        await mw(self._scope(path, session), receive, send)
        status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
        return seen.get("passed", False), status

    async def test_low_priv_cannot_open_roles(self):
        from bot.database.models.main import Permission as P

        await _make_web_admin("mwuser", "pw", "ADMIN")
        async with Database().session() as s:
            admin_id = (await s.execute(
                select(WebAdmin.id).where(WebAdmin.login == "mwuser")
            )).scalar_one()
        session = {"authenticated": True, "web_admin_id": admin_id}
        perm_map = {"role": P.ADMINS_MANAGE, "goods": P.CATALOG_MANAGE}

        passed, _ = await self._run("/admin/goods/list", session, perm_map)
        assert passed is True
        passed, status = await self._run("/admin/role/list", session, perm_map)
        assert passed is False
        assert status == 403

    async def test_owner_passes_unknown_paths(self):
        passed, _ = await self._run(
            "/admin/whatever/list",
            {"authenticated": True, "web_owner": True}, {},
        )
        assert passed is True

    async def test_unauthenticated_passes_through_to_login_redirect(self):
        passed, status = await self._run("/admin/role/list", {}, {"role": 32})
        assert passed is True
        assert status is None

    async def test_login_paths_skipped(self):
        passed, _ = await self._run(
            "/admin/login",
            {"authenticated": True, "web_admin_id": 999999}, {},
        )
        assert passed is True

    async def test_platega_callback_is_publicly_reachable_for_provider_auth(self):
        passed, _ = await self._run("/payments/platega/callback", {}, {})
        assert passed is True

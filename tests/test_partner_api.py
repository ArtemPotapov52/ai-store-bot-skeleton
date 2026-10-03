"""ASGI-level regression tests for the isolated partner API."""

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import select

from bot.web.api import create_partner_api_app
from bot.web.api.auth import issue_partner_api_key
from bot.database import Database
from bot.database.models import BoughtGoods, Goods, Payments, PromoCodes, User


@pytest.fixture
async def api_client(mock_bot):
    from bot.web.api.common import _AUTH_FAILURES, _REQUESTS

    # SQLite reuses IDs after per-test cleanup, while the production limiter is
    # intentionally process-local and keyed by the persistent API-key row ID.
    _REQUESTS.clear()
    _AUTH_FAILURES.clear()
    app = create_partner_api_app(bot=mock_bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="https://api.example.test",
    ) as client:
        yield client


@pytest.fixture
async def partner_key(user_factory):
    await user_factory(telegram_id=661001, balance=250)
    return await issue_partner_api_key(661001)


@pytest.mark.parametrize("path", ["/health", "/docs", "/openapi.json"])
async def test_public_api_discovery_routes_are_available(api_client, path):
    response = await api_client.get(path)

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert "default-src 'none'" in response.headers["content-security-policy"]


async def test_security_headers_are_applied_to_not_found_responses(api_client):
    response = await api_client.get("/missing-route")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["request_id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_method_errors_use_the_documented_json_error_shape(api_client):
    response = await api_client.put("/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"
    assert response.headers["x-request-id"] == response.json()["error"]["request_id"]


async def test_openapi_documents_every_registered_customer_route(api_client):
    response = await api_client.get("/openapi.json")
    paths = response.json()["paths"]
    required = {
        "/v1/me", "/v1/me/balance", "/v1/me/operations", "/v1/me/purchases",
        "/v1/me/purchases/{purchase_id}", "/v1/categories", "/v1/products",
        "/v1/products/{product_id}", "/v1/cart", "/v1/cart/items/{product_id}",
        "/v1/cart/checkout", "/v1/orders/quote", "/v1/orders",
        "/v1/products/{product_id}/reviews", "/v1/products/{product_id}/stock-alert",
        "/v1/referrals", "/v1/referrals/earnings", "/v1/info",
        "/v1/balance/payment-methods", "/v1/balance/promos/redeem",
        "/v1/balance/top-ups", "/v1/balance/payments", "/v1/balance/payments/{payment_id}",
    }

    assert response.status_code == 200
    assert required.issubset(paths)
    assert "get" in paths["/v1/balance/payments"]
    assert {"get", "post"}.issubset(paths["/v1/products/{product_id}/reviews"])
    assert {"post", "delete"}.issubset(paths["/v1/products/{product_id}/stock-alert"])


@pytest.mark.parametrize(
    "path",
    [
        "/admin",
        "/admin/goods/list",
        "/metrics",
        "/export/users",
        "/payments/platega/callback",
    ],
)
async def test_admin_and_payment_callback_routes_are_not_mounted(api_client, path):
    response = await api_client.get(path)

    assert response.status_code == 404


async def test_me_requires_a_valid_bearer_key(api_client):
    missing = await api_client.get("/v1/me")
    invalid = await api_client.get(
        "/v1/me",
        headers={"Authorization": "Bearer not-a-real-key"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"


async def test_failed_authentication_is_rate_limited_per_client_address(
    api_client, partner_key, monkeypatch,
):
    from bot.web.api import common
    from httpx2 import ASGITransport, AsyncClient

    monkeypatch.setattr(common, "_AUTH_FAILURE_LIMIT_PER_MINUTE", 1)
    first = await api_client.get("/v1/me")
    limited = await api_client.get("/v1/me")
    other_transport = ASGITransport(
        app=api_client._transport.app,
        client=("198.51.100.7", 54321),
    )
    async with AsyncClient(
        transport=other_transport, base_url="https://api.example.test",
    ) as other_client:
        valid = await other_client.get(
            "/v1/me", headers={"Authorization": f"Bearer {partner_key}"},
        )

    assert first.status_code == 401
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert valid.status_code == 200


async def test_query_parameters_cannot_select_another_account(api_client):
    response = await api_client.get("/v1/me?telegram_id=987654321")

    assert response.status_code == 401


async def test_me_uses_the_account_bound_to_the_api_key(api_client, partner_key):
    response = await api_client.get(
        "/v1/me?telegram_id=123456",
        headers={"Authorization": f"Bearer {partner_key}"},
    )

    assert response.status_code == 200
    assert response.json()["telegram_id"] == 661001
    assert response.json()["balance"] == "250.00"


async def test_catalog_shows_sellable_count_but_never_stock_values(
    api_client, partner_key, item_factory,
):
    await item_factory(
        name="Secret stock item",
        price=17,
        values=[("DO-NOT-EXPOSE-THIS-CODE", False)],
    )
    headers = {"Authorization": f"Bearer {partner_key}"}

    categories = await api_client.get("/v1/categories", headers=headers)
    products = await api_client.get("/v1/products?search=Secret", headers=headers)
    product_id = products.json()["data"][0]["id"]
    detail = await api_client.get(f"/v1/products/{product_id}", headers=headers)

    assert categories.status_code == 200
    assert products.status_code == 200
    assert detail.status_code == 200
    assert products.json()["data"][0]["available_quantity"] == 1
    assert "DO-NOT-EXPOSE-THIS-CODE" not in products.text
    assert "DO-NOT-EXPOSE-THIS-CODE" not in detail.text


async def test_catalog_pagination_and_invalid_filters_are_bounded(
    api_client, partner_key, item_factory,
):
    await item_factory(name="Page product 1", price=1)
    await item_factory(name="Page product 2", price=2)
    headers = {"Authorization": f"Bearer {partner_key}"}

    page = await api_client.get("/v1/products?limit=1&offset=1", headers=headers)
    invalid = await api_client.get("/v1/products?limit=1000", headers=headers)

    assert page.status_code == 200
    assert len(page.json()["data"]) == 1
    assert page.json()["pagination"]["offset"] == 1
    assert invalid.status_code == 400


async def test_unknown_product_is_not_found(api_client, partner_key):
    response = await api_client.get(
        "/v1/products/999999",
        headers={"Authorization": f"Bearer {partner_key}"},
    )

    assert response.status_code == 404


async def test_account_balance_and_operations_are_key_scoped(api_client, partner_key, operation_factory):
    await operation_factory(661001, 25)
    response = await api_client.get(
        "/v1/me/operations?limit=5",
        headers={"Authorization": f"Bearer {partner_key}"},
    )
    balance = await api_client.get(
        "/v1/me/balance?telegram_id=999999",
        headers={"Authorization": f"Bearer {partner_key}"},
    )

    assert response.status_code == 200
    assert response.json()["data"][0]["amount"] == "25.00"
    assert balance.json()["balance"] == "250.00"


async def test_referral_and_info_endpoints_are_account_safe(api_client, partner_key):
    headers = {"Authorization": f"Bearer {partner_key}"}
    referrals = await api_client.get("/v1/referrals", headers=headers)
    earnings = await api_client.get("/v1/referrals/earnings", headers=headers)
    info = await api_client.get("/v1/info", headers=headers)

    assert referrals.status_code == earnings.status_code == info.status_code == 200
    assert referrals.json()["referral_count"] == 0
    assert earnings.json()["data"] == []
    assert "currency" in info.json()


async def test_order_quote_is_server_priced_and_retry_does_not_double_deliver(
    api_client, partner_key, item_factory,
):
    await item_factory(name="Idempotent product", price=17, values=[("only-one-secret", False)])
    async with Database().session() as session:
        product_id = await session.scalar(select(Goods.id).where(Goods.name == "Idempotent product"))
    headers = {
        "Authorization": f"Bearer {partner_key}",
        "Idempotency-Key": "order-test-1",
    }
    request_body = {"product_id": product_id, "quantity": 1}
    quote = await api_client.post("/v1/orders/quote", json=request_body, headers=headers)
    request_body["expected_total"] = quote.json()["data"]["total"]

    first = await api_client.post("/v1/orders", json=request_body, headers=headers)
    async with Database().session() as session:
        product = (await session.execute(
            select(Goods).where(Goods.id == product_id)
        )).scalar_one()
        product.is_active = False
    replay = await api_client.post("/v1/orders", json=request_body, headers=headers)
    reused_key = await api_client.post(
        "/v1/orders", json={**request_body, "quantity": 2}, headers=headers,
    )
    balance = await api_client.get("/v1/me/balance", headers=headers)

    assert quote.status_code == 200
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["data"]["items"][0]["delivery"] == "only-one-secret"
    assert reused_key.status_code == 409
    assert balance.json()["balance"] == "233.00"
    async with Database().session() as session:
        purchases = (await session.execute(
            select(BoughtGoods).where(BoughtGoods.buyer_id == 661001)
        )).scalars().all()
    assert len(purchases) == 1


async def test_order_rejects_stale_client_quote_without_charging(
    api_client, partner_key, item_factory,
):
    await item_factory(name="Changing product", price=10, values=[("secret", False)])
    async with Database().session() as session:
        product = (await session.execute(select(Goods).where(Goods.name == "Changing product"))).scalar_one()
        product_id = int(product.id)
    headers = {"Authorization": f"Bearer {partner_key}"}
    body = {"product_id": product_id, "quantity": 1}
    quote = await api_client.post("/v1/orders/quote", json=body, headers=headers)
    body["expected_total"] = quote.json()["data"]["total"]
    async with Database().session() as session:
        product = (await session.execute(select(Goods).where(Goods.id == product_id))).scalar_one()
        product.price = 11

    result = await api_client.post(
        "/v1/orders", json=body,
        headers={**headers, "Idempotency-Key": "stale-quote-test"},
    )
    balance = await api_client.get("/v1/me/balance", headers=headers)

    assert result.status_code == 409
    assert result.json()["error"]["code"] == "price_changed"
    assert balance.json()["balance"] == "250.00"


async def test_purchase_history_and_receipt_are_owner_scoped(api_client, partner_key, user_factory):
    await user_factory(telegram_id=661002, balance=1)
    async with Database().session() as session:
        session.add(BoughtGoods(
            item_name="Receipt product", value="owner-only-delivery", price=8,
            buyer_id=661001, unique_id=987654322,
        ))
        session.add(BoughtGoods(
            item_name="Other receipt", value="not-for-you", price=9,
            buyer_id=661002, unique_id=987654323,
        ))
    headers = {"Authorization": f"Bearer {partner_key}"}
    history = await api_client.get("/v1/me/purchases", headers=headers)
    own_id = history.json()["data"][0]["id"]
    own = await api_client.get(f"/v1/me/purchases/{own_id}", headers=headers)
    other = await api_client.get(f"/v1/me/purchases/{own_id + 1}", headers=headers)

    assert history.status_code == own.status_code == 200
    assert "value" not in history.json()["data"][0]
    assert own.json()["data"]["delivery"] == "owner-only-delivery"
    assert other.status_code == 404
    assert "not-for-you" not in history.text + own.text + other.text


async def test_cart_uses_exact_quantity_and_checkout_is_idempotent(api_client, partner_key, item_factory):
    await item_factory(name="Cart product", price=12, values=[("cart-secret", False)])
    async with Database().session() as session:
        product_id = await session.scalar(select(Goods.id).where(Goods.name == "Cart product"))
    headers = {"Authorization": f"Bearer {partner_key}"}
    set_line = await api_client.put(
        f"/v1/cart/items/{product_id}", json={"quantity": 1}, headers=headers,
    )
    cart = await api_client.get("/v1/cart", headers=headers)
    body = {"expected_total": cart.json()["total"]}
    checkout_headers = {**headers, "Idempotency-Key": "cart-checkout-1"}
    first = await api_client.post("/v1/cart/checkout", json=body, headers=checkout_headers)
    replay = await api_client.post("/v1/cart/checkout", json=body, headers=checkout_headers)

    assert set_line.status_code == 200
    assert cart.json()["total"] == "12.00"
    assert first.status_code == 200
    assert first.json()["data"]["items"][0]["delivery"] == "cart-secret"
    assert replay.json() == first.json()
    assert (await api_client.get("/v1/cart", headers=headers)).json()["data"] == []


async def test_review_requires_purchase_and_is_unique(api_client, partner_key, item_factory):
    await item_factory(name="Review product", price=5, values=[("delivered", False)])
    async with Database().session() as session:
        product = (await session.execute(select(Goods).where(Goods.name == "Review product"))).scalar_one()
        product_id = int(product.id)
    headers = {"Authorization": f"Bearer {partner_key}"}
    payload = {"rating": 5, "text": "Works"}
    denied = await api_client.post(f"/v1/products/{product_id}/reviews", json=payload, headers=headers)
    async with Database().session() as session:
        session.add(BoughtGoods(
            item_name="Review product", value="delivered", price=5,
            buyer_id=661001, unique_id=987654321,
        ))
    first = await api_client.post(f"/v1/products/{product_id}/reviews", json=payload, headers=headers)
    duplicate = await api_client.post(f"/v1/products/{product_id}/reviews", json=payload, headers=headers)
    listing = await api_client.get(f"/v1/products/{product_id}/reviews", headers=headers)

    assert denied.status_code == 403
    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert listing.json()["average_rating"] == 5.0


async def test_topup_uses_configured_provider_and_idempotency_without_real_payment(
    api_client, partner_key, monkeypatch,
):
    from unittest.mock import AsyncMock, patch
    from bot.misc.services import CryptoPayAPI

    monkeypatch.setattr("bot.misc.env.EnvKeys.CRYPTO_PAY_TOKEN", "isolated-test-token")
    create_invoice = AsyncMock(return_value={
        "invoice_id": 789012,
        "mini_app_invoice_url": "https://t.me/CryptoBot?start=invoice-test",
    })
    headers = {
        "Authorization": f"Bearer {partner_key}",
        "Idempotency-Key": "topup-isolated-1",
    }
    with patch.object(CryptoPayAPI, "create_invoice", create_invoice):
        first = await api_client.post(
            "/v1/balance/top-ups", json={"provider": "cryptopay", "amount": 25}, headers=headers,
        )
        replay = await api_client.post(
            "/v1/balance/top-ups", json={"provider": "cryptopay", "amount": 25}, headers=headers,
        )

    assert first.status_code == 201
    assert first.json()["data"]["checkout_url"].startswith("https://t.me/")
    assert replay.json() == first.json()
    assert create_invoice.await_count == 1
    async with Database().session() as session:
        payment = (await session.execute(select(Payments))).scalar_one()
    assert payment.status == "pending"
    assert payment.amount == 25


async def test_topup_rejects_fields_outside_the_contract(api_client, partner_key):
    response = await api_client.post(
        "/v1/balance/top-ups",
        json={"provider": "platega", "amount": 100, "telegram_id": 999999},
        headers={
            "Authorization": f"Bearer {partner_key}",
            "Idempotency-Key": "unknown-topup-field",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_fields"


@pytest.mark.parametrize(
    ("provider", "target", "response", "monkeypatches"),
    [
        (
            "platega", "bot.web.api.payments.PlategaAPI.create_sbp_transaction",
            {"transactionId": "ec0273dd-fac5-46a8-81a7-a2852dd3209f", "redirect": "https://pay.platega.io/pay/isolated"},
            {"PLATEGA_MERCHANT_ID": "test-merchant", "PLATEGA_API_KEY": "test-key"},
        ),
        (
            "xrocket", "bot.web.api.payments.XRocketPayAPI.create_invoice",
            {"id": "xrocket-isolated-123", "links": {"webLink": "https://xrocket.exchange/invoice/isolated"}},
            {"XROCKET_PAY_TOKEN": "test-xrocket"},
        ),
    ],
)
async def test_hosted_provider_topups_use_mocked_provider_only(
    api_client, partner_key, monkeypatch, provider, target, response, monkeypatches,
):
    from unittest.mock import AsyncMock, patch

    for name, value in monkeypatches.items():
        monkeypatch.setattr(f"bot.misc.env.EnvKeys.{name}", value)
    create_invoice = AsyncMock(return_value=response)
    headers = {
        "Authorization": f"Bearer {partner_key}",
        "Idempotency-Key": f"{provider}-topup-test",
    }
    with patch(target, create_invoice):
        result = await api_client.post(
            "/v1/balance/top-ups", json={"provider": provider, "amount": 25}, headers=headers,
        )
        replay = await api_client.post(
            "/v1/balance/top-ups", json={"provider": provider, "amount": 25}, headers=headers,
        )

    assert result.status_code == 201
    assert result.json()["data"]["checkout_url"].startswith("https://")
    assert replay.json() == result.json()
    assert create_invoice.await_count == 1


@pytest.mark.parametrize(
    ("provider", "method_name"),
    [("stars", "send_stars_invoice"), ("telegram", "send_fiat_invoice")],
)
async def test_telegram_invoice_topups_send_once_to_bound_account(
    api_client, partner_key, provider, method_name,
):
    from unittest.mock import AsyncMock, patch
    import bot.web.api.payments as api_payments

    send_invoice = AsyncMock()
    headers = {
        "Authorization": f"Bearer {partner_key}",
        "Idempotency-Key": f"{provider}-invoice-once",
    }
    with patch.object(api_payments, method_name, send_invoice):
        response = await api_client.post(
            "/v1/balance/top-ups", json={"provider": provider, "amount": 30}, headers=headers,
        )
        replay = await api_client.post(
            "/v1/balance/top-ups", json={"provider": provider, "amount": 30}, headers=headers,
        )

    assert response.status_code == 201
    assert response.json()["data"]["sent_to_telegram"] is True
    assert response.json()["data"]["checkout_url"] is None
    assert replay.json() == response.json()
    send_invoice.assert_awaited_once()
    assert send_invoice.await_args.kwargs["chat_id"] == 661001


async def test_payment_status_verifies_provider_and_credits_only_owner_account(
    api_client, partner_key,
):
    from unittest.mock import AsyncMock, patch
    from bot.database.methods.create import create_pending_payment, bind_pending_payment
    from bot.misc.services import CryptoPayAPI

    await create_pending_payment("cryptopay", "intent:status-test", 661001, 40, "RUB")
    await bind_pending_payment("cryptopay", "intent:status-test", "456789")
    async with Database().session() as session:
        payment_id = await session.scalar(select(Payments.id).where(Payments.external_id == "456789"))
    headers = {"Authorization": f"Bearer {partner_key}"}

    with patch.object(CryptoPayAPI, "get_invoice", AsyncMock(return_value={
        "status": "paid", "amount": "40.00", "fiat": "RUB",
    })):
        status = await api_client.get(f"/v1/balance/payments/{payment_id}", headers=headers)
        replay = await api_client.get(f"/v1/balance/payments/{payment_id}", headers=headers)
    history = await api_client.get("/v1/balance/payments", headers=headers)
    balance = await api_client.get("/v1/me/balance", headers=headers)

    assert status.status_code == 200
    assert status.json()["data"]["status"] == "succeeded"
    assert status.json()["data"]["balance"] == "290.00"
    assert replay.json()["data"]["balance"] == "290.00"
    assert balance.json()["balance"] == "290.00"
    assert history.json()["data"][0]["payment_id"] == payment_id


async def test_balance_promo_redemption_is_idempotent(api_client, partner_key):
    async with Database().session() as session:
        session.add(PromoCodes(
            code="CREDIT25", discount_type="balance", discount_value=25,
            max_uses=0, current_uses=0, is_active=True,
        ))
    headers = {
        "Authorization": f"Bearer {partner_key}",
        "Idempotency-Key": "balance-promo-once",
    }
    first = await api_client.post(
        "/v1/balance/promos/redeem", json={"code": "credit25"}, headers=headers,
    )
    replay = await api_client.post(
        "/v1/balance/promos/redeem", json={"code": "CREDIT25"}, headers=headers,
    )
    balance = await api_client.get("/v1/me/balance", headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert balance.json()["balance"] == "275.00"


async def test_stock_alert_can_be_subscribed_and_cancelled(api_client, partner_key, item_factory):
    await item_factory(name="Restock API", price=10)
    async with Database().session() as session:
        product_id = await session.scalar(select(Goods.id).where(Goods.name == "Restock API"))
    headers = {"Authorization": f"Bearer {partner_key}"}
    created = await api_client.post(f"/v1/products/{product_id}/stock-alert", headers=headers)
    removed = await api_client.delete(f"/v1/products/{product_id}/stock-alert", headers=headers)

    assert created.status_code == 201
    assert created.json()["subscribed"] is True
    assert removed.status_code == 200
    assert removed.json() == {"subscribed": False, "removed": True}


async def test_payment_method_list_excludes_unconfigured_and_manual_admin_methods(api_client, partner_key):
    headers = {"Authorization": f"Bearer {partner_key}"}
    response = await api_client.get("/v1/balance/payment-methods", headers=headers)
    methods = {row["id"] for row in response.json()["data"]}

    assert response.status_code == 200
    assert {"cryptopay", "xrocket", "stars", "telegram"}.issubset(methods)
    assert "test" not in methods
    assert "manual_crypto" not in methods
    assert "pay_admin" not in methods


async def test_indeterminate_provider_timeout_cannot_create_second_invoice(api_client, partner_key):
    from unittest.mock import AsyncMock, patch
    from bot.misc.services import CryptoPayAPI

    create_invoice = AsyncMock(side_effect=TimeoutError("simulated network timeout"))
    headers = {
        "Authorization": f"Bearer {partner_key}",
        "Idempotency-Key": "uncertain-payment-safe-retry",
    }
    with patch.object(CryptoPayAPI, "create_invoice", create_invoice):
        first = await api_client.post(
            "/v1/balance/top-ups", json={"provider": "cryptopay", "amount": 25}, headers=headers,
        )
        second = await api_client.post(
            "/v1/balance/top-ups", json={"provider": "cryptopay", "amount": 25}, headers=headers,
        )

    assert first.status_code == 503
    assert first.json()["error"]["code"] == "payment_creation_indeterminate"
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "request_in_progress"
    assert create_invoice.await_count == 1


async def test_payment_status_hides_another_accounts_payment(api_client, partner_key, user_factory):
    await user_factory(telegram_id=661002, balance=1)
    from bot.database.methods.create import create_pending_payment
    await create_pending_payment("cryptopay", "other-account-payment", 661002, 10, "RUB")
    async with Database().session() as session:
        payment_id = await session.scalar(select(Payments.id).where(Payments.external_id == "other-account-payment"))

    response = await api_client.get(
        f"/v1/balance/payments/{payment_id}",
        headers={"Authorization": f"Bearer {partner_key}"},
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", p) for p in [
            "/v1/me", "/v1/me/balance", "/v1/me/operations", "/v1/me/purchases",
            "/v1/me/purchases/1", "/v1/referrals", "/v1/referrals/earnings", "/v1/info",
            "/v1/categories", "/v1/products", "/v1/products/1", "/v1/cart",
            "/v1/products/1/reviews", "/v1/balance/payment-methods", "/v1/balance/payments/1",
            "/v1/balance/payments",
        ]
    ] + [
        ("POST", p) for p in [
            "/v1/orders/quote", "/v1/orders", "/v1/cart/checkout",
            "/v1/products/1/reviews", "/v1/products/1/stock-alert", "/v1/balance/top-ups",
            "/v1/balance/promos/redeem",
        ]
    ] + [
        ("PUT", "/v1/cart/items/1"),
        ("DELETE", "/v1/cart"),
        ("DELETE", "/v1/cart/items/1"),
        ("DELETE", "/v1/products/1/stock-alert"),
    ],
)
async def test_every_customer_api_route_requires_authentication(api_client, method, path):
    response = await api_client.request(method, path, json={} if method in {"POST", "PUT"} else None)

    assert response.status_code == 401, f"{method} {path}: {response.text}"

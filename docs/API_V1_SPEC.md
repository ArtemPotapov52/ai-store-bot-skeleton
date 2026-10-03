# My Store Partner API v1

## Purpose and accepted scope

Provide partner stores with a server-to-server API for customer-facing MyStore
Store operations. Every API key belongs to one existing Telegram account;
orders debit that account's balance and top-ups credit that same account only
after the configured payment provider confirms the transaction.

The existing admin panel remains on
`https://admin.185-23-239-241.nip.io`. The apex
`example.com` must not proxy to the admin application. The new host
`https://api.example.com` serves only API health, public API docs,
and versioned `/v1` routes. The API is a separate Starlette application/listener
inside the existing bot process; it does not change Telegram polling or expose
SQLAdmin routes.

## Authentication and invariants

- Partners use `Authorization: Bearer <api_key>` over HTTPS.
- `/apikey` in a private chat issues or rotates the account's single active key;
  the plaintext is returned once and only its digest/prefix are stored. A
  separate private-chat revoke command disables it. No API endpoint accepts a
  caller-selected Telegram user ID.
- Every operation derives its user from the authenticated key. Blocked or
  missing accounts fail closed.
- Keys grant fixed customer/partner capabilities only: no admin RBAC, catalog
  or stock writes, manual balance changes, refunds, broadcasts, exports, or
  database access.
- Product responses may show sellable quantity, never `ItemValues.value` or
  other unpurchased stock credentials. Purchased delivery values are returned
  only to the owning account.
- The server is authoritative for price, discounts, promos, and stock. The API
  never accepts a client-computed charge as the amount to debit.
- Mutating order/checkout/top-up/balance-promo requests require `Idempotency-Key`. Repeating
  the same key and payload returns the original result; reusing it with a
  different payload returns `409 idempotency_key_reused`.
- Balance increases only through the existing verified provider callback or
  Telegram payment confirmation. No API route can mark an invoice paid.
- Payment card data is never collected or stored by My Store. Hosted
  provider URLs remain provider-owned. Telegram invoice methods deliver an
  invoice to the Telegram account bound to the key.
- API credentials, payment URLs, and purchased delivery values are redacted
  from application logs.
- API responses include restrictive browser security headers. CORS is not
  enabled: this is a server-to-server API, and partners should call it from
  their own backend rather than expose bearer keys in a browser.

## HTTP contract

All responses use JSON except the HTML docs page. Errors have the shape
`{"error":{"code":"...","message":"...","request_id":"..."}}`.
List endpoints use bounded `limit` and `offset` pagination. Monetary
values are decimal strings with an explicit ISO currency code.
Read requests are limited to 120/minute per API key, writes to 20/minute per
key, and failed authentication to 30/minute per client address. `429` responses
include `Retry-After`; limits use the single bot process's in-memory counters.

Public routes (no API key):

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness only; no database details |
| `GET` | `/openapi.json` | OpenAPI 3 contract |
| `GET` | `/docs` | Human/interactive integration documentation |

Authenticated routes:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/v1/me` | Bound account identity and status |
| `GET` | `/v1/me/balance` | Current balance and currency |
| `GET` | `/v1/me/operations` | Paginated balance-operation history |
| `GET` | `/v1/me/purchases` | Paginated purchase history |
| `GET` | `/v1/me/purchases/{purchase_id}` | Owner-only purchase receipt/delivery |
| `GET` | `/v1/categories` | Active catalog categories |
| `GET` | `/v1/products` | Active products with category/search/page filters |
| `GET` | `/v1/products/{product_id}` | Product details, effective price, sellable count |
| `GET` | `/v1/cart` | Current account's cart and server-calculated total |
| `PUT` | `/v1/cart/items/{product_id}` | Set quantity and optional promo; idempotent replacement |
| `DELETE` | `/v1/cart/items/{product_id}` | Remove one owned cart line |
| `DELETE` | `/v1/cart` | Clear the authenticated account's cart |
| `POST` | `/v1/orders/quote` | Server-calculated direct-purchase quote |
| `POST` | `/v1/orders` | Buy one product/quantity from balance; idempotent |
| `POST` | `/v1/cart/checkout` | Atomic cart checkout; idempotent |
| `GET` | `/v1/referrals` | Referral count and earnings summary |
| `GET` | `/v1/referrals/earnings` | Paginated earnings for the authenticated account |
| `GET` | `/v1/info` | FAQ, support, legal links, and configured shop info |
| `GET` | `/v1/products/{product_id}/reviews` | Paginated published reviews and average |
| `POST` | `/v1/products/{product_id}/reviews` | Submit one review after a verified purchase |
| `POST` | `/v1/products/{product_id}/stock-alert` | Subscribe to stock notification |
| `DELETE` | `/v1/products/{product_id}/stock-alert` | Cancel stock notification |
| `GET` | `/v1/balance/payment-methods` | List only currently configured methods |
| `POST` | `/v1/balance/promos/redeem` | Redeem a one-time balance promo; idempotent |
| `POST` | `/v1/balance/top-ups` | Create provider payment/invoice; idempotent |
| `GET` | `/v1/balance/payments` | Paginated payment history for this account |
| `GET` | `/v1/balance/payments/{payment_id}` | Read/refresh only this account's payment status |

Top-up providers are limited to methods configured and enabled in the bot:
hosted-link methods return a validated provider URL; Telegram invoice methods
send an invoice to the Telegram account linked to the API key and return that
delivery state. Amount limits/currency use the existing shop configuration.
The Platega callback stays on its current configured HTTPS URL and retains its
existing authentication, amount checks, and duplicate-credit protection.

## Idempotency and transaction boundaries

Order, cart-checkout, balance-promo redemption, and payment-intent creation use persistent database
idempotency records scoped to the API key and request key. A successful purchase
record references its existing `BoughtGoods` rows rather than duplicating
delivery values into an idempotency payload. Reservation, balance debit, bought
goods, and order-idempotency state commit atomically. A payment link or Telegram
invoice response is persisted so a retry recovers the original response. If an
external provider call ends ambiguously, the key remains in `processing` and
cannot create another invoice; resolve the original intent before using a new
key. Nothing credits a balance until the existing verified provider/Telegram
payment flow confirms it. Completed/failed idempotency records are retained for
365 days; unresolved `processing` records are retained until reconciled so a
delayed retry cannot create a duplicate payment intent.

## Test and release safety

- Every route has isolated ASGI-level tests for success, validation, auth,
  ownership boundaries, and relevant failure/replay cases.
- Database tests use the existing temporary test database; provider and Telegram
  calls are mocked. No test reads from or mutates production inventory/balances.
- Production checks are read-only: DNS/TLS, `/health`, `/docs`, `/openapi.json`,
  API-host admin-route denial, and confirmation that the legacy admin host still
  responds. No production checkout or top-up is used as a smoke test.
- Production Caddy edits require a backup, `caddy validate`, reload only after
  validation, and post-reload checks. Existing bot polling, callback URL,
  database, secrets, and admin host are preserved.

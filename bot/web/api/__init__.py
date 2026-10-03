"""Isolated Starlette application for the My Store partner API."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from bot.database import Database
from bot.database.models import User
from bot.misc import EnvKeys
from bot.misc.timezone import moscow_isoformat
from bot.web.api.common import ApiError, SecurityHeadersMiddleware, api_route, error_response, json_response
from bot.web.api.catalog import routes as catalog_routes
from bot.web.api.account import routes as account_routes
from bot.web.api.commerce import routes as commerce_routes
from bot.web.api.payments import routes as payment_routes
from bot.misc.vpn_subscription_proxy import routes as vpn_subscription_routes

_DOCS_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Store API v1</title>
<style>body{font:16px/1.55 system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 20px;color:#202534}h1,h2{line-height:1.2}code,pre{background:#f2f4f8;border-radius:5px}code{padding:2px 5px}pre{padding:16px;overflow:auto}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:9px;border-bottom:1px solid #ddd}th{background:#f6f7fa}.note{padding:12px 16px;background:#fff8dc;border-left:4px solid #d5a400}</style></head>
<body><h1>My Store Partner API</h1><p>Версия v1. API предназначено для партнёрских магазинов; все операции выполняются от имени аккаунта, привязанного к API-ключу.</p>
<p><a href="/openapi.json">OpenAPI 3 JSON</a></p><h2>Авторизация</h2><p>В личном чате с ботом выполните <code>/api-key</code> (также работает <code>/apikey</code>). Ключ отображается только при выпуске. Повторный выпуск отключает старый ключ; <code>/revokeapikey</code> отзывает его.</p>
<pre>Authorization: Bearer ps_live_…</pre><p>Не отправляйте API-ключ из браузера или публичного кода. Считайте его паролем от привязанного аккаунта.</p>
<p>Все временные метки в ответах API возвращаются в часовом поясе <code>Europe/Moscow</code> с явным смещением <code>+03:00</code>.</p>
<div class="note">Цены, промокоды и остатки рассчитывает сервер. Покупка списывает баланс и может вернуть выданный товарный доступ. Не используйте production API для тестовой покупки.</div>
<p>В ответе товара поле <code>price</code> — цена одной единицы. Для товара с ценой по количеству каталог также возвращает <code>is_variable_pricing=true</code>, <code>min_quantity</code> и <code>max_quantity</code>. Перед покупкой запросите котировку: количество должно попадать в этот диапазон, а сумма рассчитывается сервером.</p>
<h2>Методы</h2><table><thead><tr><th>Метод</th><th>Назначение</th><th>Ключ</th></tr></thead><tbody>
<tr><td>GET /v1/me, /v1/me/balance</td><td>Профиль и баланс привязанного аккаунта</td><td>Да</td></tr>
<tr><td>GET /v1/me/operations, /v1/me/purchases, /v1/me/purchases/{id}</td><td>История операций и покупки (чек включает только собственную выдачу)</td><td>Да</td></tr>
<tr><td>GET /v1/categories, /v1/products, /v1/products/{id}</td><td>Категории, каталог, цены и количество без содержимого склада</td><td>Да</td></tr>
<tr><td>GET/PUT/DELETE /v1/cart…</td><td>Корзина аккаунта; PUT задаёт точное количество, а не прибавляет его</td><td>Да</td></tr>
<tr><td>POST /v1/orders/quote</td><td>Серверный расчёт каталожной цены перед покупкой</td><td>Да</td></tr>
<tr><td>POST /v1/orders, /v1/cart/checkout</td><td>Покупка со списанием баланса; нужны expected_total и Idempotency-Key</td><td>Да</td></tr>
<tr><td>GET /v1/balance/payment-methods</td><td>Фактически настроенные способы пополнения</td><td>Да</td></tr>
<tr><td>POST /v1/balance/top-ups</td><td>Создать счёт у включённого провайдера; нужен Idempotency-Key</td><td>Да</td></tr>
<tr><td>GET /v1/balance/payments, /v1/balance/payments/{id}</td><td>История и статус собственных платежей</td><td>Да</td></tr>
<tr><td>POST /v1/balance/promos/redeem</td><td>Активировать балансный промокод; нужен Idempotency-Key</td><td>Да</td></tr>
<tr><td>GET/POST /v1/products/{id}/reviews</td><td>Чтение и отзыв после подтверждённой покупки</td><td>Да</td></tr>
<tr><td>POST /v1/products/{id}/stock-alert</td><td>Подписка на уведомление о пополнении товара</td><td>Да</td></tr>
<tr><td>GET /v1/referrals, /v1/referrals/earnings, /v1/info</td><td>Рефералы, начисления и публичная информация магазина</td><td>Да</td></tr>
<tr><td>GET/HEAD /sub/{token}</td><td>Персональная HTTPS-подписка VPN; токен в URL, заголовки статуса/обновления проксируются</td><td>Секретная ссылка</td></tr>
</tbody></table>
<h2>Идентификаторы каталога и аккаунта</h2>
<p><strong>Получите актуальные ID из API каталога</strong>: числовой ID обычно сохраняется, пока запись существует, но может измениться после удаления и повторного добавления. Не копируйте ID из чужого примера и не используйте название вроде K12 вместо числа.</p>
<ol><li><code>GET /v1/categories</code> — возьмите <code>data[].id</code> и нужное имя категории.</li>
<li><code>GET /v1/products?category_id=CATEGORY_ID</code> — возьмите <code>data[].id</code> нужной позиции; запрос возвращает только активные товары.</li>
<li><code>GET /v1/products/PRODUCT_ID</code> — перед покупкой перепроверьте название, цену и наличие.</li></ol>
<table><thead><tr><th>Поле</th><th>Что означает и где использовать</th></tr></thead><tbody>
<tr><td><code>telegram_id</code></td><td>ID аккаунта Telegram, уже привязанного к ключу; возвращается в <code>GET /v1/me</code>. Нельзя подставить другой ID, чтобы переключить аккаунт.</td></tr>
<tr><td><code>category_id</code></td><td>Числовой <code>id</code> категории из <code>GET /v1/categories</code>; фильтр списка товаров.</td></tr>
<tr><td><code>product_id</code></td><td>Числовой <code>id</code> товара из <code>GET /v1/products</code>; используется в карточке, корзине, котировке и заказе.</td></tr>
<tr><td><code>purchase_id</code></td><td>Значение <code>id</code> из истории покупок; им запрашивают собственный чек и выдачу.</td></tr>
<tr><td><code>payment_id</code></td><td>ID своего пополнения из ответа/истории платежей; им проверяют его статус.</td></tr>
<tr><td><code>cart_item_id</code></td><td>ID строки корзины в ответе. Для изменения или удаления строки передавайте <code>product_id</code>.</td></tr>
<tr><td><code>id</code> операции/начисления</td><td>ID записи истории из <code>/v1/me/operations</code> или <code>/v1/referrals/earnings</code>; это ID записи, не пользователя.</td></tr>
<tr><td><code>provider</code></td><td>Строковый ID способа из <code>GET /v1/balance/payment-methods</code>; передавайте его без изменений при создании пополнения.</td></tr>
</tbody></table>
<p>Известные ID провайдеров: <code>platega</code> — СБП/карта; <code>cryptopay</code> — Crypto Pay; <code>xrocket</code> — xRocket; <code>telegram</code> — Telegram Payments. Доступны только способы, которые сейчас настроены: ориентируйтесь на актуальный ответ <code>GET /v1/balance/payment-methods</code>.</p>
<h2>Обозначения в названии товара</h2>
<table><thead><tr><th>Код</th><th>Значение</th></tr></thead><tbody>
<tr><td><code>K12</code></td><td>Учительская подписка: аналог Plus с лимитами на 15% выше.</td></tr>
<tr><td><code>FW</code></td><td>Полная гарантия.</td></tr>
<tr><td><code>NW</code></td><td>Гарантии нет.</td></tr>
<tr><td><code>UPI</code></td><td>Способ оплаты.</td></tr>
<tr><td><code>10D 5FW</code></td><td>Доступ на 10 дней; гарантия действует 5 дней.</td></tr>
</tbody></table>
<p>Коды и слова в названии описывают позицию, но не заменяют числовой <code>product_id</code>. Категории вроде ChatGPT, Gemini, Claude, Grok, Perplexity и CapCut также нужно выбирать по текущему <code>category_id</code>.</p>
<h2>Текущие категории и товары</h2>
<p>Снимок активного каталога на проде на <strong>24.09.2026</strong>. Категории с нулём позиций сейчас не содержат активных товаров. Перед заказом всё равно запрашивайте каталог: ID сохраняется, пока запись существует, но изменится, если товар удалить и создать заново.</p>
<table><thead><tr><th>category_id</th><th>Категория</th><th>Активных товаров</th></tr></thead><tbody>
<tr><td><code>1</code></td><td>ChatGPT</td><td>3</td></tr>
<tr><td><code>2</code></td><td>Gemini</td><td>1</td></tr>
<tr><td><code>3</code></td><td>Claude</td><td>0</td></tr>
<tr><td><code>4</code></td><td>Grok</td><td>1</td></tr>
<tr><td><code>5</code></td><td>Perplexity</td><td>0</td></tr>
<tr><td><code>11</code></td><td>CapCut</td><td>1</td></tr>
</tbody></table>
<table><thead><tr><th>category_id</th><th>product_id</th><th>Название товара (как в каталоге)</th></tr></thead><tbody>
<tr><td><code>1</code></td><td><code>2</code></td><td>ChatGPT K12 EDU 2 ГОДА</td></tr>
<tr><td><code>1</code></td><td><code>7</code></td><td>ChatGPT Plus 1m (NW)</td></tr>
<tr><td><code>1</code></td><td><code>6</code></td><td>CDK ChatGPT 1 Месяц (FW) Официальная покупка</td></tr>
<tr><td><code>2</code></td><td><code>5</code></td><td>Gemini + 5ТБ 18 месяцев (ccылка)</td></tr>
<tr><td><code>4</code></td><td><code>4</code></td><td>Grok 9-10D FW</td></tr>
<tr><td><code>11</code></td><td><code>8</code></td><td>CapCut 7D</td></tr>
</tbody></table>
<p>Например, список товаров категории CapCut можно запросить как <code>GET /v1/products?category_id=11</code>; для покупки CapCut 7D используйте <code>product_id=8</code>.</p>
<h2>Как купить товар по ID</h2>
<p>Ниже пример покупки <code>ChatGPT K12 EDU 2 ГОДА</code> (<code>product_id=2</code>). Сначала сервер считает актуальную сумму; затем передайте её без изменений как <code>expected_total</code>. На привязанном аккаунте должен быть достаточный баланс.</p>
<pre>import uuid
import requests

BASE = "https://api.example.com"
API_KEY = "YOUR_API_KEY"
product_id = 2
headers = {"Authorization": f"Bearer {API_KEY}"}

quote_response = requests.post(
    f"{BASE}/v1/orders/quote",
    headers=headers,
    json={"product_id": product_id, "quantity": 1},
)
quote_response.raise_for_status()
quote = quote_response.json()["data"]

order_response = requests.post(
    f"{BASE}/v1/orders",
    headers={**headers, "Idempotency-Key": f"store-{uuid.uuid4()}"},
    json={
        "product_id": product_id,
        "quantity": 1,
        "expected_total": quote["total"],
    },
)
print(order_response.status_code, order_response.json())</pre>
<p>Для других позиций подставьте их <code>product_id</code> из таблицы. Уникальный <code>Idempotency-Key</code> создавайте на каждую новую покупку; при сетевом тайм-ауте повторяйте тот же запрос с тем же ключом.</p>
<h2>Пример запроса</h2>
<pre>curl -H 'Authorization: Bearer YOUR_API_KEY' \\
  https://api.example.com/v1/me</pre>
<p>Перед покупкой отправьте JSON в <code>POST /v1/orders/quote</code>, затем повторите product_id, quantity и полученный total как expected_total в <code>POST /v1/orders</code>. Цена всегда берётся из каталога; скидки и промокоды на покупку отключены. Заголовок <code>Idempotency-Key</code> обязателен. Корзина оформляется аналогично через <code>GET /v1/cart</code> и <code>POST /v1/cart/checkout</code>.</p>
<p>Поле <code>expected_total</code> берётся из серверной котировки, передаётся десятичной строкой и защищает от незаметного изменения цены. При ошибке сети повторяйте тот же запрос с тем же ключом.</p>
<h2>Пример пополнения</h2><pre>POST /v1/balance/top-ups
Authorization: Bearer YOUR_API_KEY
Idempotency-Key: store-topup-00043
Content-Type: application/json

{"provider":"platega","amount":100}</pre>
<p>Для ответа <code>payment_creation_indeterminate</code> не создавайте новый ключ и второе пополнение: сначала проверьте оригинальный платёж или обратитесь в поддержку.</p>
<p>Ответы не кэшируются. Ошибки имеют JSON-поле <code>error.code</code>; ключ идемпотентности можно повторять только с тем же методом, путем и телом. Состояние <code>request_in_progress</code> следует повторять с тем же ключом, не создавая новый.</p>
</body></html>"""

OPENAPI_SCHEMA: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "My Store Partner API",
        "version": "1.0.0",
        "description": "Партнёрский API. Все операции привязаны к аккаунту владельца bearer API-ключа.",
    },
    "servers": [{"url": "https://api.example.com"}],
    "components": {
        "securitySchemes": {
            "PartnerApiKey": {"type": "http", "scheme": "bearer", "bearerFormat": "Store API key"}
        },
        "schemas": {
            "Error": {
                "type": "object",
                "required": ["error"],
                "properties": {"error": {"type": "object", "required": ["code", "message", "request_id"]}},
            },
            "OrderQuoteRequest": {
                "type": "object", "required": ["product_id"], "additionalProperties": False,
                "properties": {
                    "product_id": {"type": "integer", "minimum": 1},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 1, "description": "Проверьте min_quantity/max_quantity товара в каталоге; цена умножается на количество."},
                },
            },
            "OrderRequest": {
                "type": "object", "required": ["product_id", "expected_total"], "additionalProperties": False,
                "properties": {
                    "product_id": {"type": "integer", "minimum": 1},
                    "quantity": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 1, "description": "Должно соответствовать диапазону количества товара из каталога."},
                    "expected_total": {"type": "string", "pattern": "^[0-9]+(?:\\.[0-9]{1,2})?$"},
                },
            },
            "CartItemRequest": {
                "type": "object", "required": ["quantity"], "additionalProperties": False,
                "properties": {"quantity": {"type": "integer", "minimum": 1, "maximum": 5000}},
            },
            "CartCheckoutRequest": {
                "type": "object", "required": ["expected_total"], "additionalProperties": False,
                "properties": {"expected_total": {"type": "string", "pattern": "^[0-9]+(?:\\.[0-9]{1,2})?$"}},
            },
            "TopUpRequest": {
                "type": "object", "required": ["provider", "amount"], "additionalProperties": False,
                "properties": {
                    "provider": {"type": "string", "enum": ["platega", "cryptopay", "xrocket", "telegram"]},
                    "amount": {"type": "integer", "minimum": 1},
                },
            },
            "BalancePromoRequest": {
                "type": "object", "required": ["code"], "additionalProperties": False,
                "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 50}},
            },
            "ReviewRequest": {
                "type": "object", "required": ["rating"], "additionalProperties": False,
                "properties": {"rating": {"type": "integer", "minimum": 1, "maximum": 5}, "text": {"type": "string", "maxLength": 2000}},
            },
        },
    },
    "paths": {
        "/health": {"get": {"summary": "Проверка доступности", "responses": {"200": {"description": "Процесс работает"}}}},
        "/sub/{token}": {
            "get": {
                "summary": "Загрузить VPN-подписку по секретной персональной ссылке",
                "parameters": [{"name": "token", "in": "path", "required": True, "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"}}],
                "responses": {"200": {"description": "Содержимое подписки и метаданные клиента"}, "404": {"description": "Ссылка не найдена или отозвана"}, "410": {"description": "Подписка-источник истекла"}, "429": {"description": "Слишком частые запросы"}, "503": {"description": "Прокси не настроен"}},
            },
            "head": {
                "summary": "Проверить статус и метаданные VPN-подписки",
                "parameters": [{"name": "token", "in": "path", "required": True, "schema": {"type": "string", "pattern": "^[A-Za-z0-9_-]{43}$"}}],
                "responses": {"200": {"description": "Заголовки профиля без содержимого"}, "404": {"description": "Ссылка не найдена или отозвана"}, "410": {"description": "Подписка-источник истекла"}, "429": {"description": "Слишком частые запросы"}},
            },
        },
        "/docs": {"get": {"summary": "Документация API", "responses": {"200": {"description": "HTML-документация"}}}},
        "/openapi.json": {"get": {"summary": "OpenAPI-контракт", "responses": {"200": {"description": "OpenAPI 3.1 JSON"}}}},
        "/v1/me": {"get": {"summary": "Текущий аккаунт", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Привязанный аккаунт"}, "401": {"description": "Нужен API-ключ"}}}},
        "/v1/categories": {"get": {"summary": "Категории каталога", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Активные категории"}}}},
        "/v1/products": {"get": {"summary": "Список и поиск товаров", "security": [{"PartnerApiKey": []}], "parameters": [{"name": "search", "in": "query", "schema": {"type": "string"}}, {"name": "category_id", "in": "query", "schema": {"type": "integer", "minimum": 1}}, {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100}}, {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0}}], "responses": {"200": {"description": "Страница каталога"}}}},
        "/v1/products/{product_id}": {"get": {"summary": "Карточка товара", "security": [{"PartnerApiKey": []}], "parameters": [{"name": "product_id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Товар без содержимого складских единиц"}, "404": {"description": "Товар не найден"}}}},
        "/v1/me/balance": {"get": {"summary": "Баланс аккаунта", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Баланс в валюте магазина"}}}},
        "/v1/me/operations": {"get": {"summary": "История операций", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Пополнения и списания аккаунта"}}}},
        "/v1/me/purchases": {"get": {"summary": "История покупок", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Список покупок без содержимого выдачи"}}}},
        "/v1/me/purchases/{purchase_id}": {"get": {"summary": "Чек покупки", "security": [{"PartnerApiKey": []}], "parameters": [{"name": "purchase_id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Чек и выдача только владельцу покупки"}, "404": {"description": "Покупка не найдена"}}}},
        "/v1/referrals": {"get": {"summary": "Сводка рефералов", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Число рефералов и начисления"}}}},
        "/v1/referrals/earnings": {"get": {"summary": "История реферальных начислений", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Пагинированная история"}}}},
        "/v1/info": {"get": {"summary": "Информация о магазине", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "FAQ, правила и контакты"}}}},
        "/v1/orders/quote": {"post": {"summary": "Расчёт заказа", "security": [{"PartnerApiKey": []}], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrderQuoteRequest"}}}}, "responses": {"200": {"description": "Серверный расчёт каталожной цены"}}}},
        "/v1/orders": {"post": {"summary": "Покупка товара с баланса", "security": [{"PartnerApiKey": []}], "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "maxLength": 128}}], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OrderRequest"}}}}, "responses": {"201": {"description": "Покупка и выдача"}, "402": {"description": "Недостаточно средств"}, "409": {"description": "Цена изменилась или товар недоступен"}}}},
        "/v1/cart": {"get": {"summary": "Корзина аккаунта", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Текущая корзина с серверными ценами"}}}, "delete": {"summary": "Очистить корзину", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Корзина очищена"}}}},
        "/v1/cart/items/{product_id}": {"put": {"summary": "Установить количество товара в корзине", "security": [{"PartnerApiKey": []}], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CartItemRequest"}}}}, "responses": {"200": {"description": "Строка корзины"}}}, "delete": {"summary": "Удалить товар из корзины", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Строка удалена"}}}},
        "/v1/cart/checkout": {"post": {"summary": "Оформить корзину", "security": [{"PartnerApiKey": []}], "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "maxLength": 128}}], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/CartCheckoutRequest"}}}}, "responses": {"200": {"description": "Покупки из корзины"}, "402": {"description": "Недостаточно средств"}}}},
        "/v1/products/{product_id}/reviews": {"get": {"summary": "Отзывы товара", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Оценка и список отзывов"}}}, "post": {"summary": "Оставить отзыв после покупки", "security": [{"PartnerApiKey": []}], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ReviewRequest"}}}}, "responses": {"201": {"description": "Отзыв создан"}, "403": {"description": "Нужна завершённая покупка"}, "409": {"description": "Отзыв уже оставлен"}}}},
        "/v1/products/{product_id}/stock-alert": {"post": {"summary": "Подписаться на пополнение", "security": [{"PartnerApiKey": []}], "responses": {"201": {"description": "Подписка активна"}}}, "delete": {"summary": "Отписаться от уведомления о пополнении", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Подписка удалена"}}}},
        "/v1/balance/payment-methods": {"get": {"summary": "Доступные способы пополнения", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Только настроенные реальные провайдеры"}}}},
        "/v1/balance/payments": {"get": {"summary": "История платежей", "security": [{"PartnerApiKey": []}], "responses": {"200": {"description": "Платежи текущего аккаунта без внешних секретов"}}}},
        "/v1/balance/promos/redeem": {"post": {"summary": "Активировать балансный промокод", "security": [{"PartnerApiKey": []}], "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "maxLength": 128}}], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BalancePromoRequest"}}}}, "responses": {"201": {"description": "Промокод применён один раз к аккаунту"}}}},
        "/v1/balance/top-ups": {"post": {"summary": "Создать пополнение баланса", "security": [{"PartnerApiKey": []}], "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "maxLength": 128}}], "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TopUpRequest"}}}}, "responses": {"201": {"description": "Платёжная ссылка или Telegram-счёт"}, "503": {"description": "Неопределённый результат создания счёта; используйте тот же ключ"}}}},
        "/v1/balance/payments/{payment_id}": {"get": {"summary": "Проверить платёж", "security": [{"PartnerApiKey": []}], "parameters": [{"name": "payment_id", "in": "path", "required": True, "schema": {"type": "integer"}}], "responses": {"200": {"description": "Текущий статус и баланс владельца"}, "404": {"description": "Платёж не найден"}}}},
    },
}


async def _health(request: Request) -> JSONResponse:
    return json_response({"status": "healthy"})


async def _docs(request: Request) -> HTMLResponse:
    return HTMLResponse(_DOCS_HTML, headers={"Cache-Control": "no-store"})


async def _openapi(request: Request) -> JSONResponse:
    return json_response(OPENAPI_SCHEMA)


async def _http_error(request: Request, exc) -> JSONResponse:
    request_id = str(uuid4())
    if exc.status_code == 404:
        return error_response(404, "not_found", "The requested API route was not found.", request_id)
    if exc.status_code == 405:
        return error_response(405, "method_not_allowed", "This method is not allowed for the route.", request_id)
    return error_response(400, "invalid_request", "The request could not be processed.", request_id)


async def _me(request: Request) -> JSONResponse:
    user_id = int(request.state.api_user_id)
    async with Database().session() as session:
        user = (await session.execute(
            select(User).where(User.telegram_id == user_id)
        )).scalar_one_or_none()
    if user is None or user.is_blocked:
        raise ApiError(403, "account_unavailable", "This account cannot use the API.")
    return json_response({
        "telegram_id": int(user.telegram_id),
        "balance": format(Decimal(str(user.balance or 0)), ".2f"),
        "currency": str(EnvKeys.PAY_CURRENCY).upper(),
        "locale": str(user.locale or "ru"),
        "registered_at": moscow_isoformat(user.registration_date),
    })


def create_partner_api_app(bot: Any = None):
    """Create the API-only ASGI app; it intentionally has no SQLAdmin mounts."""
    routes = [
        api_route("/health", _health, protected=False, name="api_health"),
        api_route("/docs", _docs, protected=False, name="api_docs"),
        api_route("/openapi.json", _openapi, protected=False, name="api_openapi"),
        api_route("/v1/me", _me, protected=True, name="api_me"),
    ] + vpn_subscription_routes + account_routes() + catalog_routes() + commerce_routes() + payment_routes()
    from starlette.applications import Starlette

    starlette_app = Starlette(
        routes=routes,
        debug=False,
        exception_handlers={404: _http_error, 405: _http_error},
    )
    starlette_app.state.bot = bot
    return SecurityHeadersMiddleware(starlette_app)


__all__ = ["create_partner_api_app"]

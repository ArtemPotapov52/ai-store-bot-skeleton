# Каркас AI Store бота — старт для нового админа

Это очищенный каркас. Здесь НЕТ реальных товаров, остатков, клиентов,
токенов, кошельков, чатов и юридических данных предыдущего магазина.
Всё это новый админ создаёт сам.

## 1. Что внутри

- Telegram-бот (aiogram 3) + веб-админка (sqladmin) + Partner API
- PostgreSQL + миграции Alembic, опционально Redis
- Оплаты: Telegram Payments, CryptoBot, xRocket, Platega (СБП), Stars, ручное пополнение
- Пустой каталог из коробки. Демо-данные только явно фейковые (`DEMO-*`)

## 2. Быстрый старт (новый магазин)

```bash
cp .env.example .env
# заполни минимум: TOKEN, OWNER_ID, POSTGRES_PASSWORD, ADMIN_PASSWORD, SECRET_KEY
# SHOP_NAME, SUPPORT_USERNAME, PAYMENT_ADMIN_USERNAME — свои, не чужие
# MANUAL_* кошельки — только свои, пустое = способ выключен
# LEGAL_* и FAQ — свои тексты, пустое = кнопка скрыта

python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head

# вариант А — только пустые категории без товаров:
.venv/bin/python -m scripts.seed_ai_categories

# вариант Б — демо-каталог с фейковым складом DEMO-* для проверки UI:
.venv/bin/python -m scripts.seed_demo_catalog

.venv/bin/python run.py
```

Затем в Telegram с аккаунта `OWNER_ID`: `/start`, далее `/xyz67` → пароль из `BOT_ADMIN_PASSWORD` (или `ADMIN_PASSWORD`).

## 3. Как добавить свои товары

1. Админка в боте (`/xyz67`) или веб-админка `http://127.0.0.1:9090/admin`:
   - Категории → создать (название, порядок, картинка из `assets/ui/`)
   - Товары → создать (название, цена, описание, видимость)
   - Склад → добавить позиции (логин/пароль/ключ построчно)
2. Проверь покупку тестовым аккаунтом.
3. Удали демо-товары `DEMO-*` перед запуском: через админку или пересоздай БД.
4. Заполни `FAQ`, `RULES`, `AGREEMENT`, `LEGAL_*`, `SUPPORT_*`, `CHANNEL_URL` в `.env`.
5. На VPS — по `DEPLOY_VPS.md`. `.env` никогда не коммитить (`chmod 600`).

## 4. Что НЕ переносить из старого магазина

- `TOKEN`, `OWNER_ID`, `ADMIN_PASSWORD`, `SECRET_KEY`, `POSTGRES_PASSWORD`
- `CRYPTO_PAY_TOKEN`, `XROCKET_PAY_TOKEN`, `PLATEGA_*`, `TELEGRAM_PROVIDER_TOKEN`
- `MANUAL_*` кошельки, `SUPPORT_*`, `CHANNEL_*`, `COMMUNITY_*`
- Товары, остатки, заказы, пользователи, дампы БД (`backups/`, `*.dump`, `*.sql`)
- `logs/`, `data/`, `.env`

Проверка перед публикацией:

```bash
# подставь реальные следы старого магазина в шаблон (показаны разорванными, чтобы сам док не срабатывал):
grep -rIn -E "palla(s)|i6yo(l)|duckdn(s)|telegra\.ph/Polzova(t)|0x25C(8)" --include="*.py" --include="*.md" --include="*.example" . | head
grep -rIn -E "185\.23\.|Z89W50(3)" --include="*.py" --include="*.md" --include="*.example" . | head
ls .env data logs backups 2>&1
```

Первая команда должна быть пустой, вторая — «No such file» для `.env` в репозитории (локальный `.env` не коммитится).

## 5. Структура

- `bot/` — хендлеры, клавиатуры, платежи, веб-админка
- `migrations/` — схема БД
- `scripts/seed_ai_categories.py` — пустые категории
- `scripts/seed_demo_catalog.py` — демо с `DEMO-*`, в проде не запускать
- `deploy/` — systemd + Caddy
- `assets/ui/` — картинки витрины (замени на свои)
- `tests/` — pytest, фейковые данные

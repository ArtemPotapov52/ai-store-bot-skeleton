# Локальный запуск на macOS без Docker

Инструкция рассчитана на Mac, включая Apple Silicon. Бот использует локальный
PostgreSQL 16, а админка доступна только с этого компьютера.

## 1. Установить зависимости

```bash
brew install python@3.11 postgresql@16
brew services start postgresql@16
```

`postgresql@16` в Homebrew является keg-only, поэтому в следующих командах
используется его полный путь:

```bash
export PG_BIN="$(brew --prefix postgresql@16)/bin"
"$PG_BIN/psql" --version
```

## 2. Скачать и установить проект

```bash
git clone https://github.com/your-org/your-shopbot.git
cd your-shopbot
python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
mkdir -p logs data
```

Не запускай `pip install` вне `.venv`: macOS/Homebrew специально блокирует
системную установку Python-пакетов.

## 3. Создать локальную базу

Homebrew создаёт PostgreSQL-роль с именем твоего пользователя macOS. Подставь
вместо `YOUR_MAC_LOGIN` результат команды `whoami`.

```bash
whoami
"$PG_BIN/createdb" telegram_shop
"$PG_BIN/psql" postgres -c "ALTER ROLE \"YOUR_MAC_LOGIN\" WITH LOGIN PASSWORD 'local_dev_password';"
```

Если `createdb` напишет, что `telegram_shop` уже существует, просто переходи
к следующему шагу. `local_dev_password` допустим только для локальной разработки.

## 4. Заполнить `.env`

Открой `.env` и укажи минимум такие значения:

```dotenv
TOKEN=PASTE_A_TEST_BOT_TOKEN_FROM_BOTFATHER
OWNER_ID=YOUR_TELEGRAM_NUMERIC_ID

POSTGRES_HOST=127.0.0.1
POSTGRES_DB=telegram_shop
POSTGRES_USER=YOUR_MAC_LOGIN
POSTGRES_PASSWORD=local_dev_password

REDIS_ENABLED=0
ADMIN_HOST=127.0.0.1
ADMIN_PORT=9090
ADMIN_USERNAME=admin
ADMIN_PASSWORD=choose_a_local_password
# Optional; if omitted, /xyz67 uses ADMIN_PASSWORD.
BOT_ADMIN_PASSWORD=choose_a_bot_admin_password
SECRET_KEY=replace_with_a_random_value
ADMIN_COOKIE_SECURE=0

DEBUG=1
TEST_PAYMENT_ENABLED=1
SHOP_NAME="My Store"
PAYMENT_ADMIN_USERNAME=
```

Сгенерируй `SECRET_KEY` командой:

```bash
.venv/bin/python -c "import secrets; print(secrets.token_hex(32))"
```

`TEST_PAYMENT_ENABLED=1` работает только вместе с `DEBUG=1`. Это локальная
демо-оплата; перед реальным развёртыванием обязательно верни значение на `0`.

В профиле также доступны заглушка «СБП / карта» и ручное пополнение через
администратора. Кнопка администратора открывает чат с указанным username и подставляет
сумму в готовое сообщение. Для автоматического CryptoBot-платежа добавь токен
Crypto Pay API в `CRYPTO_PAY_TOKEN` и перезапусти бота.

## 5. Применить миграции, загрузить демо-каталог и запустить

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed_demo_catalog
.venv/bin/python run.py
```

Затем открой тестового бота в Telegram с аккаунта, чей ID указан в `OWNER_ID`,
и отправь `/start`. Для входа в защищённую админ-панель отправь `/xyz67` и
введи `BOT_ADMIN_PASSWORD` (или `ADMIN_PASSWORD`, если отдельный пароль не задан).
Именно в этот личный чат бот присылает уведомления об
изменениях склада: название товара, цену и добавленное количество. Пока процесс
запущен, админка доступна по адресу `http://127.0.0.1:9090/admin`. В демо-
каталоге лежит только явно фейковый склад `DEMO-*`.

## 6. Проверить и остановить

```bash
tail -f logs/bot.log
brew services list
brew services stop postgresql@16
```

Для реального VPS используй [DEPLOY_VPS.md](DEPLOY_VPS.md). Не переноси в
production локальный пароль базы, флаг тестовой оплаты или dev-логины.

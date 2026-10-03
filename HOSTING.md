# Подготовка к хостингу без Docker

Основной вариант для этого репозитория — Render Blueprint в [`render.yaml`](render.yaml): один постоянно работающий Python веб-сервис, управляемый PostgreSQL и небольшой постоянный диск. Бот и веб-админка уже работают в одном процессе. `run.py` запускает их вместе; `/health` проверяет доступность базы. Версия Python зафиксирована в [`.python-version`](.python-version).

## Render

Blueprint рассчитан на Git-корень `shopbot/`. При его создании Render установит зависимости из `requirements.txt`, применит миграции до запуска процесса и затем выполнит `python run.py`. Python-сервис слушает системный `PORT` на `0.0.0.0`; приложение теперь использует `PORT`, если не задан `ADMIN_PORT`. Render обычно выдаёт порт `10000`. PostgreSQL-поля берутся из управляемой базы через `fromDatabase`, без пароля в репозитории. Проверка готовности — `GET /health`. [Blueprint YAML](https://render.com/docs/blueprint-spec), [порты веб-сервиса](https://render.com/docs/web-services), [версии Python](https://render.com/docs/python-version).

План `starter` выбран для процесса, который должен принимать сообщения постоянно. Бесплатный веб-сервис Render засыпает после 15 минут без входящего HTTP-трафика, даже если бот продолжает ожидать Telegram-обновления; бесплатная база PostgreSQL ограничена 30 днями. База `basic-256mb` и 1 ГБ диска — начальные размеры для небольшого каталога; после реального запуска следует смотреть память, соединения и время ответа. Актуальную цену проверяй перед созданием сервисов. [Ограничения бесплатного плана](https://render.com/docs/free), [цены](https://render.com/pricing).

Диск смонтирован в `data/`. Это сохраняет файловый снимок метрик и заставляет Render остановить старый процесс до запуска нового при обновлении. Для бота на long polling это предотвращает одновременную работу двух копий с одним Telegram-токеном. Следствие — короткая пауза на каждом обновлении вместо нулевого простоя. Логи идут в поток вывода Render; каталог, пользователи и платежи живут в PostgreSQL. [Постоянные диски](https://render.com/docs/disks), [порядок развёртывания](https://render.com/docs/deploys).

Перед первым развёртыванием:

1. Опубликовать текущие изменения репозитория на выбранной Git-ветке и связать с ней Blueprint. Пока они есть только в локальной копии этого проекта, Render их не видит. Автоматическое обновление в Blueprint выключено (`autoDeployTrigger: off`).
2. При создании Blueprint заполнить `TOKEN`, `OWNER_ID` и сильный `ADMIN_PASSWORD` в настройках Render. `SECRET_KEY` Render сгенерирует сам. Платежи и поддержка (`CRYPTO_PAY_TOKEN`, `XROCKET_PAY_TOKEN`, `SUPPORT_USERNAME`, `PAYMENT_ADMIN_USERNAME`, кошельки `MANUAL_*`) тоже задаются в дашборде — пустое значение выключает способ. Локальный `.env` не входит в Git и не должен загружаться как файл. Админка будет доступна по HTTPS на `/admin`; публичный доступ требует сильного пароля. Ориентир цены — Starter веб-сервис $7/мес + Postgres Basic-256mb $6/мес, итого ~$13/мес (бесплатный план не годится: сервис засыпает, а база удаляется через 30 дней).
3. Решить, переносить ли существующие данные локальной PostgreSQL-базы. Blueprint создаёт новую пустую базу и таблицы, но не переносит каталог, пользователей и покупки. Внешний доступ к управляемой базе сейчас закрыт (`ipAllowList: []`); для импорта с Mac его нужно будет временно разрешить или использовать другой способ переноса.
4. Проверить нужные бизнес-настройки отдельно: название магазина, тексты, изображения витрины и способ оплаты. Этот Blueprint запускает бот с отключёнными тестовой оплатой, CryptoPay, Telegram Payments, Redis и webhook. Готовых изображений в `assets/ui/` пока нет.

Это подготовка файлов. Сервисы Render не созданы, денег не списано, репозиторий никуда не отправлен.

## Railway и Vercel

Railway также может запустить этот бот как постоянный Python-сервис. Базовые настройки уже лежат в [`railway.toml`](railway.toml): сборка Railpack, запуск `python -m alembic upgrade head && python run.py`, проверка `/health` и строго **одна реплика**. В дашборде Railway нужно **явно выбрать Railpack**, чтобы имеющийся в репозитории `Dockerfile` не стал способом сборки, и добавить PostgreSQL-плагин. Обязательные переменные сервиса:
- `ADMIN_HOST=0.0.0.0`, `ADMIN_COOKIE_SECURE=1`, `REDIS_ENABLED=0`, `DEBUG=0`, `TEST_PAYMENT_ENABLED=0`, `WEBHOOK_ENABLED=0`;
- `TOKEN`, `OWNER_ID`, сильный `ADMIN_PASSWORD` (+ сгенерируй `SECRET_KEY`);
- платежи/поддержка: `CRYPTO_PAY_TOKEN`, `XROCKET_PAY_TOKEN`, `SUPPORT_USERNAME`, `PAYMENT_ADMIN_USERNAME`, `SHOP_NAME=My Store`, `MANUAL_USDT_BEP20`, `MANUAL_TON`, `MANUAL_SOL` (пустое = способ выключен);
- подключение к Railway PostgreSQL собрать из ссылочных `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD` в `POSTGRES_HOST`, `DB_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.

Разрешить только одну копию получателя Telegram-обновлений (`numReplicas = 1` уже в конфиге), иначе две копии с одним токеном будут драться через 409 Conflict — это же касается и локального бота на Mac: после переезда его надо остановить. Ориентир цены — план Hobby $5/мес, дальше по факту потребления (память $10/ГБ, CPU $20/vCPU): бот + Postgres обычно выходят в ~$8–12/мес. [Сборка Railpack](https://docs.railway.com/builds/build-and-start-commands), [переменные PostgreSQL](https://docs.railway.com/databases/postgresql), [цены](https://railway.com/pricing).

Vercel здесь требует переделки архитектуры: текущий `run.py` — постоянный процесс с long polling и встроенной веб-админкой, а Vercel запускает Python как ограниченные по времени функции. Для него понадобились бы webhook-обработчик, отдельное размещение админки и надёжное хранение состояния между вызовами. Поэтому конфигурация Vercel для текущего кода не добавлена. [Лимиты Vercel Functions](https://vercel.com/docs/functions/configuring-functions/duration).

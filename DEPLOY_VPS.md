# Deployment on a regular Ubuntu VPS (without Docker)

The production layout uses Telegram long polling, PostgreSQL, optional Redis,
an admin panel bound to localhost, and a hardened `systemd` service.

## 1. OS packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv postgresql redis-server git
sudo useradd --system --create-home --home-dir /opt/ai-store-bot --shell /usr/sbin/nologin aistore
sudo -u postgres createuser --pwprompt shop_user
sudo -u postgres createdb --owner shop_user telegram_shop
```

## 2. Application

```bash
sudo -u aistore git clone YOUR_GITHUB_REPOSITORY /opt/ai-store-bot
cd /opt/ai-store-bot
sudo -u aistore python3 -m venv .venv
sudo -u aistore .venv/bin/pip install -r requirements.txt
sudo -u aistore cp .env.example .env
sudo chmod 600 .env
```

Fill `.env`. For production use at least:

```dotenv
APP_ENV=production
TOKEN=...
OWNER_ID=...
POSTGRES_HOST=127.0.0.1
POSTGRES_DB=telegram_shop
POSTGRES_USER=shop_user
POSTGRES_PASSWORD=...
REDIS_ENABLED=1
REDIS_HOST=127.0.0.1
ADMIN_HOST=127.0.0.1
ADMIN_PORT=9090
ADMIN_USERNAME=...
ADMIN_PASSWORD=...
SECRET_KEY=...
CRYPTO_PAY_TOKEN=...
XROCKET_PAY_TOKEN=...
SUPPORT_USERNAME=...
PAYMENT_ADMIN_USERNAME=...
SHOP_NAME=My Store
MANUAL_USDT_BEP20=...
MANUAL_TON=...
MANUAL_SOL=...
DEBUG=0
TEST_PAYMENT_ENABLED=0
```

Empty payment/support values simply disable that method. Never commit this
file — it stays only on the server (`chmod 600`).

Generate secrets instead of inventing them manually:

```bash
openssl rand -base64 36
python3 -c "import secrets; print(secrets.token_hex(32))"
```

## 3. Database and service

```bash
cd /opt/ai-store-bot
sudo -u aistore .venv/bin/alembic upgrade head
sudo install -o root -g root -m 0644 deploy/ai-store-bot.service /etc/systemd/system/ai-store-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now ai-store-bot
sudo systemctl status ai-store-bot
```

Do not seed demo values on production. Load real categories, products and stock
through the admin panel after reviewing the product terms.

## 4. Admin access

The admin panel deliberately listens only on `127.0.0.1`. Two ways to reach
it:

### Option A — public HTTPS with login/password (recommended)

The bot keeps listening on localhost; Caddy terminates HTTPS and proxies to
it. Logins stay protected by the built-in rate limiter (5 attempts per
15 minutes per IP), Secure cookies and audit logging.

```bash
# 1. Point a subdomain at the server, e.g. admin.yourdomain.com -> VPS IP.
#    No domain? Use free nip.io instead: if your VPS IP is 45.155.204.9,
#    your address is admin.45-155-204-9.nip.io (resolves automatically,
#    Caddy still issues a real HTTPS certificate).
# 2. On the VPS (as root), from the repo directory:
sudo DOMAIN=admin.yourdomain.com bash deploy/setup-public-admin.sh
#    ...or with nip.io:
sudo DOMAIN=admin.45-155-204-9.nip.io bash deploy/setup-public-admin.sh
#    ...or bare IP over plain HTTP (login travels unencrypted — last resort):
sudo IP_MODE=1 DOMAIN=45.155.204.9 bash deploy/setup-public-admin.sh
```

The script installs Caddy (auto-HTTPS via Let's Encrypt), refuses to proceed
with a default `ADMIN_PASSWORD`/`SECRET_KEY`, forces `ADMIN_COOKIE_SECURE=1`
and opens ports 80/443. Then open `https://admin.yourdomain.com/admin` and
log in with `ADMIN_USERNAME` + `ADMIN_PASSWORD`. A ready-made template is in
[`deploy/Caddyfile`](deploy/Caddyfile). Never expose port 9090 itself — it
must stay localhost-only.

### Option B — SSH tunnel (no domain needed)

```bash
ssh -L 9090:127.0.0.1:9090 YOUR_VPS
```

Then visit `http://127.0.0.1:9090/admin`. Do not expose this port directly to
the internet.

## 5. Updates

```bash
cd /opt/ai-store-bot
sudo -u aistore git pull --ff-only
sudo -u aistore .venv/bin/pip install -r requirements.txt
sudo -u aistore .venv/bin/alembic upgrade head
sudo systemctl restart ai-store-bot
journalctl -u ai-store-bot -n 100 --no-pager
```

Back up PostgreSQL before migrations and retain encrypted off-server copies.

## 6. Nightly database backups

Purchases live only in PostgreSQL — without backups a disk failure loses
money records and delivered goods with no way to recover. The repo ships
`scripts/backup_postgres.sh` (works with both docker-compose and native
Postgres, keeps the last 14 dumps in `./backups/`):

```bash
cp scripts/backup_postgres.sh /opt/ai-store-bot/scripts/  # if deployed elsewhere
crontab -e
# add:
30 3 * * * /opt/ai-store-bot/scripts/backup_postgres.sh >> /opt/ai-store-bot/logs/backup.log 2>&1
```

Verify the first dump the next morning (`ls -lh backups/`) and test a
restore on a scratch database at least once — an untested backup is not
a backup. Off-server copies (S3, another host) are strongly recommended.

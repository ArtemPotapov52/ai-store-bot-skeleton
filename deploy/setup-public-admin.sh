#!/usr/bin/env bash
# Publish the bot admin panel on https://<domain>/admin behind Caddy (auto-HTTPS).
#
# Usage (as root, on the VPS):
#   DOMAIN=admin.example.com bash deploy/setup-public-admin.sh
#
# No domain? Two options (pick one):
#   a) Free trusted HTTPS without buying anything: use nip.io, which resolves
#      to your IP automatically. If your VPS IP is 45.155.204.9, run with:
#        DOMAIN=admin.45-155-204-9.nip.io bash deploy/setup-public-admin.sh
#      (dashes instead of dots). Caddy will issue a real Let's Encrypt cert.
#   b) Bare IP over plain HTTP (works, but login/password travel unencrypted):
#        DOMAIN=45.155.204.9 IP_MODE=1 bash deploy/setup-public-admin.sh
#      Prefer (a) or the SSH tunnel unless you accept the risk.
#
# Prerequisites:
#   1. DNS: a real A-record, or a nip.io name (no setup needed).
#   2. The bot is already installed in /opt/ai-store-bot with a strong
#      ADMIN_PASSWORD and a non-default SECRET_KEY in its .env.
#
# What it does:
#   - installs Caddy from the official repo (idempotent),
#   - proxies https://<domain> to the bot's localhost admin (127.0.0.1:9090),
#   - forces ADMIN_COOKIE_SECURE=1 and restarts the bot,
#   - opens 80/443 in ufw (port 9090 itself stays localhost-only).
set -euo pipefail

DOMAIN="${DOMAIN:?Set DOMAIN, e.g. DOMAIN=admin.example.com bash $0}"
APP_DIR="${APP_DIR:-/opt/ai-store-bot}"
ENV_FILE="$APP_DIR/.env"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo)." >&2
  exit 1
fi

# --- 0. Detect bare-IP mode (plain HTTP, no certificate possible). ---
IP_MODE="${IP_MODE:-0}"
if [[ "$DOMAIN" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  if [ "$IP_MODE" != "1" ]; then
    dashed="${DOMAIN//./-}"
    echo "DOMAIN looks like a bare IP. Two choices:" >&2
    echo "  1) Free trusted HTTPS: DOMAIN=admin.$dashed.nip.io bash $0" >&2
    echo "  2) Plain HTTP on the IP itself: IP_MODE=1 DOMAIN=$DOMAIN bash $0" >&2
    exit 1
  fi
  if [ "${I_ACCEPT_PLAINTEXT_HTTP:-0}" != "1" ]; then
    echo "Refusing plain-HTTP admin without explicit acknowledgement." >&2
    echo "Re-run with I_ACCEPT_PLAINTEXT_HTTP=1, or better use nip.io + HTTPS:" >&2
    echo "  DOMAIN=admin.${DOMAIN//./-}.nip.io bash $0" >&2
    exit 1
  fi
  SITE_ADDRESS="http://$DOMAIN"
  echo "WARNING: plain HTTP — login and password travel unencrypted." >&2
  echo "Migrate to nip.io + HTTPS as soon as possible." >&2
else
  SITE_ADDRESS="$DOMAIN"
fi

# --- 0b. Refuse weak credentials: the panel is about to face the internet. ---
# Strip optional surrounding quotes: dotenv removes them, so "admin" == admin.
admin_pw=$(grep -E '^ADMIN_PASSWORD=' "$ENV_FILE" | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true)
secret=$(grep -E '^SECRET_KEY=' "$ENV_FILE" | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true)
if [ "$admin_pw" = "admin" ] || [ -z "$admin_pw" ]; then
  echo "ADMIN_PASSWORD is default/empty in $ENV_FILE — set a strong one first." >&2
  exit 1
fi
if [ "$secret" = "change-me-in-production" ] || [ -z "$secret" ]; then
  echo "SECRET_KEY is default/empty in $ENV_FILE — generate one first:" >&2
  echo '  python3 -c "import secrets; print(secrets.token_hex(32))"' >&2
  exit 1
fi

# --- 1. DNS sanity check (warn only; propagation may lag). ---
resolved="$(getent hosts "$DOMAIN" | awk '{print $1}' | head -1 || true)"
if [ -z "$resolved" ]; then
  echo "WARNING: $DOMAIN does not resolve yet — create the A-record, then re-run if Caddy fails to get a certificate." >&2
else
  echo "DNS: $DOMAIN -> $resolved"
fi

# --- 2. Install Caddy (idempotent). ---
if ! command -v caddy >/dev/null 2>&1; then
  apt update
  apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
  apt update
  apt install -y caddy
fi

# --- 3. Site block (backup existing Caddyfile once). ---
if [ -f /etc/caddy/Caddyfile ] && [ ! -f /etc/caddy/Caddyfile.bak-by-bot ]; then
  cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-by-bot
fi
if grep -q "reverse_proxy 127.0.0.1:9090" /etc/caddy/Caddyfile 2>/dev/null; then
  echo "Caddy already proxies to the bot admin — leaving Caddyfile as is."
else
  {
    echo ""
    echo "# My Store bot admin ($DOMAIN)"
    echo "$SITE_ADDRESS {"
    echo "	reverse_proxy 127.0.0.1:9090"
    echo "}"
  } >> /etc/caddy/Caddyfile
fi
caddy fmt --overwrite /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl reload caddy

# --- 4. Harden: 80/443 open, cookies Secure, bot restarted. ---
if command -v ufw >/dev/null 2>&1; then
  ufw allow OpenSSH >/dev/null
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
  # NOTE: enabling ufw for the first time can drop existing SSH sessions on
  # non-standard setups — enable only if it is currently inactive AND you
  # are sure SSH is on port 22.
  if ! ufw status | grep -q "Status: active"; then
    echo "ufw is inactive — NOT enabling automatically (do it manually once SSH access is confirmed)."
  fi
fi

if grep -qE '^ADMIN_COOKIE_SECURE=' "$ENV_FILE"; then
  sed -i 's/^ADMIN_COOKIE_SECURE=.*/ADMIN_COOKIE_SECURE=1/' "$ENV_FILE"
else
  printf '\nADMIN_COOKIE_SECURE=1\n' >> "$ENV_FILE"
fi
systemctl restart ai-store-bot

if [ "$IP_MODE" = "1" ]; then
  echo "Done. Open http://$DOMAIN:80/admin and log in with ADMIN_USERNAME + ADMIN_PASSWORD."
else
  echo "Done. Open https://$DOMAIN/admin and log in with ADMIN_USERNAME + ADMIN_PASSWORD."
fi
echo "Failed logins are rate-limited (5 per 15 min per IP) and recorded in the audit log."

#!/usr/bin/env bash
# Nightly PostgreSQL backup for the shop bot.
#
# Usage (cron, daily at 03:30):
#   30 3 * * * /path/to/shopbot/scripts/backup_postgres.sh >> /path/to/shopbot/logs/backup.log 2>&1
#
# Reads connection settings from the repo .env (same names as docker-compose).
# Keeps the last 14 dumps in ./backups (next to this repo).
set -euo pipefail
# Dumps hold the whole customer DB (balances, delivered goods, hashes).
umask 077

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$REPO_DIR/backups}"
KEEP="${BACKUP_KEEP:-14}"

set -a
# shellcheck disable=SC1091
. "$REPO_DIR/.env"
set +a

STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/shopbot-$STAMP.dump"

export PGPASSWORD="${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is not set in .env}"

if command -v docker >/dev/null 2>&1 && docker compose -f "$REPO_DIR/docker-compose.yml" ps db >/dev/null 2>&1; then
    docker compose -f "$REPO_DIR/docker-compose.yml" exec -T db \
        pg_dump -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -Fc > "$OUT"
else
    pg_dump -h "${POSTGRES_HOST:-127.0.0.1}" -p "${DB_PORT:-5432}" \
        -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" -Fc > "$OUT"
fi

chmod 600 "$OUT"
ls -1t "$BACKUP_DIR"/shopbot-*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "$(date -Is) backup OK: $OUT ($(du -h "$OUT" | cut -f1))"

# Restore example (stops the bot first!):
#   pg_restore -h 127.0.0.1 -U shopbot_local_user -d shopbot_local --clean backups/shopbot-<stamp>.dump

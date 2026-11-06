#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${RUSA_DEPLOY_ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE_FILE="${RUSA_DEPLOY_COMPOSE_FILE:-$ROOT_DIR/docker-compose.yml}"
BACKUP_FILE="${ENV_FILE}.previous"
CURRENT_FILE="${ENV_FILE}.rollback-current"
LOCK_FILE="$ROOT_DIR/.deploy.lock"

[[ -s "$ENV_FILE" ]] || { echo "Current production .env is missing." >&2; exit 1; }
[[ -s "$BACKUP_FILE" ]] || { echo "No previous production environment snapshot exists." >&2; exit 1; }

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another deployment or rollback is already running." >&2; exit 1; }

compose() {
  docker compose --env-file "$ENV_FILE" --file "$COMPOSE_FILE" "$@"
}

health_check() {
  local port
  port="$(awk -F= '$1 == "RUSA_PORT" { value=$2 } END { print value }' "$ENV_FILE")"
  port="${port:-8000}"
  curl --retry 12 --retry-delay 5 --retry-connrefused --fail --silent --show-error \
    "http://127.0.0.1:$port/health/live" >/dev/null
}

cp -p "$ENV_FILE" "$CURRENT_FILE"
cp -p "$BACKUP_FILE" "$ENV_FILE"

if compose pull && compose up -d --no-build --remove-orphans --wait --wait-timeout 180 && health_check; then
  mv "$CURRENT_FILE" "$BACKUP_FILE"
  chmod 600 "$ENV_FILE" "$BACKUP_FILE"
  echo "Rollback completed. The environment that was rolled back from is retained as the next rollback snapshot."
  exit 0
fi

echo "Rollback target failed health checks; restoring the previously active configuration." >&2
cp -p "$CURRENT_FILE" "$ENV_FILE"
rm -f "$CURRENT_FILE"
compose pull || true
compose up -d --no-build --remove-orphans --wait --wait-timeout 180 || true
exit 2

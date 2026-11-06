#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${RUSA_DEPLOY_ENV_FILE:-$ROOT_DIR/.env}"
COMPOSE_FILE="${RUSA_DEPLOY_COMPOSE_FILE:-$ROOT_DIR/docker-compose.yml}"
API_IMAGE="${1:?Usage: compose-deploy.sh <api-image@sha256:digest> <agent-image@sha256:digest>}"
AGENT_IMAGE="${2:?Usage: compose-deploy.sh <api-image@sha256:digest> <agent-image@sha256:digest>}"
BACKUP_FILE="${ENV_FILE}.previous"
LOCK_FILE="$ROOT_DIR/.deploy.lock"

[[ "$API_IMAGE" =~ ^ghcr\.io/[a-z0-9._-]+/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || {
  echo "API image must be an immutable GHCR sha256 digest reference." >&2
  exit 1
}
[[ "$AGENT_IMAGE" =~ ^ghcr\.io/[a-z0-9._-]+/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || {
  echo "Agent image must be an immutable GHCR sha256 digest reference." >&2
  exit 1
}
[[ -s "$ENV_FILE" ]] || {
  echo "Production .env file is missing or empty: $ENV_FILE" >&2
  exit 1
}
[[ -f "$COMPOSE_FILE" ]] || {
  echo "Compose file is missing: $COMPOSE_FILE" >&2
  exit 1
}

# A first deployment must establish a known-good digest baseline before this
# automatic rollback mechanism can be used safely.
grep -Eq '^RUSA_API_IMAGE=ghcr\.io/.+@sha256:[a-f0-9]{64}$' "$ENV_FILE" || {
  echo "Seed RUSA_API_IMAGE with the currently running GHCR digest before first automated deployment." >&2
  exit 1
}
grep -Eq '^RUSA_AGENT_IMAGE=ghcr\.io/.+@sha256:[a-f0-9]{64}$' "$ENV_FILE" || {
  echo "Seed RUSA_AGENT_IMAGE with the currently running GHCR digest before first automated deployment." >&2
  exit 1
}

exec 9>"$LOCK_FILE"
flock -n 9 || {
  echo "Another deployment or rollback is already running." >&2
  exit 1
}

compose() {
  docker compose --env-file "$ENV_FILE" --file "$COMPOSE_FILE" "$@"
}

set_env_value() {
  local key="$1"
  local value="$2"
  local temporary
  temporary="$(mktemp "${ENV_FILE}.XXXXXX")"
  awk -v key="$key" 'index($0, key "=") != 1 { print }' "$ENV_FILE" > "$temporary"
  printf '%s=%s\n' "$key" "$value" >> "$temporary"
  chmod 600 "$temporary"
  mv "$temporary" "$ENV_FILE"
}

health_check() {
  local port agent_port
  port="$(awk -F= '$1 == "RUSA_PORT" { value=$2 } END { print value }' "$ENV_FILE")"
  agent_port="$(awk -F= '$1 == "RUSA_AGENT_PORT" { value=$2 } END { print value }' "$ENV_FILE")"
  port="${port:-8000}"
  agent_port="${agent_port:-8001}"

  # Liveness alone is insufficient: verify that the model is loaded and the
  # tenant-scoped agent API can reach its initialized PostgreSQL schema.
  curl --retry 12 --retry-delay 5 --retry-connrefused --fail --silent --show-error \
    "http://127.0.0.1:$port/health/ready" >/dev/null
  curl --retry 12 --retry-delay 5 --retry-connrefused --fail --silent --show-error \
    "http://127.0.0.1:$agent_port/health/ready" >/dev/null
}

rollback_on_error() {
  local original_status="$?"
  trap - ERR
  echo "Deployment failed. Restoring the previous image digests and restarting the last known-good stack." >&2

  if [[ ! -s "$BACKUP_FILE" ]]; then
    echo "No previous environment snapshot exists; automatic rollback is unavailable." >&2
    exit 2
  fi

  cp -p "$BACKUP_FILE" "$ENV_FILE"
  if compose pull && compose up -d --no-build --remove-orphans --wait --wait-timeout 180 && health_check; then
    echo "Automatic rollback completed successfully." >&2
    exit "$original_status"
  fi

  echo "CRITICAL: automatic rollback did not become healthy. Operator intervention is required." >&2
  exit 2
}

cp -p "$ENV_FILE" "$BACKUP_FILE"
chmod 600 "$BACKUP_FILE"
trap rollback_on_error ERR

set_env_value RUSA_API_IMAGE "$API_IMAGE"
set_env_value RUSA_AGENT_IMAGE "$AGENT_IMAGE"

compose config --quiet
compose pull
compose up -d --no-build --remove-orphans --wait --wait-timeout 180
health_check

trap - ERR
echo "Deployment is healthy. Previous configuration retained at $BACKUP_FILE for manual rollback."

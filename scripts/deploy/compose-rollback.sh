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

IMAGE_REGISTRY="${RUSA_IMAGE_REGISTRY:-ghcr.io}"
[[ "$IMAGE_REGISTRY" =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]{1,5})?$ ]] || {
  echo "RUSA_IMAGE_REGISTRY must be a registry hostname with an optional port." >&2
  exit 1
}
is_immutable_image() {
  local reference="$1"
  [[ "$reference" == "$IMAGE_REGISTRY/"* ]] || return 1
  local image_path="${reference#"$IMAGE_REGISTRY/"}"
  [[ "$image_path" =~ ^[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}$ ]]
}
for file in "$ENV_FILE" "$BACKUP_FILE"; do
  for key in RUSA_API_IMAGE RUSA_AGENT_IMAGE; do
    reference="$(awk -F= -v key="$key" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$file")"
    is_immutable_image "$reference" || {
      echo "$key in $file must be an immutable sha256 digest from $IMAGE_REGISTRY." >&2
      exit 1
    }
  done
done

exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another deployment or rollback is already running." >&2; exit 1; }

compose() {
  docker compose --env-file "$ENV_FILE" --file "$COMPOSE_FILE" "$@"
}

wait_for_migration() {
  local services migration_id state exit_code attempt
  services="$(compose config --services)"
  if ! grep -Fxq "rusa-agent-migrate" <<< "$services"; then
    return 0
  fi

  for ((attempt = 0; attempt < 180; attempt++)); do
    migration_id="$(compose ps --all --quiet rusa-agent-migrate)"
    if [[ -n "$migration_id" ]]; then
      state="$(docker inspect --format '{{.State.Status}}' "$migration_id")"
      if [[ "$state" == "exited" ]]; then
        exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$migration_id")"
        if [[ "$exit_code" == "0" ]]; then
          return 0
        fi
        echo "PostgreSQL migration service exited with status $exit_code." >&2
        compose logs --tail 100 rusa-agent-migrate >&2 || true
        return 1
      fi
      if [[ "$state" == "dead" ]]; then
        echo "PostgreSQL migration service entered the dead state." >&2
        compose logs --tail 100 rusa-agent-migrate >&2 || true
        return 1
      fi
    fi
    sleep 1
  done

  echo "Timed out waiting for the PostgreSQL migration service to complete." >&2
  compose logs --tail 100 rusa-agent-migrate >&2 || true
  return 1
}

health_check() {
  local port agent_port retries retry_delay
  port="$(awk -F= '$1 == "RUSA_PORT" { value=$2 } END { print value }' "$ENV_FILE")"
  agent_port="$(awk -F= '$1 == "RUSA_AGENT_PORT" { value=$2 } END { print value }' "$ENV_FILE")"
  port="${port:-8000}"
  agent_port="${agent_port:-8001}"
  retries="${RUSA_DEPLOY_HEALTH_RETRIES:-12}"
  retry_delay="${RUSA_DEPLOY_HEALTH_RETRY_DELAY:-5}"
  [[ "$retries" =~ ^[0-9]{1,2}$ && "$retry_delay" =~ ^[0-9]{1,2}$ ]] || {
    echo "Deployment health retry settings must be small non-negative integers." >&2
    return 1
  }

  # Liveness alone is insufficient: verify that the model is loaded and the
  # tenant-scoped agent API can reach its initialized PostgreSQL schema.
  curl --retry "$retries" --retry-delay "$retry_delay" --retry-connrefused --fail --silent --show-error \
    "http://127.0.0.1:$port/health/ready" >/dev/null
  curl --retry "$retries" --retry-delay "$retry_delay" --retry-connrefused --fail --silent --show-error \
    "http://127.0.0.1:$agent_port/health/ready" >/dev/null
}

cp -p "$ENV_FILE" "$CURRENT_FILE"
cp -p "$BACKUP_FILE" "$ENV_FILE"

if compose pull && compose up -d --no-build --remove-orphans && wait_for_migration && health_check; then
  mv "$CURRENT_FILE" "$BACKUP_FILE"
  chmod 600 "$ENV_FILE" "$BACKUP_FILE"
  echo "Rollback completed. The environment that was rolled back from is retained as the next rollback snapshot."
  exit 0
fi

echo "Rollback target failed health checks; restoring the previously active configuration." >&2
cp -p "$CURRENT_FILE" "$ENV_FILE"
rm -f "$CURRENT_FILE"
compose pull || true
compose up -d --no-build --remove-orphans || true
wait_for_migration || true
health_check || true
exit 2

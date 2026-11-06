#!/usr/bin/env bash
set -Eeuo pipefail

candidate="${1:?Candidate image SHA is required}"
previous="${2:?Previous baseline image SHA is required}"
project="${COMPOSE_PROJECT_NAME:-rusa-release-compatibility}"
api="http://127.0.0.1:${RUSA_PORT:-8000}"
agent="http://127.0.0.1:${RUSA_AGENT_PORT:-8001}"
tenant_key="$(jq -er '."tenant-a"' <<< "${RUSA_AGENT_TENANT_KEYS:?RUSA_AGENT_TENANT_KEYS is required}")"

compose() {
  docker compose --project-name "$project" --file docker-compose.production.yml "$@"
}

cleanup() {
  local result="$?"
  trap - EXIT
  if (( result != 0 )); then
    compose ps --all || true
    compose logs --tail 100 || true
  fi
  compose down --volumes --remove-orphans || true
  exit "$result"
}
trap cleanup EXIT

assert_image() {
  local service="$1" image="$2" container_id
  container_id="$(compose ps --quiet "$service")"
  test -n "$container_id"
  test "$(docker inspect --format '{{.Image}}' "$container_id")" = \
    "$(docker image inspect --format '{{.Id}}' "$image")"
}

deploy() {
  local revision="$1"
  export RUSA_API_IMAGE="rusa-ai:$revision"
  export RUSA_AGENT_IMAGE="rusa-agent-runtime:$revision"
  echo "Starting production Compose stack for revision $revision"
  compose up --detach --no-build --pull never

  # The one-shot migration must complete successfully before workers can start.
  local migration_id migration_state
  migration_id="$(compose ps --all --quiet rusa-agent-migrate)"
  test -n "$migration_id"
  for attempt in {1..180}; do
    migration_state="$(docker inspect --format '{{.State.Status}}' "$migration_id")"
    [[ "$migration_state" == "exited" ]] && break
    sleep 1
  done
  test "$migration_state" = "exited"
  test "$(docker inspect --format '{{.State.ExitCode}}' "$migration_id")" = "0"
  test "$(docker inspect --format '{{.Image}}' "$migration_id")" = \
    "$(docker image inspect --format '{{.Id}}' "$RUSA_AGENT_IMAGE")"

  # Wait for readiness rather than treating container creation as success.
  local api_ready=false agent_ready=false
  for attempt in {1..180}; do
    if curl --fail --silent --show-error "$api/health/live" >/dev/null; then
      api_ready=true
      break
    fi
    sleep 1
  done
  test "$api_ready" = "true"

  for attempt in {1..180}; do
    if curl --fail --silent --show-error "$agent/health/ready" | jq -e '.status == "ready"' >/dev/null; then
      agent_ready=true
      break
    fi
    sleep 1
  done
  test "$agent_ready" = "true"

  assert_image rusa-api "$RUSA_API_IMAGE"
  assert_image rusa-agent-api "$RUSA_AGENT_IMAGE"
  assert_image rusa-agent-worker "$RUSA_AGENT_IMAGE"
}

sql() {
  compose exec -T rusa-agent-db psql -X -v ON_ERROR_STOP=1 \
    --username rusa_agent_owner --dbname rusa_agent -At "$@"
}

seed_task() {
  local task_id="$1"
  sql -v "task_id=$task_id" <<'SQL'
INSERT INTO rusa_agent_tasks (tenant_id, task_id, idempotency_key, status, payload, result)
VALUES ('tenant-a', :'task_id', :'task_id', 'succeeded', '{}'::jsonb, '{"smoke":true}'::jsonb);
SQL
}

read_task() {
  local task_id="$1"
  curl --retry 10 --retry-delay 1 --retry-connrefused --fail --silent --show-error \
    --header "X-Tenant-ID: tenant-a" --header "X-API-Key: $tenant_key" \
    "$agent/v1/agent/tasks/$task_id" |
    jq -e --arg id "$task_id" '.task_id == $id and .status == "succeeded" and .result.smoke == true'
}

# Use the previous branch image as the rollback baseline, then upgrade both
# application services while keeping the exact same PostgreSQL container.
deploy "$previous"
db_container="$(compose ps --quiet rusa-agent-db)"
sql -c 'SHOW server_version'
seed_task before-upgrade
read_task before-upgrade

deploy "$candidate"
test "$(compose ps --quiet rusa-agent-db)" = "$db_container"
read_task before-upgrade
seed_task after-upgrade
read_task after-upgrade

auth_status="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
  --header 'Content-Type: application/json' \
  --data '{"model":"rusa-ai-v1","messages":[{"role":"user","content":"health probe"}]}' \
  "$api/v1/chat/completions")"
test "$auth_status" = "401"

# Roll back both application images and prove records written by both versions
# remain readable through the previous API/worker pair on the same database.
deploy "$previous"
test "$(compose ps --quiet rusa-agent-db)" = "$db_container"
read_task before-upgrade
read_task after-upgrade
echo "Application compatibility drill passed: previous -> candidate -> previous; PostgreSQL container and both task records remained intact."

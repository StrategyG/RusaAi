#!/usr/bin/env bash
set -euo pipefail

candidate="${1:?Candidate SHA required}"
previous="${2:?Rollback baseline SHA required}"
api="http://127.0.0.1:${RUSA_PORT:-8000}"
agent="http://127.0.0.1:${RUSA_AGENT_PORT:-8001}"
tenant_key="$(jq -er '."tenant-a"' <<< "$RUSA_AGENT_TENANT_KEYS")"

compose() {
  docker compose --project-name rusa-release-smoke --file docker-compose.production.yml "$@"
}

cleanup() {
  local result=$?
  if (( result != 0 )); then
    compose ps --all || true
    compose logs --tail 100 || true
  fi
  compose down --volumes --remove-orphans
}
trap cleanup EXIT

assert_image() {
  local service="$1" image="$2"
  test "$(docker inspect --format '{{.Image}}' "$(compose ps --quiet "$service")")" = \
    "$(docker image inspect --format '{{.Id}}' "$image")"
}

deploy() {
  export RUSA_API_IMAGE="rusa-ai:$1"
  export RUSA_AGENT_IMAGE="rusa-agent-runtime:$1"
  echo "Starting Compose stack for $1"
  compose up --detach --no-build --pull never --wait --wait-timeout 180
  assert_image rusa-api "$RUSA_API_IMAGE"
  assert_image rusa-agent-api "$RUSA_AGENT_IMAGE"
  assert_image rusa-agent-worker "$RUSA_AGENT_IMAGE"
  curl --fail --silent --show-error "$api/health/live" >/dev/null
  curl --fail --silent --show-error "$agent/health/ready" | jq -e '.status == "ready"'
}

sql() {
  compose exec -T rusa-agent-db psql -X -v ON_ERROR_STOP=1 \
    --username rusa_agent_owner --dbname rusa_agent -At "$@"
}

seed_task() {
  # A terminal task exercises persistent data without calling the external provider.
  sql -v "task_id=$1" <<'SQL'
INSERT INTO rusa_agent_tasks (tenant_id, task_id, idempotency_key, status, payload, result)
VALUES ('tenant-a', :'task_id', :'task_id', 'succeeded', '{}', '{"smoke":true}');
SQL
}

read_task() {
  curl --fail --silent --show-error \
    --header "X-Tenant-ID: tenant-a" --header "X-API-Key: $tenant_key" \
    "$agent/v1/agent/tasks/$1" |
    jq -e --arg id "$1" '.task_id == $id and .status == "succeeded" and .result.smoke == true'
}

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
test "$auth_status" = 401

deploy "$previous"
test "$(compose ps --quiet rusa-agent-db)" = "$db_container"
read_task before-upgrade
read_task after-upgrade
echo "Rollback restored both baseline images and read both task records on the same PostgreSQL instance."

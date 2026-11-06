#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

AGENT_IMAGE_TAG="${1:?Pass the built agent runtime image tag as the first argument}"
REGISTRY_HOST="127.0.0.1:5000"
REGISTRY_IMAGE="registry:2.8.3@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
REGISTRY_NAME="rusa-ci-registry-$$"
PROJECT_NAME="rusa-rollback-ci-$$"
TMP_ROOT="$(mktemp -d)"
ENV_FILE="$TMP_ROOT/.env"
COMPOSE_FILE="$ROOT_DIR/docker-compose.production.yml"
API_KEY="ci-rollback-api-key-123456789012345678901234567890"
DB_PASSWORD="ciownerpassword123456789"
WORKER_PASSWORD="ciworkerpassword123456789"
API_DB_PASSWORD="ciapassword123456789"
SENTINEL="rollback-preserved-$$"

compose() {
  docker compose --env-file "$ENV_FILE" --file "$COMPOSE_FILE" "$@"
}

cleanup() {
  local status="$?"
  trap - EXIT
  if [[ -f "$ENV_FILE" ]]; then
    compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
  docker rm -f "$REGISTRY_NAME" >/dev/null 2>&1 || true
  rm -rf "$TMP_ROOT"
  exit "$status"
}
trap cleanup EXIT

docker pull "$REGISTRY_IMAGE"
docker run --detach --name "$REGISTRY_NAME" --publish 127.0.0.1:5000:5000 "$REGISTRY_IMAGE" >/dev/null
curl --retry 30 --retry-delay 1 --retry-connrefused --fail --silent --show-error \
  http://127.0.0.1:5000/v2/ >/dev/null

build_and_push_api() {
  local tag="$1"
  local ready_status="$2"
  local release_id="$3"
  local image="$REGISTRY_HOST/rusa-api:$tag"
  docker build --pull --file scripts/testing/Dockerfile.health-stub \
    --build-arg "READY_STATUS=$ready_status" \
    --build-arg "RELEASE_ID=$release_id" \
    --tag "$image" . >&2
  docker push "$image" >&2
  docker image inspect --format='{{index .RepoDigests 0}}' "$image"
}

PREVIOUS_API_IMAGE="$(build_and_push_api previous 200 previous)"
CANDIDATE_API_IMAGE="$(build_and_push_api candidate 200 candidate)"
UNHEALTHY_API_IMAGE="$(build_and_push_api unhealthy 503 unhealthy)"

if ! docker image inspect "$AGENT_IMAGE_TAG" >/dev/null 2>&1; then
  docker pull "$AGENT_IMAGE_TAG"
fi
build_and_push_agent() {
  local tag="$1"
  local release_id="$2"
  local image="$REGISTRY_HOST/rusa-agent-runtime:$tag"
  docker build --file scripts/testing/Dockerfile.agent-label \
    --build-arg "BASE_IMAGE=$AGENT_IMAGE_TAG" \
    --build-arg "RELEASE_ID=$release_id" \
    --tag "$image" . >&2
  docker push "$image" >&2
  docker image inspect --format='{{index .RepoDigests 0}}' "$image"
}

PREVIOUS_AGENT_IMAGE="$(build_and_push_agent previous previous)"
CANDIDATE_AGENT_IMAGE="$(build_and_push_agent candidate candidate)"

cat > "$ENV_FILE" <<EOF
RUSA_API_IMAGE=$PREVIOUS_API_IMAGE
RUSA_AGENT_IMAGE=$PREVIOUS_AGENT_IMAGE
RUSA_API_KEY=$API_KEY
RUSA_MODEL_DIR=$ROOT_DIR
RUSA_PORT=18080
RUSA_AGENT_PORT=18081
RUSA_AGENT_DB_PASSWORD=$DB_PASSWORD
RUSA_AGENT_WORKER_DB_PASSWORD=$WORKER_PASSWORD
RUSA_AGENT_API_DB_PASSWORD=$API_DB_PASSWORD
RUSA_AGENT_TENANT_KEYS='{"tenant-a":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
RUSA_AGENT_TENANT_REPOSITORIES='{"tenant-a":["StrategyG/RusaAi"]}'
RUSA_AGENT_PROVIDER_BASE_URL=https://provider.example.invalid/v1
RUSA_AGENT_PROVIDER_API_KEY=ci-provider-placeholder
RUSA_AGENT_MODEL=ci-model
RUSA_AGENT_GITHUB_TOKEN=ci-github-placeholder
RUSA_AGENT_AUDIT_KEY=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
EOF
chmod 0600 "$ENV_FILE"

export RUSA_DEPLOY_ENV_FILE="$ENV_FILE"
export RUSA_DEPLOY_COMPOSE_FILE="$COMPOSE_FILE"
export RUSA_IMAGE_REGISTRY="$REGISTRY_HOST"
export RUSA_DEPLOY_HEALTH_RETRIES=2
export RUSA_DEPLOY_HEALTH_RETRY_DELAY=1
export COMPOSE_PROJECT_NAME="$PROJECT_NAME"

# Deploy a healthy candidate through the real immutable-digest deployment path.
"$ROOT_DIR/scripts/deploy/compose-deploy.sh" "$CANDIDATE_API_IMAGE" "$CANDIDATE_AGENT_IMAGE"
grep -Fxq "RUSA_API_IMAGE=$CANDIDATE_API_IMAGE" "$ENV_FILE"

# Store a sentinel row, then recreate the worker so its idempotent schema setup
# runs again against an existing database with real persisted data.
compose exec -T -e PGPASSWORD="$DB_PASSWORD" rusa-agent-db \
  psql -h 127.0.0.1 -U rusa_agent_owner -d rusa_agent -v ON_ERROR_STOP=1 \
  -c "INSERT INTO rusa_agent_memory (tenant_id, namespace, content, source, metadata, expires_at) VALUES ('rollback-ci', 'episodic', '$SENTINEL', 'rollback-drill', '{}'::jsonb, now() + interval '1 day')"
compose up --detach --force-recreate --no-deps --wait --wait-timeout 180 rusa-agent-worker
compose exec -T -e PGPASSWORD="$DB_PASSWORD" rusa-agent-db \
  psql -h 127.0.0.1 -U rusa_agent_owner -d rusa_agent -Atqc \
  "SELECT 1 FROM rusa_agent_memory WHERE tenant_id='rollback-ci' AND content='$SENTINEL'" | grep -qx 1

# Manual rollback must restore the old immutable image and preserve database data.
"$ROOT_DIR/scripts/deploy/compose-rollback.sh"
grep -Fxq "RUSA_API_IMAGE=$PREVIOUS_API_IMAGE" "$ENV_FILE"
grep -Fxq "RUSA_AGENT_IMAGE=$PREVIOUS_AGENT_IMAGE" "$ENV_FILE"
compose exec -T -e PGPASSWORD="$DB_PASSWORD" rusa-agent-db \
  psql -h 127.0.0.1 -U rusa_agent_owner -d rusa_agent -Atqc \
  "SELECT 1 FROM rusa_agent_memory WHERE tenant_id='rollback-ci' AND content='$SENTINEL'" | grep -qx 1

# An unhealthy release must fail deployment, automatically restore the known-good
# image, and leave the PostgreSQL volume and sentinel record intact.
if "$ROOT_DIR/scripts/deploy/compose-deploy.sh" "$UNHEALTHY_API_IMAGE" "$CANDIDATE_AGENT_IMAGE"; then
  echo "Unhealthy candidate unexpectedly passed deployment readiness." >&2
  exit 1
fi
grep -Fxq "RUSA_API_IMAGE=$PREVIOUS_API_IMAGE" "$ENV_FILE"
grep -Fxq "RUSA_AGENT_IMAGE=$PREVIOUS_AGENT_IMAGE" "$ENV_FILE"
compose exec -T -e PGPASSWORD="$DB_PASSWORD" rusa-agent-db \
  psql -h 127.0.0.1 -U rusa_agent_owner -d rusa_agent -Atqc \
  "SELECT 1 FROM rusa_agent_memory WHERE tenant_id='rollback-ci' AND content='$SENTINEL'" | grep -qx 1

echo "Real Compose rollback drill passed: PostgreSQL persisted data, worker schema re-initialization, manual rollback, and automatic rollback."

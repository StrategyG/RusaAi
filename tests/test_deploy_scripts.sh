#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "$TMP_ROOT"' EXIT

PROJECT="$TMP_ROOT/project"
BIN="$TMP_ROOT/bin"
mkdir -p "$PROJECT/scripts/deploy" "$PROJECT/scripts/postgres" "$BIN"
cp "$SOURCE_ROOT/scripts/deploy/compose-deploy.sh" "$PROJECT/scripts/deploy/"
cp "$SOURCE_ROOT/scripts/deploy/compose-rollback.sh" "$PROJECT/scripts/deploy/"
cat > "$PROJECT/docker-compose.yml" <<'COMPOSE'
services:
  rusa-agent-migrate:
    image: test
COMPOSE

cat > "$BIN/docker" <<'DOCKER'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$DOCKER_LOG"
case "$1" in
  compose)
    if [[ "$*" == *"config --services"* ]]; then
      echo "rusa-agent-migrate"
    elif [[ "$*" == *"ps --all --quiet rusa-agent-migrate"* ]]; then
      echo "migration-container"
    fi
    ;;
  inspect)
    if [[ "$3" == "{{.State.Status}}" ]]; then
      echo "exited"
    elif [[ "$3" == "{{.State.ExitCode}}" ]]; then
      current_api="$(awk -F= '$1 == "RUSA_API_IMAGE" { print substr($0, index($0, "=") + 1) }' "$RUSA_DEPLOY_ENV_FILE")"
      if [[ -n "${FAIL_MIGRATION_IMAGE:-}" && "$current_api" == "$FAIL_MIGRATION_IMAGE" ]]; then
        echo "1"
      else
        echo "0"
      fi
    else
      exit 1
    fi
    ;;
  *)
    exit 1
    ;;
esac
DOCKER

cat > "$BIN/curl" <<'CURL'
#!/usr/bin/env bash
set -euo pipefail
current_api="$(awk -F= '$1 == "RUSA_API_IMAGE" { print substr($0, index($0, "=") + 1) }' "$RUSA_DEPLOY_ENV_FILE")"
if [[ -n "${FAIL_API_IMAGE:-}" && "$current_api" == "$FAIL_API_IMAGE" ]]; then
  exit 22
fi
exit 0
CURL

chmod 0755 "$BIN/docker" "$BIN/curl"
export PATH="$BIN:$PATH"
export RUSA_DEPLOY_ENV_FILE="$PROJECT/.env"
export RUSA_DEPLOY_COMPOSE_FILE="$PROJECT/docker-compose.yml"
export DOCKER_LOG="$TMP_ROOT/docker.log"

old_api="ghcr.io/strategyg/rusa-ai@sha256:$(printf 'a%.0s' {1..64})"
old_agent="ghcr.io/strategyg/rusa-agent-runtime@sha256:$(printf 'b%.0s' {1..64})"
new_api="ghcr.io/strategyg/rusa-ai@sha256:$(printf 'c%.0s' {1..64})"
new_agent="ghcr.io/strategyg/rusa-agent-runtime@sha256:$(printf 'd%.0s' {1..64})"
failed_api="ghcr.io/strategyg/rusa-ai@sha256:$(printf 'e%.0s' {1..64})"
failed_agent="ghcr.io/strategyg/rusa-agent-runtime@sha256:$(printf 'f%.0s' {1..64})"
migration_failed_api="ghcr.io/strategyg/rusa-ai@sha256:$(printf '1%.0s' {1..64})"
migration_failed_agent="ghcr.io/strategyg/rusa-agent-runtime@sha256:$(printf '2%.0s' {1..64})"

cat > "$RUSA_DEPLOY_ENV_FILE" <<EOF
RUSA_API_IMAGE=$old_api
RUSA_AGENT_IMAGE=$old_agent
RUSA_PORT=18000
RUSA_AGENT_PORT=18001
EOF
chmod 0600 "$RUSA_DEPLOY_ENV_FILE"

assert_env_value() {
  local key="$1"
  local expected="$2"
  grep -Fxq "$key=$expected" "$RUSA_DEPLOY_ENV_FILE"
}

# Successful deploy records the previous known-good digest pair.
"$PROJECT/scripts/deploy/compose-deploy.sh" "$new_api" "$new_agent"
assert_env_value RUSA_API_IMAGE "$new_api"
assert_env_value RUSA_AGENT_IMAGE "$new_agent"
grep -Fxq "RUSA_API_IMAGE=$old_api" "$RUSA_DEPLOY_ENV_FILE.previous"
grep -Fxq "RUSA_AGENT_IMAGE=$old_agent" "$RUSA_DEPLOY_ENV_FILE.previous"

# Manual rollback restores the previous digest pair and rotates the snapshot.
"$PROJECT/scripts/deploy/compose-rollback.sh"
assert_env_value RUSA_API_IMAGE "$old_api"
assert_env_value RUSA_AGENT_IMAGE "$old_agent"
grep -Fxq "RUSA_API_IMAGE=$new_api" "$RUSA_DEPLOY_ENV_FILE.previous"

# A failed readiness check on the new image must automatically restore old digests.
export FAIL_API_IMAGE="$failed_api"
if "$PROJECT/scripts/deploy/compose-deploy.sh" "$failed_api" "$failed_agent"; then
  echo "Expected failed readiness to return a non-zero deployment status." >&2
  exit 1
else
  deployment_status="$?"
fi
[[ "$deployment_status" -ne 0 ]]
assert_env_value RUSA_API_IMAGE "$old_api"
assert_env_value RUSA_AGENT_IMAGE "$old_agent"

# A failed one-shot schema migration must trigger the same automatic rollback.
export FAIL_MIGRATION_IMAGE="$migration_failed_api"
if "$PROJECT/scripts/deploy/compose-deploy.sh" "$migration_failed_api" "$migration_failed_agent"; then
  echo "Expected failed migration to return a non-zero deployment status." >&2
  exit 1
else
  migration_status="$?"
fi
[[ "$migration_status" -ne 0 ]]
unset FAIL_MIGRATION_IMAGE
assert_env_value RUSA_API_IMAGE "$old_api"
assert_env_value RUSA_AGENT_IMAGE "$old_agent"

if "$PROJECT/scripts/deploy/compose-deploy.sh" "ghcr.io/strategyg/rusa-ai:latest" "$new_agent"; then
  echo "Expected mutable image tags to be rejected." >&2
  exit 1
fi

grep -q " pull$" "$DOCKER_LOG"
grep -q " up -d --no-build" "$DOCKER_LOG"
echo "Deployment script tests passed: immutable deploy, migration failure rollback, readiness failure rollback, and manual rollback."

#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
deploy_script="$repo_root/scripts/deploy/compose-deploy.sh"
rollback_script="$repo_root/scripts/deploy/compose-rollback.sh"
tmp_root="$(mktemp -d)"
trap 'rm -rf "$tmp_root"' EXIT

old_api="ghcr.io/strategyg/rusa-ai-api@sha256:$(printf 'a%.0s' {1..64})"
old_worker="ghcr.io/strategyg/rusa-ai-worker@sha256:$(printf 'a%.0s' {1..64})"
old_sandbox="ghcr.io/strategyg/rusa-ai-sandbox@sha256:$(printf 'a%.0s' {1..64})"
old_web="ghcr.io/strategyg/rusa-ai-web@sha256:$(printf 'a%.0s' {1..64})"
new_api="ghcr.io/strategyg/rusa-ai-api@sha256:$(printf 'b%.0s' {1..64})"
new_worker="ghcr.io/strategyg/rusa-ai-worker@sha256:$(printf 'b%.0s' {1..64})"
new_sandbox="ghcr.io/strategyg/rusa-ai-sandbox@sha256:$(printf 'b%.0s' {1..64})"
new_web="ghcr.io/strategyg/rusa-ai-web@sha256:$(printf 'b%.0s' {1..64})"

make_case() {
  local dir="$1"
  mkdir -p "$dir/bin"
  : > "$dir/docker-compose.production.yml"
  cat > "$dir/.env" <<EOF
RUSA_API_IMAGE=$old_api
RUSA_WORKER_IMAGE=$old_worker
RUSA_SANDBOX_IMAGE=$old_sandbox
RUSA_WEB_IMAGE=$old_web
RUSA_DOCKER_SOCKET=/run/user/1001/docker.sock
RUSA_AICODE_ENABLED=false
RUSA_API_DATABASE_URL=postgresql://api:secret@db.example/rusa?sslmode=verify-full
EOF
  cat > "$dir/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${DOCKER_HOST:-}" != "unix:///run/user/1001/docker.sock" ]]; then
  echo "Docker operation did not use the configured rootless daemon." >&2
  exit 91
fi
if [[ "${1:-}" == "login" ]]; then
  [[ "${2:-}" == "ghcr.io" ]] || exit 92
  token="$(cat)"
  [[ "$token" == "ci-test-token" ]] || exit 93
  echo "Login Succeeded"
  exit 0
fi
if [[ "${1:-}" == "inspect" ]]; then
  if [[ "${RUSA_TEST_FAIL_NEW_IMAGE:-0}" == "1" ]] && grep -q '^RUSA_API_IMAGE=.*sha256:bbbbbbbb' .env; then
    printf 'unhealthy\n'
  else
    printf 'healthy\n'
  fi
  exit 0
fi
if [[ "${1:-}" == "compose" ]]; then
  case " $* " in
    *" ps -q api "*) printf 'api-container\n' ;;
    *" ps -q worker "*) printf 'worker-container\n' ;;
    *" ps -q web "*) printf 'web-container\n' ;;
    *) : ;;
  esac
  exit 0
fi
if [[ "${1:-}" == "pull" ]]; then
  exit 0
fi
echo "Unexpected fake docker invocation: $*" >&2
exit 90
DOCKER
  chmod 0755 "$dir/bin/docker"
}

run_deploy() {
  local dir="$1" fail_new="$2" api="$3" worker="$4" sandbox="$5" web="$6"
  (
    cd "$dir"
    PATH="$dir/bin:$PATH" \
    RUSA_ENV_FILE=.env \
    RUSA_COMPOSE_FILE=docker-compose.production.yml \
    RUSA_DEPLOY_LOCK_FILE=.deploy.lock \
    RUSA_DEPLOY_HEALTH_TIMEOUT_SECONDS=60 \
    RUSA_TEST_FAIL_NEW_IMAGE="$fail_new" \
    "$deploy_script" "$api" "$worker" "$sandbox" "$web"
  )
}

success_dir="$tmp_root/success"
make_case "$success_dir"
run_deploy "$success_dir" 0 "$new_api" "$new_worker" "$new_sandbox" "$new_web"
grep -Fx "RUSA_API_IMAGE=$new_api" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_WORKER_IMAGE=$new_worker" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_SANDBOX_IMAGE=$new_sandbox" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_WEB_IMAGE=$new_web" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_API_IMAGE=$old_api" "$success_dir/.env.previous" >/dev/null
grep -Fx "RUSA_WEB_IMAGE=$old_web" "$success_dir/.env.previous" >/dev/null
echo "PASS: immutable deployment updates all four images and retains rollback snapshot"

(
  cd "$success_dir"
  PATH="$success_dir/bin:$PATH" \
  RUSA_ENV_FILE=.env \
  RUSA_DEPLOY_LOCK_FILE=.deploy.lock \
  RUSA_DEPLOY_HEALTH_TIMEOUT_SECONDS=60 \
  "$rollback_script"
)
grep -Fx "RUSA_API_IMAGE=$old_api" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_WORKER_IMAGE=$old_worker" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_SANDBOX_IMAGE=$old_sandbox" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_WEB_IMAGE=$old_web" "$success_dir/.env" >/dev/null
echo "PASS: manual rollback restores the previous immutable image set"

failure_dir="$tmp_root/failure"
make_case "$failure_dir"
if run_deploy "$failure_dir" 1 "$new_api" "$new_worker" "$new_sandbox" "$new_web"; then
  echo "Expected the failed candidate deployment to return non-zero after rollback." >&2
  exit 1
fi
grep -Fx "RUSA_API_IMAGE=$old_api" "$failure_dir/.env" >/dev/null
grep -Fx "RUSA_WORKER_IMAGE=$old_worker" "$failure_dir/.env" >/dev/null
grep -Fx "RUSA_SANDBOX_IMAGE=$old_sandbox" "$failure_dir/.env" >/dev/null
grep -Fx "RUSA_WEB_IMAGE=$old_web" "$failure_dir/.env" >/dev/null
echo "PASS: unhealthy candidate automatically restores the previous healthy image set"

enabled_dir="$tmp_root/enabled"
make_case "$enabled_dir"
sed -i 's/^RUSA_AICODE_ENABLED=false$/RUSA_AICODE_ENABLED=true/' "$enabled_dir/.env"
if run_deploy "$enabled_dir" 0 "$new_api" "$new_worker" "$new_sandbox" "$new_web"; then
  echo "Deployment must be rejected while AICode intake is enabled." >&2
  exit 1
fi
grep -Fx "RUSA_API_IMAGE=$old_api" "$enabled_dir/.env" >/dev/null
echo "PASS: deployment is blocked until AICode intake is disabled"

if run_deploy "$failure_dir" 0 "ghcr.io/strategyg/rusa-ai-api:latest" "$new_worker" "$new_sandbox" "$new_web"; then
  echo "Mutable image tags must be rejected." >&2
  exit 1
fi
echo "PASS: mutable image tags are rejected before deployment"

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
old_mcp="ghcr.io/strategyg/rusa-ai-rag-mcp@sha256:$(printf 'a%.0s' {1..64})"
old_qa_sandbox="$old_sandbox"
new_api="ghcr.io/strategyg/rusa-ai-api@sha256:$(printf 'b%.0s' {1..64})"
new_worker="ghcr.io/strategyg/rusa-ai-worker@sha256:$(printf 'b%.0s' {1..64})"
new_sandbox="ghcr.io/strategyg/rusa-ai-sandbox@sha256:$(printf 'b%.0s' {1..64})"
new_mcp="ghcr.io/strategyg/rusa-ai-rag-mcp@sha256:$(printf 'b%.0s' {1..64})"
new_qa_sandbox="$new_sandbox"

make_case() {
  local dir="$1"
  mkdir -p "$dir/bin"
  : > "$dir/docker-compose.production.yml"
  cat > "$dir/.env" <<EOF
RUSA_API_IMAGE=$old_api
RUSA_WORKER_IMAGE=$old_worker
RUSA_SANDBOX_IMAGE=$old_sandbox
RUSA_MCP_IMAGE=$old_mcp
RUSA_QA_SANDBOX_IMAGE=$old_qa_sandbox
RUSA_DOCKER_SOCKET=/run/user/1001/docker.sock
RUSA_QA_DOCKER_SOCKET=/run/user/1002/docker.sock
RUSA_QA_DOCKER_GID=1002
RUSA_WORKSPACE_ROOT=/var/lib/rusa/workspaces
RUSA_QA_WORKSPACE_ROOT=/var/lib/rusa/qa-workspaces
RUSA_QA_WORKSPACE_GID=1002
RUSA_AICODE_ENABLED=false
RUSA_RAG_ENABLED=false
RUSA_API_DATABASE_URL=postgresql://api:secret@db.example/rusa?sslmode=verify-full
EOF
  cat > "$dir/bin/docker" <<'DOCKER'
#!/usr/bin/env bash
set -euo pipefail
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
    *" ps -q qa "*) printf 'qa-container\n' ;;
    *" ps -q rusa-mcp "*) printf 'mcp-container\n' ;;
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
  local dir="$1" fail_new="$2" api="$3" worker="$4" sandbox="$5" mcp="$6"
  (
    cd "$dir"
    PATH="$dir/bin:$PATH" \
    RUSA_ENV_FILE=.env \
    RUSA_COMPOSE_FILE=docker-compose.production.yml \
    RUSA_RAG_COMPOSE_FILE=docker-compose.production.rag-mcp.yml \
    RUSA_DEPLOY_LOCK_FILE=.deploy.lock \
    RUSA_DEPLOY_HEALTH_TIMEOUT_SECONDS=60 \
    RUSA_TEST_FAIL_NEW_IMAGE="$fail_new" \
    "$deploy_script" "$api" "$worker" "$sandbox" "$mcp"
  )
}

success_dir="$tmp_root/success"
make_case "$success_dir"
run_deploy "$success_dir" 0 "$new_api" "$new_worker" "$new_sandbox" "$new_mcp"
grep -Fx "RUSA_API_IMAGE=$new_api" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_WORKER_IMAGE=$new_worker" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_SANDBOX_IMAGE=$new_sandbox" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_MCP_IMAGE=$new_mcp" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_QA_SANDBOX_IMAGE=$new_qa_sandbox" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_API_IMAGE=$old_api" "$success_dir/.env.previous" >/dev/null
grep -Fx "RUSA_MCP_IMAGE=$old_mcp" "$success_dir/.env.previous" >/dev/null
echo "PASS: immutable deployment updates service images and both sandbox daemons, retaining rollback snapshot"

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
grep -Fx "RUSA_MCP_IMAGE=$old_mcp" "$success_dir/.env" >/dev/null
grep -Fx "RUSA_QA_SANDBOX_IMAGE=$old_qa_sandbox" "$success_dir/.env" >/dev/null
echo "PASS: manual rollback restores the previous image set and QA sandbox digest"

failure_dir="$tmp_root/failure"
make_case "$failure_dir"
if run_deploy "$failure_dir" 1 "$new_api" "$new_worker" "$new_sandbox" "$new_mcp"; then
  echo "Expected the failed candidate deployment to return non-zero after rollback." >&2
  exit 1
fi
grep -Fx "RUSA_API_IMAGE=$old_api" "$failure_dir/.env" >/dev/null
grep -Fx "RUSA_WORKER_IMAGE=$old_worker" "$failure_dir/.env" >/dev/null
grep -Fx "RUSA_SANDBOX_IMAGE=$old_sandbox" "$failure_dir/.env" >/dev/null
grep -Fx "RUSA_MCP_IMAGE=$old_mcp" "$failure_dir/.env" >/dev/null
grep -Fx "RUSA_QA_SANDBOX_IMAGE=$old_qa_sandbox" "$failure_dir/.env" >/dev/null
echo "PASS: unhealthy candidate automatically restores the previous healthy image set"

rag_dir="$tmp_root/rag-enabled"
make_case "$rag_dir"
: > "$rag_dir/docker-compose.production.rag-mcp.yml"
sed -i 's/^RUSA_RAG_ENABLED=false$/RUSA_RAG_ENABLED=true/' "$rag_dir/.env"
run_deploy "$rag_dir" 0 "$new_api" "$new_worker" "$new_sandbox" "$new_mcp"
grep -Fx "RUSA_MCP_IMAGE=$new_mcp" "$rag_dir/.env" >/dev/null
echo "PASS: RAG-enabled deployment includes and health-checks the MCP overlay"

rag_failure_dir="$tmp_root/rag-failure"
make_case "$rag_failure_dir"
: > "$rag_failure_dir/docker-compose.production.rag-mcp.yml"
sed -i 's/^RUSA_RAG_ENABLED=false$/RUSA_RAG_ENABLED=true/' "$rag_failure_dir/.env"
if run_deploy "$rag_failure_dir" 1 "$new_api" "$new_worker" "$new_sandbox" "$new_mcp"; then
  echo "Expected unhealthy RAG/MCP deployment to roll back and return non-zero." >&2
  exit 1
fi
grep -Fx "RUSA_API_IMAGE=$old_api" "$rag_failure_dir/.env" >/dev/null
grep -Fx "RUSA_MCP_IMAGE=$old_mcp" "$rag_failure_dir/.env" >/dev/null
echo "PASS: unhealthy MCP overlay triggers full four-image rollback"

enabled_dir="$tmp_root/enabled"
make_case "$enabled_dir"
sed -i 's/^RUSA_AICODE_ENABLED=false$/RUSA_AICODE_ENABLED=true/' "$enabled_dir/.env"
if run_deploy "$enabled_dir" 0 "$new_api" "$new_worker" "$new_sandbox" "$new_mcp"; then
  echo "Deployment must be rejected while AICode intake is enabled." >&2
  exit 1
fi
grep -Fx "RUSA_API_IMAGE=$old_api" "$enabled_dir/.env" >/dev/null
echo "PASS: deployment is blocked until AICode intake is disabled"

missing_overlay_dir="$tmp_root/missing-overlay"
make_case "$missing_overlay_dir"
sed -i 's/^RUSA_RAG_ENABLED=false$/RUSA_RAG_ENABLED=true/' "$missing_overlay_dir/.env"
if run_deploy "$missing_overlay_dir" 0 "$new_api" "$new_worker" "$new_sandbox" "$new_mcp"; then
  echo "RAG deployment must fail closed when its Compose overlay is missing." >&2
  exit 1
fi
grep -Fx "RUSA_API_IMAGE=$old_api" "$missing_overlay_dir/.env" >/dev/null
echo "PASS: RAG deployment fails closed without the MCP Compose overlay"

if run_deploy "$failure_dir" 0 "ghcr.io/strategyg/rusa-ai-api:latest" "$new_worker" "$new_sandbox" "$new_mcp"; then
  echo "Mutable image tags must be rejected." >&2
  exit 1
fi
echo "PASS: mutable image tags are rejected before deployment"

same_socket_dir="$tmp_root/same-socket"
make_case "$same_socket_dir"
sed -i 's#^RUSA_QA_DOCKER_SOCKET=.*$#RUSA_QA_DOCKER_SOCKET=/run/user/1001/docker.sock#' "$same_socket_dir/.env"
if run_deploy "$same_socket_dir" 0 "$new_api" "$new_worker" "$new_sandbox" "$new_mcp"; then
  echo "Worker and QA services must not share a Docker socket." >&2
  exit 1
fi
grep -Fx "RUSA_API_IMAGE=$old_api" "$same_socket_dir/.env" >/dev/null
echo "PASS: deployment rejects a shared worker/QA Docker socket"

same_workspace_dir="$tmp_root/same-workspace"
make_case "$same_workspace_dir"
sed -i 's#^RUSA_QA_WORKSPACE_ROOT=.*$#RUSA_QA_WORKSPACE_ROOT=/var/lib/rusa/workspaces#' "$same_workspace_dir/.env"
if run_deploy "$same_workspace_dir" 0 "$new_api" "$new_worker" "$new_sandbox" "$new_mcp"; then
  echo "Worker and QA services must not share a workspace mount." >&2
  exit 1
fi
grep -Fx "RUSA_API_IMAGE=$old_api" "$same_workspace_dir/.env" >/dev/null
echo "PASS: deployment rejects a shared worker/QA workspace"

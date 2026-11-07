#!/usr/bin/env bash
set -Eeuo pipefail

api_image="${1:-}"
worker_image="${2:-}"
sandbox_image="${3:-}"
mcp_image="${4:-}"

readonly API_PATTERN='^ghcr\.io/strategyg/rusa-ai-api@sha256:[0-9a-f]{64}$'
readonly WORKER_PATTERN='^ghcr\.io/strategyg/rusa-ai-worker@sha256:[0-9a-f]{64}$'
readonly SANDBOX_PATTERN='^ghcr\.io/strategyg/rusa-ai-sandbox@sha256:[0-9a-f]{64}$'
readonly MCP_PATTERN='^ghcr\.io/strategyg/rusa-ai-rag-mcp@sha256:[0-9a-f]{64}$'

[[ "$api_image" =~ $API_PATTERN ]] || { echo "API image must be an immutable approved GHCR digest." >&2; exit 64; }
[[ "$worker_image" =~ $WORKER_PATTERN ]] || { echo "Worker image must be an immutable approved GHCR digest." >&2; exit 64; }
[[ "$sandbox_image" =~ $SANDBOX_PATTERN ]] || { echo "Sandbox image must be an immutable approved GHCR digest." >&2; exit 64; }
[[ "$mcp_image" =~ $MCP_PATTERN ]] || { echo "MCP image must be an immutable approved GHCR digest." >&2; exit 64; }

compose_file="${RUSA_COMPOSE_FILE:-docker-compose.production.yml}"
rag_compose_file="${RUSA_RAG_COMPOSE_FILE:-docker-compose.production.rag-mcp.yml}"
env_file="${RUSA_ENV_FILE:-.env}"
lock_file="${RUSA_DEPLOY_LOCK_FILE:-.rusa-deploy.lock}"
health_timeout="${RUSA_DEPLOY_HEALTH_TIMEOUT_SECONDS:-300}"

[[ -f "$compose_file" ]] || { echo "Production Compose file is missing: $compose_file" >&2; exit 66; }
[[ -f "$env_file" ]] || { echo "Production environment file is missing: $env_file" >&2; exit 66; }
[[ "$health_timeout" =~ ^[0-9]+$ ]] && (( health_timeout >= 60 && health_timeout <= 1800 )) || {
  echo "RUSA_DEPLOY_HEALTH_TIMEOUT_SECONDS must be between 60 and 1800." >&2
  exit 64
}

if [[ "${RUSA_DEPLOY_LOCK_HELD:-0}" != "1" ]]; then
  command -v flock >/dev/null 2>&1 || { echo "flock is required for serialized deployments." >&2; exit 69; }
  exec 9>"$lock_file"
  flock -n 9 || { echo "Another RUSA-AI deployment or rollback is in progress." >&2; exit 75; }
fi

get_env_value() {
  local key="$1"
  awk -F= -v key="$key" '
    $1 == key {
      sub(/^[^=]*=/, "")
      value = $0
      count++
    }
    END {
      if (count != 1) exit 1
      print value
    }
  ' "$env_file"
}

aicode_enabled="$(get_env_value RUSA_AICODE_ENABLED)" || { echo "Exactly one RUSA_AICODE_ENABLED setting must exist in .env." >&2; exit 64; }
case "${aicode_enabled,,}" in
  false|0|no) ;;
  *) echo "Disable AICode and drain active tasks before deploying or rolling back." >&2; exit 64 ;;
esac

rag_value="$(get_env_value RUSA_RAG_ENABLED)" || { echo "Exactly one RUSA_RAG_ENABLED setting must exist in .env." >&2; exit 64; }
case "${rag_value,,}" in
  true|1|yes) rag_enabled=true ;;
  false|0|no) rag_enabled=false ;;
  *) echo "RUSA_RAG_ENABLED must be true or false." >&2; exit 64 ;;
esac
if [[ "$rag_enabled" == "true" && ! -f "$rag_compose_file" ]]; then
  echo "RAG is enabled but the production RAG/MCP Compose overlay is missing." >&2
  exit 66
fi

old_api_image="$(get_env_value RUSA_API_IMAGE)" || { echo "Exactly one RUSA_API_IMAGE must exist in .env." >&2; exit 64; }
old_worker_image="$(get_env_value RUSA_WORKER_IMAGE)" || { echo "Exactly one RUSA_WORKER_IMAGE must exist in .env." >&2; exit 64; }
old_sandbox_image="$(get_env_value RUSA_SANDBOX_IMAGE)" || { echo "Exactly one RUSA_SANDBOX_IMAGE must exist in .env." >&2; exit 64; }
old_mcp_image="$(get_env_value RUSA_MCP_IMAGE)" || { echo "Exactly one RUSA_MCP_IMAGE must exist in .env." >&2; exit 64; }
docker_socket="$(get_env_value RUSA_DOCKER_SOCKET)" || { echo "Exactly one RUSA_DOCKER_SOCKET must exist in .env." >&2; exit 64; }
qa_docker_socket="$(get_env_value RUSA_QA_DOCKER_SOCKET)" || { echo "Exactly one RUSA_QA_DOCKER_SOCKET must exist in .env." >&2; exit 64; }
worker_workspace_root="$(get_env_value RUSA_WORKSPACE_ROOT)" || { echo "Exactly one RUSA_WORKSPACE_ROOT must exist in .env." >&2; exit 64; }
qa_workspace_root="$(get_env_value RUSA_QA_WORKSPACE_ROOT)" || { echo "Exactly one RUSA_QA_WORKSPACE_ROOT must exist in .env." >&2; exit 64; }
qa_workspace_gid="$(get_env_value RUSA_QA_WORKSPACE_GID)" || { echo "Exactly one RUSA_QA_WORKSPACE_GID must exist in .env." >&2; exit 64; }
qa_docker_gid="$(get_env_value RUSA_QA_DOCKER_GID)" || { echo "Exactly one RUSA_QA_DOCKER_GID must exist in .env." >&2; exit 64; }
old_qa_sandbox_image="$(get_env_value RUSA_QA_SANDBOX_IMAGE)" || { echo "Exactly one RUSA_QA_SANDBOX_IMAGE must exist in .env." >&2; exit 64; }

[[ "$old_api_image" =~ $API_PATTERN ]] || { echo "Current API image is not a digest-pinned rollback target." >&2; exit 64; }
[[ "$old_worker_image" =~ $WORKER_PATTERN ]] || { echo "Current worker image is not a digest-pinned rollback target." >&2; exit 64; }
[[ "$old_sandbox_image" =~ $SANDBOX_PATTERN ]] || { echo "Current sandbox image is not a digest-pinned rollback target." >&2; exit 64; }
[[ "$old_mcp_image" =~ $MCP_PATTERN ]] || { echo "Current MCP image is not a digest-pinned rollback target." >&2; exit 64; }
[[ "$old_qa_sandbox_image" =~ $SANDBOX_PATTERN && "$old_qa_sandbox_image" == "$old_sandbox_image" ]] || { echo "QA and worker sandbox rollback digests must match." >&2; exit 64; }
[[ "$docker_socket" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "RUSA_DOCKER_SOCKET must be a safe absolute Unix-socket path." >&2; exit 64; }
[[ "$qa_docker_socket" =~ ^/[A-Za-z0-9._/-]+$ && "$qa_docker_socket" != "$docker_socket" ]] || { echo "RUSA_QA_DOCKER_SOCKET must be a separate safe absolute Unix-socket path." >&2; exit 64; }
[[ "$qa_workspace_root" =~ ^/[A-Za-z0-9._/-]+$ && "$qa_workspace_root" != "$worker_workspace_root" ]] || { echo "RUSA_QA_WORKSPACE_ROOT must be a separate safe absolute path from the worker workspace." >&2; exit 64; }
[[ "$qa_workspace_gid" =~ ^[0-9]+$ && "$qa_docker_gid" =~ ^[0-9]+$ ]] || { echo "QA workspace and Docker socket GIDs must be numeric." >&2; exit 64; }

tmp_env="$(mktemp "${env_file}.tmp.XXXXXX")"
trap 'rm -f "$tmp_env"' EXIT
cp -p -- "$env_file" "${env_file}.previous"
chmod 600 "${env_file}.previous"

awk -F= -v api="$api_image" -v worker="$worker_image" -v sandbox="$sandbox_image" -v mcp="$mcp_image" '
  BEGIN { api_count = 0; worker_count = 0; sandbox_count = 0; mcp_count = 0; qa_sandbox_count = 0 }
  $1 == "RUSA_API_IMAGE" { api_count++; print "RUSA_API_IMAGE=" api; next }
  $1 == "RUSA_WORKER_IMAGE" { worker_count++; print "RUSA_WORKER_IMAGE=" worker; next }
  $1 == "RUSA_SANDBOX_IMAGE" { sandbox_count++; print "RUSA_SANDBOX_IMAGE=" sandbox; next }
  $1 == "RUSA_QA_SANDBOX_IMAGE" { qa_sandbox_count++; print "RUSA_QA_SANDBOX_IMAGE=" sandbox; next }
  $1 == "RUSA_MCP_IMAGE" { mcp_count++; print "RUSA_MCP_IMAGE=" mcp; next }
  { print }
  END {
    if (api_count != 1 || worker_count != 1 || sandbox_count != 1 || qa_sandbox_count != 1 || mcp_count != 1) {
      print "Production .env must contain exactly one API, worker, sandbox, QA sandbox, and MCP image setting." > "/dev/stderr"
      exit 42
    }
  }
' "$env_file" > "$tmp_env"
chmod 600 "$tmp_env"
mv -- "$tmp_env" "$env_file"

compose() {
  local -a files
  files=(-f "$compose_file")
  if [[ "$rag_enabled" == "true" ]]; then
    files+=(-f "$rag_compose_file")
  fi
  docker compose --env-file "$env_file" "${files[@]}" "$@"
}

wait_for_health() {
  local deadline=$((SECONDS + health_timeout))
  local api_id worker_id qa_id mcp_id api_state worker_state qa_state mcp_state
  while (( SECONDS < deadline )); do
    api_id="$(compose ps -q api 2>/dev/null || true)"
    worker_id="$(compose ps -q worker 2>/dev/null || true)"
    qa_id="$(compose ps -q qa 2>/dev/null || true)"
    mcp_id=""
    if [[ "$rag_enabled" == "true" ]]; then
      mcp_id="$(compose ps -q rusa-mcp 2>/dev/null || true)"
    fi

    if [[ -n "$api_id" && -n "$worker_id" && -n "$qa_id" ]] && { [[ "$rag_enabled" != "true" ]] || [[ -n "$mcp_id" ]]; }; then
      api_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$api_id" 2>/dev/null || echo missing)"
      worker_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$worker_id" 2>/dev/null || echo missing)"
      qa_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$qa_id" 2>/dev/null || echo missing)"
      mcp_state=healthy
      if [[ "$rag_enabled" == "true" ]]; then
        mcp_state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$mcp_id" 2>/dev/null || echo missing)"
      fi

      if [[ "$api_state" == "healthy" && "$worker_state" == "healthy" && "$qa_state" == "healthy" && "$mcp_state" == "healthy" ]]; then
        return 0
      fi
      if [[ "$api_state" == "unhealthy" || "$api_state" == "exited" || "$api_state" == "dead" ||
            "$worker_state" == "unhealthy" || "$worker_state" == "exited" || "$worker_state" == "dead" ||
            "$qa_state" == "unhealthy" || "$qa_state" == "exited" || "$qa_state" == "dead" ||
            "$mcp_state" == "unhealthy" || "$mcp_state" == "exited" || "$mcp_state" == "dead" ]]; then
        echo "A production container entered a terminal or unhealthy state (API=$api_state, worker=$worker_state, QA=$qa_state, MCP=$mcp_state)." >&2
        return 1
      fi
    fi
    sleep 5
  done
  echo "Timed out waiting for required production health checks." >&2
  return 1
}

apply_release() {
  local next_api="$1" next_worker="$2" next_sandbox="$3" next_mcp="$4"
  [[ "$next_api" =~ $API_PATTERN && "$next_worker" =~ $WORKER_PATTERN &&
     "$next_sandbox" =~ $SANDBOX_PATTERN && "$next_mcp" =~ $MCP_PATTERN ]] || return 1

  compose config --quiet || return 1
  if [[ "$rag_enabled" == "true" ]]; then
    compose pull api worker qa rusa-mcp || return 1
  else
    compose pull api worker qa || return 1
  fi
  DOCKER_HOST="unix://$docker_socket" docker pull "$next_sandbox" || return 1
  DOCKER_HOST="unix://$qa_docker_socket" docker pull "$next_sandbox" || return 1
  if [[ "$rag_enabled" == "true" ]]; then
    compose up -d --no-build --remove-orphans api worker qa rusa-mcp || return 1
  else
    compose up -d --no-build --remove-orphans api worker qa || return 1
  fi
  wait_for_health
}

if apply_release "$api_image" "$worker_image" "$sandbox_image" "$mcp_image"; then
  echo "RUSA-AI deployment is healthy, including independent QA (RAG/MCP enabled: $rag_enabled)."
  echo "Previous immutable image digests are retained in ${env_file}.previous."
  exit 0
fi

echo "Candidate deployment failed; restoring the previous immutable image digests." >&2
cp -p -- "${env_file}.previous" "$env_file"
chmod 600 "$env_file"
if apply_release "$old_api_image" "$old_worker_image" "$old_sandbox_image" "$old_mcp_image"; then
  echo "Automatic rollback restored the previous healthy service set." >&2
  exit 1
fi

echo "CRITICAL: automatic rollback failed. Keep task intake disabled and intervene manually." >&2
exit 2

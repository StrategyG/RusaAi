#!/usr/bin/env bash
set -Eeuo pipefail

env_file="${RUSA_ENV_FILE:-.env}"
lock_file="${RUSA_DEPLOY_LOCK_FILE:-.rusa-deploy.lock}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

[[ -f "$env_file" ]] || { echo "Production environment file is missing: $env_file" >&2; exit 66; }
[[ -f "${env_file}.previous" ]] || { echo "No previous production environment snapshot exists." >&2; exit 66; }

command -v flock >/dev/null 2>&1 || { echo "flock is required for serialized rollbacks." >&2; exit 69; }
exec 9>"$lock_file"
flock -n 9 || { echo "Another RUSA-AI deployment or rollback is in progress." >&2; exit 75; }

get_snapshot_value() {
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
  ' "${env_file}.previous"
}

api_image="$(get_snapshot_value RUSA_API_IMAGE)" || { echo "Previous snapshot has no unique API image." >&2; exit 64; }
worker_image="$(get_snapshot_value RUSA_WORKER_IMAGE)" || { echo "Previous snapshot has no unique worker image." >&2; exit 64; }
sandbox_image="$(get_snapshot_value RUSA_SANDBOX_IMAGE)" || { echo "Previous snapshot has no unique sandbox image." >&2; exit 64; }
web_image="$(get_snapshot_value RUSA_WEB_IMAGE)" || { echo "Previous snapshot has no unique web image." >&2; exit 64; }

RUSA_DEPLOY_LOCK_HELD=1 "$script_dir/compose-deploy.sh" "$api_image" "$worker_image" "$sandbox_image" "$web_image"

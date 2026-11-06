#!/usr/bin/env bash
set -Eeuo pipefail

interval_seconds="${RUSA_BACKUP_INTERVAL_SECONDS:-3600}"
retry_seconds="${RUSA_BACKUP_RETRY_SECONDS:-300}"
require_remote="${RUSA_BACKUP_REQUIRE_REMOTE:-false}"

if [[ "$require_remote" != "true" && "$require_remote" != "false" ]]; then
    echo "RUSA_BACKUP_REQUIRE_REMOTE must be true or false" >&2
    exit 1
fi
if [[ "$require_remote" == "true" && -z "${RUSA_BACKUP_S3_URI:-}" ]]; then
    echo "RUSA_BACKUP_S3_URI is required when remote backups are mandatory" >&2
    exit 1
fi

if [[ ! "$interval_seconds" =~ ^[1-9][0-9]{0,5}$ ]] || (( interval_seconds < 60 || interval_seconds > 604800 )); then
    echo "RUSA_BACKUP_INTERVAL_SECONDS must be between 60 and 604800" >&2
    exit 1
fi
if [[ ! "$retry_seconds" =~ ^[1-9][0-9]{0,4}$ ]] || (( retry_seconds > 86400 )); then
    echo "RUSA_BACKUP_RETRY_SECONDS must be between 1 and 86400" >&2
    exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
while true; do
    cycle_started="$(date +%s)"
    if "$script_dir/backup.sh"; then
        elapsed="$(( $(date +%s) - cycle_started ))"
        delay="$(( interval_seconds - elapsed ))"
        (( delay > 0 )) || delay=1
        echo "Scheduled backup succeeded; next run in ${delay}s"
    else
        delay="$retry_seconds"
        echo "Scheduled backup failed; retrying in ${delay}s" >&2
    fi
    sleep "$delay"
done

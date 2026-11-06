#!/usr/bin/env bash
set -Eeuo pipefail

: "${SOURCE_DATABASE_URL:?SOURCE_DATABASE_URL is required}"
: "${RESTORE_DATABASE_URL:?RESTORE_DATABASE_URL is required}"
: "${BACKUP_DIR:?BACKUP_DIR is required}"
: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT is required}"
: "${BACKUP_AGE_IDENTITY_FILE:?BACKUP_AGE_IDENTITY_FILE is required}"
: "${RESTORE_CONFIRM_DATABASE:?RESTORE_CONFIRM_DATABASE is required}"

for command in psql date; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "Required command is unavailable: $command" >&2
        exit 1
    }
done

source_database="$(
    psql "$SOURCE_DATABASE_URL" -XAtq -v ON_ERROR_STOP=1 -c 'SELECT current_database()'
)"
target_database="$(
    psql "$RESTORE_DATABASE_URL" -XAtq -v ON_ERROR_STOP=1 -c 'SELECT current_database()'
)"
if [[ "$source_database" == "$target_database" ]]; then
    echo "Source and restore target must be different databases" >&2
    exit 1
fi
if [[ "$target_database" != "$RESTORE_CONFIRM_DATABASE" ]]; then
    echo "Restore confirmation does not match the target database" >&2
    exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
started_at="$(date +%s)"
backup_started_at="$started_at"
backup_file="$(
    DATABASE_URL="$SOURCE_DATABASE_URL" \
    BACKUP_DIR="$BACKUP_DIR" \
    BACKUP_AGE_RECIPIENT="$BACKUP_AGE_RECIPIENT" \
    BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}" \
    RUSA_BACKUP_REQUIRE_REMOTE="${RUSA_BACKUP_REQUIRE_REMOTE:-false}" \
    RUSA_BACKUP_S3_URI="${RUSA_BACKUP_S3_URI:-}" \
    RUSA_BACKUP_S3_ENDPOINT="${RUSA_BACKUP_S3_ENDPOINT:-}" \
    bash "$script_dir/backup.sh"
)"
backup_finished_at="$(date +%s)"
restore_backup_file="$backup_file"
if [[ -n "${RUSA_BACKUP_S3_URI:-}" ]]; then
    restore_backup_file="${RUSA_BACKUP_S3_URI%/}/$(basename "$backup_file")"
fi

RESTORE_DATABASE_URL="$RESTORE_DATABASE_URL" \
BACKUP_FILE="$restore_backup_file" \
BACKUP_AGE_IDENTITY_FILE="$BACKUP_AGE_IDENTITY_FILE" \
RESTORE_CONFIRM_DATABASE="$RESTORE_CONFIRM_DATABASE" \
RUSA_BACKUP_S3_ENDPOINT="${RUSA_BACKUP_S3_ENDPOINT:-}" \
bash "$script_dir/restore.sh"
restore_finished_at="$(date +%s)"

printf 'BACKUP_FILE=%s\n' "$restore_backup_file"
printf 'BACKUP_SECONDS=%s\n' "$((backup_finished_at - backup_started_at))"
printf 'RESTORE_SECONDS=%s\n' "$((restore_finished_at - backup_finished_at))"
printf 'TOTAL_RECOVERY_SECONDS=%s\n' "$((restore_finished_at - started_at))"
printf 'RESTORE_TARGET_DATABASE=%s\n' "$target_database"

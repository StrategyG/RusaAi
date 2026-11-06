#!/usr/bin/env bash
set -Eeuo pipefail

: "${RESTORE_DATABASE_URL:?RESTORE_DATABASE_URL is required}"
: "${BACKUP_FILE:?BACKUP_FILE is required}"
: "${BACKUP_AGE_IDENTITY_FILE:?BACKUP_AGE_IDENTITY_FILE is required}"
: "${RESTORE_CONFIRM_DATABASE:?RESTORE_CONFIRM_DATABASE must exactly match the target database name}"

for command in psql pg_restore age sha256sum mktemp; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "Required command is unavailable: $command" >&2
        exit 1
    }
done

if [[ ! -r "$BACKUP_AGE_IDENTITY_FILE" ]]; then
    echo "Backup age identity file is not readable" >&2
    exit 1
fi

target_database="$(
    psql "$RESTORE_DATABASE_URL" -XAtq -v ON_ERROR_STOP=1 -c 'SELECT current_database()'
)"
if [[ "$target_database" != "$RESTORE_CONFIRM_DATABASE" ]]; then
    echo "Restore confirmation does not match the target database" >&2
    exit 1
fi
case "$target_database" in
    postgres|template0|template1)
        echo "Refusing to restore into a PostgreSQL system database" >&2
        exit 1
        ;;
esac

umask 077
temporary_encrypted=""
temporary_checksum=""
temporary_plain="$(mktemp /tmp/rusa-agent-restore.XXXXXX.dump)"
rm -f "$temporary_plain"
cleanup() {
    rm -f "$temporary_plain"
    [[ -z "$temporary_encrypted" ]] || rm -f "$temporary_encrypted"
    [[ -z "$temporary_checksum" ]] || rm -f "$temporary_checksum"
}
trap cleanup EXIT

encrypted_file="$BACKUP_FILE"
if [[ "$BACKUP_FILE" == s3://* ]]; then
    command -v aws >/dev/null 2>&1 || {
        echo "aws CLI is required to restore an S3 backup" >&2
        exit 1
    }
    temporary_encrypted="$(mktemp /tmp/rusa-agent-backup.XXXXXX.age)"
    aws_args=()
    if [[ -n "${RUSA_BACKUP_S3_ENDPOINT:-}" ]]; then
        aws_args+=(--endpoint-url "$RUSA_BACKUP_S3_ENDPOINT")
    fi
    aws "${aws_args[@]}" s3 cp "$BACKUP_FILE" "$temporary_encrypted" --only-show-errors
    encrypted_file="$temporary_encrypted"
    temporary_checksum="$(mktemp /tmp/rusa-agent-checksum.XXXXXX)"
    if aws "${aws_args[@]}" s3 cp "${BACKUP_FILE}.sha256" "$temporary_checksum" --only-show-errors 2>/dev/null; then
        expected_hash="$(awk '{print $1}' "$temporary_checksum")"
        actual_hash="$(sha256sum "$encrypted_file" | awk '{print $1}')"
        [[ "$expected_hash" == "$actual_hash" ]] || {
            echo "Backup checksum verification failed" >&2
            exit 1
        }
    fi
elif [[ ! -f "$encrypted_file" ]]; then
    echo "Backup file does not exist" >&2
    exit 1
elif [[ -f "${encrypted_file}.sha256" ]]; then
    (
        cd "$(dirname "$encrypted_file")"
        sha256sum -c "$(basename "$encrypted_file").sha256"
    )
fi

age --decrypt \
    --identity "$BACKUP_AGE_IDENTITY_FILE" \
    --output "$temporary_plain" \
    "$encrypted_file"

pg_restore --list "$temporary_plain" >/dev/null

# The exact database-name confirmation is mandatory because --clean replaces objects.
pg_restore \
    --exit-on-error \
    --single-transaction \
    --clean \
    --if-exists \
    --no-owner \
    --no-privileges \
    --dbname="$RESTORE_DATABASE_URL" \
    "$temporary_plain"

psql "$RESTORE_DATABASE_URL" -XAtq -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null
echo "PostgreSQL restore completed for target database: $target_database"

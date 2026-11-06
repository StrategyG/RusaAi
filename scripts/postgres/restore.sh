#!/usr/bin/env bash
set -Eeuo pipefail

: "${RESTORE_DATABASE_URL:?RESTORE_DATABASE_URL is required}"
: "${BACKUP_FILE:?BACKUP_FILE is required}"
: "${BACKUP_AGE_IDENTITY_FILE:?BACKUP_AGE_IDENTITY_FILE is required}"
: "${RESTORE_CONFIRM_DATABASE:?RESTORE_CONFIRM_DATABASE must exactly match the target database name}"

for command in psql pg_restore age sha256sum mktemp awk; do
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
restore_tmp_dir="${RESTORE_TMP_DIR:-${BACKUP_DIR:-/backups}}"
mkdir -p "$restore_tmp_dir"
cleanup() {
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
    temporary_encrypted="$(mktemp "$restore_tmp_dir/.rusa-agent-restore.XXXXXX.age")"
    temporary_checksum="$(mktemp "$restore_tmp_dir/.rusa-agent-checksum.XXXXXX")"
    aws_args=()
    if [[ -n "${RUSA_BACKUP_S3_ENDPOINT:-}" ]]; then
        aws_args+=(--endpoint-url "$RUSA_BACKUP_S3_ENDPOINT")
    fi
    aws "${aws_args[@]}" s3 cp "$BACKUP_FILE" "$temporary_encrypted" --only-show-errors
    if ! aws "${aws_args[@]}" s3 cp "${BACKUP_FILE}.sha256" "$temporary_checksum" --only-show-errors 2>/dev/null; then
        echo "S3 backup checksum sidecar is missing" >&2
        exit 1
    fi
    expected_hash="$(awk '{print $1}' "$temporary_checksum")"
    if [[ ! "$expected_hash" =~ ^[a-fA-F0-9]{64}$ ]]; then
        echo "S3 backup checksum sidecar is invalid" >&2
        exit 1
    fi
    actual_hash="$(sha256sum "$temporary_encrypted" | awk '{print $1}')"
    [[ "$expected_hash" == "$actual_hash" ]] || {
        echo "Backup checksum verification failed" >&2
        exit 1
    }
    encrypted_file="$temporary_encrypted"
elif [[ ! -f "$encrypted_file" ]]; then
    echo "Backup file does not exist" >&2
    exit 1
elif [[ ! -f "${encrypted_file}.sha256" ]]; then
    echo "Backup checksum sidecar is missing" >&2
    exit 1
else
    (
        cd "$(dirname "$encrypted_file")"
        sha256sum -c "$(basename "$encrypted_file").sha256"
    )
fi

# Decrypt directly into pg_restore. Plaintext database archives are never written
# to the container filesystem, so restore size is not constrained by /tmp tmpfs.
age --decrypt \
    --identity "$BACKUP_AGE_IDENTITY_FILE" \
    "$encrypted_file" \
    | pg_restore --list >/dev/null

age --decrypt \
    --identity "$BACKUP_AGE_IDENTITY_FILE" \
    "$encrypted_file" \
    | pg_restore \
        --exit-on-error \
        --single-transaction \
        --clean \
        --if-exists \
        --no-owner \
        --no-privileges \
        --dbname="$RESTORE_DATABASE_URL"

psql "$RESTORE_DATABASE_URL" -XAtq -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null
echo "PostgreSQL restore completed for target database: $target_database"

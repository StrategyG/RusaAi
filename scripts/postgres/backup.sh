#!/usr/bin/env bash
set -Eeuo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${BACKUP_DIR:?BACKUP_DIR is required}"
: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT is required}"

for command in pg_dump age sha256sum date find; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "Required command is unavailable: $command" >&2
        exit 1
    }
done

if [[ ! "$BACKUP_AGE_RECIPIENT" =~ ^age1[0-9a-z]+$ ]]; then
    echo "BACKUP_AGE_RECIPIENT must be an age public recipient" >&2
    exit 1
fi

retention_days="${BACKUP_RETENTION_DAYS:-30}"
if [[ ! "$retention_days" =~ ^[1-9][0-9]{0,3}$ ]] || (( retention_days > 3650 )); then
    echo "BACKUP_RETENTION_DAYS must be between 1 and 3650" >&2
    exit 1
fi

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_file="${BACKUP_DIR%/}/rusa-agent-${timestamp}-$$.dump.age"
temporary_file="$(mktemp "${BACKUP_DIR%/}/.rusa-agent-backup.XXXXXX")"
rm -f "$temporary_file"
trap 'rm -f "$temporary_file"' EXIT

# Stream pg_dump directly into age: no plaintext dump is written to disk.
pg_dump \
    --format=custom \
    --compress=9 \
    --no-owner \
    --no-privileges \
    "$DATABASE_URL" \
    | age --encrypt --recipient "$BACKUP_AGE_RECIPIENT" --output "$temporary_file"

if [[ ! -s "$temporary_file" ]]; then
    echo "Encrypted backup artifact is empty" >&2
    exit 1
fi

mv "$temporary_file" "$backup_file"
(
    cd "$BACKUP_DIR"
    sha256sum "$(basename "$backup_file")" > "$(basename "$backup_file").sha256"
)

if [[ -n "${RUSA_BACKUP_S3_URI:-}" ]]; then
    command -v aws >/dev/null 2>&1 || {
        echo "aws CLI is required when RUSA_BACKUP_S3_URI is configured" >&2
        exit 1
    }
    destination="${RUSA_BACKUP_S3_URI%/}/$(basename "$backup_file")"
    checksum_destination="${destination}.sha256"
    aws_args=()
    if [[ -n "${RUSA_BACKUP_S3_ENDPOINT:-}" ]]; then
        aws_args+=(--endpoint-url "$RUSA_BACKUP_S3_ENDPOINT")
    fi
    aws "${aws_args[@]}" s3 cp "$backup_file" "$destination" --only-show-errors
    aws "${aws_args[@]}" s3 cp "${backup_file}.sha256" "$checksum_destination" --only-show-errors
fi

while IFS= read -r -d '' expired_file; do
    rm -f "$expired_file" "${expired_file}.sha256"
done < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f \
        -name 'rusa-agent-*.dump.age' -mtime "+${retention_days}" -print0
)

echo "Encrypted PostgreSQL backup created: $(basename "$backup_file")" >&2
printf '%s\n' "$backup_file"

"""Cryptographic QA verdicts and safe artifact snapshot handling."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

REQUIRED_QA_PROFILES = ("pytest", "bandit", "ruff", "compile")
SERVICE_ID = "rusa-independent-qa"
VERDICT_SCHEMA = "rusa.qa-verdict.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_TREE = re.compile(r"^[0-9a-f]{40}$")
_SENSITIVE_PARTS = {
    ".git", ".secrets", "secrets", "credentials", ".ssh", ".aws",
    ".azure", ".terraform",
}
_SENSITIVE_NAMES = {
    ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519",
    "authorized_keys", "known_hosts", "credentials.json",
    "service-account.json", "secrets.json", "token.json",
}
_SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".keystore", ".crt",
    ".cer", ".der", ".ppk", ".tfstate", ".tfvars",
}


class SnapshotError(ValueError):
    """Raised when an artifact archive is unsafe or incomplete."""


class AttestationError(ValueError):
    """Raised when a signed QA verdict is invalid or not bound to the task."""


def canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _safe_snapshot_path(raw_path: str) -> PurePosixPath:
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or raw_path.startswith("/")
        or "\\" in raw_path
    ):
        raise SnapshotError("Snapshot contains an invalid path")
    path = PurePosixPath(raw_path)
    if (
        path.is_absolute()
        or path.as_posix() != raw_path
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise SnapshotError("Snapshot contains a non-canonical or traversing path")
    parts = {part.lower() for part in path.parts}
    if (
        parts.intersection(_SENSITIVE_PARTS)
        or any(part.startswith(".env") and part != ".env.example" for part in parts)
        or path.name.lower() in _SENSITIVE_NAMES
        or path.suffix.lower() in _SENSITIVE_SUFFIXES
    ):
        raise SnapshotError("Snapshot contains a protected or credential-like path")
    return path


def snapshot_manifest(root: str | Path) -> list[dict[str, Any]]:
    base = Path(root).resolve(strict=True)
    if not base.is_dir():
        raise SnapshotError("Snapshot root must be a directory")
    entries: list[dict[str, Any]] = []
    for item in sorted(base.rglob("*"), key=lambda path: path.as_posix()):
        if item.is_symlink():
            raise SnapshotError("Snapshot symlinks are prohibited")
        if not item.is_file():
            continue
        relative = item.relative_to(base).as_posix()
        _safe_snapshot_path(relative)
        mode = stat.S_IMODE(item.stat().st_mode)
        normalized_mode = 0o755 if mode & 0o111 else 0o644
        content = item.read_bytes()
        entries.append({
            "path": relative,
            "mode": normalized_mode,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    if not entries:
        raise SnapshotError("An empty artifact snapshot cannot be validated")
    return entries


def digest_snapshot(root: str | Path) -> str:
    return hashlib.sha256(canonical_json(snapshot_manifest(root))).hexdigest()


def extract_snapshot_archive(
    archive_bytes: bytes,
    destination: str | Path,
    *,
    max_files: int = 20_000,
    max_total_bytes: int = 100_000_000,
    max_file_bytes: int = 25_000_000,
) -> str:
    if not isinstance(archive_bytes, bytes) or not archive_bytes:
        raise SnapshotError("Artifact archive is empty")
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    total_bytes = 0
    file_count = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:")
    except (tarfile.TarError, OSError) as exc:
        raise SnapshotError("Artifact archive is not a valid uncompressed tar") from exc

    with archive:
        for member in archive:
            if not member.isfile() or member.issym() or member.islnk():
                raise SnapshotError("Only regular files are accepted in QA snapshots")
            path = _safe_snapshot_path(member.name)
            normalized = path.as_posix()
            if normalized in seen:
                raise SnapshotError("Artifact archive contains duplicate paths")
            seen.add(normalized)
            file_count += 1
            if file_count > max_files:
                raise SnapshotError("Artifact snapshot exceeds the file-count limit")
            if member.size < 0 or member.size > max_file_bytes:
                raise SnapshotError("Artifact file exceeds the configured size limit")
            total_bytes += member.size
            if total_bytes > max_total_bytes:
                raise SnapshotError("Artifact snapshot exceeds the configured total size limit")
            mode = member.mode & 0o777
            if mode not in {0o644, 0o755}:
                raise SnapshotError("Artifact file has an unsupported permission mode")
            target = root.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            current = root
            for part in path.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise SnapshotError("Artifact path traverses a symbolic link")
            source = archive.extractfile(member)
            if source is None:
                raise SnapshotError("Artifact archive contains an unreadable file")
            content = source.read(member.size + 1)
            if len(content) != member.size:
                raise SnapshotError("Artifact archive file size does not match its header")
            with target.open("xb") as stream:
                stream.write(content)
            os_mode = 0o555 if mode == 0o755 else 0o444
            target.chmod(os_mode)

    if file_count == 0:
        raise SnapshotError("Artifact archive contains no files")
    for directory, _, _ in __import__("os").walk(root, topdown=False):
        Path(directory).chmod(0o555)
    return digest_snapshot(root)


def load_private_key(encoded: str) -> Ed25519PrivateKey:
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != 32:
            raise ValueError("Ed25519 private key must contain exactly 32 bytes")
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("RUSA_QA_PRIVATE_KEY_B64 must be a base64 Ed25519 private key") from exc


def load_public_key(encoded: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must contain exactly 32 bytes")
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError("RUSA_QA_PUBLIC_KEY_B64 must be a base64 Ed25519 public key") from exc


def sign_verdict(
    payload: dict[str, Any],
    private_key: Ed25519PrivateKey,
) -> dict[str, Any]:
    signature = private_key.sign(canonical_json(payload))
    return {
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def verify_verdict(
    envelope: dict[str, Any],
    public_key: Ed25519PublicKey,
    *,
    tenant_id: str,
    task_id: str,
    git_tree_sha: str,
    key_id: str,
    sandbox_image: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "signature"}:
        raise AttestationError("QA response envelope is malformed")
    payload = envelope.get("payload")
    signature_text = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature_text, str):
        raise AttestationError("QA response payload or signature is missing")
    try:
        signature = base64.b64decode(signature_text, validate=True)
        public_key.verify(signature, canonical_json(payload))
    except (ValueError, InvalidSignature, TypeError) as exc:
        raise AttestationError("QA verdict signature verification failed") from exc

    expected_keys = {
        "schema_version", "service_id", "key_id", "tenant_id", "task_id",
        "git_tree_sha", "artifact_digest", "sandbox_image", "profiles",
        "verdict", "issued_at", "expires_at",
    }
    if set(payload) != expected_keys:
        raise AttestationError("QA verdict has an unsupported schema")
    if (
        payload.get("schema_version") != VERDICT_SCHEMA
        or payload.get("service_id") != SERVICE_ID
        or payload.get("key_id") != key_id
        or payload.get("tenant_id") != tenant_id
        or payload.get("task_id") != task_id
        or payload.get("git_tree_sha") != git_tree_sha
        or payload.get("sandbox_image") != sandbox_image
        or not isinstance(payload.get("artifact_digest"), str)
        or not _SHA256.fullmatch(payload["artifact_digest"])
        or not isinstance(git_tree_sha, str)
        or not _GIT_TREE.fullmatch(git_tree_sha)
    ):
        raise AttestationError("QA verdict is not bound to this tenant, task, tree, or runner")

    try:
        issued_at = datetime.fromisoformat(payload["issued_at"])
        expires_at = datetime.fromisoformat(payload["expires_at"])
    except (TypeError, ValueError) as exc:
        raise AttestationError("QA verdict timestamps are invalid") from exc
    if issued_at.tzinfo is None or expires_at.tzinfo is None:
        raise AttestationError("QA verdict timestamps must include a timezone")
    current = now or datetime.now(timezone.utc)
    if issued_at.timestamp() > current.timestamp() + 60:
        raise AttestationError("QA verdict was issued in the future")
    lifetime = expires_at.timestamp() - issued_at.timestamp()
    if lifetime <= 0 or lifetime > 3600 or current >= expires_at:
        raise AttestationError("QA verdict is expired or has an invalid lifetime")

    profiles = payload.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != set(REQUIRED_QA_PROFILES):
        raise AttestationError("QA verdict does not contain the complete required profile set")
    exit_codes: list[int] = []
    for profile in REQUIRED_QA_PROFILES:
        item = profiles.get(profile)
        if (
            not isinstance(item, dict)
            or set(item) != {"exit_code", "output_sha256"}
            or type(item.get("exit_code")) is not int
            or not isinstance(item.get("output_sha256"), str)
            or not _SHA256.fullmatch(item["output_sha256"])
        ):
            raise AttestationError(f"QA evidence for profile {profile} is malformed")
        exit_codes.append(item["exit_code"])

    expected_verdict = "PASS" if all(code == 0 for code in exit_codes) else "FAIL"
    if payload.get("verdict") != expected_verdict:
        raise AttestationError("QA verdict does not match its signed profile results")
    return payload

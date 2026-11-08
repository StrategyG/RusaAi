"""Tenant-private, content-addressed storage for DevOps text artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4


_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ALLOWED_CONTENT_TYPES = frozenset(
    {"text/markdown", "application/yaml", "application/json"}
)


class ArtifactIntegrityError(RuntimeError):
    """An artifact reference, tenant, manifest, or content digest is invalid."""


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    uri: str
    name: str
    content_type: str
    sha256: str
    size_bytes: int
    created_at: str


class FilesystemArtifactStore:
    """Immutable tenant-private text artifacts stored by SHA-256 content address.

    The root must be a persistent private volume in deployment. Tenant identifiers
    are never used as path segments; the directory key is a one-way SHA-256 digest.
    """

    def __init__(self, root: str | Path, *, max_artifact_bytes: int = 1_000_000):
        if isinstance(max_artifact_bytes, bool) or not 1 <= max_artifact_bytes <= 10_000_000:
            raise ValueError("max_artifact_bytes must be between 1 and 10000000")
        self.root = Path(root).expanduser().resolve()
        self.max_artifact_bytes = max_artifact_bytes
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @staticmethod
    def _tenant_key(tenant_id: str) -> str:
        if not isinstance(tenant_id, str) or not _TENANT_RE.fullmatch(tenant_id):
            raise ValueError("tenant_id is not a valid RUSA tenant identifier")
        return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".rusa-artifact-", dir=str(path.parent)
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise

    def put(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        task_id: str,
        name: str,
        content: str,
        content_type: str,
    ) -> ArtifactRecord:
        tenant_key = self._tenant_key(tenant_id)
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError("workflow_id is required")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id is required")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError("artifact name must be a safe filename")
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise ValueError("unsupported DevOps artifact content type")
        if not isinstance(content, str):
            raise TypeError("artifact content must be UTF-8 text")
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > self.max_artifact_bytes:
            raise ValueError("artifact content is empty or exceeds the configured limit")

        digest = hashlib.sha256(encoded).hexdigest()
        artifact_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        tenant_root = self.root / tenant_key
        blob_path = tenant_root / "blobs" / digest
        manifest_path = tenant_root / "manifests" / f"{artifact_id}.json"

        tenant_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if blob_path.exists():
            existing = blob_path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != digest:
                raise ArtifactIntegrityError("existing content-addressed blob failed integrity check")
        else:
            self._atomic_write(blob_path, encoded)

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "name": name,
            "content_type": content_type,
            "sha256": digest,
            "size_bytes": len(encoded),
            "created_at": created_at,
            "access_policy": "tenant-private",
            "blob": f"blobs/{digest}",
        }
        self._atomic_write(
            manifest_path,
            json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        uri = f"artifact://{tenant_key}/{artifact_id}?sha256={digest}"
        return ArtifactRecord(
            artifact_id=artifact_id,
            uri=uri,
            name=name,
            content_type=content_type,
            sha256=digest,
            size_bytes=len(encoded),
            created_at=created_at,
        )

    def read(self, *, tenant_id: str, uri: str) -> tuple[bytes, dict[str, Any]]:
        tenant_key = self._tenant_key(tenant_id)
        if not isinstance(uri, str):
            raise ArtifactIntegrityError("artifact URI must be text")
        parsed = urlsplit(uri)
        if (
            parsed.scheme != "artifact"
            or parsed.netloc != tenant_key
            or parsed.query == ""
            or parsed.fragment
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ArtifactIntegrityError("artifact URI is not scoped to the authenticated tenant")
        artifact_id = parsed.path.lstrip("/")
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
            raise ArtifactIntegrityError("artifact URI contains an invalid asset identifier")
        query = parse_qs(parsed.query, strict_parsing=True)
        if set(query) != {"sha256"} or len(query["sha256"]) != 1:
            raise ArtifactIntegrityError("artifact URI must contain exactly one SHA-256 digest")
        expected_digest = query["sha256"][0]
        if not re.fullmatch(r"[a-f0-9]{64}", expected_digest):
            raise ArtifactIntegrityError("artifact URI contains an invalid SHA-256 digest")

        tenant_root = self.root / tenant_key
        manifest_path = tenant_root / "manifests" / f"{artifact_id}.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError("artifact manifest is unavailable or invalid") from exc
        if (
            manifest.get("schema_version") != 1
            or manifest.get("artifact_id") != artifact_id
            or manifest.get("tenant_id") != tenant_id
            or manifest.get("access_policy") != "tenant-private"
            or manifest.get("sha256") != expected_digest
        ):
            raise ArtifactIntegrityError("artifact manifest does not match the requested reference")

        blob_name = manifest.get("blob")
        if blob_name != f"blobs/{expected_digest}":
            raise ArtifactIntegrityError("artifact manifest contains an unsafe blob reference")
        blob_path = tenant_root / blob_name
        try:
            content = blob_path.read_bytes()
        except OSError as exc:
            raise ArtifactIntegrityError("artifact content is unavailable") from exc
        if (
            len(content) != manifest.get("size_bytes")
            or hashlib.sha256(content).hexdigest() != expected_digest
        ):
            raise ArtifactIntegrityError("artifact content failed its integrity check")
        return content, manifest

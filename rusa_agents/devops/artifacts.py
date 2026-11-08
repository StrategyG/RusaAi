"""Tenant-scoped immutable content-addressed storage for DevOps artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any


_DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
_ALLOWED_NAMESPACES = frozenset({"artifact", "evidence"})


class ArtifactStoreError(RuntimeError):
    """Base error for immutable DevOps artifact storage."""


class ArtifactNotFoundError(ArtifactStoreError, FileNotFoundError):
    """An artifact reference is not present for the authenticated tenant."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Stored bytes do not match their content-addressed reference."""


class ContentAddressedArtifactStore:
    """Local content-addressed adapter with tenant-separated object paths.

    References are immutable (artifact://sha256/<digest> or
    evidence://sha256/<digest>). The tenant is deliberately supplied separately
    on every read/write; identical content in two tenants is stored separately.
    A production object-store adapter can implement the same put/get interface.
    """

    def __init__(self, root: str | Path, *, max_object_bytes: int = 25_000_000):
        if isinstance(max_object_bytes, bool) or not 1 <= max_object_bytes <= 250_000_000:
            raise ValueError("max_object_bytes must be between 1 and 250000000")
        raw_root = Path(root).expanduser()
        if raw_root.exists() and raw_root.is_symlink():
            raise ValueError("artifact root cannot be a symbolic link")
        raw_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not raw_root.is_dir() or raw_root.is_symlink():
            raise ValueError("artifact root must be a real directory")
        self.root = raw_root.resolve(strict=True)
        self.max_object_bytes = max_object_bytes

    @staticmethod
    def _tenant_key(tenant_id: str) -> str:
        if not isinstance(tenant_id, str) or not 1 <= len(tenant_id) <= 128:
            raise ValueError("tenant_id must contain 1..128 characters")
        return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()

    def _object_path(self, tenant_id: str, namespace: str, digest: str) -> Path:
        if namespace not in _ALLOWED_NAMESPACES or not _DIGEST_RE.fullmatch(digest):
            raise ValueError("artifact reference is invalid")
        tenant_root = self.root / self._tenant_key(tenant_id)
        namespace_root = tenant_root / namespace
        shard = namespace_root / digest[:2]
        for directory in (tenant_root, namespace_root, shard):
            if directory.exists() and directory.is_symlink():
                raise ArtifactIntegrityError("artifact storage path contains a symbolic link")
            directory.mkdir(mode=0o700, exist_ok=True)
            if not directory.is_dir() or directory.is_symlink():
                raise ArtifactIntegrityError("artifact storage path is not a real directory")
        target = shard / digest
        if self.root not in target.parents:
            raise ArtifactIntegrityError("artifact path escaped the configured root")
        return target

    def put(self, tenant_id: str, content: bytes, *, namespace: str = "artifact") -> str:
        """Persist bytes once and return an immutable content-addressed reference."""
        if namespace not in _ALLOWED_NAMESPACES:
            raise ValueError("namespace must be artifact or evidence")
        if not isinstance(content, bytes) or not content:
            raise ValueError("artifact content must be non-empty bytes")
        if len(content) > self.max_object_bytes:
            raise ValueError("artifact exceeds configured object size limit")
        digest = hashlib.sha256(content).hexdigest()
        target = self._object_path(tenant_id, namespace, digest)
        fd, temporary_name = tempfile.mkstemp(prefix=".rusa-object-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                # Hard-link publication is atomic and never replaces an existing object.
                os.link(temporary, target)
            except FileExistsError:
                existing = self._read_verified(target, digest)
                if existing != content:
                    raise ArtifactIntegrityError("existing immutable object has conflicting bytes")
        finally:
            temporary.unlink(missing_ok=True)
        return f"{namespace}://sha256/{digest}"

    def put_json(
        self,
        tenant_id: str,
        payload: dict[str, Any],
        *,
        namespace: str = "evidence",
    ) -> str:
        if not isinstance(payload, dict):
            raise ValueError("JSON artifact payload must be an object")
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return self.put(tenant_id, encoded, namespace=namespace)

    def _read_verified(self, path: Path, expected_digest: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise ArtifactIntegrityError("artifact object is not a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError("artifact object was not found") from exc
        with os.fdopen(fd, "rb") as stream:
            content = stream.read(self.max_object_bytes + 1)
        if len(content) > self.max_object_bytes:
            raise ArtifactIntegrityError("artifact object exceeds configured size limit")
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected_digest:
            raise ArtifactIntegrityError("artifact content digest does not match its reference")
        return content

    def get(self, tenant_id: str, reference: str) -> bytes:
        """Read and re-hash an object in the caller's tenant namespace."""
        if not isinstance(reference, str) or len(reference) > 256:
            raise ValueError("artifact reference is invalid")
        parsed = urlsplit(reference)
        if (
            parsed.scheme not in _ALLOWED_NAMESPACES
            or parsed.netloc != "sha256"
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/")
        ):
            raise ValueError("artifact reference must use <namespace>://sha256/<digest>")
        digest = parsed.path[1:]
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError("artifact reference digest is invalid")
        path = self._object_path(tenant_id, parsed.scheme, digest)
        return self._read_verified(path, digest)

    def get_json(self, tenant_id: str, reference: str) -> dict[str, Any]:
        try:
            payload = json.loads(self.get(tenant_id, reference))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactIntegrityError("JSON artifact is malformed") from exc
        if not isinstance(payload, dict):
            raise ArtifactIntegrityError("JSON artifact must contain an object")
        return payload

"""High-confidence credential-material checks before document text is embedded."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

_PATTERNS = (
    (
        re.compile(r"-----BEGIN (?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----"),
        "private-key material",
    ),
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]{20,}?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "private-key material",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}(?![A-Za-z0-9])|github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_]))"),
        "GitHub credential",
    ),
    (
        re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
        "provider API credential",
    ),
    (re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])"), "AWS access-key identifier"),
    (re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"), "Google API credential"),
    (re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[0-9A-Za-z-]{10,}(?![A-Za-z0-9-])"), "Slack credential"),
)


def reject_credential_material(text: str) -> None:
    """Reject common high-confidence credential patterns without echoing matched content."""
    if not isinstance(text, str):
        raise TypeError("document text must be a string")
    for pattern, label in _PATTERNS:
        if pattern.search(text):
            raise ValueError(f"document contains {label}; ingestion was blocked")


def validate_document_metadata(source_uri: str, title: str, metadata: dict) -> str:
    """Validate metadata before embedding or persistence; never store embedded credentials."""
    if not isinstance(metadata, dict):
        raise ValueError("document metadata must be a JSON object")
    try:
        parsed = urlsplit(source_uri)
    except ValueError as exc:
        raise ValueError("source_uri is invalid") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("source_uri must not contain embedded credentials")
    try:
        metadata_json = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("document metadata must be JSON-serializable") from exc
    if len(metadata_json.encode("utf-8")) > 16_384:
        raise ValueError("document metadata exceeds 16 KiB")
    reject_credential_material("\n".join((source_uri, title, metadata_json)))
    return metadata_json

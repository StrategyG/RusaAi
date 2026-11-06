"""High-confidence credential-material checks before document text is embedded."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

_PATTERNS = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]{20,}?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "private-key material",
    ),
    (
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
        "GitHub credential",
    ),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access-key identifier"),
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
    reject_credential_material("\\n".join((source_uri, title, metadata_json)))
    return metadata_json

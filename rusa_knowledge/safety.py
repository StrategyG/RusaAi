"""High-confidence credential-material checks before document text is embedded."""

from __future__ import annotations

import re

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

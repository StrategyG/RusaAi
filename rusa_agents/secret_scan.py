"""Bounded high-confidence credential-content scanner for sandbox snapshots."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import BinaryIO

_SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
    re.compile(rb"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def contains_high_confidence_secret_stream(source: BinaryIO) -> bool:
    """Scan a seekable binary stream in bounded chunks without returning secret data."""
    original_position = source.tell()
    overlap = b""
    found = False
    try:
        source.seek(0)
        while chunk := source.read(64 * 1024):
            sample = overlap + chunk
            if any(pattern.search(sample) for pattern in _SECRET_PATTERNS):
                found = True
                break
            overlap = sample[-256:]
    finally:
        source.seek(original_position)
    return found


def contains_high_confidence_secret(path: str | Path) -> bool:
    """Open without following a symlink and scan in bounded chunks."""
    descriptor = os.open(
        Path(path),
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, "rb") as source:
        return contains_high_confidence_secret_stream(source)


def contains_high_confidence_secret_text(value: str | bytes) -> bool:
    """Inspect text before it is returned to or persisted from an agent."""
    if isinstance(value, str):
        try:
            content = value.encode("utf-8")
        except UnicodeError:
            return True
    elif isinstance(value, bytes):
        content = value
    else:
        raise TypeError("secret scanner accepts only text or bytes")
    return any(pattern.search(content) for pattern in _SECRET_PATTERNS)

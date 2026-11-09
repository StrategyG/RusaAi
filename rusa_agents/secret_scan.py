"""Bounded high-confidence credential-content scanner for sandbox snapshots."""
from __future__ import annotations

import os
import re
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"AIza[0-9A-Za-z_-]{35}"),
    re.compile(rb"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def contains_high_confidence_secret(path: str | Path) -> bool:
    """Scan a file in bounded chunks; return only a Boolean, never the matched value."""
    overlap = b""
    descriptor = os.open(
        Path(path),
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, "rb") as source:
        while chunk := source.read(64 * 1024):
            sample = overlap + chunk
            if any(pattern.search(sample) for pattern in _SECRET_PATTERNS):
                return True
            overlap = sample[-256:]
    return False

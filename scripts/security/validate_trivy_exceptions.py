#!/usr/bin/env python3
"""Validate explicit, mitigated, and time-bounded Trivy exceptions."""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IGNORE = ROOT / ".trivyignore"
REGISTER = ROOT / "docs" / "SECURITY_EXCEPTIONS.md"
CVE_PATTERN = re.compile(r"^CVE-\d{4}-\d{4,}$")


def fail(message: str) -> None:
    print(f"Trivy exception validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    text = REGISTER.read_text(encoding="utf-8")
    review_match = re.search(r"Review date:\*\*\s*(\d{4}-\d{2}-\d{2})", text)
    expiry_match = re.search(r"Expiry:\*\*\s*(\d{4}-\d{2}-\d{2})", text)
    if not review_match or not expiry_match:
        fail("security exception register must declare review and expiry dates")

    review_date = date.fromisoformat(review_match.group(1))
    expiry_date = date.fromisoformat(expiry_match.group(1))
    today = date.today()
    if review_date > today:
        fail("review date cannot be in the future")
    if expiry_date <= today:
        fail(f"exception register expired on {expiry_date.isoformat()}")
    if expiry_date > review_date + timedelta(days=30):
        fail("exception window cannot exceed 30 days")

    ignored = {
        line.strip()
        for line in IGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not ignored or any(not CVE_PATTERN.fullmatch(item) for item in ignored):
        fail("root .trivyignore must contain valid CVE IDs only")

    rows = [
        line for line in text.splitlines()
        if line.startswith("| CVE-")
    ]
    documented = set()
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) < 3 or not cells[2]:
            fail(f"missing mitigation in exception row: {row}")
        documented.add(cells[0])

    if len(documented) != len(rows):
        fail("security exception register contains duplicate CVE IDs")
    if ignored != documented:
        fail("root .trivyignore IDs do not exactly match documented CVEs")

    print(f"Validated {len(ignored)} time-bound Trivy exceptions; expiry {expiry_date.isoformat()}.")


if __name__ == "__main__":
    main()

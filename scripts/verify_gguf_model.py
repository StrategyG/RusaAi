#!/usr/bin/env python3
"""Verify the pinned GGUF model artifact before starting the CPU Compose stack."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rusa_agents.gguf import verify_gguf_file  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=os.getenv("RUSA_GGUF_MODEL_PATH"))
    parser.add_argument("--sha256", default=os.getenv("RUSA_GGUF_MODEL_SHA256"))
    args = parser.parse_args()
    if not args.model:
        parser.error("provide --model or set RUSA_GGUF_MODEL_PATH")
    if not args.sha256:
        parser.error("provide --sha256 or set RUSA_GGUF_MODEL_SHA256")

    try:
        verified = verify_gguf_file(args.model, args.sha256)
    except (OSError, ValueError) as exc:
        print(f"GGUF verification failed: {exc}", file=sys.stderr)
        return 1

    print(
        "GGUF verified: "
        f"path={verified.path} size_bytes={verified.size_bytes} sha256={verified.sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

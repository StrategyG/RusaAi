"""CLI entry point for a deterministic, non-executing repository quality audit."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .analysis import analyze_workspace


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--min-score", type=float, default=85.0)
    parser.add_argument("--files", nargs="*", default=None)
    args = parser.parse_args(argv)
    try:
        report = analyze_workspace(
            args.workspace,
            paths=args.files,
            min_score=args.min_score,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(report.to_dict(), sort_keys=True, ensure_ascii=False))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())

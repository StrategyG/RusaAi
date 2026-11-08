"""Command-line entry points for release-manifest and certification checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from rusa_agents.certification.contracts import (
    CertificationContractError,
    evaluate_production_certification,
    validate_release_manifest,
)

EXIT_PASS = 0
EXIT_HOLD = 2
EXIT_FAIL = 3
EXIT_INPUT_ERROR = 4


def _read_json(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON document {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON document {path} must contain an object at its root")
    return payload


def _parse_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--now must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must include a timezone")
    return parsed


def _manifest_digest(path: str) -> str:
    try:
        content = Path(path).read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read release manifest {path}: {exc}") from exc
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rusa-certify",
        description="Validate RUSA-AI release manifests and production certification evidence.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser(
        "validate-release-manifest",
        help="validate a release manifest and print its exact-byte SHA-256 digest",
    )
    manifest.add_argument("--manifest", required=True, help="path to release-manifest.json")
    manifest.add_argument("--repository", required=True, help="expected owner/repository")
    manifest.add_argument("--commit-sha", required=True, help="expected 40-character source SHA")
    manifest.add_argument("--now", help="optional timezone-aware ISO-8601 evaluation time")

    certification = commands.add_parser(
        "evaluate",
        help="evaluate a production certification evidence record",
    )
    certification.add_argument("--record", required=True, help="path to certification-record.json")
    certification.add_argument("--repository", required=True, help="expected owner/repository")
    certification.add_argument("--commit-sha", required=True, help="expected 40-character candidate SHA")
    certification.add_argument(
        "--manifest-digest",
        required=True,
        help="expected immutable SHA-256 digest of the exact release manifest bytes",
    )
    certification.add_argument("--now", help="optional timezone-aware ISO-8601 evaluation time")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a certification command and return a stable process exit code."""
    args = _parser().parse_args(argv)
    try:
        now = _parse_now(args.now)
        if args.command == "validate-release-manifest":
            payload = _read_json(args.manifest)
            validate_release_manifest(
                payload,
                expected_repository=args.repository,
                expected_commit_sha=args.commit_sha,
                now=now,
            )
            report = {
                "status": "PASS",
                "release_id": payload["release_id"],
                "repository": payload["source"]["repository"],
                "commit_sha": payload["source"]["commit_sha"],
                "manifest_digest": _manifest_digest(args.manifest),
            }
            print(json.dumps(report, sort_keys=True))
            return EXIT_PASS

        payload = _read_json(args.record)
        report = evaluate_production_certification(
            payload,
            expected_commit_sha=args.commit_sha,
            expected_release_manifest_digest=args.manifest_digest,
            expected_repository=args.repository,
            now=now,
        )
        print(json.dumps(report, sort_keys=True))
        return {
            "PASS": EXIT_PASS,
            "HOLD": EXIT_HOLD,
            "FAIL": EXIT_FAIL,
        }[report["status"]]
    except CertificationContractError as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return EXIT_FAIL
    except ValueError as exc:
        print(json.dumps({"status": "INPUT_ERROR", "error": str(exc)}, sort_keys=True))
        return EXIT_INPUT_ERROR


if __name__ == "__main__":
    sys.exit(main())

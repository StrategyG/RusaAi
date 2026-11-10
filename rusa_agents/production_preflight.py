"""Fail-closed preflight for the production Compose deployment.

This validates the production runtime configuration and binds every deployable
image to the repository-owned GHCR path by immutable SHA-256 digest. It does not
verify Cosign signatures or provenance, deploy services, or certify infrastructure.
"""
from __future__ import annotations

import os
import re
import sys

from dotenv import load_dotenv

from .config import RuntimeConfig

_EXPECTED_IMAGES = {
    "RUSA_API_IMAGE": "ghcr.io/strategyg/rusa-ai-api",
    "RUSA_WORKER_IMAGE": "ghcr.io/strategyg/rusa-ai-worker",
    "RUSA_WEB_IMAGE": "ghcr.io/strategyg/rusa-ai-web",
    "RUSA_SANDBOX_IMAGE": "ghcr.io/strategyg/rusa-ai-sandbox",
}
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


def validate_production_environment() -> list[str]:
    """Return configuration errors without printing or exposing secret values."""
    errors: list[str] = []
    if os.getenv("ENVIRONMENT", "").lower() != "production":
        errors.append("ENVIRONMENT must be set to production")

    for variable, expected_repository in _EXPECTED_IMAGES.items():
        image = os.getenv(variable, "")
        if not image:
            errors.append(f"{variable} is required")
            continue
        repository, separator, digest = image.partition("@")
        if not separator or repository != expected_repository or not _SHA256_RE.fullmatch(digest):
            errors.append(
                f"{variable} must use {expected_repository}@sha256:<64 lowercase hex characters>"
            )

    web_secret = os.getenv("RUSA_WEB_SESSION_SECRET", "")
    if (
        len(web_secret.encode("utf-8")) < 32
        or RuntimeConfig._looks_placeholder(web_secret)
    ):
        errors.append(
            "RUSA_WEB_SESSION_SECRET must be a real secret of at least 32 UTF-8 bytes"
        )

    try:
        config = RuntimeConfig.from_env()
    except ValueError as exc:
        errors.append(f"runtime configuration: {exc}")
        return errors

    for label, validator in (
        ("API configuration", config.validate_api),
        ("worker configuration", config.validate_worker),
    ):
        try:
            validator()
        except RuntimeError as exc:
            errors.append(f"{label}: {exc}")

    return errors


def main() -> int:
    # Compose uses .env for interpolation; load the same file for this process
    # without overriding secrets already injected by the deployment environment.
    load_dotenv(override=False)
    errors = validate_production_environment()
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Production environment preflight passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

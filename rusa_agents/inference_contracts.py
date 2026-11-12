"""Runtime validation for the versioned RusaAi inference envelopes."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_ROOT = (
    Path(__file__).resolve().parents[1]
    / "shared"
    / "contracts"
    / "inference_contract"
)


@lru_cache(maxsize=2)
def _validator(schema_name: str) -> jsonschema.Draft202012Validator:
    if schema_name not in {
        "inference-request.v1.schema.json",
        "inference-response.v1.schema.json",
    }:
        raise ValueError("Unknown RusaAi inference contract schema")
    schema_path = SCHEMA_ROOT / schema_name
    if not schema_path.is_file():
        raise RuntimeError(f"RusaAi inference contract is missing: {schema_name}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    )


def _errors(payload: Any, schema_name: str) -> list[str]:
    validator = _validator(schema_name)
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
        for error in errors
    ]


def validate_inference_request(payload: Any) -> list[str]:
    """Return deterministic validation errors for an inference request envelope."""
    return _errors(payload, "inference-request.v1.schema.json")


def validate_inference_response(payload: Any) -> list[str]:
    """Return deterministic validation errors for an inference response envelope."""
    return _errors(payload, "inference-response.v1.schema.json")

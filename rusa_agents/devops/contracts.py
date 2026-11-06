"""JSON Schema validation for the Agentic DevOps task/result/event contracts."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema.exceptions import SchemaError
from jsonschema import Draft202012Validator, FormatChecker


_REPOSITORY_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas" / "devops"
_PACKAGED_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
_SCHEMA_FILES = {
    "agent_task": "agent-task.schema.json",
    "agent_result": "agent-result.schema.json",
    "workflow_event": "workflow-event.schema.json",
}


class ContractValidationError(ValueError):
    """Raised when a DevOps contract payload does not match its versioned schema."""


@lru_cache(maxsize=len(_SCHEMA_FILES))
def _validator(contract_name: str) -> Draft202012Validator:
    try:
        filename = _SCHEMA_FILES[contract_name]
    except KeyError as exc:
        raise ValueError(f"unknown DevOps contract: {contract_name}") from exc

    repository_path = _REPOSITORY_SCHEMA_DIR / filename
    packaged_path = _PACKAGED_SCHEMA_DIR / filename
    schema_path = repository_path if repository_path.is_file() else packaged_path
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load DevOps contract schema {filename}") from exc

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise RuntimeError(f"invalid DevOps contract schema {filename}: {exc.message}") from exc

    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_contract(contract_name: str, payload: dict[str, Any]) -> None:
    """Validate a JSON-compatible payload against one versioned DevOps contract."""
    if not isinstance(payload, dict):
        raise ContractValidationError("contract payload must be a JSON object")

    validator = _validator(contract_name)
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: "
            f"{error.message}"
            for error in errors[:20]
        )
        raise ContractValidationError(
            f"{contract_name} contract validation failed: {details}"
        )


def validate_agent_task(payload: dict[str, Any]) -> None:
    validate_contract("agent_task", payload)


def validate_agent_result(payload: dict[str, Any]) -> None:
    validate_contract("agent_result", payload)


def validate_workflow_event(payload: dict[str, Any]) -> None:
    validate_contract("workflow_event", payload)

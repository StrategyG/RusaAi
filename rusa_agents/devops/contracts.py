"""JSON Schema validation for the Agentic DevOps task/result/event contracts."""

from __future__ import annotations  # noqa: I001

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema


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
def _validator(contract_name: str) -> jsonschema.Draft202012Validator:
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
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        raise RuntimeError(f"invalid DevOps contract schema {filename}: {exc.message}") from exc

    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


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


def validate_agent_result_for_task(
    task: dict[str, Any], result: dict[str, Any]
) -> None:
    """Validate result identity and immutable evidence against its originating task."""
    validate_agent_task(task)
    validate_agent_result(result)

    for field in ("task_id", "workflow_id", "tenant_id", "agent_id", "stage", "attempt"):
        if task.get(field) != result.get(field):
            raise ContractValidationError(
                f"agent result does not match originating task field: {field}"
            )

    task_input = task.get("input", {})
    result_audit = result.get("audit", {})
    expected_commit = task_input.get("commit_sha")
    if expected_commit is not None and result_audit.get("source_commit") != expected_commit:
        raise ContractValidationError(
            "agent result source_commit does not match the task commit_sha"
        )

    expected_digest = task_input.get("artifact_digest")
    if expected_digest is not None and result_audit.get("artifact_digest") != expected_digest:
        raise ContractValidationError(
            "agent result artifact_digest does not match the task artifact_digest"
        )

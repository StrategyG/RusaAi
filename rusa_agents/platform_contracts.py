"""Versioned, transport-neutral contracts for cross-domain RUSA-AI runs.

These contracts define payload shape and ordering invariants only. They do not
implement a broker, event store, signature verification, or agent authorization.
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "platform"
_SCHEMA_FILES = {
    "agent_task": "agent-task-v1.schema.json",
    "agent_result": "agent-result-v1.schema.json",
    "run_event": "run-event-v1.schema.json",
}


class PlatformContractError(ValueError):
    """Raised when a versioned RUSA-AI platform contract is invalid."""


@lru_cache(maxsize=len(_SCHEMA_FILES))
def _validator(contract_name: str) -> jsonschema.Draft202012Validator:
    try:
        filename = _SCHEMA_FILES[contract_name]
    except KeyError as exc:
        raise ValueError(f"unknown platform contract: {contract_name}") from exc
    try:
        schema = json.loads((_SCHEMA_DIR / filename).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
    except (OSError, json.JSONDecodeError, jsonschema.exceptions.SchemaError) as exc:
        raise RuntimeError(f"cannot load platform contract schema: {filename}") from exc
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


def _validate(contract_name: str, payload: Any) -> None:
    if not isinstance(payload, dict):
        raise PlatformContractError("contract payload must be a JSON object")
    errors = sorted(
        _validator(contract_name).iter_errors(payload),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            error.message,
        ),
    )
    if errors:
        messages = [
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: "
            f"{error.message}"
            for error in errors[:25]
        ]
        raise PlatformContractError(
            f"{contract_name} contract validation failed: " + "; ".join(messages)
        )


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise PlatformContractError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PlatformContractError(f"{field} must include a timezone")
    return parsed


def validate_agent_task(payload: Any) -> None:
    """Validate a dispatchable task and its authorization/deadline envelope."""
    _validate("agent_task", payload)
    created = _parse_timestamp(payload["created_at"], "created_at")
    deadline = _parse_timestamp(payload["deadline_at"], "deadline_at")
    if deadline <= created:
        raise PlatformContractError("deadline_at must be later than created_at")


def validate_agent_result(payload: Any) -> None:
    """Validate a result and ensure its execution interval is chronological."""
    _validate("agent_result", payload)
    started = _parse_timestamp(payload["started_at"], "started_at")
    completed = _parse_timestamp(payload["completed_at"], "completed_at")
    if completed < started:
        raise PlatformContractError("completed_at cannot be earlier than started_at")
    if payload["status"] == "succeeded":
        failures = [
            item for item in payload["evidence"] if item["status"] != "pass"
        ]
        if failures:
            raise PlatformContractError(
                "a succeeded result cannot contain failed or held evidence"
            )


def validate_run_event(payload: Any) -> None:
    """Validate one event envelope; sequence allocation remains an event-store duty."""
    _validate("run_event", payload)


def validate_run_event_batch(events: Any) -> None:
    """Validate one ordered event page without pretending to verify signatures/replay."""
    if not isinstance(events, list) or not events:
        raise PlatformContractError("event batch must be a non-empty list")
    for event in events:
        validate_run_event(event)

    first = events[0]
    seen_ids: set[str] = set()
    previous_sequence = 0
    previous_state_version = 0
    for event in events:
        if event["run_id"] != first["run_id"]:
            raise PlatformContractError("event batch cannot mix run IDs")
        if event["tenant_id"] != first["tenant_id"]:
            raise PlatformContractError("event batch cannot mix tenant IDs")
        if event["correlation_id"] != first["correlation_id"]:
            raise PlatformContractError("event batch cannot mix correlation IDs")
        if event["event_id"] in seen_ids:
            raise PlatformContractError("event IDs must be unique within a batch")
        if event["sequence"] <= previous_sequence:
            raise PlatformContractError("event sequence must be strictly increasing")
        if event["state_version"] < previous_state_version:
            raise PlatformContractError("state_version cannot move backwards")
        seen_ids.add(event["event_id"])
        previous_sequence = event["sequence"]
        previous_state_version = event["state_version"]

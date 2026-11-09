"""Versioned, transport-neutral contracts for cross-domain RUSA-AI runs.

These contracts define payload shape and ordering invariants only. They do not
implement a broker, event store, signature verification, or agent authorization.
"""
from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
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


class PlatformDomain(str, Enum):
    RUSA_INTELLIGENCE = "rusa_intelligence"
    AUTONOMOUS_SOFTWARE_ENGINEERING = "autonomous_software_engineering"
    RUSA_BUSINESS = "rusa_business"
    RUSA_ECOSYSTEM = "rusa_ecosystem"


class CoreAgentRole(str, Enum):
    SUPREME_ORCHESTRATOR = "supreme_orchestrator"
    SYSTEM_ARCHITECT = "system_architect"
    PLANNER = "planner"
    TASK_DECOMPOSER = "task_decomposer"
    SOFTWARE_ENGINEER = "software_engineer"
    DEVOPS = "devops"
    QA_TESTING = "qa_testing"
    SECURITY_POLICY = "security_policy"
    MEMORY_KNOWLEDGE = "memory_knowledge"
    EVALUATION_REPORTING = "evaluation_reporting"


class AgentType(str, Enum):
    CORE_AGENT = "core_agent"
    DEVOPS_STAGE_AGENT = "devops_stage_agent"


class DevOpsStage(str, Enum):
    PLAN = "plan"
    CODE = "code"
    BUILD = "build"
    TEST = "test"
    RELEASE = "release"
    DEPLOY = "deploy"
    OPERATE = "operate"
    MONITOR = "monitor"


class DevOpsStageAgentId(str, Enum):
    PLAN = "rusa_plan_agent"
    CODE = "rusa_code_agent"
    BUILD = "rusa_build_agent"
    TEST = "rusa_test_agent"
    RELEASE = "rusa_release_agent"
    DEPLOY = "rusa_deploy_agent"
    OPERATE = "rusa_operate_agent"
    MONITOR = "rusa_monitor_agent"


_STAGE_AGENT_STAGE = {
    DevOpsStageAgentId.PLAN.value: DevOpsStage.PLAN.value,
    DevOpsStageAgentId.CODE.value: DevOpsStage.CODE.value,
    DevOpsStageAgentId.BUILD.value: DevOpsStage.BUILD.value,
    DevOpsStageAgentId.TEST.value: DevOpsStage.TEST.value,
    DevOpsStageAgentId.RELEASE.value: DevOpsStage.RELEASE.value,
    DevOpsStageAgentId.DEPLOY.value: DevOpsStage.DEPLOY.value,
    DevOpsStageAgentId.OPERATE.value: DevOpsStage.OPERATE.value,
    DevOpsStageAgentId.MONITOR.value: DevOpsStage.MONITOR.value,
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
        # jsonschema's default error.message may echo the invalid value. Contract
        # payloads can contain credential-like material, so expose only the path
        # and schema keyword in diagnostics.
        messages = []
        for error in errors[:25]:
            location = ".".join(str(part) for part in error.absolute_path) or "<root>"
            if error.validator == "required" and isinstance(error.instance, dict):
                missing = [
                    field for field in error.validator_value if field not in error.instance
                ]
                detail = f"missing required field(s): {', '.join(missing)}"
            else:
                detail = f"violates {error.validator or 'schema'} constraint"
            messages.append(f"{location}: {detail}")
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
    agent = payload["agent"]
    if agent["agent_type"] == AgentType.DEVOPS_STAGE_AGENT.value:
        if agent["agent_id"] != agent["stage_agent_id"]:
            raise PlatformContractError(
                "stage agent_id must match its canonical stage_agent_id"
            )
        expected_stage = _STAGE_AGENT_STAGE[agent["stage_agent_id"]]
        if payload.get("stage") != expected_stage:
            raise PlatformContractError(
                "DevOps stage-agent identity must match the task lifecycle stage"
            )
    created = _parse_timestamp(payload["created_at"], "created_at")
    deadline = _parse_timestamp(payload["deadline_at"], "deadline_at")
    if deadline <= created:
        raise PlatformContractError("deadline_at must be later than created_at")


def validate_agent_result(payload: Any) -> None:
    """Validate a result and ensure its execution interval is chronological."""
    if isinstance(payload, dict):
        if payload.get("status") == "succeeded" and not payload.get("evidence"):
            raise PlatformContractError(
                "a succeeded result requires mandatory verification evidence"
            )
        if payload.get("status") == "failed" and not payload.get("error"):
            raise PlatformContractError(
                "failed results require a structured error object"
            )
    _validate("agent_result", payload)
    started = _parse_timestamp(payload["started_at"], "started_at")
    completed = _parse_timestamp(payload["completed_at"], "completed_at")
    if completed < started:
        raise PlatformContractError("completed_at cannot be earlier than started_at")
    if payload["status"] == "succeeded":
        if not payload["evidence"]:
            raise PlatformContractError(
                "a succeeded result requires mandatory verification evidence"
            )
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
    actor = payload["actor"]
    if actor["actor_type"] == "agent" and actor["agent_type"] == AgentType.DEVOPS_STAGE_AGENT.value:
        if actor["stage_agent_id"] not in {item.value for item in DevOpsStageAgentId}:
            raise PlatformContractError("agent actor must declare a registered stage_agent_id")
    elif actor["actor_type"] == "agent" and actor["agent_type"] == AgentType.CORE_AGENT.value:
        if actor["core_role"] not in {item.value for item in CoreAgentRole}:
            raise PlatformContractError("agent actor must declare a registered core_role")


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

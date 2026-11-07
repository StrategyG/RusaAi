"""Safe, in-process DevOps workflow simulation using dummy agents only.

This module deliberately has no queue, network client, repository writer, deployer,
or production credential integration. It exercises the versioned contracts and
handoff rules before specialist execution is introduced.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .contracts import (
    validate_agent_result_for_task,
    validate_agent_task,
    validate_workflow_event,
)

STAGES = ("plan", "code", "build", "test", "release", "deploy", "operate", "monitor")
AGENT_BY_STAGE = {
    "plan": "rusa_plan_agent",
    "code": "rusa_code_agent",
    "build": "rusa_build_agent",
    "test": "rusa_test_agent",
    "release": "rusa_release_agent",
    "deploy": "rusa_deploy_agent",
    "operate": "rusa_operate_agent",
    "monitor": "rusa_monitor_agent",
}
DOMAIN_EVENT_BY_STAGE = {
    "plan": "plan.completed",
    "code": "code.completed",
    "build": "build.completed",
    "test": "test.completed",
    "release": "release.approved",
    "deploy": "deploy.completed",
    "operate": "operate.recovered",
    "monitor": "monitor.alert_triggered",
}
SECURITY_GUARD_ID = "rusa_security_guard_agent"
DEFAULT_SOURCE_COMMIT = "a" * 40
DEFAULT_ARTIFACT_DIGEST = "sha256:" + "b" * 64


@dataclass
class DevOpsDryRunReport:
    workflow_id: str
    tenant_id: str
    status: str
    tasks: list[dict[str, Any]] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    feedback_request: dict[str, Any] | None = None
    external_side_effects: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat().replace("+00:00", "Z")


def _traceparent() -> str:
    return f"00-{uuid4().hex}-{uuid4().hex[:16]}-01"


def _sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _result_ref(task: Mapping[str, Any]) -> str:
    return f"artifact://dry-run/results/{task['task_id']}/{task['attempt']}"


class SyntheticDevOpsAgent:
    """Deterministic fixture agent; it never invokes tools or external services."""

    def __init__(
        self,
        *,
        status_by_stage: Mapping[str, str] | None = None,
        monitor_feedback: bool = False,
        source_commit: str = DEFAULT_SOURCE_COMMIT,
        source_commit_by_stage: Mapping[str, str] | None = None,
        artifact_digest: str = DEFAULT_ARTIFACT_DIGEST,
    ):
        self.status_by_stage = dict(status_by_stage or {})
        self.monitor_feedback = monitor_feedback
        self.source_commit = source_commit
        self.source_commit_by_stage = dict(source_commit_by_stage or {})
        self.artifact_digest = artifact_digest

    def run(self, task: dict[str, Any]) -> dict[str, Any]:
        stage = task["stage"]
        task_input = task["input"]
        constraints = task["constraints"]
        status = self.status_by_stage.get(stage, "passed")
        if (
            status == "passed"
            and constraints["approval_required"]
            and not constraints.get("approval_ref")
        ):
            status = "needs_approval"

        if status not in {
            "passed", "failed", "blocked", "needs_approval", "cancelled", "skipped"
        }:
            raise ValueError(f"unsupported synthetic result status: {status}")

        evidence_ref = f"evidence://dry-run/{stage}/{task['task_id']}"
        artifact_ref = f"artifact://dry-run/{stage}/{task['task_id']}"
        findings: list[dict[str, str]] = []
        if status in {"failed", "blocked"}:
            findings.append({
                "severity": "high",
                "code": f"{stage}.synthetic_gate_failed",
                "message": f"Synthetic {stage} gate was configured to {status}.",
                "evidence_ref": evidence_ref,
            })
        elif stage == "monitor" and self.monitor_feedback and status == "passed":
            findings.append({
                "severity": "info",
                "code": "monitor.feedback_created",
                "message": "Synthetic monitoring detected an improvement opportunity.",
                "evidence_ref": evidence_ref,
            })

        output: dict[str, Any] = {
            "summary": f"Synthetic {stage} result: {status}. No external action was performed.",
            "artifact_refs": [artifact_ref],
            "evidence_refs": [evidence_ref],
            "findings": findings,
        }
        if status == "needs_approval":
            output["approval_ref"] = f"approval://pending/{stage}/{task['task_id']}"

        audit: dict[str, Any] = {
            "started_at": _timestamp(),
            "completed_at": _timestamp(),
            "policy_version": task["audit"]["policy_version"],
            "model_id": "synthetic/dry-run-agent",
        }
        if stage == "code" or "commit_sha" in task_input:
            audit["source_commit"] = self.source_commit_by_stage.get(
                stage, task_input.get("commit_sha", self.source_commit)
            )
        if stage in {"build", "test", "release", "deploy"} or "artifact_digest" in task_input:
            audit["artifact_digest"] = task_input.get("artifact_digest", self.artifact_digest)

        return {
            "schema_version": 1,
            "task_id": task["task_id"],
            "workflow_id": task["workflow_id"],
            "tenant_id": task["tenant_id"],
            "agent_id": task["agent_id"],
            "stage": stage,
            "attempt": task["attempt"],
            "status": status,
            "output": output,
            "audit": audit,
        }


class DevOpsDryRunOrchestrator:
    """Runs Plan-to-Monitor in process against synthetic agents and fixtures."""

    def __init__(
        self,
        *,
        tenant_id: str = "tenant-dry-run",
        requested_by: str = "dry-run-operator",
        policy_version: str = "dry-run-policy-v1",
        environment: str = "staging",
        approval_refs: Mapping[str, str] | None = None,
        agents: Mapping[str, SyntheticDevOpsAgent] | None = None,
    ):
        if environment not in {"staging", "production"}:
            raise ValueError("dry-run environment must be staging or production")
        self.tenant_id = tenant_id
        self.requested_by = requested_by
        self.policy_version = policy_version
        self.environment = environment
        self.approval_refs = dict(approval_refs or {})
        self.agents: dict[str, SyntheticDevOpsAgent] = {
            **{agent_id: SyntheticDevOpsAgent() for agent_id in AGENT_BY_STAGE.values()},
            SECURITY_GUARD_ID: SyntheticDevOpsAgent(),
        }
        for agent_id, agent in (agents or {}).items():
            if agent_id not in self.agents:
                raise ValueError(f"unknown dry-run agent identity: {agent_id}")
            if type(agent) is not SyntheticDevOpsAgent:
                raise TypeError("dry-run accepts SyntheticDevOpsAgent fixtures only")
            self.agents[agent_id] = agent

    def _task(
        self,
        *,
        workflow_id: str,
        traceparent: str,
        stage: str,
        agent_id: str,
        objective: str,
        task_input: dict[str, Any],
        approval_required: bool = False,
        approval_scope: list[str] | None = None,
        approval_ref: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        constraints: dict[str, Any] = {
            "allowed_tools": [],
            "network_access": False,
            "allowed_network_destinations": [],
            "write_scope": [],
            "approval_required": approval_required,
            "approval_scope": approval_scope or [],
            "budget": {
                "max_cost_usd": 0,
                "max_tool_calls": 0,
                "max_runtime_seconds": 600,
            },
        }
        if approval_ref:
            constraints["approval_ref"] = approval_ref
        return {
            "schema_version": 1,
            "task_id": str(uuid4()),
            "workflow_id": workflow_id,
            "tenant_id": self.tenant_id,
            "agent_id": agent_id,
            "stage": stage,
            "objective": objective,
            "input": task_input,
            "constraints": constraints,
            "attempt": 1,
            "deadline_at": _timestamp(now + timedelta(minutes=10)),
            "audit": {
                "created_at": _timestamp(now),
                "requested_by": self.requested_by,
                "policy_version": self.policy_version,
                "traceparent": traceparent,
                "model_id": "synthetic/dry-run-agent",
            },
        }

    def _event(
        self,
        *,
        event_type: str,
        workflow_id: str,
        correlation_id: str,
        traceparent: str,
        producer: str,
        payload: dict[str, Any],
        task_id: str | None = None,
        attempt: int | None = None,
        causation_id: str | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": 1,
            "type": event_type,
            "message_id": str(uuid4()),
            "tenant_id": self.tenant_id,
            "run_id": workflow_id,
            "correlation_id": correlation_id,
            "traceparent": traceparent,
            "producer": producer,
            "occurred_at": _timestamp(),
            "payload": payload,
        }
        if task_id is not None:
            event["task_id"] = task_id
        if attempt is not None:
            event["attempt"] = attempt
        if causation_id is not None:
            event["causation_id"] = causation_id
        validate_workflow_event(event)
        return event

    def _blocked_event(
        self,
        *,
        workflow_id: str,
        correlation_id: str,
        traceparent: str,
        stage: str,
        reason_code: str,
        state_version: int = 1,
    ) -> dict[str, Any]:
        return self._event(
            event_type="workflow.blocked",
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            traceparent=traceparent,
            producer="rusa_devops_orchestrator",
            payload={
                "state_version": state_version,
                "stage": stage,
                "reason_code": reason_code,
            },
        )

    @staticmethod
    def _stage_input(
        stage: str,
        *,
        user_request: str,
        repository: str,
        acceptance_criteria: list[str],
        service: str,
        metrics_window: str,
        workflow_id: str,
        commit_sha: str,
        artifact_digest: str,
        plan_ref: str | None,
        test_evidence_ref: str | None,
        release_manifest_ref: str | None,
        environment: str,
    ) -> dict[str, Any]:
        if stage == "plan":
            return {"user_request": user_request}
        if stage == "code":
            return {
                "repository": repository,
                "plan_ref": plan_ref or f"artifact://dry-run/plans/{workflow_id}",
                "acceptance_criteria": acceptance_criteria,
            }
        if stage == "build":
            return {
                "repository": repository,
                "commit_sha": commit_sha,
                "build_config_ref": f"artifact://dry-run/build-config/{workflow_id}",
            }
        if stage == "test":
            return {
                "commit_sha": commit_sha,
                "artifact_digest": artifact_digest,
                "test_plan_ref": f"artifact://dry-run/test-plan/{workflow_id}",
                "acceptance_criteria_ref": f"artifact://dry-run/criteria/{workflow_id}",
            }
        if stage == "release":
            return {
                "commit_sha": commit_sha,
                "artifact_digest": artifact_digest,
                "test_evidence_ref": test_evidence_ref or f"evidence://dry-run/tests/{workflow_id}",
                "sbom_ref": f"artifact://dry-run/sbom/{workflow_id}",
            }
        if stage == "deploy":
            return {
                "release_manifest_ref": release_manifest_ref or f"artifact://dry-run/release/{workflow_id}",
                "environment": environment,
                "commit_sha": commit_sha,
                "artifact_digest": artifact_digest,
            }
        if stage == "operate":
            return {
                "service": service,
                "runbook_ref": f"artifact://dry-run/runbooks/{service}",
            }
        if stage == "monitor":
            return {"service": service, "metrics_window": metrics_window}
        raise ValueError(f"unknown DevOps stage: {stage}")

    def run(
        self,
        *,
        user_request: str,
        repository: str,
        acceptance_criteria: list[str],
        service: str = "rusa-demo",
        metrics_window: str = "last-15m",
    ) -> DevOpsDryRunReport:
        if not user_request.strip():
            raise ValueError("user_request cannot be empty")
        if not acceptance_criteria or any(not item.strip() for item in acceptance_criteria):
            raise ValueError("acceptance_criteria must contain non-empty items")

        workflow_id = str(uuid4())
        correlation_id = str(uuid4())
        traceparent = _traceparent()
        report = DevOpsDryRunReport(
            workflow_id=workflow_id,
            tenant_id=self.tenant_id,
            status="running",
        )
        commit_sha = DEFAULT_SOURCE_COMMIT
        artifact_digest = DEFAULT_ARTIFACT_DIGEST
        plan_ref: str | None = None
        test_evidence_ref: str | None = None
        release_manifest_ref: str | None = None
        monitor_event: dict[str, Any] | None = None

        for stage in STAGES:
            if (
                stage == "deploy"
                and self.environment == "production"
                and not self.approval_refs.get("deploy")
            ):
                report.status = "needs_approval"
                report.blockers.append("Production deployment requires an approval reference.")
                report.events.append(self._blocked_event(
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    stage=stage,
                    reason_code="approval_required",
                    state_version=len(report.events) + 1,
                ))
                return report

            task_input = self._stage_input(
                stage,
                user_request=user_request,
                repository=repository,
                acceptance_criteria=acceptance_criteria,
                service=service,
                metrics_window=metrics_window,
                workflow_id=workflow_id,
                commit_sha=commit_sha,
                artifact_digest=artifact_digest,
                plan_ref=plan_ref,
                test_evidence_ref=test_evidence_ref,
                release_manifest_ref=release_manifest_ref,
                environment=self.environment,
            )
            approval_required = stage in {"release", "deploy"}
            approval_scope = [stage] if approval_required else []
            task = self._task(
                workflow_id=workflow_id,
                traceparent=traceparent,
                stage=stage,
                agent_id=AGENT_BY_STAGE[stage],
                objective=f"Dry-run the {stage} stage against synthetic fixtures.",
                task_input=task_input,
                approval_required=approval_required,
                approval_scope=approval_scope,
                approval_ref=self.approval_refs.get(stage),
            )
            report.tasks.append(task)
            try:
                validate_agent_task(task)
                result = self.agents[task["agent_id"]].run(task)
                validate_agent_result_for_task(task, result)
            except Exception as exc:
                report.status = "blocked"
                report.blockers.append(f"{stage} contract or agent execution failed: {exc}")
                report.events.append(self._blocked_event(
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    stage=stage,
                    reason_code="contract_or_agent_failure",
                    state_version=len(report.events) + 1,
                ))
                return report

            report.results.append(result)
            if result["status"] == "needs_approval":
                report.status = "needs_approval"
                report.blockers.append(f"{stage} is waiting for human approval.")
                report.events.append(self._event(
                    event_type="audit.recorded",
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    producer=task["agent_id"],
                    task_id=task["task_id"],
                    attempt=task["attempt"],
                    payload={
                        "action": "approval.requested",
                        "outcome": "needs_approval",
                        "resource_ref": _result_ref(task),
                        "policy_version": self.policy_version,
                        "state_version": 1,
                    },
                ))
                report.events.append(self._blocked_event(
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    stage=stage,
                    reason_code="approval_required",
                    state_version=len(report.events) + 1,
                ))
                return report

            if result["status"] != "passed":
                report.status = result["status"]
                report.blockers.append(
                    f"{stage} returned {result['status']}; downstream stages were not executed."
                )
                report.events.append(self._event(
                    event_type="task.failed",
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    producer=task["agent_id"],
                    task_id=task["task_id"],
                    attempt=task["attempt"],
                    payload={
                        "lease_epoch": 1,
                        "state_version": 2,
                        "code": f"{stage}_{result['status']}",
                        "category": "POLICY" if result["status"] == "blocked" else "PERMANENT",
                        "side_effect": "NONE",
                    },
                ))
                report.events.append(self._blocked_event(
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    stage=stage,
                    reason_code=f"{stage}_{result['status']}",
                    state_version=len(report.events) + 1,
                ))
                return report

            domain_event = DOMAIN_EVENT_BY_STAGE[stage]
            if stage == "monitor" and any(
                finding["code"] == "monitor.feedback_created"
                for finding in result["output"]["findings"]
            ):
                domain_event = "monitor.feedback_created"

            specialist_event = self._event(
                event_type="task.completed",
                workflow_id=workflow_id,
                correlation_id=correlation_id,
                traceparent=traceparent,
                producer=task["agent_id"],
                task_id=task["task_id"],
                attempt=task["attempt"],
                payload={
                    "lease_epoch": 1,
                    "state_version": 2,
                    "result_ref": _result_ref(task),
                    "sha256": _sha256(result),
                    "stage": stage,
                    "domain_event": domain_event,
                },
            )
            report.events.append(specialist_event)
            if stage == "monitor":
                monitor_event = specialist_event

            if stage == "code":
                commit_sha = result["audit"]["source_commit"]
            if stage == "build":
                artifact_digest = result["audit"]["artifact_digest"]
            if stage == "plan":
                plan_ref = result["output"]["artifact_refs"][0]
            if stage == "test":
                test_evidence_ref = result["output"]["evidence_refs"][0]
            if stage == "release":
                release_manifest_ref = result["output"]["artifact_refs"][0]

            security_input = dict(task_input)
            security_input["evidence_refs"] = list(result["output"]["evidence_refs"])
            if "source_commit" in result["audit"]:
                security_input["commit_sha"] = result["audit"]["source_commit"]
            if "artifact_digest" in result["audit"]:
                security_input["artifact_digest"] = result["audit"]["artifact_digest"]

            security_task = self._task(
                workflow_id=workflow_id,
                traceparent=traceparent,
                stage=stage,
                agent_id=SECURITY_GUARD_ID,
                objective=f"Read-only security and policy review of the {stage} evidence.",
                task_input=security_input,
                approval_required=stage in {"release", "deploy"},
                approval_scope=[stage] if stage in {"release", "deploy"} else [],
                approval_ref=self.approval_refs.get(stage),
            )
            report.tasks.append(security_task)
            try:
                validate_agent_task(security_task)
                security_result = self.agents[SECURITY_GUARD_ID].run(security_task)
                validate_agent_result_for_task(security_task, security_result)
            except Exception as exc:
                report.status = "blocked"
                report.blockers.append(f"Security Guard {stage} review failed: {exc}")
                report.events.append(self._blocked_event(
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    stage=stage,
                    reason_code="security_review_invalid",
                    state_version=len(report.events) + 1,
                ))
                return report

            report.results.append(security_result)
            security_ref = _result_ref(security_task)
            if security_result["status"] != "passed":
                report.status = "blocked"
                report.blockers.append(f"Security Guard blocked the {stage} stage.")
                report.events.append(self._event(
                    event_type="policy.violation",
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    producer=SECURITY_GUARD_ID,
                    task_id=security_task["task_id"],
                    attempt=security_task["attempt"],
                    payload={
                        "policy_id": "devops-security-guard",
                        "policy_version": self.policy_version,
                        "decision_id": str(uuid4()),
                        "action": "security.review",
                        "resource_ref": security_ref,
                        "reason_code": "security_guard_blocked",
                    },
                ))
                report.events.append(self._blocked_event(
                    workflow_id=workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    stage=stage,
                    reason_code="security_guard_blocked",
                    state_version=len(report.events) + 1,
                ))
                return report

            report.events.append(self._event(
                event_type="audit.recorded",
                workflow_id=workflow_id,
                correlation_id=correlation_id,
                traceparent=traceparent,
                producer=SECURITY_GUARD_ID,
                task_id=security_task["task_id"],
                attempt=security_task["attempt"],
                payload={
                    "action": "security.review",
                    "outcome": "passed",
                    "resource_ref": security_ref,
                    "policy_version": self.policy_version,
                    "state_version": 1,
                },
            ))

        report.status = "completed"
        report.events.append(self._event(
            event_type="workflow.completed",
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            traceparent=traceparent,
            producer="rusa_devops_orchestrator",
            payload={
                "state_version": len(STAGES) + 1,
                "result_ref": f"artifact://dry-run/workflows/{workflow_id}/result",
                "sha256": _sha256(report.results),
            },
        ))

        if monitor_event is not None:
            monitor_result = next(
                result for result in reversed(report.results)
                if result["agent_id"] == AGENT_BY_STAGE["monitor"]
            )
            if any(
                finding["code"] == "monitor.feedback_created"
                for finding in monitor_result["output"]["findings"]
            ):
                next_workflow_id = str(uuid4())
                next_task_id = str(uuid4())
                report.feedback_request = self._event(
                    event_type="task.request",
                    workflow_id=next_workflow_id,
                    correlation_id=correlation_id,
                    traceparent=traceparent,
                    producer="rusa_devops_orchestrator",
                    task_id=next_task_id,
                    attempt=1,
                    causation_id=monitor_event["message_id"],
                    payload={
                        "capability": "plan",
                        "input_ref": monitor_result["output"]["artifact_refs"][0],
                        "deadline_at": _timestamp(_now() + timedelta(minutes=10)),
                        "idempotency_key": f"{next_workflow_id}/{next_task_id}/1",
                        "priority": "P2",
                    },
                )
                report.events.append(report.feedback_request)

        return report

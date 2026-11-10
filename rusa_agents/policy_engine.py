"""Deterministic, versioned policy decisions with append-only PostgreSQL records."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

from .platform_contracts import PolicyDecisionV1
from .policy import RiskLevel


@dataclass(frozen=True)
class PolicyRequest:
    tenant_id: str
    authenticated_tenant_id: str
    actor: str
    action: str
    resource_ref: str
    policy_id: str
    policy_version: str
    risk: RiskLevel = RiskLevel.LOW
    requires_approval: bool = False
    workflow_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict)


class PolicyDecisionError(RuntimeError):
    """A policy request could not be safely evaluated or persisted."""


def _reject_secret_fields(value: Any, path: str = "context") -> None:
    blocked = {
        "api_key", "apikey", "access_token", "refresh_token", "authorization",
        "password", "client_secret", "private_key", "secret", "bearer_token",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in blocked:
                raise ValueError(f"credential-shaped policy field is prohibited: {path}.{key}")
            _reject_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class DeterministicPolicyEngine:
    """Evaluate explicit policy inputs; model output is never an authorization input."""

    DENIED_ACTIONS = frozenset({
        "disable_audit", "exfiltrate_secret", "force_push_main",
    })
    APPROVAL_ACTIONS = frozenset({
        "merge", "deploy", "release", "secret_change",
        "permission_change", "destructive_action", "production_write",
        "financial_transaction", "contract_sign",
    })

    def __init__(
        self,
        *,
        policy_id: str,
        policy_version: str,
        allowed_actions: set[str] | frozenset[str],
    ):
        if not policy_id or not policy_version or not allowed_actions:
            raise ValueError("policy identity and a non-empty allowlist are required")
        self.policy_id = policy_id
        self.policy_version = policy_version
        self.allowed_actions = frozenset(allowed_actions)

    def evaluate(self, request: PolicyRequest) -> PolicyDecisionV1:
        for name in ("tenant_id", "authenticated_tenant_id", "actor", "action", "resource_ref"):
            value = getattr(request, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if request.policy_id != self.policy_id or request.policy_version != self.policy_version:
            raise ValueError("policy request version does not match the loaded deterministic policy")
        if not isinstance(request.context, Mapping):
            raise ValueError("policy context must be an object")
        _reject_secret_fields(request.context)

        evaluated_at = datetime.now(timezone.utc)
        canonical_input = {
            "tenant_id": request.tenant_id,
            "authenticated_tenant_id": request.authenticated_tenant_id,
            "actor": request.actor,
            "action": request.action,
            "resource_ref": request.resource_ref,
            "policy_id": request.policy_id,
            "policy_version": request.policy_version,
            "risk": request.risk.value,
            "requires_approval": request.requires_approval,
            "workflow_id": request.workflow_id,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "context": dict(request.context),
        }
        digest = _canonical_digest(canonical_input)

        if request.tenant_id != request.authenticated_tenant_id:
            outcome, reason = "deny", "tenant_mismatch"
        elif request.action in self.DENIED_ACTIONS:
            outcome, reason = "deny", "action_prohibited"
        elif (
            request.requires_approval
            or request.action in self.APPROVAL_ACTIONS
            or request.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}
        ):
            outcome, reason = "require_approval", "human_approval_required"
        elif request.action not in self.allowed_actions:
            outcome, reason = "deny", "action_not_allowlisted"
        else:
            outcome, reason = "allow", "policy_allowlist_match"

        return PolicyDecisionV1(
            decision_id=str(uuid4()),
            tenant_id=request.tenant_id,
            actor=request.actor,
            action=request.action,
            resource_ref=request.resource_ref,
            policy_id=request.policy_id,
            policy_version=request.policy_version,
            outcome=outcome,
            reason_code=reason,
            input_sha256=digest,
            evaluated_at=evaluated_at,
            workflow_id=request.workflow_id,
            run_id=request.run_id,
            task_id=request.task_id,
        )


class PostgresPolicyDecisionStore:
    """Append-only decision persistence and identical-denial lookup."""

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install PostgreSQL runtime dependencies to persist policy decisions") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def find_identical_denial(self, decision: PolicyDecisionV1) -> PolicyDecisionV1 | None:
        with self._connect() as conn:
            self._set_tenant(conn, decision.tenant_id)
            row = conn.execute(
                """
                SELECT decision_id, tenant_id, actor, action, resource_ref,
                       policy_id, policy_version, outcome, reason_code,
                       input_sha256, evaluated_at, approval_id
                FROM public.rusa_policy_decisions
                WHERE tenant_id=%s AND action=%s AND resource_ref=%s
                  AND policy_id=%s AND policy_version=%s
                  AND input_sha256=%s AND outcome='deny'
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                (
                    decision.tenant_id, decision.action, decision.resource_ref,
                    decision.policy_id, decision.policy_version, decision.input_sha256,
                ),
            ).fetchone()
        if row is None:
            return None
        return PolicyDecisionV1(
            decision_id=row[0], tenant_id=row[1], actor=row[2], action=row[3],
            resource_ref=row[4], policy_id=row[5], policy_version=row[6],
            outcome=row[7], reason_code=row[8], input_sha256=row[9],
            evaluated_at=row[10], approval_id=row[11],
        )

    def record(self, decision: PolicyDecisionV1) -> None:
        with self._connect() as conn:
            self._set_tenant(conn, decision.tenant_id)
            conn.execute(
                """
                INSERT INTO public.rusa_policy_decisions(
                    tenant_id, decision_id, actor, action, resource_ref,
                    policy_id, policy_version, outcome, reason_code,
                    input_sha256, evaluated_at, approval_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    decision.tenant_id, decision.decision_id, decision.actor,
                    decision.action, decision.resource_ref, decision.policy_id,
                    decision.policy_version, decision.outcome, decision.reason_code,
                    decision.input_sha256, decision.evaluated_at, decision.approval_id,
                ),
            )


class PolicyDecisionService:
    """Coordinates deterministic evaluation and durable append-only decision records."""

    def __init__(
        self,
        engine: DeterministicPolicyEngine,
        store: PostgresPolicyDecisionStore | None = None,
    ):
        self.engine = engine
        self.store = store

    def evaluate(self, request: PolicyRequest, *, persist: bool = True) -> PolicyDecisionV1:
        decision = self.engine.evaluate(request)
        if persist and self.store is not None:
            if decision.outcome == "deny":
                previous = self.store.find_identical_denial(decision)
                if previous is not None:
                    return previous
            self.store.record(decision)
        return decision

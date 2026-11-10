"""Durable incident correlation, kill switches, and workflow execution guards.

This module is the PostgreSQL-backed Phase 5 control plane. It does not
execute infrastructure actions; it records incident state and enforces
host-owned execution budgets and kill switches before adapters run.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REF_RE = re.compile(r"^(?:artifact|evidence)://sha256/[a-f0-9]{64}$")
_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_KILL_SWITCH_SCOPES = frozenset({
    "all", "workflow", "devops", "plan", "code", "build", "test",
    "release", "deploy", "operate", "monitor", "model", "tool",
})
_ACTION_SCOPES = {
    "workflow.start": ("all", "workflow", "devops"),
    "plan": ("all", "devops", "plan"),
    "code": ("all", "devops", "code"),
    "build": ("all", "devops", "build"),
    "test": ("all", "devops", "test"),
    "release": ("all", "devops", "release"),
    "deploy": ("all", "devops", "deploy"),
    "operate": ("all", "devops", "operate"),
    "monitor": ("all", "devops", "monitor"),
    "model.invoke": ("all", "model"),
    "tool.invoke": ("all", "tool"),
}
_TRANSITIONS = {
    "open": frozenset({"acknowledged", "investigating", "mitigating", "resolved"}),
    "acknowledged": frozenset({"investigating", "mitigating", "resolved"}),
    "investigating": frozenset({"acknowledged", "mitigating", "resolved"}),
    "mitigating": frozenset({"investigating", "resolved"}),
    "resolved": frozenset({"closed"}),
    "closed": frozenset(),
}


class ClosedLoopStoreError(RuntimeError):
    """A durable incident or kill-switch operation could not be confirmed."""


class ClosedLoopConflictError(ClosedLoopStoreError):
    """An idempotency key, incident state, or kill-switch version conflicted."""


class IncidentNotFoundError(ClosedLoopStoreError):
    """The requested tenant-scoped incident does not exist."""


class KillSwitchActiveError(PermissionError):
    """A host-owned kill switch blocks the requested action."""

    def __init__(self, scope: str, reason_code: str):
        self.scope = scope
        self.reason_code = reason_code
        super().__init__(f"execution blocked by active kill switch: {scope} ({reason_code})")


class ExecutionBudgetExceeded(PermissionError):
    """The workflow budget could not authorize the requested action."""


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError("observed_at must be an aware datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: str, name: str, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be non-empty and bounded")
    return value.strip()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def alert_fingerprint(
    tenant_id: str,
    service_id: str,
    environment: str,
    rule_id: str,
) -> str:
    """Stable identity across metric windows and observation timestamps."""
    parts = (
        _safe_text(tenant_id, "tenant_id", 128),
        _safe_text(service_id, "service_id", 128),
        _safe_text(environment, "environment", 32),
        _safe_text(rule_id, "rule_id", 128),
    )
    if not all(_ID_RE.fullmatch(part) for part in parts):
        raise ValueError("alert identity contains an invalid identifier")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SLOAlert:
    tenant_id: str
    message_id: str
    service_id: str
    environment: str
    rule_id: str
    severity: str
    summary: str
    evidence_ref: str
    observed_at: datetime | str
    correlation_id: str
    workflow_id: str
    metric_name: str
    operator: str
    threshold: float
    observed_value: float | None

    def __post_init__(self) -> None:
        for name in ("tenant_id", "service_id", "environment", "rule_id", "correlation_id", "workflow_id"):
            value = _safe_text(getattr(self, name), name, 128)
            if not _ID_RE.fullmatch(value):
                raise ValueError(f"{name} contains an invalid identifier")
        _safe_text(self.message_id, "message_id", 128)
        _safe_text(self.summary, "summary", 1000)
        if self.severity not in _SEVERITY_RANK:
            raise ValueError("alert severity is invalid")
        if not _REF_RE.fullmatch(self.evidence_ref):
            raise ValueError("alert evidence_ref must be a content-addressed artifact/evidence URI")
        if not _ID_RE.fullmatch(self.metric_name):
            raise ValueError("metric_name is invalid")
        if self.operator not in {"lte", "gte"}:
            raise ValueError("alert operator must be lte or gte")
        if type(self.threshold) not in {int, float} or not math.isfinite(float(self.threshold)):
            raise ValueError("alert threshold must be finite")
        if self.observed_value is not None and (
            type(self.observed_value) not in {int, float}
            or not math.isfinite(float(self.observed_value))
        ):
            raise ValueError("observed_value must be finite or null")
        _timestamp(self.observed_at)

    @property
    def fingerprint(self) -> str:
        return alert_fingerprint(self.tenant_id, self.service_id, self.environment, self.rule_id)

class PostgresClosedLoopStore:
    """Tenant-scoped durable incident lifecycle and kill-switch state."""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install PostgreSQL runtime dependencies to use PostgresClosedLoopStore") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required for closed-loop data access")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def initialize(self) -> None:
        """Verify migration 014 and forced RLS; never create production schema at runtime."""
        required = {
            "rusa_incidents",
            "rusa_incident_events",
            "rusa_closed_loop_inbox",
            "rusa_kill_switches",
            "rusa_kill_switch_events",
        }
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relname = ANY(%s)
                """,
                (list(required),),
            ).fetchall()
        states = {row[0]: (row[1], row[2]) for row in rows}
        if set(states) != required:
            missing = sorted(required - set(states))
            raise ClosedLoopStoreError(
                "Closed-loop migration 014 is missing required tables: " + ", ".join(missing)
            )
        if any(not enabled or not forced for enabled, forced in states.values()):
            raise ClosedLoopStoreError("Closed-loop tables require ENABLE and FORCE ROW LEVEL SECURITY")

    @staticmethod
    def _incident_select(conn, tenant_id: str, incident_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT incident_id::text, fingerprint, service_id, environment, rule_id,
                   severity, status, title, summary, correlation_id, workflow_id,
                   first_seen_at, last_seen_at, occurrence_count, latest_evidence_ref,
                   state_version, acknowledged_by, resolved_by, resolved_at, closed_at
            FROM public.rusa_incidents
            WHERE tenant_id=%s AND incident_id=%s::uuid
            """,
            (tenant_id, incident_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "incident_id": row[0],
            "fingerprint": row[1],
            "service_id": row[2],
            "environment": row[3],
            "rule_id": row[4],
            "severity": row[5],
            "status": row[6],
            "title": row[7],
            "summary": row[8],
            "correlation_id": row[9],
            "workflow_id": row[10],
            "first_seen_at": row[11].isoformat(),
            "last_seen_at": row[12].isoformat(),
            "occurrence_count": row[13],
            "latest_evidence_ref": row[14],
            "state_version": row[15],
            "acknowledged_by": row[16],
            "resolved_by": row[17],
            "resolved_at": row[18].isoformat() if row[18] else None,
            "closed_at": row[19].isoformat() if row[19] else None,
        }

    @staticmethod
    def _switch_dict(row) -> dict[str, Any]:
        return {
            "tenant_id": row[0],
            "scope": row[1],
            "enabled": row[2],
            "reason_code": row[3],
            "updated_by": row[4],
            "version": row[5],
            "updated_at": row[6].isoformat(),
        }

    def record_slo_alerts(self, alerts: list[SLOAlert]) -> list[dict[str, Any]]:
        """Atomically deduplicate observations and append immutable incident events."""
        if not isinstance(alerts, list) or not 1 <= len(alerts) <= 200:
            raise ValueError("record_slo_alerts requires 1 to 200 alerts")
        if any(not isinstance(alert, SLOAlert) for alert in alerts):
            raise TypeError("record_slo_alerts accepts SLOAlert values only")
        tenant_ids = {alert.tenant_id for alert in alerts}
        if len(tenant_ids) != 1:
            raise ValueError("one incident transaction may contain only one tenant")
        tenant_id = next(iter(tenant_ids))
        results: list[dict[str, Any]] = []
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            for alert in alerts:
                observed_at = _timestamp(alert.observed_at)
                message_fingerprint = hashlib.sha256(
                    _json({
                        "incident_fingerprint": alert.fingerprint,
                        "observed_at": observed_at,
                        "severity": alert.severity,
                        "summary": alert.summary,
                        "evidence_ref": alert.evidence_ref,
                        "metric_name": alert.metric_name,
                        "operator": alert.operator,
                        "threshold": float(alert.threshold),
                        "observed_value": (
                            float(alert.observed_value) if alert.observed_value is not None else None
                        ),
                        "correlation_id": alert.correlation_id,
                        "workflow_id": alert.workflow_id,
                    }).encode("utf-8")
                ).hexdigest()
                receipt = conn.execute(
                    """
                    INSERT INTO public.rusa_closed_loop_inbox(
                        tenant_id, consumer_name, message_id, fingerprint
                    )
                    VALUES (%s, 'monitor.slo', %s, %s)
                    ON CONFLICT (tenant_id, consumer_name, message_id) DO NOTHING
                    RETURNING message_id
                    """,
                    (tenant_id, alert.message_id, message_fingerprint),
                ).fetchone()
                if receipt is None:
                    prior = conn.execute(
                        """
                        SELECT fingerprint, subject_id
                        FROM public.rusa_closed_loop_inbox
                        WHERE tenant_id=%s AND consumer_name='monitor.slo' AND message_id=%s
                        """,
                        (tenant_id, alert.message_id),
                    ).fetchone()
                    if prior is None or prior[0] != message_fingerprint or not prior[1]:
                        raise ClosedLoopConflictError("alert message_id was reused with different content")
                    incident = self._incident_select(conn, tenant_id, prior[1])
                    if incident is None:
                        raise ClosedLoopStoreError("deduplicated alert receipt references a missing incident")
                    incident.update({
                        "incident_ref": f"incident://{tenant_id}/{incident['incident_id']}",
                        "duplicate": True,
                        "correlated": incident["occurrence_count"] > 1,
                    })
                    results.append(incident)
                    continue

                incident_id = str(uuid4())
                metadata = {
                    "metric_name": alert.metric_name,
                    "operator": alert.operator,
                    "threshold": float(alert.threshold),
                    "observed_value": (
                        float(alert.observed_value) if alert.observed_value is not None else None
                    ),
                }
                row = conn.execute(
                    """
                    INSERT INTO public.rusa_incidents AS current(
                        tenant_id, incident_id, fingerprint, service_id, environment,
                        rule_id, severity, status, title, summary, correlation_id,
                        workflow_id, first_seen_at, last_seen_at, occurrence_count,
                        latest_evidence_ref, metadata
                    )
                    VALUES (
                        %s, %s::uuid, %s, %s, %s, %s, %s, 'open', %s, %s, %s,
                        %s, %s::timestamptz, %s::timestamptz, 1, %s, %s::jsonb
                    )
                    ON CONFLICT (tenant_id, fingerprint) DO UPDATE SET
                        last_seen_at=GREATEST(current.last_seen_at, EXCLUDED.last_seen_at),
                        occurrence_count=current.occurrence_count+1,
                        severity=CASE
                            WHEN CASE EXCLUDED.severity
                                WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END
                            >
                            CASE current.severity
                                WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                                WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END
                            THEN EXCLUDED.severity ELSE current.severity END,
                        summary=EXCLUDED.summary,
                        latest_evidence_ref=EXCLUDED.latest_evidence_ref,
                        metadata=current.metadata || EXCLUDED.metadata,
                        status=CASE WHEN current.status IN ('resolved', 'closed')
                                    THEN 'open' ELSE current.status END,
                        resolved_by=CASE WHEN current.status IN ('resolved', 'closed')
                                         THEN NULL ELSE current.resolved_by END,
                        resolved_at=CASE WHEN current.status IN ('resolved', 'closed')
                                         THEN NULL ELSE current.resolved_at END,
                        closed_at=CASE WHEN current.status IN ('resolved', 'closed')
                                       THEN NULL ELSE current.closed_at END,
                        state_version=current.state_version+1,
                        updated_at=clock_timestamp()
                    RETURNING incident_id::text, status, occurrence_count,
                              state_version, correlation_id
                    """,
                    (
                        tenant_id, incident_id, alert.fingerprint, alert.service_id,
                        alert.environment, alert.rule_id, alert.severity,
                        alert.summary[:1000], alert.summary[:1000], alert.correlation_id,
                        alert.workflow_id, observed_at, observed_at, alert.evidence_ref,
                        _json(metadata),
                    ),
                ).fetchone()
                if row is None:
                    raise ClosedLoopStoreError("incident upsert did not return canonical state")
                canonical_id, status, occurrence_count, state_version, correlation_id = row
                conn.execute(
                    """
                    UPDATE public.rusa_closed_loop_inbox
                    SET subject_id=%s
                    WHERE tenant_id=%s AND consumer_name='monitor.slo' AND message_id=%s
                    """,
                    (canonical_id, tenant_id, alert.message_id),
                )
                conn.execute(
                    """
                    INSERT INTO public.rusa_incident_events(
                        tenant_id, event_id, incident_id, message_id, correlation_id,
                        event_type, actor, payload, occurred_at
                    )
                    VALUES (
                        %s, %s::uuid, %s::uuid, %s, %s,
                        'incident.alert.observed', 'rusa_monitor_agent', %s::jsonb,
                        %s::timestamptz
                    )
                    ON CONFLICT (tenant_id, message_id) DO NOTHING
                    """,
                    (
                        tenant_id, str(uuid4()), canonical_id, alert.message_id,
                        alert.correlation_id,
                        _json({
                            "fingerprint": alert.fingerprint,
                            "service_id": alert.service_id,
                            "environment": alert.environment,
                            "rule_id": alert.rule_id,
                            "severity": alert.severity,
                            "evidence_ref": alert.evidence_ref,
                            "state_version": state_version,
                        }),
                        observed_at,
                    ),
                )
                incident = self._incident_select(conn, tenant_id, canonical_id)
                if incident is None:
                    raise ClosedLoopStoreError("incident disappeared before transaction commit")
                incident.update({
                    "incident_ref": f"incident://{tenant_id}/{canonical_id}",
                    "duplicate": False,
                    "correlated": occurrence_count > 1,
                })
                results.append(incident)
        return results

    def get_incident(self, tenant_id: str, incident_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            return self._incident_select(conn, tenant_id, incident_id)

    def get_incident_by_ref(self, tenant_id: str, incident_ref: str) -> dict[str, Any] | None:
        if not isinstance(incident_ref, str) or len(incident_ref) > 256:
            raise ValueError("incident_ref is invalid")
        parsed = urlsplit(incident_ref)
        if (
            parsed.scheme != "incident"
            or parsed.netloc != tenant_id
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or parsed.path.count("/") != 1
        ):
            raise ValueError("incident_ref must be tenant-bound incident:// URI")
        incident_id = parsed.path.lstrip("/")
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", incident_id):
            raise ValueError("incident_ref identifier is invalid")
        return self.get_incident(tenant_id, incident_id)

    def transition_incident(
        self,
        *,
        tenant_id: str,
        incident_id: str,
        target_status: str,
        expected_state_version: int,
        actor: str,
        message_id: str,
        evidence_ref: str,
    ) -> dict[str, Any]:
        if target_status not in _TRANSITIONS:
            raise ValueError("incident target_status is invalid")
        if (
            isinstance(expected_state_version, bool)
            or not isinstance(expected_state_version, int)
            or expected_state_version < 1
        ):
            raise ValueError("expected_state_version must be a positive integer")
        _safe_text(actor, "actor", 128)
        _safe_text(message_id, "message_id", 128)
        if not _REF_RE.fullmatch(evidence_ref):
            raise ValueError("incident lifecycle evidence must be content-addressed")
        receipt_fingerprint = hashlib.sha256(
            f"{incident_id}|{target_status}|{expected_state_version}|{evidence_ref}".encode()
        ).hexdigest()
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            receipt = conn.execute(
                """
                INSERT INTO public.rusa_closed_loop_inbox(
                    tenant_id, consumer_name, message_id, fingerprint, subject_id
                )
                VALUES (%s, 'incident.lifecycle', %s, %s, %s)
                ON CONFLICT (tenant_id, consumer_name, message_id) DO NOTHING
                RETURNING message_id
                """,
                (tenant_id, message_id, receipt_fingerprint, incident_id),
            ).fetchone()
            if receipt is None:
                prior = conn.execute(
                    """
                    SELECT fingerprint, subject_id FROM public.rusa_closed_loop_inbox
                    WHERE tenant_id=%s AND consumer_name='incident.lifecycle' AND message_id=%s
                    """,
                    (tenant_id, message_id),
                ).fetchone()
                if prior is None or prior[0] != receipt_fingerprint or prior[1] != incident_id:
                    raise ClosedLoopConflictError("incident lifecycle message_id was reused")
                incident = self._incident_select(conn, tenant_id, incident_id)
                if incident is None:
                    raise IncidentNotFoundError("incident does not exist")
                incident["duplicate"] = True
                return incident

            current = conn.execute(
                """
                SELECT status, state_version FROM public.rusa_incidents
                WHERE tenant_id=%s AND incident_id=%s::uuid
                FOR UPDATE
                """,
                (tenant_id, incident_id),
            ).fetchone()
            if current is None:
                raise IncidentNotFoundError("incident does not exist")
            current_status, current_version = current
            if current_version != expected_state_version:
                raise ClosedLoopConflictError("incident state_version changed; reload before transition")
            if target_status not in _TRANSITIONS[current_status]:
                raise ClosedLoopConflictError(
                    f"incident transition {current_status} -> {target_status} is not allowed"
                )
            updated = conn.execute(
                """
                UPDATE public.rusa_incidents
                SET status=%s,
                    state_version=state_version+1,
                    acknowledged_by=CASE WHEN %s='acknowledged' THEN %s ELSE acknowledged_by END,
                    resolved_by=CASE WHEN %s='resolved' THEN %s ELSE resolved_by END,
                    resolved_at=CASE WHEN %s='resolved' THEN clock_timestamp() ELSE resolved_at END,
                    closed_at=CASE WHEN %s='closed' THEN clock_timestamp() ELSE closed_at END,
                    updated_at=clock_timestamp()
                WHERE tenant_id=%s AND incident_id=%s::uuid AND state_version=%s
                RETURNING state_version
                """,
                (
                    target_status, target_status, actor, target_status, actor,
                    target_status, target_status, tenant_id, incident_id,
                    expected_state_version,
                ),
            ).fetchone()
            if updated is None:
                raise ClosedLoopConflictError("incident transition lost its compare-and-set")
            state_version = int(updated[0])
            conn.execute(
                """
                INSERT INTO public.rusa_incident_events(
                    tenant_id, event_id, incident_id, message_id, correlation_id,
                    event_type, actor, payload, occurred_at
                )
                SELECT %s, %s::uuid, %s::uuid, %s, correlation_id,
                       %s, %s, %s::jsonb, clock_timestamp()
                FROM public.rusa_incidents
                WHERE tenant_id=%s AND incident_id=%s::uuid
                ON CONFLICT (tenant_id, message_id) DO NOTHING
                """,
                (
                    tenant_id, str(uuid4()), incident_id, message_id,
                    f"incident.{target_status}", actor,
                    _json({"evidence_ref": evidence_ref, "state_version": state_version}),
                    tenant_id, incident_id,
                ),
            )
            incident = self._incident_select(conn, tenant_id, incident_id)
            if incident is None:
                raise ClosedLoopStoreError("incident disappeared after transition")
            incident["duplicate"] = False
            return incident

    def get_kill_switch(self, tenant_id: str, scope: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT tenant_id, scope, enabled, reason_code, updated_by, version, updated_at
                FROM public.rusa_kill_switches
                WHERE tenant_id=%s AND scope=%s
                """,
                (tenant_id, scope),
            ).fetchone()
            return self._switch_dict(row) if row else None

    def set_kill_switch(
        self,
        *,
        tenant_id: str,
        scope: str,
        enabled: bool,
        reason_code: str,
        actor: str,
        expected_version: int,
        message_id: str,
    ) -> dict[str, Any]:
        if scope not in _KILL_SWITCH_SCOPES:
            raise ValueError("kill-switch scope is not allowlisted")
        if type(enabled) is not bool:
            raise ValueError("kill-switch enabled must be a boolean")
        _safe_text(reason_code, "reason_code", 128)
        _safe_text(actor, "actor", 128)
        _safe_text(message_id, "message_id", 128)
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise ValueError("expected_version must be a non-negative integer")
        fingerprint = hashlib.sha256(
            f"{scope}|{enabled}|{reason_code}|{expected_version}".encode()
        ).hexdigest()
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            receipt = conn.execute(
                """
                INSERT INTO public.rusa_closed_loop_inbox(
                    tenant_id, consumer_name, message_id, fingerprint, subject_id
                )
                VALUES (%s, 'kill-switch', %s, %s, %s)
                ON CONFLICT (tenant_id, consumer_name, message_id) DO NOTHING
                RETURNING message_id
                """,
                (tenant_id, message_id, fingerprint, scope),
            ).fetchone()
            if receipt is None:
                prior = conn.execute(
                    """
                    SELECT fingerprint, subject_id FROM public.rusa_closed_loop_inbox
                    WHERE tenant_id=%s AND consumer_name='kill-switch' AND message_id=%s
                    """,
                    (tenant_id, message_id),
                ).fetchone()
                if prior is None or prior[0] != fingerprint or prior[1] != scope:
                    raise ClosedLoopConflictError("kill-switch message_id was reused")
                row = conn.execute(
                    """
                    SELECT tenant_id, scope, enabled, reason_code, updated_by, version, updated_at
                    FROM public.rusa_kill_switches WHERE tenant_id=%s AND scope=%s
                    """,
                    (tenant_id, scope),
                ).fetchone()
                if row is None:
                    raise ClosedLoopStoreError("deduplicated kill-switch receipt has no current state")
                result = self._switch_dict(row)
                result["duplicate"] = True
                return result

            current = conn.execute(
                """
                SELECT version FROM public.rusa_kill_switches
                WHERE tenant_id=%s AND scope=%s FOR UPDATE
                """,
                (tenant_id, scope),
            ).fetchone()
            if current is None:
                if expected_version != 0:
                    raise ClosedLoopConflictError("kill-switch state is missing; expected_version must be zero")
                row = conn.execute(
                    """
                    INSERT INTO public.rusa_kill_switches(
                        tenant_id, scope, enabled, reason_code, updated_by, version
                    )
                    VALUES (%s, %s, %s, %s, %s, 1)
                    RETURNING tenant_id, scope, enabled, reason_code, updated_by, version, updated_at
                    """,
                    (tenant_id, scope, enabled, reason_code, actor),
                ).fetchone()
            else:
                if int(current[0]) != expected_version:
                    raise ClosedLoopConflictError("kill-switch version changed; reload before update")
                row = conn.execute(
                    """
                    UPDATE public.rusa_kill_switches
                    SET enabled=%s, reason_code=%s, updated_by=%s,
                        version=version+1, updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND scope=%s AND version=%s
                    RETURNING tenant_id, scope, enabled, reason_code, updated_by, version, updated_at
                    """,
                    (enabled, reason_code, actor, tenant_id, scope, expected_version),
                ).fetchone()
            if row is None:
                raise ClosedLoopConflictError("kill-switch compare-and-set failed")
            conn.execute(
                """
                INSERT INTO public.rusa_kill_switch_events(
                    tenant_id, event_id, message_id, scope, enabled,
                    reason_code, actor, version, occurred_at
                )
                VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, clock_timestamp())
                ON CONFLICT (tenant_id, message_id) DO NOTHING
                """,
                (tenant_id, str(uuid4()), message_id, scope, enabled, reason_code, actor, row[5]),
            )
            result = self._switch_dict(row)
            result["duplicate"] = False
            return result

    def assert_action_enabled(self, tenant_id: str, action: str) -> None:
        scopes = _ACTION_SCOPES.get(action)
        if scopes is None:
            raise ValueError("action is not registered with the kill-switch policy")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                """
                SELECT tenant_id, scope, reason_code
                FROM public.rusa_kill_switches
                WHERE tenant_id IN (%s, 'platform')
                  AND scope = ANY(%s)
                  AND enabled=true
                ORDER BY CASE WHEN tenant_id='platform' THEN 0 ELSE 1 END, scope
                """,
                (tenant_id, list(scopes)),
            ).fetchall()
        if rows:
            _, scope, reason_code = rows[0]
            raise KillSwitchActiveError(scope, reason_code)


class WorkflowExecutionGuard:
    """Fail-closed kill-switch and budget gate for host model/tool/agent adapters."""

    def __init__(self, *, workflow_store: Any, closed_loop_store: PostgresClosedLoopStore):
        if not callable(getattr(workflow_store, "consume_budget", None)):
            raise TypeError("workflow_store must implement atomic consume_budget")
        if not callable(getattr(closed_loop_store, "assert_action_enabled", None)):
            raise TypeError("closed_loop_store must implement kill-switch checks")
        self.workflow_store = workflow_store
        self.closed_loop_store = closed_loop_store

    def authorize_action(
        self,
        *,
        tenant_id: str,
        run_id: str,
        action: str,
        budget_metric: str = "tool_calls",
        amount: int | float = 1,
    ) -> dict[str, float]:
        self.closed_loop_store.assert_action_enabled(tenant_id, action)
        try:
            return self.workflow_store.consume_budget(
                tenant_id, run_id, budget_metric, amount
            )
        except Exception as exc:
            raise ExecutionBudgetExceeded(
                "workflow budget could not authorize the requested action"
            ) from exc

    def authorize_tool_call(self, *, tenant_id: str, run_id: str) -> dict[str, float]:
        return self.authorize_action(
            tenant_id=tenant_id, run_id=run_id, action="tool.invoke",
            budget_metric="tool_calls", amount=1,
        )

    def authorize_model_call(
        self,
        *,
        tenant_id: str,
        run_id: str,
        estimated_tokens: int,
        estimated_cost_usd: float,
    ) -> dict[str, float]:
        if isinstance(estimated_tokens, bool) or not isinstance(estimated_tokens, int) or estimated_tokens < 1:
            raise ValueError("estimated_tokens must be a positive integer")
        if type(estimated_cost_usd) not in {int, float} or not math.isfinite(float(estimated_cost_usd)) or estimated_cost_usd < 0:
            raise ValueError("estimated_cost_usd must be non-negative and finite")
        self.closed_loop_store.assert_action_enabled(tenant_id, "model.invoke")
        try:
            token_budget = self.workflow_store.consume_budget(
                tenant_id, run_id, "tokens", estimated_tokens
            )
            cost_budget = (
                self.workflow_store.consume_budget(
                    tenant_id, run_id, "cost_usd", estimated_cost_usd
                )
                if estimated_cost_usd > 0
                else {"consumed": 0.0}
            )
        except Exception as exc:
            raise ExecutionBudgetExceeded(
                "model call could not be authorized within the workflow budget"
            ) from exc
        return {"tokens_consumed": token_budget["consumed"], "cost_usd_consumed": cost_budget["consumed"]}

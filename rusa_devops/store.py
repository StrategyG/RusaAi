"""Tenant-scoped PostgreSQL persistence for telemetry, model runs, incidents, and proposals."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any
from uuid import uuid4

from .contracts import AnomalyAssessment, TelemetrySample
from .telemetry import validate_metric_identity


class PostgresDevOpsStore:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.getenv("RUSA_DEVOPS_DATABASE_URL") or os.getenv("RUSA_API_DATABASE_URL") or os.getenv("RUSA_DATABASE_URL")
        if not self.dsn:
            raise ValueError("RUSA_DEVOPS_DATABASE_URL or an existing RUSA database URL is required")

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install psycopg to use DevOps persistence") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not isinstance(tenant_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", tenant_id):
            raise ValueError("tenant_id is invalid")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def check_ready(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT to_regclass('public.rusa_devops_telemetry'), "
                "to_regclass('public.rusa_devops_model_runs'), "
                "to_regclass('public.rusa_devops_incidents'), "
                "to_regclass('public.rusa_devops_action_proposals')"
            ).fetchone()
        return bool(row and all(value is not None for value in row))

    def write_telemetry(self, tenant_id: str, samples: list[TelemetrySample]) -> int:
        if not samples or len(samples) > 50_000:
            raise ValueError("telemetry batch must contain between 1 and 50000 samples")
        rows = []
        for sample in samples:
            if sample.tenant_id != tenant_id:
                raise ValueError("telemetry tenant does not match authenticated tenant")
            validate_metric_identity(sample.metric_name, sample.resource_id)
            if sample.observed_at.tzinfo is None:
                raise ValueError("telemetry timestamps must be timezone-aware")
            value = float(sample.value)
            if not (-1e15 < value < 1e15):
                raise ValueError("telemetry value is outside the supported range")
            rows.append((
                tenant_id, sample.metric_name, sample.resource_id, sample.observed_at,
                value, json.dumps(sample.labels, ensure_ascii=False),
            ))
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO public.rusa_devops_telemetry
                        (tenant_id, metric_name, resource_id, observed_at, value, labels)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (tenant_id, metric_name, resource_id, observed_at)
                    DO UPDATE SET value=EXCLUDED.value, labels=EXCLUDED.labels
                    """, rows,
                )
        return len(rows)

    def read_telemetry(
        self, tenant_id: str, metric_name: str, resource_id: str, *,
        start: datetime | None = None, end: datetime | None = None, limit: int = 20_000,
    ) -> list[dict[str, Any]]:
        validate_metric_identity(metric_name, resource_id)
        if not 1 <= limit <= 100_000:
            raise ValueError("telemetry limit must be between 1 and 100000")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                """
                SELECT observed_at, value, labels
                FROM public.rusa_devops_telemetry
                WHERE tenant_id=%s AND metric_name=%s AND resource_id=%s
                  AND (%s::timestamptz IS NULL OR observed_at >= %s)
                  AND (%s::timestamptz IS NULL OR observed_at <= %s)
                ORDER BY observed_at DESC LIMIT %s
                """,
                (tenant_id, metric_name, resource_id, start, start, end, end, limit),
            ).fetchall()
        return [{"ds": row[0], "y": float(row[1]), "labels": row[2] or {}} for row in reversed(rows)]

    def save_model_run(
        self, tenant_id: str, *, run_id: str, metric_name: str, resource_id: str,
        artifact_uri: str, artifact_sha256: str, metrics: dict[str, Any],
        config: dict[str, Any], train_rows: int, validation_rows: int, test_rows: int,
    ) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
            raise ValueError("model artifact SHA-256 is invalid")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            conn.execute(
                """
                INSERT INTO public.rusa_devops_model_runs
                    (tenant_id, run_id, metric_name, resource_id, status, artifact_uri,
                     artifact_sha256, metrics, config, train_rows, validation_rows, test_rows)
                VALUES (%s, %s::uuid, %s, %s, 'succeeded', %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                """,
                (
                    tenant_id, run_id, metric_name, resource_id, artifact_uri, artifact_sha256,
                    json.dumps(metrics, allow_nan=False), json.dumps(config, allow_nan=False),
                    train_rows, validation_rows, test_rows,
                ),
            )
        return run_id

    def get_model_run(self, tenant_id: str, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT run_id, metric_name, resource_id, status, artifact_uri,
                       artifact_sha256, metrics, config, train_rows, validation_rows,
                       test_rows, created_at
                FROM public.rusa_devops_model_runs
                WHERE tenant_id=%s AND run_id=%s::uuid
                """, (tenant_id, run_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": str(row[0]), "metric_name": row[1], "resource_id": row[2],
            "status": row[3], "artifact_uri": row[4], "artifact_sha256": row[5],
            "metrics": row[6], "config": row[7], "train_rows": row[8],
            "validation_rows": row[9], "test_rows": row[10],
            "created_at": row[11].isoformat() if row[11] else None,
        }

    def list_model_runs(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                """
                SELECT run_id, metric_name, resource_id, status, metrics, created_at
                FROM public.rusa_devops_model_runs
                WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s
                """, (tenant_id, limit),
            ).fetchall()
        return [
            {"run_id": str(row[0]), "metric_name": row[1], "resource_id": row[2],
             "status": row[3], "metrics": row[4], "created_at": row[5].isoformat()}
            for row in rows
        ]

    def record_incidents(self, tenant_id: str, metric_name: str, resource_id: str, assessments: list[AnomalyAssessment]) -> int:
        rows = [
            (
                str(uuid4()), tenant_id, metric_name, resource_id, item.observed_at,
                item.value, item.z_score, item.posterior_probability,
                "high" if item.posterior_probability >= 0.99 else "medium",
                json.dumps({"detector": "bayesian_robust_gaussian"}, ensure_ascii=False),
            )
            for item in assessments if item.is_anomaly
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO public.rusa_devops_incidents
                        (incident_id, tenant_id, metric_name, resource_id, observed_at,
                         observed_value, z_score, anomaly_probability, severity, details)
                    VALUES (%s::uuid, %s, %s, %s, %s::timestamptz, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (tenant_id, metric_name, resource_id, observed_at)
                    DO UPDATE SET anomaly_probability=EXCLUDED.anomaly_probability,
                                  severity=EXCLUDED.severity, details=EXCLUDED.details
                    """, rows,
                )
        return len(rows)

    def list_incidents(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                """
                SELECT incident_id, metric_name, resource_id, observed_at, observed_value,
                       z_score, anomaly_probability, severity, details
                FROM public.rusa_devops_incidents
                WHERE tenant_id=%s ORDER BY observed_at DESC LIMIT %s
                """, (tenant_id, limit),
            ).fetchall()
        return [
            {"incident_id": str(row[0]), "metric_name": row[1], "resource_id": row[2],
             "observed_at": row[3].isoformat(), "observed_value": float(row[4]),
             "z_score": float(row[5]), "anomaly_probability": float(row[6]),
             "severity": row[7], "details": row[8]}
            for row in rows
        ]

    def list_action_proposals(self, tenant_id: str, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                """
                SELECT proposal_id, action_type, status, payload, approval_required,
                       proposed_at, decided_at, decided_by, decision_reason
                FROM public.rusa_devops_action_proposals
                WHERE tenant_id=%s ORDER BY proposed_at DESC LIMIT %s
                """, (tenant_id, limit),
            ).fetchall()
        return [
            {"proposal_id": str(row[0]), "action_type": row[1], "status": row[2],
             "payload": row[3], "approval_required": row[4],
             "proposed_at": row[5].isoformat(), "decided_at": row[6].isoformat() if row[6] else None,
             "decided_by": row[7], "decision_reason": row[8]}
            for row in rows
        ]

    def decide_action_proposal(
        self, tenant_id: str, proposal_id: str, *, approved: bool, actor: str, reason: str = ""
    ) -> bool:
        status = "approved" if approved else "rejected"
        action = "action.approve" if approved else "action.reject"
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            cursor = conn.execute(
                """
                UPDATE public.rusa_devops_action_proposals
                SET status=%s, decided_at=NOW(), decided_by=%s, decision_reason=%s
                WHERE tenant_id=%s AND proposal_id=%s::uuid AND status='pending_approval'
                """, (status, actor[:128], reason[:2000], tenant_id, proposal_id),
            )
            if cursor.rowcount == 1:
                conn.execute(
                    """
                    INSERT INTO public.rusa_devops_audit_events
                        (tenant_id, actor, action, decision, details)
                    VALUES (%s, %s, %s, 'allow', %s::jsonb)
                    """,
                    (tenant_id, actor[:128], action, json.dumps({"proposal_id": proposal_id, "reason": reason[:2000]})),
                )
            return cursor.rowcount == 1

    def create_action_proposal(self, tenant_id: str, proposal: dict[str, Any]) -> str:
        proposal_id = str(uuid4())
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            conn.execute(
                """
                INSERT INTO public.rusa_devops_action_proposals
                    (tenant_id, proposal_id, action_type, status, payload, approval_required)
                VALUES (%s, %s::uuid, %s, 'pending_approval', %s::jsonb, true)
                """, (tenant_id, proposal_id, proposal["action"], json.dumps(proposal, allow_nan=False)),
            )
        return proposal_id

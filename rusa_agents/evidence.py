"""Immutable evidence persistence and digest-bound QA verdict verification."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .platform_contracts import EvidenceReference, ValidationVerdictPayloadV2, WorkflowEventV2
from .workflow_store import PostgresWorkflowStore


class EvidenceValidationError(RuntimeError):
    """Evidence is missing, stale, or does not bind to the exact artifact under review."""


def _reject_secrets(value: Any, path: str = "metadata") -> None:
    blocked = {
        "api_key", "apikey", "access_token", "refresh_token", "authorization",
        "password", "client_secret", "private_key", "secret", "bearer_token",
    }
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower().replace("-", "_") in blocked:
                raise ValueError(f"credential-shaped evidence metadata is prohibited: {path}.{key}")
            _reject_secrets(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}[{index}]")


class PostgresEvidenceStore:
    """Stores immutable evidence content references and verifies QA against an exact digest."""

    def __init__(self, dsn: str):
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        self.dsn = dsn

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install PostgreSQL runtime dependencies to use PostgresEvidenceStore") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required for evidence access")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def record(
        self,
        tenant_id: str,
        reference: EvidenceReference,
        *,
        subject_commit: str,
        test_profile: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not reference.subject_digest:
            raise ValueError("evidence must identify the exact subject artifact digest")
        if not reference.producer_version:
            raise ValueError("evidence producer_version is required")
        if not reference.test_profile or reference.test_profile != test_profile:
            raise ValueError("evidence test profile must match the declared profile")
        if reference.subject_commit != subject_commit:
            raise ValueError("evidence subject commit must match the declared commit")
        occurred_at = reference.occurred_at
        if occurred_at is None or occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("evidence occurred_at must be timezone-aware")
        now = datetime.now(timezone.utc)
        occurred_at = occurred_at.astimezone(timezone.utc)
        if occurred_at > now:
            raise ValueError("evidence timestamp cannot be in the future")
        if subject_commit and not re.fullmatch(r"[a-f0-9]{40,64}", subject_commit):
            raise ValueError("subject commit must be a full hexadecimal Git commit SHA")
        details = dict(metadata or {})
        _reject_secrets(details)
        evidence_id = str(uuid4())

        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            inserted = conn.execute(
                """
                INSERT INTO public.rusa_evidence_records(
                    tenant_id, evidence_id, uri, sha256, kind, producer_id,
                    producer_version, subject_digest, subject_commit,
                    test_profile, occurred_at, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, uri, sha256) DO NOTHING
                RETURNING evidence_id
                """,
                (
                    tenant_id, evidence_id, reference.uri, reference.sha256,
                    reference.kind, reference.producer_id, reference.producer_version,
                    reference.subject_digest, subject_commit, test_profile,
                    occurred_at, json.dumps(details, ensure_ascii=False),
                ),
            ).fetchone()
            if inserted is not None:
                return str(inserted[0])
            existing = conn.execute(
                """
                SELECT evidence_id, producer_id, producer_version,
                       subject_digest, subject_commit, test_profile, occurred_at
                FROM public.rusa_evidence_records
                WHERE tenant_id=%s AND uri=%s AND sha256=%s
                """,
                (tenant_id, reference.uri, reference.sha256),
            ).fetchone()
            if existing is None or (
                existing[1] != reference.producer_id
                or existing[2] != reference.producer_version
                or existing[3] != reference.subject_digest
                or existing[4] != subject_commit
                or existing[5] != test_profile
                or existing[6] != occurred_at
            ):
                raise EvidenceValidationError("evidence URI/digest already exists with different provenance")
            return str(existing[0])

    def _verify_on_connection(
        self,
        conn,
        tenant_id: str,
        verdict: ValidationVerdictPayloadV2,
        *,
        expected_digest: str,
        expected_commit: str | None,
        max_age_seconds: int,
        now: datetime,
    ) -> list[str]:
        if verdict.verdict != "passed":
            raise EvidenceValidationError("only a passed QA verdict can satisfy the release evidence gate")
        if verdict.subject_digest != expected_digest:
            raise EvidenceValidationError("QA verdict subject digest does not match the artifact being promoted")
        if expected_commit is not None and verdict.commit_sha != expected_commit:
            raise EvidenceValidationError("QA verdict commit SHA does not match the artifact commit")
        evaluated_at = verdict.evaluated_at.astimezone(timezone.utc)
        if evaluated_at > now or evaluated_at < now - timedelta(seconds=max_age_seconds):
            raise EvidenceValidationError("QA verdict is stale or has a future timestamp")

        evidence_ids: list[str] = []
        for reference in verdict.evidence_refs:
            if (
                reference.subject_digest != expected_digest
                or reference.subject_commit != expected_commit
                or reference.test_profile != verdict.test_profile
                or reference.occurred_at is None
                or not reference.producer_version
            ):
                raise EvidenceValidationError("QA evidence is not bound to the exact digest, commit, and test profile")
            occurred_at = reference.occurred_at.astimezone(timezone.utc)
            if occurred_at > now or occurred_at < now - timedelta(seconds=max_age_seconds):
                raise EvidenceValidationError("QA evidence is stale or has a future timestamp")
            row = conn.execute(
                """
                SELECT evidence_id, producer_id, producer_version,
                       subject_digest, subject_commit, test_profile, occurred_at
                FROM public.rusa_evidence_records
                WHERE tenant_id=%s AND uri=%s AND sha256=%s
                """,
                (tenant_id, reference.uri, reference.sha256),
            ).fetchone()
            if row is None:
                raise EvidenceValidationError("QA evidence reference is not present in the immutable evidence store")
            if (
                row[1] != reference.producer_id
                or row[2] != reference.producer_version
                or row[3] != expected_digest
                or row[4] != expected_commit
                or row[5] != verdict.test_profile
                or row[6] != occurred_at
            ):
                raise EvidenceValidationError("stored evidence provenance does not match the QA verdict")
            evidence_ids.append(str(row[0]))
        return evidence_ids

    def verify_verdict(
        self,
        tenant_id: str,
        verdict: ValidationVerdictPayloadV2,
        *,
        expected_digest: str,
        expected_commit: str | None = None,
        max_age_seconds: int = 86_400,
        now: datetime | None = None,
    ) -> list[str]:
        if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", expected_digest):
            raise ValueError("expected artifact digest must be SHA-256")
        if not 1 <= max_age_seconds <= 90 * 86_400:
            raise ValueError("evidence freshness window must be between one second and 90 days")
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("evidence verification time must be timezone-aware")
        instant = instant.astimezone(timezone.utc)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            return self._verify_on_connection(
                conn, tenant_id, verdict,
                expected_digest=expected_digest,
                expected_commit=expected_commit,
                max_age_seconds=max_age_seconds,
                now=instant,
            )

    def verify_and_publish_verdict(
        self,
        *,
        tenant_id: str,
        workflow_id: str,
        run_id: str,
        task_id: str,
        attempt: int,
        verdict: ValidationVerdictPayloadV2,
        expected_digest: str,
        expected_commit: str | None = None,
        max_age_seconds: int = 86_400,
    ) -> list[str]:
        """Verify evidence and append the typed validation event in one tenant transaction."""
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            evidence_ids = self._verify_on_connection(
                conn, tenant_id, verdict,
                expected_digest=expected_digest,
                expected_commit=expected_commit,
                max_age_seconds=max_age_seconds,
                now=now,
            )
            event = WorkflowEventV2(
                tenant_id=tenant_id,
                workflow_id=workflow_id,
                run_id=run_id,
                task_id=task_id,
                attempt=attempt,
                correlation_id=workflow_id,
                event_type="validation.verdict",
                producer_id=verdict.producer_id,
                producer_version=verdict.producer_version,
                occurred_at=verdict.evaluated_at,
                payload=verdict.model_dump(mode="json"),
            )
            PostgresWorkflowStore._append_event(conn, event)
        return evidence_ids

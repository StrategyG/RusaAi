"""Versioned agent registry with runtime binding validation and audited lifecycle transitions."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .platform_contracts import (
    AgentHealthSnapshot,
    AgentHealthStatus,
    AgentManifestV1,
    AgentStatus,
    RuntimeKind,
    validate_manifest_update,
)


class AgentRegistryError(RuntimeError):
    """Agent registration, validation, lifecycle, or lookup failed."""


Resolver = Callable[[RuntimeKind, str], bool]
SchemaResolver = Callable[[str], bool]
PolicyResolver = Callable[[str], bool]


def _manifest_hash(manifest: AgentManifestV1) -> str:
    immutable = manifest.model_dump(
        exclude={"status", "health", "updated_at"},
        mode="json",
    )
    encoded = json.dumps(immutable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _tenant_allowed(manifest: AgentManifestV1, tenant_id: str, registry_tenant: str) -> bool:
    scope = manifest.tenant_scope
    if scope.mode == "system":
        return registry_tenant == "platform"
    if scope.mode == "allowlist":
        return tenant_id in scope.tenant_ids
    return tenant_id == registry_tenant


class PostgresAgentRegistry:
    """Tenant-scoped manifest registry. Registration never grants tool permission."""

    def __init__(
        self,
        dsn: str,
        *,
        runtime_resolver: Resolver,
        schema_resolver: SchemaResolver,
        policy_resolver: PolicyResolver,
    ):
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        if not all(callable(item) for item in (runtime_resolver, schema_resolver, policy_resolver)):
            raise ValueError("runtime, schema, and policy resolvers are required and fail closed")
        self.dsn = dsn
        self.runtime_resolver = runtime_resolver
        self.schema_resolver = schema_resolver
        self.policy_resolver = policy_resolver

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Install PostgreSQL runtime dependencies to use PostgresAgentRegistry") from exc
        return psycopg.connect(self.dsn)

    @staticmethod
    def _set_tenant(conn, tenant_id: str) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required for registry access")
        conn.execute("SELECT set_config('rusa.tenant_id', %s, true)", (tenant_id,))

    def _validate_bindings(self, manifest: AgentManifestV1) -> None:
        if not manifest.capabilities:
            raise AgentRegistryError("agent manifest must declare at least one capability")
        if not self.runtime_resolver(manifest.runtime_binding.kind, manifest.runtime_binding.target):
            raise AgentRegistryError("agent runtime binding is not registered")
        if not self.schema_resolver(manifest.input_schema_ref):
            raise AgentRegistryError("agent input schema reference is not registered")
        if not self.schema_resolver(manifest.output_schema_ref):
            raise AgentRegistryError("agent output schema reference is not registered")
        if not self.policy_resolver(manifest.policy_profile):
            raise AgentRegistryError("agent policy profile is not registered")

    @staticmethod
    def _write_event(
        conn,
        *,
        tenant_id: str,
        manifest: AgentManifestV1,
        actor: str,
        action: str,
        previous_status: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO public.rusa_agent_registry_events(
                tenant_id, event_id, agent_id, version, action, actor,
                previous_status, new_status, manifest_sha256
            )
            VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id, str(uuid4()), manifest.agent_id, manifest.version,
                action, actor, previous_status, manifest.status.value,
                _manifest_hash(manifest),
            ),
        )

    def register(
        self,
        tenant_id: str,
        manifest: AgentManifestV1,
        *,
        actor: str,
    ) -> AgentManifestV1:
        if not actor or len(actor) > 128:
            raise ValueError("registry actor must be a non-empty identity of at most 128 characters")
        if manifest.status != AgentStatus.DRAFT:
            raise AgentRegistryError("new agent registrations must begin in draft status")
        if manifest.tenant_scope.mode == "system" and tenant_id != "platform":
            raise AgentRegistryError("system-scoped agents must be registered by the platform tenant")
        if manifest.tenant_scope.mode == "allowlist" and tenant_id not in manifest.tenant_scope.tenant_ids:
            raise AgentRegistryError("registry tenant must be included in the manifest tenant allowlist")
        self._validate_bindings(manifest)

        data = manifest.model_dump(mode="json")
        digest = _manifest_hash(manifest)
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            inserted = conn.execute(
                """
                INSERT INTO public.rusa_agent_registry(
                    tenant_id, agent_id, version, domain, status,
                    manifest, manifest_sha256, registered_by
                )
                VALUES (%s, %s, %s, %s, 'draft', %s::jsonb, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING agent_id
                """,
                (
                    tenant_id, manifest.agent_id, manifest.version, manifest.domain.value,
                    json.dumps(data, ensure_ascii=False), digest, actor,
                ),
            ).fetchone()
            if inserted is None:
                existing = conn.execute(
                    """
                    SELECT manifest_sha256 FROM public.rusa_agent_registry
                    WHERE tenant_id=%s AND agent_id=%s AND version=%s
                    """,
                    (tenant_id, manifest.agent_id, manifest.version),
                ).fetchone()
                if existing is None or existing[0] != digest:
                    raise AgentRegistryError("agent version already exists with different immutable content")
                return manifest
            self._write_event(
                conn, tenant_id=tenant_id, manifest=manifest,
                actor=actor, action="registered", previous_status=None,
            )
        return manifest

    def get(
        self,
        tenant_id: str,
        agent_id: str,
        version: str,
    ) -> AgentManifestV1 | None:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT tenant_id, status, manifest
                FROM public.rusa_agent_registry
                WHERE tenant_id IN (%s, 'platform') AND agent_id=%s AND version=%s
                ORDER BY (tenant_id=%s) DESC
                LIMIT 1
                """,
                (tenant_id, agent_id, version, tenant_id),
            ).fetchone()
        if row is None:
            return None
        manifest_data = row[2] if isinstance(row[2], dict) else json.loads(row[2])
        manifest = AgentManifestV1.model_validate(manifest_data)
        if manifest.status.value != row[1]:
            raise AgentRegistryError("registry status and immutable manifest snapshot are inconsistent")
        return manifest

    def _transition(
        self,
        tenant_id: str,
        agent_id: str,
        version: str,
        new_status: AgentStatus,
        *,
        actor: str,
        action: str,
        validated_at: datetime | None = None,
        activated_at: datetime | None = None,
    ) -> AgentManifestV1:
        if not actor or len(actor) > 128:
            raise ValueError("registry actor must be a non-empty identity of at most 128 characters")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT status, manifest
                FROM public.rusa_agent_registry
                WHERE tenant_id=%s AND agent_id=%s AND version=%s
                FOR UPDATE
                """,
                (tenant_id, agent_id, version),
            ).fetchone()
            if row is None:
                raise AgentRegistryError("agent registration was not found in this tenant")
            previous = AgentManifestV1.model_validate(
                row[1] if isinstance(row[1], dict) else json.loads(row[1])
            )
            candidate_data = previous.model_dump(mode="json")
            candidate_data["status"] = new_status.value
            candidate_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            candidate = AgentManifestV1.model_validate(candidate_data)
            validate_manifest_update(previous, candidate)

            data = candidate.model_dump(mode="json")
            updated = conn.execute(
                """
                UPDATE public.rusa_agent_registry
                SET status=%s, manifest=%s::jsonb,
                    validated_at=COALESCE(%s, validated_at),
                    activated_at=COALESCE(%s, activated_at),
                    updated_at=%s
                WHERE tenant_id=%s AND agent_id=%s AND version=%s
                RETURNING status
                """,
                (
                    new_status.value, json.dumps(data, ensure_ascii=False),
                    validated_at, activated_at, candidate.updated_at,
                    tenant_id, agent_id, version,
                ),
            ).fetchone()
            if updated is None:
                raise AgentRegistryError("agent lifecycle update was not persisted")
            self._write_event(
                conn, tenant_id=tenant_id, manifest=candidate,
                actor=actor, action=action, previous_status=row[0],
            )
            return candidate

    def validate(self, tenant_id: str, agent_id: str, version: str, *, actor: str) -> AgentManifestV1:
        manifest = self.get(tenant_id, agent_id, version)
        if manifest is None:
            raise AgentRegistryError("agent registration was not found")
        if manifest.status != AgentStatus.DRAFT:
            raise AgentRegistryError("only a draft agent can be validated")
        self._validate_bindings(manifest)
        return self._transition(
            tenant_id, agent_id, version, AgentStatus.VALIDATED,
            actor=actor, action="validated", validated_at=datetime.now(timezone.utc),
        )

    def activate(self, tenant_id: str, agent_id: str, version: str, *, actor: str) -> AgentManifestV1:
        manifest = self.get(tenant_id, agent_id, version)
        if manifest is None or manifest.status != AgentStatus.VALIDATED:
            raise AgentRegistryError("only a validated agent can be activated")
        self._validate_bindings(manifest)
        if manifest.health.status != AgentHealthStatus.HEALTHY:
            raise AgentRegistryError("agent must have a healthy runtime snapshot before activation")
        return self._transition(
            tenant_id, agent_id, version, AgentStatus.ACTIVE,
            actor=actor, action="activated", activated_at=datetime.now(timezone.utc),
        )

    def suspend(self, tenant_id: str, agent_id: str, version: str, *, actor: str) -> AgentManifestV1:
        return self._transition(
            tenant_id, agent_id, version, AgentStatus.SUSPENDED,
            actor=actor, action="suspended",
        )

    def deprecate(self, tenant_id: str, agent_id: str, version: str, *, actor: str) -> AgentManifestV1:
        return self._transition(
            tenant_id, agent_id, version, AgentStatus.DEPRECATED,
            actor=actor, action="deprecated",
        )

    def revoke(self, tenant_id: str, agent_id: str, version: str, *, actor: str) -> AgentManifestV1:
        return self._transition(
            tenant_id, agent_id, version, AgentStatus.REVOKED,
            actor=actor, action="revoked",
        )

    def update_health(
        self,
        tenant_id: str,
        agent_id: str,
        version: str,
        health: AgentHealthSnapshot,
        *,
        actor: str,
    ) -> AgentManifestV1:
        if not actor or len(actor) > 128:
            raise ValueError("registry actor is invalid")
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            row = conn.execute(
                """
                SELECT status, manifest FROM public.rusa_agent_registry
                WHERE tenant_id=%s AND agent_id=%s AND version=%s FOR UPDATE
                """,
                (tenant_id, agent_id, version),
            ).fetchone()
            if row is None:
                raise AgentRegistryError("agent registration was not found")
            data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
            previous = AgentManifestV1.model_validate(data)
            candidate_data = previous.model_dump(mode="json")
            candidate_data["health"] = health.model_dump(mode="json")
            candidate_data["updated_at"] = datetime.now(timezone.utc).isoformat()
            candidate = AgentManifestV1.model_validate(candidate_data)
            validate_manifest_update(previous, candidate)
            conn.execute(
                """
                UPDATE public.rusa_agent_registry
                SET manifest=%s::jsonb, updated_at=%s
                WHERE tenant_id=%s AND agent_id=%s AND version=%s
                """,
                (
                    json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False),
                    candidate.updated_at, tenant_id, agent_id, version,
                ),
            )
            self._write_event(
                conn, tenant_id=tenant_id, manifest=candidate,
                actor=actor, action="health.updated", previous_status=row[0],
            )
        return candidate

    def assert_runnable(
        self,
        tenant_id: str,
        agent_id: str,
        version: str,
        capability: str,
    ) -> AgentManifestV1:
        manifest = self.get(tenant_id, agent_id, version)
        if manifest is None:
            raise AgentRegistryError("unregistered agent version cannot start")
        if manifest.status != AgentStatus.ACTIVE:
            raise AgentRegistryError(f"agent version is not active: {manifest.status.value}")
        if capability not in manifest.capabilities:
            raise AgentRegistryError("agent version does not declare the requested capability")
        if not _tenant_allowed(manifest, tenant_id, "platform" if manifest.tenant_scope.mode == "system" else tenant_id):
            raise AgentRegistryError("agent tenant scope does not authorize this tenant")
        self._validate_bindings(manifest)
        return manifest

    def select(
        self,
        tenant_id: str,
        capability: str,
        *,
        domain: str | None = None,
    ) -> AgentManifestV1:
        with self._connect() as conn:
            self._set_tenant(conn, tenant_id)
            rows = conn.execute(
                """
                SELECT tenant_id, manifest
                FROM public.rusa_agent_registry
                WHERE tenant_id IN (%s, 'platform') AND status='active'
                  AND (%s IS NULL OR domain=%s)
                ORDER BY (tenant_id=%s) DESC, agent_id, version DESC
                """,
                (tenant_id, domain, domain, tenant_id),
            ).fetchall()
        for registry_tenant, raw in rows:
            data = raw if isinstance(raw, dict) else json.loads(raw)
            manifest = AgentManifestV1.model_validate(data)
            if capability not in manifest.capabilities:
                continue
            if _tenant_allowed(manifest, tenant_id, registry_tenant):
                self._validate_bindings(manifest)
                return manifest
        raise AgentRegistryError("no active registered agent provides the requested capability")

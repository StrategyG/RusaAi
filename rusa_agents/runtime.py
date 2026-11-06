"""Environment-backed construction of the shared enterprise agent runtime."""

from __future__ import annotations

import json
import os
import re

from .audit import PostgresAuditLog
from .github_gateway import GitHubRepositoryGateway, GitHubToolGateway
from .memory import PostgresMemory
from .orchestrator import AICodeOrchestrator
from .providers import OpenAICompatibleProvider
from .workers import AgentWorker


def build_orchestrator() -> AICodeOrchestrator:
    provider_url = os.getenv("RUSA_PROVIDER_BASE_URL", "")
    provider_model = os.getenv("RUSA_PROVIDER_MODEL", os.getenv("RUSA_MODEL_NAME", "rusa-ai-v1"))
    github_token = os.getenv("RUSA_GITHUB_TOKEN", "")
    database_url = os.getenv("DATABASE_URL", "")
    if not provider_url:
        raise RuntimeError("RUSA_PROVIDER_BASE_URL must be configured for AICode workers")
    if not github_token:
        raise RuntimeError("RUSA_GITHUB_TOKEN must be configured for repository tools")
    if not database_url:
        raise RuntimeError("DATABASE_URL must be configured for shared agent memory and audit")

    provider = OpenAICompatibleProvider(
        provider_url,
        provider_model,
        os.getenv("RUSA_PROVIDER_API_KEY"),
        timeout_seconds=float(os.getenv("RUSA_PROVIDER_TIMEOUT_SECONDS", "120")),
        allowed_hosts={
            host.strip()
            for host in os.getenv("RUSA_PROVIDER_ALLOWED_HOSTS", "").split(",")
            if host.strip()
        },
    )
    raw_tenant_repositories = (
        os.getenv("RUSA_GITHUB_TENANT_REPOSITORIES_JSON", "").strip() or None
    )
    raw_api_tenants = os.getenv("RUSA_API_KEY_TENANTS_JSON", "").strip() or None
    tenant_repositories = None
    if raw_tenant_repositories is not None:
        try:
            parsed_repositories = json.loads(raw_tenant_repositories)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "RUSA_GITHUB_TENANT_REPOSITORIES_JSON must contain valid JSON"
            ) from exc
        if not isinstance(parsed_repositories, dict) or not parsed_repositories:
            raise RuntimeError(
                "RUSA_GITHUB_TENANT_REPOSITORIES_JSON must be a non-empty object"
            )
        tenant_repositories = {}
        for tenant_id, repositories in parsed_repositories.items():
            if not isinstance(tenant_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", tenant_id
            ):
                raise RuntimeError("tenant repository map contains an invalid tenant ID")
            if (
                not isinstance(repositories, list)
                or not repositories
                or any(
                    not isinstance(repository, str)
                    or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                    for repository in repositories
                )
            ):
                raise RuntimeError(
                    f"tenant {tenant_id!r} must map to a non-empty list of owner/repo entries"
                )
            tenant_repositories[tenant_id] = set(repositories)

    if raw_api_tenants is not None:
        try:
            api_tenants = json.loads(raw_api_tenants)
        except json.JSONDecodeError as exc:
            raise RuntimeError("RUSA_API_KEY_TENANTS_JSON must contain valid JSON") from exc
        if not isinstance(api_tenants, dict) or not api_tenants:
            raise RuntimeError("RUSA_API_KEY_TENANTS_JSON must be a non-empty object")
        tenant_ids = set()
        for key_hash, tenant_id in api_tenants.items():
            if not isinstance(key_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key_hash):
                raise RuntimeError("tenant API-key map keys must be SHA-256 hex digests")
            if not isinstance(tenant_id, str) or not re.fullmatch(
                r"[A-Za-z0-9_.:-]{1,128}", tenant_id
            ):
                raise RuntimeError("tenant API-key map contains an invalid tenant ID")
            tenant_ids.add(tenant_id)
        if tenant_repositories is None:
            raise RuntimeError(
                "multi-tenant API keys require RUSA_GITHUB_TENANT_REPOSITORIES_JSON"
            )
        missing_tenants = tenant_ids - tenant_repositories.keys()
        if missing_tenants:
            raise RuntimeError(
                "tenant repository allowlist is missing authenticated tenants: "
                + ", ".join(sorted(missing_tenants))
            )
    elif tenant_repositories is not None:
        fallback_tenant = os.getenv("RUSA_TENANT_ID", "default")
        if fallback_tenant not in tenant_repositories:
            raise RuntimeError(
                "tenant repository allowlist is missing fallback tenant "
                f"{fallback_tenant!r}"
            )

    allowed_repositories = {
        item.strip()
        for item in os.getenv("RUSA_GITHUB_ALLOWED_REPOSITORIES", "").split(",")
        if item.strip()
    }
    if tenant_repositories is None and not allowed_repositories:
        raise RuntimeError(
            "configure RUSA_GITHUB_ALLOWED_REPOSITORIES or "
            "RUSA_GITHUB_TENANT_REPOSITORIES_JSON"
        )
    github = GitHubRepositoryGateway(github_token)
    gateway = GitHubToolGateway(
        github,
        allowed_repositories=allowed_repositories or None,
        tenant_repositories=tenant_repositories,
    )
    memory = PostgresMemory(database_url)
    audit = PostgresAuditLog(database_url)
    workers = [
        AgentWorker(
            role=role,
            provider=provider,
            tools=gateway,
            max_turns=int(os.getenv("RUSA_AGENT_MAX_TURNS", "8")),
        )
        for role in AICodeOrchestrator.PIPELINE
    ]
    return AICodeOrchestrator(workers, memory=memory, audit=audit)

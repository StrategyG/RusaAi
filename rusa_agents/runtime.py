"""Environment-backed construction of the shared enterprise agent runtime."""

from __future__ import annotations

import os

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
    )
    allowed_repositories = {
        item.strip()
        for item in os.getenv("RUSA_GITHUB_ALLOWED_REPOSITORIES", "").split(",")
        if item.strip()
    }
    if not allowed_repositories:
        raise RuntimeError(
            "RUSA_GITHUB_ALLOWED_REPOSITORIES must contain explicit owner/repo entries"
        )
    github = GitHubRepositoryGateway(github_token)
    gateway = GitHubToolGateway(github, allowed_repositories=allowed_repositories)
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

"""Environment-backed construction of the production agent runtime."""

from __future__ import annotations

import os
from pathlib import Path

from .audit import AuditLog
from .contracts import WorkerRole
from .github_gateway import GitHubRepositoryGateway, GitHubToolGateway
from .memory import SQLiteMemory
from .orchestrator import AICodeOrchestrator
from .providers import OpenAICompatibleProvider
from .workers import AgentWorker


def build_orchestrator() -> AICodeOrchestrator:
    provider_url = os.getenv("RUSA_PROVIDER_BASE_URL", "")
    provider_model = os.getenv("RUSA_PROVIDER_MODEL", os.getenv("RUSA_MODEL_NAME", "rusa-ai-v1"))
    github_token = os.getenv("RUSA_GITHUB_TOKEN", "")
    if not provider_url:
        raise RuntimeError("RUSA_PROVIDER_BASE_URL must be configured for AICode workers")
    if not github_token:
        raise RuntimeError("RUSA_GITHUB_TOKEN must be configured for repository tools")

    provider = OpenAICompatibleProvider(
        provider_url,
        provider_model,
        os.getenv("RUSA_PROVIDER_API_KEY"),
        timeout_seconds=float(os.getenv("RUSA_PROVIDER_TIMEOUT_SECONDS", "120")),
    )
    github = GitHubRepositoryGateway(github_token)
    gateway = GitHubToolGateway(github)
    data_dir = Path(os.getenv("RUSA_AGENT_DATA_DIR", ".rusa-ai"))
    data_dir.mkdir(parents=True, exist_ok=True)
    memory = SQLiteMemory(str(data_dir / "aicode-memory.sqlite3"))
    audit = AuditLog(str(data_dir / "aicode-audit.jsonl"))
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

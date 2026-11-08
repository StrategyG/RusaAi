"""Per-agent execution authority for the Agentic DevOps worker fleet.

The registry is a local, fail-closed capability ceiling. It is not a grant:
the Policy Engine must still intersect these capabilities with tenant/resource
policy, approvals, budgets, deadlines, and the current cancellation state.
"""

from __future__ import annotations  # noqa: I001

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .contracts import validate_agent_task


PLAN_AGENT = "rusa_plan_agent"
CODE_AGENT = "rusa_code_agent"
BUILD_AGENT = "rusa_build_agent"
TEST_AGENT = "rusa_test_agent"
RELEASE_AGENT = "rusa_release_agent"
DEPLOY_AGENT = "rusa_deploy_agent"
OPERATE_AGENT = "rusa_operate_agent"
MONITOR_AGENT = "rusa_monitor_agent"
SECURITY_GUARD_AGENT = "rusa_security_guard_agent"

STAGE_AGENT_IDS = MappingProxyType(
    {
        "plan": PLAN_AGENT,
        "code": CODE_AGENT,
        "build": BUILD_AGENT,
        "test": TEST_AGENT,
        "release": RELEASE_AGENT,
        "deploy": DEPLOY_AGENT,
        "operate": OPERATE_AGENT,
        "monitor": MONITOR_AGENT,
    }
)

# These are observation-only capabilities. They cannot mutate a repository,
# workflow, deployment, runbook, or evidence record.
READ_ONLY_TOOLS = frozenset(
    {
        "alerts.read",
        "artifact.read",
        "audit.read",
        "evidence.read",
        "health.read",
        "issue.read",
        "logs.read",
        "metrics.read",
        "policy.read",
        "release.read",
        "repository.read",
        "runbook.read",
        "sbom.read",
        "traces.read",
    }
)

# The Code Agent receives a task-specific path scope. The workspace gateway
# applies a second deny-list for secrets, CI, migrations, schemas, and infra.
_CODE_FORBIDDEN_PREFIXES = (
    ".github/",
    "migrations/",
    "schemas/",
    "deploy/",
    "deployment/",
    "infra/",
    "infrastructure/",
    "terraform/",
    "k8s/",
    "kubernetes/",
    "helm/",
    "systemd/",
    ".devcontainer/",
)
_CODE_FORBIDDEN_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.example",
        ".gitignore",
        ".dockerignore",
        ".gitattributes",
        ".gitmodules",
        ".lfsconfig",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "pyproject.toml",
        "requirements.txt",
        "requirements-agent-sandbox.txt",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "uv.lock",
        "go.mod",
        "cargo.toml",
        "setup.py",
        "setup.cfg",
        "makefile",
        "containerfile",
        "procfile",
        "serverless.yml",
        "vercel.json",
        "netlify.toml",
        "pipfile",
        "pipfile.lock",
    }
)
_CODE_FORBIDDEN_PARTS = frozenset(
    {
        ".git",
        ".github",
        ".devcontainer",
        ".env",
        ".secrets",
        "secrets",
        "credentials",
        ".ssh",
        ".aws",
        ".azure",
        "migrations",
        "schemas",
        "deploy",
        "deployment",
        "infra",
        "infrastructure",
        "terraform",
        "k8s",
        "kubernetes",
        "helm",
        "systemd",
    }
)
_CODE_FORBIDDEN_SUFFIXES = frozenset(
    {".pem", ".key", ".p12", ".pfx", ".keystore", ".crt", ".cer", ".der", ".ppk", ".tfstate", ".tfvars"}
)


class AgentAuthorizationError(PermissionError):
    """A task exceeds the authenticated agent's stage or capability boundary."""


@dataclass(frozen=True)
class AgentPolicy:
    agent_id: str
    stage: str | None
    allowed_tools: frozenset[str]
    write_scope_prefixes: tuple[str, ...] | None
    read_only: bool = False
    allow_network: bool = False


_POLICIES = {
    PLAN_AGENT: AgentPolicy(
        agent_id=PLAN_AGENT,
        stage="plan",
        allowed_tools=frozenset(
            {
                "repository.read",
                "issue.read",
                "artifact.read",
                "plan.write",
                "task.create",
                "documentation.write",
            }
        ),
        write_scope_prefixes=("plans/", "requirements/", "backlog/", "acceptance/", "risk-assessment/"),
    ),
    CODE_AGENT: AgentPolicy(
        agent_id=CODE_AGENT,
        stage="code",
        allowed_tools=frozenset(
            {
                "repository.read",
                "repository.branch.write",
                "artifact.read",
                "artifact.write",
                "documentation.write",
                "test.write",
            }
        ),
        write_scope_prefixes=None,
    ),
    BUILD_AGENT: AgentPolicy(
        agent_id=BUILD_AGENT,
        stage="build",
        allowed_tools=frozenset(
            {
                "repository.read",
                "build.execute",
                "artifact.write",
                "manifest.write",
                "sbom.write",
            }
        ),
        write_scope_prefixes=("build/", "artifacts/", "manifests/", "sbom/"),
    ),
    TEST_AGENT: AgentPolicy(
        agent_id=TEST_AGENT,
        stage="test",
        allowed_tools=frozenset(
            {
                "repository.read",
                "artifact.read",
                "test.execute",
                "test.write",
                "evidence.write",
                "policy.read",
                "sbom.read",
            }
        ),
        write_scope_prefixes=("test-reports/", "evidence/"),
    ),
    RELEASE_AGENT: AgentPolicy(
        agent_id=RELEASE_AGENT,
        stage="release",
        allowed_tools=frozenset(
            {
                "artifact.read",
                "evidence.read",
                "sbom.read",
                "release.read",
                "release.prepare",
                "manifest.write",
                "policy.read",
            }
        ),
        write_scope_prefixes=("release/", "manifests/"),
    ),
    DEPLOY_AGENT: AgentPolicy(
        agent_id=DEPLOY_AGENT,
        stage="deploy",
        allowed_tools=frozenset(
            {
                "artifact.read",
                "release.read",
                "deployment.execute",
                "health.read",
                "evidence.write",
                "runbook.read",
                "policy.read",
            }
        ),
        write_scope_prefixes=("deployments/", "evidence/"),
    ),
    OPERATE_AGENT: AgentPolicy(
        agent_id=OPERATE_AGENT,
        stage="operate",
        allowed_tools=frozenset(
            {
                "health.read",
                "logs.read",
                "metrics.read",
                "traces.read",
                "incident.write",
                "runbook.read",
                "runbook.execute",
                "audit.read",
            }
        ),
        write_scope_prefixes=("incidents/", "reports/"),
    ),
    MONITOR_AGENT: AgentPolicy(
        agent_id=MONITOR_AGENT,
        stage="monitor",
        allowed_tools=READ_ONLY_TOOLS,
        write_scope_prefixes=(),
        read_only=True,
    ),
    SECURITY_GUARD_AGENT: AgentPolicy(
        agent_id=SECURITY_GUARD_AGENT,
        stage=None,
        allowed_tools=READ_ONLY_TOOLS,
        write_scope_prefixes=(),
        read_only=True,
    ),
}

AGENT_POLICIES = MappingProxyType(_POLICIES)


def _safe_relative_scope(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise AgentAuthorizationError("write_scope entries must be non-empty relative paths")
    if (
        value.startswith("/")
        or "\\" in value
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.rstrip("/").split("/"))
    ):
        raise AgentAuthorizationError("write_scope contains an unsafe path")
    return value


def _code_scope_is_allowed(scope: str) -> bool:
    normalized = scope.lower()
    parts = tuple(part.lower() for part in normalized.rstrip("/").split("/"))
    if any(
        part in _CODE_FORBIDDEN_PARTS or part.startswith(".env")
        for part in parts
    ):
        return False
    if parts and (
        parts[-1] in _CODE_FORBIDDEN_NAMES
        or parts[-1].startswith(("requirements", "dockerfile.", "docker-compose.", "compose."))
    ):
        return False
    if any(normalized.startswith(prefix) for prefix in _CODE_FORBIDDEN_PREFIXES):
        return False
    if any(
        any(part.endswith(suffix) for suffix in _CODE_FORBIDDEN_SUFFIXES)
        for part in parts
    ):
        return False
    return True


def _scope_matches(scope: str, allowed_prefixes: tuple[str, ...]) -> bool:
    normalized = scope.rstrip("/") + ("/" if scope.endswith("/") else "")
    for prefix in allowed_prefixes:
        if normalized == prefix or normalized.startswith(prefix):
            return True
        if not prefix.endswith("/") and normalized.rstrip("/") == prefix:
            return True
    return False


def authorize_agent_task(
    task: dict[str, Any],
    *,
    authenticated_principal: str,
) -> AgentPolicy:
    """Validate a task against its authenticated agent's least-privilege policy.

    The authenticated principal must come from a verified workload identity
    (for example, the mTLS/SPIFFE certificate), never from the task body or
    transport headers.
    """
    validate_agent_task(task)
    if not isinstance(authenticated_principal, str) or not authenticated_principal:
        raise AgentAuthorizationError("authenticated agent principal is required")

    agent_id = task["agent_id"]
    policy = AGENT_POLICIES.get(agent_id)
    if policy is None:
        raise AgentAuthorizationError("agent identity is not registered")
    if authenticated_principal != agent_id:
        raise AgentAuthorizationError("authenticated principal does not match task agent_id")
    if policy.stage is not None and task["stage"] != policy.stage:
        raise AgentAuthorizationError("agent identity is not authorized for this lifecycle stage")

    constraints = task["constraints"]
    requested_tools = frozenset(constraints["allowed_tools"])
    excess_tools = requested_tools - policy.allowed_tools
    if excess_tools:
        raise AgentAuthorizationError(
            "task requests tools outside the agent allowlist: "
            + ", ".join(sorted(excess_tools))
        )

    if constraints["network_access"] and not policy.allow_network:
        raise AgentAuthorizationError("network access is not permitted for this agent identity")
    destinations = constraints.get("allowed_network_destinations", [])
    if destinations and not policy.allow_network:
        raise AgentAuthorizationError("network destinations are not permitted for this agent identity")

    write_scope = constraints["write_scope"]
    if policy.read_only and write_scope:
        raise AgentAuthorizationError("read-only agent identities cannot receive write_scope")
    for raw_scope in write_scope:
        scope = _safe_relative_scope(raw_scope)
        if policy.write_scope_prefixes is not None:
            if not _scope_matches(scope, policy.write_scope_prefixes):
                raise AgentAuthorizationError(
                    f"write_scope is outside the {task['stage']} agent's allowed paths: {scope}"
                )
        elif policy.agent_id == CODE_AGENT and not _code_scope_is_allowed(scope):
            raise AgentAuthorizationError(
                f"Code Agent write_scope contains a protected repository path: {scope}"
            )

    approval_required = constraints["approval_required"]
    approval_scope = constraints.get("approval_scope", [])
    if approval_required and not approval_scope:
        raise AgentAuthorizationError("approval_required needs an explicit approval_scope")
    if approval_scope and not approval_required:
        raise AgentAuthorizationError("approval_scope cannot be present when approval_required is false")

    if task["stage"] == "deploy" and task["input"].get("environment") == "production":
        approval_ref = constraints.get("approval_ref")
        if (
            not approval_required
            or "deploy" not in approval_scope
            or not isinstance(approval_ref, str)
            or not approval_ref
        ):
            raise AgentAuthorizationError(
                "production deployment requires approval_required, deploy approval_scope, and approval_ref"
            )

    return policy


def registered_agent_ids() -> tuple[str, ...]:
    """Return the registered workload identities in stable order."""
    return tuple(sorted(AGENT_POLICIES))

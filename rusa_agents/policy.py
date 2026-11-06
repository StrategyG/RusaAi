"""Least-privilege policy for autonomous repository operations."""

from dataclasses import dataclass
from pathlib import PurePosixPath

from .contracts import RepositoryTarget, RiskLevel


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str


class AutonomyPolicy:
    ALLOWED_ACTIONS = {
        "repo_read", "branch_create", "repo_write", "pull_request_create", "ci_read",
    }
    APPROVAL_ACTIONS = {
        "merge", "deploy", "release", "secret_change", "permission_change",
        "production_write", "destructive_action", "workflow_change", "dependency_change",
    }
    DENIED_ACTIONS = {
        "disable_audit", "read_secret", "exfiltrate_secret", "force_push",
        "force_push_main", "arbitrary_shell", "unrestricted_network", "delete_repository",
    }
    PROTECTED_PATHS = (
        ".github/workflows", ".github/actions", ".github/codeowners",
        "scripts/postgres", ".env", "secrets", "credentials", ".aws", ".ssh",
    )
    PROTECTED_FILES = {
        "codeowners", "dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "compose.yml", "compose.yaml", "pyproject.toml", "poetry.lock", "uv.lock",
        "pipfile", "pipfile.lock", "package.json", "package-lock.json",
        "pnpm-lock.yaml", "yarn.lock", "cargo.toml", "cargo.lock",
        "go.mod", "go.sum", "gemfile", "gemfile.lock",
        "composer.json", "composer.lock", ".npmrc", ".pypirc", ".netrc",
        ".git-credentials", "credentials.json", "secrets.json",
    }

    def __init__(self, allowed_actions: set[str] | None = None) -> None:
        self.allowed_actions = set(self.ALLOWED_ACTIONS if allowed_actions is None else allowed_actions)

    @staticmethod
    def safe_repository_path(path: str) -> bool:
        if not path or path.startswith("/") or "\\" in path:
            return False
        parts = path.split("/")
        if any(part.lower() in {"", ".", "..", ".git"} for part in parts):
            return False
        candidate = PurePosixPath(path)
        if candidate.is_absolute():
            return False
        lowered = path.lower()
        if any(
            lowered == protected or lowered.startswith(protected + "/")
            for protected in AutonomyPolicy.PROTECTED_PATHS
        ):
            return False
        filename = parts[-1].lower()
        if filename in AutonomyPolicy.PROTECTED_FILES:
            return False
        if filename.startswith("requirements") and filename.endswith(".txt"):
            return False
        if lowered.endswith((".pem", ".key", ".p12", ".pfx", ".env")):
            return False
        return not any(
            part.lower() in {
                "id_rsa", "id_ed25519", "secrets", "credentials", ".env",
            }
            or part.lower().startswith(".env.")
            for part in parts
        )

    def evaluate(
        self,
        action: str,
        *,
        risk: RiskLevel = RiskLevel.LOW,
        repository: RepositoryTarget | None = None,
        path: str | None = None,
    ) -> PolicyDecision:
        if action in self.DENIED_ACTIONS:
            return PolicyDecision(False, False, f"Action '{action}' is prohibited")
        if action in self.APPROVAL_ACTIONS:
            return PolicyDecision(False, True, f"Human approval is required for '{action}'")
        if risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return PolicyDecision(False, True, f"{risk.value} risk requires human approval")
        if action not in self.allowed_actions:
            return PolicyDecision(False, False, f"Action '{action}' is not allowlisted")
        if path is not None and not self.safe_repository_path(path):
            return PolicyDecision(False, True, "Repository path is protected or unsafe")
        if action == "repo_write" and (
            repository is None or repository.work_branch == repository.base_branch
        ):
            return PolicyDecision(False, False, "Writes require an isolated work branch")
        return PolicyDecision(True, False, "Allowed by autonomy policy")

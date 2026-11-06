"""Feature-branch worktree lifecycle and GitHub publication."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess  # nosec B404 - fixed argv only, shell execution is never enabled
import tempfile
import shutil
from pathlib import Path
from urllib.parse import urlparse

from .contracts import CodingTask

GIT_BIN = shutil.which("git")
BRANCH_PATTERN = re.compile(r"^(feature|fix|chore|docs)/[A-Za-z0-9._/-]{1,180}$")
BLOCKED_PATH_PARTS = {".git", ".env", ".secrets", "secrets", "credentials", ".ssh", ".aws", ".gitattributes", ".gitmodules", ".lfsconfig"}
BLOCKED_SUFFIXES = {
    ".pem", ".key", ".p12", ".pfx", ".keystore", ".crt", ".cer", ".der",
    ".ppk", ".tfstate", ".tfvars",
}
BLOCKED_WRITE_NAMES = {
    ".env.example", ".gitignore", ".dockerignore", "requirements.txt",
    "requirements-agent-sandbox.txt", "pyproject.toml", "dockerfile",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "containerfile", "makefile", "procfile", "serverless.yml",
    "vercel.json", "netlify.toml",
    "setup.py", "setup.cfg", "package.json", "package-lock.json",
    "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock",
    "pipfile", "pipfile.lock", "cargo.toml", "go.mod", ".npmrc", ".pypirc", ".netrc",
}
BLOCKED_WRITE_PREFIXES = (
    ".github/", "migrations/", "schemas/", "llm-project/configs/",
    "deploy/", "deployment/", "infra/", "infrastructure/", "terraform/",
    "k8s/", "kubernetes/", "helm/", "systemd/", ".devcontainer/",
)


class GitWorkspaceManager:
    """Creates one Git worktree per task from a pre-fetched local bare mirror."""

    def __init__(
        self, mirror_root: str, workspace_root: str, github_token: str | None = None
    ):
        self.mirror_root = Path(mirror_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.github_token = github_token
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def _run(
        self,
        args: list[str],
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.getenv("PATH", "/usr/bin:/bin"),
            "HOME": tempfile.gettempdir(),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        if extra_env:
            env.update(extra_env)
        if not GIT_BIN:
            raise RuntimeError("Git executable is unavailable in the worker image")
        if args and args[0] == "git":
            args = [GIT_BIN, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args[1:]]
        return subprocess.run(  # nosec B603 - static argv, absolute executable, shell=False
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=120
        )

    def refresh(self, repository: str, base_branch: str) -> None:
        owner, repo = repository.split("/", 1)
        mirror = (self.mirror_root / owner / f"{repo}.git").resolve()
        if self.mirror_root not in mirror.parents or not mirror.is_dir():
            raise FileNotFoundError(f"Authorized bare Git mirror is missing: {repository}")
        remote = self._run(["git", "--git-dir", str(mirror), "remote", "get-url", "origin"])
        if remote.returncode:
            raise RuntimeError("Git mirror origin is unavailable")
        GitHubPublisher.validate_origin_url(remote.stdout, repository)

        askpass_path = None
        extra_env: dict[str, str] = {}
        try:
            if self.github_token:
                askpass_fd, askpass_name = tempfile.mkstemp(prefix="rusa-fetch-askpass-")
                os.close(askpass_fd)
                askpass = Path(askpass_name)
                askpass.write_text(
                    '#!/bin/sh\ncase "$1" in *Username*) printf "%s" "x-access-token" ;; *) printf "%s" "$GITHUB_TOKEN" ;; esac\n',
                    encoding="utf-8",
                )
                os.chmod(askpass, 0o700)
                askpass_path = askpass
                extra_env = {
                    "GIT_ASKPASS": str(askpass),
                    "GIT_ASKPASS_REQUIRE": "force",
                    "GITHUB_TOKEN": self.github_token,
                }
            fetched = self._run(
                [
                    "git", "-c", "credential.helper=", "--git-dir", str(mirror),
                    "fetch", "--prune", "origin",
                    f"+refs/heads/{base_branch}:refs/heads/{base_branch}",
                ],
                extra_env=extra_env,
            )
            if fetched.returncode:
                raise RuntimeError(f"Git mirror refresh failed: {fetched.stderr[-2000:]}")
        finally:
            if askpass_path:
                askpass_path.unlink(missing_ok=True)

    def create(self, task: CodingTask, tenant_id: str) -> Path:
        owner, repo = task.repository.full_name.split("/", 1)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid repository identity")
        branch = task.repository.work_branch
        if not BRANCH_PATTERN.fullmatch(branch) or ".." in branch or "//" in branch:
            raise ValueError("Work branch must be a safe feature/fix/chore/docs branch")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", task.repository.base_branch) or ".." in task.repository.base_branch:
            raise ValueError("Base branch is invalid")
        if task.repository.base_branch in {"main", "master"} and branch in {"main", "master"}:
            raise ValueError("Agent may not work directly on a protected branch")

        mirror = (self.mirror_root / owner / f"{repo}.git").resolve()
        if self.mirror_root not in mirror.parents or not mirror.is_dir():
            raise FileNotFoundError(f"Pre-fetched bare mirror not found for {task.repository.full_name}")
        self.refresh(task.repository.full_name, task.repository.base_branch)
        self._run(["git", "--git-dir", str(mirror), "worktree", "prune", "--expire", "now"])
        bare = self._run(["git", "--git-dir", str(mirror), "rev-parse", "--is-bare-repository"])
        if bare.returncode or bare.stdout.strip() != "true":
            raise ValueError("Repository mirror must be a valid bare Git repository")

        tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:20]
        attempt_key = hashlib.sha256(
            str(task.metadata.get("lease_token", "legacy")).encode("utf-8")
        ).hexdigest()[:12]
        workspace = (self.workspace_root / tenant_key / task.task_id / attempt_key).resolve()
        if self.workspace_root not in workspace.parents:
            raise ValueError("Workspace path escaped configured root")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            current = self._run(["git", "-C", str(workspace), "branch", "--show-current"])
            if current.returncode == 0 and current.stdout.strip() == branch:
                return workspace
            raise FileExistsError("Task workspace exists but is not on the expected branch")

        existing_branch = self._run([
            "git", "--git-dir", str(mirror), "show-ref", "--verify",
            f"refs/heads/{branch}",
        ])
        command = [
            "git", "-c", "core.hooksPath=/dev/null",
            "--git-dir", str(mirror), "worktree", "add",
        ]
        if existing_branch.returncode == 0:
            command.extend([str(workspace), branch])
        else:
            command.extend(["-b", branch, str(workspace), task.repository.base_branch])
        created = self._run(command)
        if created.returncode:
            raise RuntimeError(f"Could not create feature worktree: {created.stderr[-3000:]}")
        return workspace

    def cleanup(self, task: CodingTask, tenant_id: str) -> None:
        owner, repo = task.repository.full_name.split("/", 1)
        mirror = (self.mirror_root / owner / f"{repo}.git").resolve()
        tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:20]
        attempt_key = hashlib.sha256(
            str(task.metadata.get("lease_token", "legacy")).encode("utf-8")
        ).hexdigest()[:12]
        workspace = (self.workspace_root / tenant_key / task.task_id / attempt_key).resolve()
        if self.mirror_root not in mirror.parents or self.workspace_root not in workspace.parents:
            raise ValueError("Cleanup path escaped configured roots")
        if not workspace.exists():
            return
        current = self._run(["git", "-C", str(workspace), "branch", "--show-current"])
        if current.returncode or current.stdout.strip() != task.repository.work_branch:
            raise RuntimeError("Refusing to clean a workspace on an unexpected branch")
        removed = self._run([
            "git", "--git-dir", str(mirror), "worktree", "remove", "--force", str(workspace)
        ])
        if removed.returncode:
            raise RuntimeError(f"Git worktree cleanup failed: {removed.stderr[-2000:]}")
        self._run(["git", "--git-dir", str(mirror), "branch", "-D", task.repository.work_branch])


class GitHubPublisher:
    """Commits only safe workspace changes, pushes one feature branch, and opens a draft PR."""

    def __init__(self, github_token: str | None = None):
        self.token = github_token or os.getenv("RUSA_GITHUB_TOKEN")
        if not self.token:
            raise ValueError("RUSA_GITHUB_TOKEN is required for GitHub publication")

    @staticmethod
    def _run(args: list[str], cwd: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        base_env = {
            "PATH": os.getenv("PATH", "/usr/bin:/bin"),
            "HOME": tempfile.gettempdir(),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        if env:
            base_env.update(env)
        if not GIT_BIN:
            raise RuntimeError("Git executable is unavailable in the worker image")
        if args and args[0] == "git":
            args = [GIT_BIN, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args[1:]]
        return subprocess.run(  # nosec B603 - static argv, absolute executable, shell=False
            args, cwd=cwd, env=base_env, capture_output=True, text=True, timeout=180
        )

    @staticmethod
    def _safe_changed_paths(workspace: Path) -> list[str]:
        if not GIT_BIN:
            return []
        result = subprocess.run(  # nosec B603 - static argv, absolute executable, shell=False
            [GIT_BIN, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
             "-C", str(workspace), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True, timeout=30, check=False,
            env={
                "PATH": os.getenv("PATH", "/usr/bin:/bin"),
                "HOME": tempfile.gettempdir(),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_LFS_SKIP_SMUDGE": "1",
            },
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        paths = []
        records = result.stdout.split(b"\0")
        skip_next = False
        for record in records:
            if not record:
                continue
            if skip_next:
                skip_next = False
                continue
            status = record[:2].decode("ascii", "replace")
            raw_path = record[3:].decode("utf-8", "surrogateescape")
            if "R" in status or "C" in status:
                skip_next = True
            path = Path(raw_path)
            parts = {part.lower() for part in path.parts}
            if (
                path.is_absolute()
                or ".." in path.parts
                or parts.intersection(BLOCKED_PATH_PARTS)
                or any(part.startswith(".env") and part != ".env.example" for part in parts)
            ):
                continue
            if (
                path.suffix.lower() in BLOCKED_SUFFIXES
                or path.name.lower() in {"id_rsa", "id_ed25519"}
                or path.name.lower() in BLOCKED_WRITE_NAMES
                or path.name.lower().startswith(("requirements", "dockerfile.", "docker-compose.", "compose."))
                or any(raw_path.lower().startswith(prefix.lower()) for prefix in BLOCKED_WRITE_PREFIXES)
            ):
                continue
            candidate = (workspace / path).resolve(strict=False)
            if candidate != workspace and workspace not in candidate.parents:
                continue
            if (workspace / path).is_symlink():
                continue
            paths.append(raw_path)
        return sorted(set(paths))

    @staticmethod
    def validate_origin_url(remote_url: str, repository: str) -> None:
        parsed = urlparse(remote_url.strip())
        remote_repository = parsed.path.strip("/").removesuffix(".git")
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or remote_repository.lower() != repository.lower()
        ):
            raise RuntimeError(
                "Origin must be credential-free HTTPS to the exact authorized GitHub repository"
            )

    def commit_and_push(self, workspace: str, repository: str, branch: str, message: str) -> str | None:
        root = Path(workspace).resolve(strict=True)
        if not BRANCH_PATTERN.fullmatch(branch) or branch in {"main", "master"}:
            raise ValueError("Publishing is restricted to task feature branches")
        current = self._run(["git", "branch", "--show-current"], str(root))
        if current.returncode or current.stdout.strip() != branch:
            raise RuntimeError("Workspace branch does not match the task branch")
        remote = self._run(["git", "remote", "get-url", "origin"], str(root))
        if remote.returncode:
            raise RuntimeError("Origin remote is unavailable")
        self.validate_origin_url(remote.stdout, repository)

        changed = self._safe_changed_paths(root)
        if changed:
            added = self._run(["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "add", "--", *changed], str(root))
            if added.returncode:
                raise RuntimeError(f"Git staging failed: {added.stderr[-3000:]}")
            commit = self._run([
                "git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=RUSA-AI",
                "-c", "user.email=rusa-ai@users.noreply.github.com",
                "commit", "--no-gpg-sign", "-m", message[:200],
            ], str(root))
            if commit.returncode:
                raise RuntimeError(f"Git commit failed: {commit.stderr[-3000:]}")
            sha = self._run(["git", "rev-parse", "HEAD"], str(root)).stdout.strip()
        else:
            sha = self._run(["git", "rev-parse", "HEAD"], str(root)).stdout.strip()

        askpass_fd, askpass_name = tempfile.mkstemp(prefix="rusa-askpass-")
        os.close(askpass_fd)
        askpass = Path(askpass_name)
        askpass.write_text(
            "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' 'x-access-token' ;; "
            "*Password*) printf '%s\\n' \"$RUSA_GITHUB_TOKEN\" ;; esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        try:
            pushed = self._run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "push", "origin", f"HEAD:refs/heads/{branch}"],
                str(root),
                {"GIT_ASKPASS": str(askpass), "GIT_ASKPASS_REQUIRE": "force", "RUSA_GITHUB_TOKEN": self.token},
            )
        finally:
            askpass.unlink(missing_ok=True)
        if pushed.returncode:
            raise RuntimeError(f"Git push failed: {pushed.stderr[-3000:]}")
        return sha

    async def create_or_get_draft_pr(self, repository: str, branch: str, base: str, title: str, body: str) -> str:
        import httpx

        owner = repository.split("/", 1)[0]
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            existing = await client.get(
                f"https://api.github.com/repos/{repository}/pulls",
                headers=headers,
                params={"state": "open", "head": f"{owner}:{branch}", "base": base},
            )
            existing.raise_for_status()
            items = existing.json()
            if items:
                return items[0]["html_url"]
            created = await client.post(
                f"https://api.github.com/repos/{repository}/pulls",
                headers=headers,
                json={"title": title, "head": branch, "base": base, "body": body, "draft": True},
            )
            created.raise_for_status()
            return created.json()["html_url"]


    async def wait_for_ci(
        self,
        repository: str,
        commit_sha: str,
        timeout_seconds: int = 900,
        poll_seconds: int = 15,
    ) -> tuple[bool, str]:
        """Wait for the required PR CI jobs; missing or failed evidence never passes."""
        import asyncio

        import httpx

        required = {"Test, lint, and security", "Build production image"}
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    response = await client.get(
                        f"https://api.github.com/repos/{repository}/commits/{commit_sha}/check-runs",
                        headers=headers,
                        params={"per_page": 100},
                    )
                    response.raise_for_status()
                    check_runs = response.json().get("check_runs", [])
                except Exception as exc:
                    return False, f"CI status could not be verified: {type(exc).__name__}"

                relevant = {
                    run.get("name"): run for run in check_runs
                    if run.get("name") in required
                }
                failed = [
                    name for name, run in relevant.items()
                    if run.get("status") == "completed"
                    and run.get("conclusion") not in {"success", "neutral", "skipped"}
                ]
                if failed:
                    return False, "Required CI jobs failed: " + ", ".join(sorted(failed))
                if required.issubset(relevant):
                    if all(
                        relevant[name].get("status") == "completed"
                        and relevant[name].get("conclusion") in {"success", "neutral"}
                        for name in required
                    ):
                        return True, "All required GitHub Actions checks passed"
                await asyncio.sleep(poll_seconds)
        return False, "Required GitHub Actions checks did not finish before the CI deadline"

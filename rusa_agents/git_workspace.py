"""Feature-branch worktree lifecycle and GitHub publication."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess  # nosec B404 - fixed argv only, shell execution is never enabled
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse


def _run_bounded_process(
    args: list[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    timeout_seconds: int,
    cancellation_check: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess:
    """Run fixed argv and terminate the whole process group when the lease is revoked."""
    if cancellation_check is None:
        return subprocess.run(  # nosec B603 - fixed argv, shell=False
            args, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=timeout_seconds,
        )

    process = subprocess.Popen(  # nosec B603 - fixed argv, shell=False
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout_seconds

    def stop_process() -> tuple[str, str]:
        # Git may spawn credential/transport helpers. Kill the process group, not
        # only the Git parent, so a revoked lease cannot leave a child running.
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            elif process.poll() is None:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            return process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.poll() is None:
                    process.kill()
            except ProcessLookupError:
                pass
            return process.communicate()

    while True:
        try:
            allowed = cancellation_check()
        except Exception as exc:
            stop_process()
            raise RuntimeError(
                "Could not verify the active task lease during Git execution"
            ) from exc
        if not allowed:
            stop_process()
            raise RuntimeError(
                "Git execution stopped because the task lease was lost or cancelled"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr = stop_process()
            raise subprocess.TimeoutExpired(
                args, timeout_seconds, output=stdout, stderr=stderr
            )
        try:
            stdout, stderr = process.communicate(timeout=min(0.5, remaining))
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            continue

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
        cancellation_check: Callable[[], bool] | None = None,
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
        return _run_bounded_process(
            args,
            cwd=cwd,
            env=env,
            timeout_seconds=120,
            cancellation_check=cancellation_check,
        )

    def refresh(
        self,
        repository: str,
        base_branch: str,
        publish_branch: str | None = None,
        *,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> None:
        owner, repo = repository.split("/", 1)
        mirror = (self.mirror_root / owner / f"{repo}.git").resolve()
        if self.mirror_root not in mirror.parents or not mirror.is_dir():
            raise FileNotFoundError(f"Authorized bare Git mirror is missing: {repository}")
        remote = self._run(
            ["git", "--git-dir", str(mirror), "remote", "get-url", "origin"],
            cancellation_check=cancellation_check,
        )
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
                cancellation_check=cancellation_check,
            )
            if fetched.returncode:
                raise RuntimeError(f"Git mirror refresh failed: {fetched.stderr[-2000:]}")
            if publish_branch:
                remote_branch = self._run(
                    [
                        "git", "-c", "credential.helper=", "--git-dir", str(mirror),
                        "ls-remote", "--heads", "origin",
                        f"refs/heads/{publish_branch}",
                    ],
                    extra_env=extra_env,
                    cancellation_check=cancellation_check,
                )
                if remote_branch.returncode:
                    raise RuntimeError(
                        f"Could not inspect stable task branch: {remote_branch.stderr[-2000:]}"
                    )
                if remote_branch.stdout.strip():
                    fetched_publish_branch = self._run(
                        [
                            "git", "-c", "credential.helper=", "--git-dir", str(mirror),
                            "fetch", "--no-tags", "origin",
                            f"+refs/heads/{publish_branch}:refs/heads/{publish_branch}",
                        ],
                        extra_env=extra_env,
                        cancellation_check=cancellation_check,
                    )
                    if fetched_publish_branch.returncode:
                        raise RuntimeError(
                            "Could not refresh stable task branch: "
                            f"{fetched_publish_branch.stderr[-2000:]}"
                        )
                else:
                    removed_stale_branch = self._run([
                        "git", "--git-dir", str(mirror), "update-ref", "-d",
                        f"refs/heads/{publish_branch}",
                    ], cancellation_check=cancellation_check)
                    if removed_stale_branch.returncode:
                        raise RuntimeError(
                            "Could not remove stale local stable task branch: "
                            f"{removed_stale_branch.stderr[-2000:]}"
                        )
        finally:
            if askpass_path:
                askpass_path.unlink(missing_ok=True)

    def create(
        self,
        task: CodingTask,
        tenant_id: str,
        *,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> Path:
        owner, repo = task.repository.full_name.split("/", 1)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid repository identity")
        branch = task.repository.work_branch
        publish_branch = task.metadata.get("publish_branch")
        if not BRANCH_PATTERN.fullmatch(branch) or ".." in branch or "//" in branch:
            raise ValueError("Work branch must be a safe feature/fix/chore/docs branch")
        if publish_branch is not None and (
            not isinstance(publish_branch, str)
            or not BRANCH_PATTERN.fullmatch(publish_branch)
            or publish_branch in {"main", "master"}
            or ".." in publish_branch
            or "//" in publish_branch
        ):
            raise ValueError("Stable publish branch must be a safe task feature branch")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", task.repository.base_branch) or ".." in task.repository.base_branch:
            raise ValueError("Base branch is invalid")
        if task.repository.base_branch in {"main", "master"} and branch in {"main", "master"}:
            raise ValueError("Agent may not work directly on a protected branch")

        mirror = (self.mirror_root / owner / f"{repo}.git").resolve()
        if self.mirror_root not in mirror.parents or not mirror.is_dir():
            raise FileNotFoundError(f"Pre-fetched bare mirror not found for {task.repository.full_name}")
        if cancellation_check is not None and not cancellation_check():
            raise RuntimeError("Workspace creation stopped because the task lease is inactive")
        if publish_branch and cancellation_check is not None:
            self.refresh(
                task.repository.full_name,
                task.repository.base_branch,
                publish_branch,
                cancellation_check=cancellation_check,
            )
        elif publish_branch:
            self.refresh(
                task.repository.full_name,
                task.repository.base_branch,
                publish_branch,
            )
        elif cancellation_check is not None:
            self.refresh(
                task.repository.full_name,
                task.repository.base_branch,
                cancellation_check=cancellation_check,
            )
        else:
            self.refresh(task.repository.full_name, task.repository.base_branch)
        self._run(
            ["git", "--git-dir", str(mirror), "worktree", "prune", "--expire", "now"],
            cancellation_check=cancellation_check,
        )
        bare = self._run(
            ["git", "--git-dir", str(mirror), "rev-parse", "--is-bare-repository"],
            cancellation_check=cancellation_check,
        )
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

        existing_branch = self._run(
            [
                "git", "--git-dir", str(mirror), "show-ref", "--verify",
                f"refs/heads/{branch}",
            ],
            cancellation_check=cancellation_check,
        )
        command = [
            "git", "-c", "core.hooksPath=/dev/null",
            "--git-dir", str(mirror), "worktree", "add",
        ]
        if existing_branch.returncode == 0:
            command.extend([str(workspace), branch])
        else:
            start_point = task.repository.base_branch
            if publish_branch:
                existing_publish_branch = self._run(
                    [
                        "git", "--git-dir", str(mirror), "show-ref", "--verify",
                        f"refs/heads/{publish_branch}",
                    ],
                    cancellation_check=cancellation_check,
                )
                if existing_publish_branch.returncode == 0:
                    start_point = publish_branch
            command.extend(["-b", branch, str(workspace), start_point])
        created = self._run(command, cancellation_check=cancellation_check)
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

    REQUIRED_CI_CHECKS = frozenset({"Test, lint, and security", "Build, test, and scan production images"})
    GITHUB_ACTIONS_APP_ID = 15368

    def __init__(self, github_token: str | None = None):
        self.token = github_token or os.getenv("RUSA_GITHUB_TOKEN")
        if not self.token:
            raise ValueError("RUSA_GITHUB_TOKEN is required for GitHub publication")

    @staticmethod
    def _run(
        args: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
        *,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> subprocess.CompletedProcess:
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
        return _run_bounded_process(
            args,
            cwd=cwd,
            env=base_env,
            timeout_seconds=180,
            cancellation_check=cancellation_check,
        )

    @staticmethod
    def _safe_changed_paths(
        workspace: Path,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> list[str]:
        if not GIT_BIN:
            return []
        if cancellation_check is not None and not cancellation_check():
            raise RuntimeError("Git publication stopped because the task lease is inactive")
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
        if cancellation_check is not None and not cancellation_check():
            raise RuntimeError("Git publication stopped because the task lease is inactive")
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

    def commit_and_push(
        self,
        workspace: str,
        repository: str,
        branch: str,
        message: str,
        *,
        publish_branch: str | None = None,
        cancellation_check: Callable[[], bool] | None = None,
    ) -> str | None:
        root = Path(workspace).resolve(strict=True)
        target_branch = publish_branch or branch
        for candidate in (branch, target_branch):
            if (
                not BRANCH_PATTERN.fullmatch(candidate)
                or candidate in {"main", "master"}
                or ".." in candidate
                or "//" in candidate
            ):
                raise ValueError("Publishing is restricted to safe task feature branches")
        def ensure_active() -> None:
            if cancellation_check is not None and not cancellation_check():
                raise RuntimeError(
                    "Git publication stopped because the task lease was lost or cancelled"
                )

        ensure_active()
        current = self._run(
            ["git", "branch", "--show-current"], str(root),
            cancellation_check=cancellation_check,
        )
        if current.returncode or current.stdout.strip() != branch:
            raise RuntimeError("Workspace branch does not match the task branch")
        remote = self._run(
            ["git", "remote", "get-url", "origin"], str(root),
            cancellation_check=cancellation_check,
        )
        if remote.returncode:
            raise RuntimeError("Origin remote is unavailable")
        self.validate_origin_url(remote.stdout, repository)

        changed = self._safe_changed_paths(root, cancellation_check)
        if changed:
            added = self._run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "add", "--", *changed],
                str(root),
                cancellation_check=cancellation_check,
            )
            if added.returncode:
                raise RuntimeError(f"Git staging failed: {added.stderr[-3000:]}")
            commit = self._run([
                "git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=RUSA-AI",
                "-c", "user.email=rusa-ai@users.noreply.github.com",
                "commit", "--no-gpg-sign", "-m", message[:200],
            ], str(root), cancellation_check=cancellation_check)
            if commit.returncode:
                raise RuntimeError(f"Git commit failed: {commit.stderr[-3000:]}")
            sha = self._run(
                ["git", "rev-parse", "HEAD"], str(root),
                cancellation_check=cancellation_check,
            ).stdout.strip()
        else:
            sha = self._run(
                ["git", "rev-parse", "HEAD"], str(root),
                cancellation_check=cancellation_check,
            ).stdout.strip()

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
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "push", "origin", f"HEAD:refs/heads/{target_branch}"],
                str(root),
                {"GIT_ASKPASS": str(askpass), "GIT_ASKPASS_REQUIRE": "force", "RUSA_GITHUB_TOKEN": self.token},
                cancellation_check=cancellation_check,
            )
        finally:
            askpass.unlink(missing_ok=True)
        if pushed.returncode:
            raise RuntimeError(f"Git push failed: {pushed.stderr[-3000:]}")
        ensure_active()
        return sha

    async def create_or_get_draft_pr(self, repository: str, branch: str, base: str, title: str, body: str) -> str:
        import httpx

        task_match = re.search(
            r"(?:RUSA-AI-Task-ID|Task ID):\s*([A-Za-z0-9-]{1,128})",
            body,
        )
        task_id = task_match.group(1) if task_match else None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            async def find_existing() -> str | None:
                closed_match: str | None = None
                legacy_branch_match: str | None = None
                for page in range(1, 11):
                    response = await client.get(
                        f"https://api.github.com/repos/{repository}/pulls",
                        headers=headers,
                        params={
                            "state": "all",
                            "base": base,
                            "per_page": 100,
                            "page": page,
                        },
                    )
                    response.raise_for_status()
                    items = response.json()
                    if not isinstance(items, list):
                        raise RuntimeError("GitHub returned an invalid pull request listing")
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        head_ref = (item.get("head") or {}).get("ref")
                        base_ref = (item.get("base") or {}).get("ref")
                        item_body = item.get("body") or ""
                        matches_task = bool(
                            task_id
                            and re.search(
                                r"(?:RUSA-AI-Task-ID|Task ID):\s*"
                                + re.escape(task_id)
                                + r"\b",
                                item_body,
                            )
                        )
                        matches_branch = head_ref == branch and base_ref == base
                        if matches_branch:
                            if item.get("state") == "open":
                                return item.get("html_url")
                            closed_match = item.get("html_url") or closed_match
                        elif matches_task:
                            # A legacy per-lease PR cannot be assumed to contain the
                            # stable publication branch's current commit.
                            legacy_branch_match = item.get("html_url") or legacy_branch_match
                    if len(items) < 100:
                        break
                else:
                    raise ValueError(
                        "Could not safely reconcile task PR: search exceeded 1000 pull requests"
                    )
                if closed_match:
                    raise ValueError(
                        "A previous pull request for this task is closed; manual reconciliation is required"
                    )
                if legacy_branch_match:
                    raise ValueError(
                        "A previous task pull request uses a lease-specific branch; "
                        "manual reconciliation is required before publishing to the stable task branch"
                    )
                return None

            existing_url = await find_existing()
            if existing_url:
                return existing_url

            created = await client.post(
                f"https://api.github.com/repos/{repository}/pulls",
                headers=headers,
                json={"title": title, "head": branch, "base": base, "body": body, "draft": True},
            )
            try:
                created.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 422:
                    # Two recovered workers may race between lookup and creation. GitHub
                    # rejects a second open PR for the same head/base; reconcile by task ID.
                    existing_url = await find_existing()
                    if existing_url:
                        return existing_url
                raise
            return created.json()["html_url"]


    @staticmethod
    def _evaluate_check_runs(
        check_runs: list[dict],
        commit_sha: str,
    ) -> tuple[str, str]:
        """Evaluate only the newest trusted GitHub Actions suite for this SHA.

        A commit can have multiple suites (for example, push and pull_request).
        Required jobs must belong to one suite; an older green suite must never
        mask a newer pending or failed suite.
        """
        suites: dict[int, list[dict]] = {}
        for run in check_runs:
            if not isinstance(run, dict) or run.get("name") not in GitHubPublisher.REQUIRED_CI_CHECKS:
                continue
            if run.get("head_sha") != commit_sha:
                continue
            app = run.get("app")
            if (
                not isinstance(app, dict)
                or app.get("id") != GitHubPublisher.GITHUB_ACTIONS_APP_ID
                or app.get("slug") != "github-actions"
            ):
                continue
            suite = run.get("check_suite")
            suite_id = suite.get("id") if isinstance(suite, dict) else None
            run_id = run.get("id")
            if type(suite_id) is not int or suite_id <= 0:
                continue
            if type(run_id) is not int or run_id <= 0:
                continue
            suites.setdefault(suite_id, []).append(run)

        if not suites:
            return "pending", "No trusted GitHub Actions checks were found for the requested commit"

        latest_suite_id = max(suites)
        latest_by_name: dict[str, dict] = {}
        for run in suites[latest_suite_id]:
            name = run["name"]
            previous = latest_by_name.get(name)
            if previous is None or run["id"] > previous["id"]:
                latest_by_name[name] = run

        missing = GitHubPublisher.REQUIRED_CI_CHECKS.difference(latest_by_name)
        if missing:
            return (
                "pending",
                "Latest trusted CI suite is incomplete; missing: " + ", ".join(sorted(missing)),
            )

        pending = [
            name for name, run in latest_by_name.items()
            if run.get("status") != "completed"
        ]
        if pending:
            return "pending", "Required CI jobs are still running: " + ", ".join(sorted(pending))

        failed = [
            name for name, run in latest_by_name.items()
            if run.get("conclusion") not in {"success", "neutral"}
        ]
        if failed:
            return "failure", "Required CI jobs failed or were skipped: " + ", ".join(sorted(failed))

        return "success", "Latest trusted GitHub Actions suite passed for the exact commit SHA"

    async def wait_for_ci(
        self,
        repository: str,
        commit_sha: str,
        timeout_seconds: int = 900,
        poll_seconds: int = 15,
    ) -> tuple[bool, str]:
        """Wait for complete, trusted CI evidence on the exact commit SHA."""
        import asyncio

        import httpx

        if not repository or "/" not in repository:
            return False, "CI repository must be an owner/name identifier"
        if timeout_seconds <= 0 or poll_seconds <= 0:
            return False, "CI timeout and polling interval must be positive"

        deadline = asyncio.get_running_loop().time() + timeout_seconds
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        max_pages = 10
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    check_runs: list[dict] = []
                    expected_total: int | None = None
                    seen_ids: set[int] = set()
                    stable_snapshot = True

                    for page in range(1, max_pages + 1):
                        response = await client.get(
                            f"https://api.github.com/repos/{repository}/commits/{commit_sha}/check-runs",
                            headers=headers,
                            params={"per_page": 100, "page": page},
                        )
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, dict):
                            return False, "GitHub returned an invalid check-run response"
                        total = payload.get("total_count")
                        page_runs = payload.get("check_runs")
                        if type(total) is not int or total < 0 or not isinstance(page_runs, list):
                            return False, "GitHub returned incomplete check-run metadata"
                        if expected_total is None:
                            expected_total = total
                            if expected_total > max_pages * 100:
                                return False, "CI evidence exceeds the bounded pagination limit"
                        elif total != expected_total:
                            stable_snapshot = False
                            break

                        for run in page_runs:
                            if not isinstance(run, dict) or type(run.get("id")) is not int:
                                return False, "GitHub returned a malformed check-run record"
                            run_id = run["id"]
                            if run_id in seen_ids:
                                stable_snapshot = False
                                break
                            seen_ids.add(run_id)
                            check_runs.append(run)
                        if not stable_snapshot:
                            break
                        if len(check_runs) >= expected_total:
                            break
                        if not page_runs:
                            stable_snapshot = False
                            break

                    if not stable_snapshot:
                        await asyncio.sleep(poll_seconds)
                        continue
                    if expected_total is None or len(check_runs) != expected_total:
                        return False, "GitHub check-run pagination was incomplete; refusing to certify CI"

                    state, reason = self._evaluate_check_runs(check_runs, commit_sha)
                    if state == "success":
                        return True, reason
                    if state == "failure":
                        return False, reason
                except Exception as exc:
                    return False, f"CI status could not be verified: {type(exc).__name__}"

                await asyncio.sleep(poll_seconds)
        return False, "Required GitHub Actions checks did not finish before the CI deadline"

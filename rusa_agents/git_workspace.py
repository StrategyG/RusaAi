"""Feature-branch worktree lifecycle and GitHub publication."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .contracts import CodingTask


BRANCH_PATTERN = re.compile(r"^(feature|fix|chore|docs)/[A-Za-z0-9._/-]{1,180}$")
BLOCKED_PATH_PARTS = {".git", ".env", ".secrets", "secrets", "credentials", ".ssh", ".aws"}
BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


class GitWorkspaceManager:
    """Creates one Git worktree per task from a pre-fetched local bare mirror."""

    def __init__(self, mirror_root: str, workspace_root: str):
        self.mirror_root = Path(mirror_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.getenv("PATH", "/usr/bin:/bin"),
            "HOME": tempfile.gettempdir(),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)

    def create(self, task: CodingTask, tenant_id: str) -> Path:
        owner, repo = task.repository.full_name.split("/", 1)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
            raise ValueError("Invalid repository identity")
        branch = task.repository.work_branch
        if not BRANCH_PATTERN.fullmatch(branch) or ".." in branch or "//" in branch:
            raise ValueError("Work branch must be a safe feature/fix/chore/docs branch")
        if task.repository.base_branch in {"main", "master"} and branch in {"main", "master"}:
            raise ValueError("Agent may not work directly on a protected branch")

        mirror = (self.mirror_root / owner / f"{repo}.git").resolve()
        if self.mirror_root not in mirror.parents or not mirror.is_dir():
            raise FileNotFoundError(f"Pre-fetched bare mirror not found for {task.repository.full_name}")
        bare = self._run(["git", "--git-dir", str(mirror), "rev-parse", "--is-bare-repository"])
        if bare.returncode or bare.stdout.strip() != "true":
            raise ValueError("Repository mirror must be a valid bare Git repository")

        tenant_key = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:20]
        workspace = (self.workspace_root / tenant_key / task.task_id).resolve()
        if self.workspace_root not in workspace.parents:
            raise ValueError("Workspace path escaped configured root")
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if workspace.exists():
            current = self._run(["git", "-C", str(workspace), "branch", "--show-current"])
            if current.returncode == 0 and current.stdout.strip() == branch:
                return workspace
            raise FileExistsError("Task workspace exists but is not on the expected branch")

        created = self._run([
            "git", "-c", "core.hooksPath=/dev/null",
            "--git-dir", str(mirror), "worktree", "add", "-b", branch,
            str(workspace), task.repository.base_branch,
        ])
        if created.returncode:
            raise RuntimeError(f"Could not create feature worktree: {created.stderr[-3000:]}")
        return workspace


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
        }
        if env:
            base_env.update(env)
        return subprocess.run(args, cwd=cwd, env=base_env, capture_output=True, text=True, timeout=180)

    @staticmethod
    def _safe_changed_paths(workspace: Path) -> list[str]:
        result = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            capture_output=True, timeout=30, check=False,
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
            if path.is_absolute() or ".." in path.parts or parts.intersection(BLOCKED_PATH_PARTS):
                continue
            if path.suffix.lower() in BLOCKED_SUFFIXES or path.name.lower() in {"id_rsa", "id_ed25519"}:
                continue
            if raw_path.startswith(".github/workflows/"):
                continue
            candidate = (workspace / path).resolve(strict=False)
            if candidate != workspace and workspace not in candidate.parents:
                continue
            if (workspace / path).is_symlink():
                continue
            paths.append(raw_path)
        return sorted(set(paths))

    def commit_and_push(self, workspace: str, repository: str, branch: str, message: str) -> str | None:
        root = Path(workspace).resolve(strict=True)
        if not BRANCH_PATTERN.fullmatch(branch) or branch in {"main", "master"}:
            raise ValueError("Publishing is restricted to task feature branches")
        current = self._run(["git", "branch", "--show-current"], str(root))
        if current.returncode or current.stdout.strip() != branch:
            raise RuntimeError("Workspace branch does not match the task branch")
        remote = self._run(["git", "remote", "get-url", "origin"], str(root))
        if remote.returncode or "github.com" not in remote.stdout or repository.lower() not in remote.stdout.lower():
            raise RuntimeError("Origin remote does not match the requested GitHub repository")
        if "@" in remote.stdout.split("://", 1)[-1].split("/", 1)[0]:
            raise RuntimeError("Credential-bearing remote URLs are prohibited")

        changed = self._safe_changed_paths(root)
        if changed:
            added = self._run(["git", "add", "--", *changed], str(root))
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

        askpass = Path(tempfile.mkstemp(prefix="rusa-askpass-")[1])
        askpass.write_text(
            "#!/bin/sh\ncase \"$1\" in *Username*) printf '%s\\n' 'x-access-token' ;; "
            "*Password*) printf '%s\\n' \"$RUSA_GITHUB_TOKEN\" ;; esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        try:
            pushed = self._run(
                ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
                str(root),
                {"GIT_ASKPASS": str(askpass), "GIT_ASKPASS_REQUIRE": "force", "RUSA_GITHUB_TOKEN": self.token},
            )
        finally:
            askpass.unlink(missing_ok=True)
        if pushed.returncode:
            raise RuntimeError(f"Git push failed: {pushed.stderr[-3000:]}")
        return sha

    async def create_or_get_draft_pr(self, repository: str, branch: str, base: str, title: str, body: str) -> str:
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

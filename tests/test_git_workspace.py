import asyncio
import subprocess

import httpx
import pytest

from rusa_agents.contracts import CodingTask, RepositoryTarget
from rusa_agents.git_workspace import GitHubPublisher, GitWorkspaceManager


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_github_origin_must_match_exact_repository():
    GitHubPublisher.validate_origin_url(
        "https://github.com/StrategyG/RusaAi.git",
        "StrategyG/RusaAi",
    )
    with pytest.raises(RuntimeError):
        GitHubPublisher.validate_origin_url(
            "https://github.com.attacker.invalid/StrategyG/RusaAi.git",
            "StrategyG/RusaAi",
        )
    with pytest.raises(RuntimeError):
        GitHubPublisher.validate_origin_url(
            "https://token@github.com/StrategyG/RusaAi.git",
            "StrategyG/RusaAi",
        )
    with pytest.raises(RuntimeError):
        GitHubPublisher.validate_origin_url(
            "https://github.com/Other/Repo.git",
            "StrategyG/RusaAi",
        )


def test_workspace_manager_creates_and_cleans_tenant_worktree(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--initial-branch=main", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.invalid", cwd=source)
    (source / "README.md").write_text("base")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "initial", cwd=source)

    mirror_root = tmp_path / "mirrors"
    mirror = mirror_root / "StrategyG" / "RusaAi.git"
    mirror.parent.mkdir(parents=True)
    git("clone", "--bare", str(source), str(mirror))

    workspace_root = tmp_path / "workspaces"
    manager = GitWorkspaceManager(str(mirror_root), str(workspace_root))
    monkeypatch.setattr(manager, "refresh", lambda repository, base_branch: None)
    task = CodingTask(
        objective="test worktree",
        repository=RepositoryTarget(
            "StrategyG/RusaAi", "main", "feature/aicode/test-task-1"
        ),
        acceptance_criteria=["worktree exists"],
    )

    workspace = manager.create(task, "tenant-a")
    assert workspace.is_dir()
    assert git("branch", "--show-current", cwd=workspace).stdout.strip() == task.repository.work_branch

    other_task = CodingTask(
        objective="test second tenant worktree",
        repository=RepositoryTarget(
            "StrategyG/RusaAi", "main", "feature/aicode/test-task-2"
        ),
        acceptance_criteria=["worktree is tenant-isolated"],
    )
    other_tenant = manager.create(other_task, "tenant-b")
    assert other_tenant != workspace
    assert other_tenant.is_dir()

    manager.cleanup(task, "tenant-a")
    assert not workspace.exists()
    branch_check = subprocess.run(
        [
            "git", "--git-dir", str(mirror), "show-ref", "--verify",
            "refs/heads/feature/aicode/test-task-1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert branch_check.returncode != 0


def test_workspace_manager_rejects_unsafe_branch_before_checkout(tmp_path):
    manager = GitWorkspaceManager(str(tmp_path / "mirrors"), str(tmp_path / "workspaces"))
    task = CodingTask(
        objective="unsafe branch",
        repository=RepositoryTarget("StrategyG/RusaAi", "main", "feature/../../main"),
        acceptance_criteria=["must be rejected"],
    )
    with pytest.raises(ValueError, match="safe feature"):
        manager.create(task, "tenant-a")



def test_workspace_manager_refuses_untrusted_mirror_origin(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--bare", str(source))
    mirror_root = tmp_path / "mirrors"
    mirror = mirror_root / "StrategyG" / "RusaAi.git"
    mirror.parent.mkdir(parents=True)
    git("clone", "--bare", str(source), str(mirror))
    manager = GitWorkspaceManager(str(mirror_root), str(tmp_path / "workspaces"))
    with pytest.raises(RuntimeError, match="credential-free HTTPS"):
        manager.refresh("StrategyG/RusaAi", "main")


def test_publisher_filters_deployment_configuration_paths(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git("init", cwd=repository)
    files = [
        "src/app.py",
        "Dockerfile.worker",
        "docker-compose.override.yml",
        "compose.yaml",
        "requirements-dev.txt",
        "K8S/deployment.yaml",
        "infra/prod.tf",
    ]
    for name in files:
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
    assert GitHubPublisher._safe_changed_paths(repository) == ["src/app.py"]



def test_workspaces_are_isolated_by_lease_token(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--initial-branch=main", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.invalid", cwd=source)
    (source / "README.md").write_text("base")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "initial", cwd=source)

    mirror_root = tmp_path / "mirrors"
    mirror = mirror_root / "StrategyG" / "RusaAi.git"
    mirror.parent.mkdir(parents=True)
    git("clone", "--bare", str(source), str(mirror))

    manager = GitWorkspaceManager(str(mirror_root), str(tmp_path / "workspaces"))
    monkeypatch.setattr(manager, "refresh", lambda repository, base_branch: None)

    first = CodingTask(
        objective="first lease attempt",
        repository=RepositoryTarget(
            "StrategyG/RusaAi", "main", "feature/aicode/lease-first"
        ),
        acceptance_criteria=["isolated workspace"],
        task_id="same-task",
        metadata={"lease_token": "lease-one"},
    )
    second = CodingTask(
        objective="second lease attempt",
        repository=RepositoryTarget(
            "StrategyG/RusaAi", "main", "feature/aicode/lease-second"
        ),
        acceptance_criteria=["isolated workspace"],
        task_id="same-task",
        metadata={"lease_token": "lease-two"},
    )

    first_workspace = manager.create(first, "tenant-a")
    second_workspace = manager.create(second, "tenant-a")
    assert first_workspace != second_workspace
    assert first_workspace.is_dir() and second_workspace.is_dir()

    manager.cleanup(first, "tenant-a")
    manager.cleanup(second, "tenant-a")


def _github_check_run(
    name,
    *,
    suite_id,
    run_id,
    status="completed",
    conclusion="success",
    head_sha="a" * 40,
    app_id=15368,
    app_slug="github-actions",
):
    return {
        "id": run_id,
        "name": name,
        "head_sha": head_sha,
        "status": status,
        "conclusion": conclusion,
        "check_suite": {"id": suite_id},
        "app": {"id": app_id, "slug": app_slug},
    }


def test_ci_evidence_requires_all_jobs_from_latest_trusted_suite():
    sha = "a" * 40
    runs = [
        _github_check_run("Test, lint, and security", suite_id=100, run_id=1001, head_sha=sha),
        _github_check_run("Build, test, and scan production images", suite_id=100, run_id=1002, head_sha=sha),
        _github_check_run("Test, lint, and security", suite_id=101, run_id=1011, head_sha=sha),
        _github_check_run(
            "Build, test, and scan production images",
            suite_id=101,
            run_id=1012,
            status="in_progress",
            conclusion=None,
            head_sha=sha,
        ),
    ]

    state, reason = GitHubPublisher._evaluate_check_runs(runs, sha)

    assert state == "pending"
    assert "still running" in reason


def test_ci_evidence_does_not_fall_back_to_an_older_successful_suite():
    sha = "b" * 40
    runs = [
        _github_check_run("Test, lint, and security", suite_id=200, run_id=2001, head_sha=sha),
        _github_check_run("Build, test, and scan production images", suite_id=200, run_id=2002, head_sha=sha),
        _github_check_run(
            "Test, lint, and security",
            suite_id=201,
            run_id=2011,
            conclusion="failure",
            head_sha=sha,
        ),
        _github_check_run("Build, test, and scan production images", suite_id=201, run_id=2012, head_sha=sha),
    ]

    state, reason = GitHubPublisher._evaluate_check_runs(runs, sha)

    assert state == "failure"
    assert "Test, lint, and security" in reason


def test_ci_evidence_requires_exact_sha_and_official_github_actions_app():
    sha = "c" * 40
    runs = [
        _github_check_run(
            "Test, lint, and security",
            suite_id=300,
            run_id=3001,
            head_sha="d" * 40,
        ),
        _github_check_run(
            "Build, test, and scan production images",
            suite_id=300,
            run_id=3002,
            head_sha=sha,
            app_id=42,
            app_slug="untrusted-check-producer",
        ),
    ]

    state, reason = GitHubPublisher._evaluate_check_runs(runs, sha)

    assert state == "pending"
    assert "No trusted GitHub Actions checks" in reason


def test_ci_evidence_rejects_skipped_required_jobs():
    sha = "e" * 40
    runs = [
        _github_check_run("Test, lint, and security", suite_id=400, run_id=4001, head_sha=sha),
        _github_check_run(
            "Build, test, and scan production images",
            suite_id=400,
            run_id=4002,
            conclusion="skipped",
            head_sha=sha,
        ),
    ]

    state, reason = GitHubPublisher._evaluate_check_runs(runs, sha)

    assert state == "failure"
    assert "skipped" in reason


def test_ci_evidence_does_not_mix_required_jobs_from_different_suites():
    sha = "f" * 40
    runs = [
        _github_check_run("Test, lint, and security", suite_id=500, run_id=5001, head_sha=sha),
        _github_check_run("Build, test, and scan production images", suite_id=499, run_id=4992, head_sha=sha),
    ]

    state, reason = GitHubPublisher._evaluate_check_runs(runs, sha)

    assert state == "pending"
    assert "incomplete" in reason



def test_draft_pr_recovery_finds_task_pr_from_previous_lease(monkeypatch):
    task_id = "12345678-1234-4234-8234-123456789abc"
    existing_pr = {
        "state": "open",
        "html_url": "https://github.com/StrategyG/RusaAi/pull/123",
        "body": f"Task ID: {task_id}",
        "head": {"ref": "feature/aicode/old-lease"},
        "base": {"ref": "main"},
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [existing_pr]

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            self.posts = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, *, headers, params):
            assert params["state"] == "all"
            assert params["base"] == "main"
            return FakeResponse()

        async def post(self, *_args, **_kwargs):
            self.posts += 1
            raise AssertionError("recovery must not create a duplicate PR")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    publisher = GitHubPublisher("test-token")
    url = asyncio.run(
        publisher.create_or_get_draft_pr(
            "StrategyG/RusaAi",
            "feature/aicode/new-lease",
            "main",
            "Recovered task",
            f"Task ID: {task_id}\nRUSA-AI-Task-ID: {task_id}",
        )
    )

    assert url == existing_pr["html_url"]


def test_draft_pr_recovery_refuses_to_duplicate_closed_task_pr(monkeypatch):
    task_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
    existing_pr = {
        "state": "closed",
        "html_url": "https://github.com/StrategyG/RusaAi/pull/124",
        "body": f"RUSA-AI-Task-ID: {task_id}",
        "head": {"ref": "feature/aicode/old-lease"},
        "base": {"ref": "main"},
    }

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [existing_pr]

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, **_kwargs):
            return FakeResponse()

        async def post(self, *_args, **_kwargs):
            raise AssertionError("closed PR must not be duplicated")

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    publisher = GitHubPublisher("test-token")
    with pytest.raises(ValueError, match="manual reconciliation"):
        asyncio.run(
            publisher.create_or_get_draft_pr(
                "StrategyG/RusaAi",
                "feature/aicode/new-lease",
                "main",
                "Recovered task",
                f"RUSA-AI-Task-ID: {task_id}",
            )
        )



def test_new_lease_worktree_starts_from_existing_stable_publish_branch(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--initial-branch=main", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.invalid", cwd=source)
    (source / "README.md").write_text("base")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "initial", cwd=source)

    mirror_root = tmp_path / "mirrors"
    mirror = mirror_root / "StrategyG" / "RusaAi.git"
    mirror.parent.mkdir(parents=True)
    git("clone", "--bare", str(source), str(mirror))

    stable_branch = "feature/aicode/tenant-hash-task-id"
    (source / "recovered-change.txt").write_text("already published")
    git("add", "recovered-change.txt", cwd=source)
    git("commit", "-m", "published before worker crash", cwd=source)
    git(
        "--git-dir", str(mirror), "fetch", str(source),
        f"+refs/heads/main:refs/heads/{stable_branch}",
    )
    stable_sha = git(
        "--git-dir", str(mirror), "rev-parse", stable_branch
    ).stdout.strip()

    manager = GitWorkspaceManager(str(mirror_root), str(tmp_path / "workspaces"))
    monkeypatch.setattr(
        manager,
        "refresh",
        lambda repository, base_branch, publish_branch=None: None,
    )
    task = CodingTask(
        objective="recover previous publication",
        repository=RepositoryTarget(
            "StrategyG/RusaAi",
            "main",
            "feature/aicode/tenant-hash-task-id-new-lease",
        ),
        acceptance_criteria=["reuse stable publication head"],
        task_id="same-task",
        metadata={
            "lease_token": "new-lease",
            "publish_branch": stable_branch,
        },
    )

    workspace = manager.create(task, "tenant-a")
    assert git("rev-parse", "HEAD", cwd=workspace).stdout.strip() == stable_sha
    assert (workspace / "recovered-change.txt").read_text() == "already published"

    manager.cleanup(task, "tenant-a")



def test_publisher_pushes_lease_worktree_to_stable_task_branch(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--initial-branch=main", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.invalid", cwd=source)
    (source / "README.md").write_text("base")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "initial", cwd=source)

    remote = tmp_path / "remote.git"
    git("clone", "--bare", str(source), str(remote))
    workspace = tmp_path / "workspace"
    git("clone", str(remote), str(workspace))
    git("checkout", "-b", "feature/aicode/attempt-branch", cwd=workspace)
    git("config", "user.name", "Test", cwd=workspace)
    git("config", "user.email", "test@example.invalid", cwd=workspace)
    (workspace / "change.py").write_text("print('recovered safely')\n")

    monkeypatch.setattr(
        GitHubPublisher,
        "validate_origin_url",
        staticmethod(lambda _remote, _repository: None),
    )
    publisher = GitHubPublisher("test-token")
    sha = publisher.commit_and_push(
        str(workspace),
        "StrategyG/RusaAi",
        "feature/aicode/attempt-branch",
        "test: publish stable task head",
        publish_branch="feature/aicode/stable-task-branch",
    )

    stable = git(
        "--git-dir", str(remote), "rev-parse",
        "refs/heads/feature/aicode/stable-task-branch",
    ).stdout.strip()
    attempt_ref = subprocess.run(
        [
            "git", "--git-dir", str(remote), "show-ref", "--verify",
            "refs/heads/feature/aicode/attempt-branch",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert sha == stable
    assert attempt_ref.returncode != 0



def test_workspace_refresh_fetches_existing_stable_publish_branch(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git("init", "--initial-branch=main", cwd=source)
    git("config", "user.name", "Test", cwd=source)
    git("config", "user.email", "test@example.invalid", cwd=source)
    (source / "README.md").write_text("base")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "initial", cwd=source)

    mirror_root = tmp_path / "mirrors"
    mirror = mirror_root / "StrategyG" / "RusaAi.git"
    mirror.parent.mkdir(parents=True)
    git("clone", "--bare", str(source), str(mirror))

    stable_branch = "feature/aicode/tenant-hash-task-id"
    (source / "stable.txt").write_text("published")
    git("add", "stable.txt", cwd=source)
    git("commit", "-m", "published stable branch", cwd=source)
    git("branch", stable_branch, cwd=source)
    expected_sha = git("rev-parse", stable_branch, cwd=source).stdout.strip()

    monkeypatch.setattr(
        GitHubPublisher,
        "validate_origin_url",
        staticmethod(lambda _remote, _repository: None),
    )
    manager = GitWorkspaceManager(str(mirror_root), str(tmp_path / "workspaces"))
    manager.refresh("StrategyG/RusaAi", "main", stable_branch)

    actual_sha = git(
        "--git-dir", str(mirror), "rev-parse", f"refs/heads/{stable_branch}"
    ).stdout.strip()
    assert actual_sha == expected_sha

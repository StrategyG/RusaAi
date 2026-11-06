import subprocess

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

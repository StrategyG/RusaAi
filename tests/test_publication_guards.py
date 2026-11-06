import subprocess

import pytest

from rusa_agents.git_workspace import GitHubPublisher


def _completed(args, stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout, "")


def _stub_git(monkeypatch, push_result=None):
    calls = []

    def fake_run(args, cwd, env=None):
        calls.append(args)
        if args[1:3] == ["branch", "--show-current"]:
            return _completed(args, "feature/aicode/lease-test\n")
        if args[1:4] == ["remote", "get-url", "origin"]:
            return _completed(args, "https://github.com/StrategyG/RusaAi.git\n")
        if args[1:3] == ["rev-parse", "HEAD"]:
            return _completed(args, "a" * 40 + "\n")
        if "push" in args:
            return push_result or _completed(args)
        raise AssertionError(f"Unexpected git command: {args}")

    monkeypatch.setattr(
        GitHubPublisher, "_safe_changed_paths", staticmethod(lambda workspace: [])
    )
    monkeypatch.setattr(GitHubPublisher, "_run", staticmethod(fake_run))
    return calls


def test_stale_lease_blocks_git_push(tmp_path, monkeypatch):
    calls = _stub_git(monkeypatch)
    publisher = GitHubPublisher("test-token")

    with pytest.raises(RuntimeError, match="lease was lost before GitHub push"):
        publisher.commit_and_push(
            str(tmp_path),
            "StrategyG/RusaAi",
            "feature/aicode/lease-test",
            "test publication",
            lease_guard=lambda: False,
        )

    assert not any("push" in args for args in calls)


def test_lease_loss_during_git_push_blocks_pull_request_followup(tmp_path, monkeypatch):
    calls = _stub_git(monkeypatch)
    publisher = GitHubPublisher("test-token")
    states = iter((True, False))

    with pytest.raises(RuntimeError, match="lease was lost during GitHub push"):
        publisher.commit_and_push(
            str(tmp_path),
            "StrategyG/RusaAi",
            "feature/aicode/lease-test",
            "test publication",
            lease_guard=lambda: next(states),
        )

    assert any("push" in args for args in calls)

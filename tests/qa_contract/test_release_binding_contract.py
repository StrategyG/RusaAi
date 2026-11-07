"""Immutable QA contract tests for candidate Git tree publication binding."""
import subprocess
from pathlib import Path

import pytest

from rusa_agents.git_workspace import GitHubPublisher
from rusa_agents.qa_attestation import digest_snapshot_archive


def git(*args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def repository(path: Path, branch: str):
    path.mkdir()
    git("init", "--initial-branch=main", cwd=path)
    git("config", "user.name", "QA Contract", cwd=path)
    git("config", "user.email", "qa-contract@example.invalid", cwd=path)
    (path / "README.md").write_text("baseline")
    git("add", "README.md", cwd=path)
    git("commit", "-m", "baseline", cwd=path)
    git("checkout", "-b", branch, cwd=path)
    git("remote", "add", "origin", "https://github.com/StrategyG/RusaAi.git", cwd=path)


def test_candidate_snapshot_digest_matches_git_index(tmp_path):
    root = tmp_path / "repo"
    repository(root, "feature/qa-contract")
    (root / "README.md").write_text("candidate")
    publisher = GitHubPublisher(github_token="test-token")

    archive, tree_sha = publisher.prepare_candidate_snapshot(root)
    assert tree_sha == git("write-tree", cwd=root).stdout.strip()
    assert len(digest_snapshot_archive(archive)) == 64


def test_publication_blocks_changed_tree_after_qa(tmp_path):
    root = tmp_path / "repo"
    repository(root, "feature/qa-contract")
    (root / "README.md").write_text("tested")
    publisher = GitHubPublisher(github_token="test-token")
    archive, tree_sha = publisher.prepare_candidate_snapshot(root)
    digest = digest_snapshot_archive(archive)
    (root / "README.md").write_text("changed after QA")

    with pytest.raises(RuntimeError, match="changed after independent QA"):
        publisher.commit_and_push(
            str(root),
            "StrategyG/RusaAi",
            "feature/qa-contract",
            "blocked release",
            expected_tree_sha=tree_sha,
            expected_artifact_digest=digest,
        )


def test_publication_blocks_digest_mismatch_even_for_same_tree(tmp_path):
    root = tmp_path / "repo"
    repository(root, "feature/qa-contract")
    (root / "README.md").write_text("tested")
    publisher = GitHubPublisher(github_token="test-token")
    _, tree_sha = publisher.prepare_candidate_snapshot(root)

    with pytest.raises(RuntimeError, match="artifact digest changed"):
        publisher.commit_and_push(
            str(root),
            "StrategyG/RusaAi",
            "feature/qa-contract",
            "blocked release",
            expected_tree_sha=tree_sha,
            expected_artifact_digest="f" * 64,
        )

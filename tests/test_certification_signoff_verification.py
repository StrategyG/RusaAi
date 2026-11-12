from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rusa_agents.certification import signoff_verification as verifier


REPOSITORY = "StrategyG/RusaAi"
CANDIDATE_SHA = "a" * 40
PR_HEAD_SHA = "b" * 40
ROLE_NAMES = (
    "platform",
    "security",
    "sre",
    "qa",
    "data_governance",
    "finops",
    "process_owner",
)


def _record():
    signoffs = {}
    for index, role in enumerate(ROLE_NAMES, start=1):
        signoffs[role] = {
            "reviewer": f"reviewer-{role}",
            "approved": True,
            "approved_at": "2026-11-12T12:00:00Z",
            "evidence_ref": (
                f"https://github.com/{REPOSITORY}/pull/42#pullrequestreview-{100 + index}"
            ),
        }
    return {"signoffs": signoffs}


def _teams():
    return {role: f"rusa-{role.replace('_', '-')}" for role in ROLE_NAMES}


def test_release_asset_reference_is_bound_to_exact_repository_and_tag():
    asset = "https://github.com/StrategyG/RusaAi/releases/download/v1.2.3/tenant-isolation.json"
    assert verifier._parse_release_asset_ref(asset, REPOSITORY, "v1.2.3") == "tenant-isolation.json"

    with pytest.raises(verifier.EvidenceVerificationError):
        verifier._parse_release_asset_ref(asset, REPOSITORY, "v1.2.4")


def test_signoff_reference_extracts_review_id_from_same_repository():
    reference = f"https://github.com/{REPOSITORY}/pull/42#pullrequestreview-123"
    assert verifier._parse_review_ref(reference, REPOSITORY) == (42, 123)

    with pytest.raises(verifier.EvidenceVerificationError):
        verifier._parse_review_ref(
            "https://github.com/attacker/Other/pull/42#pullrequestreview-123",
            REPOSITORY,
        )


def test_verify_signoffs_requires_merged_certification_pr_and_active_role_membership(monkeypatch):
    record = _record()
    calls = []

    def fake_api(args, *, gh_binary, timeout_seconds=30):
        calls.append(args)
        endpoint = args[0]
        if endpoint == f"repos/{REPOSITORY}/pulls/42":
            return {
                "base": {"ref": "main"},
                "state": "closed",
                "merged_at": "2026-11-12T12:30:00Z",
                "merge_commit_sha": "c" * 40,
                "head": {"sha": PR_HEAD_SHA},
            }
        if endpoint == f"repos/{REPOSITORY}/compare/{CANDIDATE_SHA}...{'c' * 40}":
            return {"status": "ahead"}
        if endpoint == f"repos/{REPOSITORY}/pulls/42/files?per_page=100":
            return [{"filename": "certification/records/v1.2.3.json"}]
        if "/reviews/" in endpoint:
            review_id = int(endpoint.rsplit("/", 1)[1])
            index = review_id - 100
            role = ROLE_NAMES[index - 1]
            return {
                "state": "APPROVED",
                "user": {"login": f"reviewer-{role}"},
                "commit_id": PR_HEAD_SHA,
                "submitted_at": "2026-11-12T12:00:00Z",
            }
        if "/memberships/" in endpoint:
            return {"state": "active"}
        raise AssertionError(f"unexpected GitHub API endpoint: {endpoint}")

    monkeypatch.setattr(verifier, "_run_gh_json", fake_api)
    verifier._verify_signoffs(
        record,
        repository=REPOSITORY,
        candidate_sha=CANDIDATE_SHA,
        record_path="certification/records/v1.2.3.json",
        teams=_teams(),
        gh_binary="gh",
    )
    assert len(calls) == 3 + len(ROLE_NAMES) * 2


def test_verify_signoffs_rejects_duplicate_reviewers(monkeypatch):
    record = _record()
    record["signoffs"]["security"]["reviewer"] = record["signoffs"]["platform"]["reviewer"]
    record["signoffs"]["security"]["evidence_ref"] = (
        f"https://github.com/{REPOSITORY}/pull/42#pullrequestreview-101"
    )

    def fake_api(args, *, gh_binary, timeout_seconds=30):
        endpoint = args[0]
        if endpoint == f"repos/{REPOSITORY}/pulls/42":
            return {
                "base": {"ref": "main"},
                "state": "closed",
                "merged_at": "2026-11-12T12:30:00Z",
                "merge_commit_sha": "c" * 40,
                "head": {"sha": PR_HEAD_SHA},
            }
        if endpoint == f"repos/{REPOSITORY}/compare/{CANDIDATE_SHA}...{'c' * 40}":
            return {"status": "ahead"}
        if endpoint == f"repos/{REPOSITORY}/pulls/42/files?per_page=100":
            return [{"filename": "certification/records/v1.2.3.json"}]
        raise AssertionError(f"unexpected GitHub API endpoint: {endpoint}")

    monkeypatch.setattr(verifier, "_run_gh_json", fake_api)
    with pytest.raises(verifier.EvidenceVerificationError, match="distinct reviewer"):
        verifier._verify_signoffs(
            record,
            repository=REPOSITORY,
            candidate_sha=CANDIDATE_SHA,
            record_path="certification/records/v1.2.3.json",
            teams=_teams(),
            gh_binary="gh",
        )


def test_verify_signoffs_rejects_review_on_stale_pull_request_head(monkeypatch):
    record = _record()

    def fake_api(args, *, gh_binary, timeout_seconds=30):
        endpoint = args[0]
        if endpoint == f"repos/{REPOSITORY}/pulls/42":
            return {
                "base": {"ref": "main"},
                "state": "closed",
                "merged_at": "2026-11-12T12:30:00Z",
                "merge_commit_sha": "c" * 40,
                "head": {"sha": PR_HEAD_SHA},
            }
        if endpoint == f"repos/{REPOSITORY}/compare/{CANDIDATE_SHA}...{'c' * 40}":
            return {"status": "ahead"}
        if endpoint == f"repos/{REPOSITORY}/pulls/42/files?per_page=100":
            return [{"filename": "certification/records/v1.2.3.json"}]
        if "/reviews/" in endpoint:
            review_id = int(endpoint.rsplit("/", 1)[1])
            role = ROLE_NAMES[review_id - 101]
            return {
                "state": "APPROVED",
                "user": {"login": f"reviewer-{role}"},
                "commit_id": "d" * 40,
                "submitted_at": "2026-11-12T12:00:00Z",
            }
        raise AssertionError(f"unexpected GitHub API endpoint: {endpoint}")

    monkeypatch.setattr(verifier, "_run_gh_json", fake_api)
    with pytest.raises(verifier.EvidenceVerificationError, match="approval of the merged"):
        verifier._verify_signoffs(
            record,
            repository=REPOSITORY,
            candidate_sha=CANDIDATE_SHA,
            record_path="certification/records/v1.2.3.json",
            teams=_teams(),
            gh_binary="gh",
        )

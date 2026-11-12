"""Verify production certification evidence against GitHub and prepare a signed record.

The verifier binds each check to an immutable asset in the exact release and
each human sign-off to an approved review on a merged certification-only PR.
It also verifies that each reviewer belongs to the configured role team.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .contracts import validate_production_certification, validate_release_manifest

_ROLE_NAMES = (
    "platform",
    "security",
    "sre",
    "qa",
    "data_governance",
    "finops",
    "process_owner",
)
_ASSET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_REVIEW_FRAGMENT_RE = re.compile(r"^pullrequestreview-([1-9][0-9]*)$")
_TEAM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceVerificationError(ValueError):
    """Raised when a certification evidence reference cannot be trusted."""


def _run_gh_json(args: list[str], *, gh_binary: str, timeout_seconds: int = 30) -> Any:
    try:
        completed = subprocess.run(
            [gh_binary, "api", *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceVerificationError("GitHub CLI request failed or timed out") from exc
    if completed.returncode != 0:
        raise EvidenceVerificationError(
            "GitHub API verification failed: " + (completed.stderr.strip()[:300] or "no details")
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceVerificationError("GitHub API returned invalid JSON") from exc


def _parse_release_asset_ref(value: str, repository: str, release_tag: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query:
        raise EvidenceVerificationError("check evidence_ref must be an HTTPS GitHub release asset URL")
    parts = parsed.path.strip("/").split("/")
    if (
        len(parts) != 6
        or "/".join(parts[:2]) != repository
        or parts[2:5] != ["releases", "download", release_tag]
        or not _ASSET_RE.fullmatch(parts[5])
    ):
        raise EvidenceVerificationError(
            "check evidence_ref must point to an asset in the exact candidate release"
        )
    return parts[5]


def _parse_review_ref(value: str, repository: str) -> tuple[int, int]:
    parsed = urlparse(value)
    parts = parsed.path.strip("/").split("/")
    match = _REVIEW_FRAGMENT_RE.fullmatch(parsed.fragment)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.query
        or len(parts) != 4
        or "/".join(parts[:2]) != repository
        or parts[2] != "pull"
        or not parts[3].isdigit()
        or int(parts[3]) < 1
        or match is None
    ):
        raise EvidenceVerificationError(
            "sign-off evidence_ref must point to a GitHub pull-request review in this repository"
        )
    return int(parts[3]), int(match.group(1))


def _download_release_asset(
    asset: str, *, repository: str, release_tag: str, gh_binary: str, directory: Path
) -> Path:
    try:
        completed = subprocess.run(
            [
                gh_binary,
                "release",
                "download",
                release_tag,
                "--repo",
                repository,
                "--pattern",
                asset,
                "--dir",
                str(directory),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EvidenceVerificationError("release evidence download failed or timed out") from exc
    if completed.returncode != 0:
        raise EvidenceVerificationError(
            f"cannot download required release evidence asset {asset}: "
            + (completed.stderr.strip()[:300] or "no details")
        )
    path = directory / asset
    if not path.is_file() or path.is_symlink():
        raise EvidenceVerificationError(f"release evidence asset {asset} is missing or unsafe")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _verify_signoffs(
    record: dict[str, Any],
    *,
    repository: str,
    candidate_sha: str,
    record_path: str,
    teams: dict[str, str],
    gh_binary: str,
) -> None:
    if set(teams) != set(_ROLE_NAMES):
        raise EvidenceVerificationError("all seven certification role teams must be configured")
    if any(not _TEAM_RE.fullmatch(slug) for slug in teams.values()):
        raise EvidenceVerificationError("certification team slugs contain invalid characters")

    signoffs = record["signoffs"]
    reviewers: set[str] = set()
    review_refs = [_parse_review_ref(signoffs[role]["evidence_ref"], repository) for role in _ROLE_NAMES]
    pull_numbers = {item[0] for item in review_refs}
    if len(pull_numbers) != 1:
        raise EvidenceVerificationError("all seven sign-offs must reference the same certification PR")
    pull_number = next(iter(pull_numbers))

    pull = _run_gh_json(
        [f"repos/{repository}/pulls/{pull_number}"], gh_binary=gh_binary
    )
    if (
        pull.get("base", {}).get("ref") != "main"
        or pull.get("state") != "closed"
        or pull.get("merged_at") is None
        or not pull.get("merge_commit_sha")
        or not pull.get("head", {}).get("sha")
    ):
        raise EvidenceVerificationError(
            "sign-off PR must be merged into main before certification can be signed"
        )
    head_sha = str(pull["head"]["sha"]).lower()

    comparison = _run_gh_json(
        [
            f"repos/{repository}/compare/{candidate_sha}...{pull['merge_commit_sha']}"
        ],
        gh_binary=gh_binary,
    )
    if comparison.get("status") not in {"ahead", "identical"}:
        raise EvidenceVerificationError(
            "certification PR merge does not descend from the exact release candidate commit"
        )

    files = _run_gh_json(
        [f"repos/{repository}/pulls/{pull_number}/files?per_page=100"],
        gh_binary=gh_binary,
    )
    if (
        not isinstance(files, list)
        or len(files) != 1
        or files[0].get("filename") != record_path
    ):
        raise EvidenceVerificationError(
            "certification PR must change only the submitted certification record"
        )

    for role, (pr_number, review_id) in zip(_ROLE_NAMES, review_refs, strict=True):
        signoff = signoffs[role]
        reviewer = signoff["reviewer"]
        if reviewer in reviewers:
            raise EvidenceVerificationError("each certification role requires a distinct reviewer")
        reviewers.add(reviewer)

        review = _run_gh_json(
            [
                f"repos/{repository}/pulls/{pr_number}/reviews/{review_id}"
            ],
            gh_binary=gh_binary,
        )
        if (
            review.get("state") != "APPROVED"
            or review.get("user", {}).get("login") != reviewer
            or str(review.get("commit_id", "")).lower() != head_sha
            or not review.get("submitted_at")
            or _parse_time(review["submitted_at"]) != _parse_time(signoff["approved_at"])
            or not signoff.get("approved")
        ):
            raise EvidenceVerificationError(
                f"{role}: sign-off does not match an approval of the merged certification PR head"
            )

        membership = _run_gh_json(
            [
                f"orgs/{repository.split('/', 1)[0]}/teams/{teams[role]}/memberships/{reviewer}"
            ],
            gh_binary=gh_binary,
        )
        if membership.get("state") != "active":
            raise EvidenceVerificationError(
                f"{role}: reviewer is not an active member of the configured certification team"
            )


def prepare_certification_record(
    *,
    record_path: str,
    manifest_path: str,
    output_path: str,
    repository: str,
    release_tag: str,
    expected_commit_sha: str,
    workflow_run_id: int,
    workflow_run_url: str,
    workflow_path: str,
    release_signer_identity: str,
    oidc_issuer: str,
    teams: dict[str, str],
    gh_binary: str = "gh",
) -> dict[str, Any]:
    record_file = Path(record_path)
    manifest_file = Path(manifest_path)
    try:
        record = json.loads(record_file.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceVerificationError("certification record or release manifest cannot be read") from exc
    validate_production_certification(record)
    validate_release_manifest(
        manifest,
        expected_repository=repository,
        expected_commit_sha=expected_commit_sha,
    )

    candidate = record["candidate"]
    if candidate["repository"] != repository:
        raise EvidenceVerificationError("certification record repository does not match")
    if candidate["commit_sha"].lower() != expected_commit_sha.lower():
        raise EvidenceVerificationError("certification record commit does not match the release")
    if candidate["release_id"] != release_tag:
        raise EvidenceVerificationError("certification record release tag does not match")
    if manifest["source"]["commit_sha"].lower() != expected_commit_sha.lower():
        raise EvidenceVerificationError("release manifest is not bound to the candidate commit")

    manifest_digest = _sha256(manifest_file)
    manifest_uri = f"https://github.com/{repository}/releases/download/{release_tag}/release-manifest.json"
    signature_uri = (
        f"https://github.com/{repository}/releases/download/{release_tag}/release-manifest.sigstore.json"
    )
    with tempfile.TemporaryDirectory(prefix="rusa-cert-evidence-") as temp:
        evidence_dir = Path(temp)
        for check_name, check in record["checks"].items():
            if not _SHA256_RE.fullmatch(check["evidence_digest"]):
                raise EvidenceVerificationError(f"{check_name}: evidence digest is invalid")
            asset = _parse_release_asset_ref(check["evidence_ref"], repository, release_tag)
            evidence_file = _download_release_asset(
                asset,
                repository=repository,
                release_tag=release_tag,
                gh_binary=gh_binary,
                directory=evidence_dir,
            )
            if _sha256(evidence_file) != check["evidence_digest"]:
                raise EvidenceVerificationError(f"{check_name}: evidence bytes do not match the declared digest")

    _verify_signoffs(
        record,
        repository=repository,
        candidate_sha=expected_commit_sha,
        record_path=record_file.as_posix(),
        teams=teams,
        gh_binary=gh_binary,
    )

    candidate["release_manifest_ref"]["uri"] = manifest_uri
    candidate["release_manifest_ref"]["digest"] = manifest_digest
    candidate["release_manifest_ref"]["signature"] = {
        "verified": True,
        "signer_identity": release_signer_identity,
        "oidc_issuer": oidc_issuer,
        "bundle_ref": signature_uri,
    }
    record["source"] = {
        "workflow": workflow_path,
        "run_id": workflow_run_id,
        "attestation_verified": True,
        "attestation_ref": workflow_run_url,
    }
    validate_production_certification(record)
    Path(output_path).write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "PREPARED",
        "repository": repository,
        "commit_sha": expected_commit_sha,
        "release_tag": release_tag,
        "manifest_digest": manifest_digest,
        "certification_record": output_path,
        "verified_signoffs": len(_ROLE_NAMES),
        "verified_checks": len(record["checks"]),
    }


def _parse_team(value: str) -> tuple[str, str]:
    role, separator, slug = value.partition("=")
    if not separator or role not in _ROLE_NAMES or not slug:
        raise argparse.ArgumentTypeError("team must use role=team-slug for a known certification role")
    return role, slug


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rusa_agents.certification.signoff_verification",
        description="Verify production certification evidence and prepare a signed record.",
    )
    parser.add_argument("--record", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--workflow-run-id", required=True, type=int)
    parser.add_argument("--workflow-run-url", required=True)
    parser.add_argument("--workflow-path", default=".github/workflows/production-certification.yml")
    parser.add_argument("--release-signer-identity", required=True)
    parser.add_argument("--oidc-issuer", default="https://token.actions.githubusercontent.com")
    parser.add_argument("--team", action="append", type=_parse_team, required=True)
    parser.add_argument("--gh-binary", default="gh")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        teams = dict(args.team)
        report = prepare_certification_record(
            record_path=args.record,
            manifest_path=args.manifest,
            output_path=args.output,
            repository=args.repository,
            release_tag=args.release_tag,
            expected_commit_sha=args.commit_sha,
            workflow_run_id=args.workflow_run_id,
            workflow_run_url=args.workflow_run_url,
            workflow_path=args.workflow_path,
            release_signer_identity=args.release_signer_identity,
            oidc_issuer=args.oidc_issuer,
            teams=teams,
            gh_binary=args.gh_binary,
        )
        print(json.dumps(report, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

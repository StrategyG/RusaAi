from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from rusa_agents.certification.artifact_verification import ArtifactVerificationError
from rusa_agents.certification.provenance_verification import verify_github_slsa_provenance

DIGEST = "sha256:" + "c" * 64
REPOSITORY = "StrategyG/RusaAi"
WORKFLOW = "StrategyG/RusaAi/.github/workflows/release.yml"
SOURCE_DIGEST = "a" * 40
IDENTITY = "https://github.com/StrategyG/RusaAi/.github/workflows/release.yml@refs/tags/v1.0.0"
ISSUER = "https://token.actions.githubusercontent.com"


def _output(digest: str = DIGEST.removeprefix("sha256:")) -> str:
    return json.dumps(
        [
            {
                "verificationResult": {
                    "statement": {
                        "predicateType": "https://slsa.dev/provenance/v1",
                        "subject": [{"name": "ghcr.io/strategyg/rusa-ai-api", "digest": {"sha256": digest}}],
                        "predicate": {},
                    }
                }
            }
        ]
    )


def _verify(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "image": "ghcr.io/strategyg/rusa-ai-api",
        "digest": DIGEST,
        "repository": REPOSITORY,
        "signer_workflow": WORKFLOW,
        "source_digest": SOURCE_DIGEST,
        "certificate_identity": IDENTITY,
        "oidc_issuer": ISSUER,
    }
    arguments.update(overrides)
    return verify_github_slsa_provenance(**arguments)


def test_slsa_verification_enforces_trusted_identity_and_exact_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: "/usr/bin/gh")
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=_output(), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = _verify()

    assert result["verified"] is True
    assert result["subject_digest"] == DIGEST
    for expected in (REPOSITORY, WORKFLOW, SOURCE_DIGEST, IDENTITY, ISSUER, "https://slsa.dev/provenance/v1"):
        assert expected in captured["command"]
    assert captured["kwargs"]["shell"] is False


def test_slsa_verification_rejects_wrong_subject_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: "/usr/bin/gh")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=_output("d" * 64), stderr=""
        ),
    )
    with pytest.raises(ArtifactVerificationError, match="does not contain"):
        _verify()


def test_slsa_verification_fails_closed_when_attestation_is_not_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: "/usr/bin/gh")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="no matching attestations"
        ),
    )
    with pytest.raises(ArtifactVerificationError, match="no matching attestations"):
        _verify()


def test_slsa_verification_fails_closed_when_gh_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: None)
    with pytest.raises(ArtifactVerificationError, match="not installed"):
        _verify()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("digest", "sha256:bad", "lowercase sha256"),
        ("image", "ghcr.io/example/image@sha256:" + "a" * 64, "without a digest"),
        ("repository", "StrategyG", "owner/name"),
        ("signer_workflow", " ", "must not be empty"),
        ("signer_workflow", ".github/workflows/release.yml", "owner/repository/.github/workflows"),
        ("source_digest", "bad", "40- or 64-character lowercase Git commit SHA"),
        ("certificate_identity", " ", "must not be empty"),
        ("oidc_issuer", "http://issuer.example", "HTTPS"),
        ("timeout_seconds", 0, "between 1 and 180"),
    ],
)
def test_slsa_verification_rejects_invalid_inputs(
    field: str, value: object, message: str
) -> None:
    arguments: dict[str, object] = {
        "image": "ghcr.io/strategyg/rusa-ai-api",
        "digest": DIGEST,
        "repository": REPOSITORY,
        "signer_workflow": WORKFLOW,
        "source_digest": SOURCE_DIGEST,
        "certificate_identity": IDENTITY,
        "oidc_issuer": ISSUER,
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=message):
        verify_github_slsa_provenance(**arguments)

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from rusa_agents.certification.artifact_verification import (
    ArtifactVerificationError,
    verify_cosign_image_signature,
)

DIGEST = "sha256:" + "a" * 64
IDENTITY = "https://github.com/StrategyG/RusaAi/.github/workflows/release.yml@refs/tags/v1.0.0"
ISSUER = "https://token.actions.githubusercontent.com"


def _record(digest: str = DIGEST) -> str:
    return json.dumps(
        [{"critical": {"image": {"docker-manifest-digest": digest}}}]
    )


def test_cosign_verification_binds_exact_digest_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: "/usr/bin/cosign")
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=_record(), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = verify_cosign_image_signature(
        image="ghcr.io/strategyg/rusa-ai-api",
        digest=DIGEST,
        certificate_identity=IDENTITY,
        oidc_issuer=ISSUER,
    )

    assert result["verified"] is True
    assert result["subject_digest"] == DIGEST
    assert "--certificate-identity" in captured["command"]
    assert IDENTITY in captured["command"]
    assert "--certificate-oidc-issuer" in captured["command"]
    assert ISSUER in captured["command"]
    assert captured["kwargs"]["shell"] is False


def test_cosign_verification_rejects_a_different_subject_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: "/usr/bin/cosign")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=_record("sha256:" + "b" * 64), stderr=""
        ),
    )
    with pytest.raises(ArtifactVerificationError, match="does not bind"):
        verify_cosign_image_signature(
            image="ghcr.io/strategyg/rusa-ai-api",
            digest=DIGEST,
            certificate_identity=IDENTITY,
            oidc_issuer=ISSUER,
        )


def test_cosign_verification_fails_closed_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: "/usr/bin/cosign")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="no matching signatures"
        ),
    )
    with pytest.raises(ArtifactVerificationError, match="no matching signatures"):
        verify_cosign_image_signature(
            image="ghcr.io/strategyg/rusa-ai-api",
            digest=DIGEST,
            certificate_identity=IDENTITY,
            oidc_issuer=ISSUER,
        )


def test_cosign_verification_fails_closed_when_tool_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _binary: None)
    with pytest.raises(ArtifactVerificationError, match="not installed"):
        verify_cosign_image_signature(
            image="ghcr.io/strategyg/rusa-ai-api",
            digest=DIGEST,
            certificate_identity=IDENTITY,
            oidc_issuer=ISSUER,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("digest", "sha256:bad", "lowercase sha256"),
        ("image", "ghcr.io/example/image@sha256:" + "a" * 64, "without a digest"),
        ("certificate_identity", " ", "must not be empty"),
        ("oidc_issuer", "http://issuer.example", "HTTPS"),
        ("timeout_seconds", 0, "between 1 and 120"),
    ],
)
def test_cosign_verification_rejects_invalid_inputs(
    field: str, value: object, message: str
) -> None:
    arguments: dict[str, object] = {
        "image": "ghcr.io/strategyg/rusa-ai-api",
        "digest": DIGEST,
        "certificate_identity": IDENTITY,
        "oidc_issuer": ISSUER,
    }
    arguments[field] = value
    with pytest.raises(ValueError, match=message):
        verify_cosign_image_signature(**arguments)

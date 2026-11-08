from __future__ import annotations

import hashlib
import json

from rusa_agents.certification import cli
from rusa_agents.certification.contracts import CertificationContractError


def test_validate_manifest_cli_prints_exact_byte_digest(tmp_path, capsys, monkeypatch) -> None:
    manifest = tmp_path / "release-manifest.json"
    raw = '{"release_id":"v1.0.0","source":{"repository":"StrategyG/RusaAi","commit_sha":"' + "a" * 40 + '"}}\n'
    manifest.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(cli, "validate_release_manifest", lambda *args, **kwargs: None)

    result = cli.main(
        [
            "validate-release-manifest",
            "--manifest",
            str(manifest),
            "--repository",
            "StrategyG/RusaAi",
            "--commit-sha",
            "a" * 40,
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == cli.EXIT_PASS
    assert output["status"] == "PASS"
    assert output["manifest_digest"] == "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def test_validate_manifest_cli_returns_fail_for_invalid_contract(tmp_path, capsys, monkeypatch) -> None:
    manifest = tmp_path / "release-manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    def reject(*args, **kwargs):
        raise CertificationContractError("invalid release contract")

    monkeypatch.setattr(cli, "validate_release_manifest", reject)
    result = cli.main(
        [
            "validate-release-manifest",
            "--manifest",
            str(manifest),
            "--repository",
            "StrategyG/RusaAi",
            "--commit-sha",
            "a" * 40,
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == cli.EXIT_FAIL
    assert output["status"] == "FAIL"
    assert output["error"] == "invalid release contract"


def test_evaluate_cli_maps_hold_to_nonzero_exit_code(tmp_path, capsys, monkeypatch) -> None:
    record = tmp_path / "certification-record.json"
    record.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "evaluate_production_certification",
        lambda *args, **kwargs: {"status": "HOLD", "blockers": ["security sign-off pending"]},
    )

    result = cli.main(
        [
            "evaluate",
            "--record",
            str(record),
            "--repository",
            "StrategyG/RusaAi",
            "--commit-sha",
            "a" * 40,
            "--manifest-digest",
            "sha256:" + "f" * 64,
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == cli.EXIT_HOLD
    assert output["status"] == "HOLD"


def test_evaluate_cli_maps_fail_to_distinct_exit_code(tmp_path, capsys, monkeypatch) -> None:
    record = tmp_path / "certification-record.json"
    record.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        cli,
        "evaluate_production_certification",
        lambda *args, **kwargs: {"status": "FAIL", "blockers": ["candidate SHA mismatch"]},
    )

    result = cli.main(
        [
            "evaluate",
            "--record",
            str(record),
            "--repository",
            "StrategyG/RusaAi",
            "--commit-sha",
            "a" * 40,
            "--manifest-digest",
            "sha256:" + "f" * 64,
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == cli.EXIT_FAIL
    assert output["status"] == "FAIL"


def test_cli_rejects_invalid_json_input(tmp_path, capsys) -> None:
    record = tmp_path / "broken.json"
    record.write_text("not-json", encoding="utf-8")

    result = cli.main(
        [
            "evaluate",
            "--record",
            str(record),
            "--repository",
            "StrategyG/RusaAi",
            "--commit-sha",
            "a" * 40,
            "--manifest-digest",
            "sha256:" + "f" * 64,
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert result == cli.EXIT_INPUT_ERROR
    assert output["status"] == "INPUT_ERROR"
    assert "cannot read JSON document" in output["error"]


def test_cli_maps_argument_errors_to_input_error(capsys) -> None:
    result = cli.main(["evaluate", "--record", "missing.json"])
    captured = capsys.readouterr()
    assert result == cli.EXIT_INPUT_ERROR
    assert "usage:" in captured.err.lower()

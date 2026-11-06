import hashlib
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")),
)

from model_runtime import ModelArtifactError, sha256_file, verify_model_artifact  # noqa: E402


def build_artifact(root, *, files=None):
    payloads = files or {
        "config.json": b'{"model_type":"test"}',
        "tokenizer.json": b'{"version":"1.0"}',
        "model.safetensors": b"test-weight-bytes",
    }
    digests = {}
    for name, payload in payloads.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        digests[name] = hashlib.sha256(payload).hexdigest()

    manifest = {
        "schema_version": 1,
        "model_id": "rusa-ai-test",
        "revision": "test-revision-001",
        "license": "test-only",
        "tokenizer_id": "rusa-ai-test-tokenizer",
        "tokenizer_revision": "test-tokenizer-revision",
        "files": digests,
    }
    manifest_path = root / "rusa-model-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return sha256_file(manifest_path)


def test_model_artifact_manifest_and_file_hashes_are_verified(tmp_path):
    manifest_sha = build_artifact(tmp_path)
    verified = verify_model_artifact(
        tmp_path,
        expected_manifest_sha256=manifest_sha,
        require_pinned_manifest=True,
    )
    assert verified.model_id == "rusa-ai-test"
    assert verified.revision == "test-revision-001"
    assert verified.files_verified == 3


def test_production_requires_pinned_manifest_digest(tmp_path):
    build_artifact(tmp_path)
    with pytest.raises(ModelArtifactError, match="RUSA_MODEL_MANIFEST_SHA256"):
        verify_model_artifact(
            tmp_path,
            expected_manifest_sha256=None,
            require_pinned_manifest=True,
        )


def test_manifest_digest_mismatch_fails_closed(tmp_path):
    build_artifact(tmp_path)
    with pytest.raises(ModelArtifactError, match="does not match"):
        verify_model_artifact(
            tmp_path,
            expected_manifest_sha256="0" * 64,
            require_pinned_manifest=True,
        )


def test_weight_tampering_is_detected(tmp_path):
    manifest_sha = build_artifact(tmp_path)
    (tmp_path / "model.safetensors").write_bytes(b"tampered")
    with pytest.raises(ModelArtifactError, match="checksum mismatch"):
        verify_model_artifact(
            tmp_path,
            expected_manifest_sha256=manifest_sha,
            require_pinned_manifest=True,
        )


def test_manifest_path_traversal_is_rejected(tmp_path):
    manifest = {
        "schema_version": 1,
        "model_id": "rusa-ai-test",
        "revision": "test-revision-001",
        "license": "test-only",
        "tokenizer_id": "rusa-ai-test-tokenizer",
        "tokenizer_revision": "test-tokenizer-revision",
        "files": {
            "config.json": hashlib.sha256(b"{}").hexdigest(),
            "tokenizer.json": hashlib.sha256(b"{}").hexdigest(),
            "../outside.safetensors": hashlib.sha256(b"weights").hexdigest(),
        },
    }
    (tmp_path / "config.json").write_bytes(b"{}")
    (tmp_path / "tokenizer.json").write_bytes(b"{}")
    (tmp_path / "rusa-model-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    manifest_sha = sha256_file(tmp_path / "rusa-model-manifest.json")
    with pytest.raises(ModelArtifactError, match="path traversal"):
        verify_model_artifact(
            tmp_path,
            expected_manifest_sha256=manifest_sha,
            require_pinned_manifest=True,
        )


def test_missing_weight_files_are_rejected(tmp_path):
    build_artifact(
        tmp_path,
        files={
            "config.json": b"{}",
            "tokenizer.json": b"{}",
        },
    )
    manifest_sha = sha256_file(tmp_path / "rusa-model-manifest.json")
    with pytest.raises(ModelArtifactError, match="safetensors weights"):
        verify_model_artifact(
            tmp_path,
            expected_manifest_sha256=manifest_sha,
            require_pinned_manifest=True,
        )


def test_unmanifested_extra_weight_shard_is_rejected(tmp_path):
    manifest_sha = build_artifact(tmp_path)
    (tmp_path / "extra-shard.safetensors").write_bytes(b"unlisted weights")
    with pytest.raises(ModelArtifactError, match="Unmanifested model artifact file"):
        verify_model_artifact(
            tmp_path,
            expected_manifest_sha256=manifest_sha,
            require_pinned_manifest=True,
        )

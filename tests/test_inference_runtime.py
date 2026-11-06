import asyncio
import hashlib
import json
import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), "../llm-project/serve")),
)

from model_runtime import (  # noqa: E402
    ModelArtifactError,
    TransformersBackend,
    sha256_file,
    verify_model_artifact,
)


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


def test_real_transformers_backend_generates_from_verified_local_artifact(tmp_path):
    """Exercise actual Transformers loading and token generation without network access."""
    import torch
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import AutoModelForCausalLM, GPT2Config, PreTrainedTokenizerFast

    vocabulary = {
        "[UNK]": 0,
        "[PAD]": 1,
        "[BOS]": 2,
        "[EOS]": 3,
        "user": 4,
        "assistant": 5,
        ":": 6,
        "hello": 7,
        "world": 8,
    }
    tokenizer_core = Tokenizer(
        models.WordLevel(vocabulary, unk_token="[UNK]")
    )
    tokenizer_core.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=tokenizer_core,
        unk_token="[UNK]",
        pad_token="[PAD]",
        bos_token="[BOS]",
        eos_token="[EOS]",
    )
    tokenizer.model_max_length = 64
    tokenizer.chat_template = (
        "{% for message in messages %}"
        "{{ message['role'] }} : {{ message['content'] }} "
        "{% endfor %}"
        "{% if add_generation_prompt %}assistant :{% endif %}"
    )
    tokenizer.save_pretrained(tmp_path)

    config = GPT2Config(
        vocab_size=len(vocabulary),
        n_positions=64,
        n_ctx=64,
        n_embd=16,
        n_layer=1,
        n_head=2,
        bos_token_id=vocabulary["[BOS]"],
        eos_token_id=vocabulary["[EOS]"],
        pad_token_id=vocabulary["[PAD]"],
        tie_word_embeddings=False,
    )
    model = AutoModelForCausalLM.from_config(config)
    model.eval()
    messages = [{"role": "user", "content": "hello"}]
    encoded = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
    )
    hidden = model.transformer(
        input_ids=encoded["input_ids"]
    ).last_hidden_state[0, -1]
    with torch.no_grad():
        model.lm_head.weight.zero_()
        model.lm_head.weight[vocabulary["hello"]].copy_(hidden * 100)
    model.save_pretrained(tmp_path, safe_serialization=True)

    manifest_files = {
        item.relative_to(tmp_path).as_posix(): sha256_file(item)
        for item in sorted(tmp_path.rglob("*"))
        if item.is_file()
    }
    manifest = {
        "schema_version": 1,
        "model_id": "rusa-ai-tiny-transformers-smoke",
        "revision": "ci-fixture",
        "license": "test-only",
        "tokenizer_id": "rusa-ai-tiny-transformers-smoke",
        "tokenizer_revision": "ci-fixture",
        "files": manifest_files,
    }
    manifest_path = tmp_path / "rusa-model-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True),
        encoding="utf-8",
    )
    manifest_sha = sha256_file(manifest_path)

    verified = verify_model_artifact(
        tmp_path,
        expected_manifest_sha256=manifest_sha,
        require_pinned_manifest=True,
    )
    assert verified.files_verified == len(manifest_files)

    backend = TransformersBackend.from_pretrained(
        str(tmp_path),
        timeout_seconds=10,
        max_model_len=64,
    )
    try:
        result = asyncio.run(
            backend.generate(
                messages,
                max_new_tokens=1,
                temperature=0.0,
                top_p=1.0,
            )
        )
    finally:
        asyncio.run(backend.close())

    assert result.content == "hello"
    assert result.prompt_tokens > 0
    assert result.completion_tokens == 1
    assert result.finish_reason == "length"

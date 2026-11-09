import hashlib
import os

import pytest

from rusa_agents.gguf import verify_gguf_file


def test_verifies_pinned_gguf_file(tmp_path):
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"gguf-fixture")
    expected = hashlib.sha256(model.read_bytes()).hexdigest()

    verified = verify_gguf_file(model, expected)

    assert verified.path == model
    assert verified.size_bytes == len(b"gguf-fixture")
    assert verified.sha256 == expected


def test_rejects_wrong_gguf_checksum(tmp_path):
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"gguf-fixture")

    with pytest.raises(ValueError, match="does not match"):
        verify_gguf_file(model, "0" * 64)


def test_rejects_non_absolute_model_path(tmp_path, monkeypatch):
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"gguf-fixture")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="absolute"):
        verify_gguf_file("tiny.gguf", hashlib.sha256(b"gguf-fixture").hexdigest())


def test_rejects_non_gguf_file(tmp_path):
    model = tmp_path / "model.bin"
    model.write_bytes(b"gguf-fixture")

    with pytest.raises(ValueError, match=".gguf"):
        verify_gguf_file(model, hashlib.sha256(b"gguf-fixture").hexdigest())


def test_rejects_symlinked_model(tmp_path):
    model = tmp_path / "tiny.gguf"
    target = tmp_path / "actual.gguf"
    target.write_bytes(b"gguf-fixture")
    try:
        model.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("Symlinks are unavailable on this platform")

    with pytest.raises(OSError):
        verify_gguf_file(model, hashlib.sha256(b"gguf-fixture").hexdigest())


def test_rejects_invalid_expected_checksum(tmp_path):
    model = tmp_path / "tiny.gguf"
    model.write_bytes(b"gguf-fixture")

    with pytest.raises(ValueError, match="64 hexadecimal"):
        verify_gguf_file(model, "not-a-checksum")

import os
from types import SimpleNamespace

import pytest

from pathlib import Path
import sys

sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../llm-project/serve")
    ),
)

from inference import InferenceNotReady, ModelRuntime  # noqa: E402


class FakeTokenizer:
    pad_token_id = 0
    eos_token_id = 1

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=True):
        return "|".join(f"{message['role']}:{message['content']}" for message in messages)

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": list(text)}


def test_load_requires_model_path(monkeypatch):
    monkeypatch.delenv("RUSA_MODEL_PATH", raising=False)
    runtime = ModelRuntime()
    with pytest.raises(RuntimeError, match="RUSA_MODEL_PATH is not configured"):
        runtime.load()


def test_load_requires_existing_local_directory(monkeypatch, tmp_path):
    missing = tmp_path / "not-created"
    monkeypatch.setenv("RUSA_MODEL_PATH", str(missing))
    runtime = ModelRuntime()
    with pytest.raises(RuntimeError, match="mounted model directory"):
        runtime.load()


def test_runtime_reports_not_ready_without_weights():
    runtime = ModelRuntime()
    assert runtime.ready is False
    with pytest.raises(InferenceNotReady):
        runtime.generate(
            [{"role": "user", "content": "hello"}],
            temperature=0.0,
            top_p=1.0,
            max_tokens=16,
        )


def test_prompt_uses_checkpoint_chat_template():
    runtime = ModelRuntime()
    runtime.model = SimpleNamespace(device="cpu")
    runtime.tokenizer = FakeTokenizer()
    prompt = runtime._prompt([{"role": "user", "content": "hello"}])
    assert prompt == "user:hello"


def test_input_limit_reserves_space_for_generation():
    runtime = ModelRuntime()
    runtime.model = SimpleNamespace(device="cpu")
    runtime.tokenizer = FakeTokenizer()
    runtime.max_input_tokens = 90
    runtime.max_context_tokens = 100
    assert runtime._input_limit(40) == 60


def test_stream_usage_is_counted_from_prompt_and_output():
    runtime = ModelRuntime()
    runtime.model = SimpleNamespace(device="cpu")
    runtime.tokenizer = FakeTokenizer()
    usage = runtime.count_usage(
        [{"role": "user", "content": "hello"}],
        "world",
    )
    assert usage["prompt_tokens"] > 0
    assert usage["completion_tokens"] == 5
    assert usage["total_tokens"] == usage["prompt_tokens"] + 5



def test_generation_kwargs_include_deadline_and_deterministic_mode(monkeypatch):
    runtime = ModelRuntime()
    runtime.tokenizer = FakeTokenizer()
    monkeypatch.setenv("RUSA_GENERATION_TIMEOUT_SECONDS", "45")
    kwargs = runtime._generation_kwargs(
        temperature=0.0,
        top_p=0.9,
        max_tokens=32,
    )
    assert kwargs["max_time"] == 45.0
    assert kwargs["do_sample"] is False
    assert "temperature" not in kwargs

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from rusa_training.dataset import DatasetValidationError, validate_dataset_splits

ROOT = Path(__file__).resolve().parents[1]
TRAINER_PATH = ROOT / "llm-project" / "train" / "train_sft.py"


def _load_trainer_module():
    spec = importlib.util.spec_from_file_location("rusa_agent_train_sft", TRAINER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeDataset:
    pass


_FAKE_TORCH = SimpleNamespace(
    utils=SimpleNamespace(data=SimpleNamespace(Dataset=_FakeDataset))
)


class _Tokenizer:
    eos_token_id = 99
    pad_token_id = 0

    def __init__(self, prompt_ids: list[int], completion_ids: list[int]):
        self.prompt_ids = prompt_ids
        self.completion_ids = completion_ids

    def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
        assert tokenize is True
        assert add_generation_prompt is True
        return list(self.prompt_ids)

    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        return list(self.completion_ids)


def _record(record_id: str = "train-1", *, scenario_id: str | None = None,
            user_prompt: str = "Diagnose the test incident.", response_message: str = "Inspect metrics.") -> dict:
    record = {
        "id": record_id,
        "agent_role": "analyst",
        "messages": [
            {"role": "system", "content": "Follow the analyst policy."},
            {"role": "user", "content": user_prompt},
        ],
        "response": {
            "message": response_message,
            "actions": [],
            "done": True,
            "blocked": True,
            "blockers": ["This fixture is a terminal blocked response."],
        },
    }
    if scenario_id is not None:
        record["scenario_id"] = scenario_id
    return record


def test_sft_builder_masks_prompt_and_trains_on_completion_and_eos():
    trainer = _load_trainer_module()
    tokenizer = _Tokenizer([11, 12, 13], [21, 22])

    dataset = trainer._build_torch_dataset([_record()], tokenizer, 16, _FAKE_TORCH)

    assert len(dataset) == 1
    example = dataset[0]
    assert example["input_ids"] == [11, 12, 13, 21, 22, 99]
    assert example["attention_mask"] == [1, 1, 1, 1, 1, 1]
    assert example["labels"] == [-100, -100, -100, 21, 22, 99]


def test_sft_builder_rejects_overlength_instead_of_silently_truncating():
    trainer = _load_trainer_module()
    tokenizer = _Tokenizer([1, 2, 3, 4], [5, 6])

    with pytest.raises(ValueError, match="exceeds max_seq_length"):
        trainer._build_torch_dataset([_record()], tokenizer, 6, _FAKE_TORCH)


def test_sft_builder_keeps_each_record_as_an_independent_example():
    trainer = _load_trainer_module()

    class PerRecordTokenizer(_Tokenizer):
        def apply_chat_template(self, messages, *, tokenize, add_generation_prompt):
            return [1] if "incident A" in messages[-1]["content"] else [2]

        def encode(self, text, *, add_special_tokens):
            return [3] if '"message":"response A"' in text else [4]

    tokenizer = PerRecordTokenizer([], [])
    records = [
        _record("a", user_prompt="incident A", response_message="response A"),
        _record("b", user_prompt="incident B", response_message="response B"),
    ]
    dataset = trainer._build_torch_dataset(records, tokenizer, 8, _FAKE_TORCH)

    assert len(dataset) == 2
    assert dataset[0]["input_ids"] == [1, 3, 99]
    assert dataset[1]["input_ids"] == [2, 4, 99]


def test_split_validation_rejects_same_prompt_with_different_target():
    train = [_record("train-1", response_message="expected target A")]
    evaluation = [_record("eval-1", response_message="expected target B")]

    with pytest.raises(DatasetValidationError, match="role/prompt"):
        validate_dataset_splits(train, evaluation)


def test_split_validation_rejects_scenario_group_crossing_splits():
    train = [_record("train-1", scenario_id="incident-family-17", user_prompt="first view")]
    evaluation = [_record("eval-1", scenario_id="incident-family-17", user_prompt="second view")]

    with pytest.raises(DatasetValidationError, match="scenario_id"):
        validate_dataset_splits(train, evaluation)


def test_split_validation_requires_complete_scenario_id_coverage():
    train = [_record("train-1", scenario_id="incident-family-1", user_prompt="training scenario")]
    # Keep this prompt distinct so the test reaches the scenario_id coverage
    # guard instead of correctly failing earlier on prompt leakage.
    evaluation = [_record("eval-1", user_prompt="held-out evaluation scenario")]

    with pytest.raises(DatasetValidationError, match="every record"):
        validate_dataset_splits(train, evaluation)


def test_split_validation_allows_disjoint_scenario_groups():
    train = [_record("train-1", scenario_id="incident-family-1")]
    evaluation = [_record("eval-1", scenario_id="incident-family-2", user_prompt="different incident")]

    validate_dataset_splits(train, evaluation)

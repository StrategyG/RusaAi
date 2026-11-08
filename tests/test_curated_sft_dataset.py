from pathlib import Path

from rusa_training.dataset import (
    load_jsonl,
    validate_dataset_splits,
    validate_eval_distribution,
)

ROOT = Path(__file__).resolve().parents[1]
TRAIN_FILE = ROOT / "llm-project" / "data" / "curated" / "train.jsonl"
EVAL_FILE = ROOT / "llm-project" / "data" / "curated" / "eval.jsonl"
EXPECTED_ROLES = {"analyst", "architect", "developer", "debugger", "tester", "security"}


def test_curated_sft_dataset_meets_seed_experiment_guardrails():
    train = load_jsonl(TRAIN_FILE)
    evaluation = load_jsonl(EVAL_FILE)

    assert len(train) == 120
    assert len(evaluation) == 36
    assert {item["agent_role"] for item in train} == EXPECTED_ROLES
    assert {item["agent_role"] for item in evaluation} == EXPECTED_ROLES

    validate_dataset_splits(train, evaluation)
    validate_eval_distribution(
        evaluation,
        min_blocked_fraction=0.10,
        min_unblocked_fraction=0.50,
    )

    assert sum(bool(item["response"].get("blocked", False)) for item in evaluation) == 12

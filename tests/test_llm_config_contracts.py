import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "llm"
CONFIG_DIR = ROOT / "llm-project" / "configs"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def validate_document(document, schema):
    validator = Draft202012Validator(schema)
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    assert not errors, "\n".join(
        f"{list(error.absolute_path)}: {error.message}" for error in errors
    )


def test_model_blueprint_matches_schema():
    config = load_yaml(CONFIG_DIR / "model.yaml")
    schema = load_json(SCHEMA_DIR / "model-config.schema.json")
    validate_document(config, schema)


def test_model_attention_geometry_is_valid():
    config = load_yaml(CONFIG_DIR / "model.yaml")
    architecture = config["model"]["architecture"]
    moe = config["model"]["moe"]

    assert architecture["hidden_size"] % architecture["attention_heads"] == 0
    assert architecture["attention_heads"] % architecture["kv_heads"] == 0
    assert moe["experts_per_token"] <= moe["num_experts"]
    assert config["status"] == "planned"


@pytest.mark.parametrize(
    "profile_name",
    ["local_dev", "single_gpu", "multi_gpu"],
)
def test_runtime_profile_matches_schema(profile_name):
    profile = load_yaml(CONFIG_DIR / "profiles" / f"{profile_name}.yaml")
    schema = load_json(SCHEMA_DIR / "runtime-profile.schema.json")
    validate_document(profile, schema)
    assert profile["profile"] == profile_name
    assert profile["status"] == "proposed"


def test_local_profile_is_safe_for_contract_only_work():
    profile = load_yaml(CONFIG_DIR / "profiles" / "local_dev.yaml")

    assert profile["hardware"]["device"] == "cpu"
    assert profile["hardware"]["gpu_count"] == 0
    assert profile["model"]["source"] == "blueprint_only"
    assert profile["training"]["enabled"] is False
    assert profile["inference"]["enabled"] is False


def test_single_gpu_profile_is_adaptation_not_pretraining():
    profile = load_yaml(CONFIG_DIR / "profiles" / "single_gpu.yaml")

    assert profile["hardware"]["gpu_count"] == 1
    assert profile["model"]["source"] == "pretrained_checkpoint"
    assert profile["training"]["strategy"] == "qlora"
    assert profile["model"]["context_mode"] == "standard"


def test_multi_gpu_profile_requires_distributed_backend():
    profile = load_yaml(CONFIG_DIR / "profiles" / "multi_gpu.yaml")

    assert profile["hardware"]["gpu_count"] >= 2
    assert profile["hardware"]["distributed_backend"] == "nccl"
    assert profile["training"]["strategy"] == "fsdp"
    assert profile["model"]["source"] == "pretrained_checkpoint"

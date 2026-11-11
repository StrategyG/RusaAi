import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "llm-project"))

from model.blueprint import ProjectBlueprint  # noqa: E402


def test_rusaai_edge_blueprint_matches_v2_dense_transformer_target():
    blueprint = ProjectBlueprint()
    blueprint.validate()

    model = blueprint.model
    assert model.model_id == "rusaai-edge-1.54b"
    assert model.name == "RusaAi-Edge-1.54B"
    assert model.layers == 32
    assert model.hidden_size == 2048
    assert model.query_heads == 16
    assert model.kv_heads == 4
    assert model.head_dimension == 128
    assert model.gqa_ratio == 4
    assert model.ffn == "swiglu"
    assert model.ffn_intermediate_size == 5632
    assert model.vocab_size == 48000
    assert model.tied_embeddings is True
    assert model.positional_encoding == "rope"
    assert model.rms_norm_epsilon == 1e-5
    assert model.linear_bias is False
    assert model.dropout == 0.0
    assert model.moe_enabled is False
    assert model.parameter_count == 1_541_277_696
    assert blueprint.inference.quantization == "Q4_K_M"
    assert blueprint.inference.batch_size == 128
    assert blueprint.inference.ubatch_size == 64
    assert blueprint.inference.http_threads == 2
    assert blueprint.inference.minimum_free_ram_gib == 2


def test_blueprint_does_not_claim_model_artifacts_or_training_exist():
    blueprint = ProjectBlueprint()
    assert blueprint.status == "DESIGN_ONLY"
    assert blueprint.training.checkpoint_status == "NOT_SELECTED"
    assert blueprint.tokenizer_status == "NOT_SELECTED"
    assert blueprint.artifact_status == "NOT_BUILT"
    assert blueprint.training.training_status == "NOT_STARTED"
    assert blueprint.benchmark_status == "NOT_TESTED"


def test_blueprint_rejects_moe_and_parameter_drift():
    with pytest.raises(ValueError, match="MoE"):
        replace(ProjectBlueprint().model, moe_enabled=True).validate()
    with pytest.raises(ValueError, match="parameter count"):
        replace(ProjectBlueprint().model, layers=31).validate()


def test_model_yaml_matches_the_reference_parameter_breakdown():
    config = yaml.safe_load((ROOT / "llm-project/configs/model.yaml").read_text(encoding="utf-8"))
    assert config["model"]["model_id"] == "rusaai-edge-1.54b"
    assert config["model"]["architecture"]["family"] == "dense_decoder_only_transformer"
    assert config["model"]["architecture"]["moe_enabled"] is False
    assert config["model"]["architecture"]["target_parameters"] == 1_541_277_696
    assert config["parameter_reference"] == {
        "attention_parameters_per_layer": 10_485_760,
        "swiglu_parameters_per_layer": 34_603_008,
        "embedding_parameters": 98_304_000,
        "normalization_parameters": 133_120,
        "total_parameters": 1_541_277_696,
    }


def test_cpu_inference_yaml_enforces_single_active_generation():
    config = yaml.safe_load(
        (ROOT / "llm-project/configs/inference.yaml").read_text(encoding="utf-8")
    )
    assert config["runtime"]["backend"] == "llama.cpp"
    assert config["runtime"]["model_format"] == "GGUF"
    assert config["runtime"]["device"] == "CPU"
    assert config["runtime"]["gpu_offload_layers"] == 0
    assert config["runtime"]["max_active_generations"] == 1
    assert config["context"]["initial_tokens"] == 4096
    assert config["context"]["extended_tokens"] == 8192
    assert config["queue"]["max_waiting_profile_16gb"] == 3
    assert config["queue"]["max_waiting_profile_8gb"] == 1
    assert config["deployment_profiles"]["rusa-cpu-16"]["max_waiting_inferences"] == 3
    assert config["deployment_profiles"]["rusa-edge-8"]["max_waiting_inferences"] == 1


def test_8gb_profile_uses_a_smaller_bounded_waiting_queue():
    values = {}
    for line in (ROOT / ".env.cpu-8gb.example").read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    assert values["RUSA_MAX_CONCURRENT_INFERENCES"] == "1"
    assert values["RUSA_MAX_WAITING_INFERENCES"] == "1"
    assert values["RUSA_MODEL_CONTEXT"] == "4096"
    assert values["RUSA_MODEL_PARALLEL"] == "1"

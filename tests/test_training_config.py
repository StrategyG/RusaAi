from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "llm-project" / "configs"


@pytest.mark.parametrize(
    "config_name",
    ["training.yaml", "training-t4.yaml"],
)
def test_sft_training_config_is_valid_yaml_and_has_required_sections(config_name: str):
    config = yaml.safe_load((CONFIG_DIR / config_name).read_text(encoding="utf-8"))

    assert isinstance(config, dict)
    assert config["model"]["id"]
    assert config["model"]["revision"]
    assert config["dataset"]["max_seq_length"] > 0
    assert config["training"]["method"] == "qlora"
    assert config["qlora"]["quant_type"] == "nf4"
    assert config["lora"]["r"] > 0


def test_t4_training_profile_declares_a_valid_fp16_experiment_target():
    config = yaml.safe_load(
        (CONFIG_DIR / "training-t4.yaml").read_text(encoding="utf-8")
    )

    assert config["hardware_target"]["gpu"] == "NVIDIA Tesla T4"
    assert config["hardware_target"]["minimum_vram_gib"] == 14
    assert config["hardware_target"]["precision"] == "fp16"
    assert isinstance(config["hardware_target"]["notes"], list)
    assert len(config["hardware_target"]["notes"]) >= 3
    assert config["qlora"]["compute_dtype"] == "float16"
    assert config["dataset"]["max_seq_length"] == 2048

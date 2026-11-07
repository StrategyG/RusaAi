import runpy
from pathlib import Path

import pytest

BLUEPRINT_PATH = Path(__file__).resolve().parents[1] / "llm-project" / "model" / "blueprint.py"
BLUEPRINT = runpy.run_path(str(BLUEPRINT_PATH))


def test_default_blueprint_does_not_claim_unverified_model_capabilities():
    blueprint = BLUEPRINT["ProjectBlueprint"]()
    blueprint.validate()

    assert blueprint.status == "PLANNED"
    assert blueprint.model.name == "rusa-ai-untrained-blueprint"
    assert blueprint.model.layers is None
    assert blueprint.model.hidden_size is None
    assert blueprint.model.context_window is None
    assert blueprint.model.modalities == []
    assert blueprint.model.moe.enabled is False
    assert blueprint.training.post_training == []
    assert blueprint.inference.quantization == []


def test_blueprint_rejects_inconsistent_attention_dimensions():
    blueprint = BLUEPRINT["ProjectBlueprint"](
        model=BLUEPRINT["ModelSpec"](hidden_size=100, attention_heads=3)
    )
    with pytest.raises(ValueError, match="divisible"):
        blueprint.validate()


def test_blueprint_cannot_claim_verified_without_artifact_evidence():
    blueprint = BLUEPRINT["ProjectBlueprint"](status="VERIFIED")
    with pytest.raises(ValueError, match="artifact and evaluation evidence"):
        blueprint.validate()

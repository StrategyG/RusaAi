"""Untrained RUSA-AI model design placeholder.

This module records only explicitly selected design parameters. It does not
instantiate a neural network, define a verified model configuration, or imply
that training data, weights, multimodal support, or performance measurements
exist.
"""
from dataclasses import dataclass, field


@dataclass
class MoESpec:
    enabled: bool = False
    num_experts: int | None = None
    experts_per_token: int | None = None

    def validate(self) -> None:
        if not self.enabled:
            return
        if type(self.num_experts) is not int or self.num_experts <= 0:
            raise ValueError("enabled MoE requires a positive num_experts value")
        if (
            type(self.experts_per_token) is not int
            or not 1 <= self.experts_per_token <= self.num_experts
        ):
            raise ValueError("enabled MoE requires experts_per_token within num_experts")


@dataclass
class ModelSpec:
    # This is a project placeholder, not a model ID or a claim of model lineage.
    name: str = "rusa-ai-untrained-blueprint"
    model_type: str | None = None
    layers: int | None = None
    hidden_size: int | None = None
    attention_heads: int | None = None
    kv_heads: int | None = None
    context_window: int | None = None
    positional_encoding: str | None = None
    normalization: str | None = None
    activation: str | None = None
    modalities: list[str] = field(default_factory=list)
    moe: MoESpec = field(default_factory=MoESpec)

    def validate(self) -> None:
        for name in (
            "layers",
            "hidden_size",
            "attention_heads",
            "kv_heads",
            "context_window",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer when specified")

        if (
            self.hidden_size is not None
            and self.attention_heads is not None
            and self.hidden_size % self.attention_heads
        ):
            raise ValueError("hidden_size must be divisible by attention_heads")
        if (
            self.attention_heads is not None
            and self.kv_heads is not None
            and self.attention_heads % self.kv_heads
        ):
            raise ValueError("attention_heads must be divisible by kv_heads")
        if any(not isinstance(item, str) or not item.strip() for item in self.modalities):
            raise ValueError("modalities must contain non-empty strings")
        self.moe.validate()


@dataclass
class TrainingSpec:
    # Empty values mean no training configuration has been approved or run.
    objective: str | None = None
    precision: str | None = None
    post_training: list[str] = field(default_factory=list)


@dataclass
class InferenceSpec:
    # These are intentionally unset until implemented and measured.
    quantization: list[str] = field(default_factory=list)
    speculative_decoding: bool | None = None
    tool_use: bool | None = None


@dataclass
class ProjectBlueprint:
    model: ModelSpec = field(default_factory=ModelSpec)
    training: TrainingSpec = field(default_factory=TrainingSpec)
    inference: InferenceSpec = field(default_factory=InferenceSpec)
    status: str = "PLANNED"

    def validate(self) -> None:
        if self.status != "PLANNED":
            raise ValueError(
                "Blueprint metadata cannot claim implementation or verification without a versioned model artifact and evaluation evidence"
            )
        self.model.validate()


if __name__ == "__main__":
    blueprint = ProjectBlueprint()
    blueprint.validate()
    print(blueprint)

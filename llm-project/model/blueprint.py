"""Design-only specification for the RusaAi-Edge-1.54B neural core."""
from __future__ import annotations

from dataclasses import dataclass, field


REFERENCE_MODEL_ID = "rusaai-edge-1.54b"
REFERENCE_MODEL_NAME = "RusaAi-Edge-1.54B"
REFERENCE_PARAMETER_COUNT = 1_541_277_696
REFERENCE_DIMENSIONS = {
    "layers": 32,
    "hidden_size": 2048,
    "query_heads": 16,
    "kv_heads": 4,
    "ffn_intermediate_size": 5632,
    "vocab_size": 48000,
}


@dataclass(frozen=True)
class ModelSpec:
    model_id: str = REFERENCE_MODEL_ID
    name: str = REFERENCE_MODEL_NAME
    architecture: str = "dense_decoder_only_transformer"
    normalization_layout: str = "pre_norm"
    layers: int = 32
    hidden_size: int = 2048
    query_heads: int = 16
    kv_heads: int = 4
    ffn: str = "swiglu"
    ffn_intermediate_size: int = 5632
    vocab_size: int = 48000
    tied_embeddings: bool = True
    positional_encoding: str = "rope"
    rms_norm_epsilon: float = 1e-5
    linear_bias: bool = False
    dropout: float = 0.0
    moe_enabled: bool = False
    target_parameters: int = REFERENCE_PARAMETER_COUNT

    @property
    def head_dimension(self) -> int:
        return self.hidden_size // self.query_heads

    @property
    def gqa_ratio(self) -> int:
        return self.query_heads // self.kv_heads

    @property
    def parameter_count(self) -> int:
        head_dim = self.head_dimension
        kv_width = self.kv_heads * head_dim
        attention_per_layer = (
            self.hidden_size * self.hidden_size
            + 2 * self.hidden_size * kv_width
            + self.hidden_size * self.hidden_size
        )
        ffn_per_layer = 3 * self.hidden_size * self.ffn_intermediate_size
        embedding_parameters = self.vocab_size * self.hidden_size
        norm_parameters = (2 * self.layers + 1) * self.hidden_size
        return (
            self.layers * (attention_per_layer + ffn_per_layer)
            + embedding_parameters
            + norm_parameters
        )

    def validate(self) -> None:
        positive = {
            "layers": self.layers,
            "hidden_size": self.hidden_size,
            "query_heads": self.query_heads,
            "kv_heads": self.kv_heads,
            "ffn_intermediate_size": self.ffn_intermediate_size,
            "vocab_size": self.vocab_size,
        }
        if any(value <= 0 for value in positive.values()):
            raise ValueError("All model dimensions must be positive")
        if self.model_id != REFERENCE_MODEL_ID or self.name != REFERENCE_MODEL_NAME:
            raise ValueError("Model identity must match the RusaAi-Edge v2.0 reference target")
        for field_name, expected in REFERENCE_DIMENSIONS.items():
            actual = getattr(self, field_name)
            if actual != expected:
                raise ValueError(
                    f"{field_name} must equal the RusaAi-Edge v2.0 reference value {expected}"
                )
        if self.target_parameters != REFERENCE_PARAMETER_COUNT:
            raise ValueError(
                "target_parameters must equal the RusaAi-Edge v2.0 reference parameter count"
            )
        if self.hidden_size % self.query_heads:
            raise ValueError("hidden_size must be divisible by query_heads")
        if self.query_heads % self.kv_heads:
            raise ValueError("query_heads must be divisible by kv_heads")
        if self.architecture != "dense_decoder_only_transformer":
            raise ValueError("RusaAi-Edge v2.0 must use a dense decoder-only Transformer")
        if self.normalization_layout != "pre_norm" or self.ffn != "swiglu":
            raise ValueError("RusaAi-Edge v2.0 requires Pre-Norm and SwiGLU")
        if self.positional_encoding != "rope":
            raise ValueError("RusaAi-Edge v2.0 baseline requires RoPE")
        if self.rms_norm_epsilon != 1e-5:
            raise ValueError("RusaAi-Edge v2.0 requires RMSNorm epsilon 1e-5")
        if not self.tied_embeddings or self.linear_bias or self.dropout != 0.0:
            raise ValueError("RusaAi-Edge v2.0 requires tied embeddings, no bias, and zero dropout")
        if self.moe_enabled:
            raise ValueError("MoE is not used in RusaAi-Edge Core V1")
        if self.parameter_count != REFERENCE_PARAMETER_COUNT:
            raise ValueError(
                "Model dimensions do not match the RusaAi-Edge v2.0 reference parameter count"
            )


@dataclass(frozen=True)
class InferenceSpec:
    runtime: str = "llama.cpp"
    model_format: str = "GGUF"
    device: str = "CPU"
    quantization: str = "Q4_K_M"
    gpu_offload_layers: int = 0
    max_active_generations: int = 1
    initial_context_tokens: int = 4096
    extended_context_tokens: int = 8192
    extended_context_requires_validation: bool = True
    kv_cache_quantization: str = "Q8_0"
    kv_cache_quantization_optional: bool = True
    generation_threads: int = 4
    batch_threads: int = 4
    http_threads: int = 2
    batch_size: int = 128
    ubatch_size: int = 64
    minimum_free_ram_gib: int = 2

    def validate(self) -> None:
        if self.runtime != "llama.cpp" or self.model_format != "GGUF":
            raise ValueError("RusaAi CPU baseline requires llama.cpp with GGUF")
        if self.device != "CPU" or self.gpu_offload_layers != 0:
            raise ValueError("RusaAi CPU baseline must disable GPU offload")
        if self.max_active_generations != 1:
            raise ValueError("RusaAi CPU baseline allows exactly one active generation")
        if self.quantization != "Q4_K_M":
            raise ValueError("RusaAi CPU baseline requires Q4_K_M model quantization")
        if (self.batch_size, self.ubatch_size, self.http_threads) != (128, 64, 2):
            raise ValueError("RusaAi CPU baseline requires batch/ubatch/http threads 128/64/2")
        if self.minimum_free_ram_gib != 2:
            raise ValueError("RusaAi CPU baseline must reserve at least 2 GiB of free RAM")
        if self.initial_context_tokens != 4096 or self.extended_context_tokens != 8192:
            raise ValueError("RusaAi context targets must remain 4K baseline and 8K validated")
        if not self.extended_context_requires_validation:
            raise ValueError("8K context must remain gated by device memory validation")
        if min(self.generation_threads, self.batch_threads, self.http_threads) < 1:
            raise ValueError("CPU thread counts must be positive")


@dataclass(frozen=True)
class TrainingSpec:
    strategy: str = "product_model"
    required_foundation_family: str = "non_qwen"
    checkpoint_status: str = "NOT_SELECTED"
    training_status: str = "NOT_STARTED"
    from_scratch_research_parameters: int = 100_100_000


@dataclass(frozen=True)
class ProjectBlueprint:
    model: ModelSpec = field(default_factory=ModelSpec)
    inference: InferenceSpec = field(default_factory=InferenceSpec)
    training: TrainingSpec = field(default_factory=TrainingSpec)
    status: str = "DESIGN_ONLY"
    tokenizer_status: str = "NOT_SELECTED"
    artifact_status: str = "NOT_BUILT"
    benchmark_status: str = "NOT_TESTED"

    def validate(self) -> None:
        self.model.validate()
        self.inference.validate()
        if self.training.required_foundation_family != "non_qwen":
            raise ValueError("RusaAi-Edge foundation checkpoint must be non-Qwen")
        if self.status != "DESIGN_ONLY":
            raise ValueError("The supplied architecture defines a design target, not a built model")


if __name__ == "__main__":
    blueprint = ProjectBlueprint()
    blueprint.validate()
    print(
        f"{blueprint.model.model_id}: "
        f"{blueprint.model.parameter_count:,} target parameters; "
        f"status={blueprint.status}"
    )

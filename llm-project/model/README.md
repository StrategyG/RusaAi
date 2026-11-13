# RusaAi-Edge model architecture

This module contains the design specification for RusaAi-Edge-1.54B.

The V1 target is a dense decoder-only Transformer with Pre-Norm, 32 decoder
layers, hidden size 2,048, 16 query heads, 4 KV heads, 128-dimensional heads,
SwiGLU intermediate size 5,632, a 48,000-token vocabulary, tied embeddings,
RoPE, RMSNorm epsilon 1e-5, no linear bias, zero initial dropout, and no MoE.

The reference parameter count is 1,541,277,696. The calculation is exposed
by blueprint.py and checked against the architecture target.

Status is DESIGN_ONLY. A non-Qwen foundation checkpoint and tokenizer have
not been selected. Model weights and GGUF artifacts have not been built,
training has not started, and CPU benchmarks have not been run. This module
does not implement or claim a runnable neural model.

The deployment target is GGUF Q4_K_M through llama.cpp on CPU with GPU
offload disabled and one active generation. Context starts at 4K; 8K remains
gated by device-specific memory validation.

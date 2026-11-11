# Artifact contract

The existing safetensors model manifest schema is
schemas/llm/model-artifact-manifest.schema.json. CPU GGUF intake currently uses
rusa_agents/gguf.py and scripts/verify_gguf_model.py to enforce an absolute
regular-file path and a pinned SHA-256 checksum.

No RusaAi-Edge-1.54B checkpoint, tokenizer, or GGUF release artifact exists
yet. A future artifact-contract version must bind model identity, immutable
source revision, license, tokenizer revision, quantization, runtime
compatibility, and artifact digests before a model can be promoted.

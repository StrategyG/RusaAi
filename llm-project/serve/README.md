# Serving and runtime module

Status: PARTIALLY IMPLEMENTED; not production-certified.

Implemented:
- FastAPI OpenAI-compatible inference gateway.
- Configured OpenAI-compatible external provider adapter.
- Optional local Transformers runtime with checksum-verified model manifests.
- Optional vLLM adapter, subject to compatible dependencies and target-hardware validation.
- Bounded request/response handling, streaming, admission control, and readiness checks.

Not supplied or not certified:
- Trained RUSA-AI model weights or tokenizer artifacts.
- A RUSA-AI pretraining or post-training pipeline.
- Production GPU deployment profiles, capacity guarantees, or model-quality results.
- Evidence that any particular external model provider is owned or operated by RUSA-AI.

The runtime loads an externally supplied local artifact when configured. If no
local artifact path is configured, it uses the explicitly configured
`RUSA_MODEL_BASE_URL` OpenAI-compatible provider. These are distinct backends
and should be identified as such in operational reports.

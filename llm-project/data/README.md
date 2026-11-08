# Data pipeline module

This directory is intended to support:

- data collection and ingestion
- cleaning and normalization
- deduplication and quality filtering
- provenance metadata
- multilingual and multimodal preparation

**Status:** broader collection/ingestion and multimodal preparation remain planned. The initial agent SFT JSONL schema and policy-aware validator are implemented.

## Agent SFT data

Place curated JSONL files under `llm-project/data/curated/`:

- `train.jsonl`
- `eval.jsonl`

Each line must match `schemas/llm/agent-sft-example.schema.json`. Each example contains `id`, `agent_role`, `messages`, and the expected `response`. The first message is `system`; the final message is `user`. The response is the exact JSON envelope consumed by `AutonomousCodeWorker`.

The validator rejects unknown roles, tool calls outside the shared role allowlist, unsafe file paths, forbidden `run_check` profiles, duplicate examples, and train/eval leakage. Training requires at least 100 reviewed train examples by default. The small examples in docs are format illustrations, not a training corpus.

Data owners must document source/license/provenance, redact credentials and personal data, remove private repository content unless its use is authorized, and review prompt-injection or harmful tool-use examples. Do not commit private datasets or generated weights to Git.

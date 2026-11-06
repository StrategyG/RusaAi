# RUSA-AI

RUSA-AI is a model-serving and agent-task validation repository. The repository includes an architecture blueprint and a checkpoint-backed FastAPI inference implementation. **The blueprint is not a trained model checkpoint.**

## Current status

- **Inference implementation:** local Hugging Face-compatible checkpoint loading, OpenAI-compatible chat completions, SSE streaming, bounded tensor batching, GPU/CPU dtype selection, concurrency limits and readiness probes.
- **Security:** fail-closed production startup, API-key authentication, request-size limits, per-process rate limiting, request IDs and fail-closed A2A schema validation.
- **Benchmarking:** reproducible local model benchmark and concurrent HTTP load-test scripts are included.
- **Model artifact:** not included in this repository. A real, compatible RUSA-AI checkpoint and tokenizer must be supplied at deployment.
- **Production acceptance:** not complete until real-checkpoint inference, streaming, batch, load, security and deployment tests pass on the target hardware.

See [Production Readiness](docs/PRODUCTION_READINESS.md) for configuration, deployment, API examples and the complete acceptance checklist.

## Repository structure

```text
RusaAi/
├── .github/workflows/ci.yml
├── docs/PRODUCTION_READINESS.md
├── llm-project/
│   ├── configs/                 # Design and training configuration
│   ├── model/blueprint.py       # Architecture blueprint, not trained weights
│   └── serve/
│       ├── app.py               # FastAPI API, auth, validation and controls
│       └── inference.py         # Real local checkpoint runtime
├── schemas/a2a/task-envelope.schema.json
├── scripts/benchmark_inference.py
├── scripts/load_test.py
├── tests/test_app.py
├── .env.example
├── Dockerfile
├── requirements.txt
└── requirements-test.txt
```

## Quick start

Install the inference dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Mount or prepare a local Hugging Face-compatible causal-LM checkpoint, then configure:

```bash
export ENVIRONMENT=development
export RUSA_MODEL_PATH=/absolute/path/to/checkpoint
export RUSA_MODEL_NAME=rusa-ai
export RUSA_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000
```

The loader uses local checkpoint files only and disables remote model code. If no model is configured, inference endpoints return HTTP 503; there is no mock response path.

## API surface

| Endpoint | Purpose |
| --- | --- |
| `GET /health/live` | Liveness probe |
| `GET /health/ready` | Readiness probe; requires a loaded checkpoint |
| `POST /v1/chat/completions` | OpenAI-compatible completion and SSE streaming |
| `POST /v1/chat/completions/batch` | Bounded tensor-batched inference |
| `POST /v1/validate-task` | A2A envelope validation against the checked-in schema |

Protected routes accept `X-API-Key`. Production mode requires a key of at least 32 characters and a real local checkpoint path.

## Architecture blueprint

The YAML and Python model specifications describe a proposed decoder-only MoE architecture. They are not evidence of training completion, supported context length, actual weights, benchmark performance, or production deployment. Record checkpoint provenance, configuration and SHA-256 hashes before making model capability claims.

## Tests

```bash
python -m pip install -r requirements-test.txt
pytest -q
python -m compileall -q llm-project tests
```

These tests do not replace real-model and target-hardware acceptance. See [docs/PRODUCTION_READINESS.md](docs/PRODUCTION_READINESS.md).

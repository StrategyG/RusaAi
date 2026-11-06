# RUSA-AI

RUSA-AI is an AI model and agent platform under active development. This repository currently provides an API serving foundation, an A2A task-envelope contract, a local Transformers adapter, and guarded agent engineering components.

## Verified scope and status

The complete RUSA-AI product is NOT production-ready yet. This repository does not currently contain trained RUSA-AI model weights, a verified training pipeline, an enabled autonomous tool-calling loop, persistent production memory/RAG, or production benchmark evidence. The serving API can load a compatible local causal language model, but the model artifact must be supplied and evaluated separately.

## Serving foundation

- FastAPI serving API with mandatory API-key authentication that fails closed when misconfigured.
- Local-only Hugging Face Transformers loading. Model files must already exist on disk; remote code execution and implicit downloads are disabled.
- OpenAI-style non-streaming chat completion response with token usage.
- A2A task-envelope validation using the schema-declared JSON Schema draft and format checks.
- Separate liveness and readiness endpoints.
- Request IDs, response security headers, request-body limits including chunked bodies, model input-token/context limits, non-root container, and hardened Compose example.
- CI for lint, compile, tests, dependency audit, container build, and liveness smoke test.

Mock model responses are intentionally not supported. Inference returns HTTP 503 until a real local model is loaded. Streaming returns HTTP 501 until implemented.

## AICode agent foundations

- Hierarchical task orchestrator, validated contracts, state transitions, and fail-closed policy.
- Tenant-scoped SQLite memory for single-instance development and HMAC-chained local audit.
- GitHub gateway for allowlisted repository reads, feature-branch creation, work-branch writes, and pull-request creation. Protected files require approval; secret-bearing files cannot be read.
- Docker/gVisor sandbox adapter with digest-pinned images, rootless runtime checks, network isolation, resource limits, bounded output, and timeout cleanup.

Important: current LLM workers are report-only. They are not yet connected to the GitHub gateway or sandbox, so autonomous repository modification and code execution are not enabled. The Docker/gVisor adapter is defense-in-depth and does not replace a dedicated ephemeral VM/runner for hostile code.

## Repository layout

- llm-project/model/: model architecture blueprint; not trained weights.
- llm-project/serve/: API and local inference adapter.
- schemas/a2a/: A2A task envelope JSON Schema.
- rusa_agents/: policy, contracts, memory, audit, orchestrator, GitHub gateway, and sandbox adapters.
- tests/: API, agent foundation, gateway, and sandbox policy tests.
- docs/: architecture, integration, and production-readiness evidence ledger.

## Requirements

- Python 3.11 for the supported container and CI baseline.
- A compatible local causal language model directory with a tokenizer chat template.
- Adequate RAM/VRAM for the selected model.
- A random API key of at least 32 characters.
- Docker with rootless mode and gVisor runsc for the sandbox adapter.
- Docker Compose is optional.

## Local setup

1. Create and activate a Python virtual environment.
2. Install production dependencies with: `python -m pip install -r requirements.txt`
3. Install development dependencies with: `python -m pip install -r requirements-dev.txt`
4. Copy .env.example to .env, generate a real secret, and set RUSA_MODEL_PATH to a local model directory.
5. Run: `uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000`

The API deliberately reports not-ready and returns HTTP 503 for inference until a valid local model is loaded. It never returns fabricated model output.

## Docker Compose

Set RUSA_API_KEY to a random secret and RUSA_MODEL_DIR to the directory containing model files. Then run `docker compose up --build -d`. The model directory is mounted read-only at /models.

The default image uses CPU PyTorch wheels. GPU deployment requires a compatible CUDA base image and matching PyTorch build for the target driver/runtime. The container runs as UID/GID 10001, drops Linux capabilities, uses a read-only root filesystem under Compose, and exposes port 8000. Put it behind a TLS-terminating reverse proxy or gateway. Configure distributed rate limiting, request timeouts, and connection limits at that gateway.

## Health endpoints

- GET /health/live: process liveness; does not imply that a model is loaded.
- GET /health: backward-compatible liveness endpoint.
- GET /health/ready: returns HTTP 200 only when a model, API-key configuration, and task schema are available.

## API endpoints

- POST /v1/chat/completions: requires X-API-Key; accepts non-streaming chat requests.
- POST /v1/validate-task: requires X-API-Key; validates an A2A envelope. Missing or invalid schema configuration fails closed with HTTP 503.

## Quality gates

Run `ruff check llm-project/serve rusa_agents tests`, `python -m compileall -q llm-project rusa_agents tests`, `python -m pytest -q`, and `pip-audit -r requirements.txt`.

A green CI run validates the serving and tested software foundation. It does not prove model quality, model capacity, complete multi-agent autonomy, or production SLOs.

## Production acceptance checklist

- [ ] Licensed model artifact, revision, checksum, tokenizer, and chat template are recorded.
- [ ] Model correctness, p50/p95 latency, throughput, RAM/VRAM, and context limits are benchmarked on target hardware.
- [ ] A suitable high-throughput serving strategy is selected. The current Transformers adapter serializes generation in one process and is a baseline adapter.
- [ ] Streaming is implemented and tested if required.
- [ ] Distributed rate limiting, TLS, ingress body limits, timeouts, and network policy are configured at the gateway.
- [ ] Secret rotation, access audit, backup/restore, incident response, and rollback are tested.
- [ ] Worker tool-calling loop is integrated with the GitHub gateway and dedicated sandbox runner.
- [ ] Durable queue, retries, idempotency, cancellation, and crash recovery are implemented.
- [ ] Persistent memory/RAG includes tenant isolation, data provenance, retention, and deletion controls.
- [ ] Evaluation, safety, prompt-injection, tool authorization, and adversarial tests meet documented thresholds.
- [ ] Staging soak test, canary rollout, monitoring, alerting, and operational runbook are approved.

See docs/PRODUCTION_READINESS.md for the evidence ledger and remaining release gates.

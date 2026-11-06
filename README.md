# RUSA-AI

RUSA-AI is an AI model and agent platform under active development. This repository currently provides an API serving foundation, an A2A task-envelope contract, a local Transformers adapter, and engineering scaffolding.

## Verified scope and status

The complete RUSA-AI product is NOT production-ready yet. The repository now contains an enterprise AICode runtime foundation (hierarchical workers, GitHub tool gateway, PostgreSQL task queue, tenant-scoped task memory, audit trail, and authenticated task intake). It has not yet passed complete end-to-end production validation. The repository also does not contain trained RUSA-AI model weights, a verified training pipeline, or production model benchmark evidence. The model artifact must be supplied and evaluated separately.

## Serving foundation

- FastAPI serving API with mandatory API-key authentication that fails closed when misconfigured.
- Local-only Hugging Face Transformers loading. Model files must already exist on disk; remote code execution and implicit downloads are disabled.
- OpenAI-style non-streaming chat completion response with token usage.
- A2A task-envelope validation using the schema-declared JSON Schema draft and format checks.
- Separate liveness and readiness endpoints.
- Request IDs, response security headers, request content-length guard, non-root container, and hardened Compose example.
- CI for lint, compile, tests, dependency audit, container build, and liveness smoke test.

Mock model responses are intentionally not supported. Inference returns HTTP 503 until a real local model is loaded. Streaming returns HTTP 501 until implemented.

## Repository layout

- llm-project/model/: model architecture blueprint; not trained weights.
- llm-project/serve/: API and local inference adapter.
- schemas/a2a/: A2A task envelope JSON Schema.
- tests/: API, authentication, readiness, and schema tests.
- rusa_agents/: hierarchical coding-agent runtime, workers, GitHub gateway, queue, memory, and audit.\n- AI_CODE.md: normative AICode operating and security contract.\n- docs/: architecture, integration, and production-readiness documentation.

## Requirements

- Python 3.11 for the supported container and CI baseline.
- A compatible local causal language model directory with a tokenizer chat template.
- Adequate RAM/VRAM for the selected model.
- A random API key of at least 32 characters.
- Docker Compose is optional.

## Local setup

1. Create and activate a Python virtual environment.
2. Install production dependencies with: python -m pip install -r requirements.txt
3. Install development dependencies with: python -m pip install -r requirements-dev.txt
4. Copy .env.example to .env, generate a real secret, and set RUSA_MODEL_PATH to a local model directory.
5. Run: uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000

The API deliberately reports not-ready and returns HTTP 503 for inference until a valid local model is loaded. It never returns fabricated model output.

## Docker Compose

Set RUSA_API_KEY to a random secret and RUSA_MODEL_DIR to the directory containing model files. Then run docker compose up --build -d. The model directory is mounted read-only at /models.

The container runs as UID/GID 10001, drops Linux capabilities, uses a read-only root filesystem under Compose, and exposes port 8000. Put it behind a TLS-terminating reverse proxy or gateway. Configure distributed rate limiting, request timeouts, and connection limits at that gateway; an in-process limiter is not sufficient across replicas.

## Health endpoints

- GET /health/live: process liveness; does not imply that a model is loaded.
- GET /health: backward-compatible liveness endpoint.
- GET /health/ready: returns HTTP 200 only when a model, API-key configuration, and task schema are available.

## API endpoints

- POST /v1/chat/completions: requires X-API-Key; accepts non-streaming chat requests.
- POST /v1/validate-task: requires X-API-Key; validates an A2A envelope. Missing or invalid schema configuration fails closed with HTTP 503.\n- POST /v1/agent/tasks: requires X-API-Key and Idempotency-Key; validates and enqueues a task in PostgreSQL. Requires DATABASE_URL.

## Agent worker\n\nSet DATABASE_URL, RUSA_PROVIDER_BASE_URL, RUSA_PROVIDER_MODEL, RUSA_PROVIDER_API_KEY (when required), and RUSA_GITHUB_TOKEN in the secret-backed .env file. Start the API and PostgreSQL with docker compose up --build -d, then start the optional worker with docker compose --profile agent up -d rusa-agent-worker. Use a GitHub App or fine-grained token restricted to the target repository. The worker does not merge or deploy.\n\nSee AI_CODE.md for the agent contract and docs/PRODUCTION_READINESS.md for release gates.\n\n## Quality gates

Run ruff check llm-project/serve tests, python -m compileall -q llm-project tests, pytest -q, and pip-audit -r requirements.txt.

A green CI run validates the serving foundation. It does not prove model quality, model capacity, multi-agent correctness, or production SLOs.

## Production acceptance checklist

- [ ] Licensed model artifact, revision, checksum, tokenizer, and chat template are recorded.
- [ ] Model correctness, p50/p95 latency, throughput, RAM/VRAM, and context limits are benchmarked on target hardware.
- [ ] A suitable high-throughput serving strategy is selected. The current Transformers adapter serializes generation in one process and is a baseline adapter.
- [ ] Streaming is implemented and tested if required.
- [ ] Distributed rate limiting, TLS, ingress body limits, timeouts, and network policy are configured at the gateway.
- [ ] Secret rotation, access audit, backup/restore, incident response, and rollback are tested.
- [ ] Multi-agent orchestration, queueing, idempotency, retries, cancellation, and worker isolation are implemented and tested.
- [ ] Persistent memory/RAG includes tenant isolation, data provenance, retention, and deletion controls.
- [ ] Evaluation, safety, prompt-injection, tool authorization, and adversarial tests meet documented thresholds.
- [ ] Staging soak test, canary rollout, monitoring, alerting, and operational runbook are approved.

See docs/PRODUCTION_READINESS.md for the evidence ledger and remaining release gates.
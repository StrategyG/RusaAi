# RUSA-AI

RUSA-AI currently provides a FastAPI service for health checks and A2A task-envelope validation, plus a chat-completions API contract. The model architecture file is a blueprint, not an executable inference implementation.

> **Production scope:** The API and schema-validation service can be built and smoke-tested as a container. Actual model inference and autonomous-agent execution are **not production-ready on this branch**: no inference backend or agent worker runtime is connected. In production, chat requests fail closed with HTTP 503 until an inference backend is integrated. The development mock is explicitly opt-in and prohibited in production.

## Repository layout

```text
RusaAi/
├── .github/workflows/ci.yml
├── llm-project/
│   ├── configs/inference.yaml
│   ├── model/blueprint.py
│   └── serve/app.py
├── schemas/a2a/task-envelope.schema.json
├── tests/test_app.py
├── .dockerignore
├── .env.example
├── .gitignore
├── Dockerfile
├── pytest.ini
├── requirements-dev.txt
├── requirements.txt
└── README.md
```

## Requirements

- Python 3.12
- pip
- Docker Engine (only for container builds/runs)

## Local setup

```bash
git clone https://github.com/StrategyG/RusaAi.git
cd RusaAi

python3.12 -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

The runtime dependencies are intentionally limited to FastAPI, Uvicorn, Pydantic, and JSON Schema. PyTorch is not installed because this branch does not contain an executable model runtime. Test and lint tools are installed separately through `requirements-dev.txt`.

## Environment configuration

The application reads environment variables from the process. It does not automatically load `.env`.

```bash
cp .env.example .env
set -a
. ./.env
set +a
```

The example file enables the development-only mock response. To enable API-key authentication locally, set a key before starting the server:

```bash
export RUSA_API_KEY="$(openssl rand -hex 32)"
```

| Variable | Behavior |
| --- | --- |
| `ENVIRONMENT` | Defaults to `development`. Use `production` for production startup checks. |
| `RUSA_API_KEY` | Optional in development/test. If set, both POST API endpoints require the matching `X-API-Key`. In production it is mandatory and must contain at least 32 characters. |
| `RUSA_ENABLE_MOCK_INFERENCE` | Defaults to disabled. When set to `true`, returns a clearly labeled deterministic mock only outside production. Startup fails if enabled in production. |
| `LOG_LEVEL` | Python logging level; defaults to `INFO`. |

`GET /health` remains unauthenticated for liveness probes. Never commit `.env` or put real secrets in source control.

## Run the API locally

From the repository root:

```bash
uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000 --reload
```

The `--app-dir` option is required because `llm-project` contains a hyphen and is not a valid Python package identifier.

### Verify health

```bash
curl --fail http://127.0.0.1:8000/health
```

The liveness response reports `"status": "healthy"`. It does not claim that model inference is ready.

### Send an authenticated chat request

When `RUSA_API_KEY` is configured, use the same value in the `X-API-Key` header:

```bash
curl --include http://127.0.0.1:8000/v1/chat/completions \
  --header 'Content-Type: application/json' \
  --header "X-API-Key: ${RUSA_API_KEY}" \
  --data '{
    "model": "rusa-ai-v1",
    "messages": [{"role": "user", "content": "Hello RUSA"}]
  }'
```

With `RUSA_ENABLE_MOCK_INFERENCE=true` in development, this returns a response labeled `[DEVELOPMENT MOCK ONLY]`. With the mock disabled and no inference backend configured, the authenticated request returns HTTP 503. This is intentional: the service does not fabricate production model output.

### Validate an A2A task

`POST /v1/validate-task` validates the `envelope` against `schemas/a2a/task-envelope.schema.json`. The schema is loaded and checked during application startup; a missing or malformed schema prevents startup. Validation uses Draft 2020-12 and checks declared formats, including UUID and date-time. The endpoint returns at most ten actionable validation errors without echoing submitted field values.

When `RUSA_API_KEY` is set, this endpoint also requires `X-API-Key`.

## Docker

Build from the repository root:

```bash
docker build --pull -t rusa-ai:local .
```

The image runs as a non-root user and defaults to `ENVIRONMENT=production`. Supply a strong API key:

```bash
export RUSA_API_KEY="$(openssl rand -hex 32)"
docker run --rm --name rusa-ai \
  --publish 8000:8000 \
  --env ENVIRONMENT=production \
  --env RUSA_API_KEY="$RUSA_API_KEY" \
  rusa-ai:local
```

Check the container:

```bash
curl --fail http://127.0.0.1:8000/health
```

The container health check verifies API liveness only. There is no `docker-compose.yml` in this repository; use the Docker commands above.

## Tests and lint

Run from the repository root:

```bash
python -m pytest tests/ -v
python -m flake8 llm-project/serve tests --count --select=E9,F63,F7,F82 --show-source --statistics
```

The CI workflow at `.github/workflows/ci.yml` runs lint, compilation, pytest, container build, and a container smoke test. Lint errors are not ignored.

## API endpoints

| Method | Path | Authentication | Behavior |
| --- | --- | --- | --- |
| GET | `/health` | None | Liveness; returns `status: healthy` |
| POST | `/v1/chat/completions` | `X-API-Key` when configured | Development mock only when explicitly enabled; otherwise HTTP 503 until an inference backend is integrated |
| POST | `/v1/validate-task` | `X-API-Key` when configured | Validates an A2A envelope against the startup-loaded JSON Schema |

The request model accepts `stream`, but Server-Sent Events are not implemented. A request with `stream: true` returns HTTP 501 rather than silently returning a non-streaming response.

## Runtime and production readiness

| Component | Status on this branch |
| --- | --- |
| FastAPI/Uvicorn API | Implemented |
| API-key authentication | Implemented; mandatory in production |
| A2A schema validation | Implemented; startup-validated Draft 2020-12 schema with format checking |
| Container | Multi-stage build, non-root runtime, health check |
| CI | Lint, tests, container build and smoke test |
| Chat inference | **Not implemented**; no real inference backend is configured |
| Model | Blueprint only; `ProjectBlueprint.status` defaults to `PLANNED` |
| Agent workers/orchestration | Not implemented |
| SSE streaming | Not implemented |

This branch is suitable for deploying the API/schema-validation scaffold after CI passes. It is **not** a production model-serving or autonomous-agent release until a real inference backend and the agent runtime are implemented, integrated, and tested.

## License

MIT. See the repository license file for the full terms.

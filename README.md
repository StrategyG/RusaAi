# RUSA-AI

FastAPI serving API and Agent-to-Agent (A2A) task-envelope validation.

## Scope and runtime status

- `llm-project/serve/app.py`: FastAPI application and liveness endpoint.
- `llm-project/model/blueprint.py`: architecture blueprint only; it is not an inference runtime.
- `schemas/a2a/task-envelope.schema.json`: A2A JSON Schema Draft 2020-12 contract.
- `tests/test_app.py`: API and schema-validation tests.
- `requirements-serve.txt`: minimal serving dependencies.
- `requirements-test.txt`: serving and test dependencies.
- `requirements.txt`: full project/training dependencies.

**Important:** `POST /v1/chat/completions` currently returns a deterministic placeholder response. No trained model or inference backend is connected. Do not represent this branch as production model inference until a real backend has been configured and smoke-tested.

## Requirements

Python 3.10+; Docker is optional for containerized operation.

## Local setup

```bash
git clone https://github.com/StrategyG/RusaAi.git
cd RusaAi
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install --upgrade pip
python -m pip install -r requirements-test.txt
```

## Configuration

Set `RUSA_API_KEY` to enable API-key authentication for
`POST /v1/chat/completions`. When configured, provide it using the
`X-API-Key` header. If unset, the endpoint is unauthenticated; configure it
before exposing the service beyond a trusted local environment.

## Run

```bash
uvicorn app:app --app-dir llm-project/serve --host 0.0.0.0 --port 8000
```

`GET /health` is a liveness check. It returns HTTP 200 and
`{"status":"healthy"}` when the API process is alive. It does not assert
inference-backend readiness.

The A2A schema is loaded and checked at application startup. Missing, malformed,
or invalid schema configuration prevents the application from starting. Task
requests are validated against the cached Draft 2020-12 validator with format
checking enabled.

## API smoke tests

```bash
curl -i http://localhost:8000/health
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_RUSA_API_KEY' \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

## Quality checks

```bash
python -m pip install flake8
flake8 llm-project/serve tests --count --select=E9,F63,F7,F82 --show-source --statistics
python -m pytest -q tests
```

GitHub Actions runs lint, pytest, and Docker build independently for pushes
and pull requests targeting `main`. Fatal lint findings fail CI; no
failure-suppressing shell fallback is used.

## Docker

```bash
docker build -t rusa-ai:local .
docker run --rm -p 8000:8000 -e RUSA_API_KEY=replace-me rusa-ai:local
```

The serving image installs only `requirements-serve.txt`; training dependencies
such as PyTorch are intentionally excluded from the API runtime image.

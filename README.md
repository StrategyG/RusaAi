# RUSA-AI

FastAPI service and agent-task protocol utilities for RUSA-AI.

## Repository layout

- `llm-project/serve/app.py`: FastAPI application.
- `llm-project/model/blueprint.py`: model architecture blueprint (not an inference runtime).
- `schemas/a2a/task-envelope.schema.json`: A2A task envelope, JSON Schema 2020-12.
- `tests/test_app.py`: API and schema-validation tests.
- `.github/workflows/ci.yml`: required lint, pytest, and Docker build checks.

## Requirements

- Python 3.10+
- Docker (optional, for container build/run)

## Local setup

```bash
git clone https://github.com/StrategyG/RusaAi.git
cd RusaAi
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Configuration

Set `RUSA_API_KEY` to enable API-key authentication for `POST /v1/chat/completions`.
When unset, the endpoint does not require an API key. Send the configured key in
the `X-API-Key` header. Do not commit secrets to the repository.

## Run the API

The current serving entry point is a Python file under a directory containing a
hyphen, so use `--app-dir` rather than a dotted package path:

```bash
uvicorn app:app --app-dir llm-project/serve --host 0.0.0.0 --port 8000 --reload
```

Check liveness:

```bash
curl -i http://localhost:8000/health
```

The liveness contract returns HTTP 200 and JSON `status: "healthy"` when the
API process is running. It does not assert that an inference backend is ready.

Example authenticated chat request (when `RUSA_API_KEY` is configured):

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_RUSA_API_KEY' \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

## Tests and lint

```bash
python -m pip install flake8
flake8 llm-project/serve tests --count --select=E9,F63,F7,F82 --show-source --statistics
python -m pytest -q tests
```

CI runs these checks and builds the Docker image for pushes and pull requests
targeting `main`. Lint errors in the configured fatal categories fail the job;
there is intentionally no `|| true` suppression.

## Docker

```bash
docker build -t rusa-ai:local .
docker run --rm -p 8000:8000 rusa-ai:local
```

The container starts the same FastAPI entry point used locally.

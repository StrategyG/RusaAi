# RUSA-AI

RUSA-AI is an early-stage Python/FastAPI service with an OpenAI-shaped chat endpoint, A2A task-envelope validation, and a model architecture blueprint.

> **Implementation status:** The current `/v1/chat/completions` handler returns a deterministic placeholder response. It is not connected to an inference backend or trained model. The model blueprint is planning/specification code (`PLANNED`), not an executable model. The repository does not currently provide an agent worker/orchestration runtime. See [Runtime status](#runtime-status) before integrating this service.

## Repository layout

Paths below reflect files present on the default branch.

```text
RusaAi/
├── .github/
│   └── workflows/
│       └── ci.yml
├── llm-project/
│   ├── configs/
│   │   └── inference.yaml
│   ├── model/
│   │   └── blueprint.py
│   └── serve/
│       └── app.py
├── schemas/
│   └── a2a/
│       └── task-envelope.schema.json
├── tests/
│   └── test_app.py
├── Dockerfile
├── requirements.txt
└── README.md
```

## Requirements

- Python 3.10+
- pip
- Docker Engine (optional, for the container workflow)

## Local setup

Clone the repository and create an isolated environment:

```bash
git clone https://github.com/StrategyG/RusaAi.git
cd RusaAi

python3.10 -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

The dependency file includes FastAPI, Uvicorn, Pydantic, jsonschema, pytest, and PyTorch. PyTorch can make installation larger and slower than a minimal API-only environment.

## Environment configuration

The application reads `RUSA_API_KEY` and `ENVIRONMENT` directly from process environment variables. It does **not** automatically load a `.env` file, so export the variables or pass an env file to Docker.

Start from the example:

```bash
cp .env.example .env
```

Set a non-empty secret in `.env` for example:

```dotenv
RUSA_API_KEY=replace-with-a-long-random-secret
ENVIRONMENT=development
```

Load it into the current shell before starting Uvicorn:

```bash
set -a
. ./.env
set +a
```

### Authentication behavior

- If `RUSA_API_KEY` is **set to a non-empty value**, `POST /v1/chat/completions` requires the matching `X-API-Key` header. Missing or incorrect keys receive `401`.
- If `RUSA_API_KEY` is **unset or empty**, chat authentication is disabled. This is the current implementation's development-friendly default; set the variable in any environment where authenticated access is required.
- `GET /health` and `POST /v1/validate-task` are currently unauthenticated.

Do not commit `.env` or place real secrets in source control.

## Run the API locally

The serving module is stored under a directory named `llm-project` (which contains a hyphen and is not a valid Python package identifier). Run Uvicorn with the serving directory on its import path:

```bash
uvicorn app:app --app-dir llm-project/serve --host 0.0.0.0 --port 8000 --reload
```

The API listens on port 8000.

### Verify health

```bash
curl -i http://localhost:8000/health
```

A healthy response currently contains `"status": "healthy"`.

### Send an authenticated chat request

With `RUSA_API_KEY` configured as above:

```bash
curl -i http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: ${RUSA_API_KEY}" \
  -d '{
    "model": "rusa-ai-v1",
    "messages": [
      {"role": "user", "content": "Hello RUSA"}
    ]
  }'
```

The current handler returns a placeholder completion based on the last message. It does not run model inference.

## Docker

Build the image from the repository root:

```bash
docker build -t rusa-ai:local .
```

Run it with the environment file (recommended when authentication is enabled):

```bash
docker run --rm --name rusa-ai -p 8000:8000 --env-file .env rusa-ai:local
```

Verify the container:

```bash
curl -i http://localhost:8000/health
```

Stop a detached container with `docker stop rusa-ai`. There is currently no `docker-compose.yml`; use the `docker build` and `docker run` commands above.

## Tests

Tests are in the repository-root `tests/` directory (not `llm-project/tests/`).

```bash
python -m pytest tests/ -v
```

Run the same suite from the repository root after installing `requirements.txt`. The CI workflow is `.github/workflows/ci.yml`.

## API endpoints

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| GET | `/health` | None | Basic process health response |
| POST | `/v1/chat/completions` | `X-API-Key` when `RUSA_API_KEY` is configured | Placeholder chat completion |
| POST | `/v1/validate-task` | None | Validate an A2A envelope against the repository JSON Schema |

The chat request model accepts a `stream` field, but the current handler does not implement Server-Sent Events; responses are ordinary JSON.

## Runtime status

| Area | Current state |
| --- | --- |
| HTTP/API server | FastAPI application served by Uvicorn |
| Chat endpoint | Placeholder/mock response; no configured model backend |
| Model | Architecture blueprint only; `ProjectBlueprint.status` defaults to `PLANNED` |
| A2A | JSON Schema validation endpoint; this is not an agent execution engine |
| Agent workers/orchestration | Not implemented in the current serving module |
| Streaming | Not implemented; `stream: true` does not produce SSE |
| Health | Basic API process health; it does not establish model-backend readiness |

Do not describe this branch as providing production model inference or a running autonomous-agent system until those components are implemented and verified.

## License

MIT. See the repository license file for the full terms.

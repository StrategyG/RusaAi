# RUSA-AI

RUSA-AI currently contains an API foundation, an A2A task-envelope schema,
and an independent-model blueprint. It is **not production-ready**: there are
no trained weights, working inference backend, hierarchical agent runtime,
durable scheduler, tenant isolation layer, or production deployment evidence.

## Available functionality

- FastAPI liveness endpoint and authenticated task-envelope validation.
- JSON Schema draft selection and UUID/date-time/URI format checking.
- Fail-closed authentication and unavailable-schema handling.
- Model architecture configuration and blueprint, not a trained model.
- CI tests, syntax/lint checks, and a Docker build job. No security scan job yet.

## Local setup

Use Python 3.10 or later:

```bash
git clone https://github.com/StrategyG/RusaAi.git
cd RusaAi
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export RUSA_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn llm-project.serve.app:app --host 127.0.0.1 --port 8000
```

`.env.example` documents configuration; the application does not auto-load `.env`.
Supply credentials through the environment or a deployment secret manager.
Both protected endpoints return 503 if authentication is unconfigured and 401
if the supplied key is absent or wrong. The current key is service-wide; it is
not a tenant identity or an enterprise authorization system.

## API contract

| Endpoint | Behavior |
|---|---|
| `GET /health` | 200 when API process is alive; does not imply inference readiness |
| `GET /ready` | 503 until a real inference runtime is implemented and checked |
| `POST /v1/chat/completions` | Authenticated, validates input, then returns 503; no simulated success |
| `POST /v1/validate-task` | Authenticated structural validation; `valid: false` for invalid envelopes; 503 if schema unavailable |

Schema validation does not authorize actions described in an envelope. A future
Policy Engine must derive permissions from trusted identity and server policy,
never from a caller's `allowed_actions` claims.

## Verification

```bash
python -m pytest -q
python -m flake8 . --select=E9,F63,F7,F82 --show-source --statistics
```

Container build and liveness smoke test:

```bash
docker build -t rusa-ai:foundation .
docker run --rm -p 127.0.0.1:8000:8000 -e RUSA_API_KEY rusa-ai:foundation
```

The Docker health check measures liveness only. Model-dependent traffic must
use readiness, which intentionally remains unavailable.

## Architecture and delivery gates

See [hierarchical agent architecture](docs/architecture/hierarchical-agent.md)
and [baseline and validation evidence](docs/architecture/foundation-evidence.md).
Use task branches and PRs. Production deployment remains subject to the
repository's explicit approval policy.

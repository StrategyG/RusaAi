# RUSA-AI

This branch provides a FastAPI service, an A2A task-envelope schema, and a model
architecture blueprint. The chat endpoint returns a simulated response based on
the last message. This branch has no trained model weights, model inference
backend, or executable agent runtime. Task validation checks an envelope against
the bundled schema. It does not queue or execute tasks.

## Repository layout

| Path | Purpose |
| --- | --- |
| [llm-project/serve/app.py](llm-project/serve/app.py) | FastAPI entry point |
| [llm-project/model/blueprint.py](llm-project/model/blueprint.py) | Model architecture blueprint |
| [llm-project/configs/](llm-project/configs/) | Model, training, and inference configuration drafts |
| [schemas/a2a/task-envelope.schema.json](schemas/a2a/task-envelope.schema.json) | A2A task-envelope schema |
| [tests/](tests/) | API test suite |
| [requirements.txt](requirements.txt) | Python dependencies for the service and tests |
| [Dockerfile](Dockerfile) | Container build and startup configuration |

## Run locally

The commands below use Bash, Python 3.10 or later, and curl.
Run them from the repository root after cloning this branch.

```bash
git clone --branch docs/api-quickstart https://github.com/StrategyG/RusaAi.git
cd RusaAi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The application reads variables from the process environment. Export them in
your shell before starting the API.

| Variable | Behavior |
| --- | --- |
| `ENVIRONMENT` | Defaults to `production` when unset or blank for the startup check. The check ignores case and surrounding whitespace. |
| `RUSA_API_KEY` | Production startup requires a non-blank value. Other environments can start without it, but both protected routes then return HTTP 401. |

Both POST endpoints require an exact match in the `X-API-Key` header. They
return HTTP 401 for a missing or incorrect key. `GET /health` is public and
reports process liveness.

Set a local test key and start the service in the background so the smoke
checks can use the same shell and key. Use a unique secret outside local testing.

```bash
export ENVIRONMENT=production
export RUSA_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000 &
RUSA_API_PID=$!
```

## Smoke check

```bash
curl --fail --show-error --silent --retry 5 --retry-connrefused http://127.0.0.1:8000/health
curl --fail --show-error --silent http://127.0.0.1:8000/v1/chat/completions \
  -H "X-API-Key: ${RUSA_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

Both calls return HTTP 200. The first reports `"status":"healthy"`. The
second returns a simulated reply in `choices[0].message.content`.
To check task-envelope structure, send a JSON object with an `envelope` field
to `POST /v1/validate-task` with the same header.

Stop the local API with `kill "$RUSA_API_PID"`.

## Run tests

From the repository root with the virtual environment active:

```bash
python -m pytest -q tests/
```

The tests configure their own API keys and start in-process clients.

## Run with Docker

From the repository root, with Docker running and host port 8000 free, export
`RUSA_API_KEY` as shown above. Build the image and run it with the key passed
through from the current shell.

```bash
docker build -t rusa-ai:local .
docker run --rm -d --name rusa-ai-local -p 127.0.0.1:8000:8000 \
  -e RUSA_API_KEY rusa-ai:local
```

Run the same smoke checks, then stop the container with
`docker stop rusa-ai-local`. The Dockerfile runs Uvicorn as a non-root user
and probes `/health` inside the container.

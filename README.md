# RUSA-AI

RUSA-AI is an AI model and agent platform under active development. The repository separates the model-serving foundation from the autonomous agent control plane.

## Current verified boundary
The complete product is NOT production-ready until the release gates in docs/PRODUCTION_READINESS.md and docs/PRODUCTION_READINESS_AGENT.md have objective evidence. No trained RUSA-AI foundation-model weights are included. Model inference requires a compatible local model artifact. The enterprise agent runtime requires a configured model-planning endpoint, tenant API keys, and GitHub credentials.

## Architecture
- Model platform: local Transformers inference adapter and model blueprint.
- Agent platform: hierarchical supervisor, typed task contracts, persistent task store, bounded worker process, policy engine, tool registry, approval workflow, and audit events.
- RAG/vector memory and sandboxed code execution are planned integrations and are not claimed as implemented.

See docs/enterprise-agent-architecture.md for topology, worker communication, memory, tools, orchestration, and evaluation design.

## Serving API
Install production dependencies with python -m pip install -r requirements.txt. Configure RUSA_API_KEY and a local RUSA_MODEL_PATH. Run:
uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000

## Agent runtime
Install agent dependencies with python -m pip install -r requirements-agent.txt.
Configure:
- RUSA_DATABASE_URL (PostgreSQL recommended; SQLite is local single-process development only)
- RUSA_TENANT_API_KEYS as JSON mapping tenant IDs to random secrets of at least 32 characters
- RUSA_AGENT_MODEL_BASE_URL and RUSA_AGENT_MODEL_NAME for an OpenAI-compatible model endpoint
- GITHUB_TOKEN with least-privilege repository access

Run API:
uvicorn rusa_agents.api:app --host 127.0.0.1 --port 8010

Run worker separately:
python -m rusa_agents.worker_cli

Submit tasks using X-API-Key and Idempotency-Key headers at POST /v1/agent/tasks. Query GET /v1/agent/tasks/{task_id}. Human approvals and cancellation have dedicated endpoints. Never expose API/worker directly to the public internet; use a TLS gateway and distributed rate limiting.

## Quality gates
Install agent dependencies and run:
ruff check rusa_agents tests
python -m compileall -q rusa_agents tests
pytest -q

The agent CI workflow executes these checks. A green unit-test run does not establish model quality, tenant-isolation certification, or production SLO compliance.

## Security
- Never commit credentials, model weights, or private datasets.
- Do not expose arbitrary shell execution to model output.
- Keep merge, deploy, release, secret/permission changes, and destructive actions under human approval.
- Treat repository content and tool output as untrusted data.
- Use PostgreSQL, schema migrations, immutable audit export, monitoring, backup/restore, and security testing before production rollout.

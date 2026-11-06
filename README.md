# RUSA-AI

RUSA-AI is an AI and autonomous-agent platform under active development. The repository currently contains a FastAPI inference gateway, a tenant-scoped coding-agent runtime, PostgreSQL task/lease controls, and A2A task validation.

**Model status:** `llm-project/model/` and `llm-project/configs/model.yaml` describe a planned architecture only. This repository does not contain trained RUSA-AI weights/checkpoints, a verified tokenizer artifact, or a standalone local inference runtime. Chat completions are forwarded to an externally configured OpenAI-compatible HTTPS endpoint (`RUSA_MODEL_BASE_URL`); the API fails closed when no inference backend is configured.

**Production status:** RUSA-AI is not production-certified. A passing CI run validates only the exact commit and automated checks it executed. Production release still requires approved model/provider artifacts, staging load and soak tests, adversarial tenant-isolation review, backup/restore and rollback drills, monitoring, TLS, secret management, and operator sign-off.

---

## Key Features & Architecture

- **Model architecture blueprint**: Decoder-only Transformer/MoE design; planned only and not a trained or verified model.
- **Inference gateway**: FastAPI OpenAI-compatible API that forwards requests to a configured external model endpoint; it does not serve local RUSA-AI weights.
- **Agent runtime**: Tenant-scoped task orchestration, durable PostgreSQL queue, worker lease controls, retry/recovery, and sandboxed tool execution.
- **A2A task validation**: Versioned JSON Schema validation for agent task envelopes.
- **Security and reliability controls**: API-key authentication, RBAC, tenant-aware data access, rate limiting, non-root containers, and audit events; production effectiveness still requires deployment-level verification.
- **CI quality gates**: Lint, tests, static security analysis, container builds, and vulnerability scanning.

---

## Repository Structure

```
RusaAi/
├── .github/
│   ├── workflows/
│   │   └── ci.yml               # Automated CI workflow (linting, tests, docker)
│   ├── ISSUE_TEMPLATE/           # GitHub issue templates for agent tasks
│   └── copilot-instructions.md   # AI collaboration & safety guidelines
├── docs/                         # Architecture & A2A integration documentation
├── llm-project/
│   ├── configs/                 # YAML configurations (model, training, inference)
│   │   ├── model.yaml
│   │   ├── training.yaml
│   │   └── inference.yaml
│   ├── model/
│   │   └── blueprint.py         # MoE model architecture specifications & blueprint
│   ├── serve/
│   │   └── app.py               # Production FastAPI serving engine
│   ├── tests/
│   │   ├── test_serve.py        # API unit & integration tests
│   │   └── test_blueprint.py    # Model blueprint unit tests
│   └── __init__.py
├── schemas/
│   └── a2a/
│       └── task-envelope.schema.json # Task payload validation schema
├── .env.example                  # Environment configuration template
├── Dockerfile                    # Production multi-stage Docker container
├── docker-compose.yml            # Container orchestration config
├── pyproject.toml                # Project metadata & build tool configuration
├── requirements.txt              # Production Python dependencies
└── README.md                     # Comprehensive project documentation
```

---

## Quick Start & Local Setup

### 1. Prerequisites
- Python 3.10 or higher
- Docker & Docker Compose (optional for containerized deployment)

### 2. Environment Setup
```bash
# Clone the repository
git clone StrategyG/RusaAi
cd RusaAi

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Variables
Copy `.env.example` to `.env` and set your secrets:
```bash
cp .env.example .env
```

---

## Running the Application

### Running Locally with Uvicorn
```bash
uvicorn app:app --app-dir llm-project/serve --host 0.0.0.0 --port 8000 --reload
```

### Running with Docker & Docker Compose
```bash
# Build and start container
docker-compose up --build -d

# Verify server status
curl localhost:8000/health
```

---

## Running Tests

Execute the automated test suite using pytest:
```bash
pytest -q
```

---

## API Endpoints

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/health` | `GET` | Health check and system metadata |
| `/v1/chat/completions` | `POST` | OpenAI-compatible chat completion inference endpoint |
| `/v1/validate-task` | `POST` | Validates agent task envelope payload against JSON Schema |

---

## Security & Governance

- **API Authorization**: Production uses tenant-bound, hashed API-key principals and role checks. Do not use development-only legacy key mappings in production.
- **Non-Root Execution**: Production API container runs under a dedicated non-root user (UID 10001).
- **Input Validation**: Pydantic request validation, bounded request bodies, and fail-closed A2A JSON Schema validation.
- **Deployment boundary**: Application controls must be combined with TLS ingress, secret-manager integration, database hardening, network/egress controls, and verified tenant-isolation tests.

---

## AICode Enterprise Coding Agent

RUSA-AI includes an autonomous coding subsystem that operates within tenant-authorized repositories and isolated task branches. It uses a hierarchical master orchestrator with Analyst, Architect, Developer, Debugger, Tester, and Security workers.

### Operating boundaries

- Repository reads and ordinary source/test/documentation edits run in a task-specific feature worktree.
- Model actions are restricted to named tools; arbitrary shell execution is not exposed.
- Validation runs in a disposable, network-disabled Docker sandbox using a bounded source snapshot.
- A successful implementation is pushed only to its generated feature branch and opened as a draft pull request.
- Merge, deployment, release, secrets, permissions, migrations, CI workflows, and protected deployment configuration remain human-controlled.
- Completion requires real pytest, Ruff, Bandit, compile, and required GitHub Actions evidence. Missing or failed evidence cannot be reported as success.

### AICode API

All task endpoints require `X-API-Key` and map the key to a tenant.

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/aicode/tasks` | POST | Enqueue a repository-scoped coding task |
| `/v1/aicode/tasks/{task_id}` | GET | Read task status within the authenticated tenant |
| `/v1/aicode/tasks/{task_id}/retry` | POST | Explicitly retry a failed or approval-paused task |
| `/v1/aicode/tasks/{task_id}` | DELETE | Cancel a cancellable task |
| `/health/ready` | GET | Verify model and database readiness |
| `/metrics` | GET | Authenticated Prometheus API metrics |

Example task request:

```json
{
  "objective": "Add a regression test for the health endpoint",
  "repository": {
    "full_name": "StrategyG/RusaAi",
    "base_branch": "main"
  },
  "acceptance_criteria": [
    "The regression test passes",
    "Ruff and Bandit checks pass"
  ]
}
```

### Production deployment

The implementation and production runbook are in [docs/aicode-agent-enterprise.md](docs/aicode-agent-enterprise.md) and [docs/aicode-production-runbook.md](docs/aicode-production-runbook.md).

Production deployment requires managed PostgreSQL, tenant repository allowlists, short-lived or fine-grained GitHub credentials, a dedicated rootless Docker daemon, immutable sandbox image digests, an approved OpenAI-compatible model endpoint, TLS ingress, monitoring, backups, and restore drills.

**Important:** Repository code and CI success alone do not certify the deployment. Complete the release gates in the runbook and record operator approval before enabling production tenants.

---

## License
No `LICENSE` file is currently present in this repository. Licensing is therefore unresolved; confirm the intended terms with the repository owner and commit the corresponding license file before redistribution.

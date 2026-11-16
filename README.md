# RusaAi

RusaAi is an AI and autonomous-agent platform under active development. The repository currently contains a FastAPI inference gateway, tenant-scoped coding-agent runtime, PostgreSQL task/lease controls, knowledge retrieval, MCP integration, and A2A task validation.

**Architecture v2.0 baseline:** The target neural core is RusaAi-Edge-1.54B (model ID: rusaai-edge-1.54b), a dense Pre-Norm decoder-only Transformer with 32 layers, hidden size 2,048, 16 query heads / 4 KV heads, SwiGLU intermediate size 5,632, 48,000 vocabulary entries, tied embeddings, RoPE, no MoE, and a reference count of 1,541,277,696 parameters.

**Model status:** The v2.0 core remains DESIGN_ONLY. The required non-Qwen foundation checkpoint and tokenizer are not selected; weights and GGUF artifacts are not built; training has not started; and no CPU benchmark has been run. The checked-in Qwen3-4B QLoRA setup is a separate legacy AICode worker-contract experiment, not the RusaAi-Edge core model.

**Inference status:** RusaModels owns GGUF artifacts, llama.cpp/embedding runtimes, model residency and the Model Server API. RusaAi is the authenticated Control Plane gateway. Worker requests pass through the RusaAi API and its PostgreSQL-backed FIFO admission scheduler before reaching the RusaModels Model Server. The CPU baseline is one active generation, with three waiting requests on the 16-GB profile and one on the 8-GB profile. The non-Qwen RusaAi-Edge checkpoint remains unselected.

**Production status:** RusaAi is not production-certified. A passing CI run validates only the exact commit and automated checks it executed. Production release still requires an approved model/provider artifact, staging load and soak tests, adversarial tenant-isolation review, backup/restore and rollback drills, monitoring, TLS, secret management, and operator sign-off. See docs/architecture/IMPLEMENTATION_ALIGNMENT.md for the implementation map and remaining gaps.

---

## Key Features & Architecture

- **Neural core blueprint**: Dense decoder-only Transformer matching the RusaAi-Edge-1.54B v2.0 design target; no model artifact or training result is claimed.
- **Inference gateway**: FastAPI OpenAI-compatible Control Plane gateway with durable PostgreSQL-backed FIFO admission; inference is delegated to the authenticated RusaModels Model Server.
- **Knowledge retrieval**: Optional tenant-scoped PostgreSQL/pgvector ingestion and hybrid retrieval, with offline Recall@K, MRR, and NDCG@K evaluation.
- **Knowledge MCP**: Separate read-only, bearer-authenticated MCP service for tenant-scoped search and document access.
- **Agent runtime**: Tenant-scoped task orchestration, durable PostgreSQL queue, worker lease controls, retry/recovery, and sandboxed tool execution.
- **Legacy agent fine-tuning**: Separate Qwen3-4B AICode worker-contract QLoRA/SFT experiment; it is not the non-Qwen RusaAi-Edge neural-core checkpoint.
- **A2A task validation**: Versioned JSON Schema validation for agent task envelopes.
- **Security and reliability controls**: API-key authentication, RBAC, tenant-aware data access, rate limiting, non-root containers, and audit events; production effectiveness still requires deployment-level verification.
- **CI quality gates**: Lint, tests, static security analysis, container builds, and vulnerability scanning.

---

## CPU-first GGUF inference (no GPU)

For CPU-only deployment, run the model artifact, llama.cpp runtime and embedding service in RusaModels. RusaAi must not mount GGUF weights or start a model runtime. Configure the RusaAi gateway to call the authenticated RusaModels Model Server at `/openai/v1`. The current RusaModels CPU profile uses `rusa-general-4b` as a historical Qwen3-4B smoke-test alias; it is not RusaAi-Edge. The v2.0 non-Qwen foundation checkpoint and tokenizer remain unselected.

1. Start the RusaModels CPU Model Server using its [CPU inference runbook](https://github.com/StrategyG/RusaModels/blob/main/docs/CPU_INFERENCE.md). Register and verify the GGUF artifact there; keep weights outside Git and mount them read-only.
2. Create a dedicated RusaModels API principal with role `inference` only. Configure its token digest in `RUSAMODELS_API_PRINCIPALS` and keep the raw token in the secret manager.
3. Copy `.env.example` to `.env`, merge `.env.cpu.example`, and set `RUSA_MODEL_BASE_URL` to the reachable RusaModels `/openai/v1` endpoint, `RUSA_MODEL_NAME` to the exact configured model ID, and `RUSA_MODEL_API_KEY` to the dedicated inference token. For the current smoke profile, the model ID is `rusa-general-4b`.
4. For an 8-GB device, merge `.env.cpu-8gb.example` and the Control Plane resource overlay. The current RusaModels CPU profile is for 16-GB Codespaces; an 8-GB combined Model Plane profile is not yet validated.
5. Start the RusaAi Control Plane with `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build -d`.
6. Check the RusaModels authenticated `/openai/v1/models` endpoint and RusaAi `/health/ready`, then send a chat request through the RusaAi API. RusaAi never connects directly to llama.cpp.

See [docs/CPU_INFERENCE_OPERATIONS.md](docs/CPU_INFERENCE_OPERATIONS.md) for model sizing, CPU tuning, smoke tests, and operational limits. The optional GPTQ and Transformers/vLLM paths remain separate experimental workflows; they are not the v2.0 CPU deployment baseline. The supplied HTTP Compose override is for development/staging; production requires the HTTPS model-endpoint boundary described in the runbook.

## Optional local model runtime

The legacy local Transformers/vLLM adapter remains available for isolated development experiments only. Production RusaAi Control Plane configuration rejects `RUSA_LOCAL_MODEL_PATH` and `RUSA_MODEL_PATH`; production inference must use the authenticated RusaModels Model Server. Model manifests, GGUF verification, model weights and runtime dependencies are owned by RusaModels. See `docs/INFERENCE_OPERATIONS.md`.

## Agent model fine-tuning

The existing Qwen3-4B SFT target is the six-role AICode worker JSON action envelope only. It is retained as a legacy, separate worker-contract experiment and must not be promoted as the RusaAi-Edge core model; v2.0 requires a non-Qwen foundation checkpoint. Use a dedicated CUDA training host and install `requirements-train.txt`; training dependencies and model weights are not part of the API image. Curated datasets belong under the ignored `llm-project/data/curated/` directory and must pass the role/tool-policy validator. Run `python llm-project/train/validate_dataset.py`, then `python llm-project/train/train_sft.py`. Evaluate the adapter, merge it, and create a pinned model manifest before configuring `RUSA_LOCAL_MODEL_PATH`. See `docs/FINETUNING_OPERATIONS.md` for hardware estimates and promotion gates. The pipeline is implemented; no fine-tuned weights or production certification are claimed.

## Optional RAG and MCP

The tenant-scoped knowledge service and read-only MCP endpoint are independently
deployable. Install `requirements-rag-mcp.txt`, apply
`migrations/007_rag_knowledge_pgvector.sql` to a PostgreSQL database with
pgvector, and configure restricted database credentials and per-tenant MCP
bearer-token hashes. The `rusa-knowledge` CLI supports ingestion, readiness,
and retrieval evaluation. See `docs/RAG_MCP_OPERATIONS.md`. See [RusaModels chat/embedding integration](docs/RUSAMODELS-INTEGRATION.md) for the authenticated model gateway and non-destructive 384d vector migration, and [the Colab smoke-test notebook](notebooks/RusaModels_Codespaces_Colab_Smoke_Test.ipynb) for Codespaces connectivity checks.

## Repository Structure

```
RusaAi/
├── Rusaai/                       # Control Plane ownership boundary
├── RusaModels/                   # Model Plane ownership boundary
├── rusa_agents/                  # Orchestrator, workers, policy, queue, tools
├── rusa_knowledge/               # Tenant-scoped knowledge and retrieval
├── rusa_mcp/                     # Read-only MCP integration
├── shared/
│   ├── contracts/                # Agent, inference, and artifact contracts
│   └── schemas/                  # Shared schema migration boundary
├── llm-project/
│   ├── configs/                  # Model, inference, and legacy SFT configs
│   ├── model/                    # RusaAi-Edge dense Transformer blueprint
│   └── serve/                    # FastAPI serving and inference gateway
├── schemas/                      # Existing A2A and model artifact schemas
├── migrations/                   # Durable task, tenant, audit, and RAG schemas
├── scripts/                      # Control Plane operations utilities
├── tests/                        # Repository-wide regression suite
├── deployment/profiles/          # rusa-cpu-16 and rusa-edge-8 CPU profiles
├── docs/
│   ├── architecture/             # v2.0 implementation alignment
│   └── adr/                      # Architecture decision records
├── docker-compose.yml
├── docker-compose.cpu.yml        # CPU-first gateway to the external RusaModels Model Plane
├── pyproject.toml
└── README.md
```

---

## Quick Start & Local Setup

### 1. Prerequisites
- Python 3.10 or higher
- Docker & Docker Compose (optional for containerized deployment)

### 2. Environment Setup
```bash
# Clone the repository
git clone https://github.com/StrategyG/RusaAi.git
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

## Web Control Center

A tenant-scoped Next.js Control Center is available in the `web/` directory. It connects to the FastAPI application through server-side route handlers, keeps tenant API keys in encrypted HttpOnly sessions, and provides task intake/history, task review visibility, chat inference, agent capability views, artifact references, and service health.

Set `RUSA_WEB_SESSION_SECRET` to a random value of at least 32 characters. Start the local stack with `docker compose up --build`, then open http://localhost:3000. Sign in using a tenant API key with the required task permissions. See `docs/UI_CONTROL_CENTER.md` for the API mapping and current capability boundaries.

The web interface does not add approve/reject actions because the current backend does not expose an approval-decision endpoint. The eight-stage DevOps lifecycle is labeled as an architecture target until the corresponding runtime agents and APIs are implemented.

## License
No `LICENSE` file is currently present in this repository. Licensing is therefore unresolved; confirm the intended terms with the repository owner and commit the corresponding license file before redistribution.


---

## HYBRIDA AI — Android Local Edition

The repository now includes an Android-first local assistant under mobile-local/. It runs a small Qwen3 0.6B GGUF model through llama.cpp, stores chat history and documents in local SQLite, provides local hybrid RAG (FTS5 plus TF-IDF sparse-vector retrieval), and exposes a bounded local MCP tool server. The chat interface is an installable mobile PWA hosted by a loopback-only Termux service; it is not yet a signed native APK.

For the no-root Android/Termux setup, model download, service startup, local API routes, and device limitations, see docs/HYBRIDA_ANDROID.md. The existing tenant-scoped PostgreSQL RAG/MCP and production AICode services remain separate and unchanged.

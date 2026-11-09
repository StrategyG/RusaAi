# RUSA-AI Predictive DevOps Intelligence Engine (PDIE)

## Status and compatibility

PDIE is an optional subsystem. It is disabled by default and does not change the existing AICode queue, worker contracts, A2A task-envelope schema, inference endpoint, or RAG collection contract.

The implementation uses a separate `rusa_devops_jobs` queue because the existing `aicode_jobs` worker expects a `CodingTask` payload. Both queues share the existing PostgreSQL service and lease/retry design but are consumed by different worker processes.

Current release boundaries:

- Implemented: tenant-scoped DevOps queue, synthetic telemetry generator, Prometheus read adapter, Prophet + ARIMA training experiment, Bayesian anomaly scoring, cost-sensitive capacity recommendations, model-run metadata, incident persistence, action proposal review, read-only MCP tools, A2A envelope adapter, DevOps SFT dataset validator/trainer/evaluator, and API/UI contracts.
- Not enabled by default: DevOps API and worker.
- Not implemented: production infrastructure mutation, Docker/Kubernetes scale execution, automated model promotion, and a trained DevOps LLM adapter. Approval/rejection decisions update proposal state and audit history only; they never execute infrastructure operations.
- No model weights or real telemetry are bundled. Synthetic telemetry is for smoke tests and benchmark reproducibility only.

## Architecture

```text
Docker / Kubernetes / local hosts / cloud exporters
                  |
        Prometheus / OpenTelemetry Collector
                  |
          Telemetry Ingestion
                  |
       PostgreSQL rusa_devops_telemetry
                  |
     +------------+-------------+
     |                          |
 Prophet trend/seasonality   Bayesian anomaly
     |                          |
 ARIMA OOF residuals         Incident records
     |                          |
 Validation calibration     RAG incident context
     +------------+-------------+
                  |
        Cost-sensitive Decision Engine
                  |
       Recommendation / Action Proposal
                  |
      Policy + Human Approval boundary
                  |
        MCP / A2A / Control Center
```

The forecasting implementation uses expanding-window out-of-fold Prophet residuals for ARIMA training. This avoids using optimistic in-sample Prophet residuals as the ARIMA training target. The chronological experiment reserves separate train, validation, and test windows. Validation is used to calibrate the asymmetric offset; test remains untouched until final evaluation.

The anomaly module is Bayesian likelihood scoring with a robust median/MAD baseline. It is not labelled Game Theory. A game-theoretic adversarial response model requires explicit actors, strategies, and payoff functions and is a separate future module.

The capacity decision engine minimizes a cost-sensitive objective and returns a recommendation. The displayed cost-derived quantile target is not itself a fitted probabilistic quantile forecast; production autoscaling must add interval/quantile calibration, hysteresis, cooldown, min/max bounds, and workload-specific cost calibration.

## Infrastructure adapters

PDIE uses Prometheus as the normalized observation interface:

- Docker: cAdvisor and Docker-compatible Prometheus exporters.
- Kubernetes: kube-state-metrics, cAdvisor, and node-exporter.
- Local servers: node-exporter and application exporters.
- Cloud: the relevant cloud exporter or OpenTelemetry Collector pipeline.

Use stable labels such as `resource_id` and metric names. The Prometheus adapter only accepts a validated metric identifier and resource ID; it does not accept arbitrary PromQL from the model. Credentials are passed through `RUSA_DEVOPS_PROMETHEUS_TOKEN` and must be injected by a secret manager.

## Database migration and runtime roles

Apply migrations 001-008 as documented by the existing repository, then apply:

```bash
psql "$RUSA_API_DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/009_rusa_devops_intelligence.sql
```

Migration 009 creates:

- `rusa_devops_jobs`: independent durable queue, idempotency key, attempts, lease owner/token/expiry, result.
- `rusa_devops_telemetry`: tenant-scoped metric observations.
- `rusa_devops_model_runs`: training metrics, artifact URI, SHA-256 and split sizes.
- `rusa_devops_incidents`: Bayesian anomaly assessments.
- `rusa_devops_action_proposals`: proposals requiring human approval.
- `rusa_devops_audit_events`: append-only policy/audit events.

The migration assumes `rusa_api_runtime` and `rusa_worker_runtime` were created by the existing tenant-RLS migration. Use separate login members:

- API: `RUSA_API_DATABASE_URL` must connect as the API login that is a member of `rusa_api_runtime`.
- Worker: `RUSA_WORKER_DATABASE_URL` must connect as the worker login that is a member of `rusa_worker_runtime`.
- MCP read tools: `RUSA_DEVOPS_DATABASE_URL` may use a tenant-scoped read-capable API runtime login.

Do not use the schema-owner login or a superuser as a runtime account. The default local Compose DSN is for the existing local development setup; after enabling forced RLS for PDIE, configure the two runtime login URLs before starting the API and worker. The worker queue policy permits cross-tenant claiming only; the worker must set the claimed tenant before accessing telemetry or writing any tenant-owned result.

## Configuration

PDIE is disabled unless explicitly enabled:

```dotenv
RUSA_DEVOPS_ENABLED=false
RUSA_DEVOPS_AUTONOMY_MODE=controlled
RUSA_DEVOPS_MAX_TRAIN_ROWS=20000
RUSA_DEVOPS_VALIDATION_HORIZON=120
RUSA_DEVOPS_TEST_HORIZON=120
RUSA_DEVOPS_FORECAST_HORIZON=30
RUSA_DEVOPS_OOF_FOLDS=3
RUSA_DEVOPS_DOWNTIME_COST=100
RUSA_DEVOPS_OVERPROVISION_COST=1
RUSA_DEVOPS_SCALE_BUFFER=1.15
RUSA_DEVOPS_ANOMALY_PRIOR=0.01
RUSA_DEVOPS_ANOMALY_THRESHOLD=0.90
RUSA_DEVOPS_MAX_ATTEMPTS=3
RUSA_DEVOPS_LEASE_SECONDS=180
RUSA_DEVOPS_POLL_SECONDS=2
RUSA_DEVOPS_ARTIFACT_ROOT=/var/lib/rusa/devops-artifacts
RUSA_DEVOPS_PROMETHEUS_URL=
RUSA_DEVOPS_PROMETHEUS_TOKEN=
```

The only accepted autonomy mode in this release is `controlled`. A scale recommendation is stored as a proposal; the worker cannot apply infrastructure changes. Approve/reject endpoints update the proposal and append an audit event, but no infrastructure executor is registered.

## Install and run

Install the optional worker dependencies in a dedicated environment:

```bash
python -m pip install -r requirements-devops.txt
```

After applying migration 009, configuring the separate API/worker runtime database logins, and setting `RUSA_DEVOPS_ENABLED=true`:

```bash
# API: restart the existing FastAPI service.
# Worker: use a separate process and the worker database login.
RUSA_DEVOPS_ENABLED=true python -m rusa_devops.worker_main

# Or use the isolated Compose profile.
RUSA_DEVOPS_ENABLED=true docker compose --profile devops up --build devops-worker
```

The worker has no Docker socket and no Kubernetes credentials. Infrastructure observation is performed through Prometheus exporters. This is intentional: the initial worker is read/forecast/propose only.

## API

All routes require the existing API-key authentication and tenant binding.

| Method | Route | Permission | Purpose |
|---|---|---|---|
| GET | `/v1/devops/agent-card` | read | Internal DevOps capability card |
| GET | `/v1/devops/overview` | read | Queue, latest models, recent incidents, pending proposals |
| POST | `/v1/devops/jobs` | operate | Synthetic ingest, Prometheus collect, anomaly scan, capacity recommendation |
| POST | `/v1/devops/training-runs` | admin | Queue a forecasting training run |
| GET | `/v1/devops/jobs` | read | Tenant-scoped job history |
| GET | `/v1/devops/jobs/{task_id}` | read | Job detail |
| POST | `/v1/devops/jobs/{task_id}/retry` | admin | Retry a failed job |
| DELETE | `/v1/devops/jobs/{task_id}` | admin | Cancel a queued/running job |
| GET | `/v1/devops/models` | read | Model-run metadata and evaluation metrics |
| GET | `/v1/devops/incidents` | read | Recent anomaly incidents |
| GET | `/v1/devops/actions` | read | Capacity action proposals |
| POST | `/v1/devops/actions/{proposal_id}/approve` | admin | Record approval only; no execution |
| POST | `/v1/devops/actions/{proposal_id}/reject` | admin | Record rejection and audit reason |

The existing role map is reused: platform_admin and tenant_admin may administer; developer may operate; read_only may read. The DevOps API never accepts a caller-supplied tenant ID.

Example synthetic smoke test:

```bash
curl -X POST http://localhost:8000/v1/devops/jobs \
  -H "X-API-Key: $RUSA_API_KEY" -H "Content-Type: application/json" \
  -H "Idempotency-Key: devops-synthetic-0001" \
  -d '{"job_type":"synthetic_ingest","payload":{"rows":2000,"metric_name":"cpu_utilization","resource_id":"demo-local"}}'
```

Then queue a training run using a tenant-admin key:

```bash
curl -X POST http://localhost:8000/v1/devops/training-runs \
  -H "X-API-Key: $RUSA_ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"metric_name":"cpu_utilization","resource_id":"demo-local","validation_horizon":120,"test_horizon":120}'
```

## Model training and evaluation

Statistical model lifecycle:

1. Collect or synthesize telemetry.
2. Validate timestamp order, duplicate timestamps, missing values, and finite values.
3. Chronologically split into train, validation, and test.
4. Fit Prophet on train; generate expanding-window out-of-fold residuals.
5. Fit ARIMA on those residuals.
6. Predict validation + test without using validation/test values in model fitting.
7. Calibrate the asymmetric offset on validation only.
8. Evaluate the frozen calibrated hybrid on test.
9. Save Prophet JSON, ARIMA residual history, config and SHA-256 artifact digest.
10. Record metrics and split sizes in the tenant-scoped model registry.

Metrics include MAE, RMSE, asymmetric squared loss, underprediction rate, and empirical prediction-interval coverage. Anomaly benchmark metrics include precision, recall, F1, and confusion counts when synthetic labels are available. Production benchmark reports must additionally include false alarms per hour, alert delay, prediction interval coverage by resource, and p50/p95 runtime. Compare last-value/naive, Prophet-only, and Prophet + ARIMA hybrid on identical chronological test windows. Do not tune on test data; run several rolling-origin windows for a production benchmark and report confidence intervals. Synthetic telemetry is for smoke tests, not proof of production accuracy.

## LLM learning

The DevOps SFT contract is separate from the existing AICode contract. Its allowed actions are telemetry read, model-run read, incident list, knowledge retrieval, and capacity proposal. It explicitly excludes infrastructure mutation.

Validate curated, tenant-redacted data:

```bash
python llm-project/train/validate_devops_dataset.py \
  --train-file llm-project/data/devops/train.jsonl \
  --eval-file llm-project/data/devops/eval.jsonl
```

Train and evaluate on a dedicated CUDA host:

```bash
python -m pip install -r requirements-devops-train.txt
python llm-project/train/train_devops_sft.py
python llm-project/train/evaluate_devops_sft.py \
  --adapter-dir artifacts/rusa-devops-qwen3-4b-qlora/adapter
```

The trainer requires curated train/eval JSONL files under `llm-project/data/devops/`. The evaluation script checks JSON validity, action-contract compliance, blocked-action recall, and blocked-action false-positive rate before an adapter can be considered for promotion. No DevOps SFT dataset or weights are committed. Model output never grants permission; runtime tool gateways must independently validate the authenticated tenant and action scopes.

## RAG, MCP, and A2A integration

- Index runbooks, service catalogues, postmortems, and approved incident reports in the existing tenant-scoped RAG collection (for example, collection `devops`). Retrieved content is evidence, not instructions.
- The existing Knowledge MCP gains read-only tools: `devops_get_telemetry`, `devops_get_model_run`, `devops_list_incidents`, and `devops_list_model_runs`. It uses the authenticated MCP tenant context and does not expose raw database access.
- `schemas/a2a/devops-task-envelope.schema.json` and `rusa_devops.a2a` define the internal DevOps delegation envelope. The existing A2A v1 schema is unchanged.
- Supreme Orchestrator may delegate forecast, anomaly, and capacity-recommendation capabilities. All cross-agent tasks retain tenant ID, task ID, correlation ID, capability, and policy constraints.

## Control Center

The Control Center adds a DevOps page backed by same-origin Next.js route handlers. It can queue synthetic/collection/anomaly jobs and administrator-only training runs; it shows queue state, model metrics, incidents, and proposals. Administrators can approve/reject proposal records, but approval does not execute an infrastructure change. The browser never receives a database DSN, Prometheus token, model artifact path, or worker lease token.

## Test and release gates

Run:

```bash
python -m pip install -e ".[dev]"
python -m pip install -r requirements-devops.txt
python -m compileall -q rusa_devops
ruff check rusa_devops tests llm-project/train
pytest -q tests/test_devops_core.py tests/test_devops_migration.py
```

Before enabling production tenants, additionally require:

- PostgreSQL 16 migration 009 applied by schema owner and RLS verified for both API/worker logins.
- Cross-tenant isolation, queue race, lease-expiry, retry and idempotency integration tests against PostgreSQL.
- Prometheus TLS/authentication and metric-label review.
- Artifact directory mounted on persistent storage with restrictive permissions and backup policy.
- Forecast benchmark on real telemetry with chronological rolling-origin evaluation.
- Anomaly false-positive review and alert fatigue measurement.
- Model artifact checksum verification and registry promotion review.
- Staging soak, resource limits, rollback and operator sign-off.

RUSA-AI remains not production-certified until the repository's existing production certification gates and the PDIE-specific gates are completed.

# RusaAi CPU-first Control Plane operations

## 1. Architecture boundary

This profile runs the RusaAi Control Plane on a CPU-only host. **The model runtime and model artifacts belong to RusaModels**, not this repository.

**Deployment scope:** this runbook describes the containerized Compose path. The supplied CPU Deployment Configuration v1.0 records a native Windows 11 baseline, while Architecture v2.0 targets Windows 11 x64 and Android ARM64; this Compose profile is not evidence that either native-device deployment has passed acceptance testing. Native Windows and Android certification remain pending. The historical Qwen3-4B v1.0 baseline is not the v2.0 non-Qwen product-model target.

The request path is:

```text
RusaAi Worker
    -> RusaAi API / Model Access Gateway
    -> RusaModels Model Server (/openai/v1)
    -> private llama.cpp GGUF runtime
```

- **RusaAi** owns users/tenants, agents, workflow state, task queue, inference admission, policy, tool execution, verification and audit.
- **RusaModels** owns the model registry, manifests, GGUF artifacts, model residency, llama.cpp runtime, embedding runtime and model telemetry.
- The Worker calls only the RusaAi gateway. It never receives the RusaModels inference token or direct runtime URL.
- RusaAi does not mount GGUF weights, start llama.cpp, or load a local model in this CPU profile.

This separation follows the RusaAi Architecture v2.0 and the RUSA-CPU-16 Control Plane / Model Plane boundary.

## 2. Prepare the RusaModels Model Plane first

Configure and start the RusaModels Model Server separately. Follow its operational runbook:

- [RusaModels CPU inference](https://github.com/StrategyG/RusaModels/blob/main/docs/CPU_INFERENCE.md)
- [RusaModels–RusaAi integration contract](https://github.com/StrategyG/RusaModels/blob/main/docs/RUSAAI-INTEGRATION.md)

The Model Server must expose its authenticated OpenAI-compatible API at `/openai/v1`. Its private llama.cpp and embedding services must not be exposed directly.

### Model identity

The current RusaModels CPU smoke profile uses model ID `rusa-general-4b`, based on the historical Qwen3-4B CPU baseline. It is a transport/integration smoke-test model only.

The RusaAi Architecture v2.0 target is **RusaAi-Edge-1.54B**, requiring a compatible non-Qwen foundation checkpoint. That checkpoint/tokenizer has not been selected and no approved RusaAi-Edge GGUF artifact exists. Do not label `rusa-general-4b`, an arbitrary GGUF, or a derived checkpoint as `rusaai-edge-1.54b`.

For an approved model, the RusaModels Model Server's configured `RUSAMODELS_INFERENCE_MODEL_ID` must exactly match RusaAi's `RUSA_MODEL_NAME`.

### Dedicated inference principal

Create a RusaModels API principal with role `inference` only. Do not use a Manager or Administrator token for agent inference.

- Store the raw inference token only in the RusaModels/RusaAi secret managers.
- Configure its SHA-256 digest in RusaModels `RUSAMODELS_API_PRINCIPALS`.
- Pass the raw token to RusaAi as `RUSA_MODEL_API_KEY`.
- Do not pass RusaModels database credentials, object-store credentials, or its internal `X-Rusa-Service-Token` to RusaAi.

RusaAi's provider readiness check calls the authenticated `GET /openai/v1/models` endpoint and verifies that the configured `RUSA_MODEL_NAME` is present in the returned model list. Production configuration requires the exact `/openai/v1` base path and rejects `RUSA_MODEL_HEALTH_URL` overrides so a generic health endpoint cannot bypass model-identity validation. This readiness check does not replace an actual chat/streaming smoke test.

## 3. Configure RusaAi

Copy the normal environment template and merge the CPU profile:

```bash
cp .env.example .env
```

Set these values in the RusaAi environment:

| Variable | Required value |
|---|---|
| `RUSA_MODEL_BASE_URL` | Reachable RusaModels URL ending in `/openai/v1` |
| `RUSA_MODEL_NAME` | Exact configured RusaModels inference model ID |
| `RUSA_MODEL_API_KEY` | Dedicated RusaModels role=`inference` token |
| `RUSA_MAX_CONCURRENT_INFERENCES` | `1` |
| `RUSA_MAX_WAITING_INFERENCES` | `3` for `rusa-cpu-16`; `1` for `rusa-edge-8` |

Example endpoint:

```text
https://<model-server-host>/openai/v1
```

Merge `.env.cpu.example` into `.env`. For the 8-GB Control Plane limits, merge `.env.cpu-8gb.example` afterward and use the Compose overlay. Do not copy GGUF path, llama.cpp thread, context, or Model Plane memory settings into RusaAi; those belong to RusaModels.

The CPU Compose override deliberately clears `RUSA_LOCAL_MODEL_PATH` and `RUSA_MODEL_PATH`. The Control Plane must not load its own model in this deployment.

## 4. Validate and start the Control Plane

Validate the resolved configuration:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml config
docker compose -f docker-compose.yml -f docker-compose.cpu.yml -f docker-compose.cpu-8gb.yml config
```

Start the 16-GB Control Plane:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build -d
```

Start the 8-GB Control Plane with its resource overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml -f docker-compose.cpu-8gb.yml up --build -d
```

For autonomous coding/DevOps tasks, enable the existing Worker profile using the same file set:

```bash
# 16-GB
docker compose --profile worker -f docker-compose.yml -f docker-compose.cpu.yml up --build -d

# 8-GB
docker compose --profile worker -f docker-compose.yml -f docker-compose.cpu.yml -f docker-compose.cpu-8gb.yml up --build -d
```

The Worker profile requires the dedicated rootless Docker socket, workspace and Git mirror paths from `.env.example`, plus the agent sandbox image built on that same rootless Docker daemon. Do not expose or mount the host's rootful Docker socket into the Worker.

The RusaModels Model Server must already be reachable from the RusaAi API container. Compose does not start or manage the RusaModels service; this is an intentional repository boundary.

## 5. CPU admission and resource policy

| Setting | `rusa-cpu-16` | `rusa-edge-8` |
|---|---:|---:|
| Active inference requests | 1 | 1 |
| Waiting inference requests | 3 | 1 |
| Worker parallelism | 1 | 1 |
| Queue timeout | 60 seconds | 60 seconds |
| Generation timeout | 300 seconds | 300 seconds |

The PostgreSQL-backed RusaAi admission controller provides FIFO queueing and shared slot ownership across API replicas. Worker inference is routed through the same RusaAi API gateway; the Worker does not bypass admission.

The 8-GB Compose overlay caps **RusaAi Control Plane containers only** (PostgreSQL 384 MiB, API 512 MiB, Worker 768 MiB, web 384 MiB; 2 GiB total configured ceilings). RusaModels memory/CPU use is separate and must be included in the total host budget. Preserve at least 2 GiB of actual free physical RAM under representative load.

The current RusaModels CPU inference configuration is a 4-vCPU/16-GB Codespaces profile with a 4B Q5 smoke-test model. It is **not** a validated `rusa-edge-8` deployment. The 8-GB profile remains uncertified until a suitable 1B–1.5B model artifact is selected, memory/latency are benchmarked, and the combined Control Plane + Model Plane profile passes the device acceptance tests.

## 6. Readiness and smoke tests

Check the RusaAi API:

```bash
curl --fail-with-body http://127.0.0.1:8000/health/ready
```

Check the RusaModels Model Server separately with its dedicated inference token:

```bash
curl --fail-with-body "$RUSA_MODEL_BASE_URL/models" \
  -H "Authorization: Bearer $RUSA_MODEL_API_KEY"
```

Send a development request through the RusaAi gateway (the sample `.env.example` maps `local-dev-key` to the development tenant):

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  -H 'X-API-Key: local-dev-key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"rusa-general-4b","messages":[{"role":"user","content":"Jelaskan fungsi utama RusaAi secara singkat."}],"temperature":0,"max_tokens":128}'
```

The model field must match `RUSA_MODEL_NAME`. Use a real registered model ID, not the example if the RusaModels deployment uses another alias.

Also test streaming, cancellation, long prompts, output limits, queue saturation, and timeout behavior against the actual model server. A successful readiness response or single chat completion does not certify agent tool calling or autonomous execution.

## 7. Security and operations

- Keep the RusaModels inference token only in the RusaAi API secret store; the Worker receives only tenant-scoped RusaAi API keys.
- Production `RUSA_MODEL_BASE_URL` must use credential-free HTTPS with certificate validation. Do not bypass production configuration checks.
- Do not publish llama.cpp or embedding service ports. Only the authenticated RusaModels Model Server boundary is reachable by RusaAi.
- Never commit model weights, raw API tokens, database URLs, or object-store credentials.
- Keep the Policy Engine authoritative. Model output cannot grant permissions or bypass the Tool Gateway.
- Record the RusaAi commit, RusaModels commit, model ID/revision, GGUF SHA-256, Model Server image digest, CPU model, physical RAM, context, and thread settings with every benchmark.
- Monitor CPU, host/cgroup RAM, free RAM, queue wait, queue depth, 429 responses, timeout count, inference latency, model-server restarts, and thermal throttling where available.

Inspect Control Plane logs:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml logs --tail=200 api worker
```

Inspect llama.cpp and embedding runtime logs from the RusaModels deployment, not from this repository.

## 8. Agent and release acceptance

An OpenAI-compatible endpoint does not prove that a model is suitable for autonomous agents. Before enabling agent workflows, test the selected model's chat template, role handling, structured JSON output, function/tool-call format, refusal behavior, and long-context performance against the RusaAi agent contracts.

Run the task, policy, tenant-isolation, sandbox, audit, ORM, PRM-Lite and independent-QA tests separately from model smoke tests. A passing chat completion does not certify the agent system or production deployment.

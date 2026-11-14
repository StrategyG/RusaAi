# RusaAi inference gateway operations runbook

## Release boundary

RusaAi is the Control Plane and Model Access Gateway. RusaModels owns model registry, manifests, weights, GGUF verification, runtime loading, model residency and the Model Server. This runbook covers RusaAi gateway behavior and the legacy local adapter used only for isolated development; production RusaAi must call the authenticated RusaModels `/openai/v1` endpoint. It does not certify the agent platform, RAG/MCP, tenant isolation, or the overall RusaAi product. Do not promote a release without evidence for the exact RusaAi/RusaModels commits, model artifact digest, Model Server image digest, hardware, and deployment configuration. The v2.0 RusaAi-Edge checkpoint is still unselected; using a model ID in an API request does not establish that the target artifact exists. The current RusaModels CPU smoke alias `rusa-general-4b` is a historical Qwen3-4B profile, not RusaAi-Edge. Reserve `rusaai-edge-1.54b` for an audited non-Qwen checkpoint matching the exact v2.0 reference dimensions; use `rusaai-edge-derived` when the selected non-Qwen checkpoint differs.

## 1. Model artifact intake boundary

Production artifact intake is performed in RusaModels, not RusaAi:

1. Register the model/version in the RusaModels registry with owner, immutable source revision, license/usage restrictions, tokenizer identity/revision, architecture, context limit and intended hardware.
2. Verify the manifest and every artifact digest in RusaModels. For GGUF, use the RusaModels GGUF verifier and pin the exact SHA-256.
3. Keep weights outside Git and mount model files read-only in the RusaModels runtime.
4. Configure a dedicated RusaModels API principal with role `inference` only. RusaAi receives only this bearer token; it never receives RusaModels database or object-store credentials.
5. Configure RusaAi `RUSA_MODEL_BASE_URL` to the authenticated RusaModels `/openai/v1` endpoint, `RUSA_MODEL_NAME` to the exact registered model ID, and `RUSA_MODEL_API_KEY` to the dedicated inference token.

The legacy local Transformers adapter described below is development-only. Production configuration rejects `RUSA_LOCAL_MODEL_PATH` and `RUSA_MODEL_PATH`; the RusaAi Control Plane must not load or own model artifacts.

## 2. Backend selection

### Legacy local Transformers adapter (development only)

Use `RUSA_INFERENCE_BACKEND=transformers` only for isolated local development tests. It uses one generation lock per process. It is not the production Model Plane and production configuration rejects local model paths. For CPU deployment, run llama.cpp in RusaModels and call it through the RusaModels Model Server.

### vLLM compatibility note

The optional vLLM dependency and local Transformers image remain legacy development adapters in this repository. Production GPU/CPU inference runtime selection, model loading and serving belong to RusaModels. RusaAi should configure only the authenticated Model Server URL, model ID and dedicated inference token. Do not install a second model-serving engine inside the RusaAi Control Plane.
- RUSA_MAX_MODEL_LEN
- RUSA_MAX_NUM_SEQS
- RUSA_MAX_NUM_BATCHED_TOKENS
- RUSA_TENSOR_PARALLEL_SIZE
- RUSA_GPU_MEMORY_UTILIZATION

Start with conservative limits. Increase one capacity dimension at a time and retain the benchmark report for each change. Avoid multiple vLLM engine processes competing for the same GPU memory. The Compose service reserves shared memory for PyTorch/NCCL and a writable temporary cache area for CUDA/Triton compilation; size RUSA_SHM_SIZE and RUSA_TMPFS_SIZE for the actual host. The temporary mount is not marked noexec because some GPU kernel toolchains load compiled artifacts from that location.

auto is a convenience selection mode, not a substitute for an explicit production deployment decision. Pin the backend in production.

## 3. Required environment

| Variable | Purpose | Suggested starting point |
|---|---|---|
| ENVIRONMENT | Controls production fail-closed behavior | production |
| RUSA_API_KEY | Service API credential | Secret manager; 48+ random bytes |
| RUSA_MODEL_BASE_URL | Authenticated RusaModels Model Server URL | `https://<model-server-host>/openai/v1` |
| RUSA_MODEL_API_KEY | Dedicated RusaModels `inference` role token | Secret manager; never Manager/Administrator |
| RUSA_MODEL_NAME | Exact registered RusaModels model ID | `rusa-general-4b` only for the current smoke profile |
| RUSA_LOCAL_MODEL_PATH / RUSA_MODEL_PATH | Legacy local model directory | Development only; forbidden in production |
| RUSA_MAX_MODEL_LEN | Maximum prompt + output context | Never above verified model limit |
| RUSA_GENERATION_TIMEOUT_SECONDS | Per-generation timeout | 180; tune to workload |
| RUSA_MAX_CONCURRENT_INFERENCES | Active API generations | 1 on the CPU-first baseline |
| RUSA_MAX_WAITING_INFERENCES | Bounded waiting API requests | 3 on 16-GB; 1 on 8-GB |
| RUSA_INFERENCE_QUEUE_TIMEOUT_SECONDS | Maximum wait for an inference slot | 60 |
| RUSA_MAX_NUM_SEQS | vLLM maximum active sequences | 16 |
| RUSA_MAX_NUM_BATCHED_TOKENS | vLLM batch token budget | 8192 |
| RUSA_TENSOR_PARALLEL_SIZE | Number of GPUs in tensor parallel group | 1 |
| RUSA_GPU_MEMORY_UTILIZATION | vLLM GPU memory target | 0.90 |

Values above are initial configuration examples, not capacity guarantees. Measure the exact model in RusaModels on the target host. The RusaAi gateway does not load weights; its readiness check calls the authenticated RusaModels model-list endpoint. Configure startup grace periods in the Model Plane according to actual artifact loading time.

The OpenAI-compatible chat request also accepts an optional body field deadline_ms (1–3,600,000 ms); this is a per-request budget, not an environment variable.

### Request deadline behavior

The OpenAI-compatible /v1/chat/completions request accepts an optional integer deadline_ms from 1 to 3,600,000. The budget starts when the API handler begins processing the validated request and covers both inference-slot admission and generation.

- If the deadline expires while waiting for a slot, the API returns HTTP 504 and removes the waiter from the FIFO queue.
- If a non-streaming generation exceeds the remaining budget, the API cancels the provider coroutine, releases its slot, and returns HTTP 504.
- If the request task is cancelled (for example, after client disconnect), the API cancels and awaits the provider coroutine before its admission slot is released.
- If streaming has already started, the API emits an SSE timeout_error event followed by data: [DONE]; it cancels the pending provider iteration and releases the slot.
- If deadline_ms is omitted, the existing queue timeout and provider timeout behavior remains unchanged.
- A queue-capacity rejection remains HTTP 429. A queue timeout that occurs before the request deadline also remains HTTP 429.
- The CPU Compose profile routes autonomous worker requests through the same RusaAi API endpoint with a tenant-scoped API key and a configured worker inference deadline. RusaAi then calls RusaModels using its dedicated `inference` role token; the Worker never receives that token or a direct llama.cpp URL.
- When `RUSA_API_DATABASE_URL` is configured, the PostgreSQL-backed scheduler shares FIFO state and active slots across API replicas. The in-memory scheduler is a development fallback only.

### Durable PostgreSQL admission scheduler

When `RUSA_API_DATABASE_URL` is configured, the API uses the PostgreSQL-backed
scheduler instead of the in-memory admission controller. All API replicas that
serve the same model endpoint therefore share one FIFO queue and one set of
active-generation slots.

- Apply `migrations/010_inference_admission.sql` after migrations 001-009 before
  deploying the API. The migration grants access only to `rusa_api_runtime`;
  the worker runtime receives no direct scheduler-table access.
- The scheduler persists request IDs, state, queue expiry, slot ownership, and
  lease expiry only. It does not store prompts, completions, or tenant payloads.
- The migration defaults are one active slot, three waiting requests, a 60-second
  queue timeout, and a 30-second lease. The database config row
  `rusa_inference_admission_config` must match
  `RUSA_MAX_CONCURRENT_INFERENCES`,
  `RUSA_MAX_WAITING_INFERENCES`,
  `RUSA_INFERENCE_QUEUE_TIMEOUT_SECONDS`, and
  `RUSA_INFERENCE_LEASE_SECONDS` on every API replica. Startup fails closed if
  they differ.
- To change the active slot count, update the config row during a maintenance
  window and provision exactly one row per slot in
  `rusa_inference_admission_slots` before restarting API replicas. Do not
  increase `max_active` beyond the model server's tested capacity.
- Active requests renew their lease every third of the lease interval. If an API
  process crashes, a later request reclaims an expired slot. If a live process
  cannot renew its lease, the associated generation task is cancelled.
- Streaming responses rebind lease ownership to the task that consumes the SSE
  body; lease loss therefore cancels the actual stream/generation task rather
  than the already-completed route handler. A stream whose lease is lost before
  its body starts fails closed without invoking the model provider.
- Lease renewal takes the same scheduler advisory lock as release and expiry
  before touching slot/request rows. This keeps row-lock order consistent and
  prevents a heartbeat/release deadlock.
- Scheduler database connections use a five-second connection timeout, five-second
  lock/statement timeouts, and a ten-second idle-in-transaction timeout. A stalled
  database therefore fails closed instead of holding admission or heartbeat work
  indefinitely.
- Waiting requests are claimed in database FIFO order. Queue-full, queue-timeout,
  request-deadline, cancellation, and stale-lease states are recorded separately.
- The API readiness/startup path checks the migration, database role privileges,
  scheduler configuration, and slot rows. Missing schema or privilege errors
  prevent the API from starting.

For a 16-GB CPU profile, the default scheduler row is one active and three
waiting. For an 8-GB profile, set `max_waiting=1` in both the database config
row and the environment. Keep the same settings across all replicas.

For example, changing to two active slots and one waiting request requires a
maintenance window with API traffic drained, then a database-owner transaction
such as:

```sql
BEGIN;
UPDATE public.rusa_inference_admission_config
SET max_active = 2,
    max_waiting = 1,
    queue_timeout_ms = 60000,
    lease_seconds = 30,
    updated_at = clock_timestamp()
WHERE singleton_id = 1;
INSERT INTO public.rusa_inference_admission_slots(slot_id)
SELECT generate_series(0, 1)::smallint
ON CONFLICT (slot_id) DO NOTHING;
COMMIT;
```

Set the same values in the environment before starting the API replicas. Only
increase active slots after the model backend has passed a concurrency and
memory-capacity test for that target hardware.

### Worker-to-API inference gateway

For the CPU Compose profile, the RusaAi API is the only RusaAi component that connects to the RusaModels Model Server. The Worker connects to the RusaAi OpenAI-compatible gateway and supplies the API key mapped to the current task tenant. RusaAi uses its separate RusaModels `inference` role token upstream. The Worker must never reuse another tenant's key or fall back to a global credential when a tenant mapping is missing.

**Scope qualification:** this paragraph describes the CPU Compose chat-completion path only. That profile leaves `RUSA_RAG_ENABLED` false. If optional Worker RAG is enabled elsewhere, the current `KnowledgeService` constructs an embedding client in the Worker and can call RusaModels `/openai/v1/embeddings` directly using `RUSA_EMBEDDING_API_KEY` or the `RUSA_MODEL_API_KEY` fallback. That direct embedding path bypasses this gateway and is not approved for production. Keep Worker RAG disabled and do not distribute Model Server credentials to workers until a tenant-aware RusaAi embedding gateway or reviewed brokered contract is implemented and verified.

- Set `RUSA_WORKER_INFERENCE_GATEWAY=true`.
- Set `RUSA_WORKER_INFERENCE_BASE_URL` to the API gateway URL. Production requires credential-free HTTPS with certificate validation.
- Mount the internal CA bundle read-only into the worker and set `SSL_CERT_FILE` to the in-container bundle path. The production Compose overlay requires `RUSA_INTERNAL_CA_BUNDLE` as the host-side source path.
- Set `RUSA_WORKER_INFERENCE_API_KEYS` to a tenant-to-secret mapping. Keep raw keys in the secret manager; each key's SHA-256 digest must resolve to a principal in `RUSA_API_KEYS_JSON` with the same tenant and an inference-capable role.
- Set `RUSA_WORKER_INFERENCE_DEADLINE_MS` to a bounded request budget. The API enforces that budget across queue wait and generation.
- The worker provider sends `X-API-Key` and a JSON-object response-format requirement. The API validates the request and forwards the response format to the model backend.
- The worker gateway does not automatically retry HTTP capacity/deadline errors or transport failures; this avoids duplicate model generations. Task-level retry remains controlled by the durable queue and its idempotency policy.
- API replicas share FIFO state and active slots through the PostgreSQL admission scheduler when configured. RusaModels' runtime parallel-slot limit remains a final capacity guard; do not increase RusaAi active slots beyond the Model Server's tested capacity.

The local CPU Compose route uses private-network HTTP for development/staging only. Do not copy that URL into production configuration.

## 4. Startup and health checks

1. Start and verify the RusaModels Model Server first. Its authenticated `GET /openai/v1/models` must return the exact configured model ID using the dedicated `inference` token.
2. Start the RusaAi API with `RUSA_MODEL_BASE_URL`, `RUSA_MODEL_NAME` and `RUSA_MODEL_API_KEY` configured. Do not mount a model directory into the RusaAi API.
3. GET `/health/live` confirms only that the RusaAi process responds.
4. GET `/health/ready` must return HTTP 200 after the RusaAi gateway has checked its upstream model endpoint, authentication and task schema.
5. GET `/v1/models` with a tenant-authorized RusaAi API key must return the configured model ID.
6. Run one non-streaming request and one streaming request through the RusaAi API using a controlled test prompt.
7. Confirm response usage counts are present and streaming terminates with `data: [DONE]`.
8. Model manifest and GGUF/safetensors verification are RusaModels responsibilities. Convert untrusted pickle checkpoints only through a trusted, isolated Model Plane intake workflow.

If any readiness check fails, keep the instance out of the load balancer. The API returns 503 rather than generating a substitute answer when the model is missing or unverified.

## 5. API smoke test

    curl --fail-with-body http://127.0.0.1:8000/health/ready

    curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
      -H "X-API-Key: $RUSA_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"model":"rusaai-cpu-smoke-test","messages":[{"role":"user","content":"Sebutkan tiga fungsi utama sistem pernapasan."}],"temperature":0,"max_tokens":128}'

    curl --no-buffer http://127.0.0.1:8000/v1/chat/completions \
      -H "X-API-Key: $RUSA_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"model":"rusaai-cpu-smoke-test","messages":[{"role":"user","content":"Jelaskan energi terbarukan."}],"temperature":0,"max_tokens":128,"stream":true}'

## 6. Reproducible benchmark

Run the smoke benchmark only against local or staging. It deliberately rejects remote targets unless --environment staging --allow-remote-staging is supplied. Production benchmarking requires a separately approved load-test plan and must not be run by this helper.

    RUSA_API_KEY="$RUSA_API_KEY" python scripts/benchmark_inference.py \
      --base-url http://127.0.0.1:8000 \
      --requests 100 --concurrency 8 --max-tokens 128 \
      --environment local --output benchmark-report.json

Store the report with:
- Git commit and container digest
- Server GPU model/VRAM, driver, CUDA, and device utilization captured on the serving host (the report's client hardware field describes the benchmark runner)
- model manifest digest and model/tokenizer revisions
- GPU model, VRAM, driver, CUDA, PyTorch and vLLM versions
- backend settings, context length, max output tokens and concurrency
- warmup policy and prompt set
- p50/p95/p99 latency and TTFT
- request throughput and completion tokens/second
- peak GPU/CPU memory, utilization, errors and timeouts

The bundled script uses zero warmup requests. For comparisons, run an explicitly documented warmup separately and keep that policy identical across candidates.

## 7. Capacity and resilience test plan

1. Run a low-concurrency baseline.
2. Increase concurrency in controlled steps (1, 2, 4, 8, 16, ...), stopping before VRAM or error-rate limits are breached.
3. Test short and long prompts, varied output lengths, streaming and non-streaming.
4. Test queue saturation and verify HTTP 429 with Retry-After.
5. Test generation deadline and verify HTTP 504 or an SSE timeout event.
6. Disconnect streaming clients and verify backend generation is cancelled/aborted.
7. Restart the process during active traffic and verify clean recovery.
8. Run a multi-hour staging soak test.
9. Verify p50/p95/p99, TTFT, tokens/second, memory, error rate and queue wait against owner-approved SLOs.

Do not infer concurrency safety from a passing unit test. Capacity is specific to the checkpoint, backend, hardware, prompt distribution, output lengths and serving configuration.

## 8. Retry and degradation policy

- Model loading retries are bounded and startup remains not-ready if loading fails.
- Inference retries apply only to backend errors explicitly classified as transient. The API does not blindly retry timeouts or arbitrary failures because that can multiply GPU work and produce duplicate completions.
- Once streaming has started, the API reports an SSE error event and closes the stream; it does not restart generation mid-response.
- Overload is rejected with 429; generation timeout is reported as 504 for non-streaming and an SSE timeout event for streaming.
- Missing or unverified model artifacts keep readiness false and inference unavailable.

## 9. Security and operational controls

- Terminate TLS at a trusted gateway.
- Apply distributed rate limiting, request/connection timeouts, IP/network restrictions, and body limits at ingress as well as in the application.
- Inject API keys and manifest digests through a secret/configuration manager; rotate credentials.
- Mount model artifacts read-only and do not bake weights or secrets into images.
- Keep logs free of prompts, completions, credentials, and raw authorization headers.
- Enable metrics, tracing, alerts, audit retention, backup/restore, rollback, and incident runbooks.
- Use one process per GPU allocation unless the memory and throughput plan explicitly permits more.
- Review model license, output safety, prompt injection, and downstream tool authorization separately from serving correctness.

## 10. Release checklist

- [ ] Exact model artifact and tokenizer approved.
- [ ] Artifact manifest and externally pinned SHA-256 verified.
- [ ] Real inference succeeds on target hardware.
- [ ] Streaming and cancellation tested with the actual backend.
- [ ] vLLM continuous batching (if selected) verified under concurrent load.
- [ ] Benchmark report attached to exact code/model/hardware revisions.
- [ ] Capacity and SLO thresholds approved by service owner.
- [ ] Dependency audit, container scan, security tests, and full CI pass on release SHA.
- [ ] Staging soak, backup/restore, rollback and alerting exercises pass.
- [ ] Human owner approves production promotion.

Until every applicable item is evidenced, the service remains a production candidate, not production-certified.

## 11. CPU-only Model Plane

For CPU-only deployment, run the llama.cpp GGUF and embedding runtimes in RusaModels using its [CPU inference runbook](https://github.com/StrategyG/RusaModels/blob/main/docs/CPU_INFERENCE.md). RusaAi calls the authenticated RusaModels Model Server through `RUSA_MODEL_BASE_URL`; it does not start llama.cpp, mount GGUF files or verify model artifacts itself.

The RusaModels Model Server exposes the OpenAI-compatible `/openai/v1` boundary. Use a dedicated `inference` role token and ensure `RUSA_MODEL_NAME` exactly matches the configured Model Server alias. Keep the RusaAi worker routed through the RusaAi API gateway so tenant policy, deadline enforcement and shared admission remain authoritative.

Set inference concurrency conservatively on CPU (initially one active generation), use a bounded waiting queue, and increase limits only after a benchmark on the exact model, CPU, RAM, context size, and prompt distribution. Tool calling and structured JSON output must be tested with the chosen model before enabling agent workflows.
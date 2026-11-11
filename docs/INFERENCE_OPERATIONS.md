# RUSA-AI inference operations runbook

## Release boundary

This runbook applies to the local model serving API. It does not certify the agent platform, RAG/MCP, tenant isolation, or the overall RUSA-AI product. Do not promote a release without evidence for the exact commit, model manifest digest, container digest, hardware, and deployment configuration. The v2.0 RusaAi-Edge checkpoint is still unselected; using a model ID in an API request does not establish that the target artifact exists. The sample configuration uses `rusaai-cpu-smoke-test`. Reserve `rusaai-edge-1.54b` for an audited checkpoint matching the exact v2.0 reference dimensions; use `rusaai-edge-derived` when the selected non-Qwen checkpoint differs.

## 1. Artifact intake

1. Obtain the checkpoint from an approved storage location.
2. Record model owner, immutable revision, license/usage restrictions, tokenizer identity/revision, architecture, context limit, and intended hardware.
3. Keep weights outside Git and mount the directory read-only.
4. Run scripts/create_model_manifest.py and review the generated manifest.
5. Store the printed manifest SHA-256 in the deployment secret/configuration manager as RUSA_MODEL_MANIFEST_SHA256.
6. Do not modify the model directory after manifest generation. Any file change requires a new manifest and digest.
7. Confirm the model tokenizer has a valid chat template and the model is compatible with the Transformers causal-LM interface or selected vLLM backend.

The manifest digest must be delivered through a trusted deployment channel. A manifest stored beside the model without an externally pinned digest does not establish artifact authenticity.

## 2. Backend selection

### Transformers baseline

Use RUSA_INFERENCE_BACKEND=transformers for CPU validation or a small single-GPU baseline. It uses one generation lock per process. Requests are bounded by the API admission controller, but generation is serialized. Do not claim continuous batching for this backend.

### vLLM production candidate

Use RUSA_INFERENCE_BACKEND=vllm 0.32.x when the model and target GPU are supported by the selected vLLM/PyTorch/CUDA versions. The default API image intentionally omits heavyweight model dependencies. Build a local Transformers image with `docker build --build-arg RUSA_INSTALL_LOCAL_MODEL=1 -t rusa-ai-local-model .`; this installs the optional `[model]` extra. The vLLM dependency is isolated in `requirements-vllm.txt` and must be installed in a deployment-specific image/environment with a mutually compatible CUDA, PyTorch, vLLM, NVIDIA Container Toolkit, and host-driver combination. This repository does not currently provide a GPU Compose override for the current mainline service topology. vLLM provides continuous batching and paged KV-cache management. Configure:
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
| RUSA_MODEL_PATH or RUSA_LOCAL_MODEL_PATH | Mounted model directory | /models |
| RUSA_MODEL_MANIFEST_SHA256 | Pinned artifact manifest digest | Exact SHA-256 from manifest generator |
| RUSA_MODEL_NAME | OpenAI API model identifier | rusaai-cpu-smoke-test until an approved artifact is configured |
| RUSA_INFERENCE_BACKEND | Backend selection | vllm after validation |
| RUSA_MAX_MODEL_LEN | Maximum prompt + output context | Never above verified model limit |
| RUSA_GENERATION_TIMEOUT_SECONDS | Per-generation timeout | 180; tune to workload |
| RUSA_MAX_CONCURRENT_INFERENCES | Active API generations | 1 on the CPU-first baseline |
| RUSA_MAX_WAITING_INFERENCES | Bounded waiting API requests | 3 on 16-GB; 1 on 8-GB |
| RUSA_INFERENCE_QUEUE_TIMEOUT_SECONDS | Maximum wait for an inference slot | 60 |
| RUSA_MAX_NUM_SEQS | vLLM maximum active sequences | 16 |
| RUSA_MAX_NUM_BATCHED_TOKENS | vLLM batch token budget | 8192 |
| RUSA_TENSOR_PARALLEL_SIZE | Number of GPUs in tensor parallel group | 1 |
| RUSA_GPU_MEMORY_UTILIZATION | vLLM GPU memory target | 0.90 |

Values above are initial configuration examples, not capacity guarantees. Measure the exact checkpoint on the target host. The Compose startup grace period defaults to 30 minutes because loading large local weights occurs before the API starts serving; increase RUSA_START_PERIOD for larger artifacts.

The OpenAI-compatible chat request also accepts an optional body field deadline_ms (1–3,600,000 ms); this is a per-request budget, not an environment variable.

### Request deadline behavior

The OpenAI-compatible /v1/chat/completions request accepts an optional integer deadline_ms from 1 to 3,600,000. The budget starts when the API handler begins processing the validated request and covers both inference-slot admission and generation.

- If the deadline expires while waiting for a slot, the API returns HTTP 504 and removes the waiter from the FIFO queue.
- If a non-streaming generation exceeds the remaining budget, the API cancels the provider coroutine, releases its slot, and returns HTTP 504.
- If the request task is cancelled (for example, after client disconnect), the API cancels and awaits the provider coroutine before its admission slot is released.
- If streaming has already started, the API emits an SSE timeout_error event followed by data: [DONE]; it cancels the pending provider iteration and releases the slot.
- If deadline_ms is omitted, the existing queue timeout and provider timeout behavior remains unchanged.
- A queue-capacity rejection remains HTTP 429. A queue timeout that occurs before the request deadline also remains HTTP 429.
- The CPU Compose profile routes autonomous worker requests through the same API endpoint with a tenant-scoped API key and a configured worker inference deadline. This shares the API-process admission controller and removes the worker's direct llama.cpp bypass for that profile.
- The admission queue is still process-local, not durable or shared across multiple API replicas. Keep one API replica per model endpoint until the PostgreSQL-backed cross-process scheduler and its crash/restart tests are implemented.

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

For the CPU Compose profile, the API is the only component that connects to llama.cpp. The autonomous worker connects to the API's OpenAI-compatible endpoint and supplies the API key mapped to the current task tenant. The worker must never reuse another tenant's key or fall back to a global credential when a tenant mapping is missing.

- Set `RUSA_WORKER_INFERENCE_GATEWAY=true`.
- Set `RUSA_WORKER_INFERENCE_BASE_URL` to the API gateway URL. Production requires credential-free HTTPS with certificate validation.
- Mount the internal CA bundle read-only into the worker and set `SSL_CERT_FILE` to the in-container bundle path. The production Compose overlay requires `RUSA_INTERNAL_CA_BUNDLE` as the host-side source path.
- Set `RUSA_WORKER_INFERENCE_API_KEYS` to a tenant-to-secret mapping. Keep raw keys in the secret manager; each key's SHA-256 digest must resolve to a principal in `RUSA_API_KEYS_JSON` with the same tenant and an inference-capable role.
- Set `RUSA_WORKER_INFERENCE_DEADLINE_MS` to a bounded request budget. The API enforces that budget across queue wait and generation.
- The worker provider sends `X-API-Key` and a JSON-object response-format requirement. The API validates the request and forwards the response format to the model backend.
- The worker gateway does not automatically retry HTTP capacity/deadline errors or transport failures; this avoids duplicate model generations. Task-level retry remains controlled by the durable queue and its idempotency policy.
- The baseline is single API process per model endpoint. Multiple API replicas do not share FIFO state; llama.cpp `--parallel 1` is only a final active-generation guard.

The local CPU Compose route uses private-network HTTP for development/staging only. Do not copy that URL into production configuration.

## 4. Startup and health checks

1. Start the API with the model directory mounted read-only. Model loading occurs before the application reports ready; configure the orchestrator startup grace period for the expected artifact size.
2. Confirm logs state that the selected backend and verified model loaded successfully. Logs must not contain prompts, completions, API keys, or secret values.
3. GET /health/live confirms only that the process responds.
4. GET /health/ready must return HTTP 200 and report model_loaded, model_manifest_verified, authentication_configured, and task_schema_loaded as true.
5. GET /v1/models with X-API-Key must return the expected configured model ID.
6. Run one non-streaming request and one streaming request using a controlled test prompt.
7. Confirm response usage counts are present and streaming terminates with data: [DONE].
8. The manifest generator and runtime accept safetensors weights only; convert any pickle-based checkpoint through a trusted, isolated conversion workflow before intake.

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

## 11. CPU-only GGUF serving

For a CPU-only server, use the separate llama.cpp GGUF deployment described in [CPU inference operations](CPU_INFERENCE_OPERATIONS.md). The API talks to llama.cpp through the existing OpenAI-compatible provider. This keeps agent orchestration, task queues, policy checks, audit, and RAG boundaries independent from the inference engine.

The Transformers backend remains available for validated safetensors artifacts. The GGUF path does not use the safetensors-only model manifest generator; record the approved GGUF source revision and SHA-256 checksum separately. Do not add model weights to Git.

Set inference concurrency conservatively on CPU (initially one active generation), use a bounded waiting queue, and increase limits only after a benchmark on the exact model, CPU, RAM, context size, and prompt distribution. Tool calling and structured JSON output must be tested with the chosen model before enabling agent workflows.
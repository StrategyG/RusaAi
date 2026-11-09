# RUSA-AI inference operations runbook

## Release boundary

This runbook applies to the local model serving API. It does not certify the agent platform, RAG/MCP, tenant isolation, or the overall RUSA-AI product. Do not promote a release without evidence for the exact commit, model manifest digest, container digest, hardware, and deployment configuration.

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
| RUSA_MODEL_NAME | OpenAI API model identifier | rusa-ai-v1 |
| RUSA_INFERENCE_BACKEND | Backend selection | vllm after validation |
| RUSA_MAX_MODEL_LEN | Maximum prompt + output context | Never above verified model limit |
| RUSA_GENERATION_TIMEOUT_SECONDS | Per-generation timeout | 180; tune to workload |
| RUSA_MAX_CONCURRENT_REQUESTS | Active API generation admission | 8; tune per backend |
| RUSA_MAX_QUEUED_REQUESTS | Bounded waiting queue | 32 |
| RUSA_QUEUE_TIMEOUT_SECONDS | Maximum wait for an inference slot | 10 |
| RUSA_MAX_NUM_SEQS | vLLM maximum active sequences | 16 |
| RUSA_MAX_NUM_BATCHED_TOKENS | vLLM batch token budget | 8192 |
| RUSA_TENSOR_PARALLEL_SIZE | Number of GPUs in tensor parallel group | 1 |
| RUSA_GPU_MEMORY_UTILIZATION | vLLM GPU memory target | 0.90 |

Values above are initial configuration examples, not capacity guarantees. Measure the exact checkpoint on the target host. The Compose startup grace period defaults to 30 minutes because loading large local weights occurs before the API starts serving; increase RUSA_START_PERIOD for larger artifacts.

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
      -d '{"model":"rusa-ai-v1","messages":[{"role":"user","content":"Sebutkan tiga fungsi utama sistem pernapasan."}],"temperature":0,"max_tokens":128}'

    curl --no-buffer http://127.0.0.1:8000/v1/chat/completions \
      -H "X-API-Key: $RUSA_API_KEY" \
      -H "Content-Type: application/json" \
      -d '{"model":"rusa-ai-v1","messages":[{"role":"user","content":"Jelaskan energi terbarukan."}],"temperature":0,"max_tokens":128,"stream":true}'

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
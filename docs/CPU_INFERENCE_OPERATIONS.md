# RusaAi CPU-only GGUF inference operations

## Scope and architecture

This deployment runs model inference on a CPU-only server using the official llama.cpp HTTP server and a GGUF model. The RUSA-AI FastAPI gateway forwards chat requests through its existing OpenAI-compatible provider adapter; no CUDA, NVIDIA driver, PyTorch, Transformers, vLLM, or GPTQ installation is required for this inference path.

The model server is an inference component only. It does not replace the RUSA-AI Supreme Orchestrator, Planner, Task Decomposer, Worker, QA, Policy Engine, workflow state, durable queue, event log, audit, or RAG services. Agent orchestration and authorization remain in RUSA-AI; only the model-serving boundary changes.

## 1. Host requirements

- Linux server with Docker Engine and Docker Compose plugin.
- Python 3.10+ with the base project dependencies installed if running the host-side GGUF verifier (`python -m pip install -e .`).
- CPU architecture supported by the selected llama.cpp server image.
- RAM sized for the GGUF weights, context/KV cache, server overhead, and operating system. Do not size RAM from the quantization label alone.
- Persistent storage for model files, outside the Git working tree.
- A network path from the RUSA-AI API container to the private llama.cpp container network.

The v2.0 neural-core target is RusaAi-Edge-1.54B and requires a non-Qwen foundation checkpoint. That checkpoint and its tokenizer are not selected yet, and no official RusaAi-Edge GGUF artifact exists. Once selected, use an approved Q4_K_M GGUF build only after confirming the architecture, license, chat template, CPU instruction set, RAM, and target workload. A temporary GGUF may be used for transport smoke tests only when clearly labeled as a test artifact; do not call it RusaAi-Edge.

## 2. Prepare the model artifact

1. Download a GGUF file from the model publisher or an approved mirror.
2. Record the model ID, immutable source revision, license, tokenizer/chat-template details, context limit, quantization, and file size.
3. Keep the file outside this repository. Example path: `/srv/rusa/models/model.gguf`.
4. Calculate and record the SHA-256 checksum in the approved model release record:

```bash
sha256sum /srv/rusa/models/model.gguf
```

5. Set `RUSA_GGUF_MODEL_PATH` and `RUSA_GGUF_MODEL_SHA256` in `.env`, then run the repository verifier. The script loads `.env` automatically; use `--env-file` to select another dotenv file, or pass `--model` and `--sha256` explicitly:

```bash
python scripts/verify_gguf_model.py
```

The verifier requires an absolute `.gguf` path, rejects symlinks and empty/non-regular files, and compares the complete file digest with the pinned SHA-256. Do not start the inference container if verification fails.

6. Ensure the Docker container can read the file. Do not make it writable by the inference service.

The existing `scripts/create_model_manifest.py` and local Transformers verifier are designed for safetensors model directories and do not establish the RusaAi-Edge GGUF release. They are not the GGUF intake mechanism. For this CPU deployment, verify the GGUF SHA-256 against the approved release record before starting the container. Never commit weights, API keys, or model access tokens.

## 3. Configure the CPU stack

From the repository root:

```bash
cp .env.example .env
```

Merge the values in `.env.cpu.example` into `.env` without overwriting existing secrets. Set `RUSA_GGUF_MODEL_PATH` to the absolute path of the verified GGUF file and `RUSA_GGUF_MODEL_SHA256` to its approved checksum. Run `python scripts/verify_gguf_model.py` before starting Compose.

The sample alias is deliberately `rusaai-cpu-smoke-test`: the RusaAi-Edge checkpoint and tokenizer have not been selected and no approved RusaAi-Edge GGUF exists. Do not label an arbitrary or temporary GGUF as `rusaai-edge-1.54b`. Reserve that ID for an audited non-Qwen checkpoint whose actual architecture matches the complete v2.0 reference dimensions. If the approved checkpoint differs, use `rusaai-edge-derived` and the checkpoint's actual context/configuration; the Training Engine v1.0's provisional 556.3M candidate documents a 2K context and is not yet approved.

For `rusa-edge-8` (8-GB), merge `.env.cpu-8gb.example` after `.env.cpu.example` and add `docker-compose.cpu-8gb.yml` as the last Compose file. The 8-GB overlay caps the maximum combined container memory at 5 GiB (model 3 GiB, worker 768 MiB, API 512 MiB, web 384 MiB, PostgreSQL 384 MiB), caps llama.cpp at 2 CPUs with two generation/batch threads, and reduces the API waiting queue to one request. These are conservative resource ceilings, not a measured fit or a guarantee of free host RAM. Verify at least 2 GiB of actual free physical RAM during model load and representative inference; reduce background work or stop accepting new tasks if the reserve is breached.

Validate the resolved Compose configuration:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml config
docker compose -f docker-compose.yml -f docker-compose.cpu.yml -f docker-compose.cpu-8gb.yml config
```

For the 16-GB CPU profile, run the API, PostgreSQL, web control center, and CPU model server:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build -d
```

For the 8-GB profile, append the resource overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml -f docker-compose.cpu-8gb.yml up --build -d
```

For the complete autonomous coding/DevOps workflow, enable the existing worker profile. Use the matching file set for the device profile:

```bash
# 16-GB
docker compose --profile worker -f docker-compose.yml -f docker-compose.cpu.yml up --build -d

# 8-GB
docker compose --profile worker -f docker-compose.yml -f docker-compose.cpu.yml -f docker-compose.cpu-8gb.yml up --build -d
```

The worker profile requires the dedicated rootless Docker socket, workspace and Git mirror paths from `.env.example`, plus the agent sandbox image built on that same rootless Docker daemon. For example, after loading the `.env` values in your shell, build the sandbox with `DOCKER_HOST="unix://${RUSA_DOCKER_SOCKET}" docker build -f Dockerfile.agent-sandbox -t "${RUSA_SANDBOX_IMAGE}" .`. Do not expose or mount the host's rootful Docker socket into the worker.

The CPU override configures the API gateway to use `http://llama-cpp:8080/v1` and clears any inherited remote model API key and local Transformers model path. The Worker sends agent inference requests to `http://api:8000/v1` using a tenant-scoped `X-API-Key`; it no longer calls llama.cpp directly. The API validates the model name, enforces the request deadline, applies FIFO admission, and forwards the JSON response-format requirement to llama.cpp. This makes API and worker generations share the same API-process admission controller. The worker gateway credential map (`RUSA_WORKER_INFERENCE_API_KEYS`) must contain one key for each tenant that can execute tasks. In development, the CPU example maps the local development tenant to `local-dev-key`; production must use separate random credentials of at least 32 bytes, store the raw values in the secret manager, and register their SHA-256 digests in `RUSA_API_KEYS_JSON` with the matching tenant and an inference-capable role.

API admission is FIFO with one active request and at most three waiting requests for the 16-GB profile. The 8-GB overlay changes the API waiting limit to one and applies per-service memory/CPU ceilings. The API uses the PostgreSQL-backed admission controller whenever `RUSA_API_DATABASE_URL` is configured, so multiple API replicas share FIFO queue state and active slots. The database scheduler configuration row must match the environment on every replica. The llama.cpp `--parallel 1` setting remains a final active-generation guard, not a replacement for API admission. The API readiness probe uses llama.cpp's `/health` endpoint, which remains unavailable while the model is loading; the worker waits until the API is healthy before starting.

The HTTP API-to-model and worker-to-API routes in this Compose override are for development and staging only. Production API and Worker configuration validation requires credential-free HTTPS; do not bypass that control. A production CPU deployment must provide trusted TLS endpoints for both the model gateway and the worker-facing API gateway, preserve certificate validation, and restrict network access to those endpoints. The llama.cpp service in this development/staging profile has no published host port, is attached only to the private Compose network, and mounts the GGUF file read-only. `--n-gpu-layers 0` explicitly keeps inference on CPU. Without `--profile worker`, the API and model are available but autonomous background task execution is not started.

## 4. Initial CPU tuning

| Setting | Conservative starting value | Guidance |
|---|---:|---|
| CPU threads | 4 (16-GB); 2 (8-GB overlay) | Conservative starting values; verify physical cores and benchmark before increasing. |
| Batch / ubatch | 128 / 64 | Conservative v2.0 starting values; benchmark prompt-processing latency and memory. |
| HTTP threads | 2 | Initial llama.cpp HTTP worker setting. |
| Context size | 4096 | Reduce to 2048 if memory is constrained; never exceed the model's supported context. |
| Parallel slots | 1 | Keep single-request inference initially. |
| API active inferences | 1 | Prevent CPU oversubscription and unbounded latency. |
| Waiting requests | 3 | rusa-cpu-16 limit from v2.0; rusa-edge-8 uses one waiting request. |
| Generation timeout | 300 seconds | Adjust for actual output length and measured tokens/second. |

Increase one setting at a time. CPU throughput depends on model architecture, quantization, CPU instruction set, memory bandwidth, context length, and prompt/output size. These defaults are starting points, not performance guarantees.

## 5. Readiness and smoke test

Wait for the model to finish loading, then check API readiness:

```bash
curl --fail-with-body http://127.0.0.1:8000/health/ready
```

Send a development smoke-test request (the sample `.env.example` maps `local-dev-key` to the development tenant):

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/chat/completions \
  -H 'X-API-Key: local-dev-key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"rusaai-cpu-smoke-test","messages":[{"role":"user","content":"Jelaskan fungsi utama RUSA-AI secara singkat."}],"temperature":0,"max_tokens":128}'
```

For production, use a tenant-bound API key from the secret manager; do not use the development key. Also test streaming, cancellation, long prompts, output limits, and queue saturation against the actual model server.

Inspect logs without enabling prompt or completion logging:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml logs --tail=200 api llama-cpp
```

## 6. Agent and tool compatibility gate

An OpenAI-compatible HTTP endpoint does not by itself prove that a model is suitable for autonomous agents. Before enabling agent workflows, test the selected model's chat template, role handling, structured JSON output, function/tool-call format, refusal behavior, and long-context performance against the RUSA-AI agent contracts. Keep the Policy Engine authoritative; model output must never grant permissions or bypass tool authorization.

Run the existing task, policy, tenant-isolation, and audit tests independently of model smoke tests. A passing chat completion does not certify the agent system.

## 7. Operations and security

- Keep port 8080 private to the Compose network; do not publish it on the public interface.
- Mount the GGUF file read-only and store it outside Git.
- Pin the llama.cpp container image to an immutable digest for production rather than relying on a mutable tag.
- Record the GGUF SHA-256, model revision, image digest, RUSA-AI commit, CPU model, RAM, context size, and thread settings with every benchmark.
- Terminate TLS at the trusted ingress and keep RUSA API authentication, tenant authorization, rate limits, policy checks, and audit enabled.
- Do not place raw credentials in Compose files or source control.
- Monitor host RAM, swap, CPU utilization, request latency, queue wait, 429 responses, timeouts, and model-server restarts.
- Keep model weights and calibration/evaluation datasets out of Git unless their licenses and data governance explicitly permit redistribution.


### Production TLS boundary

The supplied `docker-compose.cpu.yml` override uses plain HTTP between containers on the isolated Compose network and is intended for development or staging. The production API configuration deliberately requires a credential-free HTTPS `RUSA_MODEL_BASE_URL`. For production, place the llama.cpp server behind a trusted internal TLS reverse proxy, configure the API to use that HTTPS endpoint, and install the issuing CA in the API container trust store. Do not weaken the production HTTPS validation to make the development override work.

Production workers must also use the shared HTTPS API gateway; direct worker-to-model access is rejected by production configuration validation because it bypasses API admission. Set `RUSA_WORKER_INFERENCE_BASE_URL` to the trusted HTTPS API endpoint and provide one random credential of at least 32 bytes per task-enabled tenant in `RUSA_WORKER_INFERENCE_API_KEYS`. Register each credential's SHA-256 digest in `RUSA_API_KEYS_JSON` with the same tenant ID and an inference-capable role. Keep raw worker credentials in the secret manager; the hash-only principal registry is safe to pass to the worker for tenant-binding validation.

Set `RUSA_INTERNAL_CA_BUNDLE` to a read-only PEM trust bundle containing the required internal CA certificates (and any public roots needed by the configured endpoints). The production Compose file mounts this bundle into both API and worker containers at `/run/secrets/rusa-internal-ca.crt` and sets `SSL_CERT_FILE`, so upstream model TLS and worker-to-API TLS remain certificate-verified. The worker clears `RUSA_MODEL_API_KEY` and waits for the API health check before it starts processing tasks.

## 8. Rollback and limitations

To stop the CPU model stack, use the same Compose file pair:

```bash
# 16-GB
docker compose -f docker-compose.yml -f docker-compose.cpu.yml down

# 8-GB
docker compose -f docker-compose.yml -f docker-compose.cpu.yml -f docker-compose.cpu-8gb.yml down
```

The GPTQ quantization pipeline remains a separate GPU-only workflow. CPU inference does not quantize models in this deployment; it loads a pre-quantized GGUF artifact. If the selected model is too large for RAM or too slow on the available CPU, choose a smaller compatible GGUF model or move inference to a separately approved model host. Do not silently fall back to an external provider.

RUSA-AI remains a production candidate until the exact model, container image, hardware, API behavior, agent-tool compatibility, load/soak tests, backup/restore, rollback, and operator approval have been evidenced.

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

Merge the values in `.env.cpu.example` into `.env` without overwriting existing secrets. Set `RUSA_GGUF_MODEL_PATH` to the absolute path of the verified GGUF file and `RUSA_GGUF_MODEL_SHA256` to its approved checksum. Run `python scripts/verify_gguf_model.py` before starting Compose. Review the CPU thread count, memory limit, context size, and model name before deployment. For rusa-edge-8 (8-GB), merge `.env.cpu-8gb.example` after `.env.cpu.example`; it reduces the API waiting queue to one request. Keep at least 2 GiB free for the operating system and other services, and lower the llama.cpp container memory cap if the host requires it.

Validate the resolved Compose configuration:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml config
```

For the API, PostgreSQL, web control center, and CPU model server only, run:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build -d
```

For the complete autonomous coding/DevOps workflow, enable the existing worker profile:

```bash
docker compose --profile worker -f docker-compose.yml -f docker-compose.cpu.yml up --build -d
```

The worker profile requires the dedicated rootless Docker socket, workspace and Git mirror paths from `.env.example`, plus the agent sandbox image built on that same rootless Docker daemon. For example, after loading the `.env` values in your shell, build the sandbox with `DOCKER_HOST="unix://${RUSA_DOCKER_SOCKET}" docker build -f Dockerfile.agent-sandbox -t "${RUSA_SANDBOX_IMAGE}" .`. Do not expose or mount the host's rootful Docker socket into the worker.

The CPU override configures both API Gateway and Worker to use `http://llama-cpp:8080/v1`, clears any inherited remote model API key and local Transformers model path, and sets worker parallelism to one by default. API admission is FIFO with one active request and at most three waiting requests for the 16-GB profile. The worker still bypasses the API admission queue; llama.cpp `--parallel 1` enforces the shared active-generation limit at the server. The API readiness probe uses llama.cpp's `/health` endpoint, which remains unavailable while the model is loading; the worker waits until the API is healthy before starting. This HTTP override is for development and staging only. Production API and Worker configuration validation continues to require credential-free HTTPS; do not bypass that control. A production CPU deployment must place llama.cpp behind a trusted TLS model endpoint, preserve certificate validation, and restrict network access to that endpoint. The llama.cpp service in this development/staging profile has no published host port, is attached only to the private Compose network, and mounts the GGUF file read-only. `--n-gpu-layers 0` explicitly keeps inference on CPU. Without `--profile worker`, the API and model are available but autonomous background task execution is not started.

## 4. Initial CPU tuning

| Setting | Conservative starting value | Guidance |
|---|---:|---|
| CPU threads | 4 | Start near the number of physical cores available to the model container; benchmark rather than assuming all logical CPUs improve throughput. |
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
  -d '{"model":"rusaai-edge-1.54b","messages":[{"role":"user","content":"Jelaskan fungsi utama RUSA-AI secara singkat."}],"temperature":0,"max_tokens":128}'
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

## 8. Rollback and limitations

To stop the CPU model stack, use the same Compose file pair:

```bash
docker compose -f docker-compose.yml -f docker-compose.cpu.yml down
```

The GPTQ quantization pipeline remains a separate GPU-only workflow. CPU inference does not quantize models in this deployment; it loads a pre-quantized GGUF artifact. If the selected model is too large for RAM or too slow on the available CPU, choose a smaller compatible GGUF model or move inference to a separately approved model host. Do not silently fall back to an external provider.

RUSA-AI remains a production candidate until the exact model, container image, hardware, API behavior, agent-tool compatibility, load/soak tests, backup/restore, rollback, and operator approval have been evidenced.

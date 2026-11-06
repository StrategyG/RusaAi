# RUSA-AI production readiness

**Status: production candidate implementation; production acceptance is BLOCKED until a real model checkpoint is supplied and the deployment tests in this document pass.** The repository currently contains a model architecture blueprint, not verified RUSA-AI weights.

## What this branch changes

- Removes the synthetic/mock chat completion path. An inference request returns HTTP 503 until a real local checkpoint has loaded.
- Loads a local Hugging Face-compatible causal-LM checkpoint with remote code execution and automatic network downloads disabled.
- Selects BF16/FP16 on supported CUDA devices and FP32 on CPU; uses `device_map=auto`, evaluation mode and inference mode.
- Provides OpenAI-compatible non-streaming chat completions and Server-Sent Events streaming.
- Adds `POST /v1/chat/completions/batch`, which tokenizes a bounded set of prompts together and performs one padded tensor-batch generation call. Batch items share sampling parameters.
- Bounds concurrent inference with a semaphore and queue timeout; returns 429 when capacity is unavailable. Generation has a configured deadline and CUDA out-of-memory failures return a retryable 503.
- Adds request body limits, per-process API rate limiting, request IDs and generic client errors.
- Makes production startup fail closed unless a strong API key and a local model path are configured.
- Makes A2A schema validation fail closed if the schema file is missing or cannot be loaded.
- Corrects the Docker entrypoint and changes CI so lint/test failures are not silently ignored.

## Model checkpoint requirements

The serving code expects a local directory readable by the non-root container user. The checkpoint must be compatible with `transformers.AutoModelForCausalLM` and include the model configuration, tokenizer files and weight shards, for example:

- `config.json`
- `tokenizer_config.json` and `tokenizer.json` or a supported tokenizer model file
- `model.safetensors` or indexed `*.safetensors` shards (or a supported PyTorch checkpoint)
- Any model-specific configuration needed by the installed Transformers version

The loader uses `local_files_only=True` and `trust_remote_code=False`. A model that requires custom remote Python code will not load until that code has been reviewed and deliberately integrated. The checkpoint must have a compatible chat template or accept the documented role-labelled prompt fallback.

The repository's `llm-project/configs/model.yaml` and `blueprint.py` are design specifications only. They do not establish that a 96-layer, 12,288-hidden-size MoE model has been trained, that the stated context window is supported, or that model weights exist. Do not use those blueprint values as benchmark claims.

## Configuration

Copy `.env.example` into a secret-managed runtime environment and replace the placeholder values. Never commit the production secret.

Required production settings:

- `ENVIRONMENT=production`
- `RUSA_API_KEY`: at least 32 characters, generated from a cryptographically secure source
- `RUSA_MODEL_PATH`: absolute path to the mounted checkpoint directory

Important tuning settings:

- `RUSA_MAX_INPUT_TOKENS`: input truncation ceiling; must not exceed the actual checkpoint context window after accounting for output tokens.
- `RUSA_MAX_TOKENS`: maximum generation ceiling.
- `RUSA_MAX_BATCH_SIZE`: maximum items accepted by the explicit batch endpoint.
- `RUSA_MAX_CONCURRENT_REQUESTS`: start at 1 for a single GPU; increase only after measured memory and load tests.
- `RUSA_QUEUE_TIMEOUT_SECONDS`: maximum time waiting for an inference slot.
- `RUSA_GENERATION_TIMEOUT_SECONDS`: Transformers generation deadline, checked between decoding steps; a deadline does not guarantee an immediate interruption during a long GPU kernel.
- `RUSA_MAX_REQUEST_BYTES` and `RUSA_RATE_LIMIT_PER_MINUTE`: API request protections.

## Run locally

Install dependencies in a clean virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Set `ENVIRONMENT=development`, `RUSA_MODEL_PATH=/absolute/path/to/checkpoint`, and an API key if desired, then run:

```bash
uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000
```

For production mode, configure all required secrets before startup. The service will refuse to start if the API key or checkpoint path is missing, and model loading errors will prevent readiness.

## Container deployment

Build the image:

```bash
docker build -t rusa-ai:production .
```

Run with a read-only checkpoint mount and secret-managed environment file:

```bash
docker run --rm --gpus all \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=1g \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --publish 127.0.0.1:8000:8000 \
  --env-file /etc/rusa-ai/production.env \
  --mount type=bind,src=/srv/models/rusa-ai,dst=/models/rusa-ai,readonly \
  rusa-ai:production
```

Ensure the host model directory and files are readable by container UID 10001. On CPU-only hosts, omit `--gpus all`; expect substantially lower throughput. Put the service behind a TLS-terminating ingress or reverse proxy. Do not expose port 8000 directly to the public internet.

The container intentionally runs one Uvicorn worker. Multiple workers would each load a separate copy of the model and can exhaust GPU memory.

## API

- `GET /health/live`: process liveness.
- `GET /health`: service summary, including whether a model is loaded.
- `GET /health/ready`: readiness; returns 503 until a real model is loaded.
- `POST /v1/chat/completions`: OpenAI-compatible chat completion; supports `stream: true` SSE.
- `POST /v1/chat/completions/batch`: bounded non-streaming tensor batch; all requests share temperature, top-p and max-token settings.
- `POST /v1/validate-task`: validates A2A task envelopes against the checked-in JSON Schema.

All `/v1/*` routes require `X-API-Key` when a key is configured. Production startup requires the key. Health routes are unauthenticated for orchestration probes.

Example request:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $RUSA_API_KEY" \
  -d '{"model":"rusa-ai","messages":[{"role":"user","content":"Hello"}],"max_tokens":128}'
```

## Security and tenancy boundaries

This API currently uses one service API key, not per-user OIDC identities or tenant-specific authorization. It does not persist tenant documents or chat history and therefore does not implement PostgreSQL row-level security. Do not treat the shared key as a multi-tenant isolation mechanism. For a shared multi-tenant deployment, place this service behind an authenticated gateway that validates issuer, audience, tenant and project membership, and ensure that downstream storage has database-enforced tenant isolation.

The built-in rate limiter is per process and is a secondary safeguard. Configure distributed rate limiting, TLS, access logging, request timeouts and network policies at the ingress. Never log request bodies, prompts, API keys or model outputs by default.

## Reproducible performance tests

After mounting the actual checkpoint and starting the API on the target hardware, run the direct-runtime benchmark:

```bash
python scripts/benchmark_inference.py --requests 50 --warmup 5 --batch-size 1
python scripts/benchmark_inference.py --requests 50 --warmup 5 --batch-size 4
python scripts/benchmark_inference.py --requests 50 --warmup 5 --stream
```

Run the HTTP load test against the deployed service (the API key is read from the environment and is never printed):

```bash
export RUSA_API_KEY='...'
python scripts/load_test.py --base-url https://your-internal-rusa-host --requests 100 --concurrency 1
python scripts/load_test.py --base-url https://your-internal-rusa-host --requests 100 --concurrency 4
python scripts/load_test.py --base-url https://your-internal-rusa-host --requests 100 --concurrency 4 --stream
```

Keep the JSON output with the checkpoint SHA-256, hardware/driver details, container image digest and environment settings. Compare p50/p95/p99 latency, streaming TTFT, requests/sec, generated tokens/sec, error counts and peak GPU memory. Repeat each scenario at least three times after warm-up. The benchmark scripts are implemented but have not been executed against a real RUSA-AI checkpoint in this repository.

## Verification

Fast, dependency-light API regression tests:

```bash
python -m pip install -r requirements-test.txt
pytest -q
python -m compileall -q llm-project tests
```

The tests verify fail-closed authentication, missing-checkpoint behavior, no mock responses, request validation, and valid/invalid/missing A2A schema behavior. They do not load real model weights.

## Production acceptance gates still required

1. **Checkpoint provenance:** provide the actual RUSA-AI checkpoint, tokenizer, model config, license and SHA-256 manifest. Confirm the architecture is supported by the loader.
2. **Real inference:** load the checkpoint on the target CPU/GPU and verify multiple prompts produce model-generated outputs, not synthetic text.
3. **Streaming:** verify SSE chunk ordering, completion termination, client disconnect behavior and error propagation with the real model.
4. **Batching:** verify batch outputs match independent requests within expected sampling variance; measure batch size 1/2/4/8/16, memory and latency.
5. **Concurrency:** run sustained and burst tests at the target concurrency; verify no OOM, deadlock, starvation or unbounded queue growth.
6. **Performance:** record p50/p95/p99 time-to-first-token, end-to-end latency, tokens/sec, requests/sec, GPU utilization and peak VRAM for a reproducible prompt set.
7. **Security:** run dependency and container scans, secret scanning, penetration tests, and ingress-level distributed rate-limit tests.
8. **Operations:** validate dashboards/alerts, graceful shutdown, model-load failure recovery, deployment rollback and a tested incident runbook.

The explicit batch endpoint is not continuous/dynamic batching across unrelated OpenAI requests. The current Transformers runtime serializes requests by default. If production throughput requires continuous batching, integrate and benchmark a dedicated engine such as vLLM as a separate, version-pinned backend before increasing concurrency.

**Do not mark this service production-ready until all acceptance gates above have evidence from the target deployment.**

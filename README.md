# RUSA-AI

RUSA-AI is an AI model and agent platform under active development. This repository provides an authenticated FastAPI serving API, a local Transformers runtime, an optional vLLM continuous-batching runtime, model-artifact integrity checks, and A2A task validation.

## Verified scope and release status

The serving implementation rejects simulated output and will not report readiness until a real local model artifact has passed manifest and file checksum verification and has loaded successfully. The repository does not include trained RUSA-AI model weights. The actual RUSA-AI checkpoint, license, tokenizer, target GPU/CPU hardware, and deployment secrets must be supplied by the model owner.

**This branch is an inference implementation candidate, not production certification.** No real checkpoint or target hardware was supplied in this work session, so model quality, actual inference, GPU performance, sustained capacity, and production SLOs remain unverified.

## Inference backends

- transformers: local Hugging Face Transformers baseline. Uses CUDA automatically when PyTorch reports a compatible GPU, otherwise CPU. It serializes generation per model process and is not a continuous-batching backend.
- vLLM: optional GPU-oriented runtime with continuous batching, paged KV cache, and async token streaming. Install the separately maintained optional runtime requirements and validate the exact model, CUDA, driver, and GPU combination before staging.
- auto: chooses vLLM when it is installed and CUDA is available; otherwise chooses Transformers.

For production GPU throughput, use vLLM after target-hardware validation. Multiple API replicas must be sized against the GPU memory budget; do not start multiple independent model processes on the same GPU without an explicit capacity plan.

## Model artifact requirements

The API loads only a local model directory. Remote downloads and trust_remote_code are disabled. The directory must contain a compatible causal language model, tokenizer chat template, config.json, and model weights.

Generate a checksummed manifest after placing the approved checkpoint and tokenizer in the model directory:

    python scripts/create_model_manifest.py \
      --model-dir ./models/rusa-ai \
      --model-id rusa-ai \
      --revision <immutable-checkpoint-revision> \
      --license <license-id-or-reference> \
      --tokenizer-id rusa-ai-tokenizer \
      --tokenizer-revision <immutable-tokenizer-revision>

The command prints the manifest SHA-256. Set that exact digest as RUSA_MODEL_MANIFEST_SHA256 in the secret/configuration store. Production startup fails closed if the digest is missing or mismatched. The manifest records checksums for model files and tokenizer artifacts; do not modify any file after generating it.

## Environment configuration

Required:
- RUSA_API_KEY: random service key of at least 32 characters, injected by a secret manager.
- RUSA_MODEL_PATH: local directory containing the approved model artifact.
- RUSA_MODEL_MANIFEST_SHA256: pinned SHA-256 of rusa-model-manifest.json.

Common inference settings:
- RUSA_MODEL_NAME: API model name.
- RUSA_INFERENCE_BACKEND: transformers, vllm, or auto.
- RUSA_MAX_MODEL_LEN: verified maximum context length. Do not set above the checkpoint's supported limit.
- RUSA_GENERATION_TIMEOUT_SECONDS: per-request generation deadline.
- RUSA_MAX_CONCURRENT_REQUESTS, RUSA_MAX_QUEUED_REQUESTS, RUSA_QUEUE_TIMEOUT_SECONDS: bounded admission controls.
- RUSA_MAX_NUM_SEQS, RUSA_MAX_NUM_BATCHED_TOKENS: vLLM continuous-batching limits.
- RUSA_TENSOR_PARALLEL_SIZE, RUSA_GPU_MEMORY_UTILIZATION: vLLM GPU configuration.

See docs/INFERENCE_OPERATIONS.md for the complete deployment and benchmark procedure.

## API endpoints

- GET /health/live: process liveness only.
- GET /health/ready: readiness; returns HTTP 200 only when a verified model, API key, and A2A schema are available.
- GET /v1/models: authenticated list of the loaded model.
- POST /v1/chat/completions: authenticated OpenAI-compatible completion; supports stream=true using Server-Sent Events.
- POST /v1/validate-task: authenticated A2A schema validation; missing or invalid schema fails closed with HTTP 503.

The API returns HTTP 503 when no model is loaded, HTTP 429 when inference admission capacity is exhausted, HTTP 504 on generation timeout, and HTTP 502 for backend failures. It never fabricates a completion.

## Local setup

    python -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt
    python -m pip install -r requirements-dev.txt
    export ENVIRONMENT=development
    export RUSA_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
    export RUSA_MODEL_PATH="$PWD/models/rusa-ai"
    export RUSA_MODEL_MANIFEST_SHA256="<manifest-sha256>"
    uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000

For GPU deployment with vLLM, configure NVIDIA Container Toolkit and a supported driver, then use docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build -d. The GPU override builds the optional vLLM runtime and requests NVIDIA devices. Verify the vLLM, PyTorch, CUDA, driver, and GPU combination on staging before production. The default image remains the Transformers baseline.

## Benchmark

The bounded benchmark uses streaming requests and records latency p50/p95/p99, time-to-first-token, request throughput, completion tokens/second, error rate, model, Git revision, and hardware metadata:

    RUSA_API_KEY="$RUSA_API_KEY" python scripts/benchmark_inference.py \
      --base-url http://127.0.0.1:8000 \
      --requests 20 --concurrency 2 --max-tokens 128 \
      --environment local --output benchmark-report.json

Remote runs require --environment staging --allow-remote-staging. The script intentionally does not support production targets. It is a repeatable smoke benchmark, not a sustained load/soak test.

## Verification

    ruff check llm-project/serve tests scripts
    python -m compileall -q llm-project tests scripts
    pytest -q
    pip-audit -r requirements.txt
    docker compose config

CI also builds and smoke-tests the CPU serving container. A green CI run proves code-level and container checks only; it does not prove the model artifact, GPU runtime, model quality, capacity, or production readiness.

## Release blockers

- [ ] Supply approved RUSA-AI model weights, immutable revision, license, and matching tokenizer/chat template.
- [ ] Generate and pin the model manifest SHA-256 in the production secret/configuration store.
- [ ] Run real inference on the exact artifact and confirm outputs are model-generated.
- [ ] Validate GPU driver/CUDA/PyTorch/vLLM compatibility on target hardware.
- [ ] Benchmark latency, TTFT, throughput, tokens/second, memory, and concurrency against owner-approved SLOs.
- [ ] Run sustained load/soak, overload, timeout, cancellation, and recovery tests.
- [ ] Complete gateway TLS, distributed rate limiting, secret rotation, network policy, monitoring, backup/restore, rollback, and operational sign-off.
- [ ] Obtain human review and approval before merge or production deployment.

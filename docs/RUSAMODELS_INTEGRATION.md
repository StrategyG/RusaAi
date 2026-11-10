# RusaModels inference integration

## Scope

RusaAi can use RusaModels as its OpenAI-compatible inference provider. RusaModels owns model discovery, the model registry, the upstream runtime proxy, and the dedicated inference credential. RusaAi keeps its own task queue, agent orchestration, policy enforcement, audit, and sandbox controls.

This integration uses the RusaModels **Model Server**, not its Quant Server. The OpenAI-compatible base path is `/openai/v1`.

## 1. RusaModels configuration

Use a RusaModels revision that includes the least-privilege `inference` API role.

In the RusaModels environment:

- Set `MODEL_SERVER_HOST_PORT=8100` when RusaAi also publishes its own API on host port 8000.
- Set `RUSAMODELS_INFERENCE_BASE_URL` to the upstream OpenAI-compatible runtime base URL, without a `/v1` suffix. For a local llama.cpp runtime this might be `http://llama-server:8088` on a shared private network.
- Set `RUSAMODELS_INFERENCE_MODEL_ID=qwen-local` (or the exact model alias you will use in RusaAi).
- Set `RUSAMODELS_INFERENCE_API_KEY` only if the upstream runtime requires a bearer key.
- Add a dedicated principal to `RUSAMODELS_API_PRINCIPALS` with `role: inference`. The registry key is SHA-256 of the raw token; keep the raw token in the secret manager.

Example principal (replace the placeholder digest with the computed value):

```json
{
  "<64-character-sha256-digest>": {
    "subject": "rusa-ai-inference",
    "role": "inference"
  }
}
```

The `inference` role may call only `GET /openai/v1/models` and the OpenAI-compatible completion/embedding routes. It cannot create or modify models, start quantization, read audit events, or perform destructive administration. Do not reuse a Manager or Administrator token.

Start RusaModels and verify its Model Server is reachable on host port 8100.

## 2. RusaAi configuration

Set these values in the RusaAi `.env`:

```dotenv
RUSA_MODEL_BASE_URL=http://host.docker.internal:8100/openai/v1
RUSA_MODEL_NAME=qwen-local
RUSA_MODEL_API_KEY=<raw-inference-role-token-from-secret-manager>
```

RusaAi Compose already maps `host.docker.internal` to the Docker host gateway for the API and worker. When running RusaAi directly on the host instead of in Compose, use `http://127.0.0.1:8100/openai/v1` instead.

The `RUSA_MODEL_NAME` value must exactly match `RUSAMODELS_INFERENCE_MODEL_ID`. The CPU-only Compose override intentionally replaces this provider URL with its private llama.cpp service; do not combine the external RusaModels endpoint with that CPU override.

## 3. Verify service authentication and model alias

From the RusaAi host or a network location allowed to reach RusaModels:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $RUSA_MODEL_API_KEY" \
  http://127.0.0.1:8100/openai/v1/models
```

The response should list the configured model alias. Verify that the same inference token is denied on management endpoints:

```bash
curl -i -H "Authorization: Bearer $RUSA_MODEL_API_KEY" \
  http://127.0.0.1:8100/v1/models
```

The second request must return HTTP 403.

Then start RusaAi and check `GET /health/ready`. A readiness response proves only that the configured provider endpoint is reachable according to the RusaAi readiness probe; it does not certify model quality, capacity, safety, or production suitability. Run controlled chat and streaming smoke tests before enabling agent execution.

## 4. Production boundary

- Use HTTPS for the RusaModels endpoint and configure the RusaModels upstream with HTTPS and its required upstream API key.
- Store the raw inference token and upstream runtime key only in the secret manager.
- Rotate the inference token independently of model-management credentials.
- Restrict network access so only RusaAi API/worker workloads can reach the Model Server.
- Do not place prompts, completions, API keys, or authorization headers in logs.
- Keep RusaAi AICode and DevOps tool permissions independent from the inference principal. A model response is untrusted input and never grants authorization.
- Do not enable production AICode workers or DevOps workers until model artifact, evaluation, staging, tenant-isolation, and operator-approval gates are complete.

A working HTTP connection is an integration smoke test, not production certification.

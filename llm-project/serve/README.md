# Serving and runtime

The FastAPI service loads a local Transformers causal-language-model checkpoint with safetensors weights and a tokenizer. Use a checkpoint that you trust. The loader uses CUDA when available and CPU otherwise. Use one process per model instance.

From the repository root, install dependencies and start the API.

```bash
python -m pip install -r requirements.txt
export RUSA_MODEL_PATH=/absolute/path/to/checkpoint
export RUSA_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m uvicorn app:app --app-dir llm-project/serve --host 127.0.0.1 --port 8000
```

| Setting | Default | Purpose |
| --- | --- | --- |
| `RUSA_MODEL_PATH` | Unset | Local checkpoint directory |
| `RUSA_MODEL_NAME` | `rusa-ai-v1` | Accepted API model identifier |
| `RUSA_MAX_MODEL_LEN` | `8192` | Prompt and output token budget, capped by the checkpoint's position limit when available |
| `RUSA_MAX_REQUEST_BYTES` | `1048576` | Actual chat body bytes, including chunked requests |
| `RUSA_GENERATION_TIMEOUT_SECONDS` | `120` | Deadline covering tokenization and generation |

Check `GET /health/live` for process liveness and `GET /health/ready` for model readiness. The server remains live while the checkpoint loads. The existing `GET /health` response remains a liveness probe.

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "X-API-Key: ${RUSA_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"rusa-ai-v1","messages":[{"role":"user","content":"Hello"}],"max_tokens":32,"stream":true}'
```

With `stream: true`, the server emits an assistant role chunk, content chunks, a finish reason, and `[DONE]`. A timeout before SSE headers returns HTTP 504. A timeout after headers sends an SSE error and closes the stream. Disconnect signals stop generation at the next Transformers stopping-criteria check. The process keeps the inference slot until the worker exits.

Chat returns HTTP 503 while the checkpoint is unavailable, 404 for an unknown model, 429 while the inference slot is busy, and 502 on backend errors before streaming starts. Oversized chat bodies return 413. Requests that exceed the context window return 400. `max_tokens` accepts 1 through 4096.

The smoke test creates and reloads a small GPT-2 checkpoint on CPU from a synthetic training example. It compares the generated text from a direct model call, the JSON API, and SSE. Run it with:

```bash
python -m pytest -q -s tests/test_inference.py::test_real_transformers_checkpoint
```

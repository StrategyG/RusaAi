# RusaModels — Model Plane boundary

RusaModels owns model identity and artifact metadata, model loading and
residency, inference scheduling, CPU resource policy, and the model-serving
runtime. It does not own agent workflow state or tool authorization.

## Current implementation mapping

- CPU-only llama.cpp server: docker-compose.cpu.yml
- GGUF checksum verification: rusa_agents/gguf.py and scripts/verify_gguf_model.py
- OpenAI-compatible model gateway adapter: rusa_agents/providers.py
- API-side inference admission: rusa_agents/inference_admission.py and
  llm-project/serve/app.py
- Design target: llm-project/configs/model.yaml and
  llm-project/configs/inference.yaml

The model file must remain outside Git and be mounted read-only. The CPU
baseline uses GGUF Q4_K_M, llama.cpp, zero GPU-offload layers, and one active
generation. The API admission queue is bounded and FIFO; llama.cpp parallel
slots provide the final single-generation guard for requests that reach the
server from more than one process.

## Explicitly not implemented yet

No non-Qwen RusaAi-Edge foundation checkpoint or tokenizer has been selected.
No RusaAi-Edge weights or GGUF release artifact have been built. Training and
device benchmarks have not started. A full cross-process scheduler shared by
API and worker callers, a model registry, and a model-residency manager remain
future work. Do not describe this directory or the design YAML as a runnable
model implementation.

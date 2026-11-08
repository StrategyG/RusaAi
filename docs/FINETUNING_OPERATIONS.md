# RUSA-AI open-model fine-tuning operations

## Status and scope

The repository now has an initial SFT/QLoRA pipeline targeting the existing AICode `AutonomousCodeWorker` contract. This is a **training implementation**, not a claim that RUSA-AI weights have already been trained, benchmarked, or production-certified.

The first candidate is `Qwen/Qwen3-4B-Instruct-2507` (Apache-2.0). Its model card describes a 4.0B-parameter causal LM, multilingual capability, coding/tool-use improvements, and a native 262,144-token context. The initial fine-tuning window is deliberately capped at 4,096 tokens to control memory and keep the agent task context bounded. The model revision is pinned in `llm-project/configs/training.yaml`.

Training adapts response quality and structured action selection. It does **not** grant permissions. `AutonomousCodeWorker`, the shared role/tool registry, `WorkspaceToolGateway` (which independently checks the role allowlist), `AutonomyPolicy`, tenant authorization, and the repository sandbox remain the deterministic enforcement boundaries.

## Pipeline

1. Curate and redact JSONL examples for the six implemented AICode worker roles: analyst, architect, developer, debugger, tester, and security.
2. Validate every record against the SFT contract and role-specific tool allowlist. Reject duplicate content and train/eval leakage.
3. Tokenize the system/user prompt using the base model chat template. Apply loss only to the expected assistant JSON response; prompt labels are masked with `-100`.
4. Load the base model in 4-bit NF4 and train LoRA adapters with gradient checkpointing and an 8-bit paged optimizer.
5. Evaluate generated responses on a held-out split. Gate JSON validity, runtime contract validity, role-scoped tool permissions, and blocked-action recall.
6. Merge the adapter into the base model, save safetensors, and create a checksum manifest.
7. Run local/staging inference and benchmark the exact merged artifact before any production decision.

## Dataset contract

Each JSONL line matches `schemas/llm/agent-sft-example.schema.json`:

```json
{
  "id": "security-0001",
  "agent_role": "security",
  "messages": [
    {"role": "system", "content": "You are the RUSA-AI security worker. Return one JSON object only."},
    {"role": "user", "content": "Inspect the application dependency manifest for risk."}
  ],
  "response": {
    "message": "I will inspect the permitted dependency manifest.",
    "actions": [{"name": "read_file", "arguments": {"path": "requirements.txt"}}],
    "done": true,
    "blocked": false,
    "blockers": []
  }
}
```

The response keys mirror `AutonomousCodeWorker._parse_response`: `message`, `actions`, `done`, optional `blocked`, and optional `blockers`. Do not add free-form tool names, shell commands, evidence claims, or approval decisions. For blocked examples, set `done=true`, `blocked=true`, and `actions=[]`.

The configured minimum is 100 training examples and 30 held-out evaluation examples. Both splits must cover all six roles, and the evaluation split must contain at least 10% explicit blocked/denial cases and at least 50% unblocked cases so both safety recall and false-positive rate are measurable. These are guardrails against accidentally training or evaluating on a handful of examples, not guarantees of model quality. For an experimental smoke run only, `--allow-small-dataset` bypasses the count and blocked-fraction minimums; such an adapter must not be promoted.

Recommended first dataset target (planning guidance, not a quality guarantee): 1,000-3,000 reviewed examples distributed across the six roles, with at least 10-15% explicit blocked/denial cases and separate held-out scenarios. Use real task/evidence pairs only when source licensing and repository-owner authorization are clear. Keep train/eval examples disjoint by scenario, not merely by line.

## Compute requirements

These are initial capacity estimates for Qwen3-4B QLoRA, not measured guarantees. Actual peak VRAM depends on sequence length, model revision, CUDA/PyTorch/bitsandbytes versions, activation checkpointing, and data distribution.

| Resource | Minimum experimental target | Recommended starting point |
|---|---:|---:|
| GPU | 1 NVIDIA CUDA GPU, 16 GB VRAM; 2K tokens, batch 1 | 1 NVIDIA GPU, 24 GB VRAM; 4K tokens, batch 1 |
| System RAM | 32 GB | 64 GB |
| Free disk | 40 GB | 80 GB or more for checkpoints and merged weights |
| CPU | 8 modern cores | 12-16 cores |
| Runtime | Linux x86-64; compatible NVIDIA driver, CUDA, PyTorch and bitsandbytes | Same, with BF16-capable GPU |

Configuration starts with NF4 double quantization, BF16 compute when supported (FP16 fallback), LoRA rank 16, batch size 1, gradient accumulation 16, gradient checkpointing, and 4K maximum sequence length. On a 16 GB card, reduce the sequence length to 2K and benchmark before increasing it. An 8 GB GPU is not a supported starting target for this 4B configuration; select a smaller model or rent a dedicated GPU instead.

The merged BF16 serving artifact is approximately 8 GB of raw model weights before tokenizer, metadata, and runtime/KV-cache overhead. Plan on at least 16 GB VRAM for a low-concurrency Transformers serving baseline; 24 GB or more is a safer initial target for vLLM and concurrent requests. These are sizing estimates only; use `scripts/benchmark_inference.py` and the existing inference runbook to establish actual capacity.

No training duration is promised before a real GPU benchmark. Record GPU model/VRAM, driver, CUDA, PyTorch, bitsandbytes, dataset hashes, token counts, step count, peak allocated/reserved VRAM, training throughput, and evaluation metrics in the run report.

## Install and run

Use a dedicated training host/environment. Do not install training dependencies or model weights into the API container.

```bash
python -m venv .venv-train
source .venv-train/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-train.txt

python llm-project/train/validate_dataset.py \
  --train-file llm-project/data/curated/train.jsonl \
  --eval-file llm-project/data/curated/eval.jsonl

python llm-project/train/train_sft.py

python llm-project/eval/evaluate_agent_sft.py \
  --adapter-dir artifacts/rusa-agent-qwen3-4b-qlora/adapter
```

The trainer refuses CPU execution and refuses fewer than 100 train examples unless `--allow-small-dataset` is explicitly supplied. It records train/eval SHA-256 hashes, the configured/resolved base model revision, software versions, GPU details, hyperparameters, and training/evaluation metrics in `rusa-finetune-provenance.json`.

## Merge and serve

```bash
python llm-project/train/merge_adapter.py \
  --adapter-dir artifacts/rusa-agent-qwen3-4b-qlora/adapter \
  --output-dir artifacts/rusa-agent-qwen3-4b-merged

python scripts/create_model_manifest.py \
  --model-dir artifacts/rusa-agent-qwen3-4b-merged \
  --model-id Qwen/Qwen3-4B-Instruct-2507+rusa-agent-sft \
  --revision eb25fbe4f35f7147763bc24445679d1c00588d89 \
  --license apache-2.0 \
  --tokenizer-id Qwen/Qwen3-4B-Instruct-2507 \
  --tokenizer-revision eb25fbe4f35f7147763bc24445679d1c00588d89
```

Copy the merged artifact to a read-only model volume. Configure `RUSA_LOCAL_MODEL_PATH` and pin the exact printed manifest SHA-256 in `RUSA_MODEL_MANIFEST_SHA256`. The current local inference runtime already verifies the manifest, file hashes, tokenizer files, and safetensors before loading. Never commit adapter weights, merged weights, or private datasets to Git.

## Promotion gates

A training run is not a production release. Before promotion:

- Review data provenance, license, redaction, and scenario coverage.
- Require 100% held-out response-contract validity (including role/tool permission checks).
- Require blocked-action recall of at least 90% and blocked false-positive rate no greater than 10% on the configured held-out split.
- Add human review of task success and output quality; the current automatic evaluator does not establish semantic correctness.
- Run prompt-injection, malicious repository-content, tenant-isolation, and tool-side-effect tests.
- Verify the merged artifact with a pinned manifest digest.
- Run local/staging inference, streaming/cancellation, load, and soak tests with the exact artifact and hardware.
- Complete the repository's production certification, rollback, backup/restore, monitoring, and operator approval gates.

A model may propose an action; only the deterministic runtime and policy layer can authorize it. Merge, deploy, release, secret changes, permission changes, and destructive operations remain human-controlled.


## Manual GitHub Actions GPU training

The repository includes a manually triggered workflow at `.github/workflows/agent-sft-training.yml`. It is deliberately not triggered by pushes or pull requests.

Before running it:

1. Register a trusted self-hosted GitHub Actions runner with the labels `self-hosted`, `linux`, `x64`, `cuda`, and `gpu`.
2. Provide an NVIDIA CUDA driver, a CUDA-compatible PyTorch/bitsandbytes stack, at least 24 GiB GPU VRAM for the committed 4,096-token configuration, and sufficient host RAM and disk (64 GiB RAM and 80 GiB free disk are the recommended starting point).
3. Configure the GitHub Actions environment `rusa-model-training` with required reviewers if GPU usage needs approval.
4. Open Actions → **RUSA-AI Agent SFT GPU Training** → Run workflow, and explicitly set `confirm_gpu_run=true`.

The workflow installs the isolated training dependencies, checks GPU capacity, validates the train/evaluation splits, runs QLoRA, and runs the held-out contract/safety evaluator. The evaluator fails the workflow if the configured promotion metrics are not met. An adapter and evaluation report are uploaded as a short-lived **unpromoted candidate artifact** for review; they are not committed to Git, merged into a base model, deployed, or certified for production.

The workflow requires a runner with the exact labels above. If no eligible runner is online, the job remains queued; no CPU fallback is attempted.


## NVIDIA T4 16 GB experiment profile

Use `llm-project/configs/training-t4.yaml` for an NVIDIA Tesla T4. This profile uses 4-bit NF4, FP16 compute, a 2,048-token sequence window, batch size 1, gradient checkpointing, LoRA rank 8, and zero dataloader workers. It is an experimental starting point; GPU memory pressure depends on the installed CUDA/PyTorch/bitsandbytes stack and actual tokenized examples. If CUDA OOM occurs, run a separately reviewed experiment with a 1,024-token window and LoRA rank 4.

The manual workflow requires a trusted self-hosted GitHub Actions runner registered with labels `self-hosted`, `linux`, `x64`, `cuda`, `gpu`, and `t4`. The workflow verifies that the visible device name is NVIDIA T4 and that at least 14 GiB VRAM is available. Register the runner on the machine that owns the T4; do not expose a runner with access to untrusted repositories or pull requests.

To run: open Actions → **RUSA-AI Agent SFT GPU Training** → Run workflow, check `confirm_gpu_run=true`, and start the job. The run validates data, trains the adapter, evaluates the held-out set, and uploads a short-lived unpromoted candidate artifact. It does not automatically merge weights into the base model or deploy them.

A T4 in a separate Colab/Kaggle session is not automatically connected to GitHub Actions. In that case, run the same training command inside that GPU notebook/runtime after checking out this repository and installing `requirements-train.txt`.

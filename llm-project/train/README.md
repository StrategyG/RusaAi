# RUSA-AI supervised fine-tuning

Status: **implemented training pipeline; no trained RUSA-AI weights yet**.

The first training target is the existing `AutonomousCodeWorker` JSON action envelope. The base model is Qwen3-4B-Instruct-2507; the training method is 4-bit NF4 QLoRA with a completion-only loss. The model is not allowed to grant itself tools or change the deterministic gateway/policy boundary.

## Commands

```bash
# Dedicated Linux/NVIDIA CUDA training host
python -m venv .venv-train
source .venv-train/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-train.txt

# Validate the reviewed train/eval split before training
python llm-project/train/validate_dataset.py \
  --train-file llm-project/data/curated/train.jsonl \
  --eval-file llm-project/data/curated/eval.jsonl

# Train the adapter
python llm-project/train/train_sft.py

# Evaluate generated agent envelopes on the held-out set
python llm-project/eval/evaluate_agent_sft.py \
  --adapter-dir artifacts/rusa-agent-qwen3-4b-qlora/adapter

# Merge adapter for the existing local inference runtime
python llm-project/train/merge_adapter.py \
  --adapter-dir artifacts/rusa-agent-qwen3-4b-qlora/adapter \
  --output-dir artifacts/rusa-agent-qwen3-4b-merged

# Create a checksummed artifact manifest (weights stay outside Git)
python scripts/create_model_manifest.py \
  --model-dir artifacts/rusa-agent-qwen3-4b-merged \
  --model-id Qwen/Qwen3-4B-Instruct-2507+rusa-agent-sft \
  --revision eb25fbe4f35f7147763bc24445679d1c00588d89 \
  --license apache-2.0 \
  --tokenizer-id Qwen/Qwen3-4B-Instruct-2507 \
  --tokenizer-revision eb25fbe4f35f7147763bc24445679d1c00588d89
```

Set `RUSA_LOCAL_MODEL_PATH` to the merged artifact directory and pin the printed digest in `RUSA_MODEL_MANIFEST_SHA256`. Production uses the existing fail-closed artifact verification in `llm-project/serve/model_runtime.py`.

See [Fine-tuning operations](../../docs/FINETUNING_OPERATIONS.md) for dataset policy, hardware sizing, evaluation gates, and deployment boundaries.

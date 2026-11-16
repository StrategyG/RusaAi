# Experimental standalone trainer

This directory contains an uploaded standalone training experiment. It is not a replacement for the canonical repository pipeline at `llm-project/train/train_sft.py`.

The standalone script has its own JSONL validation schema and a NumPy CPU fallback that trains a small surrogate adapter; that fallback does **not** fine-tune Qwen3-4B. The canonical RUSA-AI pipeline enforces the runtime tool contract in `rusa_training/dataset.py` and requires a CUDA GPU for QLoRA. Validate any dataset against the canonical schema before using it for a production adapter.

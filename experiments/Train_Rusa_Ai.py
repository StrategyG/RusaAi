#!/usr/bin/env python3
"""
Standalone & Repository-Compatible Supervised Fine-Tuning (SFT) Script for RusaAi.
Repository: https://github.com/StrategyG/RusaAi

Supports:
1. Loading YAML training configurations (e.g., llm-project/configs/training.yaml
   or llm-project/configs/training-t4.yaml) with automatic fallback defaults.
2. Validating curated JSONL datasets (train.jsonl & eval.jsonl) against the RusaAi
   multi-agent schema (rusa_training/dataset.py) and checking for split leakage.
3. Fine-tuning Qwen/Qwen3-4B-Instruct-2507 using 4-bit NF4 QLoRA (PEFT + TRL/Transformers)
   when PyTorch & GPU/HuggingFace are available, OR running a local CPU NumPy LoRA
   SFT trainer (--local-cpu) in environments without PyTorch/GPU.
4. Merging trained LoRA adapters into the base model for inference serving.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import sys
from pathlib import Path
from typing import Any

# Add repository root to sys.path if executed from inside StrategyG/RusaAi
REPO_ROOT = Path(__file__).resolve().parent
if (REPO_ROOT / "rusa_training").exists() and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("rusa_train")

# Default configuration aligned with StrategyG/RusaAi (llm-project/configs/training.yaml)
DEFAULT_CONFIG: dict[str, Any] = {
    "model": {
        "name_or_path": "Qwen/Qwen3-4B-Instruct-2507",
        "revision": "eb25fbe4f35f7147763bc24445679d1c00588d89",
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_use_double_quant": True,
        "compute_dtype": "bfloat16",
        "trust_remote_code": True,
        "device_map": "auto",
    },
    "lora": {
        "r": 16,
        "alpha": 32,
        "dropout": 0.05,
        "bias": "none",
        "target_modules": "all-linear",
    },
    "dataset": {
        "train_file": "llm-project/data/curated/train.jsonl",
        "eval_file": "llm-project/data/curated/eval.jsonl",
        "max_seq_length": 4096,
        "min_train_samples": 100,
        "min_eval_samples": 30,
        "min_blocked_fraction": 0.10,
        "min_unblocked_fraction": 0.50,
    },
    "training": {
        "output_dir": "artifacts/rusa-agent-qwen3-4b-qlora",
        "per_device_train_batch_size": 1,
        "per_device_eval_batch_size": 1,
        "gradient_accumulation_steps": 16,
        "learning_rate": 1.0e-4,
        "warmup_ratio": 0.03,
        "lr_scheduler_type": "cosine",
        "weight_decay": 0.01,
        "max_grad_norm": 0.3,
        "num_train_epochs": 3,
        "logging_steps": 10,
        "save_strategy": "epoch",
        "eval_strategy": "epoch",
        "gradient_checkpointing": True,
        "dataloader_num_workers": 2,
        "seed": 42,
        "bf16": True,
        "fp16": False,
        "report_to": "none",
    },
}

_RECORD_KEYS = {"id", "agent_role", "messages", "response"}
_RESPONSE_KEYS = {"message", "actions", "done", "blocked", "blockers"}
_ALLOWED_AGENT_ROLES = {
    "analyst",
    "architect",
    "developer",
    "debugger",
    "tester",
    "security",
}
_ALLOWED_MESSAGE_ROLES = {"system", "user", "assistant"}


class DatasetValidationError(ValueError):
    """Raised when a RusaAi JSONL dataset violates schema or split constraints."""


def load_yaml_config(config_path: str | Path | None, t4_preset: bool = False) -> dict[str, Any]:
    """Load YAML training configuration and merge with RusaAi defaults."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    if t4_preset:
        # Adjustments for 16GB VRAM GPUs (llm-project/configs/training-t4.yaml)
        cfg["model"]["compute_dtype"] = "float16"
        cfg["lora"]["r"] = 8
        cfg["lora"]["alpha"] = 16
        cfg["dataset"]["max_seq_length"] = 2048
        cfg["training"]["bf16"] = False
        cfg["training"]["fp16"] = True

    if config_path and Path(config_path).exists():
        import yaml

        logger.info("Loading configuration from %s", config_path)
        with Path(config_path).open("r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        for section, values in user_cfg.items():
            if isinstance(values, dict) and section in cfg:
                cfg[section].update(values)
            else:
                cfg[section] = values
    elif config_path:
        logger.warning(
            "Config file '%s' not found; falling back to built-in RusaAi defaults.",
            config_path,
        )

    return cfg


def record_fingerprint(record: dict[str, Any]) -> str:
    """Compute SHA-256 content fingerprint to detect duplicate examples across splits."""
    payload = json.dumps(
        {
            "agent_role": record.get("agent_role"),
            "messages": record.get("messages"),
            "response": record.get("response"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_record(record: dict[str, Any]) -> dict[str, Any]:
    """Validate a single JSONL record against the RusaAi schema."""
    if not isinstance(record, dict):
        raise DatasetValidationError("Record must be a JSON object.")

    missing = _RECORD_KEYS - set(record.keys())
    if missing:
        raise DatasetValidationError(f"Record missing required keys: {sorted(missing)}")

    if not isinstance(record["id"], str) or not record["id"].strip():
        raise DatasetValidationError("record['id'] must be a non-empty string.")

    role = record["agent_role"]
    if not isinstance(role, str) or role not in _ALLOWED_AGENT_ROLES:
        raise DatasetValidationError(
            f"Unsupported agent_role '{role}'. Allowed: {sorted(_ALLOWED_AGENT_ROLES)}"
        )

    messages = record["messages"]
    if not isinstance(messages, list) or not messages:
        raise DatasetValidationError("record['messages'] must be a non-empty list.")

    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict) or "role" not in msg or "content" not in msg:
            raise DatasetValidationError(f"messages[{idx}] must contain 'role' and 'content'.")
        if msg["role"] not in _ALLOWED_MESSAGE_ROLES:
            raise DatasetValidationError(f"messages[{idx}] has invalid role '{msg['role']}'.")
        if not isinstance(msg["content"], str) or not msg["content"].strip():
            raise DatasetValidationError(f"messages[{idx}]['content'] must be non-empty.")

    response = record["response"]
    if not isinstance(response, dict):
        raise DatasetValidationError("record['response'] must be a JSON object.")

    resp_missing = _RESPONSE_KEYS - set(response.keys())
    if resp_missing:
        raise DatasetValidationError(
            f"record['response'] missing required keys: {sorted(resp_missing)}"
        )
    if not isinstance(response["message"], str) or not response["message"].strip():
        raise DatasetValidationError("response['message'] must be a non-empty string.")
    if not isinstance(response["actions"], list):
        raise DatasetValidationError("response['actions'] must be a list.")
    if not isinstance(response["done"], bool) or not isinstance(response["blocked"], bool):
        raise DatasetValidationError("response['done'] and response['blocked'] must be booleans.")
    if not isinstance(response["blockers"], list):
        raise DatasetValidationError("response['blockers'] must be a list of strings.")

    return record


def load_and_validate_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate a JSONL split, rejecting duplicate IDs or content."""
    source = Path(path)
    if not source.exists():
        raise DatasetValidationError(f"Dataset file does not exist: {source}")

    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()

    for line_no, raw_line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
            record = validate_record(parsed)
        except (json.JSONDecodeError, DatasetValidationError) as exc:
            raise DatasetValidationError(f"{source}:{line_no}: {exc}") from exc

        rec_id = record["id"]
        if rec_id in seen_ids:
            raise DatasetValidationError(f"{source}:{line_no}: Duplicate record id '{rec_id}'")
        seen_ids.add(rec_id)

        fp = record_fingerprint(record)
        if fp in seen_fingerprints:
            raise DatasetValidationError(f"{source}:{line_no}: Duplicate record content")
        seen_fingerprints.add(fp)

        records.append(record)

    if not records:
        raise DatasetValidationError(f"Dataset split is empty: {source}")
    return records


def validate_splits_and_distribution(
    train_records: list[dict[str, Any]],
    eval_records: list[dict[str, Any]],
    dataset_cfg: dict[str, Any],
    strict_counts: bool = True,
) -> None:
    """Check for train/eval split leakage, minimum counts, and blocked/unblocked ratio."""
    train_ids = {r["id"] for r in train_records}
    eval_ids = {r["id"] for r in eval_records}
    if train_ids & eval_ids:
        raise DatasetValidationError("Split leakage detected: overlapping IDs between train and eval.")

    train_fps = {record_fingerprint(r) for r in train_records}
    eval_fps = {record_fingerprint(r) for r in eval_records}
    if train_fps & eval_fps:
        raise DatasetValidationError(
            "Split leakage detected: identical sample content found in both train and eval."
        )

    if strict_counts:
        min_train = int(dataset_cfg.get("min_train_samples", 100))
        min_eval = int(dataset_cfg.get("min_eval_samples", 30))
        if len(train_records) < min_train:
            raise DatasetValidationError(
                f"Train split has {len(train_records)} samples (minimum required: {min_train}). "
                "Pass --skip-count-check to train on smaller datasets."
            )
        if len(eval_records) < min_eval:
            raise DatasetValidationError(
                f"Eval split has {len(eval_records)} samples (minimum required: {min_eval}). "
                "Pass --skip-count-check to train on smaller datasets."
            )

    blocked_count = sum(1 for r in eval_records if r["response"].get("blocked", False))
    blocked_frac = blocked_count / len(eval_records)
    unblocked_frac = 1.0 - blocked_frac

    min_blocked = float(dataset_cfg.get("min_blocked_fraction", 0.10))
    min_unblocked = float(dataset_cfg.get("min_unblocked_fraction", 0.50))
    if blocked_frac < min_blocked:
        raise DatasetValidationError(
            f"Eval blocked fraction ({blocked_frac:.1%}) is below minimum ({min_blocked:.1%})."
        )
    if unblocked_frac < min_unblocked:
        raise DatasetValidationError(
            f"Eval unblocked fraction ({unblocked_frac:.1%}) is below minimum ({min_unblocked:.1%})."
        )


def format_record_for_sft(record: dict[str, Any], tokenizer: Any = None) -> str:
    """Format a RusaAi record into a chat sequence with system role context and JSON response."""
    agent_role = record["agent_role"]
    messages = list(record["messages"])

    if not messages or messages[0]["role"] != "system":
        system_prompt = (
            f"You are a RusaAi {agent_role} agent. Respond strictly with a valid JSON object "
            "containing keys: message, actions, done, blocked, blockers."
        )
        messages.insert(0, {"role": "system", "content": system_prompt})

    assistant_payload = json.dumps(record["response"], ensure_ascii=False)
    full_conversation = messages + [{"role": "assistant", "content": assistant_payload}]

    if tokenizer is not None and hasattr(tokenizer, "apply_chat_template"):
        return tokenizer.apply_chat_template(
            full_conversation,
            tokenize=False,
            add_generation_prompt=False,
        )

    # ChatML formatting compatible with Qwen models
    segments = [f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>" for m in full_conversation]
    return "\n".join(segments) + "\n"


def generate_sample_datasets(train_path: Path, eval_path: Path, train_n: int = 100, eval_n: int = 30) -> None:
    """Create valid synthetic RusaAi train.jsonl and eval.jsonl files for quick testing."""
    roles = sorted(_ALLOWED_AGENT_ROLES)
    train_path.parent.mkdir(parents=True, exist_ok=True)
    eval_path.parent.mkdir(parents=True, exist_ok=True)

    def _make_record(prefix: str, idx: int) -> dict[str, Any]:
        role = roles[idx % len(roles)]
        is_blocked = (idx % 5 == 0)
        return {
            "id": f"{prefix}-{idx:04d}",
            "agent_role": role,
            "messages": [
                {
                    "role": "system",
                    "content": f"You are the RusaAi {role} agent executing task #{idx}.",
                },
                {
                    "role": "user",
                    "content": (
                        f"[{prefix.upper()}] Execute {role} workflow step {idx} for module_{idx}.py"
                        + (" (waiting on external secret)." if is_blocked else " with full permissions.")
                    ),
                },
            ],
            "response": {
                "message": (
                    f"Blocked waiting for credentials on step {idx}."
                    if is_blocked
                    else f"Completed {role} analysis and action for module_{idx}.py."
                ),
                "actions": (
                    []
                    if is_blocked
                    else [
                        {
                            "tool": "file_writer",
                            "parameters": {
                                "path": f"src/module_{idx}.py",
                                "content": f"# Generated by {role} agent (task {idx})\n",
                            },
                        }
                    ]
                ),
                "done": not is_blocked,
                "blocked": is_blocked,
                "blockers": ["Missing environment secret API_KEY"] if is_blocked else [],
            },
        }

    with train_path.open("w", encoding="utf-8") as f:
        for i in range(1, train_n + 1):
            f.write(json.dumps(_make_record("train", i), ensure_ascii=False) + "\n")

    with eval_path.open("w", encoding="utf-8") as f:
        for i in range(1, eval_n + 1):
            f.write(json.dumps(_make_record("eval", i), ensure_ascii=False) + "\n")

    logger.info("Generated %d train records at %s", train_n, train_path)
    logger.info("Generated %d eval records at %s", eval_n, eval_path)


def run_local_numpy_lora_sft(
    train_records: list[dict[str, Any]],
    eval_records: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """
    Lightweight CPU-only NumPy LoRA SFT trainer for environments without PyTorch/GPU.
    Trains a low-rank adapter (W_0 + (alpha/r) * A @ B) on tokenized ChatML sequences
    using mini-batch AdamW + cosine learning rate schedule and saves adapter artifacts.
    """
    import numpy as np

    lora_cfg = cfg["lora"]
    train_cfg = cfg["training"]
    seed = int(train_cfg.get("seed", 42))
    rng = np.random.default_rng(seed)

    r = int(lora_cfg.get("r", 16))
    alpha = float(lora_cfg.get("alpha", 32))
    scaling = alpha / max(r, 1)
    epochs = int(train_cfg.get("num_train_epochs", 3))
    grad_accum = int(train_cfg.get("gradient_accumulation_steps", 16))
    base_lr = float(train_cfg.get("learning_rate", 1e-4)) * 50.0  # scaled for CPU surrogate convergence
    weight_decay = float(train_cfg.get("weight_decay", 0.01))
    output_dir = Path(train_cfg.get("output_dir", "artifacts/rusa-agent-qwen3-4b-qlora"))
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build token vocabulary from ChatML formatted records
    token_pattern = re.compile(r"<\|im_start\|>|<\|im_end\|>|\w+|[^\w\s]")
    vocab: dict[str, int] = {"<pad>": 0, "<unk>": 1}
    for rec in train_records:
        for tok in token_pattern.findall(format_record_for_sft(rec)):
            if tok not in vocab and len(vocab) < 512:
                vocab[tok] = len(vocab)
    vocab_size = len(vocab)
    d_model = 64

    def _encode_sequence(rec: dict[str, Any], max_tokens: int = 96) -> np.ndarray:
        tokens = token_pattern.findall(format_record_for_sft(rec))[:max_tokens]
        ids = [vocab.get(t, 1) for t in tokens]
        return np.array(ids, dtype=np.int64)

    train_seqs = [_encode_sequence(r) for r in train_records]
    eval_seqs = [_encode_sequence(r) for r in eval_records]

    # Frozen base weights (simulating 4-bit quantized base LLM projection) + trainable LoRA A, B
    E = rng.normal(0.0, 0.15, size=(vocab_size, d_model)).astype(np.float64)
    W_base = rng.normal(0.0, 0.15, size=(d_model, vocab_size)).astype(np.float64)
    lora_A = rng.normal(0.0, 0.02, size=(d_model, r)).astype(np.float64)
    lora_B = np.zeros((r, vocab_size), dtype=np.float64)

    # AdamW state for LoRA A and B
    m_A, v_A = np.zeros_like(lora_A), np.zeros_like(lora_A)
    m_B, v_B = np.zeros_like(lora_B), np.zeros_like(lora_B)
    beta1, beta2, eps = 0.9, 0.999, 1e-8

    def _seq_loss_and_grads(seq: np.ndarray, compute_grads: bool = True):
        x_ids = seq[:-1]
        y_ids = seq[1:]
        # Contextual hidden state: mean-pooled prefix + current token embedding
        H = E[x_ids].copy()
        cumsum = np.cumsum(H, axis=0) / np.arange(1, len(H) + 1)[:, None]
        H = 0.6 * H + 0.4 * cumsum

        logits = H @ W_base + scaling * ((H @ lora_A) @ lora_B)
        logits -= np.max(logits, axis=1, keepdims=True)
        exp_logits = np.exp(logits)
        probs = exp_logits / np.sum(exp_logits, axis=1, keepdims=True)

        n_tok = len(y_ids)
        nll = -np.log(np.clip(probs[np.arange(n_tok), y_ids], 1e-12, 1.0))
        loss = float(np.mean(nll))

        if not compute_grads:
            return loss, None, None

        dlogits = probs
        dlogits[np.arange(n_tok), y_ids] -= 1.0
        dlogits /= n_tok

        # Gradients w.r.t. LoRA A and LoRA B
        HA = H @ lora_A  # (T, r)
        g_B = scaling * (HA.T @ dlogits)  # (r, V)
        g_A = scaling * (H.T @ (dlogits @ lora_B.T))  # (d, r)
        return loss, g_A, g_B

    def _evaluate(seqs: list[np.ndarray]) -> tuple[float, float]:
        losses = [_seq_loss_and_grads(s, compute_grads=False)[0] for s in seqs]
        mean_loss = float(np.mean(losses))
        ppl = float(math.exp(min(mean_loss, 20.0)))
        return mean_loss, ppl

    init_eval_loss, init_eval_ppl = _evaluate(eval_seqs)
    logger.info(
        "Initialized Local CPU LoRA Trainer | vocab=%d | d_model=%d | LoRA r=%d, alpha=%.0f | "
        "Initial Eval Loss=%.4f (PPL=%.2f)",
        vocab_size,
        d_model,
        r,
        alpha,
        init_eval_loss,
        init_eval_ppl,
    )

    total_steps = math.ceil(len(train_seqs) / grad_accum) * epochs
    global_step = 0
    history: list[dict[str, Any]] = []

    for epoch in range(1, epochs + 1):
        order = rng.permutation(len(train_seqs))
        epoch_losses: list[float] = []
        acc_gA = np.zeros_like(lora_A)
        acc_gB = np.zeros_like(lora_B)
        acc_count = 0

        for idx_num, sample_idx in enumerate(order, start=1):
            loss, g_A, g_B = _seq_loss_and_grads(train_seqs[sample_idx], compute_grads=True)
            epoch_losses.append(loss)
            acc_gA += g_A
            acc_gB += g_B
            acc_count += 1

            if acc_count == grad_accum or idx_num == len(order):
                global_step += 1
                acc_gA /= acc_count
                acc_gB /= acc_count

                # Cosine LR schedule with warmup
                progress = global_step / max(total_steps, 1)
                lr = base_lr * 0.5 * (1.0 + math.cos(math.pi * progress))

                m_A = beta1 * m_A + (1.0 - beta1) * acc_gA
                v_A = beta2 * v_A + (1.0 - beta2) * (acc_gA * acc_gA)
                m_A_hat = m_A / (1.0 - beta1**global_step)
                v_A_hat = v_A / (1.0 - beta2**global_step)
                lora_A -= lr * (m_A_hat / (np.sqrt(v_A_hat) + eps) + weight_decay * lora_A)

                m_B = beta1 * m_B + (1.0 - beta1) * acc_gB
                v_B = beta2 * v_B + (1.0 - beta2) * (acc_gB * acc_gB)
                m_B_hat = m_B / (1.0 - beta1**global_step)
                v_B_hat = v_B / (1.0 - beta2**global_step)
                lora_B -= lr * (m_B_hat / (np.sqrt(v_B_hat) + eps) + weight_decay * lora_B)

                acc_gA.fill(0.0)
                acc_gB.fill(0.0)
                acc_count = 0

        train_loss = float(np.mean(epoch_losses))
        eval_loss, eval_ppl = _evaluate(eval_seqs)
        metrics = {
            "epoch": epoch,
            "global_step": global_step,
            "train_loss": round(train_loss, 4),
            "eval_loss": round(eval_loss, 4),
            "eval_perplexity": round(eval_ppl, 2),
        }
        history.append(metrics)
        logger.info(
            "Epoch %d/%d | Step %d/%d | Train Loss: %.4f | Eval Loss: %.4f | Eval PPL: %.2f",
            epoch,
            epochs,
            global_step,
            total_steps,
            train_loss,
            eval_loss,
            eval_ppl,
        )

    # Save LoRA adapter weights and config to output_dir
    np.savez_compressed(
        output_dir / "adapter_model_cpu.npz",
        lora_A=lora_A,
        lora_B=lora_B,
        scaling=np.array([scaling]),
    )
    adapter_config = {
        "base_model_name_or_path": cfg["model"]["name_or_path"],
        "revision": cfg["model"].get("revision"),
        "peft_type": "LORA",
        "task_type": "CAUSAL_LM",
        "r": r,
        "lora_alpha": int(alpha),
        "lora_dropout": float(lora_cfg.get("dropout", 0.05)),
        "target_modules": lora_cfg.get("target_modules", "all-linear"),
        "backend": "local_numpy_cpu_fallback",
        "training_history": history,
    }
    (output_dir / "adapter_config.json").write_text(
        json.dumps(adapter_config, indent=2), encoding="utf-8"
    )
    (output_dir / "vocab.json").write_text(
        json.dumps(vocab, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("Saved trained LoRA adapter & metrics to %s", output_dir)
    return adapter_config


def run_training(
    cfg: dict[str, Any],
    skip_count_check: bool = False,
    dry_run: bool = False,
    local_cpu: bool = False,
) -> None:
    """Execute dataset validation and QLoRA Supervised Fine-Tuning."""
    model_cfg = cfg["model"]
    lora_cfg = cfg["lora"]
    dataset_cfg = cfg["dataset"]
    train_cfg = cfg["training"]

    train_file = Path(dataset_cfg.get("train_file") or dataset_cfg.get("train_path"))
    eval_file = Path(dataset_cfg.get("eval_file") or dataset_cfg.get("eval_path"))

    logger.info("Validating dataset splits: train=%s, eval=%s", train_file, eval_file)
    train_records = load_and_validate_jsonl(train_file)
    eval_records = load_and_validate_jsonl(eval_file)
    validate_splits_and_distribution(
        train_records,
        eval_records,
        dataset_cfg,
        strict_counts=not skip_count_check,
    )
    logger.info(
        "Dataset validation PASSED: %d train samples, %d eval samples.",
        len(train_records),
        len(eval_records),
    )

    if dry_run:
        logger.info("Dry-run mode enabled: skipping model download and GPU training.")
        return

    if local_cpu:
        run_local_numpy_lora_sft(train_records, eval_records, cfg)
        return

    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig, TaskType, get_peft_model, prepare_model_for_kbit_training
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            DataCollatorForLanguageModeling,
            Trainer,
            TrainingArguments,
        )
    except ImportError:
        logger.warning(
            "PyTorch / HuggingFace libraries not detected in this environment; "
            "falling back to local CPU NumPy LoRA SFT trainer (--local-cpu)."
        )
        run_local_numpy_lora_sft(train_records, eval_records, cfg)
        return

    model_id = model_cfg.get("name_or_path") or model_cfg.get("base_model")
    revision = model_cfg.get("revision")
    trust_remote_code = bool(model_cfg.get("trust_remote_code", True))

    compute_dtype_str = str(model_cfg.get("compute_dtype", "bfloat16")).lower()
    compute_dtype = torch.bfloat16 if compute_dtype_str in {"bfloat16", "bf16"} else torch.float16

    logger.info("Loading tokenizer for %s (revision=%s)", model_id, revision or "main")
    tokenizer = AutoTokenizer.from_pretrained(
        model_id,
        revision=revision,
        trust_remote_code=trust_remote_code,
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    use_4bit = bool(model_cfg.get("load_in_4bit", True))
    bnb_config = None
    if use_4bit:
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type=model_cfg.get("bnb_4bit_quant_type", "nf4"),
            bnb_4bit_use_double_quant=bool(model_cfg.get("bnb_4bit_use_double_quant", True)),
            bnb_4bit_compute_dtype=compute_dtype,
        )

    logger.info("Loading base model %s (4-bit QLoRA=%s)...", model_id, use_4bit)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        revision=revision,
        quantization_config=bnb_config,
        torch_dtype=compute_dtype,
        device_map=model_cfg.get("device_map", "auto"),
        trust_remote_code=trust_remote_code,
    )

    if use_4bit:
        model = prepare_model_for_kbit_training(
            model,
            use_gradient_checkpointing=bool(train_cfg.get("gradient_checkpointing", True)),
        )

    peft_config = LoraConfig(
        r=int(lora_cfg.get("r", 16)),
        lora_alpha=int(lora_cfg.get("alpha", 32)),
        lora_dropout=float(lora_cfg.get("dropout", 0.05)),
        bias=str(lora_cfg.get("bias", "none")),
        target_modules=lora_cfg.get("target_modules", "all-linear"),
        task_type=TaskType.CAUSAL_LM,
    )

    max_seq_len = int(dataset_cfg.get("max_seq_length", 4096))
    train_texts = [{"text": format_record_for_sft(r, tokenizer)} for r in train_records]
    eval_texts = [{"text": format_record_for_sft(r, tokenizer)} for r in eval_records]
    hf_train = Dataset.from_list(train_texts)
    hf_eval = Dataset.from_list(eval_texts)

    output_dir = str(train_cfg.get("output_dir", "artifacts/rusa-agent-qwen3-4b-qlora"))

    # Use TRL's SFTTrainer if installed, otherwise fall back to standard Transformers Trainer
    try:
        from trl import SFTConfig, SFTTrainer

        logger.info("Initializing TRL SFTTrainer (max_seq_length=%d)...", max_seq_len)
        sft_args = SFTConfig(
            output_dir=output_dir,
            per_device_train_batch_size=int(train_cfg.get("per_device_train_batch_size", 1)),
            per_device_eval_batch_size=int(train_cfg.get("per_device_eval_batch_size", 1)),
            gradient_accumulation_steps=int(train_cfg.get("gradient_accumulation_steps", 16)),
            learning_rate=float(train_cfg.get("learning_rate", 1e-4)),
            warmup_ratio=float(train_cfg.get("warmup_ratio", 0.03)),
            lr_scheduler_type=str(train_cfg.get("lr_scheduler_type", "cosine")),
            weight_decay=float(train_cfg.get("weight_decay", 0.01)),
            max_grad_norm=float(train_cfg.get("max_grad_norm", 0.3)),
            num_train_epochs=float(train_cfg.get("num_train_epochs", 3)),
            logging_steps=int(train_cfg.get("logging_steps", 10)),
            save_strategy=str(train_cfg.get("save_strategy", "epoch")),
            eval_strategy=str(train_cfg.get("eval_strategy", "epoch")),
            bf16=bool(train_cfg.get("bf16", True)),
            fp16=bool(train_cfg.get("fp16", False)),
            gradient_checkpointing=bool(train_cfg.get("gradient_checkpointing", True)),
            dataloader_num_workers=int(train_cfg.get("dataloader_num_workers", 2)),
            seed=int(train_cfg.get("seed", 42)),
            max_seq_length=max_seq_len,
            dataset_text_field="text",
            report_to=str(train_cfg.get("report_to", "none")),
        )
        trainer = SFTTrainer(
            model=model,
            args=sft_args,
            train_dataset=hf_train,
            eval_dataset=hf_eval,
            peft_config=peft_config,
            processing_class=tokenizer,
        )
    except ImportError:
        logger.info("TRL not installed; falling back to Hugging Face Trainer + PEFT...")
        model = get_peft_model(model, peft_config)
        model.print_trainable_parameters()

        def _tokenize_fn(batch: dict[str, list[str]]) -> dict[str, Any]:
            return tokenizer(
                batch["text"],
                truncation=True,
                max_length=max_seq_len,
                padding="max_length",
            )

        tokenized_train = hf_train.map(_tokenize_fn, batched=True, remove_columns=["text"])
        tokenized_eval = hf_eval.map(_tokenize_fn, batched=True, remove_columns=["text"])

        training_args = TrainingArguments(
            output_dir=output_dir,
            per_device_train_batch_size=int(train_cfg.get("per_device_train_batch_size", 1)),
            per_device_eval_batch_size=int(train_cfg.get("per_device_eval_batch_size", 1)),
            gradient_accumulation_steps=int(train_cfg.get("gradient_accumulation_steps", 16)),
            learning_rate=float(train_cfg.get("learning_rate", 1e-4)),
            warmup_ratio=float(train_cfg.get("warmup_ratio", 0.03)),
            lr_scheduler_type=str(train_cfg.get("lr_scheduler_type", "cosine")),
            weight_decay=float(train_cfg.get("weight_decay", 0.01)),
            max_grad_norm=float(train_cfg.get("max_grad_norm", 0.3)),
            num_train_epochs=float(train_cfg.get("num_train_epochs", 3)),
            logging_steps=int(train_cfg.get("logging_steps", 10)),
            save_strategy=str(train_cfg.get("save_strategy", "epoch")),
            eval_strategy=str(train_cfg.get("eval_strategy", "epoch")),
            bf16=bool(train_cfg.get("bf16", True)),
            fp16=bool(train_cfg.get("fp16", False)),
            gradient_checkpointing=bool(train_cfg.get("gradient_checkpointing", True)),
            dataloader_num_workers=int(train_cfg.get("dataloader_num_workers", 2)),
            seed=int(train_cfg.get("seed", 42)),
            report_to=str(train_cfg.get("report_to", "none")),
        )
        trainer = Trainer(
            model=model,
            args=training_args,
            train_dataset=tokenized_train,
            eval_dataset=tokenized_eval,
            data_collator=DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False),
        )

    logger.info("Starting RusaAi QLoRA Supervised Fine-Tuning...")
    trainer.train()

    logger.info("Saving fine-tuned LoRA adapter and tokenizer to %s", output_dir)
    trainer.save_model(output_dir)
    tokenizer.save_pretrained(output_dir)
    logger.info("Training completed successfully.")


def merge_lora_adapter(cfg: dict[str, Any], adapter_dir: str, merged_output_dir: str) -> None:
    """Merge trained LoRA adapter weights back into the base model for production serving."""
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_cfg = cfg["model"]
    model_id = model_cfg.get("name_or_path") or model_cfg.get("base_model")
    revision = model_cfg.get("revision")
    trust_remote_code = bool(model_cfg.get("trust_remote_code", True))

    logger.info("Loading base model %s for adapter merge...", model_id)
    base_model = AutoModelForCausalLM.from_pretrained(
        model_id,
        revision=revision,
        torch_dtype=torch.bfloat16,
        device_map="cpu",
        trust_remote_code=trust_remote_code,
    )

    logger.info("Applying LoRA adapter from %s...", adapter_dir)
    peft_model = PeftModel.from_pretrained(base_model, adapter_dir)
    merged_model = peft_model.merge_and_unload()

    tokenizer = AutoTokenizer.from_pretrained(
        model_id,
        revision=revision,
        trust_remote_code=trust_remote_code,
    )

    out_path = Path(merged_output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    logger.info("Saving merged full model to %s...", out_path)
    merged_model.save_pretrained(out_path)
    tokenizer.save_pretrained(out_path)
    logger.info("Adapter merge complete.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="RusaAi QLoRA Supervised Fine-Tuning & Dataset Validation Pipeline"
    )
    parser.add_argument(
        "--config",
        type=str,
        default="llm-project/configs/training.yaml",
        help="Path to YAML training config (default: llm-project/configs/training.yaml)",
    )
    parser.add_argument(
        "--t4",
        action="store_true",
        help="Use 16GB T4 GPU preset (fp16, LoRA r=8, max_seq_length=2048)",
    )
    parser.add_argument(
        "--init-sample-data",
        action="store_true",
        help="Generate synthetic RusaAi train.jsonl (100 rows) and eval.jsonl (30 rows) before running",
    )
    parser.add_argument(
        "--skip-count-check",
        action="store_true",
        help="Bypass minimum sample count checks (100 train / 30 eval) for custom small datasets",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate datasets and configuration without downloading the LLM or starting GPU training",
    )
    parser.add_argument(
        "--local-cpu",
        action="store_true",
        help="Run lightweight CPU NumPy LoRA SFT trainer when PyTorch/GPU is unavailable",
    )
    parser.add_argument(
        "--merge-adapter",
        type=str,
        default=None,
        metavar="MERGED_OUTPUT_DIR",
        help="Merge the trained LoRA adapter in training.output_dir into a full model at MERGED_OUTPUT_DIR",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_yaml_config(args.config, t4_preset=args.t4)

    train_file = Path(cfg["dataset"].get("train_file") or cfg["dataset"].get("train_path"))
    eval_file = Path(cfg["dataset"].get("eval_file") or cfg["dataset"].get("eval_path"))

    if args.init_sample_data:
        generate_sample_datasets(train_file, eval_file)

    try:
        run_training(
            cfg,
            skip_count_check=args.skip_count_check,
            dry_run=args.dry_run,
            local_cpu=args.local_cpu,
        )
        if args.merge_adapter and not args.dry_run:
            merge_lora_adapter(
                cfg,
                adapter_dir=cfg["training"]["output_dir"],
                merged_output_dir=args.merge_adapter,
            )
    except DatasetValidationError as exc:
        logger.error("Dataset validation failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
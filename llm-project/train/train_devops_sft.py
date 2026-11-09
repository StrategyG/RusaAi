#!/usr/bin/env python3
"""Run QLoRA SFT for the restricted DevOps agent action contract."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rusa_devops.learning import load_jsonl, validate_splits  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-file", type=Path, default=ROOT / "llm-project/data/devops/train.jsonl")
    parser.add_argument("--eval-file", type=Path, default=ROOT / "llm-project/data/devops/eval.jsonl")
    parser.add_argument("--config", type=Path, default=ROOT / "llm-project/configs/devops-training.yaml")
    parser.add_argument("--allow-small-dataset", action="store_true")
    args = parser.parse_args()

    train_records = load_jsonl(args.train_file)
    eval_records = load_jsonl(args.eval_file)
    validate_splits(train_records, eval_records, require_minimum=not args.allow_small_dataset)

    try:
        import yaml
        import torch
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
        from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, Trainer, TrainingArguments
    except ImportError as exc:
        raise SystemExit("Install requirements-devops-train.txt on a dedicated CUDA host") from exc
    if not torch.cuda.is_available():
        raise SystemExit("QLoRA training requires a CUDA GPU; CPU training is disabled")

    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    model_cfg, dataset_cfg = config["model"], config["dataset"]
    training_cfg, qlora_cfg, lora_cfg = config["training"], config["qlora"], config["lora"]
    compute_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type=str(qlora_cfg.get("quant_type", "nf4")),
        bnb_4bit_use_double_quant=bool(qlora_cfg.get("double_quant", True)),
        bnb_4bit_compute_dtype=compute_dtype,
    )
    tokenizer = AutoTokenizer.from_pretrained(
        model_cfg["id"], revision=model_cfg["revision"], trust_remote_code=False
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        model_cfg["id"], revision=model_cfg["revision"], quantization_config=quantization,
        torch_dtype=compute_dtype, device_map="auto", low_cpu_mem_usage=True,
        trust_remote_code=False, use_safetensors=True,
    )
    model = prepare_model_for_kbit_training(
        model, use_gradient_checkpointing=bool(training_cfg.get("gradient_checkpointing", True))
    )
    model = get_peft_model(model, LoraConfig(
        r=int(lora_cfg.get("r", 16)), lora_alpha=int(lora_cfg.get("alpha", 32)),
        lora_dropout=float(lora_cfg.get("dropout", 0.05)), bias="none",
        task_type="CAUSAL_LM", target_modules=lora_cfg.get("target_modules", "all-linear"),
    ))

    class DevOpsDataset(torch.utils.data.Dataset):
        def __init__(self, records):
            self.items = []
            max_length = int(dataset_cfg.get("max_seq_length", 4096))
            for record in records:
                prompt = tokenizer.apply_chat_template(
                    record["messages"], tokenize=True, add_generation_prompt=True
                )
                completion = tokenizer.encode(
                    json.dumps(record["response"], ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    add_special_tokens=False,
                )
                if tokenizer.eos_token_id is None:
                    raise ValueError("Tokenizer must define eos_token_id")
                completion = list(completion) + [tokenizer.eos_token_id]
                if not prompt or len(prompt) + len(completion) > max_length:
                    raise ValueError(f"Example {record['id']} is empty or exceeds max_seq_length")
                ids = list(prompt) + completion
                self.items.append({
                    "input_ids": ids,
                    "attention_mask": [1] * len(ids),
                    "labels": [-100] * len(prompt) + completion,
                })

        def __len__(self):
            return len(self.items)

        def __getitem__(self, index):
            return self.items[index]

    def collate(features):
        width = max(len(item["input_ids"]) for item in features)
        pad_id = tokenizer.pad_token_id
        return {
            "input_ids": torch.tensor([x["input_ids"] + [pad_id] * (width-len(x["input_ids"])) for x in features]),
            "attention_mask": torch.tensor([x["attention_mask"] + [0] * (width-len(x["attention_mask"])) for x in features]),
            "labels": torch.tensor([x["labels"] + [-100] * (width-len(x["labels"])) for x in features]),
        }

    output_dir = ROOT / training_cfg["output_dir"]
    training_args = TrainingArguments(
        output_dir=str(output_dir / "checkpoints"),
        per_device_train_batch_size=int(training_cfg.get("per_device_train_batch_size", 1)),
        per_device_eval_batch_size=int(training_cfg.get("per_device_eval_batch_size", 1)),
        gradient_accumulation_steps=int(training_cfg.get("gradient_accumulation_steps", 16)),
        num_train_epochs=float(training_cfg.get("num_train_epochs", 3)),
        learning_rate=float(training_cfg.get("learning_rate", 1e-4)),
        weight_decay=float(training_cfg.get("weight_decay", 0.01)),
        warmup_ratio=float(training_cfg.get("warmup_ratio", 0.03)),
        lr_scheduler_type=str(training_cfg.get("lr_scheduler_type", "cosine")),
        logging_steps=int(training_cfg.get("logging_steps", 5)),
        eval_strategy=str(training_cfg.get("eval_strategy", "epoch")),
        save_strategy=str(training_cfg.get("save_strategy", "epoch")),
        save_total_limit=int(training_cfg.get("save_total_limit", 3)),
        load_best_model_at_end=True, metric_for_best_model="eval_loss",
        greater_is_better=False, gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        bf16=compute_dtype == torch.bfloat16, fp16=compute_dtype == torch.float16,
        optim="paged_adamw_8bit", report_to=[], remove_unused_columns=False,
        seed=int(training_cfg.get("seed", 42)),
    )
    trainer = Trainer(
        model=model, args=training_args,
        train_dataset=DevOpsDataset(train_records),
        eval_dataset=DevOpsDataset(eval_records),
        data_collator=collate,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    adapter_dir = output_dir / "adapter"
    train_result = trainer.train()
    trainer.save_model(str(adapter_dir))
    tokenizer.save_pretrained(adapter_dir)
    eval_metrics = trainer.evaluate()
    provenance = {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model_cfg, "method": "QLoRA",
        "dataset": {
            "train_sha256": sha256(args.train_file), "train_examples": len(train_records),
            "eval_sha256": sha256(args.eval_file), "eval_examples": len(eval_records),
        },
        "training_metrics": train_result.metrics, "evaluation_metrics": eval_metrics,
        "hardware": {
            "gpu_name": torch.cuda.get_device_name(torch.cuda.current_device()),
            "gpu_vram_bytes": torch.cuda.get_device_properties(torch.cuda.current_device()).total_memory,
        },
        "runtime_policy": "Model output cannot authorize or execute infrastructure mutations.",
    }
    (adapter_dir / "rusa-devops-finetune-provenance.json").write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps({"adapter_dir": str(adapter_dir), "eval_metrics": eval_metrics}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

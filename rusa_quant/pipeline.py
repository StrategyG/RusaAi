#!/usr/bin/env python3
"""Quantize a dense Hugging Face causal-LM model to GPTQ INT4 using GPT-QModel."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def project_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_jsonl(path: Path, max_samples: int) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Calibration file not found: {path}\n"
            "Create it from data/calibration/calibration.example.jsonl and add "
            "at least the configured minimum number of representative samples."
        )

    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: each JSONL row must be an object")
            if not isinstance(row.get("text"), str) and not isinstance(row.get("messages"), list):
                raise ValueError(
                    f"{path}:{line_no}: row must contain either string 'text' "
                    "or list 'messages'"
                )
            rows.append(row)
            if len(rows) >= max_samples:
                break

    if not rows:
        raise ValueError(f"Calibration file is empty: {path}")
    return rows


def row_to_text(row: dict[str, Any], tokenizer: Any, row_number: int) -> str:
    text = row.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    messages = row.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError(f"Calibration row {row_number} has no usable text/messages")

    if not hasattr(tokenizer, "apply_chat_template"):
        raise ValueError(
            f"Calibration row {row_number} contains messages, but tokenizer "
            "does not expose apply_chat_template. Use a 'text' field instead."
        )

    rendered = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=False,
    )
    if not isinstance(rendered, str) or not rendered.strip():
        raise ValueError(f"Could not render chat calibration row {row_number}")
    return rendered.strip()


def tokenize_calibration(
    rows: list[dict[str, Any]],
    tokenizer: Any,
    *,
    max_length: int,
    min_tokens: int,
) -> list[dict[str, list[int]]]:
    samples: list[dict[str, list[int]]] = []

    for index, row in enumerate(rows, start=1):
        text = row_to_text(row, tokenizer, index)
        encoded = tokenizer(
            text,
            truncation=True,
            max_length=max_length,
            padding=False,
            add_special_tokens=True,
        )
        input_ids = encoded["input_ids"]
        attention_mask = encoded.get("attention_mask", [1] * len(input_ids))

        if len(input_ids) <= min_tokens:
            continue

        samples.append(
            {
                "input_ids": [int(token) for token in input_ids],
                "attention_mask": [int(mask) for mask in attention_mask],
            }
        )

    return samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="rusa_quant/config.yaml",
        help="Path to the YAML quantization configuration",
    )
    parser.add_argument("--model-id", default=None, help="Override model.id")
    parser.add_argument("--output-dir", default=None, help="Override output.directory")
    args = parser.parse_args()

    config_path = project_path(args.config)
    if not config_path.is_file():
        raise FileNotFoundError(f"Configuration not found: {config_path}")

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("YAML config must contain a mapping")

    model_cfg = config["model"]
    quant_cfg = config["quantization"]
    calibration_cfg = config["calibration"]
    output_cfg = config["output"]
    validation_cfg = config["validation"]

    model_id = (
        args.model_id
        or os.getenv("RUSA_QUANT_MODEL_ID")
        or model_cfg.get("id")
    )
    if not model_id or model_id == "REPLACE_WITH_HF_MODEL_ID":
        raise ValueError(
            "Set model.id in rusa_quant/config.yaml or pass --model-id "
            "ORG/MODEL. The requested model was not specified yet."
        )

    if quant_cfg.get("method", "gptq").lower() != "gptq":
        raise ValueError("This pipeline only supports method: gptq")

    import torch
    import transformers
    import gptqmodel
    from gptqmodel import GPTQConfig, GPTQModel

    device = str(model_cfg.get("device", "cuda:0"))
    if device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(
            "CUDA is not available. Select a CUDA runtime with an NVIDIA GPU "
            "and verify torch.cuda.is_available() before quantization."
        )

    if device.startswith("cuda"):
        gpu_name = torch.cuda.get_device_name(0)
        gpu_memory_gib = round(
            torch.cuda.get_device_properties(0).total_memory / (1024**3), 2
        )
    else:
        gpu_name = "CPU"
        gpu_memory_gib = None

    dtype_name = str(model_cfg.get("dtype", "float16"))
    dtype_map = {
        "float16": torch.float16,
        "fp16": torch.float16,
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
    }
    if dtype_name.lower() not in dtype_map:
        raise ValueError("model.dtype must be float16/fp16 or bfloat16/bf16")
    dtype = dtype_map[dtype_name.lower()]

    calibration_path = project_path(calibration_cfg["file"])
    rows = load_jsonl(calibration_path, int(calibration_cfg["max_samples"]))
    min_samples = int(calibration_cfg.get("min_samples", 32))
    if len(rows) < min_samples:
        raise ValueError(
            f"Only {len(rows)} calibration rows found; at least {min_samples} "
            "are required by this configuration. Use representative data."
        )

    output_dir = project_path(args.output_dir or output_cfg["directory"])
    if output_dir.exists():
        raise FileExistsError(
            f"Output directory already exists: {output_dir}\n"
            "Choose a new output directory; this pipeline never overwrites an artifact."
        )
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    revision = (
        os.getenv("RUSA_QUANT_MODEL_REVISION")
        or model_cfg.get("revision")
    )
    trust_remote_code = bool(model_cfg.get("trust_remote_code", False))

    print("=== RUSA-AI GPTQ quantization ===")
    print(f"Model: {model_id}")
    print(f"Revision: {revision or 'not pinned'}")
    print(f"Device: {device} ({gpu_name}; {gpu_memory_gib} GiB)")
    print(f"Torch: {torch.__version__}; CUDA build: {torch.version.cuda}")
    print(f"Transformers: {transformers.__version__}")
    print(f"GPTQModel: {gptqmodel.__version__}")
    print(f"Calibration rows: {len(rows)}")
    print(f"Calibration SHA-256: {sha256_file(calibration_path)}")
    print(f"Output: {output_dir}")

    gptq_config = GPTQConfig(
        bits=int(quant_cfg.get("bits", 4)),
        group_size=int(quant_cfg.get("group_size", 128)),
        sym=bool(quant_cfg.get("sym", True)),
        desc_act=bool(quant_cfg.get("desc_act", False)),
        true_sequential=bool(quant_cfg.get("true_sequential", True)),
    )

    load_kwargs: dict[str, Any] = {
        "device": device,
        "dtype": dtype,
        "trust_remote_code": trust_remote_code,
    }
    if revision:
        load_kwargs["revision"] = revision

    # HF_TOKEN is read by Hugging Face Hub automatically for gated repositories.
    model = GPTQModel.load(model_id, gptq_config, **load_kwargs)
    tokenizer = model.tokenizer

    calibration = tokenize_calibration(
        rows,
        tokenizer,
        max_length=int(calibration_cfg.get("max_length", 512)),
        min_tokens=int(calibration_cfg.get("min_tokens", 10)),
    )
    if len(calibration) < min_samples:
        raise ValueError(
            f"Only {len(calibration)} usable calibration samples remain after "
            f"tokenization; minimum is {min_samples}. Check the dataset and tokenizer."
        )

    print(f"Usable calibration samples: {len(calibration)}")
    print(
        "Starting GPTQ: "
        f"bits={gptq_config.bits}, group_size={gptq_config.group_size}, "
        f"batch_size={int(calibration_cfg.get('batch_size', 1))}"
    )

    model.quantize(
        calibration,
        batch_size=int(calibration_cfg.get("batch_size", 1)),
    )
    model.save(str(output_dir))
    usable_sample_count = len(calibration)
    print(f"Quantized model saved: {output_dir}")

    del model
    del calibration
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    print("Reloading the quantized model for a smoke test...")
    loaded = GPTQModel.load(
        str(output_dir),
        device=device,
        dtype=dtype,
    )
    prompt = str(validation_cfg["prompt"])
    generated = loaded.generate(
        prompt,
        max_new_tokens=int(validation_cfg.get("max_new_tokens", 64)),
        do_sample=False,
    )
    token_ids = generated[0] if isinstance(generated, (list, tuple)) else generated
    decoded = loaded.tokenizer.decode(token_ids, skip_special_tokens=True)
    print("\n=== Smoke test output ===")
    print(decoded)

    run_metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_id": model_id,
        "model_revision": revision,
        "trust_remote_code": trust_remote_code,
        "quantization": {
            "method": "GPTQ",
            "bits": gptq_config.bits,
            "group_size": gptq_config.group_size,
            "sym": bool(quant_cfg.get("sym", True)),
            "desc_act": bool(quant_cfg.get("desc_act", False)),
            "true_sequential": bool(quant_cfg.get("true_sequential", True)),
            "dtype": dtype_name,
        },
        "calibration": {
            "file": str(calibration_path),
            "sha256": sha256_file(calibration_path),
            "rows_loaded": len(rows),
            "usable_samples": usable_sample_count,
            "max_length": int(calibration_cfg.get("max_length", 512)),
        },
        "software": {
            "python": sys.version,
            "torch": torch.__version__,
            "torch_cuda": torch.version.cuda,
            "transformers": transformers.__version__,
            "gptqmodel": gptqmodel.__version__,
        },
        "hardware": {
            "device": device,
            "gpu": gpu_name,
            "gpu_memory_gib": gpu_memory_gib,
        },
        "smoke_test": {
            "prompt": prompt,
            "output": decoded,
            "passed": bool(decoded.strip()),
        },
        "artifact_directory": str(output_dir),
    }

    log_dir = project_path(output_cfg.get("log_directory", "logs/quantization"))
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"gptq-{stamp}.json"
    log_path.write_text(
        json.dumps(run_metadata, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"\nRun metadata: {log_path}")
    print("PASS: quantized checkpoint reloaded and generated a response.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

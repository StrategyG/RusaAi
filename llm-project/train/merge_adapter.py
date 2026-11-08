#!/usr/bin/env python3
"""Merge a trained PEFT adapter into a standalone safetensors model artifact."""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dtype", choices=("bfloat16", "float16"), default="bfloat16")
    args = parser.parse_args()
    adapter_dir = args.adapter_dir.expanduser().resolve(strict=True)
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir == adapter_dir or adapter_dir in output_dir.parents:
        parser.error("--output-dir must not be inside the adapter directory")
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error("--output-dir must be empty or not yet exist")

    try:
        import torch
        from peft import AutoPeftModelForCausalLM
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise SystemExit("Install requirements-train.txt before merging adapters") from exc

    dtype = torch.bfloat16 if args.dtype == "bfloat16" else torch.float16
    model = AutoPeftModelForCausalLM.from_pretrained(
        str(adapter_dir),
        torch_dtype=dtype,
        device_map="auto",
        low_cpu_mem_usage=True,
        trust_remote_code=False,
        use_safetensors=True,
    )
    merged = model.merge_and_unload(safe_merge=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    merged.save_pretrained(
        output_dir,
        safe_serialization=True,
        max_shard_size="4GB",
    )
    tokenizer = AutoTokenizer.from_pretrained(str(adapter_dir), trust_remote_code=False)
    tokenizer.save_pretrained(output_dir)
    provenance = adapter_dir / "rusa-finetune-provenance.json"
    if provenance.is_file():
        shutil.copy2(provenance, output_dir / provenance.name)
    print(f"Merged model saved to: {output_dir}")
    print("Next: create and pin rusa-model-manifest.json before serving this artifact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# RUSA-AI GPTQ INT4 Quantization

This module adds a separate post-training quantization path. It does not replace or modify the existing QLoRA/SFT training pipeline.

## What it produces

- Algorithm: GPTQ post-training weight quantization
- Default: 4-bit weights, group size 128, symmetric quantization
- Activation/compute dtype: FP16 by default (W4A16)
- Calibration: JSONL text or chat records
- Output: GPTQModel checkpoint saved by GPT-QModel

## Requirements

Use Python 3.10+ and a supported NVIDIA GPU for the CUDA workflow. GPT-QModel's current general NVIDIA support starts at Turing (SM 7.5); the model architecture and selected kernels must also be supported. A successful package install does not guarantee that a particular model fits in VRAM.

Install dependencies from the repository root:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements-quant.txt
python -m pip check
```

Check the accelerator before downloading a model:

```bash
python - <<'PY'
import torch
import transformers
import gptqmodel

print("Torch:", torch.__version__)
print("Torch CUDA:", torch.version.cuda)
print("Transformers:", transformers.__version__)
print("GPTQModel:", gptqmodel.__version__)
print("CUDA available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("GPU:", torch.cuda.get_device_name(0))
    print("VRAM GiB:", round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 2))
PY
```

Install a PyTorch build compatible with the machine's NVIDIA driver/CUDA runtime if `torch.cuda.is_available()` is false. Do not blindly replace a working Colab Torch/CUDA build.

## Configure the model

Edit `rusa_quant/config.yaml`:

1. Set `model.id` to the exact Hugging Face model ID or a local dense-model directory.
2. Set `model.revision` to an immutable Hugging Face commit SHA for reproducibility.
3. Keep `trust_remote_code: false` unless the model genuinely requires custom code and that code has been reviewed.
4. Adjust the output directory for each experiment; the pipeline refuses to overwrite an existing output.

You can override the model from the shell:

```bash
RUSA_QUANT_MODEL_ID="organization/model-name" \
RUSA_QUANT_MODEL_REVISION="40-character-huggingface-commit-sha" \
python -m rusa_quant.pipeline --config rusa_quant/config.yaml
```

For gated Hugging Face repositories, set `HF_TOKEN` through the environment or Colab Secrets. Never commit the token.

## Prepare calibration data

Create `data/calibration/calibration.jsonl`, one JSON object per line. Supported rows:

```json
{"text": "Representative text from the target domain."}
```

or:

```json
{"messages": [{"role": "user", "content": "Representative request."}, {"role": "assistant", "content": "Representative answer."}]}
```

The chat format requires a usable chat template in the selected model tokenizer. The pipeline enforces at least 32 usable samples. The starter configuration uses up to 128 samples and truncates each sample to 512 tokens. Use representative, authorized data from the model's actual deployment domain; 128–256 is a reasonable initial experiment, not a universal quality guarantee.

The `calibration.example.jsonl` file contains only five schema examples. Copy it only as a starting point, then replace/expand it with at least 32 representative rows:

```bash
cp data/calibration/calibration.example.jsonl data/calibration/calibration.jsonl
```

## Run quantization

From the repository root:

```bash
python -m rusa_quant.pipeline --config rusa_quant/config.yaml
```

In Google Colab, after cloning the repository and changing to its root:

```python
!python -m pip install --upgrade pip
!python -m pip install -r requirements-quant.txt
```

Then set the actual model ID and run:

```python
import os
os.environ["RUSA_QUANT_MODEL_ID"] = "organization/model-name"
# For a gated model, use Colab Secrets for HF_TOKEN.
```

```python
!python -m rusa_quant.pipeline --config rusa_quant/config.yaml
```

The output is written to the configured `artifacts/quantized/...` directory. A separate run record is written under `logs/quantization/`. The pipeline reloads the saved checkpoint and runs a small deterministic generation smoke test. This verifies basic loading/generation only; it does not prove acceptable accuracy.

## Evaluation before deployment

Compare the original dense model and quantized model on the same held-out evaluation set. Measure at least:
- task accuracy / exact match or a domain-appropriate metric
- perplexity, where applicable
- output consistency and instruction-following
- peak VRAM and system RAM
- latency and tokens/second
- failure rate on long prompts and edge cases

Do not use the calibration set as the evaluation set. Keep the dense source model and quantized artifact separate.

## RUSA-AI manifest and serving caveat

The current RUSA-AI repository has `scripts/create_model_manifest.py`; it requires the exact source model revision, license, and tokenizer identity. After verifying the GPTQ output contains `config.json`, tokenizer files, and `.safetensors` weights, create a manifest:

```bash
python scripts/create_model_manifest.py \
  --model-dir artifacts/quantized/model-gptq-int4 \
  --model-id "organization/model-name" \
  -revision "40-haracter-huggingface-commit-sha" \
  --license "EXACT-MODEL-LICENSE" \
  --tokenizer-id "organization/model-name" \
  --tokenizer-revision "40-character-tokenizer-commit-sha"
```

Use the SHA-256 printed by the script as `RUSA_MODEL_MANIFEST_SHA256`. Do not guess the license or revision. If the tokenizer is hosted in a separate repository, use its actual ID and revision.

Important: creating a GPTQ artifact does not automatically integrate GPTQ loading into the current RUSA-AI API. The current `TransformersBackend` loads through `AutoModelForCausalLM.from_pretrained`, and the repository's optional model requirements do not yet declare GPTQModel. Before deploying through the API, add and test the compatible GPTQModel dependency/loader path or use a separately validated serving engine such as a compatible vLLM release. A successful GPTQModel smoke test alone is not production certification.

## Troubleshooting

- `CUDA not available`: select a CUDA GPU runtime and verify the PyTorch CUDA build.
- `out of memory`: use a smaller dense base model, shorter calibration length, fewer samples, or a GPU with more VRAM. GPTS needs the dense source model during quantization; a 4-bit output does not mean quantization itself needs only 4-bit memory.
- unsupported model/module: confirm the architecture is supported by the installed GPT-QModel release; check the model loader and keep remote code disabled unless reviewed.
- gated model: accept the model terms and provide `HF_TOKEN` securely.
- output directory exists: choose a new directory; the pipeline deliberately does not overwrite previous artifacts.

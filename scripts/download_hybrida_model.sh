#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

MODEL_DIR="$HOME/.rusa-hybrida/models"
MODEL_FILE="$MODEL_DIR/Qwen3-0.6B-Q4_0.gguf"
MODEL_URL="https://huggingface.co/ggml-org/Qwen3-0.6B-GGUF/resolve/main/Qwen3-0.6B-Q4_0.gguf"
EXPECTED_SHA256="da2572f16c06133561ce56accaa822216f2391ef4d37fba427801cd6736417d4"

mkdir -p "$MODEL_DIR"
if [ -s "$MODEL_FILE" ]; then
  echo "Model already exists: $MODEL_FILE"
  exit 0
fi

curl --fail --location --retry 3 --retry-delay 2 \
  "$MODEL_URL" --output "$MODEL_FILE.part"
printf '%s  %s\n' "$EXPECTED_SHA256" "$MODEL_FILE.part" | sha256sum -c -
mv "$MODEL_FILE.part" "$MODEL_FILE"
echo "Downloaded and verified Qwen3 0.6B Q4_0 at $MODEL_FILE"

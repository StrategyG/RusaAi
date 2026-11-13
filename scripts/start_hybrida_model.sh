#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [ -f "$ROOT/mobile-local/.env" ]; then
  set -a
  . "$ROOT/mobile-local/.env"
  set +a
fi

MODEL_FILE="$HOME/.rusa-hybrida/models/Qwen3-0.6B-Q4_0.gguf"
LLAMA_SERVER="$HOME/.local/bin/llama-server"

if [ ! -x "$LLAMA_SERVER" ]; then
  echo "llama-server is missing. Run scripts/setup_hybrida_termux.sh first."
  exit 1
fi
if [ ! -s "$MODEL_FILE" ]; then
  echo "Qwen model is missing. Run scripts/download_hybrida_model.sh first."
  exit 1
fi
if ! printenv RUSA_MOBILE_CONTEXT >/dev/null 2>&1; then
  export RUSA_MOBILE_CONTEXT=2048
fi
if ! printenv RUSA_MOBILE_THREADS >/dev/null 2>&1; then
  export RUSA_MOBILE_THREADS=4
fi

exec "$LLAMA_SERVER" \
  --model "$MODEL_FILE" \
  --alias qwen-local \
  --host 127.0.0.1 \
  --port 8081 \
  --ctx-size "$RUSA_MOBILE_CONTEXT" \
  --threads "$RUSA_MOBILE_THREADS" \
  --batch-size 128 \
  --ubatch-size 64 \
  --parallel 1 \
  --n-gpu-layers 0

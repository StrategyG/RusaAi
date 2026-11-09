#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
if [ -f "$ROOT/mobile-local/.env" ]; then
  set -a
  . "$ROOT/mobile-local/.env"
  set +a
fi
if ! printenv RUSA_MOBILE_DATA_DIR >/dev/null 2>&1; then
  export RUSA_MOBILE_DATA_DIR="$HOME/.rusa-hybrida"
fi
if ! printenv RUSA_LLAMA_BASE_URL >/dev/null 2>&1; then
  export RUSA_LLAMA_BASE_URL="http://127.0.0.1:8081/v1"
fi
if ! printenv RUSA_LLAMA_MODEL >/dev/null 2>&1; then
  export RUSA_LLAMA_MODEL="qwen-local"
fi

cd "$ROOT"
exec python -m uvicorn app:app --app-dir "$ROOT/mobile-local" \
  --host 127.0.0.1 --port 8765 --workers 1

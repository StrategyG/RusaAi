#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LLAMA_DIR="$HOME/src/llama.cpp"

pkg update -y
pkg install -y git clang cmake ninja make python curl

python -m pip install --upgrade pip
python -m pip install -r "$ROOT/mobile-local/requirements.txt"

mkdir -p "$HOME/src" "$HOME/.local/bin" "$HOME/.rusa-hybrida/models"

if [ ! -d "$LLAMA_DIR/.git" ]; then
  git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
else
  git -C "$LLAMA_DIR" pull --ff-only
fi

cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=ON \
  -DGGML_OPENMP=OFF \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_TESTS=OFF \
  -DLLAMA_BUILD_EXAMPLES=OFF

cmake --build "$LLAMA_DIR/build" --target llama-server -j2
ln -sf "$LLAMA_DIR/build/bin/llama-server" "$HOME/.local/bin/llama-server"

echo
echo "HYBRIDA dependencies and llama-server are ready."
echo "Next: bash scripts/download_hybrida_model.sh"

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cpu_compose_routes_api_to_private_gguf_server():
    compose = (ROOT / "docker-compose.cpu.yml").read_text(encoding="utf-8")
    model = compose.split("  llama-cpp:\n", 1)[1]

    assert "RUSA_MODEL_BASE_URL: http://llama-cpp:8080/v1" in compose
    assert "${RUSA_MAX_CONCURRENT_INFERENCES:-1}" in compose
    assert "${RUSA_MAX_WAITING_INFERENCES:-4}" in compose
    assert "${RUSA_GGUF_MODEL_PATH:?Set RUSA_GGUF_MODEL_PATH in .env}" in model
    assert "    ports:" not in model
    assert '      - --n-gpu-layers\n      - "0"' in model
    assert "      - --no-webui" in model
    assert '      - "8080"' in model

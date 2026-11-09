import yaml
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cpu_compose_routes_api_to_private_gguf_server():
    config = yaml.safe_load((ROOT / "docker-compose.cpu.yml").read_text(encoding="utf-8"))
    services = config["services"]
    api = services["api"]
    model = services["llama-cpp"]

    assert api["environment"]["RUSA_MODEL_BASE_URL"] == "http://llama-cpp:8080/v1"
    assert api["environment"]["RUSA_MAX_CONCURRENT_INFERENCES"].endswith(":-1}")
    assert "ports" not in model
    assert model["volumes"][0].endswith(":/models/model.gguf:ro")
    assert model["command"][model["command"].index("--n-gpu-layers") + 1] == "0"
    assert "--no-webui" in model["command"]

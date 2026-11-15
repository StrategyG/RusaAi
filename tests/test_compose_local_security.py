from pathlib import Path

import yaml


def test_worker_does_not_inherit_local_setup_or_api_session_secrets():
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    worker = compose["services"]["worker"]
    environment = worker["environment"]

    assert worker["env_file"][0] == ".env"
    assert worker["env_file"][1] == {"path": ".env.worker", "required": False}
    assert environment["RUSA_LOCAL_INSTALL_MODE"] == "false"
    assert environment["RUSA_LOCAL_API_KEYS_DB"] == ""
    assert environment["RUSA_LOCAL_SETUP_TOKEN"] == ""
    assert environment["RUSA_API_KEYS"] == "{}"
    assert environment["RUSA_API_KEYS_JSON"] == "{}"
    assert environment["RUSA_WEB_SESSION_SECRET"] == ""
    assert environment["RUSA_METRICS_API_KEY"] == ""


def test_external_rag_bearer_credential_is_not_injected_into_api_container():
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    api_environment = compose["services"]["api"]["environment"]

    assert "RUSA_EXTERNAL_RAG_CONNECTIONS_JSON" not in api_environment
    for secret_name in (
        "RUSA_GITHUB_TOKEN",
        "RUSA_WORKER_MODEL_API_KEY",
        "RUSA_WORKER_INFERENCE_API_KEYS",
        "RUSA_WORKER_INFERENCE_BASE_URL",
        "RUSA_INTERNAL_CA_BUNDLE",
        "RUSA_WORKSPACE_ROOT",
        "RUSA_GIT_MIRROR_ROOT",
        "RUSA_SANDBOX_IMAGE",
        "RUSA_DOCKER_SOCKET",
        "RUSA_WEB_SESSION_SECRET",
    ):
        assert api_environment[secret_name] in {"", "{}"}

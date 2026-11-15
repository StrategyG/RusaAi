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

    assert api_environment["RUSA_EXTERNAL_RAG_API_KEY"] == ""

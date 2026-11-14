from fastapi.testclient import TestClient

from app import app


client = TestClient(app)


def configure_local_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("RUSA_LOCAL_INSTALL_MODE", "true")
    monkeypatch.setenv("RUSA_LOCAL_API_KEYS_DB", str(tmp_path / "local-api-keys.sqlite3"))
    monkeypatch.setenv("RUSA_LOCAL_SETUP_TOKEN", "setup-capability-" + ("x" * 40))
    monkeypatch.delenv("RUSA_API_KEYS", raising=False)
    monkeypatch.delenv("RUSA_API_KEYS_JSON", raising=False)


def test_local_setup_status_and_one_time_owner_key(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    status = client.get("/v1/local/setup/status")
    assert status.status_code == 200
    assert status.json() == {"enabled": True, "initialized": False}

    denied = client.post("/v1/local/setup/initialize")
    assert denied.status_code == 403

    initialized = client.post(
        "/v1/local/setup/initialize",
        headers={"X-RUSA-Setup-Token": "setup-capability-" + ("x" * 40)},
    )
    assert initialized.status_code == 200
    payload = initialized.json()
    assert payload["api_key"].startswith("rusa_sk_")
    assert payload["key_display_policy"] == "shown_once"

    replay = client.post(
        "/v1/local/setup/initialize",
        headers={"X-RUSA-Setup-Token": "setup-capability-" + ("x" * 40)},
    )
    assert replay.status_code == 409

    session = client.get(
        "/v1/auth/session", headers={"X-API-Key": payload["api_key"]}
    )
    assert session.status_code == 200
    assert session.json()["tenant_id"] == "local"
    assert "tenant_admin" in session.json()["roles"]


def test_local_editor_key_is_least_privilege_and_tenant_scoped(monkeypatch, tmp_path):
    configure_local_setup(monkeypatch, tmp_path)
    initialized = client.post(
        "/v1/local/setup/initialize",
        headers={"X-RUSA-Setup-Token": "setup-capability-" + ("x" * 40)},
    )
    owner_key = initialized.json()["api_key"]

    created = client.post(
        "/v1/local/api-keys",
        headers={"X-API-Key": owner_key},
        json={"label": "VS Code", "roles": ["developer"]},
    )
    assert created.status_code == 200
    editor_key = created.json()["api_key"]
    assert editor_key.startswith("rusa_sk_")

    session = client.get("/v1/auth/session", headers={"X-API-Key": editor_key})
    assert session.status_code == 200
    assert session.json()["roles"] == ["developer"]

    escalation = client.post(
        "/v1/local/api-keys",
        headers={"X-API-Key": owner_key},
        json={"label": "Admin escalation", "roles": ["tenant_admin"]},
    )
    assert escalation.status_code == 422

    revoked = client.delete(
        "/v1/local/api-keys/" + created.json()["key_id"],
        headers={"X-API-Key": owner_key},
    )
    assert revoked.status_code == 200
    assert client.get("/v1/auth/session", headers={"X-API-Key": editor_key}).status_code == 401

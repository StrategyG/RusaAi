import pytest

from rusa_agents.local_api_keys import LocalApiKeyStore, LocalSetupAlreadyInitialized


def test_first_run_creates_one_time_owner_key_and_stores_only_digest(tmp_path):
    path = tmp_path / "local" / "keys.sqlite3"
    store = LocalApiKeyStore(path)
    assert store.is_initialized() is False

    issued = store.initialize_owner()
    assert issued["api_key"].startswith("rusa_sk_")
    assert store.is_initialized() is True
    principal = store.authenticate(issued["api_key"])
    assert principal.subject == "local-owner"
    assert principal.tenant_id == "local"
    assert principal.roles == ("tenant_admin",)
    assert issued["api_key"] not in path.read_bytes().decode("latin1", errors="ignore")


def test_first_run_is_one_time(tmp_path):
    store = LocalApiKeyStore(tmp_path / "keys.sqlite3")
    store.initialize_owner()
    with pytest.raises(LocalSetupAlreadyInitialized):
        store.initialize_owner()


def test_editor_keys_are_scoped_and_revocable(tmp_path):
    store = LocalApiKeyStore(tmp_path / "keys.sqlite3")
    owner = store.initialize_owner()
    issued = store.create_editor_key(
        tenant_id="local", label="VS Code", roles=["developer"]
    )
    assert store.authenticate(issued["api_key"]).roles == ("developer",)
    assert store.authenticate(issued["api_key"]).tenant_id == "local"
    assert store.authenticate(owner["api_key"]).roles == ("tenant_admin",)
    assert store.list_keys("local")[0]["label"] in {"VS Code", "Local owner"}
    assert store.revoke_key(key_id=issued["key_id"], tenant_id="local") is True
    assert store.authenticate(issued["api_key"]) is None


def test_editor_key_cannot_escalate_to_admin(tmp_path):
    store = LocalApiKeyStore(tmp_path / "keys.sqlite3")
    store.initialize_owner()
    with pytest.raises(ValueError):
        store.create_editor_key(
            tenant_id="local", label="Escalation", roles=["tenant_admin"]
        )


def test_last_admin_key_cannot_be_revoked(tmp_path):
    store = LocalApiKeyStore(tmp_path / "keys.sqlite3")
    owner = store.initialize_owner()
    with pytest.raises(ValueError, match="last active"):
        store.revoke_key(key_id=owner["key_id"], tenant_id="local")


def test_tampered_unknown_role_is_rejected(tmp_path):
    import sqlite3

    store = LocalApiKeyStore(tmp_path / "keys.sqlite3")
    issued = store.initialize_owner()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE local_api_keys SET roles_json=? WHERE key_id=?",
            ('["superadmin"]', issued["key_id"]),
        )
    assert store.authenticate(issued["api_key"]) is None

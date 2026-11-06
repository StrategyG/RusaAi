import uuid

import pytest

from rusa_agents.identity import (
    IdentityError,
    PostgresIdentityStore,
    validate_email,
    validate_password,
    validate_tenant_id,
)


def test_email_is_normalized():
    assert validate_email("  USER@Example.COM ") == "user@example.com"


@pytest.mark.parametrize("email", ["", "missing-at-sign", "user@", "user @example.com"])
def test_invalid_email_is_rejected(email):
    with pytest.raises(IdentityError):
        validate_email(email)


def test_password_minimum_length_is_enforced():
    validate_password("correct-horse-battery-staple")
    with pytest.raises(IdentityError):
        validate_password("short")


def test_tenant_identifier_is_restricted():
    assert validate_tenant_id("tenant-acme_1") == "tenant-acme_1"
    with pytest.raises(IdentityError):
        validate_tenant_id("tenant/acme")


def test_access_tokens_are_signed_scoped_and_expiring():
    store = PostgresIdentityStore(
        "postgresql://rusa:unused@localhost:5432/rusa",
        "a-secret-key-with-at-least-thirty-two-bytes",
        access_token_seconds=300,
    )
    user = {
        "id": str(uuid.uuid4()),
        "tenant_id": "tenant-acme",
        "email": "admin@example.test",
        "role": "admin",
    }
    token = store._create_access_token(user)
    claims = store.decode_access_token(token)
    assert claims is not None
    assert claims["sub"] == user["id"]
    assert claims["tenant_id"] == user["tenant_id"]
    assert claims["role"] == "admin"
    assert store.decode_access_token(token + "tampered") is None


def test_auth_secret_must_be_strong():
    with pytest.raises(ValueError, match="at least 32"):
        PostgresIdentityStore(
            "postgresql://rusa:unused@localhost:5432/rusa",
            "weak",
        )



@pytest.fixture
def postgres_identity_store():
    import os

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    tenant = "identity-ci-" + uuid.uuid4().hex
    store = PostgresIdentityStore(
        dsn,
        "ci-auth-secret-that-is-independent-and-at-least-thirty-two-bytes",
    )
    store.ensure_schema()
    try:
        yield store, tenant
    finally:
        with store._connect() as db:
            db.execute("DELETE FROM rusa_users WHERE tenant_id=%s", (tenant,))


def test_postgres_identity_password_auth_and_tenant_scoping(postgres_identity_store):
    store, tenant = postgres_identity_store
    user = store.create_user(
        tenant,
        "member@example.test",
        "Workspace Member",
        "a-strong-password-for-ci",
        role="user",
    )
    assert store.authenticate(tenant, "member@example.test", "a-strong-password-for-ci")["id"] == user["id"]
    assert store.authenticate("another-tenant", "member@example.test", "a-strong-password-for-ci") is None
    assert store.authenticate(tenant, "member@example.test", "wrong-password") is None


def test_postgres_refresh_tokens_rotate_and_revoke(postgres_identity_store):
    store, tenant = postgres_identity_store
    user = store.create_user(
        tenant,
        "session@example.test",
        "Session User",
        "a-strong-password-for-ci",
    )
    first = store.issue_session(user)
    assert store.decode_access_token(first["access_token"])["sub"] == user["id"]

    second = store.rotate_refresh_token(first["refresh_token"])
    assert second is not None
    assert second["refresh_token"] != first["refresh_token"]
    assert store.rotate_refresh_token(first["refresh_token"]) is None
    assert store.revoke_refresh_token(second["refresh_token"]) is True
    assert store.rotate_refresh_token(second["refresh_token"]) is None


def test_disabling_user_revokes_sessions_and_last_admin_is_protected(postgres_identity_store):
    store, tenant = postgres_identity_store
    admin = store.create_user(
        tenant,
        "admin@example.test",
        "Tenant Admin",
        "a-strong-password-for-ci",
        role="admin",
    )
    user = store.create_user(
        tenant,
        "disabled@example.test",
        "Disabled User",
        "a-strong-password-for-ci",
    )
    session = store.issue_session(user)
    with pytest.raises(IdentityError, match="last active administrator"):
        store.update_user(tenant, admin["id"], role="user")
    with pytest.raises(IdentityError, match="last active administrator"):
        store.update_user(tenant, admin["id"], is_active=False)

    updated = store.update_user(tenant, user["id"], is_active=False)
    assert updated is not None and updated["is_active"] is False
    assert store.rotate_refresh_token(session["refresh_token"]) is None


def test_postgres_bootstrap_admin_does_not_reset_existing_password(postgres_identity_store):
    store, tenant = postgres_identity_store
    first = store.bootstrap_admin(
        tenant,
        "bootstrap@example.test",
        "first-bootstrap-password",
    )
    second = store.bootstrap_admin(
        tenant,
        "bootstrap@example.test",
        "second-bootstrap-password",
    )
    assert first["id"] == second["id"]
    assert store.authenticate(tenant, "bootstrap@example.test", "first-bootstrap-password") is not None
    assert store.authenticate(tenant, "bootstrap@example.test", "second-bootstrap-password") is None


def test_postgres_login_attempts_are_rate_limited_and_reset(postgres_identity_store):
    store, _ = postgres_identity_store
    bucket = "identity-ci-rate-limit-" + uuid.uuid4().hex
    try:
        assert store.allow_login_attempt(bucket, max_attempts=2) is True
        assert store.allow_login_attempt(bucket, max_attempts=2) is True
        assert store.allow_login_attempt(bucket, max_attempts=2) is False
        store.clear_login_attempts(bucket)
        assert store.allow_login_attempt(bucket, max_attempts=2) is True
    finally:
        store.clear_login_attempts(bucket)

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

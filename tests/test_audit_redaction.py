from rusa_agents.audit import AuditLog, redact_audit_value


def test_audit_redaction_covers_nested_secrets_and_common_token_formats(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit = AuditLog(str(path), hmac_key="audit-test-signing-key-at-least-32-bytes")
    audit.record(
        "task.context",
        "task-1",
        "pytest",
        {
            "api_key": "api-secret-value",
            "nested": {
                "Authorization": "Bearer super-secret-bearer",
                "note": "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
            },
            "connection": "postgresql://rusa:db-secret@db.internal/rusa?sslmode=verify-full",
            "message": "token=plain-token-value",
            "cloud": "AKIAABCDEFGHIJKLMNOP",
            "slack": "xoxb-1234567890-abcdefghij",
            "stripe": "sk_live_12345678901234567890",
            "google": "AIza12345678901234567890123456789012345",
            "pem": "-----BEGIN PRIVATE KEY-----\\nprivate-key-material\\n-----END PRIVATE KEY-----",
        },
    )
    raw = path.read_text(encoding="utf-8")
    for secret in (
        "api-secret-value",
        "super-secret-bearer",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
        "db-secret",
        "plain-token-value",
        "AKIAABCDEFGHIJKLMNOP",
        "xoxb-1234567890-abcdefghij",
        "sk_live_12345678901234567890",
        "AIza12345678901234567890123456789012345",
        "private-key-material",
    ):
        assert secret not in raw
    assert "[REDACTED]" in raw
    assert audit.verify()


def test_redaction_does_not_mutate_input_and_preserves_non_secret_fields():
    source = {
        "status": "success",
        "details": [{"password": "secret", "count": 3}],
    }
    safe = redact_audit_value(source)
    assert safe["status"] == "success"
    assert safe["details"][0]["password"] == "[REDACTED]"
    assert source["details"][0]["password"] == "secret"

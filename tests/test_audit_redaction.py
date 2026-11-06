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
        },
    )
    raw = path.read_text(encoding="utf-8")
    for secret in (
        "api-secret-value",
        "super-secret-bearer",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
        "db-secret",
        "plain-token-value",
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

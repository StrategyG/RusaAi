import pytest

from rusa_knowledge.safety import reject_credential_material, validate_document_metadata


def test_rejects_private_key_material_without_echoing_secret():
    text = "-----BEGIN RSA PRIVATE KEY-----\n" + ("A" * 64) + "\n-----END RSA PRIVATE KEY-----"
    with pytest.raises(ValueError, match="private-key material") as exc:
        reject_credential_material(text)
    assert "A" * 32 not in str(exc.value)


def test_rejects_github_and_aws_credentials():
    with pytest.raises(ValueError, match="GitHub credential"):
        reject_credential_material("token: ghp_" + ("a" * 36))
    with pytest.raises(ValueError, match="AWS access-key identifier"):
        reject_credential_material("AKIA1234567890ABCDEF")


def test_allows_non_secret_technical_documentation():
    reject_credential_material("Use an API key stored in the deployment secret manager.")


def test_rejects_credentials_in_document_metadata():
    token = "ghp_" + ("b" * 36)
    with pytest.raises(ValueError, match="GitHub credential"):
        validate_document_metadata("file:///guide.txt", "Guide", {"owner_token": token})


def test_rejects_credentials_embedded_in_source_uri():
    with pytest.raises(ValueError, match="embedded credentials"):
        validate_document_metadata("https://user:password@example.com/private", "Guide", {})


def test_rejects_credentials_in_document_title():
    token = "ghp_" + ("c" * 36)
    with pytest.raises(ValueError, match="GitHub credential"):
        validate_document_metadata("file:///guide.txt", token, {})


def test_rejects_non_json_metadata():
    with pytest.raises(ValueError, match="JSON-serializable"):
        validate_document_metadata("file:///guide.txt", "Guide", {"bad": object()})


def test_rejects_metadata_over_utf8_byte_limit():
    with pytest.raises(ValueError, match="16 KiB"):
        validate_document_metadata("file:///guide.txt", "Guide", {"text": "é" * 9000})


def test_rejects_provider_api_credentials():
    with pytest.raises(ValueError, match="provider API credential"):
        reject_credential_material("embedding_api_key=sk-proj-" + ("x" * 36))


def test_agent_specific_metadata_requires_explicit_valid_agent_scope():
    with pytest.raises(ValueError, match="knowledge_scope must be explicit"):
        validate_document_metadata(
            "file:///guide.txt", "Guide", {"agent_ids": ["aicode-analyst"]}
        )
    with pytest.raises(ValueError, match="requires at least one agent_id"):
        validate_document_metadata(
            "file:///guide.txt", "Guide", {"knowledge_scope": "agent_specific"}
        )


def test_agent_and_project_scope_metadata_is_validated():
    validate_document_metadata(
        "file:///guide.txt",
        "Guide",
        {
            "knowledge_scope": "agent_specific",
            "agent_ids": ["aicode-analyst", "aicode-security"],
            "project_id": "strategyg--rusaai",
        },
    )
    with pytest.raises(ValueError, match="unique array"):
        validate_document_metadata(
            "file:///guide.txt",
            "Guide",
            {
                "knowledge_scope": "agent_specific",
                "agent_ids": ["aicode-analyst", "aicode-analyst"],
            },
        )
    with pytest.raises(ValueError, match="project_id"):
        validate_document_metadata(
            "file:///guide.txt",
            "Guide",
            {
                "knowledge_scope": "agent_specific",
                "agent_ids": ["aicode-analyst"],
                "project_id": "../tenant-b",
            },
        )

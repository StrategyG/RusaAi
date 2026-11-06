import pytest

from rusa_knowledge.safety import reject_credential_material


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

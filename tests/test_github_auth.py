import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from rusa_agents.github_auth import GitHubAppTokenProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def _provider():
    return GitHubAppTokenProvider(
        app_id=123456,
        private_key="-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----",
        tenant_installations={"tenant-a": 987654321},
        tenant_repositories={"tenant-a": ["StrategyG/RusaAi", "StrategyG/OtherRepo"]},
    )


def test_github_app_mints_one_repo_scoped_token_and_caches_it(monkeypatch):
    jwt_calls = []
    monkeypatch.setitem(
        sys.modules,
        "jwt",
        SimpleNamespace(
            encode=lambda payload, key, algorithm: (
                jwt_calls.append((payload, key, algorithm)) or "app-jwt"
            )
        ),
    )
    calls = []
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=50)).isoformat()
    response = FakeResponse({"token": "ghs_" + "t" * 40, "expires_at": expires_at})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeClient(response, calls))

    provider = _provider()
    first = provider.token_for("tenant-a", "StrategyG/RusaAi")
    second = provider.token_for("tenant-a", "StrategyG/RusaAi")

    assert first == second == "ghs_" + "t" * 40
    assert len(calls) == 1
    assert len(jwt_calls) == 1
    assert jwt_calls[0][2] == "RS256"
    assert calls[0]["url"].endswith("/app/installations/987654321/access_tokens")
    assert calls[0]["json"]["repositories"] == ["RusaAi"]
    assert calls[0]["json"]["permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "checks": "read",
        "metadata": "read",
    }


def test_github_app_refuses_cross_tenant_or_unallowlisted_repository():
    provider = _provider()
    with pytest.raises(PermissionError, match="no configured GitHub App installation"):
        provider.token_for("tenant-b", "StrategyG/RusaAi")
    with pytest.raises(PermissionError, match="not authorized"):
        provider.token_for("tenant-a", "OtherOrg/RusaAi")


def test_github_app_rejects_nearly_expired_installation_token(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "jwt",
        SimpleNamespace(encode=lambda payload, key, algorithm: "app-jwt"),
    )
    calls = []
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    response = FakeResponse({"token": "ghs_" + "t" * 40, "expires_at": expires_at})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: FakeClient(response, calls))

    with pytest.raises(RuntimeError, match="nearly expired"):
        _provider().token_for("tenant-a", "StrategyG/RusaAi")


def test_github_app_from_env_reads_private_key_and_installation_map(tmp_path, monkeypatch):
    key_file = tmp_path / "github-app.pem"
    key_file.write_text("-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----")
    key_file.chmod(0o400)
    monkeypatch.setenv("RUSA_GITHUB_APP_ID", "123456")
    monkeypatch.setenv("RUSA_GITHUB_APP_PRIVATE_KEY_FILE", str(key_file))
    monkeypatch.setenv("RUSA_TENANT_GITHUB_INSTALLATIONS_JSON", '{"tenant-a":987654321}')

    provider = GitHubAppTokenProvider.from_env({"tenant-a": ["StrategyG/RusaAi"]})

    assert provider.app_id == 123456
    assert provider.tenant_installations == {"tenant-a": 987654321}


def test_github_app_rejects_shared_installation_and_cross_tenant_repository():
    with pytest.raises(ValueError, match="distinct GitHub App installation"):
        GitHubAppTokenProvider(
            123456,
            "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----",
            {"tenant-a": 123, "tenant-b": 123},
            {"tenant-a": ["Org/RepoA"], "tenant-b": ["Org/RepoB"]},
        )
    with pytest.raises(ValueError, match="multiple tenants"):
        GitHubAppTokenProvider(
            123456,
            "-----BEGIN PRIVATE KEY-----\nkey\n-----END PRIVATE KEY-----",
            {"tenant-a": 123, "tenant-b": 456},
            {"tenant-a": ["Org/Shared"], "tenant-b": ["Org/Shared"]},
        )

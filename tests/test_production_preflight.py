import json
from pathlib import Path

import yaml

from rusa_agents.production_preflight import validate_production_environment


def _set_valid_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "RUSA_API_IMAGE",
        "ghcr.io/strategyg/rusa-ai-api@sha256:" + "a" * 64,
    )
    monkeypatch.setenv(
        "RUSA_WORKER_IMAGE",
        "ghcr.io/strategyg/rusa-ai-worker@sha256:" + "b" * 64,
    )
    monkeypatch.setenv(
        "RUSA_WEB_IMAGE",
        "ghcr.io/strategyg/rusa-ai-web@sha256:" + "c" * 64,
    )
    monkeypatch.setenv(
        "RUSA_SANDBOX_IMAGE",
        "ghcr.io/strategyg/rusa-ai-sandbox@sha256:" + "d" * 64,
    )
    monkeypatch.setenv(
        "RUSA_API_DATABASE_URL",
        "postgresql://rusa_api_login:ci-db-password-12345678901234567890123456789012@db/rusa?sslmode=verify-full&sslrootcert=/run/secrets/rusa-internal-ca.crt",
    )
    monkeypatch.setenv(
        "RUSA_WORKER_DATABASE_URL",
        "postgresql://rusa_worker_login:ci-db-password-12345678901234567890123456789012@db/rusa?sslmode=verify-full&sslrootcert=/run/secrets/rusa-internal-ca.crt",
    )
    worker_key = "worker-inference-secret-ci-12345678901234567890"
    monkeypatch.setenv(
        "RUSA_API_KEYS_JSON",
        json.dumps({
            "e" * 64: {"subject": "api-test", "tenant_id": "tenant-a", "roles": ["tenant_admin"]},
            "43e35305847a4f9e7a8dccdd4d8325c6ceaac628740ce992da2ad23969adcc45": {
                "subject": "worker-ci", "tenant_id": "tenant-a", "roles": ["developer"]
            },
        }),
    )
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_GATEWAY", "true")
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_BASE_URL", "https://api.internal/v1")
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_API_KEYS", json.dumps({"tenant-a": worker_key}))
    monkeypatch.setenv("RUSA_WORKER_INFERENCE_DEADLINE_MS", "270000")
    monkeypatch.setenv("RUSA_METRICS_API_KEY", "m" * 48)
    monkeypatch.setenv("RUSA_AUDIT_HMAC_KEY", "a" * 48)
    monkeypatch.setenv("RUSA_GITHUB_TOKEN", "github_pat_" + "g" * 32)
    monkeypatch.setenv("RUSA_WORKSPACE_ROOT", "/srv/rusa/workspaces")
    monkeypatch.setenv("RUSA_GIT_MIRROR_ROOT", "/srv/rusa/mirrors")
    monkeypatch.setenv("RUSA_TENANT_REPOSITORIES", '{"tenant-a":["StrategyG/RusaAi"]}')
    monkeypatch.setenv("RUSA_MODEL_BASE_URL", "https://model.internal/openai/v1")
    monkeypatch.setenv("RUSA_MODEL_API_KEY", "r" * 48)
    monkeypatch.setenv("RUSA_WEB_SESSION_SECRET", "w" * 48)


def test_production_preflight_accepts_valid_digest_pinned_configuration(monkeypatch):
    _set_valid_environment(monkeypatch)
    assert validate_production_environment() == []


def test_production_preflight_rejects_mutable_api_image_tag(monkeypatch):
    _set_valid_environment(monkeypatch)
    monkeypatch.setenv("RUSA_API_IMAGE", "ghcr.io/strategyg/rusa-ai-api:latest")
    errors = validate_production_environment()
    assert any("RUSA_API_IMAGE" in error and "sha256" in error for error in errors)


def test_production_preflight_rejects_image_from_unapproved_registry_path(monkeypatch):
    _set_valid_environment(monkeypatch)
    monkeypatch.setenv(
        "RUSA_WEB_IMAGE",
        "ghcr.io/untrusted/rusa-ai-web@sha256:" + "c" * 64,
    )
    errors = validate_production_environment()
    assert any("RUSA_WEB_IMAGE" in error for error in errors)



def test_ci_production_preflight_sets_production_environment():
    workflow_path = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == "Validate production Compose configuration":
                assert step.get("env", {}).get("ENVIRONMENT") == "production"
                return

    raise AssertionError("production Compose validation step is missing from CI")

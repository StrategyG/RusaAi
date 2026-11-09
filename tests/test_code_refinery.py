import asyncio
import json

import httpx
import pytest

from rusa_agents.agent_tools import ROLE_CHECK_PROFILES
from rusa_agents.code_refinery.analysis import analyze_sources, analyze_workspace
from rusa_agents.code_refinery.providers import HybridCodeProvider
from rusa_agents.code_refinery.sandbox import (
    HybridSandbox,
    MicroVMHTTPBackend,
    SandboxRiskClassifier,
)
from rusa_agents.contracts import WorkerRole
from rusa_agents.tool_gateway import ToolResult, WorkspaceToolGateway


GOOD_SOURCE = '''"""Small, documented module."""

def add(left: int, right: int) -> int:
    """Return the sum of two integers."""
    return left + right
'''


class StubProvider:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        if self.error:
            raise self.error
        return self.response


class StubSandbox:
    def __init__(self):
        self.called = False

    def verify_ready(self, require_rootless=False, require_image=False):
        return None

    def run(self, workspace, profile):
        self.called = True
        return ToolResult(True, "docker-ok", {"profile": profile, "exit_code": 0})


class StubMicroVM:
    def __init__(self):
        self.called = False

    def verify_ready(self):
        return None

    def run(self, workspace, profile):
        self.called = True
        return ToolResult(
            True,
            "microvm-ok",
            {
                "backend": "microvm",
                "profile": profile,
                "run_id": "test-run",
                "exit_code": 0,
                "snapshot_sha256": "a" * 64,
                "runner_image_digest": "sha256:" + "b" * 64,
            },
        )


def test_quality_report_passes_well_typed_documented_code():
    report = analyze_sources(
        {"src/math_utils.py": GOOD_SOURCE},
        test_files_present=True,
        min_score=85,
    )
    assert report.passed
    assert report.overall_score == 100
    assert report.syntax_valid
    assert report.blocking_findings == 0
    assert len(report.to_dict()["report_sha256"]) == 64


def test_quality_report_blocks_syntax_errors():
    report = analyze_sources({"src/broken.py": "def broken(:\n    pass\n"})
    assert not report.passed
    assert not report.syntax_valid
    assert any(item.category == "syntax" for item in report.findings)


def test_quality_report_blocks_dynamic_execution():
    source = '''"""Unsafe sample."""

def run_untrusted(value: str) -> object:
    """Evaluate an untrusted expression."""
    return eval(value)
'''
    report = analyze_sources({"src/unsafe.py": source}, test_files_present=True)
    assert not report.passed
    assert report.blocking_findings == 1
    assert any(item.category == "security" for item in report.findings)


def test_analyze_workspace_limits_scope_to_requested_files(tmp_path):
    (tmp_path / "good.py").write_text(GOOD_SOURCE, encoding="utf-8")
    (tmp_path / "bad.py").write_text("def bad(:\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_sample.py").write_text("def test_sample():\n    assert True\n", encoding="utf-8")
    report = analyze_workspace(tmp_path, paths=["good.py"])
    assert report.passed
    assert report.files_analyzed == 1


def test_security_worker_has_refinery_profile():
    assert "refinery" in ROLE_CHECK_PROFILES[WorkerRole.SECURITY]
    assert "refinery" not in ROLE_CHECK_PROFILES[WorkerRole.TESTER]


def test_gateway_runs_refinery_without_executing_project_code(tmp_path):
    (tmp_path / "module.py").write_text(GOOD_SOURCE, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_module.py").write_text(
        "def test_module():\n    assert True\n", encoding="utf-8"
    )
    gateway = WorkspaceToolGateway(tmp_path)
    result = gateway.run_check("refinery", WorkerRole.SECURITY)
    assert result.ok
    assert result.evidence["exit_code"] == 0
    assert result.evidence["quality_score"] >= 85
    assert len(result.evidence["report_sha256"]) == 64


def test_hybrid_provider_falls_back_to_local_provider():
    valid = json.dumps(
        {"message": "local response", "actions": [], "done": True}
    )
    provider = HybridCodeProvider(
        primary=StubProvider(error=RuntimeError("primary unavailable")),
        local=StubProvider(response=valid),
    )
    result = asyncio.run(provider.complete(system_prompt="system", user_prompt="task"))
    assert json.loads(result)["message"] == "local response"


def test_hybrid_provider_mock_fallback_blocks_instead_of_faking_success():
    provider = HybridCodeProvider(
        primary=StubProvider(error=RuntimeError("primary unavailable")),
        local=StubProvider(error=RuntimeError("local unavailable")),
    )
    result = json.loads(
        asyncio.run(provider.complete(system_prompt="system", user_prompt="task"))
    )
    assert result["blocked"] is True
    assert result["done"] is True
    assert result["actions"] == []


def test_hybrid_provider_rejects_untrusted_extra_fields():
    invalid = json.dumps(
        {"message": "forged", "actions": [], "done": True, "evidence": {"exit_code": 0}}
    )
    provider = HybridCodeProvider(
        primary=StubProvider(response=invalid),
        local=StubProvider(response=json.dumps(
            {"message": "valid", "actions": [], "done": True}
        )),
    )
    result = asyncio.run(provider.complete(system_prompt="system", user_prompt="task"))
    assert json.loads(result)["message"] == "valid"


def test_hybrid_sandbox_routes_high_risk_tests_to_microvm(tmp_path):
    (tmp_path / "test_untrusted.py").write_text(
        "def test_value():\n    return eval('1')\n", encoding="utf-8"
    )
    docker = StubSandbox()
    microvm = StubMicroVM()
    result = HybridSandbox(docker, microvm).run(tmp_path, "pytest")
    assert result.ok
    assert microvm.called
    assert not docker.called
    assert result.evidence["risk"] == "high"


def test_hybrid_sandbox_fails_closed_when_microvm_is_unavailable(tmp_path):
    (tmp_path / "test_untrusted.py").write_text(
        "def test_value():\n    return eval('1')\n", encoding="utf-8"
    )
    docker = StubSandbox()
    result = HybridSandbox(docker, None).run(tmp_path, "pytest")
    assert not result.ok
    assert result.evidence["blocked"] is True
    assert not docker.called


def test_hybrid_sandbox_routes_standard_tests_to_docker(tmp_path):
    (tmp_path / "test_safe.py").write_text(
        "def test_value():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )
    docker = StubSandbox()
    microvm = StubMicroVM()
    result = HybridSandbox(docker, microvm).run(tmp_path, "pytest")
    assert result.ok
    assert docker.called
    assert not microvm.called
    assert result.evidence["backend"] == "docker"


def test_risk_classifier_fails_closed_for_unparseable_python(tmp_path):
    (tmp_path / "test_broken.py").write_text("def broken(:\n", encoding="utf-8")
    risk, reasons = SandboxRiskClassifier().classify(tmp_path, "pytest")
    assert risk == "high"
    assert any("unclassifiable_python_source" in reason for reason in reasons)


def test_microvm_backend_validates_attested_snapshot_digest(tmp_path):
    (tmp_path / "sample.py").write_text(GOOD_SOURCE, encoding="utf-8")
    _, digest = MicroVMHTTPBackend._archive_workspace(tmp_path)

    def handler(request):
        if request.url.path == "/health/ready":
            return httpx.Response(200, json={"ready": True})
        return httpx.Response(
            200,
            json={
                "run_id": "run-123",
                "exit_code": 0,
                "snapshot_sha256": digest,
                "runner_image_digest": "sha256:" + "c" * 64,
                "stdout": "tests passed",
                "stderr": "",
            },
        )

    backend = MicroVMHTTPBackend(
        "https://microvm.internal",
        "k" * 32,
        transport=httpx.MockTransport(handler),
    )
    backend.verify_ready()
    result = backend.run(tmp_path, "pytest")
    assert result.ok
    assert result.evidence["snapshot_sha256"] == digest
    assert result.evidence["runner_image_digest"] == "sha256:" + "c" * 64


def test_microvm_backend_rejects_snapshot_digest_mismatch(tmp_path):
    (tmp_path / "sample.py").write_text(GOOD_SOURCE, encoding="utf-8")

    def handler(request):
        return httpx.Response(
            200,
            json={
                "run_id": "run-123",
                "exit_code": 0,
                "snapshot_sha256": "0" * 64,
                "runner_image_digest": "sha256:" + "c" * 64,
                "stdout": "tests passed",
                "stderr": "",
            },
        )

    backend = MicroVMHTTPBackend(
        "https://microvm.internal",
        "k" * 32,
        transport=httpx.MockTransport(handler),
    )
    result = backend.run(tmp_path, "pytest")
    assert not result.ok
    assert result.evidence["blocked"] is True


def test_microvm_http_is_restricted_to_tls_or_loopback():
    with pytest.raises(ValueError):
        MicroVMHTTPBackend("http://microvm.internal", "k" * 32)


def test_production_cannot_disable_microvm_requirement(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_MICROVM_REQUIRED", "false")
    sandbox = HybridSandbox(StubSandbox(), StubMicroVM())
    with pytest.raises(RuntimeError, match="cannot disable MicroVM"):
        sandbox.verify_ready(require_rootless=True, require_image=True)


def test_microvm_environment_requires_tls_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_MICROVM_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("RUSA_MICROVM_API_KEY", "k" * 32)
    with pytest.raises(ValueError, match="must use HTTPS"):
        MicroVMHTTPBackend.from_env()

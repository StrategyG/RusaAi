# ruff: noqa: I001
import asyncio
import io
import json
import tarfile
from types import SimpleNamespace

import httpx
import pytest

from rusa_agents.agent_tools import ROLE_CHECK_PROFILES
from rusa_agents.code_refinery import analysis as analysis_module
from rusa_agents.code_refinery.analysis import analyze_sources, analyze_workspace
from rusa_agents.code_refinery.providers import HybridCodeProvider
from rusa_agents.code_refinery.sandbox import (
    HybridSandbox,
    MicroVMHTTPBackend,
    SandboxRiskClassifier,
)
from rusa_agents.contracts import WorkerRole
from rusa_agents.providers import OpenAICompatibleProvider
from rusa_agents import tool_gateway as gateway_module
from rusa_agents.tool_gateway import DockerSandbox, ToolDenied, ToolResult, WorkspaceToolGateway
from rusa_agents.secret_scan import contains_high_confidence_secret


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



def test_microvm_snapshot_is_deterministic_and_excludes_secrets(tmp_path):
    (tmp_path / "app.py").write_text(GOOD_SOURCE, encoding="utf-8")
    (tmp_path / ".env").write_text("API_KEY=must-not-leave-workspace", encoding="utf-8")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "token.txt").write_text("private-token", encoding="utf-8")

    first, first_digest = MicroVMHTTPBackend._archive_workspace(tmp_path)
    second, second_digest = MicroVMHTTPBackend._archive_workspace(tmp_path)

    assert first_digest == second_digest
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
        names = archive.getnames()
        assert "app.py" in names
        assert ".env" not in names
        assert "secrets/token.txt" not in names



def test_local_provider_does_not_inherit_primary_api_key(monkeypatch):
    monkeypatch.setenv("RUSA_MODEL_API_KEY", "primary-provider-secret")
    local = OpenAICompatibleProvider(
        base_url="http://localhost:9000/v1",
        api_key="",
    )
    assert local.api_key == ""


def test_primary_provider_uses_default_api_key_when_not_overridden(monkeypatch):
    monkeypatch.setenv("RUSA_MODEL_API_KEY", "primary-provider-secret")
    primary = OpenAICompatibleProvider(base_url="https://model.example/v1")
    assert primary.api_key == "primary-provider-secret"



def test_ast_analysis_enforces_aggregate_source_limit(monkeypatch):
    monkeypatch.setattr(analysis_module, "MAX_TOTAL_SOURCE_BYTES", 4)
    with pytest.raises(ValueError, match="Total source inventory"):
        analyze_sources({"one.py": "12345"})


def test_test_modules_are_not_penalized_for_optional_annotations_or_docstrings():
    source = "def test_simple_case():\n    assert 2 + 2 == 4\n"
    report = analyze_sources(
        {"tests/test_simple_case.py": source},
        test_files_present=True,
    )
    assert report.passed
    assert report.metrics["type_safety"] == 100
    assert report.metrics["documentation"] == 100



def test_analyzer_blocks_dynamic_import_and_shell_subprocess():
    dynamic_import = '''"""Dynamic import sample."""

import importlib

def load_module(name: str):
    return importlib.import_module(name)
'''
    shell_subprocess = '''"""Shell subprocess sample."""

import subprocess

def run(command: str, shell_mode: bool):
    return subprocess.run(command, shell=shell_mode)
'''
    dynamic_report = analyze_sources(
        {"src/dynamic_import.py": dynamic_import},
        test_files_present=True,
    )
    shell_report = analyze_sources(
        {"src/shell_command.py": shell_subprocess},
        test_files_present=True,
    )
    assert not dynamic_report.passed
    assert dynamic_report.blocking_findings == 1
    assert not shell_report.passed
    assert shell_report.blocking_findings == 1


def test_classifier_routes_dynamic_import_and_unknown_shell_to_microvm(tmp_path):
    dynamic = tmp_path / "test_dynamic.py"
    dynamic.write_text(
        "import importlib\ndef test_dynamic():\n    importlib.import_module('plugin')\n",
        encoding="utf-8",
    )
    risk, reasons = SandboxRiskClassifier().classify(tmp_path, "pytest")
    assert risk == "high"
    assert any("importlib.import_module" in reason for reason in reasons)

    dynamic.unlink()
    shell = tmp_path / "test_shell.py"
    shell.write_text(
        "import subprocess\ndef test_shell(mode):\n    subprocess.run('echo x', shell=mode)\n",
        encoding="utf-8",
    )
    risk, reasons = SandboxRiskClassifier().classify(tmp_path, "pytest")
    assert risk == "high"
    assert any("subprocess.run" in reason for reason in reasons)


def test_microvm_snapshot_rejects_embedded_high_confidence_credentials(tmp_path):
    token = "ghp_" + "A" * 36
    (tmp_path / "module.py").write_text(
        f"TOKEN = '{token}'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="high-confidence credential material"):
        MicroVMHTTPBackend._archive_workspace(tmp_path)


def test_microvm_from_env_requires_pinned_image_digest_in_production(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RUSA_MICROVM_BASE_URL", "https://microvm.internal")
    monkeypatch.setenv("RUSA_MICROVM_API_KEY", "k" * 32)
    monkeypatch.delenv("RUSA_MICROVM_RUNNER_IMAGE_DIGEST", raising=False)
    with pytest.raises(ValueError, match="RUSA_MICROVM_RUNNER_IMAGE_DIGEST"):
        MicroVMHTTPBackend.from_env()


def test_microvm_readiness_rejects_runner_digest_mismatch():
    expected = "sha256:" + "a" * 64

    def handler(request):
        return httpx.Response(
            200,
            json={"ready": True, "runner_image_digest": "sha256:" + "b" * 64},
        )

    backend = MicroVMHTTPBackend(
        "https://microvm.internal",
        "k" * 32,
        expected_runner_image_digest=expected,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(RuntimeError, match="does not match the pinned image"):
        backend.verify_ready()


def test_microvm_execution_rejects_runner_digest_mismatch(tmp_path):
    (tmp_path / "sample.py").write_text(GOOD_SOURCE, encoding="utf-8")
    _, digest = MicroVMHTTPBackend._archive_workspace(tmp_path)

    def handler(request):
        return httpx.Response(
            200,
            json={
                "run_id": "run-123",
                "exit_code": 0,
                "snapshot_sha256": digest,
                "runner_image_digest": "sha256:" + "b" * 64,
                "stdout": "tests passed",
                "stderr": "",
            },
        )

    backend = MicroVMHTTPBackend(
        "https://microvm.internal",
        "k" * 32,
        expected_runner_image_digest="sha256:" + "a" * 64,
        transport=httpx.MockTransport(handler),
    )
    result = backend.run(tmp_path, "pytest")
    assert not result.ok
    assert result.evidence["blocked"] is True


def test_microvm_invalid_utf8_response_fails_closed(tmp_path):
    (tmp_path / "sample.py").write_text(GOOD_SOURCE, encoding="utf-8")

    def handler(request):
        return httpx.Response(200, content=bytes([255]))

    backend = MicroVMHTTPBackend(
        "https://microvm.internal",
        "k" * 32,
        transport=httpx.MockTransport(handler),
    )
    result = backend.run(tmp_path, "pytest")
    assert not result.ok
    assert result.evidence["blocked"] is True



def test_docker_snapshot_rejects_embedded_high_confidence_credentials(tmp_path, monkeypatch):
    token = "ghp_" + "A" * 36
    (tmp_path / "module.py").write_text(
        f"TOKEN = '{token}'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_module, "DOCKER_BIN", "/usr/bin/docker")
    result = DockerSandbox().run(tmp_path, "pytest")
    assert not result.ok
    assert "high-confidence credential material" in result.output


def test_shared_secret_scanner_detects_token_across_chunk_boundary(tmp_path):
    token = "ghp_" + "B" * 36
    source = tmp_path / "large.txt"
    source.write_bytes(b"x" * (64 * 1024 - 2) + token.encode("ascii"))
    assert contains_high_confidence_secret(source)



def test_analyzer_marks_invalid_unicode_as_blocking_input():
    invalid_text = "value = '" + chr(0xDCFF) + "'\n"
    report = analyze_sources({"src/invalid.py": invalid_text})
    assert not report.passed
    assert not report.syntax_valid
    assert not report.not_applicable
    assert any(item.category == "input" for item in report.findings)


def test_analyzer_fails_closed_when_parser_exceeds_recursion_limit(monkeypatch):
    def fail_parse(source, filename):
        raise RecursionError("parser recursion limit")

    monkeypatch.setattr(analysis_module.ast, "parse", fail_parse)
    report = analyze_sources({"src/deep.py": "value = 1\n"})
    assert not report.passed
    assert not report.syntax_valid
    assert any(item.category == "analysis" for item in report.findings)


def test_analyzer_does_not_call_oversized_input_not_applicable(monkeypatch):
    monkeypatch.setattr(analysis_module, "MAX_FILE_BYTES", 4)
    report = analyze_sources({"src/oversized.py": "12345"})
    assert not report.passed
    assert not report.syntax_valid
    assert not report.not_applicable



def test_gateway_withholds_embedded_credentials_from_agent_reads(tmp_path):
    token = "ghp_" + "C" * 36
    (tmp_path / "module.py").write_text(f"TOKEN = '{token}'\n", encoding="utf-8")
    result = WorkspaceToolGateway(tmp_path).read_file("module.py")
    assert not result.ok
    assert token not in result.output
    assert result.evidence["secret_scan"] == "blocked"


def test_gateway_rejects_writes_containing_high_confidence_credentials(tmp_path):
    token = "ghp_" + "D" * 36
    gateway = WorkspaceToolGateway(tmp_path)
    with pytest.raises(ToolDenied, match="Writing high-confidence credential"):
        gateway.write_file("new_module.py", f"TOKEN = '{token}'\n", WorkerRole.DEVELOPER)
    assert not (tmp_path / "new_module.py").exists()


def test_gateway_withholds_secret_bearing_git_diff(tmp_path, monkeypatch):
    token = "ghp_" + "E" * 36
    monkeypatch.setattr(gateway_module, "GIT_BIN", "/usr/bin/git")
    monkeypatch.setattr(
        gateway_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=token, stderr=""),
    )
    result = WorkspaceToolGateway(tmp_path).git_diff()
    assert not result.ok
    assert token not in result.output
    assert result.evidence["secret_scan"] == "blocked"


def test_analyzer_resolves_aliased_dynamic_import_and_process_calls():
    source = '''"""Alias-aware security sample."""
import importlib as loader
import subprocess as proc
from os import system as launch

def load_plugin(name: str):
    return loader.import_module(name)

def execute(command: str, shell_mode: bool):
    proc.run(command, shell=shell_mode)
    launch(command)
'''
    report = analyze_sources({"src/aliases.py": source}, test_files_present=True)
    assert not report.passed
    assert report.blocking_findings >= 3
    names = [item.message for item in report.findings if item.category == "security"]
    assert any("importlib.import_module" in item for item in names)
    assert any("subprocess.run" in item for item in names)
    assert any("os.system" in item for item in names)


def test_classifier_routes_aliased_dynamic_execution_to_microvm(tmp_path):
    source = '''"""Aliased subprocess test."""
import subprocess as proc
from importlib import import_module as load

def test_run(flag):
    proc.run("echo x", shell=flag)
    load("optional_plugin")
'''
    (tmp_path / "test_aliases.py").write_text(source, encoding="utf-8")
    risk, reasons = SandboxRiskClassifier().classify(tmp_path, "pytest")
    assert risk == "high"
    assert any("subprocess.run" in reason for reason in reasons)
    assert any("importlib.import_module" in reason for reason in reasons)


def test_shared_secret_scanner_detects_modern_credential_formats():
    from rusa_agents.secret_scan import contains_high_confidence_secret_text

    assert contains_high_confidence_secret_text("token=github_pat_" + "a" * 40)
    assert contains_high_confidence_secret_text("key=sk-proj-" + "b" * 40)
    assert contains_high_confidence_secret_text("token=xoxb-" + "c" * 24)

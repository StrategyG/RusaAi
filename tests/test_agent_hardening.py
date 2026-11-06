import asyncio
import base64

from rusa_agents.contracts import CodingTask, RepositoryTarget, ToolCall
from rusa_agents.github import GitHubToolGateway


def make_task():
    tenant_id = "tenant-a"
    identity = "hardening-test-key"
    task_id = "hardening-test-task-001"
    return CodingTask(
        tenant_id=tenant_id,
        objective="Review bounded repository changes",
        repository=RepositoryTarget(
            full_name="StrategyG/RusaAi",
            base_branch="main",
            work_branch=CodingTask.expected_work_branch_for(tenant_id, identity, task_id),
        ),
        acceptance_criteria=("CI passes", "Security review passes"),
        idempotency_key=identity,
        task_id=task_id,
    )


def make_gateway(**overrides):
    options = {
        "token": "test-token",
        "allowed_repositories_by_tenant": {"tenant-a": {"StrategyG/RusaAi"}},
    }
    options.update(overrides)
    return GitHubToolGateway(**options)


def run(coro):
    return asyncio.run(coro)


def test_ci_evidence_paginates_all_check_runs(monkeypatch):
    gateway = make_gateway()
    task = make_task()
    calls = []

    def check_run(index):
        return {
            "id": index + 1,
            "name": f"check-{index}",
            "status": "completed",
            "conclusion": "success",
            "app": {"id": 99},
            "created_at": f"2026-11-06T00:{index // 60:02d}:{index % 60:02d}Z",
        }

    async def fake_request(method, path, *, json_body=None, params=None, allow_not_found=False):
        calls.append((path, params))
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        if "/check-runs" in path:
            page = int(params["page"])
            items = [check_run(i) for i in range(100)] if page == 1 else [check_run(100)]
            return {"total_count": 101, "check_runs": items}
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = run(gateway.execute(task, ToolCall("get_ci_status")))

    assert result.success is True
    assert result.data["checks_complete"] is True
    assert result.data["raw_total_count"] == 101
    assert result.data["total_count"] == 101
    assert len(result.data["checks"]) == 101
    assert result.data["all_checks_success"] is True
    assert any(params and params.get("page") == "2" for _, params in calls)


def test_ci_evidence_uses_latest_check_run_for_reruns(monkeypatch):
    gateway = make_gateway()
    task = make_task()

    async def fake_request(method, path, *, json_body=None, params=None, allow_not_found=False):
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        if "/check-runs" in path:
            return {
                "total_count": 3,
                "check_runs": [
                    {
                        "id": 1, "name": "unit-tests", "status": "completed",
                        "conclusion": "failure", "app": {"id": 7},
                        "started_at": "2026-11-06T10:00:00Z",
                    },
                    {
                        "id": 2, "name": "unit-tests", "status": "completed",
                        "conclusion": "success", "app": {"id": 7},
                        "started_at": "2026-11-06T10:05:00Z",
                    },
                    {
                        "id": 3, "name": "security", "status": "completed",
                        "conclusion": "success", "app": {"id": 7},
                        "started_at": "2026-11-06T10:04:00Z",
                    },
                ],
            }
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = run(gateway.execute(task, ToolCall("get_ci_status")))
    assert result.data["checks_complete"] is True
    assert result.data["total_count"] == 2
    assert result.data["all_checks_success"] is True
    unit = next(item for item in result.data["checks"] if item["name"] == "unit-tests")
    assert unit["id"] == 2
    assert unit["conclusion"] == "success"


def test_ci_evidence_fails_closed_when_pagination_is_incomplete(monkeypatch):
    gateway = make_gateway()
    task = make_task()

    async def fake_request(method, path, *, json_body=None, params=None, allow_not_found=False):
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        if "/check-runs" in path:
            page = int(params["page"])
            runs = [
                {
                    "id": i + 1, "name": f"check-{i}", "status": "completed",
                    "conclusion": "success", "app": {"id": 1},
                }
                for i in range(100)
            ] if page == 1 else []
            return {"total_count": 101, "check_runs": runs}
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = run(gateway.execute(task, ToolCall("get_ci_status")))
    assert result.data["checks_complete"] is False
    assert result.data["all_checks_success"] is False


def test_security_diff_redacts_credentials_and_marks_them(monkeypatch):
    gateway = make_gateway()
    task = make_task()

    async def fake_request(method, path, *, json_body=None, params=None, allow_not_found=False):
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "b" * 40}}
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        if "/compare/" in path:
            return {
                "status": "ahead",
                "behind_by": 0,
                "changed_files": 1,
                "files": [{
                    "filename": "src/settings.py",
                    "status": "modified",
                    "additions": 1,
                    "deletions": 0,
                    "patch": '+api_key = "supersecret-value-123456"\n+github_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"',
                }],
            }
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = run(gateway.execute(task, ToolCall("get_branch_diff")))
    patch = result.data["files"][0]["patch"]
    assert "[REDACTED]" in patch
    assert "[REDACTED TOKEN]" in patch
    assert result.data["sensitive_content_detected"] is True
    assert result.data["diff_complete"] is True


def test_security_diff_fails_closed_for_protected_or_truncated_files(monkeypatch):
    gateway = make_gateway(max_diff_files=1)
    task = make_task()

    async def fake_request(method, path, *, json_body=None, params=None, allow_not_found=False):
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "b" * 40}}
        if "/branches/" in path:
            return {"commit": {"sha": "a" * 40}}
        if "/compare/" in path:
            return {
                "status": "ahead",
                "behind_by": 0,
                "changed_files": 2,
                "files": [
                    {"filename": ".env", "status": "modified", "patch": "+SECRET=abc"},
                    {"filename": "src/second.py", "status": "modified", "patch": "+ok = True"},
                ],
            }
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = run(gateway.execute(task, ToolCall("get_branch_diff")))
    assert result.data["diff_complete"] is False
    assert len(result.data["files"]) == 1
    assert result.data["files"][0]["patch"] == ""


def test_repository_reads_redact_sensitive_values(monkeypatch):
    gateway = make_gateway()
    task = make_task()
    raw_content = b'password = "do-not-expose-this-value"'
    payload = base64.b64encode(raw_content).decode()

    async def fake_request(method, path, *, json_body=None, params=None, allow_not_found=False):
        if "/contents/" in path:
            return {
                "type": "file",
                "content": payload,
                "encoding": "base64",
                "size": len(raw_content),
            }
        raise AssertionError(path)

    monkeypatch.setattr(gateway, "_request", fake_request)
    result = run(gateway.execute(task, ToolCall("read_file", {"path": "src/config.py"})))
    assert "do-not-expose-this-value" not in result.data["content"]

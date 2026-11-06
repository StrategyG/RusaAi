from __future__ import annotations
import asyncio
import pytest
from rusa_agents.audit import AuditLog
from rusa_agents.contracts import CodingTask,RepositoryTarget,TaskRecord,TaskState
from rusa_agents.memory import SQLiteMemory
from rusa_agents.policy import AutonomyPolicy,RiskLevel
from rusa_agents.sandbox import ToolDenied,WorkspaceGateway

def make_task():
    return CodingTask("Implement bounded coding agent",RepositoryTarget("StrategyG/RusaAi"),["Tests pass"])

def test_task_requires_criteria():
    with pytest.raises(ValueError):
        CodingTask("x",RepositoryTarget("owner/repo"),[])

def test_state_machine_rejects_terminal_shortcut():
    with pytest.raises(ValueError):
        TaskRecord(make_task()).transition(TaskState.SUCCEEDED)

def test_policy_fails_closed():
    policy=AutonomyPolicy()
    assert policy.evaluate("workspace_write").allowed
    assert policy.evaluate("deploy").requires_approval
    assert not policy.evaluate("disable_audit").allowed
    assert not policy.evaluate("unknown").allowed
    assert policy.evaluate("repo_read",RiskLevel.CRITICAL).requires_approval

def test_empty_allowlist_remains_empty():
    assert not AutonomyPolicy(set()).evaluate("repo_read").allowed

def test_memory_repository_scope(tmp_path):
    memory=SQLiteMemory(str(tmp_path/"memory.sqlite3"))
    memory.save("task-1","owner/repo","executing",{"attempt":2})
    assert memory.get("task-1","owner/repo")["payload"]["attempt"]==2
    assert memory.get("task-1","other/repo") is None

def test_audit_hash_chain(tmp_path):
    audit=AuditLog(str(tmp_path/"audit.jsonl"))
    first=audit.record("started","task-1","master")
    second=audit.record("finished","task-1","master")
    assert second["previous_hash"]==first["hash"]

def test_workspace_path_confined(tmp_path):
    root=tmp_path/"repo"
    root.mkdir()
    gateway=WorkspaceGateway(root)
    gateway.write_text("src/example.py","value = 1\n")
    assert gateway.read_text("src/example.py")=="value = 1\n"
    with pytest.raises(ToolDenied):
        gateway.write_text("../escape.txt","bad")

def test_gateway_rejects_shell_injection(tmp_path):
    root=tmp_path/"repo"
    root.mkdir()
    gateway=WorkspaceGateway(root)
    with pytest.raises(ToolDenied):
        asyncio.run(gateway.run_command(["python","-m","pytest",";","echo","bad"]))

def test_gateway_rejects_unknown_command(tmp_path):
    root=tmp_path/"repo"
    root.mkdir()
    with pytest.raises(ToolDenied):
        asyncio.run(WorkspaceGateway(root).run_command(["bash","-c","id"]))

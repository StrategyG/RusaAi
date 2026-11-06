import pytest

from rusa_agents.audit import AuditLog
from rusa_agents.memory import SQLiteMemory


def test_memory_is_scoped_by_tenant_and_project(tmp_path):
    memory = SQLiteMemory(str(tmp_path / "memory.sqlite3"))
    memory.put_memory("tenant-a", "project-a", "m1", "semantic", "known fact")
    assert len(memory.search_memory("tenant-a", "project-a")) == 1
    assert memory.search_memory("tenant-b", "project-a") == []
    assert memory.search_memory("tenant-a", "project-b") == []


def test_task_record_survives_reopen(tmp_path):
    path = str(tmp_path / "memory.sqlite3")
    memory = SQLiteMemory(path)
    memory.save_task("tenant-a", "project-a", "task-1", "running", {"step": 2})
    restored = SQLiteMemory(path).get_task("tenant-a", "project-a", "task-1")
    assert restored["payload"]["step"] == 2


def test_audit_chain_verifies_and_rejects_mutation(tmp_path):
    audit = AuditLog(str(tmp_path / "audit.sqlite3"))
    audit.append("event-1", "task.created", "tenant-a", "user-1", {"task": "one"})
    audit.append("event-2", "task.finished", "tenant-a", "agent-1", {"ok": True})
    assert audit.verify_chain()
    with pytest.raises(Exception):
        with audit._connect() as db:
            db.execute("UPDATE audit_events SET payload='tampered' WHERE event_id='event-1'")
    assert audit.verify_chain()

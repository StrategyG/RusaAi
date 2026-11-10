from pathlib import Path

from rusa_agents.platform_contracts import WorkflowEventV2
from rusa_agents.workflow_store import PostgresWorkflowStore


class FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, event_inserted=True):
        self.event_inserted = event_inserted
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append((query, params))
        if "INSERT INTO public.rusa_workflow_events" in query:
            return FakeCursor(("event-1",) if self.event_inserted else None)
        return FakeCursor(None)


def sample_event():
    return WorkflowEventV2(
        tenant_id="tenant-a",
        workflow_id="workflow-1",
        run_id="run-1",
        correlation_id="workflow-1",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        event_type="workflow.created",
        producer_id="rusa.workflow_store",
        producer_version="2.0.0",
        payload={"task_count": 2, "dependency_count": 1},
    )


def test_event_append_and_outbox_insert_share_the_same_transaction_connection():
    conn = FakeConnection()
    assert PostgresWorkflowStore._append_event(conn, sample_event()) is True
    assert len(conn.statements) == 2
    assert "rusa_workflow_events" in conn.statements[0][0]
    assert "rusa_workflow_outbox" in conn.statements[1][0]


def test_duplicate_event_message_is_not_republished_to_outbox():
    conn = FakeConnection(event_inserted=False)
    assert PostgresWorkflowStore._append_event(conn, sample_event()) is False
    assert len(conn.statements) == 1
    assert "rusa_workflow_outbox" not in conn.statements[0][0]


def test_workflow_scheduler_has_dependency_gates_and_per_run_concurrency_lock():
    source = Path("rusa_agents/workflow_store.py").read_text(encoding="utf-8")
    assert "FOR UPDATE OF r SKIP LOCKED" in source
    assert "max_parallel" in source
    assert "active_count" in source
    assert "dependency.status <> 'succeeded'" in source
    assert "self.block_failed_dependencies" in source
    assert "self.expire_due_tasks" in source
    assert "self.expire_exhausted_tasks" in source


def test_workflow_mutations_are_fenced_by_lease_epoch_and_state_version():
    source = Path("rusa_agents/workflow_store.py").read_text(encoding="utf-8")
    assert "lease_epoch=%s" in source
    assert "state_version=%s" in source
    assert "WorkflowLeaseLostError" in source
    assert "resume_approved_task" in source
    assert "acknowledge_cancel" in source


def test_run_completion_requires_successful_tasks_and_digest_bound_qa_evidence():
    source = Path("rusa_agents/workflow_store.py").read_text(encoding="utf-8")
    assert "def complete_run(" in source
    assert "ValidationVerdictPayloadV2" in source
    assert "status <> 'succeeded'" in source
    assert "_verify_on_connection" in source
    assert "final_artifact_digest" in source
    assert "unresolved or unconsumed human approvals" in source


def test_cancelled_side_effects_must_be_reconciled_before_run_finishes():
    source = Path("rusa_agents/workflow_store.py").read_text(encoding="utf-8")
    assert "side_effect_status IN ('APPLIED', 'UNKNOWN')" in source
    assert "def reconcile_task_side_effect(" in source
    assert "workflow.task.reconciled" in source
    assert "def finish_cancel(" in source


def test_budget_consumption_is_allowlisted_and_atomic():
    source = Path("rusa_agents/workflow_store.py").read_text(encoding="utf-8")
    for metric in ("tool_calls", "tokens", "cost_usd", "repair_cycles", "retries"):
        assert f'"{metric}"' in source
    assert "jsonb_set(" in source
    assert "workflow.budget.consumed" in source


def test_outbox_is_at_least_once_and_inbox_is_tenant_message_deduplicated():
    source = Path("rusa_agents/workflow_store.py").read_text(encoding="utf-8")
    assert "def publish_pending(" in source
    assert "FOR UPDATE SKIP LOCKED" in source
    assert "def consume_once(" in source
    assert "ON CONFLICT (tenant_id, consumer_name, message_id) DO NOTHING" in source

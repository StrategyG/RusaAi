"""Versioned internal A2A envelope adapter for DevOps task delegation."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

DEVOPS_CAPABILITIES = {
    "devops.forecast",
    "devops.anomaly_detection",
    "devops.capacity_recommendation",
    "devops.telemetry_ingestion",
}


def build_task_envelope(
    *, task_id: str, tenant_id: str, sender_agent: str, recipient_agent: str,
    capability: str, objective: str, payload: dict, approval_required: bool = True,
) -> dict:
    if capability not in DEVOPS_CAPABILITIES:
        raise ValueError("unsupported DevOps A2A capability")
    if not objective.strip() or len(objective) > 8000:
        raise ValueError("objective must contain 1-8000 characters")
    if not tenant_id or len(tenant_id) > 128:
        raise ValueError("tenant_id is invalid")
    return {
        "protocol_version": "1.0", "task_id": task_id, "message_id": str(uuid4()),
        "type": "task.create", "created_at": datetime.now(timezone.utc).isoformat(),
        "from": {"agent_id": sender_agent, "role": "rusa_orchestrator"},
        "to": {"agent_id": recipient_agent, "role": "rusa_devops"},
        "tenant_id": tenant_id, "capability": capability,
        "task": {"objective": objective, "payload": payload},
        "policy": {
            "allowed_actions": ["read_telemetry", "forecast", "detect_anomaly", "propose_capacity"],
            "prohibited_actions": ["direct_infrastructure_write", "secret_read", "tenant_override"],
            "human_approval_required": approval_required,
        },
    }


def agent_card() -> dict:
    return {
        "agent_id": "rusa-devops-intelligence",
        "name": "RUSA-AI Predictive DevOps Intelligence",
        "version": "0.1.0",
        "capabilities": sorted(DEVOPS_CAPABILITIES),
        "transport": "internal-task-envelope",
        "autonomy": "controlled",
        "infrastructure_mutations": False,
    }

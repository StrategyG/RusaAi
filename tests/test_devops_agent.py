from __future__ import annotations

import asyncio
import json

import pytest

from rusa_devops.agent import DevOpsAgentError, DevOpsReasoningAgent


class FakeProvider:
    def __init__(self, response: dict):
        self.response = response
    async def complete(self, messages):
        assert messages[0]["role"] == "system"
        return json.dumps(self.response)


class FakeGateway:
    def __init__(self):
        self.calls = []
    async def call(self, name, arguments, *, tenant_id):
        self.calls.append((name, arguments, tenant_id))
        if name == "propose_capacity_recommendation":
            return {"task_id": "job-1", "execution_enabled": False}
        return {"rows": []}


def test_devops_agent_uses_authenticated_tenant_and_allowlisted_tools():
    response = {
        "message": "Saya akan membaca metrik CPU.",
        "actions": [{"name": "devops_get_telemetry", "arguments": {
            "metric_name": "cpu_utilization", "resource_id": "host-a", "limit": 60
        }}],
        "done": True, "blocked": False, "blockers": [],
    }
    gateway = FakeGateway()
    agent = DevOpsReasoningAgent(FakeProvider(response), gateway)
    result = asyncio.run(agent.run("tenant-verified", "Periksa CPU host-a."))
    assert result["infrastructure_mutations_executed"] is False
    assert gateway.calls[0][2] == "tenant-verified"


def test_devops_agent_rejects_unlisted_mutation_action():
    response = {
        "message": "Saya akan mengubah cluster.",
        "actions": [{"name": "kubectl_scale", "arguments": {"replicas": 10}}],
        "done": True,
    }
    agent = DevOpsReasoningAgent(FakeProvider(response), FakeGateway())
    with pytest.raises(DevOpsAgentError):
        asyncio.run(agent.run("tenant-verified", "Scale cluster sekarang."))

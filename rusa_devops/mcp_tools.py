"""Read-only DevOps tools registered on the existing tenant-authenticated Knowledge MCP."""
from __future__ import annotations

from functools import lru_cache
from typing import Callable

from .store import PostgresDevOpsStore


@lru_cache(maxsize=1)
def _store() -> PostgresDevOpsStore:
    return PostgresDevOpsStore()


def register_devops_mcp_tools(mcp, require_tenant: Callable[[], str]) -> None:
    @mcp.tool()
    def devops_get_telemetry(metric_name: str, resource_id: str, limit: int = 120) -> dict:
        """Read tenant-scoped telemetry samples. This tool never writes infrastructure."""
        tenant_id = require_tenant()
        return {
            "metric_name": metric_name, "resource_id": resource_id,
            "rows": _store().read_telemetry(tenant_id, metric_name, resource_id, limit=limit),
        }

    @mcp.tool()
    def devops_get_model_run(run_id: str) -> dict:
        """Read one tenant-scoped forecasting model run and held-out metrics."""
        tenant_id = require_tenant()
        result = _store().get_model_run(tenant_id, run_id)
        if result is not None:
            result.pop("artifact_uri", None)
        return {"found": result is not None, "model_run": result}

    @mcp.tool()
    def devops_list_incidents(limit: int = 50) -> dict:
        """List recent tenant-scoped anomaly incidents."""
        tenant_id = require_tenant()
        return {"items": _store().list_incidents(tenant_id, limit=limit)}

    @mcp.tool()
    def devops_list_model_runs(limit: int = 20) -> dict:
        """List tenant-scoped forecasting model runs and evaluation metrics."""
        tenant_id = require_tenant()
        return {"items": _store().list_model_runs(tenant_id, limit=limit)}

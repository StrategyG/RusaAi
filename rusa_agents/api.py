"""Authenticated tenant-scoped agent task API."""
from __future__ import annotations
import hmac, json, logging, os
from contextlib import asynccontextmanager
from typing import Any
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from .contracts import TaskCreate
from .providers import OpenAICompatibleProvider
from .storage import Database
from .worker import AgentRuntime
from .tools import GitHubTools, ToolRegistry

logger = logging.getLogger("rusa-agent-api")
key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

class ApprovalRequest(BaseModel):
    action: str = Field(min_length=1, max_length=128)

def tenant_keys() -> dict[str, str]:
    raw = os.getenv("RUSA_TENANT_API_KEYS", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("RUSA_TENANT_API_KEYS must be valid JSON") from exc
    if not isinstance(data, dict) or not data or any(not isinstance(k, str) or not isinstance(v, str) or len(v) < 32 for k, v in data.items()):
        raise RuntimeError("Tenant API keys must map tenant IDs to secrets of at least 32 characters")
    return data

def resolve_tenant(api_key: str | None) -> str:
    keys = tenant_keys()
    if not keys:
        raise HTTPException(status_code=503, detail="Agent API tenant authentication is not configured")
    for tenant, expected in keys.items():
        if api_key and hmac.compare_digest(api_key.encode(), expected.encode()):
            return tenant
    raise HTTPException(status_code=401, detail="Invalid API key")

@asynccontextmanager
async def lifespan(app: FastAPI):
    database = Database()
    await database.initialize()
    app.state.database = database
    app.state.runtime = None
    try:
        if tenant_keys() and os.getenv("RUSA_AGENT_MODEL_BASE_URL") and os.getenv("RUSA_AGENT_MODEL_NAME") and os.getenv("GITHUB_TOKEN"):
            provider = OpenAICompatibleProvider()
            tools = ToolRegistry()
            GitHubTools().register(tools)
            app.state.runtime = AgentRuntime(database, provider, tools)
    except Exception:
        logger.exception("Agent runtime configuration failed")
    yield
    await database.close()

app = FastAPI(title="RUSA-AI Agent Control Plane", version="1.0.0", lifespan=lifespan)

async def tenant_dependency(api_key: str | None = Depends(key_header)) -> str:
    return resolve_tenant(api_key)

def runtime_dependency(request: Request) -> AgentRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Agent runtime is not configured")
    return runtime

@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "service": "rusa-agent-api"}

@app.get("/health/ready")
async def ready(request: Request) -> dict[str, Any]:
    try:
        await request.app.state.database.ping()
        db_ok = True
    except Exception:
        db_ok = False
    runtime_ok = getattr(request.app.state, "runtime", None) is not None
    auth_ok = bool(tenant_keys())
    if not (db_ok and runtime_ok and auth_ok):
        raise HTTPException(status_code=503, detail={"database": db_ok, "runtime": runtime_ok, "tenant_auth": auth_ok})
    return {"status": "ready"}

@app.post("/v1/agent/tasks", status_code=202)
async def create_task(payload: TaskCreate, tenant: str = Depends(tenant_dependency), runtime: AgentRuntime = Depends(runtime_dependency), idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128)) -> dict[str, str]:
    task_id = await runtime.submit(tenant, idempotency_key, payload.model_dump())
    return {"task_id": task_id, "status": "queued"}

@app.get("/v1/agent/tasks/{task_id}")
async def get_task(task_id: str, tenant: str = Depends(tenant_dependency), runtime: AgentRuntime = Depends(runtime_dependency)) -> dict[str, Any]:
    task = await runtime.get_task(tenant, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.pop("tenant_id", None)
    return task

@app.post("/v1/agent/tasks/{task_id}/approve")
async def approve_task(task_id: str, payload: ApprovalRequest, tenant: str = Depends(tenant_dependency), runtime: AgentRuntime = Depends(runtime_dependency)) -> dict[str, str]:
    try:
        await runtime.approve(tenant, task_id, payload.action)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "queued"}

@app.post("/v1/agent/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, tenant: str = Depends(tenant_dependency), runtime: AgentRuntime = Depends(runtime_dependency)) -> dict[str, str]:
    try:
        await runtime.cancel(tenant, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    return {"task_id": task_id, "status": "cancellation_requested"}

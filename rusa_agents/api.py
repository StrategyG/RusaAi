"""Authenticated tenant-scoped agent control plane API."""
from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

from .bootstrap import build_planner, build_tooling
from .contracts import TaskCreate
from .memory import MemoryStore
from .rag import RAGService
from .storage import Database
from .worker import AgentRuntime, IdempotencyConflict

logger = logging.getLogger("rusa-agent-api")
key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
approver_header = APIKeyHeader(name="X-RUSA-Approver-Key", auto_error=False)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1, max_length=128)


class MemoryWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=100000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RAGIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=2000000)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _key_map(environment_name: str) -> dict[str, str]:
    raw = os.getenv(environment_name, "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{environment_name} must be valid JSON") from exc
    if not isinstance(data, dict) or not data:
        raise RuntimeError(f"{environment_name} must be a non-empty JSON object")
    for tenant, secret in data.items():
        if (
            not isinstance(tenant, str)
            or not tenant
            or len(tenant) > 128
            or not isinstance(secret, str)
            or len(secret) < 32
        ):
            raise RuntimeError(
                f"{environment_name} must map tenant IDs to secrets of at least 32 characters"
            )
    return data


def tenant_keys() -> dict[str, str]:
    return _key_map("RUSA_TENANT_API_KEYS")


def approver_keys() -> dict[str, str]:
    return _key_map("RUSA_TENANT_APPROVER_KEYS")


def _resolve_key(api_key: str | None, key_map: dict[str, str], label: str) -> str:
    if not key_map:
        raise HTTPException(status_code=503, detail=f"{label} authentication is not configured")
    for tenant, expected in key_map.items():
        if api_key and hmac.compare_digest(api_key.encode(), expected.encode()):
            return tenant
    raise HTTPException(status_code=401, detail=f"Invalid {label} credential")


def resolve_tenant(api_key: str | None) -> str:
    return _resolve_key(api_key, tenant_keys(), "tenant")


def resolve_approver_tenant(api_key: str | None) -> str:
    return _resolve_key(api_key, approver_keys(), "approver")


@asynccontextmanager
async def lifespan(app: FastAPI):
    database = Database()
    await database.initialize()
    app.state.database = database
    app.state.memory = MemoryStore(database)
    app.state.runtime = None
    app.state.rag = None
    mcp_gateway = None
    if (
        tenant_keys()
        and os.getenv("RUSA_AGENT_MODEL_BASE_URL")
        and os.getenv("RUSA_AGENT_MODEL_NAME")
        and os.getenv("GITHUB_TOKEN")
    ):
        tools, mcp_gateway, rag_service = await build_tooling(database)
        app.state.mcp_gateway = mcp_gateway
        app.state.rag = rag_service
        app.state.runtime = AgentRuntime(database, build_planner(), tools)
    yield
    if mcp_gateway is not None:
        await mcp_gateway.close()
    await database.close()


environment = os.getenv("ENVIRONMENT", "production").lower()
app = FastAPI(
    title="RUSA-AI Agent Control Plane",
    version="1.1.0",
    docs_url="/docs" if environment != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if environment != "production" else None,
    lifespan=lifespan,
)


async def tenant_dependency(api_key: str | None = Depends(key_header)) -> str:
    return resolve_tenant(api_key)


async def approver_dependency(api_key: str | None = Depends(approver_header)) -> str:
    return resolve_approver_tenant(api_key)


def runtime_dependency(request: Request) -> AgentRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="Agent runtime is not configured")
    return runtime


def memory_dependency(request: Request) -> MemoryStore:
    memory = getattr(request.app.state, "memory", None)
    if memory is None:
        raise HTTPException(status_code=503, detail="Memory service is not configured")
    return memory


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
    try:
        tenant_auth_ok = bool(tenant_keys())
        approver_auth_ok = bool(approver_keys())
    except RuntimeError:
        tenant_auth_ok = False
        approver_auth_ok = False
    checks = {
        "database": db_ok,
        "runtime": runtime_ok,
        "tenant_auth": tenant_auth_ok,
        "approver_auth": approver_auth_ok,
    }
    if not all(checks.values()):
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ready", "checks": checks}


@app.get("/v1/agent/audit/verify")
async def verify_audit_chain(
    tenant: str = Depends(tenant_dependency),
    runtime: AgentRuntime = Depends(runtime_dependency),
) -> dict[str, Any]:
    valid = await runtime.audit.verify_chain(tenant)
    if not valid:
        raise HTTPException(status_code=503, detail="Tenant audit chain integrity verification failed")
    return {"status": "valid", "tenant_audit_chain_valid": True}


@app.get("/v1/agent/tools")
async def list_agent_tools(
    tenant: str = Depends(tenant_dependency),
    runtime: AgentRuntime = Depends(runtime_dependency),
) -> dict[str, Any]:
    return {
        "tools": runtime.tools.describe(),
        "internal_tools": sorted(runtime.orchestrator.INTERNAL_TOOLS),
    }


@app.post("/v1/agent/tasks", status_code=202)
async def create_task(
    payload: TaskCreate,
    tenant: str = Depends(tenant_dependency),
    runtime: AgentRuntime = Depends(runtime_dependency),
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
    ),
) -> dict[str, str]:
    try:
        task_id = await runtime.submit(tenant, idempotency_key, payload.model_dump())
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "queued"}


@app.get("/v1/agent/tasks/{task_id}")
async def get_task(
    task_id: str,
    tenant: str = Depends(tenant_dependency),
    runtime: AgentRuntime = Depends(runtime_dependency),
) -> dict[str, Any]:
    task = await runtime.get_task(tenant, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.pop("tenant_id", None)
    return task


@app.get("/v1/agent/tasks/{task_id}/messages")
async def get_task_messages(
    task_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    tenant: str = Depends(tenant_dependency),
    runtime: AgentRuntime = Depends(runtime_dependency),
) -> dict[str, Any]:
    if not await runtime.get_task(tenant, task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "task_id": task_id,
        "messages": await runtime.messages.history(tenant, task_id, limit),
    }


@app.post("/v1/agent/tasks/{task_id}/approve")
async def approve_task(
    task_id: str,
    payload: ApprovalRequest,
    tenant: str = Depends(approver_dependency),
    runtime: AgentRuntime = Depends(runtime_dependency),
) -> dict[str, str]:
    try:
        await runtime.approve(tenant, task_id, payload.action)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "queued"}


@app.post("/v1/agent/tasks/{task_id}/resume")
async def resume_task(
    task_id: str,
    tenant: str = Depends(approver_dependency),
    runtime: AgentRuntime = Depends(runtime_dependency),
) -> dict[str, str]:
    try:
        await runtime.resume(tenant, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task_id, "status": "queued"}


@app.post("/v1/agent/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    tenant: str = Depends(tenant_dependency),
    runtime: AgentRuntime = Depends(runtime_dependency),
) -> dict[str, str]:
    try:
        await runtime.cancel(tenant, task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task not found") from exc
    return {"task_id": task_id, "status": "cancellation_requested"}


@app.put("/v1/agent/memory/{namespace}/{memory_key}")
async def put_memory(
    namespace: str,
    memory_key: str,
    payload: MemoryWriteRequest,
    tenant: str = Depends(tenant_dependency),
    memory: MemoryStore = Depends(memory_dependency),
) -> dict[str, Any]:
    try:
        return await memory.put(
            tenant,
            namespace,
            memory_key,
            payload.content,
            payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v1/agent/memory/search")
async def search_memory(
    q: str = Query(min_length=1, max_length=1000),
    namespace: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=10, ge=1, le=50),
    tenant: str = Depends(tenant_dependency),
    memory: MemoryStore = Depends(memory_dependency),
) -> dict[str, Any]:
    try:
        results = await memory.search(tenant, q, namespace, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"results": results}


@app.get("/v1/agent/memory/{namespace}/{memory_key}")
async def get_memory(
    namespace: str,
    memory_key: str,
    tenant: str = Depends(tenant_dependency),
    memory: MemoryStore = Depends(memory_dependency),
) -> dict[str, Any]:
    result = await memory.get(tenant, namespace, memory_key)
    if result is None:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return result


@app.delete("/v1/agent/memory/{namespace}/{memory_key}")
async def delete_memory(
    namespace: str,
    memory_key: str,
    tenant: str = Depends(tenant_dependency),
    memory: MemoryStore = Depends(memory_dependency),
) -> dict[str, bool]:
    deleted = await memory.delete(tenant, namespace, memory_key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"deleted": True}


def rag_dependency(request: Request) -> RAGService:
    rag = getattr(request.app.state, "rag", None)
    if rag is None:
        raise HTTPException(status_code=503, detail="RAG service is not configured")
    return rag


@app.post("/v1/agent/rag/documents", status_code=202)
async def ingest_rag_document(
    payload: RAGIngestRequest,
    tenant: str = Depends(tenant_dependency),
    rag: RAGService = Depends(rag_dependency),
) -> dict[str, Any]:
    try:
        chunks = await rag.ingest_text(
            tenant_id=tenant,
            source_id=payload.source_id,
            title=payload.title,
            content=payload.content,
            metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"source_id": payload.source_id, "chunks_indexed": chunks}


@app.get("/v1/agent/rag/search")
async def search_rag(
    q: str = Query(min_length=1, max_length=4000),
    top_k: int = Query(default=5, ge=1, le=20),
    source_ids: list[str] | None = Query(default=None),
    tenant: str = Depends(tenant_dependency),
    rag: RAGService = Depends(rag_dependency),
) -> dict[str, Any]:
    try:
        chunks = await rag.retrieve(
            tenant_id=tenant,
            query=q,
            top_k=top_k,
            allowed_source_ids=source_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "results": [
            {
                "chunk_id": chunk.chunk_id,
                "source_id": chunk.source_id,
                "text": chunk.text,
                "score": chunk.score,
                "metadata": chunk.metadata,
                "citation": f"[{chunk.source_id}#{chunk.chunk_id[:12]}]",
            }
            for chunk in chunks
        ]
    }


@app.delete("/v1/agent/rag/documents/{source_id}")
async def delete_rag_document(
    source_id: str,
    tenant: str = Depends(tenant_dependency),
    rag: RAGService = Depends(rag_dependency),
) -> dict[str, Any]:
    deleted = await rag.delete_source(tenant, source_id)
    return {"source_id": source_id, "chunks_deleted": deleted}

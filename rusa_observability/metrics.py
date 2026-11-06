from __future__ import annotations

from time import time
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, start_http_server

HTTP_REQUESTS = Counter(
    "rusa_http_requests",
    "HTTP requests handled by RUSA-AI services.",
    ["service", "method", "route", "status_code"],
)
HTTP_DURATION = Histogram(
    "rusa_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["service", "method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
INFERENCE_REQUESTS = Counter(
    "rusa_inference_requests",
    "Local model inference requests by outcome.",
    ["model", "outcome"],
)
INFERENCE_DURATION = Histogram(
    "rusa_inference_duration_seconds",
    "Local model generation latency in seconds.",
    ["model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 45, 90, 180),
)
INFERENCE_TOKENS_PER_SECOND = Histogram(
    "rusa_inference_tokens_per_second",
    "Generated completion tokens per second for completed model requests.",
    ["model"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 40, 80, 120, 200),
)
TOKEN_USAGE = Counter(
    "rusa_token_usage",
    "Exact token usage reported by local inference or an OpenAI-compatible provider.",
    ["model", "token_type"],
)
AGENT_EXECUTIONS = Counter(
    "rusa_agent_task_executions",
    "Agent task executions by final state.",
    ["outcome"],
)
AGENT_DURATION = Histogram(
    "rusa_agent_task_duration_seconds",
    "End-to-end agent task execution duration in seconds.",
    ["outcome"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1200, 2400),
)
AGENT_WORKER_EXECUTIONS = Counter(
    "rusa_agent_worker_executions",
    "Specialist worker executions by role and outcome.",
    ["role", "outcome"],
)
AGENT_WORKER_DURATION = Histogram(
    "rusa_agent_worker_execution_seconds",
    "Specialist worker execution duration in seconds.",
    ["role", "outcome"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)
AGENT_TOOL_CALLS = Counter(
    "rusa_agent_tool_calls",
    "Agent tool calls by tool and outcome.",
    ["tool", "outcome"],
)
AGENT_TOOL_DURATION = Histogram(
    "rusa_agent_tool_duration_seconds",
    "Agent tool execution duration in seconds.",
    ["tool", "outcome"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60),
)
AGENT_PROVIDER_REQUESTS = Counter(
    "rusa_agent_provider_requests",
    "Agent model provider requests by model and outcome.",
    ["model", "outcome"],
)
AGENT_PROVIDER_DURATION = Histogram(
    "rusa_agent_provider_duration_seconds",
    "Agent model provider completion latency in seconds.",
    ["model"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 45, 90, 180),
)
RAG_REQUESTS = Counter(
    "rusa_rag_retrieval_requests",
    "Tenant-scoped RAG retrieval requests by outcome.",
    ["outcome"],
)
RAG_DURATION = Histogram(
    "rusa_rag_retrieval_duration_seconds",
    "End-to-end hybrid retrieval latency, including query embedding.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
RAG_RESULTS = Histogram(
    "rusa_rag_retrieval_results",
    "Number of documents returned by hybrid retrieval.",
    buckets=(0, 1, 2, 3, 5, 8, 10, 15, 20),
)
RAG_EVAL_RECALL = Gauge(
    "rusa_rag_eval_recall_at_k",
    "Mean Recall@k from the latest curated retrieval evaluation.",
    ["k"],
)
RAG_EVAL_MRR = Gauge(
    "rusa_rag_eval_mrr_at_k",
    "Mean reciprocal rank from the latest curated retrieval evaluation.",
    ["k"],
)
RAG_EVAL_NDCG = Gauge(
    "rusa_rag_eval_ndcg_at_k",
    "Mean NDCG@k from the latest curated retrieval evaluation.",
    ["k"],
)
RAG_EVAL_LAST_RUN = Gauge(
    "rusa_rag_eval_last_run_timestamp_seconds",
    "Unix timestamp of the latest curated retrieval evaluation.",
)
RAG_EVAL_QUERIES = Gauge(
    "rusa_rag_eval_queries",
    "Number of cases in the latest curated retrieval evaluation.",
)
AGENT_QUEUE_DEPTH = Gauge(
    "rusa_agent_queue_depth",
    "Tenant-aggregated agent task queue depth by state; no tenant labels.",
    ["state"],
)
AGENT_QUEUE_OLDEST_AGE = Gauge(
    "rusa_agent_queue_oldest_age_seconds",
    "Age of the oldest queued agent task across configured tenants.",
)
DB_POOL_SIZE = Gauge(
    "rusa_database_pool_size",
    "Current psycopg connection pool size.",
    ["service"],
)
DB_POOL_AVAILABLE = Gauge(
    "rusa_database_pool_available",
    "Available psycopg connections.",
    ["service"],
)
DB_POOL_WAITING = Gauge(
    "rusa_database_pool_waiting",
    "Requests waiting for a psycopg connection.",
    ["service"],
)
DB_SIZE_LIMIT = Gauge(
    "rusa_database_size_limit_bytes",
    "Configured database capacity alert threshold; this does not enforce a PostgreSQL hard limit.",
    ["database"],
)
RAG_EVAL_LAST_RUN.set(0)


def record_http_request(
    service: str, method: str, route: str, status_code: int, duration: float
) -> None:
    HTTP_REQUESTS.labels(service, method, route, str(status_code)).inc()
    HTTP_DURATION.labels(service, method, route).observe(max(0.0, duration))


def record_inference(
    model: str,
    outcome: str,
    duration: float,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    model_label = model[:64]
    INFERENCE_REQUESTS.labels(model_label, outcome).inc()
    safe_duration = max(0.0, duration)
    INFERENCE_DURATION.labels(model_label).observe(safe_duration)
    if prompt_tokens > 0:
        TOKEN_USAGE.labels(model_label, "prompt").inc(prompt_tokens)
    if completion_tokens > 0:
        TOKEN_USAGE.labels(model_label, "completion").inc(completion_tokens)
        if safe_duration > 0:
            INFERENCE_TOKENS_PER_SECOND.labels(model_label).observe(
                completion_tokens / safe_duration
            )


def record_database_capacity_limit(limit_bytes: int, database: str = "rusa_agent") -> None:
    if type(limit_bytes) is not int or not 1 <= limit_bytes <= 10**15:
        raise ValueError("database capacity threshold must be a positive byte count")
    DB_SIZE_LIMIT.labels(database[:63]).set(limit_bytes)


def record_agent_execution(outcome: str, duration: float) -> None:
    allowed = {
        "succeeded", "failed", "blocked", "cancelled", "awaiting_approval",
        "waiting_external", "queued", "running", "exception",
    }
    label = outcome if outcome in allowed else "other"
    AGENT_EXECUTIONS.labels(label).inc()
    AGENT_DURATION.labels(label).observe(max(0.0, duration))


def record_agent_worker(role: str, outcome: str, duration: float) -> None:
    allowed_roles = {"analyst", "architect", "developer", "debugger", "tester", "security"}
    allowed_outcomes = {"completed", "failed", "blocked", "approval_required", "timeout"}
    role_label = role if role in allowed_roles else "other"
    outcome_label = outcome if outcome in allowed_outcomes else "other"
    AGENT_WORKER_EXECUTIONS.labels(role_label, outcome_label).inc()
    AGENT_WORKER_DURATION.labels(role_label, outcome_label).observe(max(0.0, duration))


def record_agent_tool_call(tool: str, outcome: str, duration: float) -> None:
    allowed_tools = {
        "create_branch", "read_file", "list_files", "write_file",
        "create_pull_request", "get_ci_status", "get_branch_diff",
        "search_knowledge", "get_document", "list_collections",
    }
    allowed_outcomes = {"success", "failure", "error"}
    tool_label = tool if tool in allowed_tools else "other"
    outcome_label = outcome if outcome in allowed_outcomes else "error"
    AGENT_TOOL_CALLS.labels(tool_label, outcome_label).inc()
    AGENT_TOOL_DURATION.labels(tool_label, outcome_label).observe(max(0.0, duration))


def record_agent_provider(
    model: str,
    outcome: str,
    duration: float,
    *,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    model_label = model[:64]
    AGENT_PROVIDER_REQUESTS.labels(model_label, outcome).inc()
    safe_duration = max(0.0, duration)
    AGENT_PROVIDER_DURATION.labels(model_label).observe(safe_duration)
    if prompt_tokens > 0:
        TOKEN_USAGE.labels(model_label, "prompt").inc(prompt_tokens)
    if completion_tokens > 0:
        TOKEN_USAGE.labels(model_label, "completion").inc(completion_tokens)
        if safe_duration > 0:
            INFERENCE_TOKENS_PER_SECOND.labels(model_label).observe(
                completion_tokens / safe_duration
            )


def record_rag_retrieval(outcome: str, duration: float, result_count: int) -> None:
    RAG_REQUESTS.labels(outcome if outcome in {"success", "error"} else "other").inc()
    RAG_DURATION.observe(max(0.0, duration))
    RAG_RESULTS.observe(max(0, result_count))


def record_retrieval_evaluation(report: dict[str, Any]) -> None:
    k = str(int(report["k"]))
    RAG_EVAL_RECALL.labels(k).set(float(report["recall_at_k"]))
    RAG_EVAL_MRR.labels(k).set(float(report["mrr_at_k"]))
    RAG_EVAL_NDCG.labels(k).set(float(report["ndcg_at_k"]))
    RAG_EVAL_QUERIES.set(int(report["queries"]))
    RAG_EVAL_LAST_RUN.set(time())


def record_agent_queue_snapshot(counts: dict[str, int], oldest_queued_age: int) -> None:
    states = {
        "queued", "running", "succeeded", "failed", "blocked",
        "cancelled", "waiting_external",
    }
    for state in states:
        AGENT_QUEUE_DEPTH.labels(state).set(max(0, int(counts.get(state, 0))))
    AGENT_QUEUE_OLDEST_AGE.set(max(0, int(oldest_queued_age)))


def update_db_pool_metrics(pool: Any, service: str) -> None:
    try:
        stats = pool.get_stats()
        DB_POOL_SIZE.labels(service).set(float(stats.get("pool_size", 0)))
        DB_POOL_AVAILABLE.labels(service).set(float(stats.get("pool_available", 0)))
        DB_POOL_WAITING.labels(service).set(float(stats.get("requests_waiting", 0)))
    except Exception:
        # Metrics must never turn a healthy database operation into a failure.
        return


def start_metrics_server(port: int = 9100, addr: str = "0.0.0.0") -> Any:
    if not 1 <= port <= 65535:
        raise ValueError("metrics server port must be between 1 and 65535")
    return start_http_server(port, addr=addr)

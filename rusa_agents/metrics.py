"""Low-cardinality Prometheus metrics for API and autonomous workers."""
from prometheus_client import Counter, Histogram

API_REQUESTS = Counter(
    "rusa_api_requests_total",
    "HTTP requests served by the RUSA-AI API",
    ("method", "route", "status"),
)
API_REQUEST_DURATION = Histogram(
    "rusa_api_request_duration_seconds",
    "HTTP request duration in seconds",
    ("method", "route"),
)
AICODE_TASKS = Counter(
    "rusa_aicode_tasks_total",
    "AICode tasks by terminal outcome",
    ("outcome",),
)
AICODE_TASK_DURATION = Histogram(
    "rusa_aicode_task_duration_seconds",
    "AICode task execution duration in seconds",
    ("outcome",),
)
WORKER_STEPS = Counter(
    "rusa_aicode_worker_steps_total",
    "Specialized worker steps",
    ("role", "outcome"),
)
TOOL_EXECUTIONS = Counter(
    "rusa_aicode_tool_executions_total",
    "Registered tool executions",
    ("tool", "outcome"),
)


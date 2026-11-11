"""Low-cardinality Prometheus metrics for API, inference, and autonomous workers."""
from prometheus_client import Counter, Gauge, Histogram

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
INFERENCE_ACTIVE_GENERATIONS = Gauge(
    "rusa_inference_active_generations",
    "Active inference generations across admission controllers in this process",
)
INFERENCE_WAITING_REQUESTS = Gauge(
    "rusa_inference_waiting_requests",
    "Requests currently waiting for an inference admission slot in this process",
)
INFERENCE_QUEUE_WAIT_SECONDS = Histogram(
    "rusa_inference_queue_wait_seconds",
    "Time spent waiting for an inference admission slot",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
INFERENCE_QUEUE_REJECTIONS = Counter(
    "rusa_inference_queue_rejections_total",
    "Inference requests rejected before generation",
    ("reason",),
)
INFERENCE_GENERATION_DURATION_SECONDS = Histogram(
    "rusa_inference_generation_duration_seconds",
    "Inference generation duration in seconds",
    ("mode",),
)
INFERENCE_TIME_TO_FIRST_TOKEN_SECONDS = Histogram(
    "rusa_inference_time_to_first_token_seconds",
    "Time from stream admission to the first generated content token",
)
INFERENCE_PROVIDER_ERRORS = Counter(
    "rusa_inference_provider_errors_total",
    "Inference provider failures by request mode",
    ("mode",),
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

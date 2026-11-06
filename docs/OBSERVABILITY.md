# RUSA-AI production observability

## Scope

The optional observability stack provides:

- Prometheus metrics and alert rules.
- Grafana dashboards with Prometheus, Loki, and Tempo data sources.
- OpenTelemetry OTLP traces from FastAPI, HTTPX, psycopg, inference, agent execution, tools, providers, and RAG retrieval.
- Centralized Docker JSON logs in Loki through the OpenTelemetry Collector filelog receiver.
- PostgreSQL health, database size, connection and filesystem metrics.
- Host/container metrics and optional NVIDIA DCGM GPU metrics.
- Alertmanager routing to an internal alert sink and an optional external HTTPS webhook.
- Curated retrieval evaluation with Recall@k, MRR@k, and NDCG@k.

Metric labels intentionally exclude tenant IDs, prompt text, document contents, credentials, task IDs, and user identifiers. Queue depth is aggregated across configured tenants.

## Prerequisites and secrets

1. Use a Linux Docker host with Docker Compose v2. The collector reads /var/lib/docker/containers; adjust that read-only mount if the Docker data-root is different.
2. Copy .env.example to .env and replace every placeholder with unique production secrets.
3. Generate a metrics bearer token with at least 32 random characters. Set it as RUSA_METRICS_BEARER_TOKEN in .env and write the exact same value, followed by a newline, to secrets/rusa-metrics-token. Set RUSA_METRICS_TOKEN_FILE=./secrets/rusa-metrics-token.
4. Set RUSA_MONITOR_DB_PASSWORD to a unique secret. The migration service creates or rotates a dedicated rusa_monitor login with pg_monitor, no superuser privileges, and read-only transactions.
5. Set GRAFANA_ADMIN_PASSWORD to a unique secret. Do not expose Grafana, Prometheus, or Alertmanager directly to the public internet; the published ports bind to loopback.
6. If an external notification endpoint is needed, configure RUSA_ALERT_WEBHOOK_URL with an HTTPS endpoint and set RUSA_ALERT_WEBHOOK_BEARER_TOKEN if required by that endpoint. Plain HTTP is disabled unless explicitly enabled in the alert sink environment.

The example metrics token file is deliberately a placeholder for Compose/CI validation. Never use it in a production deployment.

## Start the stack

~~~bash
mkdir -p secrets evaluation
cp .env.example .env
cp secrets/rusa-metrics-token.example secrets/rusa-metrics-token
# Replace the token in both .env and secrets/rusa-metrics-token before starting.
docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability up -d --build
~~~

Local-only interfaces:

- Grafana: http://127.0.0.1:3001 (admin / configured password)
- Prometheus: http://127.0.0.1:9090
- Alertmanager: http://127.0.0.1:9093

Loki, Tempo, Pushgateway, exporters, and OTLP receivers are not published on host ports.

To enable agent and MCP monitoring, start their existing application profiles as well:

~~~bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability --profile agent --profile knowledge up -d
~~~

Prometheus target files are empty by default. Copy the example files into their active target names only when the matching application profile is enabled:

- agent-api.example.yml to agent-api.yml
- agent-worker.example.yml to agent-worker.yml
- mcp.example.yml to mcp.yml

The API and MCP targets use the metrics bearer token. The worker metrics port is internal to the Docker network and is not published on the host.

## GPU monitoring

GPU monitoring is optional and requires a supported NVIDIA driver and NVIDIA Container Toolkit on the host.

~~~bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability --profile gpu up -d dcgm-exporter
cp observability/prometheus/targets/gpu.example.yml observability/prometheus/targets/gpu.yml
~~~

If the host has no NVIDIA GPU, do not enable the GPU profile; the rest of the stack remains functional.

## Retrieval-quality evaluation

Latency and result-count metrics do not prove retrieval relevance. Supply a manually reviewed JSONL evaluation set at evaluation/dataset.jsonl; each line has this shape:

~~~json
{"query":"How do I rotate an API credential?","relevant_document_ids":["doc-id-1","doc-id-2"]}
~~~

Set RUSA_RAG_EVAL_TENANT_ID to the tenant that owns the evaluation documents. The dataset must contain queries and relevant document IDs from that same tenant. The evaluation job calls the tenant-scoped knowledge service and pushes aggregate metrics to Pushgateway without adding tenant IDs to Prometheus labels.

Start the daily evaluation loop after the dataset and tenant are configured:

~~~bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml --profile observability --profile rag-evaluation up -d rusa-rag-evaluation
~~~

The default evaluation interval is 24 hours. Set RUSA_RAG_MIN_RECALL_AT_K to the approved Recall@5 acceptance threshold. The threshold is published alongside the evaluation metrics, so the alert follows the configured value. A result below that threshold is still published to monitoring and the evaluation process logs a threshold failure before retrying at the next interval. Maintain the dataset as a controlled evaluation artifact; do not commit tenant documents, secrets, or production prompts.

## What to watch

| Signal | Primary metric / source | Default alert |
|---|---|---|
| Slow inference | rusa_inference_duration_seconds p95; rusa_inference_tokens_per_second | p95 above 15 seconds |
| Token usage | rusa_token_usage_total by model and token type | Dashboard trend; set budget alerts per deployment |
| Agent failures | rusa_agent_task_executions_total with outcome=failed | Failure ratio above 10% |
| Agent backlog | rusa_agent_queue_depth and rusa_agent_queue_oldest_age_seconds | More than 50 queued or oldest above 10 minutes |
| Database pressure | pg_up, pg_database_size_bytes, rusa_database_pool_waiting, node filesystem metrics | DB unavailable, pool wait, 80% capacity threshold, or disk below 10% |
| GPU saturation | DCGM_FI_DEV_GPU_UTIL, DCGM_FI_DEV_FB_USED, DCGM_FI_DEV_FB_FREE | GPU above 95% or memory above 90% |
| Retrieval quality | rusa_rag_eval_recall_at_k, rusa_rag_eval_mrr_at_k, rusa_rag_eval_ndcg_at_k | Recall@5 below configured threshold or evaluation stale for 48 hours |

The configured database-size metric is an alert threshold, not a PostgreSQL hard quota. Actual storage enforcement and capacity expansion remain infrastructure responsibilities.

## Incident triage

- **Inference slow:** open the RUSA-AI dashboard, inspect model p95 and token throughput, then follow the inference trace in Tempo. Check GPU utilization/memory and database/RAG spans before changing concurrency or batch size.
- **Agent failing or stuck:** inspect task outcome rates, specialist/tool/provider latency, queue depth, and oldest queued age. Search Loki for the worker service and correlate with the trace.
- **Database full or saturated:** check pg_database_size_bytes, pg_up, pool waiting, host free space, long-running transactions, and Docker volume usage. Do not delete tenant data as an emergency action without the approved retention procedure.
- **Retrieval inaccurate:** confirm the evaluation ran against the correct tenant and collection, inspect Recall@5/MRR/NDCG, then review embedding model, chunking, metadata filters, and hybrid-ranking configuration. A low result is a quality signal, not an automatic ranking fix.

## Retention and security

- Prometheus: 15 days or 10 GB, whichever limit is reached first.
- Loki and Tempo: 7 days; Docker JSON logs are also rotated to three 10 MB files per service.
- Pushgateway persists the latest evaluation report across restarts.
- Metrics endpoints require a bearer token. Prometheus receives it through a Docker secret file.
- Dashboard and query UIs bind to loopback. Put them behind an authenticated TLS reverse proxy for remote operator access.
- The OTel Collector reads Docker log files read-only and does not mount the Docker socket.
- The alert sink forwards only a bounded allowlist of alert fields. It does not log webhook credentials or forward arbitrary alert payload fields.
- Direct OTLP application-log export is disabled by default because Docker log collection already supplies centralized logs; enable it only if duplicate filelog ingestion for those application streams is intentionally disabled.

## Production verification

Do not sign off from a successful image build alone. Verify in the target environment:

1. All expected Prometheus targets show UP.
2. Grafana dashboards display non-empty inference, agent, database, and host metrics after representative traffic.
3. Tempo contains an inference trace with child HTTPX/psycopg spans.
4. Loki contains API, worker, database, and alert-sink logs with the expected service labels.
5. Trigger a controlled alert in staging and verify Alertmanager delivery to the sink and configured webhook.
6. Run the curated RAG evaluation and confirm the quality gauges update; deliberately lower the threshold in staging to verify the low-recall alert.
7. On an NVIDIA host, verify DCGM metrics and GPU alerts; otherwise document that GPU monitoring is not applicable.
8. Test retention, volume capacity alarms, and restoration of Prometheus/Grafana/Loki/Tempo persistent volumes.

The repository supplies the instrumentation and deployment configuration. Production sign-off still requires real host, GPU, database, notification, and incident-drill evidence.

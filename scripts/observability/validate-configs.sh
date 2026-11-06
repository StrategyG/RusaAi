#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

docker run --rm \
  -v "$ROOT_DIR/observability/otel-collector/config.yaml:/etc/otelcol-contrib/config.yaml:ro" \
  otel/opentelemetry-collector-contrib:0.120.0 \
  validate --config=/etc/otelcol-contrib/config.yaml

docker run --rm \
  -v "$ROOT_DIR/observability/prometheus:/etc/prometheus:ro" \
  prom/prometheus:v3.1.0 \
  promtool check config /etc/prometheus/prometheus.yml

docker run --rm \
  -v "$ROOT_DIR/observability/alertmanager/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro" \
  prom/alertmanager:v0.28.0 \
  amtool check-config /etc/alertmanager/alertmanager.yml

docker run --rm \
  -v "$ROOT_DIR/observability/loki/loki.yml:/etc/loki/loki.yml:ro" \
  grafana/loki:3.4.2 \
  -config.file=/etc/loki/loki.yml -verify-config

python -m json.tool "$ROOT_DIR/observability/grafana/dashboards/rusa-ai-overview.json" >/dev/null

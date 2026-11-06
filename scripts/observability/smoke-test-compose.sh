#!/usr/bin/env bash
set -euo pipefail

if [[ "${CI:-}" != "true" ]]; then
  echo "This destructive Compose smoke test is CI-only." >&2
  exit 2
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

created_env=0
if [[ ! -f .env ]]; then
  cp .env.example .env
  created_env=1
fi

compose=(docker compose -p "rusa-observability-smoke-${GITHUB_RUN_ID:-local}" -f docker-compose.yml -f docker-compose.observability.yml --profile observability)

cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  if [[ "$created_env" == "1" ]]; then
    rm -f .env
  fi
}
trap cleanup EXIT

docker image inspect rusa-agent-runtime:local >/dev/null

"${compose[@]}" up -d prometheus grafana rusa-alert-sink

wait_url() {
  local url="$1"
  local name="$2"
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 2 "$url" >/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "Timed out waiting for $name at $url" >&2
  "${compose[@]}" ps >&2
  "${compose[@]}" logs --tail=100 >&2
  return 1
}

wait_url "http://127.0.0.1:9090/-/healthy" "Prometheus"
wait_url "http://127.0.0.1:3001/api/health" "Grafana"
wait_url "http://127.0.0.1:9093/-/healthy" "Alertmanager"

grafana_user="$(grep -m1 '^GRAFANA_ADMIN_USER=' .env | cut -d= -f2-)"
grafana_password="$(grep -m1 '^GRAFANA_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
for _ in $(seq 1 30); do
  if curl -fsS -u "$grafana_user:$grafana_password" \
      "http://127.0.0.1:3001/api/search?query=RUSA-AI" | grep -q "rusa-ai-overview"; then
    break
  fi
  sleep 2
done
curl -fsS -u "$grafana_user:$grafana_password" \
  "http://127.0.0.1:3001/api/search?query=RUSA-AI" | grep -q "rusa-ai-overview"

"${compose[@]}" exec -T rusa-alert-sink python - <<'PY'
import json
import time
import urllib.request

checks = {
    "OpenTelemetry Collector": "http://otel-collector:13133/",
    "Loki": "http://loki:3100/ready",
    "Tempo": "http://tempo:3200/ready",
    "Alert sink": "http://rusa-alert-sink:8080/health/ready",
}
for name, url in checks.items():
    last_error = None
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    break
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    else:
        raise RuntimeError(f"{name} did not become ready: {last_error}")

last_metrics = ""
for _ in range(30):
    try:
        with urllib.request.urlopen("http://postgres-exporter:9187/metrics", timeout=3) as response:
            last_metrics = response.read().decode("utf-8")
        if "pg_up 1" in last_metrics:
            break
    except Exception:
        pass
    time.sleep(2)
else:
    raise RuntimeError("PostgreSQL exporter did not report pg_up 1")

alert_payload = {
    "alerts": [{
        "status": "firing",
        "fingerprint": "observability-smoke-test",
        "labels": {
            "alertname": "ObservabilitySmokeTest",
            "severity": "warning",
            "service": "ci",
            "tenant_id": "must-not-be-forwarded",
        },
        "annotations": {
            "summary": "Observability smoke test",
            "description": "Alert sink delivery test",
        },
    }]
}
request = urllib.request.Request(
    "http://rusa-alert-sink:8080/alerts",
    data=json.dumps(alert_payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=5) as response:
    result = json.loads(response.read())
    if response.status != 200 or result.get("accepted") != 1:
        raise RuntimeError("Alert sink smoke test was not accepted")

last_query = None
for _ in range(30):
    try:
        query_url = "http://prometheus:9090/api/v1/query?query=up%7Bjob%3D%22postgres%22%7D"
        with urllib.request.urlopen(query_url, timeout=3) as response:
            last_query = json.loads(response.read())
        series = last_query.get("data", {}).get("result", [])
        if series and series[0].get("value", [None, None])[1] == "1":
            break
    except Exception:
        pass
    time.sleep(2)
else:
    raise RuntimeError(f"Prometheus did not scrape PostgreSQL successfully: {last_query}")
print("Observability stack smoke test passed")
PY

"""Controlled DevOps worker: collect, train, forecast, detect, and propose (never apply) actions."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

from rusa_agents.metrics import DEVOPS_ANOMALIES, DEVOPS_FORECAST_RUNS, DEVOPS_JOBS, DEVOPS_JOB_DURATION

from .anomaly import BayesianAnomalyDetector
from .config import DevOpsConfig
from .decision import recommend_capacity
from .queue import PostgresDevOpsQueue
from .store import PostgresDevOpsStore
from .telemetry import PrometheusTelemetrySource, generate_synthetic_telemetry

logger = logging.getLogger("rusa-devops-worker")


class DevOpsWorkerRuntime:
    def __init__(self, config: DevOpsConfig | None = None):
        self.config = config or DevOpsConfig.from_env()
        self.worker_id = os.getenv("RUSA_DEVOPS_WORKER_ID") or f"devops-{uuid4().hex[:12]}"
        dsn = os.getenv("RUSA_WORKER_DATABASE_URL") or os.getenv("RUSA_API_DATABASE_URL") or os.getenv("RUSA_DATABASE_URL")
        self.queue = PostgresDevOpsQueue(dsn)
        self.store = PostgresDevOpsStore(dsn)

    def initialize(self) -> None:
        if not self.queue.verify_ready() or not self.store.check_ready():
            raise RuntimeError("DevOps schema is not ready; apply migrations/009_rusa_devops_intelligence.sql")

    def run_once(self) -> bool:
        job = self.queue.claim(self.worker_id, self.config.lease_seconds)
        if job is None:
            return False
        started = time.perf_counter()
        try:
            with DEVOPS_JOB_DURATION.labels(job_type=job.job_type).time():
                result = self._execute(job.tenant_id, job.job_type, job.payload)
            if not self.queue.complete(job, result):
                raise RuntimeError("DevOps job lease was lost before completion")
            DEVOPS_JOBS.labels(job_type=job.job_type, outcome="succeeded").inc()
            if job.job_type == "train_forecaster":
                DEVOPS_FORECAST_RUNS.labels(outcome="succeeded").inc()
            logger.info("job=%s type=%s status=succeeded duration_ms=%.1f",
                        job.task_id, job.job_type, (time.perf_counter() - started) * 1000)
        except Exception as exc:
            DEVOPS_JOBS.labels(job_type=job.job_type, outcome="failed").inc()
            if job.job_type == "train_forecaster":
                DEVOPS_FORECAST_RUNS.labels(outcome="failed").inc()
            logger.exception("DevOps job failed: %s", job.task_id)
            self.queue.fail(job, f"{type(exc).__name__}: {exc}")
        return True

    def _execute(self, tenant_id: str, job_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if job_type == "synthetic_ingest":
            samples = generate_synthetic_telemetry(
                tenant_id,
                metric_name=payload.get("metric_name", "cpu_utilization"),
                resource_id=payload.get("resource_id", "demo-local"),
                rows=int(payload.get("rows", 2000)),
                seed=int(payload.get("seed", 42)),
            )
            count = self.store.write_telemetry(tenant_id, samples)
            return {"ingested": count, "source": "synthetic", "metric_name": samples[0].metric_name}
        if job_type == "prometheus_collect":
            if not self.config.prometheus_url:
                raise RuntimeError("RUSA_DEVOPS_PROMETHEUS_URL is not configured")
            start = datetime.fromisoformat(payload["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(payload["end"].replace("Z", "+00:00"))
            source = PrometheusTelemetrySource(self.config.prometheus_url, self.config.prometheus_token)
            samples = source.query_range(
                tenant_id, payload["metric_name"], payload["resource_id"],
                start, end, int(payload.get("step_seconds", 60)),
            )
            count = self.store.write_telemetry(tenant_id, samples) if samples else 0
            return {"ingested": count, "source": "prometheus"}
        if job_type == "train_forecaster":
            import pandas as pd
            from .service import run_forecast_experiment

            rows = self.store.read_telemetry(
                tenant_id, payload["metric_name"], payload["resource_id"],
                limit=int(payload.get("max_train_rows", self.config.max_train_rows)),
            )
            if not rows:
                raise RuntimeError("No telemetry available for the requested metric/resource")
            frame = pd.DataFrame([{"ds": row["ds"], "y": row["y"]} for row in rows])
            report = run_forecast_experiment(
                frame,
                metric_name=payload["metric_name"],
                resource_id=payload["resource_id"],
                validation_horizon=int(payload.get("validation_horizon", self.config.validation_horizon)),
                test_horizon=int(payload.get("test_horizon", self.config.test_horizon)),
                oof_folds=self.config.oof_folds,
                downtime_cost=self.config.downtime_cost,
                overprovision_cost=self.config.overprovision_cost,
                artifact_root=self.config.artifact_root,
                tenant_id=tenant_id,
            )
            run_id = self.store.save_model_run(
                tenant_id,
                run_id=report["run_id"],
                metric_name=report["metric_name"],
                resource_id=report["resource_id"],
                artifact_uri=report["artifact_uri"],
                artifact_sha256=report["artifact_sha256"],
                metrics=report["metrics"],
                config={
                    "oof_folds": self.config.oof_folds,
                    "validation_horizon": payload.get("validation_horizon", self.config.validation_horizon),
                    "test_horizon": payload.get("test_horizon", self.config.test_horizon),
                    "downtime_cost": self.config.downtime_cost,
                    "overprovision_cost": self.config.overprovision_cost,
                },
                train_rows=report["train_rows"],
                validation_rows=report["validation_rows"],
                test_rows=report["test_rows"],
            )
            return {
                "run_id": run_id, "metric_name": report["metric_name"],
                "resource_id": report["resource_id"], "metrics": report["metrics"],
                "artifact_sha256": report["artifact_sha256"],
            }
        if job_type == "anomaly_scan":
            rows = self.store.read_telemetry(
                tenant_id, payload["metric_name"], payload["resource_id"],
                limit=int(payload.get("limit", self.config.max_train_rows)),
            )
            if len(rows) < 40:
                raise RuntimeError("At least 40 telemetry points are required for anomaly scanning")
            split = max(20, int(len(rows) * 0.8))
            detector = BayesianAnomalyDetector(
                self.config.anomaly_prior, self.config.anomaly_posterior_threshold
            ).fit([row["y"] for row in rows[:split]])
            assessments = detector.assess(
                [row["y"] for row in rows[split:]],
                [row["ds"].isoformat() for row in rows[split:]],
            )
            created = self.store.record_incidents(
                tenant_id, payload["metric_name"], payload["resource_id"], assessments
            )
            for item in assessments:
                if item.is_anomaly:
                    severity = "high" if item.posterior_probability >= 0.99 else "medium"
                    DEVOPS_ANOMALIES.labels(severity=severity).inc()
            result = {
                "scanned": len(assessments),
                "anomalies": sum(item.is_anomaly for item in assessments),
                "incidents_written": created,
            }
            labels = [row["labels"] for row in rows[split:]]
            if labels and all(item.get("source") == "synthetic" for item in labels):
                from .evaluation import classification_metrics
                expected = [item.get("is_anomaly") == "true" for item in labels]
                result["synthetic_benchmark"] = classification_metrics(
                    expected, [item.is_anomaly for item in assessments]
                )
            return result
        if job_type == "capacity_recommendation":
            recommendation = recommend_capacity(
                payload["predicted_values"], float(payload["current_capacity"]),
                downtime_cost=self.config.downtime_cost,
                overprovision_cost=self.config.overprovision_cost,
                scale_buffer=self.config.scale_buffer,
                min_capacity=float(payload.get("min_capacity", 1.0)),
                max_capacity=float(payload.get("max_capacity", 1_000_000.0)),
            )
            proposal = {
                "action": recommendation.action,
                "current_capacity": recommendation.current_capacity,
                "recommended_capacity": recommendation.recommended_capacity,
                "predicted_peak": recommendation.predicted_peak,
                "target_quantile": recommendation.quantile_level,
                "approval_required": recommendation.approval_required,
                "execution_enabled": False,
                "reason": "Recommendation only; infrastructure mutations are disabled in this release.",
            }
            proposal_id = self.store.create_action_proposal(tenant_id, proposal) if recommendation.approval_required else None
            return {"recommendation": proposal, "proposal_id": proposal_id}
        raise ValueError(f"unsupported DevOps job type: {job_type}")

    def run_forever(self, stop_event=None) -> None:
        while stop_event is None or not stop_event.is_set():
            if not self.run_once():
                time.sleep(self.config.poll_seconds)

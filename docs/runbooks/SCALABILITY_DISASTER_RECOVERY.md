# Scalability and Disaster Recovery Runbook

Status: **implementation in progress; not production-certified**.

Repository: `StrategyG/RusaAi`  
Working branch: `feat/scalability-disaster-recovery-20261106`  
Parent integration branch: `integration/production-100-performance-20261106`

## 1. Current architecture and boundaries

The agent task queue is PostgreSQL-backed. It is not a Redis or RabbitMQ broker.

- Task claims use `FOR UPDATE SKIP LOCKED`, so multiple worker replicas can claim different tasks concurrently.
- Each claim receives a random lease token. Heartbeat, checkpoint, retry, and acknowledgement operations are fenced by the current token.
- Worker execution is one task at a time per process. Add replicas to increase parallel task capacity.
- Checkpoints and task state are stored in PostgreSQL. A crashed worker's lease expires and the task can be reclaimed.
- The audit log is stored on the shared audit volume and uses file locking for concurrent writers. The volume still needs an independent off-host backup/retention policy.
- The inference API is a separate process. Compose restarts a process that exits and exposes a liveness healthcheck. Docker Compose does **not** automatically restart a container solely because it is marked unhealthy; use an orchestrator with liveness-probe restart semantics for production hung-process recovery.

The current queue implementation creates short-lived PostgreSQL connections per operation; it does not yet use a connection pool or PgBouncer. Worker replica count must therefore be capped against the database connection budget until pooling and load tests establish safe limits.

## 2. Horizontal worker scaling

For local or single-host staging, start the agent profile and choose the worker count explicitly:

```bash
docker compose --profile agent up -d --build --scale rusa-agent-worker=3
docker compose --profile agent ps
docker compose logs --tail=100 rusa-agent-worker
```

Scale down without deleting task state:

```bash
docker compose --profile agent up -d --scale rusa-agent-worker=1
```

Do not set `container_name` on the worker service; Compose needs unique replica names. All replicas use the same PostgreSQL queue and the same lease-fencing contract. A replica must not run an older worker version that does not understand lease tokens.

The current worker has one in-flight task per replica. Theoretical service rate is approximately:

`worker_capacity ≈ worker_replicas / mean_task_duration_seconds`

Use measured task duration and peak arrival rate to size the pool. Keep sustained utilization below the approved target and reserve headroom for retries, maintenance, and provider latency. CPU-only autoscaling is insufficient for GPU inference; use queue age/depth and GPU memory/utilization metrics when the model-serving pool is introduced.

## 3. Queue backpressure

`RUSA_AGENT_MAX_OUTSTANDING_PER_TENANT` sets the maximum number of non-terminal tasks for one tenant. The default is 1,000.

The admission count includes `queued`, `retry`, `waiting_external`, `running`, and `awaiting_approval`. Terminal tasks do not consume the budget.

Admission is serialized with a tenant-scoped PostgreSQL advisory transaction lock. This prevents concurrent API replicas from racing past the same tenant limit. Replaying an existing idempotency key still returns the original task even when the tenant is at capacity.

When the limit is reached:
- New submissions return HTTP 429.
- Failed-task retries return HTTP 429.
- The response includes `Retry-After: 10`.
- The client should retry with the same idempotency key after outstanding work drains.

This is tenant-scoped admission control, not a global queue cap. Global throughput remains bounded by the worker pool, PostgreSQL capacity, provider quotas, and API gateway limits. Add a global overload policy only after load tests establish safe database and worker thresholds.

## 4. Worker crash recovery and graceful shutdown

Worker recovery uses durable task state and lease expiry:

1. A worker claims a task and receives a unique lease token.
2. The worker renews its lease while executing and persists checkpoints.
3. If the worker crashes, its lease expires.
4. Another worker claims the task with a new token.
5. A stale worker cannot checkpoint, retry, or acknowledge after its token is replaced.

This provides at-least-once task execution. Repository/tool side effects must remain idempotent or be guarded by the current lease before each action.

On SIGTERM, the worker stops claiming new tasks after the current task finishes. Compose gives the worker a 180-second termination grace period. If the process is forcibly killed before finishing, lease expiry is the recovery path; this is not evidence that the task completed.

Failure-injection acceptance:
- Kill one worker while it owns a task.
- Confirm the task remains durable.
- Wait for lease expiry or force expiry in an isolated test.
- Start another worker and confirm it reclaims the task.
- Confirm the stale lease token cannot checkpoint or acknowledge.
- Confirm no duplicate external side effect is produced.

## 5. Inference/model-server recovery

The Compose inference API uses `restart: unless-stopped`, a 45-second stop grace period, and a liveness healthcheck. A process crash should cause the container runtime to restart the process.

A healthcheck alone is only detection. If the process remains alive but hangs, Docker Compose can report it as unhealthy without restarting it. Production deployment must use an orchestrator that restarts failed liveness probes, plus readiness gates that remove an unready model server from traffic.

Recovery validation must cover:
- Kill the inference process and confirm restart.
- Simulate a model load failure and confirm readiness remains false.
- Simulate an inference timeout and confirm the API returns a bounded error rather than hanging indefinitely.
- Confirm the worker/API can retry or degrade without duplicating task side effects.
- Measure model reload time with the real model artifact and target GPU. No model recovery RTO can be certified without those artifacts and hardware.

## 6. Encrypted PostgreSQL backup

The opt-in `backup` Compose profile builds a separate utility image with PostgreSQL 16 client tools, age encryption, and AWS CLI support.

Generate an age identity outside the repository and store the private identity in a secret manager or protected offline location:

```bash
mkdir -p ./secrets
age-keygen -o ./secrets/rusa-backup-identity.txt
age-keygen -y ./secrets/rusa-backup-identity.txt
```

Set the public recipient in `RUSA_BACKUP_AGE_RECIPIENT`. Do not commit the identity/private key.

Run a one-shot backup:

```bash
docker compose --profile backup run --rm rusa-agent-db-backup
```

The backup script:
- Streams `pg_dump` directly into age encryption; it does not persist a plaintext dump.
- Produces a custom-format PostgreSQL archive encrypted as `.dump.age`.
- Writes a SHA-256 sidecar.
- Applies local backup retention (default 30 days).
- Optionally uploads the encrypted archive and checksum to `RUSA_BACKUP_S3_URI`.

For production, the remote bucket must have versioning and immutable retention/Object Lock configured independently. Use a separate backup credential with write access only to the backup location. Do not store AWS credentials in Git or ordinary application configuration.

The dump covers the application database only. It does not include PostgreSQL cluster roles/global objects, the audit volume, model weights, or source documents stored outside PostgreSQL. Recreate database roles using the reviewed initialization procedure and restore the audit/model/document stores from their own protected backups. The bootstrap database credential is privileged; keep the backup profile disabled except during scheduled backup/restore operations.

The snapshot RPO is no better than the interval between successful backups. The current scripts do not configure WAL archiving or point-in-time recovery. If the approved RPO is shorter than the tested snapshot interval, implement WAL archiving/PITR (for example, pgBackRest or WAL-G) before release.

## 7. Restore procedure

Restore into a separately provisioned PostgreSQL 16/pgvector database. Stop application writers first, recreate required roles/extensions, and verify the target database name before running the destructive restore.

Example:

```bash
docker compose --profile backup run --rm \
  -v "$PWD/secrets/rusa-backup-identity.txt:/run/secrets/age-identity:ro" \
  -e RESTORE_DATABASE_URL='postgresql://rusa_agent_owner:REDACTED@rusa-agent-db:5432/rusa_agent_restore' \
  -e BACKUP_FILE='/backups/rusa-agent-<timestamp>.dump.age' \
  -e BACKUP_AGE_IDENTITY_FILE=/run/secrets/age-identity \
  -e RESTORE_CONFIRM_DATABASE=rusa_agent_restore \
  rusa-agent-db-backup /app/scripts/postgres/restore.sh
```

The restore script refuses PostgreSQL system databases, requires an exact target database-name confirmation, verifies the checksum when available, decrypts to a temporary file, validates the archive, and restores in one transaction. The `--clean` operation replaces objects in the confirmed target database. Never point it at production unless the incident commander has approved that exact target.

Run a repeatable backup-and-restore drill with distinct source and target databases:

```bash
bash scripts/postgres/restore_drill.sh
```

Required environment: `SOURCE_DATABASE_URL`, `RESTORE_DATABASE_URL`, `RESTORE_CONFIRM_DATABASE`, `BACKUP_DIR`, `BACKUP_AGE_RECIPIENT`, and `BACKUP_AGE_IDENTITY_FILE`. The script reports backup duration, restore duration, and total recovery duration.

CI now performs an encrypted PostgreSQL backup/restore drill against a disposable database and verifies a restored marker row. This validates the script path and archive integrity in CI; it does not establish production RTO/RPO or off-host recovery.

## 8. Disaster recovery drill and evidence

Run at least quarterly and after major database/schema changes. Use an isolated recovery target, not the live database.

Record:
- Source environment and database identifier (never credentials).
- Repository commit SHA and image tags.
- Backup object name, checksum, encryption-key version, and backup timestamp.
- Incident/failure start time and service restoration time.
- Data watermark at the source and restored target.
- Backup duration, restore duration, validation results, and observed data loss.
- Actual RTO and RPO, compared with the approved targets.
- Operator, reviewer, follow-up actions, and evidence links.

Minimum failure scenarios:
1. Worker process termination during a running task.
2. Inference process termination and model reload.
3. PostgreSQL unavailable, followed by database restore to an isolated target.
4. Host/volume loss with recovery from off-host encrypted backup.
5. Queue saturation with HTTP 429 and successful retry after capacity drains.

## 9. RTO/RPO targets and release gate

No owner-approved RTO/RPO values or production storage/deployment environment are recorded yet. The following are **proposals only**, not accepted SLOs:

| Recovery scope | Proposed RTO | Proposed RPO | Evidence needed |
|---|---:|---:|---|
| Agent/control-plane PostgreSQL | 4 hours | 1 hour | Scheduled encrypted backup, off-host copy, restore drill |
| Agent workers | 30 minutes | 0 acknowledged task loss beyond database RPO | Lease-expiry and checkpoint recovery drill |
| Inference service | 2 hours | Model artifact is immutable/versioned | Real model reload test on target GPU |
| RAG/index data | 4 hours | 1 hour | Source-document backup and re-index/restore drill |

The PostgreSQL proposal requires successful backups at least hourly or a verified WAL/PITR design. A successful CI restore does not prove that the 4-hour/1-hour targets are met.

Do not mark this workstream 5/5 until the owner approves targets and a staging drill demonstrates the measured RTO/RPO under the actual deployment topology. Production deployment and release remain separate approval gates.

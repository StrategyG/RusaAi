# Phase 5 — Closed-loop operations control plane

## Scope delivered in this increment

This increment adds the durable control-plane foundation for SLO alert correlation, incident lifecycle, kill switches, and workflow-budget gates. It does not provision live telemetry, runbook, notification, or dashboard infrastructure.

### Durable incident lifecycle

- Monitor computes a stable fingerprint from tenant, service, environment, and host-owned SLO rule. Observation timestamps and metric windows do not create new incident identities.
- PostgreSQL migration 014 adds tenant-scoped incident state, an idempotent closed-loop inbox, and append-only incident events.
- Repeated observations correlate to one incident and increase its occurrence count. Replayed message IDs do not increase the count.
- Incident transitions use state_version compare-and-set and an explicit state-transition graph.
- Monitor may emit a tenant-bound incident:// reference. Operate may resolve or advance that incident only after the immutable runbook report is stored and linked as evidence.

### Kill switches and budgets

- Tenant and platform scopes support all, workflow, devops, individual lifecycle stages, model, and tool.
- The kill-switch lookup is fail-closed. Platform-level switches are visible to tenant workers but writable only in platform tenant context.
- WorkflowExecutionGuard checks the action kill switch before consuming tool-call, token, or cost budgets from the durable workflow store.
- Production hosts must inject the guard into model/tool/stage adapters. The library exposes the guard; it does not silently assume that an unconfigured gateway is protected.

### Database migration

Apply 014_devops_closed_loop_governance.sql after 013_devops_task_leases_and_cancellation.sql on the platform database. The migration creates forced-RLS tables and immutable event ledgers. No migration runs automatically at application startup.

## Verification

The unit suite covers stable fingerprints, immutable evidence validation, guard ordering, fail-closed budgets, Monitor persistence, and Operate incident transitions. PostgreSQL integration tests cover deduplication, replay idempotency, lifecycle CAS, reopen semantics, tenant scoping, and tenant/platform kill switches when RUSA_DATABASE_URL is available.

## Remaining production gates

- Provision and operate real telemetry, alert-routing, runbook, notification, and SLO dashboard adapters.
- Inject WorkflowExecutionGuard into every production model and tool gateway, not only the Operate/Monitor specialists.
- Run staging soak, on-call routing, rollback, backup/restore, worker recovery, and disaster-recovery rehearsals.
- Complete independent security review and owner sign-off before any production certification.

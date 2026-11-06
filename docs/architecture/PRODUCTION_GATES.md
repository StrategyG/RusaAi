# RUSA-AI Production Gates

A production-ready claim requires evidence for every applicable gate.

| Gate | Required evidence | Current implementation |
|---|---|---|
| API serving | Auth, schema validation, readiness, model artifact test | Tracked by serving foundation PR |
| Agent contracts | Typed task/result contracts and valid state machine | Implemented in agent foundation branch |
| Hierarchical orchestration | DAG validation, concurrency bounds, dependency failure propagation | Implemented in agent foundation branch |
| Policy | Default deny, approval gates, prohibited actions | Implemented in agent foundation branch |
| Memory | Tenant/project-scoped persistence and restart test | SQLite adapter implemented; single-instance only |
| Audit | Append-only hash chain and verification test | SQLite adapter implemented; external immutable backup still required |
| Tools | Explicit registry and policy enforcement | Registry implemented; production adapters not yet enabled |
| Model | Licensed artifact, revision, checksum, tokenizer, chat template | Blocked until model artifact is supplied |
| Sandbox | Container/VM isolation, filesystem/network/resource limits | Not yet implemented |
| Distributed execution | Durable queue, idempotency, leases, cancellation, recovery | Not yet implemented |
| Observability | OpenTelemetry traces, metrics, dashboards, alerting | Not yet implemented |
| Security operations | TLS gateway, secret rotation, tenant penetration tests | Deployment-specific work remains |
| Reliability | Backup/restore, disaster recovery, incident runbooks | Not yet evidenced |
| Performance | Target hardware load and soak tests with p95 SLOs | Blocked until target hardware/model are supplied |
| Release | Staging soak, canary, rollback, owner approval | Not performed |

## Release rule

Do not mark the complete RUSA-AI product production-ready while any critical gate is open. Green unit tests validate only the tested code paths and do not certify model quality, isolation, scaling, or operations.

# RUSA-AI autonomous implementation roadmap

This document separates the current repository implementation from the broader agent specifications supplied for RUSA-AI.

## Implemented baseline in this repository

- `AICodeOrchestrator` coordinates the existing Analyst, Architect, Developer, Debugger, Tester, and Security workers.
- `AutonomousCodeWorker` uses a bounded JSON action loop.
- `WorkspaceToolGateway` is the final enforcement point for named tools, safe paths, and sandboxed validation.
- `AutonomyPolicy` keeps protected operations and high-risk work outside autonomous authorization.
- PostgreSQL queue/lease, audit, tenant, inference artifact controls, bounded Code Refinery, alias-aware AST security checks, shared credential scanning, and cooperative cancellation/reconciliation are present on the integration branch.
- Transport-neutral v1.0 task, result, and run-event JSON Schemas plus Python validators cover the four platform domains, ten core-agent roles, eight lifecycle stages, and eight distinct canonical DevOps stage-agent IDs. Task validation keeps `core_agent` and `devops_stage_agent` identities separate and binds stage-agent IDs to their lifecycle stages. A separate A2A task envelope is also defined. These are contract foundations only, not a broker, transactional outbox, event store, or cross-domain agent runtime.
- The new SFT pipeline trains only the existing six-role worker response contract. It does not invent runtime support for agents that are not implemented yet.

## Mapping to the ten-agent specification

| Specified agent | Current implementation status |
|---|---|
| Supreme Orchestrator | Partially represented by `AICodeOrchestrator`; broader cross-domain orchestration remains future work |
| System Architect | Partially represented by `WorkerRole.ARCHITECT` in the coding workflow |
| Planner | Planning responsibilities are partly in the orchestrator/task intake; dedicated planner agent is future work |
| Task Decomposer | Existing coding execution DAG is a baseline; a general-purpose task-decomposer agent is future work |
| Software Engineer | Implemented through Developer and Debugger workers |
| DevOps | Not yet a separate runtime agent |
| QA & Testing | Implemented for the coding workflow through Tester |
| Security & Policy | Security worker plus deterministic `AutonomyPolicy` and gateway enforcement |
| Memory & Knowledge | Existing SQLite task memory and tenant-scoped RAG/knowledge services; dedicated agent behavior remains future work |
| Evaluation & Reporting | Existing metrics/audit/evidence are a baseline; dedicated evaluation/reporting agent remains future work |

## Eight-stage DevOps lifecycle

The separate Plan, Code, Build, Test, Release, Deploy, Operate, and Monitor agents remain a subsequent implementation phase. The current PR does not grant autonomous release/deploy/production permissions. Release, deployment, secrets, permissions, and destructive actions remain approval-gated.

## Next implementation phases

1. **Agent contract foundation:** harden and integrate the existing versioned task/result/run-event and A2A schemas with durable cross-domain run state, transactional outbox, idempotent event consumers, cursor-based status projections, and the existing AICode task contract. Keep the ADR's message envelope as a separately versioned transport contract until an explicit compatibility mapping is approved.
2. **Orchestrator and decomposer:** bind the new run/task/result/event contracts to durable cross-domain workflow state, general DAG validation, bounded retry/replan budgets, cancel-aware execution, transactional outbox, and event-sourced audit evidence.
3. **Agent registry:** implement separate capability declarations and per-agent identities; avoid sharing a broad administrator token.
4. **DevOps workers:** add isolated Build, Release, Deploy, Operate, and Monitor workers with artifact provenance, SLO/incident contracts, and explicit human approval gates; keep Code and Test specialist contracts separate.
5. **Model expansion:** extend SFT data and evaluation only when the corresponding agent runtime contract and tool permissions exist.
6. **Production qualification:** add adversarial, tenant-isolation, reliability, load/soak, recovery, and operator sign-off evidence.

The ADR's gRPC/Protobuf, RabbitMQ command queues, Kafka event log, transactional outbox, JWS message signing, and internal mTLS are still target architecture—not current runtime capabilities. The existing PostgreSQL queue is the durable AICode baseline; do not claim broker/event-bus conformance until those components and their recovery tests are deployed.

The fine-tuned model is an implementation detail inside the agent execution layer. It must never become the policy engine, workflow state store, or source of authorization truth.

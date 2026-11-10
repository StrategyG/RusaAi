# RUSA-AI autonomous implementation roadmap

This document separates the current repository implementation from the broader agent specifications supplied for RUSA-AI.

## Implemented baseline in this repository

- `AICodeOrchestrator` coordinates the existing Analyst, Architect, Developer, Debugger, Tester, and Security workers.
- `AutonomousCodeWorker` uses a bounded JSON action loop.
- `WorkspaceToolGateway` is the final enforcement point for named tools, safe paths, and sandboxed validation.
- `AutonomyPolicy` keeps protected operations and high-risk work outside autonomous authorization.
- PostgreSQL queue/lease, audit, tenant, and inference artifact controls already exist in the repository.
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

## Phase 1 — Agent contract foundation

Implemented on the feature branch (PR #95): Draft 2020-12 task/result/event schemas, a permission-scoped registry for the ten general agents and eight lifecycle agents, deterministic protected-action decisions, evidence-bound handoff validation, and a synthetic Plan Agent dry-run test. Existing AICode contracts remain unchanged.

Migration `009_aicode_queue_partitions.sql` partitions the existing PostgreSQL queue by worker capability: AICode defaults to `queue_name="aicode"`, while a future DevOps worker must explicitly claim and acknowledge `queue_name="devops"`. The same tenant RLS boundary and durable queue are retained; no RabbitMQ/Kafka service or second queue is introduced. DevOps task intake remains disabled until its dedicated worker and API adapter exist.

## Eight-stage DevOps lifecycle

The separate Plan, Code, Build, Test, Release, Deploy, Operate, and Monitor agents remain a subsequent implementation phase. The current PR does not grant autonomous release/deploy/production permissions. Release, deployment, secrets, permissions, and destructive actions remain approval-gated.

## Next implementation phases

1. **Orchestrator and decomposer:** add durable workflow state, dependency-DAG validation, bounded retry/replan budgets, cancellation, and audit evidence.
2. **Stage-agent runtime:** implement Plan, Code, Build, and Test workers using the new contracts and existing queue partitions.
3. **Release and operations workers:** add Release, Deploy, Operate, and Monitor workers with explicit human approval gates and rollback evidence.
4. **Integration and evaluation:** connect the workers to RusaModels through the least-privilege inference role; extend SFT data and evaluation only when the corresponding runtime contracts and tool permissions exist.
5. **Production qualification:** add adversarial, tenant-isolation, reliability, load/soak, recovery, and operator sign-off evidence.

The fine-tuned model is an implementation detail inside the agent execution layer. It must never become the policy engine, workflow state store, or source of authorization truth.

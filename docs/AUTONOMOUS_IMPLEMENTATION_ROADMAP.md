# RusaAi autonomous implementation roadmap

This document separates the current repository implementation from the broader agent specifications supplied for RUSA-AI.


## Relationship to the v2.0 neural core

The Qwen3-4B QLoRA configuration is a separate legacy experiment for the
six-role AICode worker JSON action contract. It is not the RusaAi-Edge-1.54B
neural core and does not satisfy the v2.0 requirement for a non-Qwen
foundation checkpoint. The core checkpoint and tokenizer remain unselected;
the core artifact is not built, training has not started, and no device
benchmark is available. See docs/architecture/IMPLEMENTATION_ALIGNMENT.md.
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

## Eight-stage DevOps lifecycle

The separate Plan, Code, Build, Test, Release, Deploy, Operate, and Monitor agents remain a subsequent implementation phase. The current PR does not grant autonomous release/deploy/production permissions. Release, deployment, secrets, permissions, and destructive actions remain approval-gated.

## Next implementation phases

1. **Agent contract foundation:** add versioned task/result/event schemas for the wider ten-agent and eight-stage lifecycle while retaining current AICode contracts.
2. **Orchestrator and decomposer:** add durable workflow state, DAG validation, bounded retry/replan budgets, cancellation, and audit evidence.
3. **Agent registry:** implement separate capability declarations and per-agent identities; avoid sharing a broad administrator token.
4. **DevOps workers:** add isolated build/test/release/deploy/operate/monitor workers with explicit human approval gates.
5. **Model expansion:** extend SFT data and evaluation only when the corresponding agent runtime contract and tool permissions exist.
6. **Production qualification:** add adversarial, tenant-isolation, reliability, load/soak, recovery, and operator sign-off evidence.

The fine-tuned model is an implementation detail inside the agent execution layer. It must never become the policy engine, workflow state store, or source of authorization truth.

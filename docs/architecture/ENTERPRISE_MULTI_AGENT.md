# RUSA-AI Enterprise Hierarchical Multi-Agent Architecture

## Architecture decision

RUSA-AI uses a hierarchical multi-agent architecture with one supervisor, domain managers, specialist agents, and bounded execution workers. Autonomy is full within an explicit policy allowlist. Protected operations require an approval record; prohibited operations remain denied even if an approval flag is supplied.

## Runtime layers

1. API and identity boundary: authenticates callers and establishes tenant/project context.
2. Supervisor: owns the top-level objective, plan lifecycle, and final synthesis.
3. Managers: decompose domain work and route capabilities to registered specialists.
4. Specialists: perform bounded domain reasoning and create execution plans.
5. Workers: execute one task with explicit limits and return typed evidence.
6. Tool gateway: checks identity, policy, risk, and approval before invoking a registered adapter.
7. Evaluation: checks task identity, acceptance criteria, evidence, and security violations.
8. Memory and audit: persist scoped task context and tamper-evident event chains.

## Communication

The internal contract carries task ID, parent task ID, trace ID, tenant ID, project ID, capability, dependencies, acceptance criteria, risk, and metadata. A2A adapters translate this internal contract to external A2A messages. Event delivery must be treated as at-least-once; consumers must be idempotent by task ID and event ID. MCP is reserved for tool/resource access, not agent task scheduling.

## Worker orchestration

The DAG orchestrator validates unique task IDs, known dependencies, and acyclic dependencies before execution. Ready tasks may execute concurrently up to a configured limit. A failed dependency blocks downstream tasks. Retry count and concurrency are bounded. Every successful task must satisfy all acceptance criteria and have no security violations. Worker output is untrusted until evaluated.

## Memory

The first adapter is SQLite and is intended for local or single-instance development. All task and memory queries include tenant and project scope. Multi-replica production must use a shared transactional database, migrations, encryption at rest, retention/deletion workflows, and tenant-isolation tests. Semantic/vector retrieval is a later adapter, not a claim of current capability.

## Tools and autonomy

Tools are explicitly registered with a policy action and risk level. Unknown tools fail closed. No arbitrary shell execution is exposed to model output. Production deployment, merge, release, secret changes, permission changes, destructive data operations, and production writes require explicit approval. Audit disabling, secret exfiltration, protected-branch force pushes, and self-modification of policy are prohibited.

## Evaluation

The deterministic evaluator checks worker success, exact acceptance-criteria coverage, and security violations. LLM-as-judge may be added as advisory scoring but cannot override deterministic gates. Test evidence should reference immutable CI runs, commit SHAs, or artifact digests.

## Production boundary

This implementation is a runtime foundation, not a complete production certification. Before multi-tenant or internet-facing production, add a durable distributed workflow engine, shared database adapter, sandbox isolation, distributed rate limiting, external secret manager, signed approval records, OpenTelemetry export, alerting, backup/restore drills, load/soak tests, penetration tests, and model quality/capacity benchmarks.

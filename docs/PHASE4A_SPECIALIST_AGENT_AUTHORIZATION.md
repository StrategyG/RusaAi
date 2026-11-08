# Phase 4A — specialist agent authorization boundary

## Scope

This increment starts Phase 4 of the Agentic DevOps implementation plan. It
adds the registry and execution authorization boundary for the eight lifecycle
specialists and the cross-cutting Security Guard. It does not yet register
production stage handlers, start a RabbitMQ worker fleet, or provision tools.

The Supreme Orchestrator remains the system-wide coordinator. The DevOps
Orchestrator remains the lifecycle coordinator beneath it. The registry is
not a workflow-state store and does not replace PostgreSQL, AICode, or the
existing worker pipeline.

## Registered identities

| Identity | Stage | Default execution boundary |
|---|---|---|
| rusa_plan_agent | Plan | Repository/issue/policy read, plan and task creation |
| rusa_code_agent | Code | Feature-branch repository changes and scoped documentation/artifacts |
| rusa_build_agent | Build | Reproducible build, artifact, manifest and SBOM production |
| rusa_test_agent | Test | Execute tests and write test/evidence results; no source-code write |
| rusa_release_agent | Release | Read evidence and prepare a release candidate; no deployment |
| rusa_deploy_agent | Deploy | Deploy an approved immutable release and verify health |
| rusa_operate_agent | Operate | Read telemetry and execute approved runbooks |
| rusa_monitor_agent | Monitor | Read-only telemetry and evidence |
| rusa_security_guard_agent | Cross-cutting | Read-only review at any lifecycle stage |

These are DevOps lifecycle identities. The ten system-wide agents retain the
authority mapping in DEVOPS_AGENT_INTEGRATION.md; the Supreme Orchestrator
does not become a stage worker and no new global authority is introduced.

## Authorization model

A task contract is validated first. Authorization then requires all of the
following to match:

1. Task tenant, authenticated workload tenant, and Policy Engine grant tenant.
2. Task agent ID, authenticated SPIFFE principal, and grant principal.
3. Task stage and the immutable stage assignment in the registry. Security
   Guard is the only cross-stage identity.
4. Task policy version and the current Policy Engine grant version.
5. Requested tools are a subset of both the registered profile and the
   Policy Engine grant.
6. Every requested write path is covered by the grant's write scope.
7. Network access is explicitly enabled by both the profile and the grant;
   every task destination must be in the grant's exact host allowlist.
8. Task cost, tool-call, and runtime budgets do not exceed the grant.
9. Protected operations have a policy-verified approval reference and scope.

The task payload is untrusted input. Its allowed_tools, write_scope,
network destinations, approval reference, and budget never grant themselves
authority. The trusted grant is supplied separately by the Policy Engine.
The grant also pins Code writes to explicit feature/feat branches, Operate
execution to an approved runbook reference, and Deploy to an approved
environment. Protected branches are not valid Code branch grants.

## Tool boundary

Handlers receive ScopedToolGateway, not the raw tool executor. The gateway
checks tool IDs, path scopes, network destinations, production deployment
approval, destructive runbook approval, and tool-call budget at each
invocation. It exposes no generic shell, secret-read, protected-branch merge,
or unrestricted deployment capability.

Monitor and Security Guard profiles are read-only. Code receives branch-write
capability only; merge is not a registered capability. Build and Test are
separate identities so test evidence cannot be self-certified by the Code
handler. Release preparation is distinct from deployment.

Production Deploy requires a deploy approval scope and an approval reference
that is present in the trusted grant. Every Deploy tool invocation must match
the task's target environment and the policy allowlist. Operate runbook
execution must match the task's exact runbook reference. Destructive Operate
actions require a destructive_action approval reference.

## Current boundary and next work

This increment supplies authorization primitives and a pluggable handler
interface. No stage handler is registered by default. A worker must fail
closed when no handler exists. The next Phase 4 increments can add one adapter
at a time, beginning with Plan and Code, then Build and independent Test.

Before a staging worker is enabled, its adapter must:
- obtain an accepted ClaimTask lease before execution;
- heartbeat and stop when the lease/cancel epoch is no longer valid;
- use only the scoped gateway;
- emit a schema-valid result tied to the originating task;
- bind Code/Build/Test evidence to the exact source commit and artifact digest;
- keep Code work in an isolated feature branch/worktree;
- require independent Test evidence before Release;
- leave protected-branch merge and production approval to human-controlled policy.

## Validation

Unit tests cover registry completeness, immutable stage assignment, tenant and
principal binding, policy-version binding, capability intersection, read-only
agents, write and network scopes, approval references, budgets, tool-call
fencing, result validation, and missing-handler failure. These are
authorization-unit tests, not evidence of a provisioned production agent fleet.

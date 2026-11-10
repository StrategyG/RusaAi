# RUSA AI Capability Status Matrix

**Last reviewed:** 10 November 2026  
**Source of truth for labels:** Platform Architecture 2.0 and ADR-PLATFORM-001/002

This matrix deliberately separates executable AICode worker roles from the target shared-agent catalogue and the eight-stage DevOps lifecycle. A role implemented inside the current coding runtime is not automatically a separately registered platform agent.

## Status vocabulary

| Status | Meaning | May be labelled active in UI? |
|---|---|---|
| implemented_runtime_role | Executable role in the current AICode worker runtime; not a versioned platform registry entry | Only as a current AICode role, never as a registered platform agent |
| partially_represented | Some responsibilities exist in current code, but the specified agent identity/runtime is not registered | No |
| architecture_target | Defined in architecture or agent specifications; no matching current runtime registration | No |
| planned | Future capability without a current implementation | No |
| registered_validated | Versioned manifest has been registered and validated against runtime, schemas, and policy | No; not until activated |
| active_registered | Active versioned registration plus passing execution evidence and health | Yes, with evidence |
| suspended, deprecated, revoked | Registry lifecycle states | No |

## Current AICode runtime roles

| Identity | Owner | Status | Contract version | Tests | Evidence |
|---|---|---|---|---|---|
| Analyst | AICode | implemented_runtime_role | aicode-worker-v1 | worker and tool-gateway tests | [contracts](../rusa_agents/contracts.py), [tool allowlist](../rusa_agents/agent_tools.py) |
| Architect | AICode | implemented_runtime_role | aicode-worker-v1 | worker and tool-gateway tests | [contracts](../rusa_agents/contracts.py), [tool allowlist](../rusa_agents/agent_tools.py) |
| Developer | AICode | implemented_runtime_role | aicode-worker-v1 | worker and tool-gateway tests | [contracts](../rusa_agents/contracts.py), [tool allowlist](../rusa_agents/agent_tools.py) |
| Debugger | AICode | implemented_runtime_role | aicode-worker-v1 | worker and tool-gateway tests | [contracts](../rusa_agents/contracts.py), [tool allowlist](../rusa_agents/agent_tools.py) |
| Tester | AICode | implemented_runtime_role | aicode-worker-v1 | evidence and tool-gateway tests | [contracts](../rusa_agents/contracts.py), [orchestrator](../rusa_agents/orchestrator.py) |
| Security | AICode | implemented_runtime_role | aicode-worker-v1 | security and tool-gateway tests | [contracts](../rusa_agents/contracts.py), [policy](../rusa_agents/policy.py) |

## Ten shared/core agent identities

| Agent identity | Current mapping | Owner | Status | Contract version | Tests / evidence |
|---|---|---|---|---|---|
| Supreme Orchestrator | No cross-domain runtime; AICodeOrchestrator is coding-domain only | Platform | architecture_target | manifest-v1 / task-v2 target | [architecture](PLATFORM_ARCHITECTURE_2.0.md), [orchestrator](../rusa_agents/orchestrator.py) |
| System Architect | Partially represented by Architect worker role | Software Engineering | partially_represented | aicode-worker-v1; manifest-v1 target | [contracts](../rusa_agents/contracts.py), [worker tests](../tests/test_aicode_contracts.py) |
| Planner | Planning responsibilities in task intake/orchestrator; no dedicated agent | Software Engineering | partially_represented | manifest-v1 target | [orchestrator](../rusa_agents/orchestrator.py) |
| Task Decomposer | Bounded AICode execution DAG; no general task-decomposer agent | Platform | partially_represented | manifest-v1 target | [orchestrator](../rusa_agents/orchestrator.py) |
| Software Engineer | Implemented through Developer and Debugger roles | Software Engineering | partially_represented | aicode-worker-v1; manifest-v1 target | [tool allowlist](../rusa_agents/agent_tools.py) |
| DevOps | No separate DevOps runtime identity | Software Engineering | planned | manifest-v1 target | [architecture](PLATFORM_ARCHITECTURE_2.0.md) |
| QA & Testing | Tester role covers current coding QA, not a general QA agent | Quality | partially_represented | aicode-worker-v1; manifest-v1 target | [evidence gate](../rusa_agents/orchestrator.py) |
| Security & Policy | Security role plus AutonomyPolicy and Tool Gateway; no registry identity | Platform Security | partially_represented | aicode-worker-v1; manifest-v1 target | [policy](../rusa_agents/policy.py), [gateway](../rusa_agents/tool_gateway.py) |
| Memory & Knowledge | Memory and tenant-scoped RAG exist; no dedicated agent identity | Intelligence | partially_represented | manifest-v1 target | [memory](../rusa_agents/memory.py), [knowledge](../rusa_knowledge/) |
| Evaluation & Reporting | Metrics/audit/evidence exist; no dedicated evaluator identity | Platform Quality | partially_represented | manifest-v1 target | [metrics](../rusa_agents/metrics.py), [audit](../rusa_agents/audit.py) |

## Eight DevOps lifecycle identities

| Agent identity | Owner | Status | Contract version | Tests / evidence |
|---|---|---|---|---|
| rusa_plan_agent | Software Engineering | planned | manifest-v1 target | No dedicated runtime registration |
| rusa_code_agent | Software Engineering | planned | manifest-v1 target | Developer/Debugger are current roles, not aliases |
| rusa_build_agent | Software Engineering | planned | manifest-v1 target | No dedicated runtime registration |
| rusa_test_agent | Software Engineering | planned | manifest-v1 target | Tester is current AICode role, not a separate lifecycle identity |
| rusa_release_agent | Software Engineering | planned | manifest-v1 target | No dedicated runtime registration |
| rusa_deploy_agent | Software Engineering | planned | manifest-v1 target | No dedicated runtime registration |
| rusa_operate_agent | Software Engineering | planned | manifest-v1 target | No dedicated runtime registration |
| rusa_monitor_agent | Software Engineering | planned | manifest-v1 target | No dedicated runtime registration |

Cross-cutting rusa_devops_orchestrator and rusa_security_guard_agent are target identities. Human approvers remain people, not agent registrations.

## Product domains

| Domain | Current evidence | Status | Owner |
|---|---|---|---|
| Software Engineering & DevOps | AICode coding API, six worker roles, PostgreSQL queue/leases, Git workspace, sandbox, evidence gate | partially_represented | Software Engineering |
| RUSA Intelligence | Provider abstraction, tenant-scoped RAG/pgvector, read-only MCP | partially_represented | Intelligence |
| RUSA Business | Architecture direction only | planned | Business |
| RUSA Ecosystem | Architecture direction only | planned | Ecosystem |

## Promotion rule

A capability may move to active_registered only when all of the following exist:

1. A versioned manifest with a unique agent identity.
2. A validated capability declaration, input/output schema, runtime binding, tenant/resource scope, policy profile, budget, and approval policy.
3. A registry record with active status; a runtime startup check that rejects unknown, suspended, or revoked versions.
4. Contract, authorization, tenant-isolation, failure/recovery, and execution tests.
5. A passing execution record and health evidence for the registered version.
6. An owner and evidence links maintained in this matrix.

A manifest or UI label alone does not establish that an agent is executable. The Control Center must not show planned or partially represented identities as active.

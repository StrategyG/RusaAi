# QA Independence and Policy Enforcement Validation

**Repository:** StrategyG/RusaAi  
**Validation scope:** AICode runtime controls currently present in the repository  
**Status:** HOLD for full enterprise QA-independence and policy-enforcement certification

## 1. Purpose

This suite tests the controls that are implemented today and records the controls that remain architectural or deployment blockers. A passing unit-test suite is not equivalent to proof that an independent QA service, a centralized PDP/PEP, or production egress controls are deployed.

The suite uses the existing Python, pytest, Ruff, and GitHub Actions toolchain. The existing CI workflow runs the complete pytest suite, so tests added here are included without adding another test framework.

## 2. Current implementation boundary

The repository currently provides:

- AICode orchestration with separate TESTER and SECURITY worker roles.
- A role-scoped workspace gateway: only DEVELOPER may write workspace files; TESTER and SECURITY can run their permitted validation profiles.
- An AutonomyPolicy used by the orchestrator for repository-read and workspace-write decisions.
- A task queue authorization check bound to tenant, worker identity, and a live lease.
- Task-policy audit persistence and hash-chained audit records, with optional mandatory HMAC for the local audit log.
- Existing regression coverage for missing/failed validation evidence, tenant isolation, lease fencing, and worker kill-switch behavior.

Important limitations:

1. TESTER and SECURITY are role-separated workers, not a separately deployed Validation/QA service with its own workload identity and credentials. The current runtime builds the role workers against the same workspace and WorkspaceToolGateway instance.
2. QA evidence is produced by the bounded run_check tool. The orchestrator now checks that the evidence profile matches the required check and that exit_code is an actual integer zero. This catches mislabelled or malformed evidence; it is not a cryptographic attestation.
3. The current orchestrator does not bind QA evidence to a cryptographic digest of the exact artifact snapshot later committed or published.
4. AutonomyPolicy is an orchestration-level action allowlist. It is not yet a centralized deterministic PDP that evaluates every final tool action and is enforced by a PEP at every external boundary.
5. The production readiness ledger identifies worker/sandbox egress enforcement as an open blocker. This suite does not claim that direct network bypass has been eliminated.

## 3. Automated test matrix

| Test ID | Control | Automated evidence | Current status |
|---|---|---|---|
| QA-01 | TESTER and SECURITY cannot write workspace files | Role enforcement test in WorkspaceToolGateway | Automated |
| QA-02 | Missing mandatory QA evidence cannot certify success | Existing orchestrator regression test | Automated |
| QA-03 | Non-zero test result blocks completion | Existing orchestrator regression test | Automated |
| QA-04 | Evidence profile must match the required check | New profile-binding unit test | Automated |
| QA-05 | Exit code must be integer 0; boolean false is not accepted as 0 | New strict-type unit test | Automated |
| QA-06 | QA verdict is bound to immutable artifact digest | No digest-attestation contract in current WorkerResult | HOLD |
| QA-07 | QA has an independent service identity and isolated read-only snapshot | Current runtime shares workspace and gateway | HOLD |
| PE-01 | Protected actions require approval | Parameterized AutonomyPolicy tests | Automated |
| PE-02 | Prohibited actions are denied | Parameterized AutonomyPolicy tests | Automated |
| PE-03 | Explicit empty allowlist denies all actions | Regression test and policy initialization fix | Automated |
| PE-04 | HIGH and CRITICAL risk require approval | Parameterized AutonomyPolicy tests | Automated |
| PE-05 | Required policy audit fails closed when the sink is missing or unavailable | New record_task_policy tests | Automated |
| PE-06 | HMAC audit detects record tampering | New AuditLog tests | Automated |
| PE-07 | Every external tool action is evaluated by a central PDP and enforced by a PEP | No complete central PDP/PEP path across external actions yet | HOLD |
| PE-08 | Mutating actions fail closed when policy state cannot be verified | Runtime-wide enforcement is not yet demonstrated | HOLD |
| PE-09 | Cross-tenant access and stale lease attempts are rejected | Existing PostgreSQL RLS and queue/lease regression suites | Existing coverage; rerun on exact commit |
| PE-10 | Worker does not claim tasks when AICode is disabled | Existing worker kill-switch regression test | Existing coverage; rerun on exact commit |
| PE-11 | Outbound egress cannot bypass the approved gateway | Production egress default-deny is an open blocker | HOLD |

## 4. Run instructions

Run the focused suite:

```bash
python -m pytest -q \
  tests/test_qa_policy_validation.py \
  tests/test_aicode_runtime.py \
  tests/test_tool_gateway.py
```

Run the complete repository suite:

```bash
python -m pytest -q
ruff check rusa_agents tests
```

The existing GitHub Actions CI workflow already executes `python -m pytest -q` and also runs the suite in the isolated Docker sandbox. These tests therefore run in both existing CI paths.

## 5. Verdict semantics

- **PASS:** all implemented controls in the required test cohort pass and their evidence is complete.
- **HOLD:** no confirmed bypass is observed in the executed cohort, but one or more critical controls are not implemented or lack sufficient evidence.
- **FAIL:** an implemented control is bypassed, a prohibited action is allowed, malformed QA evidence is accepted, or required audit integrity fails.

A HOLD must not be represented as a production security certification. The current overall status remains **HOLD** because QA service identity separation, artifact-digest attestation, complete runtime PDP/PEP enforcement, and deny-by-default egress have not been demonstrated.

## 6. Required work before full certification

1. Introduce a dedicated QA workload identity and a read-only, immutable artifact snapshot.
2. Generate a cryptographic digest for the exact artifact version under test. Bind the QA verdict to tenant, task, artifact digest, test profile, tool/image digest, and policy version.
3. Make the release/commit PEP verify the signed QA verdict and recompute or verify the artifact digest immediately before any side effect.
4. Route every protected tool, data, publication, and deployment action through a deterministic PDP/PEP decision path.
5. Prove fail-closed behavior when policy, identity, revocation state, or mandatory audit storage cannot be verified.
6. Enforce outbound egress through a default-deny policy or controlled egress proxy, then run negative bypass tests.
7. Run adversarial tests against real non-superuser runtime identities in staging, including tenant crossover, expired/revoked credentials, stale queue messages, replay, and kill-switch races.

## 7. Release decision

Do not use a green pytest result alone to mark QA independence or policy enforcement as validated. The suite is a regression baseline for implemented controls. Full enterprise certification remains on HOLD until every required control above has implementation evidence and passes its corresponding integration and red-team tests.

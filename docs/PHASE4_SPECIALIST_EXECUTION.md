# Phase 4B — Plan and Code specialist execution

## Phase 4A — specialist authority foundation

PR #68 establishes the execution-time authority boundary for the eight
lifecycle specialists and the cross-cutting Security Guard. Its registry is
a capability ceiling, not an authorization grant.

## Phase 4B — Plan and Code execution

This increment implements structured planning and isolated Code execution.
It is not a claim that the complete eight-agent worker fleet or staging
delivery path is production-ready.

## Agent registry

```text
rusa_plan_agent
rusa_code_agent
rusa_build_agent
rusa_test_agent
rusa_release_agent
rusa_deploy_agent
rusa_operate_agent
rusa_monitor_agent
rusa_security_guard_agent (cross-cutting)
```

Each identity has a fixed lifecycle stage (except Security Guard), a canonical
tool allowlist, and a write-scope policy. Monitor and Security Guard are
read-only. The registry is a capability ceiling, not an authorization grant:
tenant/resource policy, approvals, budgets, deadlines, and cancellation state
must still be evaluated by the Policy Engine and control plane.

## Enforcement points

1. The gRPC GetTask path validates the assigned task against the authenticated
   mTLS/SPIFFE principal before returning its contract to a specialist.
2. ClaimTask performs the same preflight before claiming.
3. The PostgreSQL control store repeats the capability check inside the task
   row-lock transaction. This closes the gap between preflight and lease
   creation; a task that requests an unregistered tool cannot obtain a lease.
4. Code write scopes are task-specific and deny protected paths such as CI,
   migrations, schemas, deployment/infra, dependency manifests, secrets, and
   private-key material. The workspace gateway remains a second enforcement
   layer.
5. Network access is denied to every specialist by this baseline. Approved
   repository publication or broker access remains a separate control-plane
   operation, not arbitrary network access from the agent sandbox.
6. Production Deploy requires a deploy approval scope and an approval
   reference. No agent identity receives merge, protected-branch, secret
   mutation, or production-approval authority.

## Validation

The new unit tests cover all eight stage identities, cross-cutting Security
Guard, principal spoofing, tool escalation, protected Code paths, stage write
scopes, read-only Monitor, network denial, and production deployment approval.
The gRPC tests prove a capability-escalated task is rejected before lease
creation. The PostgreSQL integration test repeats that assertion inside the
atomic claim transaction and verifies the lease epoch remains zero.

## Phase 4B implementation

- Plan Agent returns schema-validated JSON for goal, scope, requirements,
  dependency DAG, acceptance criteria, risks, and approval scopes.
- The Plan Agent persists plan.md, requirements.md, backlog.yaml,
  acceptance-criteria.md, and risk-assessment.md as tenant-scoped immutable
  content-addressed artifacts. A separate evidence object maps artifact names
  to refs and records their digests, tenant, workflow, task, and policy version.
- If policy requires approval or the plan identifies an approval scope,
  the Plan Agent persists a pending approval request, returns needs_approval,
  and emits no Code handoff. A pending request is never treated as approval.
- Code independently rejects both tasks marked approval_required and Plan
  evidence containing pending approval scopes or a pending approval reference.
- Code tasks must carry the Plan Agent evidence ref plus plan and acceptance
  criteria refs. Code verifies the evidence belongs to the same tenant and
  workflow and that both document digests match before opening a worktree.
- Code Agent requires an accepted durable claim, the matching authenticated
  principal, repository.read, repository.branch.write, and a non-empty
  task-specific write scope.
- Code Agent uses a task/attempt/claim-scoped Git worktree. Its tool gateway
  intersects the agent registry, task capabilities, and the actual file path
  for every read/write action. The existing workspace gateway remains a
  second protection layer.
- Code Agent publishes only its generated feature branch and a draft pull
  request. It cannot merge, deploy, modify CI, migrations, schemas,
  dependencies, deployment/infra files, or credential material.
- The source commit, changed-file manifest, draft PR, and immutable evidence
  are bound to the Code result. Ambiguous push/PR failures report the side
  effect as UNKNOWN or APPLIED rather than silently inviting a duplicate retry.
- Executors accept a lease checkpoint callback so the worker host can enforce
  heartbeat renewal and cancellation during model/tool execution. The worker
  host remains responsible for running the heartbeat loop.

## Phase 4C — Build and independent QA

The Build and Test specialists are separate claim-bound executors. Both
require an authenticated workload principal, an accepted durable claim, a
live lease/cancellation checkpoint, and the capability/write scopes in the
agent registry.

### Build Agent

- Accepts only a content-addressed build profile with an allowlisted recipe,
  approved target platforms, network access disabled, and a two-clean-build
  reproducibility requirement.
- Delegates execution to an injected isolated BuildKit runner. The task never
  supplies shell commands, Docker arguments, credentials, or registry hosts.
- Requires both clean builds to produce the same SHA-256 OCI digest and
  validates the exact registry host against a host-side allowlist.
- Validates SPDX or CycloneDX SBOM JSON, stores it immutably, and signs both
  the SBOM attestation and build provenance using a Build Agent workload key.
- Emits a Build-to-Test handoff only after artifact, SBOM, and provenance
  evidence have been stored and signed.

### Independent Test Agent

- Requires a separate Test Agent identity and a signed Build evidence ref.
- Verifies the Build and SBOM detached signatures against trusted workload
  keys and checks tenant, workflow, source commit, artifact digest, SBOM
  digest, and approved OCI registry before invoking the test runner.
- Accepts only named suites from an offline test plan; no task-provided shell
  command or arbitrary network access is accepted.
- Runs the exact OCI artifact and rejects runner output that reports a
  different source commit or artifact digest.
- Signs the immutable test report. Required skipped suites and any failed
  suite produce a failed QA result with no Release handoff.
- A passing result is bound to the exact source commit, artifact digest,
  Build evidence, SBOM evidence, test plan, and acceptance criteria.

### Execution adapter boundary

The BuildKit and test runner are injected host adapters, not shell commands
executed by the agent model. Unit tests use deterministic fakes and verify
authorization, reproducibility, evidence signatures, tenant isolation,
commit/digest binding, and QA independence. A production rollout still needs
the actual isolated builder/test services, workload key provisioning and
rotation, durable worker heartbeat integration, and Release Agent verification.

## Phase 4D — Release candidate preparation

The Release Agent is a separate workload identity and cannot publish an
official release or deploy an environment. It verifies detached-signed Build,
SBOM, and independent Test evidence before preparing an immutable candidate.

- Verifies Build and SBOM signatures against trusted Build Agent keys.
- Verifies Test Agent signature and requires a passing test report for the
  exact source commit and OCI artifact digest.
- Confirms tenant/workflow identity, SBOM checksum, reproducible-build
  attestation, and host-configured required test suites.
- Requires a host-injected approval verifier whenever the task policy marks
  release approval as mandatory.
- Checks changelog and migration notes, and can optionally verify signed
  Security Guard findings against host-configured severity thresholds.
- Writes a content-addressed release-candidate manifest, a readiness report,
  and a detached-signed release attestation.
- Emits a Release-to-Deploy handoff only after all gates pass. Official release
  publication remains a separately approved repository action.

The Release Agent does not invoke a deployment adapter. Staging deployment,
health checks, smoke tests, and rollback remain the next increment.

## Phase 4E — staging-only Deploy Agent

The Deploy Agent is a separate workload identity. It does not publish an
official release and it rejects production targets in this increment.

- Verifies the detached-signed Release attestation and immutable release
  candidate manifest against tenant, workflow, exact source commit, artifact
  digest, release version, and approved OCI registry host.
- Accepts only a host-configured staging target ID and a service in that
  target's service allowlist. Task input contains no endpoint, shell command,
  credential, or arbitrary network destination.
- Requires a host approval verifier when the task or host policy requires
  staging approval. Approval is bound to tenant, workflow, deploy stage,
  source commit, artifact digest, expiry, and a human approver identity.
- Loads a content-addressed rollback plan and smoke-test plan. Both schemas
  allow only named checks and a previous immutable digest; no user-supplied
  commands or URLs are accepted.
- Inspects the active staging digest and requires it to match the rollback
  plan before deployment. A host adapter rehearses rollback to that exact
  digest before applying the candidate.
- Requires the deployment adapter to confirm the exact target, service,
  candidate digest, and previous digest. Health and smoke results must observe
  the candidate digest and return exactly the requested named checks.
- Stores an immutable deployment record and a detached-signed Deploy
  attestation. A passing result hands off to Operate Agent.
- Automatic rollback after failed verification is disabled by default.
  Enabling it is a host policy decision; the executor verifies the restored
  digest after rollback and records ambiguous outcomes as UNKNOWN.

### Execution adapter boundary

The deployment adapter is injected by the host and owns network access,
environment credentials, serialization, and secret-manager integration.
The agent model cannot invoke shell commands or inspect raw secrets. Tests
use deterministic fakes; they do not claim a live staging deployment.

## Phase 4F — Operate and Monitor execution adapters

The initial Operate and Monitor executors are implemented in
`rusa_agents/devops/operations.py`. They are host-adapter boundaries, not
claims that production runbook or telemetry services are already deployed.

### Operate Agent

- Accepts an immutable runbook invocation artifact whose exact reference is
  registered by the host. The artifact can select only a registered runbook,
  action ID, and schema-validated parameters.
- The host runbook registry binds each runbook to an explicit service
  allowlist, action allowlist, parameter schema, and execution-time ceiling.
- A mandatory Policy Engine adapter authorizes the tenant, workflow, task,
  service, runbook, action, and risk before the runner is called.
- Destructive actions require an approval scope configured by the runbook and
  a host approval verifier bound to the exact tenant, workflow, task, action,
  and service. Missing, pending, expired, or mismatched approval fails closed.
- A privileged host runner receives identifiers and structured parameters;
  it does not receive model-supplied shell commands, endpoints, or credentials.
- A confirmed runbook result is persisted as an immutable tenant-scoped
  incident/recovery report. Ambiguous execution or evidence-persistence
  failures report UNKNOWN/APPLIED side effects for reconciliation.

### Monitor Agent

- Uses a host-injected read-only telemetry adapter for a bounded service,
  environment, and time-window allowlist.
- Evaluates host-owned SLO rules deterministically; task input cannot change
  thresholds or severity.
- Requires immutable telemetry evidence references. It returns counts and
  SLO findings, not raw log/trace content.
- Uses a stable alert fingerprint bound to tenant, service, rule, window, and
  observation timestamp so duplicate delivery of the same observation can be
  correlated without suppressing later observations.
- Routes SLO breaches to Operate with `monitor.alert_triggered`; recurring
  trend feedback without an active SLO breach is handed to Plan with
  `monitor.feedback_created`.
- Monitor has no write scope, network authority, approval authority, or
  mutating adapter interface.

### Validation and remaining boundary

Unit tests cover runbook allowlists, policy denial, destructive-action
approval, ambiguous side-effect reporting, SLO evaluation, evidence
requirements, feedback routing, and read-only enforcement. These tests use
deterministic fake host adapters; they do not prove live service operations.

Still required before claiming full Phase 4 production completion:

- Production runbook adapter and allowlist provisioning.
- Production metrics/logs/traces/alert adapter and evidence export.
- Security Guard evidence production and its release/operations policy wiring.
- Worker-pool deployment, heartbeat renewal, durable child-task handoffs, and
  end-to-end staging-to-operate-to-monitor execution.

## Phase 5 — closed-loop operations

The first specialist feedback handoff is implemented, but Phase 5 is not
complete until the durable workflow host persists and correlates these
handoffs. Remaining work includes durable alert deduplication/correlation,
incident lifecycle projections, SLO dashboards and routing, workflow/model/tool
budget enforcement, kill-switch controls, backup/restore rehearsal, and
security/SRE/product-owner sign-off.

## Phase 6 — scale and enterprise readiness

Phase 6 remains open: organization-specific policy and budgets, workload
isolation, high availability, disaster recovery, regional deployment,
enterprise audit retention, and measurable SLA/SLO reporting.

The Phase 4 exit criterion remains a staging workflow that produces a signed
release candidate whose tests and provenance match the exact source revision,
followed by an approved and evidence-backed operations handoff.

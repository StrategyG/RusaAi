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

## Explicitly not completed by Phase 4B

- Build Agent reproducible build execution and SBOM generation.
- Independent Test Agent execution and commit/digest-bound QA evidence.
- Cryptographically signed build, SBOM, test, and release evidence.
- Release-candidate publication and staging deployment.
- Deploy, Operate, Monitor, and Security Guard stage handlers.
- Production worker-pool provisioning and orchestration of live child tasks.

## Next increments

- **4C — Build and independent QA:** reproducible build output, SBOM, test
  reports, and immutable commit/digest-bound evidence. QA must be an
  independent Test Agent identity and cannot PASS without exact evidence.
- **4D — Release and staging:** verify Build and QA evidence, produce a signed
  release candidate, and deploy only to an approved staging target.
- **4E — remaining specialists:** integrate Deploy, Operate, Monitor, and
  Security Guard against allowlisted services and telemetry.

The Phase 4 exit criterion remains a staging workflow that produces a signed
release candidate whose tests and provenance match the exact source revision.

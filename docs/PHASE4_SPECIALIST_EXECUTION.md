# Phase 4A — specialist execution authority foundation

## Scope

This increment establishes the execution-time authority boundary for the
eight lifecycle specialists and the cross-cutting Security Guard. It is a
foundation for Phase 4, not a claim that the eight agent implementations or
worker fleet are complete.

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

## Explicitly not completed by Phase 4A

- Stage-specific Plan, Code, Build, Test, Release, Deploy, Operate, and Monitor
  execution handlers.
- Artifact storage and signed build/SBOM/test evidence.
- Independent QA verification that evidence matches the exact source commit
  and artifact digest.
- Release-candidate publication and staging deployment.
- Production worker-pool provisioning.

## Next increments

- **4B — Plan and Code execution:** structured Plan output and isolated
  task-scoped Git workspaces for Code, with branch-only publication.
- **4C — Build and independent QA:** reproducible build output, SBOM, test
  reports, and immutable commit/digest-bound evidence.
- **4D — Release and staging:** verify Build and QA evidence, produce a signed
  release candidate, and deploy only to an approved staging target.
- **4E — remaining specialists:** integrate Deploy, Operate, Monitor, and
  Security Guard against allowlisted services and telemetry.

The Phase 4 exit criterion remains a staging workflow that produces a signed
release candidate whose tests and provenance match the exact source revision.

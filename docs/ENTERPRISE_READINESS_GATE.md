# ERG-06 — Enterprise Readiness Gate

**Program:** RUSA-AI  
**Next roadmap phase:** Phase 6 — Scale and Enterprise  
**Gate type:** Mandatory promotion gate  
**Initial status:** HOLD — staging evidence and independent sign-off are not yet available

## 1. Purpose and decision rule

ERG-06 is the mandatory evidence gate between the existing Phase 1–5 roadmap and Phase 6. A green GitHub Actions run proves only the checks executed on that exact commit. It does not certify the deployed service, tenant boundary, disaster recovery, or operational readiness.

**NO PASS = NO GO.** Phase 6 must not start until the certification evaluator returns PASS for the exact candidate commit and the required control owners have approved the evidence.

The repository currently has a code-level PostgreSQL suite for tenant-scoped API/RLS behavior, memory access, audit immutability, queue leases, retry, and worker concurrency. The queue worker role is intentionally able to consume queue rows across tenants; its memory access is tenant-scoped. This is a trusted control-plane boundary, not proof that a compromised shared worker process is isolated from every other tenant. Strong isolation against a compromised worker requires a separately verified tenant-bound worker identity or credential-broker design.

## 2. Six control pillars

| ID | Control pillar | Required evidence | Accountable owner |
|---|---|---|---|
| ERG-01 | Multi-tenant isolation | Negative tests for unauthorized cross-tenant reads/writes, identity and credential-scope review, no unauthorized access; explicitly allow only documented control-plane queue consumption | Security |
| ERG-02 | Failure recovery and resilience | At least 100 recoverable-fault simulations, >=95% recovery, no duplicate external effects, consistent durable state | Platform / SRE |
| ERG-03 | Backup, restore and disaster recovery | Restore drill with RPO <=5 minutes, RTO <=60 minutes, no confirmed loss of acknowledged audit events | SRE |
| ERG-04 | Policy enforcement and audit integrity | 100% sensitive-action policy/audit reconciliation, zero critical false-allow, durable intent and outcome/receipt | Security / Governance |
| ERG-05 | Scale and operational resilience | 2x projected peak load for 60 minutes, service SLOs met, backpressure and tenant quotas verified | Platform / FinOps |
| ERG-06 | Independent QA and release approval | Independent review, exact candidate digest, complete evidence provenance, all required owner sign-offs | QA / Release owner |

## 3. Automated CI sub-gate

The separate .github/workflows/enterprise-readiness.yml workflow runs the required suites against its own PostgreSQL 16 service, with isolated databases for backend, RLS, and recovery tests to prevent cross-suite data contamination. It emits separate JUnit reports plus a machine-readable ERG-06 report:

- tests/test_postgres_rls.py
- tests/test_postgres_backends.py
- tests/test_queue_races.py
- tests/test_enterprise_readiness_gate.py

Each PostgreSQL suite must execute at least five tests, with zero skipped tests, failures, or errors. The existing main CI workflow remains a separate required check; configure both the baseline CI checks and ERG-06 automated check in branch protection.

The automated check has two separate outcomes:

- **Automation status PASS:** the required code-level tenant/RLS and queue-recovery tests ran and passed on the candidate commit.
- **Enterprise readiness status HOLD:** the staging, disaster-recovery, load, identity-isolation, and independent approval evidence is still outstanding.

This separation is intentional. A successful CI job must never be presented as enterprise certification.

## 4. Certification evidence contract

A staging acceptance run must produce a JSON document conforming to:

schemas/enterprise-readiness/erg06-evidence.schema.json

The evidence must be bound to the exact 40-character candidate commit SHA and include:

1. Staging environment identity and workflow run ID.
2. Verified provenance/attestation reference.
3. Tenant-isolation negative-test counts and evidence reference.
4. Recovery simulation counts, recovered count, duplicate-effect count, and state-integrity result.
5. Measured RPO/RTO and acknowledged audit-loss count.
6. Kill-switch propagation p99 and internal cancellation p95.
7. Load multiplier, duration, and SLO result.
8. Sensitive-action audit reconciliation and critical false-allow count.
9. Independent QA verdict and evidence reference.
10. Platform, Security, SRE, QA, Data Governance, FinOps, and Process Owner sign-offs.

Do not include credentials, API keys, raw tenant data, personal data, or unredacted customer payloads in the evidence document. Use restricted artifact references for detailed traces and reports.

## 5. Certification evaluator

Install the project dependencies, then evaluate the staging evidence against the exact candidate commit:

    python scripts/enterprise_readiness_gate.py \
      --mode certify \
      --evidence /secure/path/erg06-staging-evidence.json \
      --expected-sha "$(git rev-parse HEAD)" \
      --expected-run-id <staging-workflow-run-id> \
      --output artifacts/erg06-certification-report.json

The command is an offline evaluator only; it cannot establish GitHub attestation validity by itself. Formal certification must run through the workflow_dispatch path, which verifies the source workflow run, candidate SHA, artifact attestation signer, and source digest before invoking the evaluator.

The evaluator returns:

| Exit code | Status | Meaning |
|---:|---|---|
| 0 | PASS | All thresholds, provenance checks, and sign-offs passed |
| 1 | FAIL | A critical violation or measured threshold failure exists |
| 2 | HOLD | Evidence is missing, incomplete, unverified, or targets another environment |

Thresholds enforced by the evaluator:

| Metric | Acceptance threshold |
|---|---:|
| Unauthorized cross-tenant reads/writes/accesses | 0 |
| Recoverable fault simulations | >=100 |
| Recovery success | >=95% |
| Duplicate business effects | 0 |
| State-integrity failures | 0 |
| RPO | <=300 seconds |
| RTO | <=3,600 seconds |
| Confirmed audit loss | 0 |
| Kill-switch propagation | p99 <=5 seconds |
| Internal cancellation | p95 <=30 seconds |
| Load | >=2x peak for >=60 minutes |
| Sensitive actions with complete audit evidence | 100% |
| Critical false-allow decisions | 0 |
| Independent QA critical findings | 0 |

## 6. GitHub enforcement and release boundary

The check named **ERG-06 automated tenant-isolation and recovery gate** should be configured as a required status check for pull requests targeting main. Repository branch protection/rulesets and required environment reviewers are owner-controlled settings and must be configured separately; adding a workflow does not enable branch protection automatically.

The workflow_dispatch certification path requires the exact candidate SHA and the run ID of a successful workflow named RUSA-AI Staging Acceptance. It downloads the erg06-staging-evidence artifact, verifies the source run SHA/workflow/conclusion, verifies the GitHub artifact attestation against the expected signer workflow at .github/workflows/staging-acceptance.yml, and evaluates the JSON evidence. Configure the enterprise-readiness GitHub Environment with required reviewers before using this path.

The full certification result must be reviewed against the evidence artifact and the exact candidate SHA before Phase 6 promotion. The current GitHub integration does not change branch protection, repository secrets, environment protection rules, database roles, or production infrastructure.

## 7. Known limitations and outstanding blockers

- CI PostgreSQL tests exercise the repository's test database, not a deployed staging environment.
- Existing worker queue access is intentionally cross-tenant for queue consumption. Do not claim isolation against a fully compromised shared worker until tenant-bound worker identities or an equivalent credential broker are deployed and adversarially tested.
- A passing unit/integration suite does not prove RPO/RTO; a real restore drill is mandatory.
- Kill-switch latency, external-side-effect reconciliation, 2x peak load, 60-minute soak, alerting, rollback, and on-call readiness require deployment-level evidence.
- PRs that are still open and unmerged are not part of main. Re-evaluate their exact head commits and required CI before relying on their changes.
- PR #61 is a separate candidate for HMAC-bound sandbox QA evidence. Its signer and verifier remain inside the same trusted worker runtime, so it does not establish an independent QA trust boundary and must not be counted as independent QA approval. It is not merged into main.

## 8. Phase 6 entry decision

Phase 6 — Scale and Enterprise is authorized only when:

- ERG-06 automated CI check is green on the exact candidate commit.
- Certification evaluator returns PASS.
- No critical security, tenant-isolation, integrity, or recovery finding remains open.
- All seven accountable owner sign-offs are present.
- The release owner records the final GO decision.

Until then, the official status remains **HOLD / NO GO**.

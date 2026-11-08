# RUSA-AI Production Certification Contract

**Contract version:** 1.0  
**Initial implementation:** PR #71  
**Decision model:** evidence-bound `PASS`, `HOLD`, or `FAIL`

## 1. Purpose and lifecycle alignment

This contract provides a versioned, machine-readable boundary between a
release candidate and a production-promotion decision. It binds the exact
source commit, immutable OCI image digests, signatures, provenance, SBOMs,
vulnerability evidence, staging control evidence, and accountable human
sign-offs.

This work does **not** renumber the Agentic DevOps Lifecycle. In
`RUSA_AI_AGENTIC_DEVOPS_LIFECYCLE.md`, Phase 4 remains Operations and
Monitoring Agents, Phase 5 remains Closed-loop Agentic DevOps, and Phase 6
remains Scale and Enterprise. Production certification is a cross-stage gate
spanning Build, Test, Release, Deploy, Operate, Monitor, and Security Guard.
It also contributes evidence to the ERG-06 gate before Phase 6 promotion.

## 2. Contract files

- `schemas/certification/release-manifest.schema.json` defines a release
  manifest for the API, worker, and sandbox OCI images.
- `schemas/certification/production-certification.schema.json` defines the
  staging evidence references and seven accountable owner sign-offs required
  for a production-promotion decision.
- `rusa_agents/certification/contracts.py` validates both schemas and checks
  cross-document bindings and deterministic decision rules.

Both schemas use JSON Schema Draft 2020-12 and reject unknown fields.

## 3. Release manifest

The release manifest binds:

1. Repository, exact 40-character source commit, tag/ref, build workflow, run
   ID, and builder identity.
2. A signature record for the manifest itself.
3. API, worker, and sandbox images by immutable `sha256:` digest.
4. A signature and provenance attestation reference for every image.
5. An SPDX JSON or CycloneDX JSON SBOM reference and SHA-256 digest per image, bound to that image digest.
6. A vulnerability scan bound to the exact image digest, including scanner
   database time, severity counts, disposition, and any approved High finding
   exceptions.

The manifest requires API, worker, and sandbox artifacts under the repository-owned GHCR namespace. SBOM and vulnerability scan subjects must match the exact image digest. Scanner database timestamps cannot post-date the scan. Additional
`rag_mcp` artifacts may be recorded. Artifact names must be unique.

### Vulnerability disposition

- Any Critical finding blocks the release.
- High findings block unless every High finding has an individual, approved,
  unexpired exception with an owner, rationale, approval reference, approver,
  and approval timestamp.
- Medium and Low findings remain visible in the manifest for triage.
- A `BLOCKED` disposition is never certifiable.

The exception metadata is not self-authenticating. The release workflow must
verify the approval reference and approver identity using the trusted approval
system before it sets `approved: true` in the manifest.

## 4. Production certification record

The certification record references, rather than copies, detailed evidence.
Every check has:

- `status`: `PASS`, `HOLD`, or `FAIL`;
- `evidence_ref` and `evidence_digest`;
- the exact `subject_commit_sha`;
- `observed_at` timestamp.

The required checks are release integrity, supply chain, vulnerability,
tenant isolation, penetration test, backup/restore, disaster recovery,
rollback, observability, SLO, staging soak, independent QA, and ERG-06.

The record must bind a staging evidence environment to a production target
and the immutable digest of the release manifest. The seven required sign-offs
are platform, security, SRE, QA, data governance, FinOps, and process owner.

## 5. Deterministic decision rules

`evaluate_production_certification` returns:

| Status | Rule |
|---|---|
| `PASS` | All 13 required checks are PASS, every check targets the exact candidate commit, evidence attestation is verified, manifest digest matches, and all seven sign-offs are approved. |
| `HOLD` | Evidence is missing or not verified, a check is on HOLD, a required owner has not approved, or a sign-off timestamp is invalid/future-dated. |
| `FAIL` | Candidate commit or manifest digest differs, any check targets another commit, or any mandatory check reports FAIL. |

A FAIL takes precedence over HOLD. No status is inferred from a green CI run alone.

## 6. Validation API

```python
from rusa_agents.certification import (
    evaluate_production_certification,
    validate_production_certification,
    validate_release_manifest,
)

validate_release_manifest(
    manifest,
    expected_repository="StrategyG/RusaAi",
    expected_commit_sha=candidate_sha,
)

validate_production_certification(certification_record)

result = evaluate_production_certification(
    certification_record,
    expected_commit_sha=candidate_sha,
    expected_release_manifest_digest=manifest_digest,
    expected_repository="StrategyG/RusaAi",
)
```

The evaluator is deterministic when an explicit timezone-aware `now` value
is supplied. It returns a report with status and blockers; it does not deploy.

## 7. Trust boundary and limitations

Schema validation is not cryptographic verification. The Python validator
checks that verification claims and evidence references are present and
consistent; it does not verify Cosign signatures, GitHub artifact attestations,
SBOM authenticity, scanner output signatures, penetration-test reports, backup
restore results, or human identities. A trusted CI/release workflow must
verify those items and bind the resulting evidence to the exact candidate
commit and manifest digest before invoking this evaluator.

The contract does not run a penetration test, perform a backup/restore drill,
measure RPO/RTO, run a staging soak test, or configure GitHub branch protection
or environment reviewers. Those are operational evidence and repository-owner
controls, not schema operations.

The manifest schema is a new JSON contract. The currently proposed release
workflow in PR #53 emits a text `release-manifest.txt`; a follow-up integration
must emit this JSON contract and verify/sign the manifest itself. The ERG-06
schema and evaluator proposed in PR #60 remain the detailed staging evidence
and threshold gate; this contract does not replace them. PRs #53, #54, and #60
were open when this contract was authored, so their code must not be treated as
part of `main` until merged and revalidated.

## 8. Tests

Run the focused tests:

```bash
python -m pytest -q tests/test_production_certification.py
ruff check rusa_agents/certification tests/test_production_certification.py
```

The tests cover schema validity, exact source/provenance binding, immutable
image digest binding, signature assertions, vulnerability policy and exception
expiry, candidate/manifest mismatch, mandatory check failures, attestation HOLD,
and missing human sign-off.

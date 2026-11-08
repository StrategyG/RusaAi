# RUSA-AI Production Artifact Verification

## Image signature verification

The certification package exposes `verify_cosign_image_signature` as a fail-closed
verification primitive. The caller must supply the immutable OCI repository
reference and SHA-256 manifest digest, plus the exact certificate identity and
OIDC issuer approved by the repository's release policy.

Example:

```python
from rusa_agents.certification import verify_cosign_image_signature

evidence = verify_cosign_image_signature(
    image="ghcr.io/strategyg/rusa-ai-api",
    digest="sha256:<64 lowercase hexadecimal characters>",
    certificate_identity=(
        "https://github.com/StrategyG/RusaAi/.github/workflows/release.yml"
        "@refs/tags/v1.0.0"
    ),
    oidc_issuer="https://token.actions.githubusercontent.com",
)
```

The function invokes the Cosign CLI without a shell, enforces the requested
certificate identity and OIDC issuer, applies a bounded timeout, parses the
verification output, and confirms that the verified subject digest exactly
matches the requested digest. Any missing tool, timeout, non-zero exit, invalid
output, or digest mismatch raises `ArtifactVerificationError`.

## Important trust boundary

This helper does not publish images, create signatures, create attestations,
authenticate reviewers, or deploy software. It must run in a trusted workflow
with an organization-approved signing identity. Do not set a manifest's
`signature.verified` field based on user input or an untrusted job output;
populate it only from the successful result of this verifier and preserve the
returned identity, issuer, and subject digest in the certification evidence.

The current RUSA-AI CI builds production images locally and scans them with
Trivy. Until a trusted registry publication/signing workflow is configured,
there is no live GHCR signature to verify and release certification must remain
HOLD for supply-chain signature evidence.

## Release integration checklist

- Publish images to the approved registry using immutable digest references.
- Sign each image digest with the approved GitHub Actions OIDC identity.
- Verify each signature with the exact expected certificate identity and issuer.
- Verify SLSA provenance independently and bind its source repository, commit,
  workflow identity, and subject digest to the release manifest.
- Generate the SBOM for the same image digest and verify its digest.
- Persist verifier output as immutable evidence and bind its digest into the
  release manifest.
- Keep promotion blocked when any verification is missing or fails.


## GitHub SLSA provenance verification

The certification package also exposes `verify_github_slsa_provenance`. It
uses `gh attestation verify` against the immutable OCI image digest and
requires the expected GitHub repository, signer workflow, certificate
identity, OIDC issuer, and SLSA v1 predicate type. GitHub CLI performs the
cryptographic attestation verification; the wrapper additionally confirms
that the verified statement contains the exact requested SHA-256 subject.

Example:

```python
from rusa_agents.certification import verify_github_slsa_provenance

provenance = verify_github_slsa_provenance(
    image="ghcr.io/strategyg/rusa-ai-api",
    digest="sha256:<64 lowercase hexadecimal characters>",
    repository="StrategyG/RusaAi",
    signer_workflow="StrategyG/RusaAi/.github/workflows/release.yml",
    source_digest="<40-character source commit SHA>",
    certificate_identity=(
        "https://github.com/StrategyG/RusaAi/.github/workflows/release.yml"
        "@refs/tags/v1.0.0"
    ),
)
```

The verifier also requires `--source-digest` to match the expected Git commit SHA, binding the attestation to the precise source revision. The helper does not treat arbitrary predicate fields as trusted identity.
GitHub's certificate identity and verified timestamp are the cryptographically
verified identity evidence. Provenance still requires a trusted builder
workflow whose execution cannot be influenced by untrusted caller input.

# RUSA-AI DevOps and CI/CD operating standard

## Pipeline gates

Every push and pull request runs the reusable CI workflow:

1. **Quality:** Ruff lint, Python 3.13 compilation, deployment/rollback shell tests, the full pytest suite against the digest-pinned PostgreSQL 16.15 image (Alpine 3.24), and pip-audit for the complete development dependency set.
2. **Container security:** Compose interpolation validation, production API and worker image builds, Trivy image scans, and CycloneDX SBOM generation for both images.
3. **Release Gate:** a single aggregate check that fails unless both mandatory jobs succeed. Configure this exact check as required on protected branches.

Trivy blocks every CRITICAL finding and every HIGH finding except the exact CVEs listed in the root .trivyignore file. Each exception has a mitigation, owner, and expiry in docs/SECURITY_EXCEPTIONS.md; scripts/security/validate_trivy_exceptions.py fails CI when an exception expires or the ignore list and register diverge. Unfiltered JSON reports for both application images and PostgreSQL are retained as 30-day workflow artifacts. New CVEs are not automatically ignored.

The CI workflow validates both the build-enabled development Compose file and the build-free production Compose manifest. It is reusable by the release workflow. A semantic-version tag is not published unless the complete CI workflow succeeds again on the exact tagged commit.

## Release and image supply chain

Push an annotated version tag after the commit has passed the required branch checks:

```bash
git tag -a v1.2.3 -m "RUSA-AI v1.2.3"
git push origin v1.2.3
```

Only `vMAJOR.MINOR.PATCH` tags are accepted. The release workflow:

- builds the API and agent runtime images;
- publishes immutable SHA and semantic-version tags to GHCR;
- rescans the published images;
- creates CycloneDX SBOMs and attaches them to the GitHub Release;
- signs image digests with Cosign using GitHub OIDC keyless signing;
- publishes SLSA build-provenance attestations.

Deployments use image digests, not mutable tags such as `latest`.

## Production deployment prerequisites

Create a GitHub Environment named `production` and require approval from at least one designated production reviewer. Configure the following environment variables:

| Name | Purpose |
|---|---|
| `PRODUCTION_SSH_HOST` | DNS name or IPv4 address of the production host |
| `PRODUCTION_SSH_USER` | Least-privilege deployment account |
| `PRODUCTION_DEPLOY_PATH` | Absolute Compose project path, e.g. `/srv/rusa-ai` |
| `GHCR_PULL_USERNAME` | Registry username, required only when using a pull token |

Configure these environment secrets:

| Name | Purpose |
|---|---|
| `PRODUCTION_SSH_PRIVATE_KEY` | Dedicated deployment SSH private key |
| `PRODUCTION_SSH_KNOWN_HOSTS` | Pinned host key entry; do not generate trust dynamically in CI |
| `GHCR_PULL_TOKEN` | Optional read:packages token if GHCR images are private |

The production host must have Docker Engine, Docker Compose v2, `curl`, `flock`, network access to GHCR, and a pre-provisioned `.env` file. Runtime images use Google Distroless Python 3.13 on Debian 13, with dependencies built in a matching Python 3.13/Trixie builder stage. The final runtime excludes shells, package managers, and build-only tooling. The model directory referenced by `RUSA_MODEL_DIR` must exist and be mounted read-only. Keep database volumes persistent and place the API behind a TLS reverse proxy or gateway.

The production `.env` file must contain all runtime secrets and configuration from `.env.example`. Before the first automated rollout, seed `RUSA_API_IMAGE` and `RUSA_AGENT_IMAGE` with the immutable GHCR digests of the currently running, known-good images. This baseline is required so the first automated deployment has a real rollback target. Never upload the production `.env` file to GitHub or commit it to the repository.

## Deployment and rollback

The release workflow copies `docker-compose.production.yml` as the host's `docker-compose.yml`, plus the required initialization/deployment scripts. The production manifest has no local build contexts and requires immutable GHCR image digests, so the host does not need a source checkout or Dockerfiles. Runtime secrets remain on the host.

`scripts/deploy/compose-deploy.sh`:

- serializes deployments with a file lock;
- snapshots the current `.env` with restrictive permissions;
- updates API and worker image references to the release digests;
- validates Compose configuration, pulls images, and updates services;
- waits for container health, verifies model readiness at `/health/ready`, and verifies PostgreSQL-backed agent API readiness at `/health/ready`;
- automatically restores the previous configuration and restarts the previous images if any deployment or health-check step fails.

Manual rollback:

```bash
cd /srv/rusa-ai
./scripts/deploy/compose-rollback.sh
```

The previous environment snapshot is retained as `.env.previous`. A rollback failure is a critical incident and requires operator intervention; the script exits non-zero rather than reporting success.

## Required GitHub branch rules

The repository currently has no active rulesets and the observed branches are not protected. This must be configured in **GitHub → Settings → Rules → Rulesets**; repository write access through the current integration does not expose the ruleset mutation API.

Create an active branch ruleset targeting `main` and `release/**` with:

- Require a pull request before merging.
- Require at least one approval; dismiss stale approvals after new commits.
- Require branches to be up to date before merging.
- Require the **Release Gate** status check.
- Block force pushes and branch deletion.
- Do not grant routine bypass permissions, including administrator bypass.
- Restrict direct pushes to the release branch to the controlled release process.

Also protect the `v*` tag namespace so only authorized release maintainers can create or update release tags. Confirm that the `Release Gate` check appears in the required-check selector after the first successful workflow run.

## Verification boundary

The repository changes provide the pipeline and deployment mechanism. They do not prove that production infrastructure is configured. A 5/5 sign-off still requires: active branch rulesets, configured production environment approval, working SSH/registry credentials, a known-good rollback baseline, a successful tagged release, a successful staging/production smoke test, and an observed rollback drill. Do not mark deployment complete until those external controls have evidence.

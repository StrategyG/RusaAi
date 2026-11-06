# RUSA-ARCH-001: foundation evidence

Date: 2026-11-06. Repository: StrategyG/RusaAi.
Baseline commit: fc27f227f7ab5d43135f9bb2d074c208bfbb956d.
Branch: fix/agent-foundation-validation.

## Acceptance criteria and scope

Make the current API reject unauthenticated requests by default; reject unavailable
schema validation instead of reporting success; validate declared schema formats;
never report simulated inference as real output; record an implementable target
architecture and accurate remaining production gates. No deployment or merge.

## Baseline

`python -m pytest -q`: 2 failed, 2 passed. Health response differed from its test;
the purported valid envelope fixture did not match the actual schema. Inspection
also found optional authentication, schema-missing success, wrong schema draft,
unchecked formats, mock inference success, and lint errors ignored by CI.
README listed absent files and capabilities not present in the repository.

## Changes

- `llm-project/serve/app.py`: fail-closed shared-key authentication for both POST
  endpoints; timing-safe comparison; schema dialect selection and format checking;
  value-free bounded diagnostics; strict chat fields; separate liveness/readiness;
  unavailable inference returns 503.
- `tests/test_app.py`: contract-correct fixtures and failure-path regression tests.
- `requirements.txt`: install JSON Schema format validation dependencies.
- `.github/workflows/ci.yml`: remove suppression of lint failures.
- `.env.example`, `.gitignore`: document required configuration and exclude local
  credentials/cache files.
- `README.md`: accurate commands, endpoint behavior and capability limitations.
- `docs/architecture/hierarchical-agent.md`: hierarchy, directory map, communication,
  durable orchestration, memory, tools, evaluation and staged acceptance criteria.

## Local verification

Environment: Python 3.12.14, FastAPI 0.141.1, Pydantic 2.13.4, jsonschema 4.26.0,
pytest 9.2.0, httpx 0.28.1. Existing environment used; not a locked clean install.

- `python -m pytest -q`: 22 passed, 2 dependency deprecation warnings, 0.44 seconds.
- `python -m flake8 . --select=E9,F63,F7,F82 --show-source --statistics`: exit 0.
- `python -m py_compile llm-project/serve/app.py tests/test_app.py`: exit 0.
- `git diff --check`: exit 0.

The warnings concern anyio's pytest integration and Starlette's httpx test client.
Docker is unavailable locally, so no local container-build success is claimed.
Remote CI must be checked on the PR; local results do not substitute for CI.

## Remaining release blockers

No real inference backend/weights; no agent scheduler or tools runtime; no tenant
identity/authorization; no durable audit or memory isolation; no request body/rate
limits; dependencies not locked/audited; no recovery/load/backup/restore evidence.
The shared API key protects access but is not enterprise identity management.
Schema validation does not enforce caller-provided policy. Production deployment
requires the repository's explicit approval. No readiness score is assigned.

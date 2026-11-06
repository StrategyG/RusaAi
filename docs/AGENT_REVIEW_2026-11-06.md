# Agent runtime review — 2026-11-06

Task: RUSA-REVIEW-20261106. Scope: StrategyG/RusaAi, PR #7 baseline
8c242a214f8e5ed9de9f85672c9e080954b75436. Acceptance: reproduce defects,
fix them without weakening security gates, and run regression checks.

## Verified findings and changes

- High: environment-file variants such as `.env.production` and nested
  `config/.env.local` passed the repository path policy. Deny `.env` and
  `.env.*` components at every depth, including case variants; reject case
  variants of `.git` as well. Gateway tests prove rejection before network access.
- High: GitHub check-runs reads retrieve only 100 records. Previously a successful
  first page could be reported as all checks successful despite omitted checks.
  Require an integer total_count matching the returned result count; the
  orchestrator waits on incomplete evidence. This intentionally fails closed;
  repositories with more than 100 checks need bounded pagination in a follow-up.
- Medium: the required container job name was singular, but CI publishes the
  plural name. Match the actual workflow and add a contract regression test.
- Test defect: the audit-integrity regression never reached the audit check
  because its tester supplied no CI evidence. Provide successful CI evidence
  so the test actually exercises rejection of invalid audit integrity.

## Evidence and limitations

Before fixes, focused runtime tests: 9 failed, 30 passed (including eight
new regression failures and the existing audit test failure).
After fixes: `python -m pytest -q`: 60 passed, 3 skipped.
`python -m ruff check llm-project/serve rusa_agents tests`: passed.
`python -m compileall -q llm-project rusa_agents tests`: passed.

Local runtime: Python 3.12. PostgreSQL tests were skipped because
RUSA_TEST_DATABASE_URL is not configured. Docker is not installed locally;
remote CI must validate PostgreSQL and container builds. Dependency security
scans, real model quality, load/soak tests, deployment and recovery drills were
not performed in this review. No production deployment or main-branch merge.

## Remaining release blockers

Main still contains the earlier mock-serving baseline; existing hardening and
agent PRs must be reconciled and reviewed before merge. This patch targets PR
#7's branch and does not certify the entire product as production-ready.
The agent API's request-size middleware currently checks Content-Length only;
actual streamed body limits still need enforcement. Provider/GitHub response
resource limits and CI evidence identity/freshness deserve further adversarial
review. Live infrastructure and model evaluation evidence remain required.

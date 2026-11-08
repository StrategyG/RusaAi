# Evaluation module

This directory is intended to support:

- benchmark execution
- safety validation
- red teaming
- regression suites
- quality measurement and reporting

**Status:** the initial agent SFT contract evaluator is implemented; broader benchmark, adversarial, and product-quality evaluation remain planned.

## Agent SFT evaluator

The initial evaluator runs the adapter on a held-out JSONL split and measures:

- JSON parse rate.
- Validity of the exact `AutonomousCodeWorker` response contract.
- Role-scoped tool allowlist and tool-argument safety.
- Recall/precision and false-positive rate for examples whose expected response is blocked.

These checks are necessary but not sufficient for production readiness. Add human-reviewed task success, regression, adversarial prompt-injection, tenant-isolation, and tool-side-effect evaluations before promotion. See `docs/FINETUNING_OPERATIONS.md`.

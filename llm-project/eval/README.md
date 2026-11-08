# RUSA-AI agent evaluation

The initial evaluator runs the adapter on a held-out JSONL split and measures:

- JSON parse rate.
- Validity of the exact `AutonomousCodeWorker` response contract.
- Role-scoped tool allowlist and tool-argument safety.
- Recall/precision for examples whose expected response is blocked.

These checks are necessary but not sufficient for production readiness. Add human-reviewed task success, regression, adversarial prompt-injection, tenant-isolation, and tool-side-effect evaluations before promotion. See `docs/FINETUNING_OPERATIONS.md`.

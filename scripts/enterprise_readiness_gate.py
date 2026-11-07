#!/usr/bin/env python3
"""Evaluate RUSA-AI ERG-06 automated and staging evidence.

The automated mode validates PostgreSQL tenant-isolation and queue-recovery
JUnit evidence. It deliberately reports overall enterprise readiness as HOLD:
CI cannot certify staging DR, real tenant identities, load, or operator sign-off.

The certify mode evaluates a complete staging evidence document against the
versioned JSON Schema and the ERG-06 acceptance thresholds.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas/enterprise-readiness/erg06-evidence.schema.json"

CONTRACT_SUITES = {
    "tests/test_enterprise_readiness_gate.py": 5,
}
TENANT_SUITES = {
    "tests/test_postgres_rls.py": 5,
    "tests/test_postgres_backends.py": 5,
}
RECOVERY_SUITES = {
    "tests/test_queue_races.py": 5,
}
REQUIRED_STAGING_EVIDENCE = [
    "staging adversarial tenant-isolation results using deployed tenant identities",
    "at least 100 recoverable-fault simulations with no duplicate business effects",
    "verified backup/restore drill meeting RPO <= 5 minutes and RTO <= 60 minutes",
    "kill-switch measurements meeting p99 <= 5 seconds and cancellation p95 <= 30 seconds",
    "2x peak-load test sustained for at least 60 minutes while meeting service SLOs",
    "complete sensitive-action policy/audit reconciliation",
    "independent QA and accountable owner sign-offs",
    "verified evidence provenance/attestation bound to the exact candidate commit",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _junit_cases(path: Path) -> list[ET.Element]:
    root = ET.parse(path).getroot()
    return list(root.iter("testcase"))


def _suite_stats(cases: list[ET.Element], suite_path: str) -> dict[str, int | str]:
    target = suite_path.replace("\\", "/").lower()
    selected: list[ET.Element] = []
    for case in cases:
        identity = " ".join(
            [case.get("file", ""), case.get("classname", ""), case.get("name", "")]
        ).replace("\\", "/").lower()
        if target in identity or Path(target).name in identity:
            selected.append(case)

    skipped = sum(case.find("skipped") is not None for case in selected)
    failures = sum(case.find("failure") is not None for case in selected)
    errors = sum(case.find("error") is not None for case in selected)
    return {
        "suite": suite_path,
        "tests": len(selected),
        "passed": len(selected) - skipped - failures - errors,
        "skipped": skipped,
        "failures": failures,
        "errors": errors,
    }


def _suite_status(stats: dict[str, int | str], minimum: int) -> tuple[str, list[str]]:
    blockers: list[str] = []
    if int(stats["failures"]) or int(stats["errors"]):
        blockers.append(
            f'{stats["suite"]}: {stats["failures"]} failure(s), {stats["errors"]} error(s)'
        )
        return "FAIL", blockers
    if int(stats["tests"]) < minimum:
        blockers.append(
            f'{stats["suite"]}: expected at least {minimum} executed tests, '
            f'found {stats["tests"]}'
        )
        return "HOLD", blockers
    if int(stats["skipped"]):
        blockers.append(
            f'{stats["suite"]}: {stats["skipped"]} test(s) skipped; PostgreSQL '
            "integration evidence is incomplete"
        )
        return "HOLD", blockers
    return "PASS", blockers


def evaluate_automated(
    junit_paths: str | Path | list[str | Path], expected_sha: str = ""
) -> tuple[dict[str, Any], int]:
    """Evaluate code-level JUnit evidence. Exit 0 means only this sub-gate passed."""
    paths = [Path(item) for item in junit_paths] if isinstance(junit_paths, list) else [Path(junit_paths)]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        report = {
            "schema_version": "1.0",
            "generated_at": utc_now(),
            "candidate_sha": expected_sha or None,
            "automation_status": "HOLD",
            "enterprise_readiness_status": "HOLD",
            "checks": {},
            "blockers": [f"JUnit evidence file not found: {path}" for path in missing],
            "required_staging_evidence": REQUIRED_STAGING_EVIDENCE,
        }
        return report, 2

    cases: list[ET.Element] = []
    try:
        for path in paths:
            cases.extend(_junit_cases(path))
    except (ET.ParseError, OSError) as exc:
        report = {
            "schema_version": "1.0",
            "generated_at": utc_now(),
            "candidate_sha": expected_sha or None,
            "automation_status": "HOLD",
            "enterprise_readiness_status": "HOLD",
            "checks": {},
            "blockers": [f"Unable to parse JUnit evidence: {exc}"],
            "required_staging_evidence": REQUIRED_STAGING_EVIDENCE,
        }
        return report, 2

    checks: dict[str, Any] = {}
    blockers: list[str] = []
    contract_statuses: list[str] = []
    statuses: list[str] = []

    contract_details: list[dict[str, Any]] = []
    for suite_path, minimum in CONTRACT_SUITES.items():
        stats = _suite_stats(cases, suite_path)
        status, reasons = _suite_status(stats, minimum)
        stats["status"] = status
        contract_details.append(stats)
        blockers.extend(reasons)
        contract_statuses.append(status)
    checks["gate_contract"] = {
        "status": (
            "FAIL" if "FAIL" in contract_statuses
            else "HOLD" if "HOLD" in contract_statuses
            else "PASS"
        ),
        "suites": contract_details,
    }

    tenant_details: list[dict[str, Any]] = []
    for suite_path, minimum in TENANT_SUITES.items():
        stats = _suite_stats(cases, suite_path)
        status, reasons = _suite_status(stats, minimum)
        stats["status"] = status
        tenant_details.append(stats)
        blockers.extend(reasons)
        statuses.append(status)

    tenant_status = (
        "FAIL" if "FAIL" in statuses else "HOLD" if "HOLD" in statuses else "PASS"
    )
    checks["tenant_isolation"] = {
        "status": tenant_status,
        "suites": tenant_details,
        "scope": (
            "Automated PostgreSQL RLS/API/memory tests only. This does not prove "
            "isolation against a compromised shared worker identity."
        ),
    }

    recovery_details: list[dict[str, Any]] = []
    recovery_statuses: list[str] = []
    for suite_path, minimum in RECOVERY_SUITES.items():
        stats = _suite_stats(cases, suite_path)
        status, reasons = _suite_status(stats, minimum)
        stats["status"] = status
        recovery_details.append(stats)
        blockers.extend(reasons)
        recovery_statuses.append(status)

    recovery_status = (
        "FAIL" if "FAIL" in recovery_statuses
        else "HOLD" if "HOLD" in recovery_statuses
        else "PASS"
    )
    checks["failure_recovery"] = {
        "status": recovery_status,
        "suites": recovery_details,
        "scope": "Automated queue lease, retry, fencing, and concurrency regressions.",
    }

    all_statuses = contract_statuses + statuses + recovery_statuses
    automation_status = (
        "FAIL" if "FAIL" in all_statuses
        else "HOLD" if "HOLD" in all_statuses
        else "PASS"
    )
    enterprise_status = "FAIL" if automation_status == "FAIL" else "HOLD"
    if automation_status == "PASS":
        blockers.append(
            "Automated CI passed, but enterprise certification remains HOLD until "
            "all staging evidence and independent sign-offs are supplied."
        )

    report = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "candidate_sha": expected_sha or None,
        "evidence_source": "GitHub Actions JUnit XML from the exact candidate commit",
        "automation_status": automation_status,
        "enterprise_readiness_status": enterprise_status,
        "checks": checks,
        "blockers": blockers,
        "required_staging_evidence": REQUIRED_STAGING_EVIDENCE,
    }
    return report, 0 if automation_status == "PASS" else 1 if automation_status == "FAIL" else 2


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _schema_errors(evidence: dict[str, Any]) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["jsonschema is not installed; install the project dependencies to certify"]
    validator = Draft202012Validator(_load_schema())
    errors = sorted(
        validator.iter_errors(evidence),
        key=lambda error: (list(map(str, error.absolute_path)), error.message),
    )
    return [
        f"{'.'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}"
        for error in errors
    ]


def evaluate_certification(
    evidence_path: str | Path, expected_sha: str, expected_run_id: int | None = None
) -> tuple[dict[str, Any], int]:
    """Evaluate staging evidence for a formal ERG-06 PASS/HOLD/FAIL decision."""
    path = Path(evidence_path)
    if not path.is_file():
        report = {
            "status": "HOLD",
            "candidate_sha": expected_sha or None,
            "blockers": [f"Staging evidence file not found: {path}"],
        }
        return report, 2

    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        report = {"status": "HOLD", "candidate_sha": expected_sha or None,
                  "blockers": [f"Staging evidence is unreadable: {exc}"]}
        return report, 2

    if not isinstance(evidence, dict):
        return {"status": "HOLD", "blockers": ["Evidence root must be a JSON object"]}, 2

    schema_errors = _schema_errors(evidence)
    if schema_errors:
        return {
            "status": "HOLD",
            "candidate_sha": expected_sha or evidence.get("candidate_sha"),
            "blockers": ["Evidence schema is incomplete or invalid.", *schema_errors],
        }, 2

    blockers: list[str] = []
    failures: list[str] = []
    holds: list[str] = []

    candidate_sha = evidence["candidate_sha"]
    if not expected_sha:
        holds.append("Expected candidate SHA was not supplied.")
    elif candidate_sha.lower() != expected_sha.lower():
        failures.append(
            f"Evidence SHA {candidate_sha} does not match candidate SHA {expected_sha}."
        )

    source = evidence["source"]
    if expected_run_id is None:
        holds.append("Expected staging workflow run ID was not supplied.")
    elif source["run_id"] != expected_run_id:
        failures.append(
            f"Evidence run ID {source['run_id']} does not match supplied run ID {expected_run_id}."
        )
    if source["attestation_verified"] is not True:
        holds.append("Evidence provenance attestation has not been verified.")
    if source["workflow"] != "RUSA-AI Staging Acceptance":
        holds.append("Evidence was not produced by the approved staging acceptance workflow.")

    checks = evidence["checks"]
    for check_name, check in checks.items():
        if check["status"] == "FAIL":
            failures.append(f"{check_name}: staging evidence reports FAIL.")
        elif check["status"] != "PASS":
            holds.append(f"{check_name}: staging evidence is not PASS.")

    tenant = checks["tenant_isolation"]
    if tenant["negative_cases"] < 1:
        holds.append("Tenant-isolation negative test coverage is missing.")
    if tenant["unauthorized_cross_tenant_reads"] != 0 or tenant["unauthorized_cross_tenant_writes"] != 0:
        failures.append("Unauthorized cross-tenant reads or writes were observed.")
    if tenant["unauthorized_accesses"] != 0:
        failures.append("At least one unauthorized tenant access was observed.")

    recovery = checks["failure_recovery"]
    if recovery["recoverable_faults"] < 100:
        holds.append("Fewer than 100 recoverable-fault simulations were recorded.")
    elif recovery["recovered"] > recovery["recoverable_faults"]:
        failures.append("Recovered count exceeds the number of recoverable faults.")
    elif recovery["recovered"] / recovery["recoverable_faults"] < 0.95:
        failures.append("Failure-recovery success is below 95%.")
    if recovery["duplicate_business_effects"] != 0:
        failures.append("Duplicate external business effects were observed.")
    if recovery["state_integrity_failures"] != 0:
        failures.append("Recovery produced state-integrity failures.")

    disaster = checks["disaster_recovery"]
    if disaster["rpo_seconds"] > 300:
        failures.append("Measured RPO exceeds 5 minutes.")
    if disaster["rto_seconds"] > 3600:
        failures.append("Measured RTO exceeds 60 minutes.")
    if disaster["confirmed_audit_loss"] != 0:
        failures.append("Confirmed audit loss was observed during recovery.")

    kill_switch = checks["kill_switch"]
    if kill_switch["propagation_p99_seconds"] > 5:
        failures.append("Kill-switch propagation p99 exceeds 5 seconds.")
    if kill_switch["cancellation_p95_seconds"] > 30:
        failures.append("Internal cancellation p95 exceeds 30 seconds.")

    load = checks["load"]
    if load["peak_multiplier"] < 2:
        holds.append("Load test did not reach 2x projected peak.")
    if load["duration_minutes"] < 60:
        holds.append("Load test did not sustain the target for 60 minutes.")
    if load["slo_met"] is not True:
        failures.append("Service SLOs were not maintained during the load test.")

    policy_audit = checks["policy_audit"]
    if policy_audit["sensitive_actions"] < 1:
        holds.append("No sensitive actions were included in the audit reconciliation.")
    if policy_audit["audited_sensitive_actions"] != policy_audit["sensitive_actions"]:
        failures.append("Not all sensitive actions have complete policy/audit evidence.")
    if policy_audit["critical_false_allows"] != 0:
        failures.append("A critical false-allow policy decision was observed.")
    if policy_audit["duplicate_business_effects"] != 0:
        failures.append("Policy/audit reconciliation found duplicate business effects.")

    qa = checks["independent_qa"]
    if qa["independent"] is not True or qa["approved"] is not True:
        holds.append("Independent QA approval is missing.")
    if qa["critical_findings"] != 0:
        failures.append("Independent QA recorded critical findings.")

    for owner, signoff in evidence["signoffs"].items():
        if signoff["approved"] is not True:
            holds.append(f"{owner} sign-off is not approved.")

    blockers.extend(failures)
    blockers.extend(holds)
    status = "FAIL" if failures else "HOLD" if holds else "PASS"
    report = {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "candidate_sha": candidate_sha,
        "source": source,
        "status": status,
        "checks": checks,
        "signoffs": evidence["signoffs"],
        "blockers": blockers,
        "decision": (
            "GO: ERG-06 passed; Phase 6 may be considered for staged entry."
            if status == "PASS"
            else "NO GO: Phase 6 remains blocked until all ERG-06 requirements pass."
        ),
    }
    return report, 0 if status == "PASS" else 1 if status == "FAIL" else 2


def _write_report(report: dict[str, Any], output: str | None) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        if "automation_status" in report:
            status = report["automation_status"]
            enterprise = report["enterprise_readiness_status"]
        else:
            status = report.get("status", "HOLD")
            enterprise = status
        lines = [
            "## ERG-06 decision",
            "",
            f"- Automated CI evidence: **{status}**",
            f"- Enterprise readiness: **{enterprise}**",
            "",
        ]
        blockers = report.get("blockers", [])
        if blockers:
            lines.extend(["### Blockers / outstanding evidence", ""])
            lines.extend(f"- {item}" for item in blockers)
            lines.append("")
        with Path(summary_path).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("automated", "certify"), required=True)
    parser.add_argument(
        "--junitxml",
        action="append",
        help="JUnit XML report; repeat once per isolated PostgreSQL test database",
    )
    parser.add_argument("--evidence", help="Staging ERG-06 evidence JSON")
    parser.add_argument("--expected-sha", default=os.getenv("GITHUB_SHA", ""))
    parser.add_argument("--expected-run-id", type=int, help="Verified staging workflow run ID")
    parser.add_argument("--output", help="Optional JSON report output path")
    args = parser.parse_args(argv)

    if args.mode == "automated":
        if not args.junitxml:
            report = {
                "automation_status": "HOLD",
                "enterprise_readiness_status": "HOLD",
                "blockers": ["--junitxml is required in automated mode."],
            }
            _write_report(report, args.output)
            return 2
        report, exit_code = evaluate_automated(args.junitxml, args.expected_sha)
    else:
        if not args.evidence:
            report = {
                "status": "HOLD",
                "blockers": ["--evidence is required in certify mode."],
            }
            _write_report(report, args.output)
            return 2
        if args.expected_run_id is None:
            report = {
                "status": "HOLD",
                "blockers": ["--expected-run-id is required for certification."],
            }
            _write_report(report, args.output)
            return 2
        report, exit_code = evaluate_certification(
            args.evidence, args.expected_sha, args.expected_run_id
        )

    _write_report(report, args.output)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

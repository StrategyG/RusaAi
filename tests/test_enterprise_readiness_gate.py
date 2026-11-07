import json
import xml.etree.ElementTree as ET

from scripts.enterprise_readiness_gate import (
    evaluate_automated,
    evaluate_certification,
)


def write_junit(path, *, failure_module=None, skipped_module=None, omit_module=None):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="pytest")
    modules = {
        "test_postgres_rls": 6,
        "test_postgres_backends": 6,
        "test_queue_races": 6,
    }
    for module, count in modules.items():
        if module == omit_module:
            continue
        for index in range(count):
            case = ET.SubElement(
                suite,
                "testcase",
                classname=f"tests.{module}",
                file=f"tests/{module}.py",
                name=f"test_case_{index}",
            )
            if module == failure_module and index == 0:
                ET.SubElement(case, "failure", message="regression")
            if module == skipped_module and index == 0:
                ET.SubElement(case, "skipped", message="PostgreSQL DSN missing")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def valid_evidence(candidate_sha="a" * 40):
    passed = {"status": "PASS", "evidence_ref": "artifact://erg06/staging/run-123"}
    checks = {
        "tenant_isolation": {
            **passed,
            "negative_cases": 25,
            "cross_tenant_reads": 0,
            "cross_tenant_writes": 0,
            "unauthorized_accesses": 0,
        },
        "failure_recovery": {
            **passed,
            "recoverable_faults": 100,
            "recovered": 97,
            "duplicate_business_effects": 0,
            "state_integrity_failures": 0,
        },
        "disaster_recovery": {
            **passed,
            "rpo_seconds": 120,
            "rto_seconds": 2400,
            "confirmed_audit_loss": 0,
        },
        "kill_switch": {
            **passed,
            "propagation_p99_seconds": 3.2,
            "cancellation_p95_seconds": 20,
        },
        "load": {
            **passed,
            "peak_multiplier": 2.0,
            "duration_minutes": 60,
            "slo_met": True,
        },
        "policy_audit": {
            **passed,
            "sensitive_actions": 250,
            "audited_sensitive_actions": 250,
            "critical_false_allows": 0,
            "duplicate_business_effects": 0,
        },
        "independent_qa": {
            **passed,
            "independent": True,
            "approved": True,
            "critical_findings": 0,
            "reviewer": "independent-qa-reviewer",
        },
    }
    signoff = {"reviewer": "approved-owner", "approved": True}
    return {
        "schema_version": "1.0",
        "generated_at": "2026-11-07T08:00:00Z",
        "candidate_sha": candidate_sha,
        "environment": "staging",
        "source": {
            "workflow": "RUSA-AI Staging Acceptance",
            "run_id": 123,
            "attestation_verified": True,
            "attestation_ref": "https://github.com/StrategyG/RusaAi/attestations/123",
        },
        "checks": checks,
        "signoffs": {
            "platform": dict(signoff),
            "security": dict(signoff),
            "sre": dict(signoff),
            "qa": dict(signoff),
            "data_governance": dict(signoff),
            "finops": dict(signoff),
            "process_owner": dict(signoff),
        },
    }


def test_automated_ci_pass_does_not_certify_enterprise_readiness(tmp_path):
    junit = tmp_path / "junit.xml"
    write_junit(junit)

    report, exit_code = evaluate_automated(junit, "a" * 40)

    assert exit_code == 0
    assert report["automation_status"] == "PASS"
    assert report["enterprise_readiness_status"] == "HOLD"
    assert report["checks"]["tenant_isolation"]["status"] == "PASS"
    assert report["checks"]["failure_recovery"]["status"] == "PASS"
    assert report["required_staging_evidence"]


def test_automated_gate_holds_when_postgres_suite_is_skipped(tmp_path):
    junit = tmp_path / "junit.xml"
    write_junit(junit, skipped_module="test_postgres_rls")

    report, exit_code = evaluate_automated(junit, "a" * 40)

    assert exit_code == 2
    assert report["automation_status"] == "HOLD"
    assert any("skipped" in item for item in report["blockers"])


def test_automated_gate_fails_when_recovery_regression_fails(tmp_path):
    junit = tmp_path / "junit.xml"
    write_junit(junit, failure_module="test_queue_races")

    report, exit_code = evaluate_automated(junit, "a" * 40)

    assert exit_code == 1
    assert report["automation_status"] == "FAIL"
    assert report["checks"]["failure_recovery"]["status"] == "FAIL"


def test_certification_holds_without_staging_evidence(tmp_path):
    report, exit_code = evaluate_certification(tmp_path / "missing.json", "a" * 40)

    assert exit_code == 2
    assert report["status"] == "HOLD"


def test_certification_passes_only_with_complete_evidence(tmp_path):
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(valid_evidence()), encoding="utf-8")

    report, exit_code = evaluate_certification(evidence_path, "a" * 40)

    assert exit_code == 0
    assert report["status"] == "PASS"
    assert report["decision"].startswith("GO:")


def test_certification_fails_on_cross_tenant_access(tmp_path):
    evidence = valid_evidence()
    evidence["checks"]["tenant_isolation"]["cross_tenant_reads"] = 1
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    report, exit_code = evaluate_certification(evidence_path, "a" * 40)

    assert exit_code == 1
    assert report["status"] == "FAIL"
    assert any("cross-tenant" in item for item in report["blockers"])


def test_certification_fails_below_recovery_threshold(tmp_path):
    evidence = valid_evidence()
    evidence["checks"]["failure_recovery"]["recovered"] = 94
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    report, exit_code = evaluate_certification(evidence_path, "a" * 40)

    assert exit_code == 1
    assert report["status"] == "FAIL"
    assert any("below 95%" in item for item in report["blockers"])


def test_certification_fails_when_evidence_targets_another_commit(tmp_path):
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(valid_evidence("b" * 40)), encoding="utf-8")

    report, exit_code = evaluate_certification(evidence_path, "a" * 40)

    assert exit_code == 1
    assert report["status"] == "FAIL"
    assert any("does not match" in item for item in report["blockers"])


def test_certification_holds_until_provenance_is_verified(tmp_path):
    evidence = valid_evidence()
    evidence["source"]["attestation_verified"] = False
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    report, exit_code = evaluate_certification(evidence_path, "a" * 40)

    assert exit_code == 2
    assert report["status"] == "HOLD"
    assert any("attestation" in item for item in report["blockers"])

#!/usr/bin/env python3
"""Convert a bounded pytest JUnit XML result into the M8 focused-test receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    assert_process_memory_budget,
    write_bounded_json,
)

DEFAULT_XML = ROOT / "output/audits/bayes_basket_target_focused_tests.xml"
DEFAULT_OUTPUT = ROOT / "output/audits/bayes_basket_target_focused_test_receipt.json"
ROOT_CAUSE_REGRESSIONS = {
    "m1_probability_and_event_contracts": [
        "tests.test_bayes_basket_target_m1_contracts::test_probability_envelope_is_full_normalized_strict_and_content_addressed",
        "tests.test_bayes_basket_target_m1_contracts::test_forecast_revision_chain_replaces_each_predecessor_exactly_once",
        "tests.test_bayes_basket_target_m1_contracts::test_canonical_observation_sequence_rejects_duplicate_rewind_and_unreviewed_revision",
        "tests.test_bayes_basket_target_m1_contracts::test_canonical_correction_is_review_only_and_linear",
        "tests.test_bayes_basket_target_m1_contracts::test_stage_probability_bias_has_one_probability_only_direction_and_replays",
    ],
    "m1_unique_document_authority": [
        "tests.test_bayes_basket_target_document_authority::test_target_doctrine_has_exactly_one_no_live_authority",
    ],
    "three_layer_support_semantics": [
        "tests.test_bayes_basket_target_kernel_boundary::test_model_extrema_are_diagnostic_only_while_disagreement_cap_owns_candidate_boundary",
        "tests.test_bayes_basket_target_kernel_boundary::test_single_outlier_changes_diagnostics_but_cannot_force_expansion",
        "tests.test_bayes_basket_target_kernel_boundary::test_peripheral_bucket_requires_all_four_independent_qualifications",
        "tests.test_bayes_basket_target_kernel_boundary::test_two_remote_core_buckets_do_not_fill_the_interval_between_them",
    ],
    "formal_ensemble_minimum_two_models": [
        "tests.test_bayes_basket_target::test_formal_ensemble_contract_rejects_single_model_artifact",
        "tests.test_bayes_basket_target_no_live_calibration_authority::test_no_live_exact_two_model_mask_materializes_without_waiting_for_third",
    ],
    "target_local_registry_scope_selection": [
        "tests.test_export_bayes_basket_target_local_registry_scope::test_export_scope_uses_latest_snapshot_that_owns_requested_date",
        "tests.test_export_bayes_basket_target_local_registry_scope::test_export_scope_rejects_shared_live_database",
    ],
    "station_scoped_ws_manifest": [
        "tests.test_bayes_basket_target_production_worker::test_station_scoped_ws_manifest_preserves_explicit_allowlist",
    ],
    "cash_policy_generation_selection": [
        "tests.test_bayes_basket_target_resident_market_ingress::test_no_live_candidate_policy_uses_exact_manifest_not_highest_generation",
    ],
    "blocked_no_order_reconciliation": [
        "tests.test_bayes_basket_target_production_readiness::test_blocked_no_order_decision_closes_reconciliation_stage_without_fake_row",
    ],
    "blocked_cycle_authoritative_closure": [
        "tests.test_bayes_basket_target_m0_closure_audit::test_blocked_prepared_cycle_is_retained_audit_fact_not_unresolved",
        "tests.test_bayes_basket_target_m0_closure_audit::test_prepared_cycle_without_blocking_decision_remains_actionable",
    ],
    "legacy_blocked_execution_vector_repair": [
        "tests.test_bayes_basket_target_execution_vector_repair::test_repair_appends_exact_no_execution_vector_and_is_idempotent",
        "tests.test_bayes_basket_target_execution_vector_repair::test_repair_fails_whole_batch_on_invalid_decision_hash",
        "tests.test_bayes_basket_target_execution_vector_repair::test_repair_rejects_downstream_intent_without_partial_write",
        "tests.test_bayes_basket_target_m0_closure_audit::test_blocked_cycle_without_no_execution_vector_is_not_closed",
    ],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_receipt(
    xml_path: Path,
    *,
    root_cause_regressions: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    resolved = xml_path.expanduser().resolve()
    size = int(resolved.stat().st_size)
    if size > MAX_JSON_INPUT_BYTES:
        raise ValueError("focused_test_junit_xml_too_large")
    assert_process_memory_budget(projected_bytes=size * 4)
    root = ET.fromstring(resolved.read_bytes())
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
    failures = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
    duration = sum(float(suite.attrib.get("time", 0.0)) for suite in suites)
    testcase_files: set[str] = set()
    testcase_ids: set[str] = set()
    for suite in suites:
        for case in suite.findall("testcase"):
            classname = str(case.attrib.get("classname") or "").strip()
            name = str(case.attrib.get("name") or "").strip()
            if classname and name:
                testcase_ids.add(f"{classname}::{name}")
            path = str(case.attrib.get("file") or "").strip()
            if not path:
                if classname.startswith("tests.test_"):
                    path = classname.replace(".", "/") + ".py"
            if path:
                testcase_files.add(path)
    sorted_testcase_files = sorted(testcase_files)
    required = ROOT_CAUSE_REGRESSIONS if root_cause_regressions is None else root_cause_regressions
    regression_evidence = {
        root_cause: {
            "required_tests": required_tests,
            "missing_tests": sorted(set(required_tests) - testcase_ids),
            "verified": set(required_tests).issubset(testcase_ids),
        }
        for root_cause, required_tests in required.items()
    }
    checks = {
        "tests_executed": tests > 0,
        "failures_zero": failures == 0,
        "errors_zero": errors == 0,
        "skipped_zero": skipped == 0,
        "target_test_files_present": bool(sorted_testcase_files),
        "root_cause_regressions_all_present": all(
            item["verified"] for item in regression_evidence.values()
        ),
    }
    identity = {
        "schema": "bayes_basket_target_focused_test_receipt_v1",
        "status": "PASS" if all(checks.values()) else "BLOCKED",
        "matrix_definition": (
            "all tests/*.py files referencing bayes_basket_target, "
            "build_bayes_basket_target, audit_cash_phase, or station_policy_transfer"
        ),
        "command": (
            "rg -l -0 \"bayes_basket_target|build_bayes_basket_target|"
            "audit_cash_phase|station_policy_transfer\" tests/*.py | sort -z | "
            "xargs -0 python3 -m pytest -q --junitxml=<bounded-output>"
        ),
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "duration_seconds": duration,
        "test_file_count": len(sorted_testcase_files),
        "test_files": sorted_testcase_files,
        "root_cause_regressions": regression_evidence,
        "junit_xml": str(resolved),
        "junit_xml_sha256": _sha256(resolved),
        "checks": checks,
        "boundary": {
            "receipt_only": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
        },
    }
    identity["receipt_sha256"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert_process_memory_budget()
    return identity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit-xml", type=Path, default=DEFAULT_XML)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_receipt(args.junit_xml)
    write_bounded_json(args.output, result)
    print(json.dumps({
        "schema": result["schema"],
        "status": result["status"],
        "tests": result["tests"],
        "test_file_count": result["test_file_count"],
        "output": str(args.output.expanduser().resolve()),
        "receipt_sha256": result["receipt_sha256"],
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import copy
import json

from scripts.materialize_probability_transaction_fault_injection_report import (
    LATEST_SCHEMA_VERSION,
    REQUIRED_TEST_NODE_IDS,
    SCHEMA_VERSION,
    _file_bindings,
    _report_sha,
    load_verified_latest_report,
    validate_report,
)


def _report(root):
    payload = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "passed_current_code",
        "pytest_exit_code": 0,
        "test_node_ids": list(REQUIRED_TEST_NODE_IDS),
        "file_sha256": _file_bindings(root),
        "boundary": {
            "isolated_or_in_memory_test_databases_only": True,
            "writes_runtime_database": False,
            "activates_model": False,
            "places_orders": False,
        },
    }
    payload["report_sha256"] = _report_sha(payload)
    return payload


def test_report_validation_binds_passing_suite_to_current_code(tmp_path) -> None:
    for relative in (
        "scripts/materialize_probability_transaction_fault_injection_report.py",
        "sequential_bayes_shadow_writer.py",
        "sequential_bayes_public_ssot_shadow_writer.py",
        "tests/test_sequential_bayes_shadow_writer.py",
        "tests/test_sequential_bayes_public_ssot_shadow_writer.py",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")

    payload = _report(tmp_path)
    assert validate_report(payload, root=tmp_path) == []

    changed = copy.deepcopy(payload)
    (tmp_path / "sequential_bayes_shadow_writer.py").write_text("changed", encoding="utf-8")
    assert "fault_injection_report_stale_for_current_code" in validate_report(changed, root=tmp_path)


def test_latest_pointer_only_accepts_content_addressed_current_report(tmp_path) -> None:
    repo = tmp_path / "repo"
    report_root = tmp_path / "reports"
    for relative in (
        "scripts/materialize_probability_transaction_fault_injection_report.py",
        "sequential_bayes_shadow_writer.py",
        "sequential_bayes_public_ssot_shadow_writer.py",
        "tests/test_sequential_bayes_shadow_writer.py",
        "tests/test_sequential_bayes_public_ssot_shadow_writer.py",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative, encoding="utf-8")
    payload = _report(repo)
    report_root.mkdir(parents=True)
    (report_root / f"{payload['report_sha256']}.json").write_text(json.dumps(payload), encoding="utf-8")
    (report_root / "latest.json").write_text(
        json.dumps({"schema": LATEST_SCHEMA_VERSION, "report_sha256": payload["report_sha256"]}),
        encoding="utf-8",
    )

    loaded, path, blockers = load_verified_latest_report(report_root=report_root, root=repo)
    assert blockers == []
    assert loaded["report_sha256"] == payload["report_sha256"]
    assert path.endswith(f"{payload['report_sha256']}.json")

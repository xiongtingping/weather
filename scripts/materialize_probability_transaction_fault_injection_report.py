#!/usr/bin/env python3
"""Materialize content-addressed atomicity evidence for probability Transaction B.

The report is research-only.  It executes isolated/in-memory tests, binds their
result to the current implementation and test file hashes, and cannot touch a
runtime database, activate a model, or place an order.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "probability_transaction_fault_injection_report_v1"
LATEST_SCHEMA_VERSION = "probability_transaction_fault_injection_latest_v1"
REPORT_ROOT = ROOT / "data" / "research" / "probability_cutover" / "fault_injection"
CONFIRMATION = "I_UNDERSTAND_THIS_WRITES_RESEARCH_EVIDENCE"

BOUND_FILES = (
    "scripts/materialize_probability_transaction_fault_injection_report.py",
    "sequential_bayes_shadow_writer.py",
    "sequential_bayes_public_ssot_shadow_writer.py",
    "tests/test_sequential_bayes_shadow_writer.py",
    "tests/test_sequential_bayes_public_ssot_shadow_writer.py",
)

REQUIRED_TEST_NODE_IDS = (
    "tests/test_sequential_bayes_public_ssot_shadow_writer.py::test_public_ssot_fault_after_history_rolls_back_p1_history_latest_and_outbox",
    "tests/test_sequential_bayes_public_ssot_shadow_writer.py::test_physical_only_public_ssot_fault_rolls_back_update_checkpoint_history_and_latest",
    "tests/test_sequential_bayes_shadow_writer.py::test_transaction_b_rejects_stale_predecessor_before_any_write",
    "tests/test_sequential_bayes_shadow_writer.py::test_transaction_b_rejects_changed_previous_posterior_hash_before_any_write",
    "tests/test_sequential_bayes_shadow_writer.py::test_transaction_b_rolls_back_all_prior_shadow_inserts_when_outbox_conflicts",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_bindings(root: Path) -> dict[str, str]:
    bindings: dict[str, str] = {}
    for relative in BOUND_FILES:
        path = root / relative
        bindings[relative] = _sha_bytes(path.read_bytes())
    return bindings


def _report_sha(payload: Mapping[str, Any]) -> str:
    identity = {key: value for key, value in payload.items() if key != "report_sha256"}
    return _sha_bytes(_canonical_json(identity).encode("utf-8"))


def validate_report(payload: Mapping[str, Any], *, root: Path = ROOT) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != SCHEMA_VERSION:
        blockers.append("fault_injection_report_schema_invalid")
    expected_report_sha = _report_sha(payload)
    if str(payload.get("report_sha256") or "") != expected_report_sha:
        blockers.append("fault_injection_report_sha256_mismatch")
    if payload.get("ok") is not True or int(payload.get("pytest_exit_code", -1)) != 0:
        blockers.append("fault_injection_test_suite_not_passing")
    if tuple(payload.get("test_node_ids") or ()) != REQUIRED_TEST_NODE_IDS:
        blockers.append("fault_injection_required_test_set_mismatch")
    try:
        current_bindings = _file_bindings(root)
    except OSError:
        blockers.append("fault_injection_bound_file_unreadable")
    else:
        if dict(payload.get("file_sha256") or {}) != current_bindings:
            blockers.append("fault_injection_report_stale_for_current_code")
    boundary = dict(payload.get("boundary") or {})
    required_boundary = {
        "isolated_or_in_memory_test_databases_only": True,
        "writes_runtime_database": False,
        "activates_model": False,
        "places_orders": False,
    }
    if any(boundary.get(key) != value for key, value in required_boundary.items()):
        blockers.append("fault_injection_report_boundary_invalid")
    return sorted(set(blockers))


def load_verified_latest_report(
    *, report_root: Path = REPORT_ROOT, root: Path = ROOT,
) -> tuple[dict[str, Any], str, list[str]]:
    latest_path = report_root / "latest.json"
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        if latest.get("schema") != LATEST_SCHEMA_VERSION:
            return {}, "", ["fault_injection_latest_pointer_schema_invalid"]
        report_sha = str(latest.get("report_sha256") or "")
        report_path = (report_root / f"{report_sha}.json").resolve()
        if report_path.parent != report_root.resolve():
            return {}, "", ["fault_injection_latest_pointer_path_invalid"]
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "", ["fault_injection_report_missing"]
    except (OSError, ValueError, TypeError):
        return {}, "", ["fault_injection_report_unreadable"]
    blockers = validate_report(payload, root=root)
    if str(payload.get("report_sha256") or "") != report_sha:
        blockers.append("fault_injection_latest_pointer_sha_mismatch")
    return dict(payload), str(report_path), sorted(set(blockers))


def build_report(*, root: Path = ROOT) -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", "-q", *REQUIRED_TEST_NODE_IDS]
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = completed.stdout or ""
    payload: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "ok": completed.returncode == 0,
        "state": "passed_current_code" if completed.returncode == 0 else "test_failure",
        "executed_at": started_at,
        "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
        "python_executable": sys.executable,
        "pytest_command": ["python", "-m", "pytest", "-q", *REQUIRED_TEST_NODE_IDS],
        "pytest_exit_code": completed.returncode,
        "pytest_output_sha256": _sha_bytes(output.encode("utf-8")),
        "pytest_output_tail": output[-12000:],
        "test_node_ids": list(REQUIRED_TEST_NODE_IDS),
        "file_sha256": _file_bindings(root),
        "evidence": {
            "public_probability_ssot_history_fault_rolls_back_entire_transaction": True,
            "physical_only_probability_ssot_history_fault_rolls_back_entire_transaction": True,
            "stale_predecessor_checkpoint_cas_rejected_before_write": True,
            "changed_predecessor_posterior_hash_cas_rejected_before_write": True,
            "outbox_conflict_rolls_back_all_prior_transaction_b_inserts": True,
        },
        "boundary": {
            "research_only": True,
            "isolated_or_in_memory_test_databases_only": True,
            "writes_runtime_database": False,
            "activates_model": False,
            "places_orders": False,
            "proves_forward_live_latency": False,
            "proves_replay_equivalence": False,
        },
    }
    payload["report_sha256"] = _report_sha(payload)
    return payload


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _write_immutable(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError("fault_injection_immutable_report_collision")
        return
    _atomic_write(path, payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--report-root", type=Path, default=REPORT_ROOT)
    args = parser.parse_args()
    report = build_report()
    if args.apply:
        if args.confirm != CONFIRMATION:
            raise ValueError("research_evidence_write_confirmation_missing")
        report_root = args.report_root.expanduser().resolve()
        allowed_root = (ROOT / "data" / "research").resolve()
        if report_root != allowed_root and allowed_root not in report_root.parents:
            raise ValueError("fault_injection_report_path_outside_research_root")
        report_path = report_root / f"{report['report_sha256']}.json"
        _write_immutable(report_path, report)
        _atomic_write(
            report_root / "latest.json",
            {
                "schema": LATEST_SCHEMA_VERSION,
                "report_sha256": report["report_sha256"],
                "report_path": str(report_path),
                "state": report["state"],
            },
        )
        report["materialized_path"] = str(report_path)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())

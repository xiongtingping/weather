#!/usr/bin/env python3
"""Run the target-lane M8-04 fault matrix in isolated memory only.

The runner is an evidence boundary, not a production acceptance switch.  It
executes the cross-layer regression module against in-memory SQLite and a
temporary WS-manifest directory.  It never opens the production database,
loads a gateway, writes an artifact, activates a model, or places an order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TEST_NODE = "tests/test_bayes_basket_target_fault_matrix.py"
SCHEMA = "bayes_basket_target_fault_matrix_v1"

FAULT_CASES: dict[str, tuple[str, ...]] = {
    "transaction_write_points": (
        "observation", "bounds", "cycle", "decision", "intent", "reserve"
    ),
    "artifact_read": ("content_sha", "scope", "validity", "activation"),
    "official_ws": ("manifest_sha", "disconnected", "generation", "partial_shard"),
    "inventory_order_state": ("incomplete_inventory", "unknown_order_state", "version_mismatch", "rest_source"),
    "execution_cas": ("posterior_version", "posterior_sha256", "snapshot_sha256", "quote_version", "inventory_version", "order_state_version", "target_price_id", "quote_expiry"),
    "reconciliation": ("unknown_post", "partial_fill", "authoritative_partial"),
    "gateway_boundary": ("gateway_authorization_rejected", "zero_ledger_write"),
}


def _passed_count(output: str) -> int:
    matches = re.findall(r"(\d+) passed", output)
    return int(matches[-1]) if matches else 0


def _stable_output_tail(output: str) -> str:
    return re.sub(r"in \d+(?:\.\d+)?s", "in <elapsed>s", output[-4000:])


def run_matrix() -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", "-q", TEST_NODE]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = completed.stdout or ""
    passed = _passed_count(output)
    return {
        "schema": SCHEMA,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "test_node": TEST_NODE,
        "pytest_exit_code": completed.returncode,
        "pytest_passed": passed,
        "fault_case_count": sum(len(items) for items in FAULT_CASES.values()),
        "fault_cases": FAULT_CASES,
        "boundary": {
            "isolated_or_in_memory_only": True,
            "writes_production_database": False,
            "activates_model": False,
            "loads_order_gateway": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "partial_target_generation_allowed": False,
            "unknown_order_state_allowed_to_submit": False,
        },
        "pytest_output_tail": _stable_output_tail(output),
    }


def _receipt_sha(receipt: dict[str, Any]) -> str:
    identity = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-receipt",
        type=Path,
        help="optional research/audit path; omitted means stdout-only read-only mode",
    )
    args = parser.parse_args()
    receipt = run_matrix()
    receipt["receipt_sha256"] = _receipt_sha(receipt)
    if args.write_receipt is not None:
        output_path = args.write_receipt if args.write_receipt.is_absolute() else ROOT / args.write_receipt
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

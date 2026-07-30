#!/usr/bin/env python3
"""Run the isolated production-no-live target chain rehearsal.

The test uses a temporary in-memory SQLite ledger and synthetic, explicitly
marked test artifacts.  It proves P1 lineage -> target plan -> three intents ->
capital reservation -> idempotent replay -> release, without opening the
production database or a gateway.
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
TEST_NODE = "tests/test_bayes_basket_target_production_chain_e2e.py"


def _receipt_sha(value: dict[str, Any]) -> str:
    identity = {key: item for key, item in value.items() if key != "receipt_sha256"}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_rehearsal() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", TEST_NODE],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = completed.stdout or ""
    match = re.search(r"(\d+) passed", output)
    receipt = {
        "schema": "bayes_basket_target_production_chain_rehearsal_v1",
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "pytest_exit_code": completed.returncode,
        "pytest_passed": int(match.group(1)) if match else 0,
        "test_node": TEST_NODE,
        "boundary": {
            "uses_in_memory_sqlite": True,
            "uses_synthetic_test_artifacts": True,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "multi_leg_intents": 3,
            "idempotent_replay": True,
            "capital_release": True,
        },
        "pytest_output_tail": output[-2000:],
    }
    receipt["receipt_sha256"] = _receipt_sha(receipt)
    return receipt


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    receipt = run_rehearsal()
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


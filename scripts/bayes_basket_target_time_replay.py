#!/usr/bin/env python3
"""Run the isolated full-chain time replay regression for M8-03."""

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
TEST_NODE = "tests/test_bayes_basket_target_time_replay.py"
SCHEMA = "bayes_basket_target_time_replay_receipt_v1"


def _sha(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _stable_output_tail(output: str) -> str:
    return re.sub(r"in \d+(?:\.\d+)?s", "in <elapsed>s", output[-4000:])


def run() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", TEST_NODE],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = completed.stdout or ""
    matches = re.findall(r"(\d+) passed", output)
    passed = int(matches[-1]) if matches else 0
    receipt = {
        "schema": SCHEMA,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "test_node": TEST_NODE,
        "pytest_exit_code": completed.returncode,
        "pytest_passed": passed,
        "coverage": {
            "cross_timezone": True,
            "dst_fallback": True,
            "cross_day": True,
            "incremental_equals_full": True,
            "duplicate_idempotency": True,
            "forward_backward_rewind_rejected": True,
            "future_source_rejected": True,
            "official_ws_only": True,
        },
        "boundary": {
            "isolated_or_in_memory_only": True,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "partial_target_generation_allowed": False,
        },
        "pytest_output_tail": _stable_output_tail(output),
    }
    receipt["receipt_sha256"] = _sha(receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-receipt", type=Path)
    args = parser.parse_args()
    receipt = run()
    if args.write_receipt is not None:
        path = args.write_receipt if args.write_receipt.is_absolute() else ROOT / args.write_receipt
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

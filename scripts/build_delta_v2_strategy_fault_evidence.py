#!/usr/bin/env python3
"""Emit bounded, in-memory evidence for DeltaBasketScore v2 safety gates.

This intentionally proves implementation boundaries only.  It neither reads
the production SSOT nor converts forward observations into OOS evidence, and
cannot promote a station-policy artifact.
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
SCHEMA = "delta_basket_score_v2_strategy_fault_evidence_v1"
DEFAULT_OUTPUT = (
    ROOT / "output" / "audits" / "delta_v2_strategy_fault_evidence_current.json"
)

# Keep the proof narrowly focused on the two previously non-isolated gates,
# plus their indispensable no-live/OOS boundaries.  Every node is entirely
# fixture/in-memory based.
TEST_CASES: dict[str, str] = {
    "stale_official_ws_holds": (
        "tests/test_bayes_basket_target_delta_v2.py::"
        "test_hard_gates_cannot_be_overridden_by_high_score"
    ),
    "missing_selected_yes_maker_leg_holds": (
        "tests/test_bayes_basket_target_delta_v2_runtime.py::"
        "test_bid_only_selected_bucket_is_retained_without_buy_candidate"
    ),
    "profitable_subset_cannot_bypass_complete_bundle": (
        "tests/test_bayes_basket_target_delta_v2_runtime.py::"
        "test_profitable_single_bucket_cannot_bypass_incomplete_basket"
    ),
    "unconfirmed_cancel_blocks_fok": (
        "tests/test_bayes_basket_target_delta_v2_runtime.py::"
        "test_fok_is_fail_closed_until_maker_cancel_is_confirmed"
    ),
    "resource_probe_failure_holds": (
        "tests/test_bayes_basket_target_delta_v2_runtime.py::"
        "test_resource_probe_failure_is_a_hard_fail_closed_gate"
    ),
    "forward_observation_never_becomes_policy_oos": (
        "tests/test_station_policy_exact_stage_scorecard.py::"
        "test_exact_stage_scorecard_excludes_open_future_event_from_coverage"
    ),
    "closed_awc_raw_binding_remains_unscored": (
        "tests/test_station_policy_exact_stage_scorecard.py::"
        "test_exact_stage_scorecard_validates_closed_awc_receipt_binding_but_never_scores"
    ),
}


def _stable_output(output: str) -> str:
    return re.sub(r"in \d+(?:\.\d+)?s", "in <elapsed>s", output[-4000:])


def _content_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_receipt() -> dict[str, Any]:
    command = [sys.executable, "-m", "pytest", "-q", *TEST_CASES.values()]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    identity: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "evidence_class": "IN_MEMORY_IMPLEMENTATION_FAULT_EVIDENCE",
        "scope": "delta_basket_score_v2_strategy_layer",
        "test_cases": TEST_CASES,
        "pytest_exit_code": completed.returncode,
        "pytest_output_tail": _stable_output(completed.stdout or ""),
        "all_cases_passed": completed.returncode == 0,
        "oos_boundary": {
            "historical_strict_oos_claimed": False,
            "forward_data_used": False,
            "forward_observational_candidate_not_oos": True,
            "score_rows_emitted": False,
            "promotion_permitted": False,
        },
        "boundary": {
            "isolated_fixture_or_in_memory_only": True,
            "network_access": False,
            "reads_production_database": False,
            "writes_production_database": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        },
    }
    return {**identity, "content_sha256": _content_sha256(identity)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    receipt = build_receipt()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": receipt["status"],
        "all_cases_passed": receipt["all_cases_passed"],
        "content_sha256": receipt["content_sha256"],
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

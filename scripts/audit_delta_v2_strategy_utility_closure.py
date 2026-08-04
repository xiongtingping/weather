#!/usr/bin/env python3
"""Audit whether existing DeltaBasketScore evidence can form utility rows.

The audit consumes only already-written receipts.  It never opens SQLite,
fetches data, creates fills, infers zero-fill from missing records, or writes
production state.  A utility row is considered safe only when the exact-stage
fact, official WS mark, closed AWC truth, and a directly bound authoritative
fill/cancel receipt are all present under the active utility contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "delta_v2_strategy_utility_closure_audit_v1"
DEFAULT_SCORECARD = (
    ROOT
    # Immutable latest no-live recheck retained on 2026-08-03.  A newer
    # snapshot may be supplied explicitly with --scorecard; this default must
    # not silently fall back to the pre-TAF 2026-08-02 inventory.
    / "output/audits/station_policy_exact_stage_scorecard_45station_utility_recheck_20260803T054035Z.json"
)
DEFAULT_FAULT_RECEIPT = (
    ROOT / "output/audits/delta_v2_strategy_fault_evidence_20260803.json"
)
DEFAULT_LEDGER = (
    ROOT
    # Keep the Delta audit aligned with the latest unified 45-station ledger
    # without fetching or rebuilding any model data.
    / "output/audits/oos_gap_ledger_45station_20260803_after_taf_proxy.json"
)
DEFAULT_OUTPUT = (
    ROOT / "output/audits/delta_v2_strategy_utility_closure_audit_20260803.json"
)


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().resolve(strict=True).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"receipt_mapping_required:{path}")
    return dict(value)


def _embedded_hash_ok(value: Mapping[str, Any]) -> bool:
    supplied = str(value.get("content_sha256") or "")
    if len(supplied) != 64:
        return False
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest() == supplied


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_audit(
    *,
    scorecard_path: Path = DEFAULT_SCORECARD,
    fault_receipt_path: Path = DEFAULT_FAULT_RECEIPT,
    ledger_path: Path = DEFAULT_LEDGER,
) -> dict[str, Any]:
    scorecard = _load(scorecard_path)
    fault = _load(fault_receipt_path)
    ledger = _load(ledger_path)
    inventory = scorecard.get("utility_evidence_inventory")
    if not isinstance(inventory, Mapping):
        raise ValueError("utility_evidence_inventory_missing")
    input_counts = scorecard.get("input_counts")
    if not isinstance(input_counts, Mapping):
        raise ValueError("utility_input_counts_missing")
    receipt_inventory = inventory.get("candidate_execution_receipts")
    fill_inventory = inventory.get("confirmed_yes_fill_receipt")
    order_events = inventory.get("order_events")
    if not all(isinstance(item, Mapping) for item in (receipt_inventory, fill_inventory, order_events)):
        raise ValueError("utility_receipt_inventory_incomplete")

    score_rows = input_counts.get("score_rows")
    if not isinstance(score_rows, Mapping):
        score_rows = {}
    score_row_counts = {
        stage: {
            family: _int((score_rows.get(stage) or {}).get(family))
            for family in ("target", "reserve", "cadence")
        }
        for stage in ("P0", "P0-M", "P1")
    }
    score_row_total = sum(
        value for stage in score_row_counts.values() for value in stage.values()
    )
    direct_binding = str(
        (scorecard.get("utility_contract_pointer") or {}).get("direct_fill_binding")
        or ""
    )
    metric_state = str(
        (scorecard.get("utility_contract_pointer") or {}).get("metric_state")
        or ""
    )
    authoritative_receipt_count = _int(receipt_inventory.get("authoritative_receipt_count"))
    non_simulated_receipt_count = _int(receipt_inventory.get("non_simulated_receipt_count"))
    fill_candidate_count = _int(receipt_inventory.get("authoritative_fill_candidate_count"))
    direct_fill_link = str(fill_inventory.get("direct_fact_to_receipt_link") or "")
    order_event_count = _int(order_events.get("record_count"))
    utility_rows_safe = bool(
        score_row_total > 0
        and direct_binding == "BOUND"
        and metric_state == "BOUND"
        and authoritative_receipt_count > 0
        and non_simulated_receipt_count > 0
        and fill_candidate_count > 0
        and direct_fill_link == "BOUND"
        and order_event_count > 0
    )
    blockers = sorted({str(item) for item in scorecard.get("blockers") or [] if str(item)})
    if not utility_rows_safe:
        blockers.extend(
            [
                "no_authoritative_fill_or_explicit_zero_fill_receipt",
                "utility_score_rows_not_safe_to_materialize",
                "absence_of_receipt_cannot_be_inferred_as_zero_fill",
            ]
        )
    blockers = sorted(set(blockers))
    identity = {
        "schema": SCHEMA,
        "state": "SAFE_UTILITY_ROWS_AVAILABLE" if utility_rows_safe else "BLOCKED_NO_SAFE_UTILITY_ROWS",
        "as_of_utc": str(scorecard.get("as_of_utc") or ""),
        "source_receipts": {
            "scorecard": {
                "path": str(scorecard_path.expanduser().resolve()),
                "schema": str(scorecard.get("schema") or ""),
                "content_sha256": str(scorecard.get("content_sha256") or ""),
                "embedded_hash_valid": _embedded_hash_ok(scorecard),
            },
            "strategy_fault": {
                "path": str(fault_receipt_path.expanduser().resolve()),
                "schema": str(fault.get("schema") or ""),
                "content_sha256": str(fault.get("content_sha256") or ""),
                "embedded_hash_valid": _embedded_hash_ok(fault),
                "status": str(fault.get("status") or ""),
            },
            "oos_ledger": {
                "path": str(ledger_path.expanduser().resolve()),
                "schema": str(ledger.get("schema") or ""),
                "content_sha256": str(ledger.get("content_sha256") or ""),
                "embedded_hash_valid": _embedded_hash_ok(ledger),
            },
        },
        "evidence": {
            "facts_read": _int(input_counts.get("facts_read")),
            "facts_with_frozen_label": _int(input_counts.get("facts_with_frozen_label")),
            "official_clob_ws_snapshot_count": _int(
                (inventory.get("official_clob_ws_snapshots") or {}).get("record_count")
            ),
            "candidate_execution_cycle_count": _int(
                (inventory.get("candidate_execution_cycles") or {}).get("record_count")
            ),
            "candidate_order_intent_count": _int(
                (inventory.get("candidate_order_intents") or {}).get("record_count")
            ),
            "order_event_count": order_event_count,
            "authoritative_receipt_count": authoritative_receipt_count,
            "non_simulated_receipt_count": non_simulated_receipt_count,
            "authoritative_fill_candidate_count": fill_candidate_count,
            "direct_fact_to_receipt_link": direct_fill_link,
            "utility_score_row_counts": score_row_counts,
            "utility_score_row_total": score_row_total,
            "utility_contract_binding": direct_binding,
            "utility_metric_state": metric_state,
        },
        "decision": {
            "safe_fact_to_receipt_utility_rows": utility_rows_safe,
            "forward_observations_count_as_oos": False,
            "missing_receipt_treated_as_zero_fill": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
        "blockers": blockers,
        "next_minimum_action": (
            "Obtain one closed historical exact station×stage×family scope with a real non-simulated fill receipt "
            "or an authoritative explicit zero-fill cancellation receipt, then directly bind candidate, baseline, "
            "official WS decision snapshot and closed AWC settlement truth; do not infer zero fill from absent rows."
        ),
    }
    return {
        **identity,
        "content_sha256": hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scorecard", type=Path, default=DEFAULT_SCORECARD)
    parser.add_argument("--strategy-fault", type=Path, default=DEFAULT_FAULT_RECEIPT)
    parser.add_argument("--oos-ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    receipt = build_audit(
        scorecard_path=args.scorecard,
        fault_receipt_path=args.strategy_fault,
        ledger_path=args.oos_ledger,
    )
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": receipt["schema"],
        "state": receipt["state"],
        "safe_fact_to_receipt_utility_rows": receipt["decision"]["safe_fact_to_receipt_utility_rows"],
        "utility_score_row_total": receipt["evidence"]["utility_score_row_total"],
        "authoritative_receipt_count": receipt["evidence"]["authoritative_receipt_count"],
        "content_sha256": receipt["content_sha256"],
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["state"] == "SAFE_UTILITY_ROWS_AVAILABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())

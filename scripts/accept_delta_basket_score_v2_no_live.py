#!/usr/bin/env python3
"""Bounded post-cutover acceptance for the resident ΔBasketScore v2 lane."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time
from typing import Any


EXPECTED_POLICY_VECTOR_SCHEMA = "delta_basket_score_policy_version_vector_v2"
EXPECTED_EXECUTION_VECTOR_SCHEMA = (
    "bayes_basket_target_execution_version_vector_v2"
)
EXPECTED_ECONOMIC_SCHEMA = "delta_basket_score_policy_artifact_v2"
MAX_TIMEOUT_SECONDS = 60
ALLOWED_WAITING_INPUT_REASONS = frozenset(
    {
        "official_ws_evidence_complete_scope_missing",
        "production_quote_expired",
    }
)


def _load_json(raw: Any, *, name: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name}_json_invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{name}_mapping_required")
    return value


def accept(
    *,
    db_path: Path,
    baseline_decision_rowid: int,
    baseline_fact_rowid: int,
    baseline_order_event_count: int,
    timeout_seconds: int,
    allow_waiting_input: bool = False,
    allow_already_current: bool = False,
) -> dict[str, Any]:
    timeout = int(timeout_seconds)
    if timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise RuntimeError("delta_v2_acceptance_timeout_out_of_bounds")
    database = db_path.expanduser().resolve(strict=True)
    deadline = time.monotonic() + timeout
    latest_reason = "resident_v2_fact_not_yet_materialized"
    accepted: dict[str, Any] | None = None
    accepted_zero_write = False
    while time.monotonic() < deadline:
        with sqlite3.connect(
            f"file:{database}?mode=ro",
            uri=True,
            timeout=5,
        ) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if quick != "ok":
                raise RuntimeError("delta_v2_acceptance_quick_check_failed")
            order_events = int(
                conn.execute(
                    "SELECT count(*) FROM basket_target_order_events"
                ).fetchone()[0]
            )
            if order_events != int(baseline_order_event_count):
                raise RuntimeError(
                    "delta_v2_acceptance_order_event_write_detected"
                )
            fact = conn.execute(
                "SELECT rowid,fact_id,decision_id,policy_version_vector_json,"
                "feature_json,created_at_utc "
                "FROM basket_policy_training_fact "
                "WHERE station_id='RKPK' AND rowid>? "
                "ORDER BY rowid DESC LIMIT 1",
                (int(baseline_fact_rowid),),
            ).fetchone()
            decision = conn.execute(
                "SELECT rowid,decision_id,decided_state,will_order,"
                "details_json,created_at,reason_codes_json "
                "FROM auto_trade_decisions "
                "WHERE station_id='RKPK' AND rowid>? "
                "ORDER BY rowid DESC LIMIT 1",
                (int(baseline_decision_rowid),),
            ).fetchone()
            candidate_zero_write = False
            if decision is None and allow_already_current:
                fact = conn.execute(
                    "SELECT rowid,fact_id,decision_id,"
                    "policy_version_vector_json,feature_json,created_at_utc "
                    "FROM basket_policy_training_fact "
                    "WHERE station_id='RKPK' AND rowid<=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (int(baseline_fact_rowid),),
                ).fetchone()
                if fact is not None:
                    decision = conn.execute(
                        "SELECT rowid,decision_id,decided_state,will_order,"
                        "details_json,created_at,reason_codes_json "
                        "FROM auto_trade_decisions "
                        "WHERE station_id='RKPK' AND decision_id=? "
                        "AND rowid<=? LIMIT 1",
                        (
                            str(fact[2]),
                            int(baseline_decision_rowid),
                        ),
                    ).fetchone()
                candidate_zero_write = (
                    fact is not None and decision is not None
                )
            if decision is None:
                latest_reason = "resident_rows_not_advanced"
            elif fact is None and allow_waiting_input:
                try:
                    reasons = json.loads(str(decision[6]))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "delta_v2_acceptance_reason_codes_invalid"
                    ) from exc
                details = _load_json(
                    decision[4],
                    name="delta_v2_acceptance_waiting_decision_details",
                )
                if (
                    isinstance(reasons, list)
                    and len(reasons) == 1
                    and str(reasons[0]) in ALLOWED_WAITING_INPUT_REASONS
                    and details.get("execution_mode")
                    == "production_no_live"
                    and details.get("no_orders_submitted") is True
                ):
                    waiting_reason = str(reasons[0])
                    return {
                        "schema": (
                            "delta_basket_score_v2_no_live_acceptance_v1"
                        ),
                        "state": "ACTIVATED_WAITING_INPUT_FAIL_CLOSED",
                        "reason": waiting_reason,
                        "decision_rowid": int(decision[0]),
                        "decision_id": str(decision[1]),
                        "fact_rowid": None,
                        "fact_id": None,
                        "order_event_count_unchanged": True,
                        "execution_mode": "production_no_live",
                        "live_submit_authorized": False,
                        "calls_order_gateway": False,
                        "orders_submitted": 0,
                    }
                latest_reason = "resident_waiting_reason_not_allowed"
            elif fact is None:
                latest_reason = "resident_v2_fact_not_yet_materialized"
            elif str(fact[1]).startswith("d2fact_") is False:
                latest_reason = "resident_training_fact_not_v2"
            elif str(fact[2]) != str(decision[1]):
                latest_reason = "resident_fact_decision_not_exact_joined"
            else:
                vector = _load_json(
                    fact[3], name="delta_v2_acceptance_version_vector"
                )
                feature = _load_json(
                    fact[4], name="delta_v2_acceptance_feature"
                )
                details = _load_json(
                    decision[4], name="delta_v2_acceptance_decision_details"
                )
                execution_vectors = feature.get("execution_version_vectors")
                if vector.get("schema") != EXPECTED_POLICY_VECTOR_SCHEMA:
                    latest_reason = "resident_policy_vector_not_v2"
                elif not str(
                    vector.get("delta_v2_artifact_sha256") or ""
                ):
                    latest_reason = "resident_delta_v2_artifact_unbound"
                elif feature.get("schema") != (
                    "delta_basket_score_forward_feature_vector_v2"
                ):
                    latest_reason = "resident_training_feature_not_v2"
                elif not isinstance(execution_vectors, list):
                    latest_reason = "resident_execution_vectors_invalid"
                elif any(
                    not isinstance(item, dict)
                    or item.get("schema") != EXPECTED_EXECUTION_VECTOR_SCHEMA
                    or item.get("economic_policy_schema")
                    != EXPECTED_ECONOMIC_SCHEMA
                    for item in execution_vectors
                ):
                    latest_reason = "resident_execution_vector_not_v2"
                elif details.get("delta_v2_authorized") is not True:
                    latest_reason = "resident_delta_v2_not_authorized"
                elif details.get("live_submit_authorized") is not False:
                    raise RuntimeError(
                        "delta_v2_acceptance_live_boundary_open"
                    )
                elif details.get("execution_mode") != "production_no_live":
                    raise RuntimeError(
                        "delta_v2_acceptance_execution_mode_invalid"
                    )
                else:
                    accepted_zero_write = candidate_zero_write
                    accepted = {
                        "decision_rowid": int(decision[0]),
                        "decision_id": str(decision[1]),
                        "decided_state": str(decision[2]),
                        "will_order": bool(decision[3]),
                        "fact_rowid": int(fact[0]),
                        "fact_id": str(fact[1]),
                        "fact_created_at_utc": str(fact[5]),
                        "economic_policy_artifact_sha256": str(
                            vector["delta_v2_artifact_sha256"]
                        ),
                    }
                    break
        time.sleep(0.5)
    if accepted is None:
        raise RuntimeError(
            "delta_v2_acceptance_timeout:" + latest_reason
        )
    return {
        "schema": "delta_basket_score_v2_no_live_acceptance_v1",
        "state": (
            "ACTIVATED_ALREADY_CURRENT_ZERO_WRITE"
            if accepted_zero_write
            else "PASSED"
        ),
        **accepted,
        "resident_zero_write_replay": accepted_zero_write,
        "order_event_count_unchanged": True,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--baseline-decision-rowid", type=int, required=True)
    parser.add_argument("--baseline-fact-rowid", type=int, required=True)
    parser.add_argument("--baseline-order-event-count", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=45)
    parser.add_argument("--allow-waiting-input", action="store_true")
    parser.add_argument("--allow-already-current", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            accept(
                db_path=args.db,
                baseline_decision_rowid=args.baseline_decision_rowid,
                baseline_fact_rowid=args.baseline_fact_rowid,
                baseline_order_event_count=args.baseline_order_event_count,
                timeout_seconds=args.timeout_seconds,
                allow_waiting_input=args.allow_waiting_input,
                allow_already_current=args.allow_already_current,
            ),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

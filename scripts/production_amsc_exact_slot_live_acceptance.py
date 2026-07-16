#!/usr/bin/env python3
"""Read-only acceptance for one exact scheduled AMSC METAR slot."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "production_amsc_exact_slot_live_acceptance_v1"
CHECKPOINT_SCHEMA = "posterior_checkpoint_v4_physical_state_bayes_v6"


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def audit(
    *,
    db: Path,
    event_date: str,
    slot_valid_at: str,
    receipt_path: Path,
    threshold_ms: float = 3000.0,
    expect_no_order: bool = False,
) -> dict[str, Any]:
    target = f"physical_weather_day_v1:ZUUU:{event_date[:10]}"
    slot = _parse_time(slot_valid_at).isoformat().replace("+00:00", "Z")
    blockers: list[str] = []
    evidence: dict[str, Any] = {"target_identity": target, "slot_valid_at": slot}
    with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        event_rows = conn.execute(
            "SELECT * FROM canonical_observation_events WHERE target_identity=? "
            "AND valid_time=? AND report_type='METAR' "
            "AND state IN ('accepted_primary','accepted_substitute','confirmed') "
            "ORDER BY revision_no DESC",
            (target, slot),
        ).fetchall()
        if len(event_rows) != 1:
            blockers.append("exact_slot_canonical_metar_not_unique")
            event: Mapping[str, Any] = {}
        else:
            event = dict(event_rows[0])
        generation_row = conn.execute(
            "SELECT MAX(replay_generation) FROM posterior_checkpoints WHERE target_identity=?",
            (target,),
        ).fetchone()
        generation = int(generation_row[0]) if generation_row and generation_row[0] is not None else -1
        checkpoint_row = None
        if event:
            checkpoint_row = conn.execute(
                "SELECT * FROM posterior_checkpoints WHERE target_identity=? "
                "AND replay_generation=? AND last_canonical_event_id=? AND state='committed' "
                "ORDER BY committed_at DESC LIMIT 1",
                (target, generation, event["canonical_observation_event_id"]),
            ).fetchone()
        checkpoint = dict(checkpoint_row) if checkpoint_row is not None else {}
        if not checkpoint:
            blockers.append("exact_slot_v6_checkpoint_missing")
        elif checkpoint.get("checkpoint_probability_schema") != CHECKPOINT_SCHEMA:
            blockers.append("exact_slot_checkpoint_schema_mismatch")
        latency_ms = None
        if event and checkpoint:
            latency_ms = (
                _parse_time(checkpoint["committed_at"])
                - _parse_time(event["first_actionable_seen_at"])
            ).total_seconds() * 1000.0
            if latency_ms < 0 or latency_ms > threshold_ms:
                blockers.append("exact_slot_t1_to_t3_slo_missed")
        posterior_run_id = str(checkpoint.get("posterior_run_id") or "")
        intent_count = 0
        outbox_count = 0
        if posterior_run_id:
            intent_count = int(conn.execute(
                "SELECT COUNT(*) FROM posterior_execution_intents WHERE posterior_run_id=?",
                (posterior_run_id,),
            ).fetchone()[0])
            outbox_count = int(conn.execute(
                "SELECT COUNT(*) FROM posterior_commit_outbox WHERE posterior_run_id=?",
                (posterior_run_id,),
            ).fetchone()[0])
        if expect_no_order and (intent_count or outbox_count):
            blockers.append("exact_slot_unexpected_execution_side_effect")
        evidence.update({
            "canonical_event_id": str(event.get("canonical_observation_event_id") or ""),
            "first_actionable_seen_at": str(event.get("first_actionable_seen_at") or ""),
            "checkpoint_id": str(checkpoint.get("posterior_checkpoint_id") or ""),
            "posterior_run_id": posterior_run_id,
            "committed_at": str(checkpoint.get("committed_at") or ""),
            "replay_generation": generation,
            "reported_x_lower_bound_c": checkpoint.get("reported_x_lower_bound_c"),
            "t1_to_t3_ms": round(latency_ms, 3) if latency_ms is not None else None,
            "posterior_execution_intent_count": intent_count,
            "posterior_commit_outbox_count": outbox_count,
        })
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        receipt = {}
    outcomes = ((receipt.get("probability") or {}).get("outcomes") or []) if isinstance(receipt, Mapping) else []
    receipt_event_ids = {
        str(value.get("event_id") or "") for value in outcomes if isinstance(value, Mapping)
    }
    receipt_bound = bool(evidence.get("canonical_event_id") in receipt_event_ids)
    if not receipt_bound:
        blockers.append("exact_slot_latest_fast_lane_receipt_not_bound")
    evidence["fast_lane_receipt_bound"] = receipt_bound
    evidence["fast_lane_total_ingress_ms"] = (receipt.get("latency_stages_ms") or {}).get("total_ingress_ms") if isinstance(receipt, Mapping) else None
    evidence["fast_lane_slo_met"] = receipt.get("probability_ssot_3000ms_slo_met") if isinstance(receipt, Mapping) else None
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "exact_slot_t1_to_t3_accepted" if not blockers else "blocked",
        "blockers": blockers,
        "evidence": evidence,
        "boundary": {
            "attests_t1_to_t3_probability_commit": True,
            "attests_t1_to_t5_exchange_post": False,
            "t1_to_t5_requires_real_gateway_sample": True,
            "read_only": True,
            "places_orders": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/archive.db")
    parser.add_argument("--event-date", required=True)
    parser.add_argument("--slot-valid-at", required=True)
    parser.add_argument(
        "--receipt",
        default="data/amsc_zuuu_metar_taf/p2_canonical_result_latest.json",
    )
    parser.add_argument("--threshold-ms", type=float, default=3000.0)
    parser.add_argument("--expect-no-order", action="store_true")
    args = parser.parse_args()
    result = audit(
        db=Path(args.db),
        event_date=args.event_date,
        slot_valid_at=args.slot_valid_at,
        receipt_path=Path(args.receipt),
        threshold_ms=args.threshold_ms,
        expect_no_order=args.expect_no_order,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

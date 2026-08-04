#!/usr/bin/env python3
"""Emit bounded, read-only evidence for an unlabelled Delta v2 truth gap."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402


MAX_FACTS = 256
SCHEMA = "delta_v2_label_truth_gap_audit_v1"
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
DEFAULT_OUTPUT = ROOT / "output" / "audits" / "lemd_p1_delta_v2_truth_gap_20260731.json"


def _iso8601_utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of_utc_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of_utc_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _event_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("event_date_invalid") from exc


def _safe_text(name: str, value: str) -> str:
    result = str(value or "").strip()
    if not result or len(result) > 32 or not result.replace("-", "").isalnum():
        raise ValueError(f"{name}_invalid")
    return result


def build_truth_gap_audit(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    stage: str,
    as_of_utc: str,
) -> dict[str, Any]:
    """Read one bounded fact/truth key without materializing labels."""

    facts = conn.execute(
        """
        SELECT f.rowid,f.fact_id,f.settlement_timezone,f.decision_at_utc,
               f.probability_envelope_id,f.content_sha256
        FROM basket_policy_training_fact AS f
        WHERE f.fact_id LIKE 'd2fact_%'
          AND f.authority_scope='PRODUCTION_NO_LIVE_FORWARD'
          AND f.station_id=? AND f.event_date=? AND f.stage=?
          AND NOT EXISTS(
              SELECT 1 FROM basket_policy_training_label AS l
              WHERE l.fact_id=f.fact_id
          )
        ORDER BY f.rowid
        LIMIT ?
        """,
        (station_id, event_date, stage, MAX_FACTS + 1),
    ).fetchall()
    if len(facts) > MAX_FACTS:
        raise RuntimeError("unlabelled_fact_bound_exceeded")
    truth_rows = conn.execute(
        """
        SELECT truth_id,source_lane,revision_no,qc_status,timestamps_complete,
               canonical_committed_at_utc,authority_scope,content_sha256
        FROM calibration_station_daily_high_truth
        WHERE station_id=? AND local_date=?
        ORDER BY revision_no,truth_id
        LIMIT ?
        """,
        (station_id, event_date, MAX_FACTS + 1),
    ).fetchall()
    if len(truth_rows) > MAX_FACTS:
        raise RuntimeError("truth_row_bound_exceeded")

    fact_items = [
        {
            "rowid": int(row[0]),
            "fact_id": str(row[1]),
            "settlement_timezone": str(row[2]),
            "decision_at_utc": str(row[3]),
            "probability_envelope_id": str(row[4]),
            "content_sha256": str(row[5]),
        }
        for row in facts
    ]
    truth_items = [
        {
            "truth_id": str(row[0]),
            "source_lane": str(row[1]),
            "revision_no": int(row[2]),
            "qc_status": str(row[3]),
            "timestamps_complete": bool(row[4]),
            "canonical_committed_at_utc": str(row[5] or ""),
            "authority_scope": str(row[6]),
            "content_sha256": str(row[7]),
        }
        for row in truth_rows
    ]
    state = (
        "BLOCKED_MISSING_FORMAL_TRUTH"
        if fact_items and not truth_items
        else "NO_UNLABELLED_TRUTH_GAP"
    )
    core = {
        "schema": SCHEMA,
        "state": state,
        "as_of_utc": as_of_utc,
        "station_id": station_id,
        "event_date": event_date,
        "stage": stage,
        "selector": {
            "fact_id_prefix": "d2fact_",
            "fact_authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
            "unlabelled_only": True,
        },
        "unlabelled_fact_count": len(fact_items),
        "unlabelled_fact_items": fact_items,
        "formal_truth_row_count": len(truth_items),
        "formal_truth_rows": truth_items,
        "materializer_consequence": (
            "no_candidate: its required inner join to "
            "calibration_station_daily_high_truth has no row"
            if state == "BLOCKED_MISSING_FORMAL_TRUTH"
            else "not_a_missing_truth_gap"
        ),
        "remediation": (
            "wait_for_or_ingest_official_AWC_truth_then_use_the_existing_"
            "settlement_label_materializer; do_not_synthesize_truth_or_label"
            if state == "BLOCKED_MISSING_FORMAL_TRUTH"
            else "none"
        ),
        "query_only": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**core, "content_sha256": content_sha256(core)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--station-id", default="LEMD")
    parser.add_argument("--event-date", default="2026-07-31")
    parser.add_argument("--stage", choices=("P0", "P0-M", "P1"), default="P1")
    parser.add_argument("--as-of", default=datetime.now(timezone.utc).isoformat())
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    station_id = _safe_text("station_id", args.station_id).upper()
    event_date = _event_date(args.event_date)
    as_of_utc = _iso8601_utc(args.as_of)
    database = args.db.resolve()
    if not database.is_file():
        raise SystemExit("database_missing")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10) as conn:
        conn.execute("PRAGMA query_only=ON")
        receipt = build_truth_gap_audit(
            conn,
            station_id=station_id,
            event_date=event_date,
            stage=args.stage,
            as_of_utc=as_of_utc,
        )
    output = args.output.resolve()
    encoded = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise SystemExit("audit_output_too_large")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp.{os.getpid()}")
    temporary.write_bytes(encoded)
    os.replace(temporary, output)
    print(json.dumps({
        "state": receipt["state"],
        "unlabelled_fact_count": receipt["unlabelled_fact_count"],
        "formal_truth_row_count": receipt["formal_truth_row_count"],
        "orders_submitted": 0,
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

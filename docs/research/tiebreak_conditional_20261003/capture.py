#!/usr/bin/env python3
"""Read-only capture for PROTOCOL.md: the replay rows, the lane's live
trades and its evaluation rows, each from the function production itself
uses.  Writes ``data.json`` beside this file and nothing else."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bayes_basket_target import tiebreak_cell_shadow as shadow  # noqa: E402
from bayes_basket_target import tiebreak_lane as lane  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
import scan_tiebreak_loss_cells as scan  # noqa: E402
import calibration_dashboard as board  # noqa: E402

FRESH_FLOOR = "2026-09-21"
STORED_CEILING = "2026-09-26"


def _mark(label: str, started: float) -> None:
    print(f"[{time.monotonic() - started:7.1f}s] {label}", flush=True)


def stored_replay() -> list[dict]:
    conn = sqlite3.connect(f"file:{shadow.SHADOW_DB}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {shadow.REPLAY_TABLE}")]
    finally:
        conn.close()


def runner_up_side(conn: sqlite3.Connection, envelope_id: str, snapshot_id: str,
                   bucket: str) -> dict:
    """The market's best bucket other than ``bucket``, and which side of it
    that sits on, by the envelope's own ordinals."""

    envelope = lane._envelope(conn, str(envelope_id)) or {}
    snapshot = lane._snapshot(conn, str(snapshot_id)) or {}
    ordinals = envelope.get("ordinals") or {}
    if not snapshot.get("buckets") or bucket not in ordinals:
        return {"runner_up": None, "runner_up_side": None}
    market = market_reference_v1(snapshot["buckets"])
    if market.get("state") != "complete":
        return {"runner_up": None, "runner_up_side": None}
    others = [
        (float(v), str(k)) for k, v in market["vector"].items()
        if str(k) != bucket and str(k) in ordinals and v is not None
    ]
    if not others:
        return {"runner_up": None, "runner_up_side": None}
    share, name = max(others)
    delta = int(ordinals[name]) - int(ordinals[bucket])
    return {"runner_up": name, "runner_up_share": share, "runner_up_offset": delta,
            "runner_up_side": ("cold" if delta < 0 else "warm")}


def main() -> int:
    started = time.monotonic()
    conn = board._connect(shadow.PRODUCTION_SSOT_DB)

    labels = shadow.settled_buckets(conn)
    _mark(f"settled labels: {len(labels)} station-days", started)

    stored = stored_replay()
    fresh = shadow.replay_cell(conn, floor_event_date=FRESH_FLOOR)
    _mark(f"replay: stored {len(stored)} rows, fresh {len(fresh)} rows", started)

    key = lambda r: (r["station_id"], r["event_date"], r["stage"])  # noqa: E731
    stored_by, fresh_by = {key(r): r for r in stored}, {key(r): r for r in fresh}
    window = lambda k: FRESH_FLOOR <= k[1] <= STORED_CEILING  # noqa: E731
    both = [k for k in stored_by if window(k) and k in fresh_by]
    overlap = {
        "stored_in_window": sum(1 for k in stored_by if window(k)),
        "fresh_in_window": sum(1 for k in fresh_by if window(k)),
        "in_both": len(both),
        "bucket_differs": sum(1 for k in both if stored_by[k]["bucket"] != fresh_by[k]["bucket"]),
        "ask_differs": sum(
            1 for k in both
            if abs((stored_by[k]["best_ask"] or 0) - (fresh_by[k]["best_ask"] or 0)) > 1e-9
        ),
    }
    merged = [r for r in stored if r["event_date"] <= STORED_CEILING]
    merged += [r for r in fresh if r["event_date"] > STORED_CEILING]
    relabelled = 0
    dropped = 0
    rows = []
    for row in merged:
        settled = labels.get((row["station_id"], row["event_date"]))
        if settled is None:
            dropped += 1
            continue
        if settled != row["settled_bucket"]:
            relabelled += 1
        rows.append(dict(row, stored_settled_bucket=row["settled_bucket"],
                         settled_bucket=settled, hit=int(settled == row["bucket"])))
    rows = scan.derive(rows, conn)
    _mark(f"replay merged {len(rows)} rows, relabelled {relabelled}, dropped {dropped}", started)

    live = board.collect_tiebreak_lane(conn)
    _mark(f"live trades: {live['trades'].get('filled')} filled", started)

    columns = [r[1] for r in conn.execute("PRAGMA table_info(tiebreak_lane_evaluation)")]
    evaluations = []
    for raw in conn.execute(
        "SELECT * FROM tiebreak_lane_evaluation WHERE fired=1"
        " AND rule_version NOT LIKE '%adjacent%'"
    ).fetchall():
        row = dict(zip(columns, raw))
        row.pop("plan_json", None)
        settled = labels.get((row["station_id"], row["event_date"]))
        row["settled_bucket"] = settled
        row["model_mode_hit"] = (
            int(settled == row["model_mode"]) if settled and row["model_mode"] else None
        )
        row.update(runner_up_side(
            conn, row["envelope_id"], row["snapshot_id"], str(row["model_mode"]),
        ))
        evaluations.append(row)
    _mark(f"evaluation rows (fired): {len(evaluations)}", started)

    # Every order the lane minted, with what the venue said about it: a leg
    # that never rested is not an unfilled order (order events decide).
    version_by_decision = {}
    for decision_id, details_json in conn.execute(
        "SELECT decision_id, details_json FROM auto_trade_decisions"
        " WHERE decision_id LIKE 'tiebreak_decision_%'"
    ).fetchall():
        signature = json.loads(details_json).get("decision_signature") or {}
        version_by_decision[str(decision_id)] = str(signature.get("rule") or "")
    legs = []
    for leg in board._render_live_book(conn).get("legs") or []:
        if not str(leg.get("decision_id") or "").startswith("tiebreak_decision_"):
            continue
        events = [
            str(r[0]) for r in conn.execute(
                "SELECT event_type FROM basket_target_order_events"
                " WHERE order_intent_id=? ORDER BY observed_at",
                (leg["order_intent_id"],),
            ).fetchall()
        ]
        legs.append({
            "order_intent_id": leg["order_intent_id"], "station_id": leg["station_id"],
            "event_date": leg["event_date"], "stage": leg.get("stage"),
            "bucket": leg["bucket"], "limit_price": leg.get("limit_price"),
            "filled_size": float(leg.get("filled_size") or 0.0),
            "filled_notional_usdc": float(leg.get("filled_notional_usdc") or 0.0),
            "state": leg.get("state"), "created_at": leg.get("created_at"),
            "events": events,
            "rule_version": version_by_decision.get(str(leg["decision_id"])),
            "settled_bucket": labels.get((leg["station_id"], leg["event_date"])),
        })
    _mark(f"legs: {len(legs)}", started)

    out = {
        "schema": "tiebreak_conditional_capture_v1",
        "legs": legs,
        "captured_at_utc": shadow.utc_now(),
        "fresh_floor": FRESH_FLOOR, "stored_ceiling": STORED_CEILING,
        "overlap": overlap, "relabelled_rows": relabelled, "dropped_unsettled": dropped,
        "replay": rows,
        "live": {k: live[k] for k in ("settings", "by_rule_version", "trades", "evaluations")},
        "evaluations_fired": evaluations,
    }
    (HERE / "data.json").write_text(json.dumps(out, ensure_ascii=False, default=str))
    _mark("written data.json", started)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

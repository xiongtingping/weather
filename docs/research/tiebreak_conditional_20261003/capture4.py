#!/usr/bin/env python3
"""Read-only capture for PROTOCOL_3.md: at every first decision since
2026-08-01, the market's mean bucket and its mode bucket, whether the lane's
own v2.1 predicate fires ON THAT BUCKET, its ask and whether it landed.
Writes ``data4.json``."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bayes_basket_target import tiebreak_cell_shadow as shadow  # noqa: E402
from bayes_basket_target import tiebreak_lane as lane  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.p1_consensus_lane import evaluate_consensus  # noqa: E402
import calibration_dashboard as board  # noqa: E402
import capture3  # noqa: E402

FLOOR = "2026-08-01"


def fire_on(bucket, envelope, market, book):
    """The lane's own v2.1 predicate, asked of ``bucket`` instead of the
    model's mode: a model vector with all its mass on that bucket has that
    bucket for its mode, and everything else in ``evaluate_rule`` is the
    market's."""

    stand_in = {str(b): (1.0 if str(b) == bucket else 0.0) for b in envelope["probabilities"]}
    return lane.evaluate_rule(
        stand_in, envelope["ordinals"], market["vector"],
        min_market_share=0.40, max_market_share=0.60, rule=lane.RULE_V21,
        spread=book["spread"], max_spread=0.031,
    )


def main() -> int:
    started = time.monotonic()
    conn = board._connect(shadow.PRODUCTION_SSOT_DB)
    labels = shadow.settled_buckets(conn)
    firsts = conn.execute(
        "SELECT station_id,event_date,stage,MIN(decision_at_utc),probability_envelope_id,"
        "ws_snapshot_id FROM basket_policy_training_fact WHERE event_date>=?"
        " GROUP BY station_id,event_date,stage", (FLOOR,),
    ).fetchall()
    firsts = [f for f in firsts if labels.get((str(f[0]), str(f[1]))) is not None]
    absent = [(str(f[5]), str(f[1])) for f in firsts if lane._snapshot(conn, str(f[5])) is None]
    print(f"[{time.monotonic() - started:6.1f}s] settled first decisions {len(firsts)};"
          f" snapshots not in the SSOT {len(absent)}; from the cold tier"
          f" {capture3.load_cold_snapshots(absent)}", flush=True)

    rows, unread = [], 0
    why = {}
    for station, day, stage, decided, envelope_id, snapshot_id in firsts:
        settled = labels[(str(station), str(day))]
        envelope = lane._envelope(conn, str(envelope_id)) or {}
        snapshot = (lane._snapshot(conn, str(snapshot_id))
                    or capture3.COLD_SNAPSHOTS.get(str(snapshot_id)) or {})
        if not envelope.get("probabilities") or not snapshot.get("buckets"):
            unread += 1
            key = "no_envelope" if not envelope.get("probabilities") else "no_snapshot"
            why[key] = why.get(key, 0) + 1
            continue
        market = market_reference_v1(snapshot["buckets"])
        if market.get("state") != "complete":
            unread += 1
            key = "market_" + str(market.get("state"))
            why[key] = why.get(key, 0) + 1
            continue
        reading = evaluate_consensus(envelope["probabilities"], envelope["ordinals"], market["vector"])
        if reading.get("state") != "read":
            unread += 1
            key = "consensus_" + str(reading.get("state"))
            why[key] = why.get(key, 0) + 1
            continue
        books = {str(r.get("bucket")): r for r in snapshot["buckets"]}
        entry = {
            "station_id": str(station), "event_date": str(day), "stage": str(stage),
            "decision_at_utc": str(decided), "settled_bucket": settled,
            "model_mode": str(reading["model_mode"]), "model_mean": str(reading["model_mean"]),
        }
        for name in ("market_mean", "market_mode"):
            bucket = str(reading[name])
            book = lane.book_features(books.get(bucket))
            fired = fire_on(bucket, envelope, market, book)
            entry[name] = {
                "bucket": bucket, "best_ask": book["best_ask"], "spread": book["spread"],
                "fired": bool(fired.get("fired")), "reason": fired.get("reason") or "",
                "market_share": fired.get("market_share_on_model_mode"),
                "hit": int(settled == bucket),
                "is_model_mode": bucket == str(reading["model_mode"]),
            }
        rows.append(entry)
    conn.close()
    print(f"unread reasons: {why}")
    print(f"[{time.monotonic() - started:6.1f}s] read {len(rows)}, unread {unread};"
          f" fired on the market mean {sum(1 for r in rows if r['market_mean']['fired'])},"
          f" on the market mode {sum(1 for r in rows if r['market_mode']['fired'])}", flush=True)
    (HERE / "data4.json").write_text(json.dumps({
        "schema": "tiebreak_conditional_capture4_v1", "captured_at_utc": shadow.utc_now(),
        "first_decisions": rows, "unread": why,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only capture for PROTOCOL_6.md: the four centres of every settled
first decision, each with the market's share, the model's probability, the
ask, the bid and whether it landed.  Writes ``data7.json``."""

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
CEILING = "2026-10-02"


def main() -> int:
    started = time.monotonic()
    conn = board._connect(shadow.PRODUCTION_SSOT_DB)
    labels = shadow.settled_buckets(conn)
    firsts = conn.execute(
        "SELECT station_id,event_date,stage,MIN(decision_at_utc),probability_envelope_id,"
        "ws_snapshot_id FROM basket_policy_training_fact WHERE event_date>=? AND event_date<=?"
        " GROUP BY station_id,event_date,stage", (FLOOR, CEILING),
    ).fetchall()
    firsts = [f for f in firsts if labels.get((str(f[0]), str(f[1]))) is not None]
    absent = [(str(f[5]), str(f[1])) for f in firsts if lane._snapshot(conn, str(f[5])) is None]
    capture3.load_cold_snapshots(absent)
    rows = []
    for station, day, stage, decided, envelope_id, snapshot_id in firsts:
        settled = labels[(str(station), str(day))]
        envelope = lane._envelope(conn, str(envelope_id)) or {}
        snapshot = (lane._snapshot(conn, str(snapshot_id))
                    or capture3.COLD_SNAPSHOTS.get(str(snapshot_id)) or {})
        if not envelope.get("probabilities") or not snapshot.get("buckets"):
            continue
        market = market_reference_v1(snapshot["buckets"])
        if market.get("state") != "complete":
            continue
        reading = evaluate_consensus(envelope["probabilities"], envelope["ordinals"], market["vector"])
        if reading.get("state") != "read":
            continue
        ords = {str(b): int(o) for b, o in envelope["ordinals"].items()}
        if settled not in ords:
            continue
        p = {str(b): float(v) for b, v in envelope["probabilities"].items() if str(b) in ords and float(v) >= 0.0}
        m = {str(b): float(v) for b, v in market["vector"].items() if str(b) in ords and float(v) >= 0.0}
        common = sorted(set(p) & set(m), key=lambda b: ords[b])
        tp, tm = sum(p[b] for b in common), sum(m[b] for b in common)
        books = {str(r.get("bucket")): r for r in snapshot["buckets"]}
        entry = {
            "station_id": str(station), "event_date": str(day), "stage": str(stage),
            "model_mu": reading["model_mu"], "market_mu": reading["market_mu"],
            "truth": ords[settled], "unit": "F" if "°F" in settled else "C",
        }
        for name in ("model_mode", "model_mean", "market_mode", "market_mean"):
            bucket = str(reading[name])
            book = lane.book_features(books.get(bucket))
            entry[name] = {
                "bucket": bucket, "ordinal": ords[bucket],
                "market_share": m[bucket] / tm, "model_probability": p[bucket] / tp,
                "best_ask": book["best_ask"], "best_bid": book["best_bid"],
                "hit": int(settled == bucket),
            }
        rows.append(entry)
    conn.close()
    print(f"[{time.monotonic() - started:6.1f}s] rows {len(rows)} of {len(firsts)}", flush=True)
    (HERE / "data7.json").write_text(json.dumps({
        "schema": "tiebreak_conditional_capture7_v1", "captured_at_utc": shadow.utc_now(), "rows": rows,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

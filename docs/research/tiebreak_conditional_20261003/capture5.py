#!/usr/bin/env python3
"""Read-only capture for PROTOCOL_4.md: every settled first decision since
2026-08-01 with the model's and the market's centre, mode, the probability
each gave the settled bucket, the hours to settlement and the envelope's
posterior version.  Writes ``data5.json``."""

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
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402
import calibration_dashboard as board  # noqa: E402
import capture3  # noqa: E402

FLOOR = "2026-08-01"


def main() -> int:
    started = time.monotonic()
    conn = board._connect(shadow.PRODUCTION_SSOT_DB)
    labels = shadow.settled_buckets(conn)
    firsts = conn.execute(
        "SELECT station_id,event_date,stage,MIN(decision_at_utc),probability_envelope_id,"
        "ws_snapshot_id,settlement_timezone,posterior_version FROM basket_policy_training_fact"
        " WHERE event_date>=? GROUP BY station_id,event_date,stage", (FLOOR,),
    ).fetchall()
    firsts = [f for f in firsts if labels.get((str(f[0]), str(f[1]))) is not None]
    absent = [(str(f[5]), str(f[1])) for f in firsts if lane._snapshot(conn, str(f[5])) is None]
    capture3.load_cold_snapshots(absent)
    print(f"[{time.monotonic() - started:6.1f}s] settled first decisions {len(firsts)}", flush=True)

    rows, skipped = [], {}
    for station, day, stage, decided, envelope_id, snapshot_id, zone, version in firsts:
        settled = labels[(str(station), str(day))]
        envelope = lane._envelope(conn, str(envelope_id)) or {}
        snapshot = (lane._snapshot(conn, str(snapshot_id))
                    or capture3.COLD_SNAPSHOTS.get(str(snapshot_id)) or {})
        if not envelope.get("probabilities") or not snapshot.get("buckets"):
            skipped["no_envelope_or_snapshot"] = skipped.get("no_envelope_or_snapshot", 0) + 1
            continue
        market = market_reference_v1(snapshot["buckets"])
        if market.get("state") != "complete":
            skipped["market_incomplete"] = skipped.get("market_incomplete", 0) + 1
            continue
        reading = evaluate_consensus(envelope["probabilities"], envelope["ordinals"], market["vector"])
        if reading.get("state") != "read":
            skipped["consensus_unread"] = skipped.get("consensus_unread", 0) + 1
            continue
        # The two vectors on the buckets they share -- the normalisation
        # evaluate_consensus reads its centres on; its modes are asserted.
        ords = {str(b): int(o) for b, o in envelope["ordinals"].items()}
        p = {str(b): float(v) for b, v in envelope["probabilities"].items() if str(b) in ords and float(v) >= 0.0}
        m = {str(b): float(v) for b, v in market["vector"].items() if str(b) in ords and float(v) >= 0.0}
        common = sorted(set(p) & set(m), key=lambda b: ords[b])
        tp, tm = sum(p[b] for b in common), sum(m[b] for b in common)
        pn = {b: p[b] / tp for b in common}
        mn = {b: m[b] / tm for b in common}
        assert max(common, key=lambda b: (pn[b], -ords[b])) == reading["model_mode"]
        assert max(common, key=lambda b: (mn[b], -ords[b])) == reading["market_mode"]
        if settled not in ords:
            skipped["settled_not_on_ladder"] = skipped.get("settled_not_on_ladder", 0) + 1
            continue
        books = {str(r.get("bucket")): r for r in snapshot["buckets"]}
        mean_bucket = str(reading["market_mean"])
        book = lane.book_features(books.get(mean_bucket))
        rows.append({
            "station_id": str(station), "event_date": str(day), "stage": str(stage),
            "decision_at_utc": str(decided), "posterior_version": str(version),
            "tau": hours_to_settlement(str(day), as_of_utc=str(decided), settlement_timezone=str(zone)),
            "unit": "F" if "°F" in settled else "C",
            "truth": ords[settled], "model_mu": reading["model_mu"], "market_mu": reading["market_mu"],
            "model_mode": ords[str(reading["model_mode"])],
            "market_mode": ords[str(reading["market_mode"])],
            "model_mean": ords[str(reading["model_mean"])],
            "market_mean": ords[mean_bucket],
            "p_model_truth": pn.get(settled, 0.0), "p_market_truth": mn.get(settled, 0.0),
            "p_model_on_model_mode": pn[str(reading["model_mode"])],
            "p_market_on_market_mean": mn[mean_bucket],
            "market_mean_spread": book["spread"], "market_mean_ask": book["best_ask"],
            "buckets": len(common),
        })
    conn.close()
    print(f"[{time.monotonic() - started:6.1f}s] rows {len(rows)}, skipped {skipped}", flush=True)
    (HERE / "data5.json").write_text(json.dumps({
        "schema": "tiebreak_conditional_capture5_v1", "captured_at_utc": shadow.utc_now(),
        "rows": rows, "skipped": skipped,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

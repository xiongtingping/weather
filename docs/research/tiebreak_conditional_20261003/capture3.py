#!/usr/bin/env python3
"""Read-only capture for PROTOCOL_2.md section C: the four centres of every
replay row and every live trade, by the consensus lane's own
``evaluate_consensus``, with each centre's ask and whether it landed.
Writes ``data3.json``."""

from __future__ import annotations

import json
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
from bayes_basket_target.p1_consensus_lane import evaluate_consensus  # noqa: E402
import calibration_dashboard as board  # noqa: E402

FLOOR = "2026-08-01"


#: Snapshots charter 238 moved to the cold tier (event days to 2026-09-01),
#: read once by event day through the offline reader -- never by production.
COLD_SNAPSHOTS: dict[str, dict] = {}


def load_cold_snapshots(keys) -> int:
    import ssot_cold_offload as cold  # noqa: PLC0415

    wanted = {snapshot_id: day for snapshot_id, day in keys}
    if not wanted:
        return 0
    conn = cold.open_cold_table("basket_official_ws_snapshot")
    try:
        for day in sorted(set(wanted.values())):
            for snapshot_id, buckets_json, observed_at, state in conn.execute(
                "SELECT snapshot_id,buckets_json,observed_at,state"
                " FROM basket_official_ws_snapshot WHERE event_date=?", (day,),
            ):
                if str(snapshot_id) in wanted:
                    COLD_SNAPSHOTS[str(snapshot_id)] = {
                        "buckets": json.loads(buckets_json or "[]") or [],
                        "observed_at": str(observed_at), "state": str(state),
                    }
    finally:
        conn.close()
    return len(COLD_SNAPSHOTS)


def centres(conn, envelope_id, snapshot_id, settled):
    envelope = lane._envelope(conn, str(envelope_id)) or {}
    snapshot = lane._snapshot(conn, str(snapshot_id)) or COLD_SNAPSHOTS.get(str(snapshot_id)) or {}
    if not envelope.get("probabilities") or not snapshot.get("buckets"):
        return None
    market = market_reference_v1(snapshot["buckets"])
    if market.get("state") != "complete":
        return None
    reading = evaluate_consensus(envelope["probabilities"], envelope["ordinals"], market["vector"])
    if reading.get("state") != "read":
        return None
    ordinals = {str(b): int(o) for b, o in envelope["ordinals"].items()}
    books = {str(r.get("bucket")): r for r in snapshot["buckets"]}
    out = {
        "model_mu": reading["model_mu"], "market_mu": reading["market_mu"],
        "four_same": reading["four_same"],
        "settled_ordinal": ordinals.get(settled),
    }
    for name in ("model_mode", "model_mean", "market_mode", "market_mean"):
        bucket = str(reading[name])
        book = lane.book_features(books.get(bucket))
        out[name] = {
            "bucket": bucket, "ordinal": ordinals[bucket], "best_ask": book["best_ask"],
            "hit": (int(settled == bucket) if settled is not None else None),
        }
    return out


def main() -> int:
    started = time.monotonic()
    data = json.loads((HERE / "data.json").read_text())
    conn = board._connect(shadow.PRODUCTION_SSOT_DB)
    firsts = {
        (str(s), str(d), str(stage)): (str(env), str(snap))
        for s, d, stage, _at, env, snap in conn.execute(
            "SELECT station_id,event_date,stage,MIN(decision_at_utc),probability_envelope_id,"
            "ws_snapshot_id FROM basket_policy_training_fact WHERE event_date>=?"
            " GROUP BY station_id,event_date,stage", (FLOOR,),
        ).fetchall()
    }
    absent = []
    for row in data["replay"]:
        ids = firsts.get((row["station_id"], row["event_date"], row["stage"]))
        if ids and lane._snapshot(conn, ids[1]) is None:
            absent.append((ids[1], row["event_date"]))
    print(f"[{time.monotonic() - started:6.1f}s] snapshots not in the SSOT: {len(absent)};"
          f" read from the cold tier: {load_cold_snapshots(absent)}", flush=True)
    replay, missing, mismatched = {}, 0, 0
    for row in data["replay"]:
        key = (row["station_id"], row["event_date"], row["stage"])
        ids = firsts.get(key)
        got = centres(conn, ids[0], ids[1], row["settled_bucket"]) if ids else None
        if got is None:
            missing += 1
            continue
        if got["model_mode"]["bucket"] != row["bucket"]:
            mismatched += 1
            continue
        replay["|".join(key)] = got
    print(f"[{time.monotonic() - started:6.1f}s] replay centres {len(replay)} of"
          f" {len(data['replay'])} (missing {missing}, mode mismatch {mismatched})", flush=True)

    by_key = {}
    for row in data["evaluations_fired"]:
        by_key.setdefault((row["station_id"], row["event_date"], row["stage"]), row)
    live = {}
    for trade in data["live"]["trades"]["rows"]:
        row = by_key.get((trade["station_id"], trade["event_date"], trade["stage"]))
        if not row:
            continue
        got = centres(conn, row["envelope_id"], row["snapshot_id"], trade.get("settled_bucket"))
        if got is not None:
            got["bought_is_model_mode"] = got["model_mode"]["bucket"] == trade["bucket"]
            live[str(trade["order_intent_id"])] = got
    conn.close()
    print(f"[{time.monotonic() - started:6.1f}s] live centres {len(live)} of"
          f" {len(data['live']['trades']['rows'])}", flush=True)
    (HERE / "data3.json").write_text(json.dumps({
        "schema": "tiebreak_conditional_capture3_v1", "captured_at_utc": shadow.utc_now(),
        "replay_centres": replay, "live_centres": live,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

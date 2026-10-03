#!/usr/bin/env python3
"""Read-only capture for PROTOCOL_2.md.  ``data.json`` is not re-taken; this
adds what the second pass needs and writes ``data2.json``:

* each station's settlement timezone;
* every order event of the lane's legs, with its time;
* for every first decision (station, day, stage) since 2026-08-01, fired or
  not, how many rungs the settled bucket sits from the model's mode bucket
  -- by the envelope's own ordinals and the lane's own ``evaluate_rule``.
"""

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
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402
import calibration_dashboard as board  # noqa: E402

FLOOR = "2026-08-01"


def main() -> int:
    started = time.monotonic()
    data = json.loads((HERE / "data.json").read_text())
    conn = board._connect(shadow.PRODUCTION_SSOT_DB)

    zones = {
        str(s): str(z) for s, z in conn.execute(
            "SELECT DISTINCT station_id,settlement_timezone FROM basket_policy_training_fact"
            " WHERE event_date>=?", ("2026-10-01",),
        ).fetchall()
    }
    print(f"[{time.monotonic() - started:6.1f}s] zones: {len(zones)}", flush=True)

    events = {}
    for leg in data["legs"]:
        events[leg["order_intent_id"]] = [
            {"event_type": str(t), "observed_at": str(at), "matched_size": float(size or 0.0)}
            for t, at, size in conn.execute(
                "SELECT event_type,observed_at,matched_size FROM basket_target_order_events"
                " WHERE order_intent_id=? ORDER BY observed_at", (leg["order_intent_id"],),
            ).fetchall()
        ]
    print(f"[{time.monotonic() - started:6.1f}s] leg events: {len(events)}", flush=True)

    labels = shadow.settled_buckets(conn)
    firsts = conn.execute(
        "SELECT station_id,event_date,stage,MIN(decision_at_utc),probability_envelope_id"
        " FROM basket_policy_training_fact WHERE event_date>=?"
        " GROUP BY station_id,event_date,stage", (FLOOR,),
    ).fetchall()
    offsets = []
    for station, day, stage, decided, envelope_id in firsts:
        settled = labels.get((str(station), str(day)))
        if settled is None:
            continue
        envelope = lane._envelope(conn, str(envelope_id)) or {}
        probabilities, ordinals = envelope.get("probabilities"), envelope.get("ordinals")
        if not probabilities or not ordinals or settled not in ordinals:
            continue
        reading = lane.evaluate_rule(
            probabilities, ordinals, probabilities, min_market_share=0.0, rule=lane.RULE_V2,
        )
        mode = reading.get("model_mode")
        if not mode:
            continue
        offsets.append({
            "station_id": str(station), "event_date": str(day), "stage": str(stage),
            "decision_at_utc": str(decided), "model_mode": str(mode), "settled_bucket": settled,
            "offset": int(ordinals[settled]) - int(ordinals[mode]),
        })
    print(f"[{time.monotonic() - started:6.1f}s] first-decision offsets: {len(offsets)}"
          f" of {len(firsts)}", flush=True)

    # The live trades' own miss, by the envelope the lane judged them on.
    by_key = {}
    for row in data["evaluations_fired"]:
        by_key.setdefault((row["station_id"], row["event_date"], row["stage"]), row)
    trade_extra = {}
    for trade in data["live"]["trades"]["rows"]:
        row = by_key.get((trade["station_id"], trade["event_date"], trade["stage"]))
        extra = {"offset": None, "tau": None}
        if row:
            envelope = lane._envelope(conn, str(row["envelope_id"])) or {}
            ordinals = envelope.get("ordinals") or {}
            settled = trade.get("settled_bucket")
            if settled in ordinals and trade["bucket"] in ordinals:
                extra["offset"] = int(ordinals[settled]) - int(ordinals[trade["bucket"]])
            zone = zones.get(trade["station_id"])
            if zone:
                extra["tau"] = hours_to_settlement(
                    trade["event_date"], as_of_utc=row["decision_at_utc"],
                    settlement_timezone=zone,
                )
        trade_extra[str(trade["order_intent_id"])] = extra
    conn.close()

    (HERE / "data2.json").write_text(json.dumps({
        "schema": "tiebreak_conditional_capture2_v1", "captured_at_utc": shadow.utc_now(),
        "zones": zones, "leg_events": events, "first_decision_offsets": offsets,
        "trade_extra": trade_extra,
    }, ensure_ascii=False))
    print(f"[{time.monotonic() - started:6.1f}s] written data2.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""P0 / P0-M change events with the book just after each (PROTOCOL_MODEL_BELIEF.md).

    python3 build_p0_events.py OUT.jsonl.gz
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import sys
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from build_events import ladder, ts, iso  # noqa: E402

SSOT = f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro"
FIRST_DAY, LAST_DAY = "2026-08-25", "2026-09-23"


def main() -> None:
    out_path = Path(sys.argv[1])
    db = sqlite3.connect(SSOT, uri=True, timeout=60)
    labels = db.execute(
        "SELECT station_id, event_date FROM basket_station_day_settlement_label"
        " WHERE event_date BETWEEN ? AND ? GROUP BY station_id, event_date ORDER BY event_date, station_id",
        (FIRST_DAY, LAST_DAY)).fetchall()
    started, n = time.monotonic(), 0
    with gzip.open(out_path, "wt") as fh:
        for st, day in labels:
            envs = db.execute(
                "SELECT stage, decision_at_utc, probabilities_json FROM basket_probability_envelope"
                " WHERE station_id=? AND event_date=? AND stage IN ('P0','P0-M') AND state='COMMITTED'"
                " ORDER BY decision_at_utc", (st, day)).fetchall()
            snaps = db.execute(
                "SELECT observed_at, snapshot_id FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
                " ORDER BY observed_at", (st, day)).fetchall()
            if not envs or not snaps:
                continue
            snap_t = [s[0] for s in snaps]
            last = {}
            for stage, decided, probs in envs:
                if last.get(stage) == probs:
                    continue
                last[stage] = probs
                chosen = None
                for lag in (5, 15):
                    i = bisect_right(snap_t, iso(ts(decided) + timedelta(minutes=lag)))
                    if i > 0 and snaps[i - 1][0] >= decided:
                        chosen = snaps[i - 1]
                        break
                if chosen is None:
                    continue
                bj = json.loads(db.execute("SELECT buckets_json FROM basket_official_ws_snapshot WHERE snapshot_id=?", (chosen[1],)).fetchall()[0][0])
                ref = market_reference_v1(bj)
                fh.write(json.dumps({
                    "st": st, "day": day, "stage": stage, "t": decided, "belief": json.loads(probs),
                    "book_at": chosen[0], "m": ref.get("vector") if ref.get("state") == "complete" else None,
                    "asks": {b["bucket"]: ladder(b.get("ask_levels"), False) for b in bj},
                    "bids": {b["bucket"]: ladder(b.get("bid_levels"), True) for b in bj},
                    "min_order": {b["bucket"]: float(b.get("min_order_size") or 5.0) for b in bj},
                }, ensure_ascii=False) + "\n")
                n += 1
    print(f"events {n}; {time.monotonic()-started:.0f}s")


if __name__ == "__main__":
    main()

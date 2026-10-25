#!/usr/bin/env python3
"""Collect the A/B/C/D experiment's records (PROTOCOL.md), SSOT read-only.

One record per station-day x P0 family (first decision beyond 36 h, first within
12-36 h): the ensemble-only vector (the served P0 before the consensus shift),
the ten deterministic systems' day maxima knowable at that decision, the hours
to settlement and the settled truth.  Written as one pickle beside this file.

    python3 docs/research/det_ens_blend_20261025/collect.py [--days 60]
"""

from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import fit_deterministic_consensus_shift as FIT  # noqa: E402
from bayes_basket_target import deterministic_consensus_shift as DCS  # noqa: E402
from bayes_basket_target.regional_point_lane import runs_for  # noqa: E402
from bayes_basket_target.settlement_time import hours_to_settlement  # noqa: E402

OUT = Path(__file__).resolve().parent / "records.pkl"


def collect(days: int) -> tuple[list[dict], dict]:
    conn = sqlite3.connect(f"file:{FIT.DB}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    today = datetime.now(timezone.utc).date()
    start, end = (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()
    stations = [r[0] for r in conn.execute("SELECT station_id FROM calibration_station_registry ORDER BY station_id")]
    out: list[dict] = []
    counts = {"station_days": 0, "picked": 0, "no_winner": 0, "pre_shift": 0}
    for station in stations:
        truth = FIT._truth(conn, station, start, end)
        for day in sorted(truth):
            picked = [(f, row) for f, row in FIT._first_per_family(conn, station, day) if f in ("P0_far", "P0_near")]
            if not picked:
                continue
            counts["station_days"] += 1
            rows = runs_for(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
            for family, (dec, tz, pj, oj, ivj, _m, _p, consensus_json) in picked:
                counts["picked"] += 1
                served = {k: float(v) for k, v in json.loads(pj).items()}
                pre = FIT._pre_shift(consensus_json)
                if pre is not None:
                    served = pre
                    counts["pre_shift"] += 1
                ordinals, intervals = json.loads(oj), json.loads(ivj)
                ordered = sorted(ordinals, key=lambda k: ordinals[k])
                winner = FIT._winning_bucket(official_tmax_c=truth[day], bucket_intervals=intervals)
                if winner is None or winner not in served:
                    counts["no_winner"] += 1
                    continue
                read = DCS.consensus_read(conn, station_id=station, event_date=day, as_of_utc=dec,
                                          settlement_timezone=tz, rows=rows)
                systems = {k: float(v["day_max_c"]) for k, v in (read.get("systems") or {}).items()}
                out.append({
                    "station": station, "day": day, "family": family, "decision_at_utc": dec,
                    "hours": float(hours_to_settlement(day, as_of_utc=dec, settlement_timezone=tz)),
                    "p": [served[k] for k in ordered], "ordered": ordered,
                    "intervals": [intervals[k] for k in ordered], "winner": ordered.index(winner),
                    "y_c": truth[day], "systems": systems,
                })
    conn.close()
    return out, counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()
    t0 = time.time()
    records, counts = collect(args.days)
    OUT.write_bytes(pickle.dumps(records))
    print(json.dumps({**counts, "records": len(records), "seconds": round(time.time() - t0, 1)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

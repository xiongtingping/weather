#!/usr/bin/env python3
"""PROTOCOL 补记一: records for the P0 decisions within 12 h of settlement.

Per station-day the first P0 decided under 12 h to settlement (``lt12_first``)
and the day's last P0 (``lt12_last``, also under 12 h; one record when they are
the same decision).  Same record shape as ``collect.py``; SSOT read-only.

    python3 docs/research/det_ens_blend_20261025/collect_lt12.py [--days 60]
"""

from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from collect import DCS, FIT, Path, hours_to_settlement, runs_for

OUT = Path(__file__).resolve().parent / "records_lt12.pkl"


def collect(days: int) -> tuple[list[dict], dict]:
    conn = sqlite3.connect(f"file:{FIT.DB}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    today = datetime.now(timezone.utc).date()
    start, end = (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()
    stations = [r[0] for r in conn.execute("SELECT station_id FROM calibration_station_registry ORDER BY station_id")]
    out: list[dict] = []
    counts = {"station_days": 0, "first": 0, "last": 0, "same": 0, "pre_shift": 0}
    for station in stations:
        truth = FIT._truth(conn, station, start, end)
        for day in sorted(truth):
            under = []
            for row in conn.execute(FIT.ENVELOPES_SQL, (station, day, "P0", -1)).fetchall():
                try:
                    hours = float(hours_to_settlement(day, as_of_utc=row[0], settlement_timezone=row[1]))
                except Exception:  # noqa: BLE001
                    continue
                if 0.0 < hours < 12.0:
                    under.append((hours, row))
            if not under:
                continue
            counts["station_days"] += 1
            picks = [("lt12_first", under[0])]
            if under[-1][1][0] != under[0][1][0]:
                picks.append(("lt12_last", under[-1]))
            else:
                counts["same"] += 1
            rows = runs_for(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
            for tag, (hours, (dec, tz, pj, oj, ivj, _m, _p, consensus_json)) in picks:
                counts["first" if tag == "lt12_first" else "last"] += 1
                served = {k: float(v) for k, v in json.loads(pj).items()}
                pre = FIT._pre_shift(consensus_json)
                if pre is not None:
                    served = pre
                    counts["pre_shift"] += 1
                ordinals, intervals = json.loads(oj), json.loads(ivj)
                ordered = sorted(ordinals, key=lambda k: ordinals[k])
                winner = FIT._winning_bucket(official_tmax_c=truth[day], bucket_intervals=intervals)
                if winner is None or winner not in served:
                    continue
                read = DCS.consensus_read(conn, station_id=station, event_date=day, as_of_utc=dec,
                                          settlement_timezone=tz, rows=rows)
                out.append({
                    "station": station, "day": day, "family": tag, "decision_at_utc": dec, "hours": hours,
                    "p": [served[k] for k in ordered], "ordered": ordered,
                    "intervals": [intervals[k] for k in ordered], "winner": ordered.index(winner),
                    "y_c": truth[day],
                    "systems": {k: float(v["day_max_c"]) for k, v in (read.get("systems") or {}).items()},
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

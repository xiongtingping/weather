#!/usr/bin/env python3
"""Records for the NGR rebuild experiment (PROTOCOL.md), SSOT read-only.

Per station-day the first P0 decision in each band (>36 h, 12-36 h, <12 h): the
served ensemble vector (before the consensus shift), each seated system's
calibrated vector and family, the radiation standard score, the ten
deterministic day maxima knowable then, the hours to settlement and the truth.

    python3 docs/research/ngr_rebuild_20261025/collect.py [--days 60]
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
SQL = (
    "SELECT decision_at_utc, settlement_timezone, probabilities_json, bucket_ordinals_json,"
    " json_extract(lineage_json,'$.bucket_intervals_c_json'),"
    " json_extract(lineage_json,'$.deterministic_consensus_json'),"
    " json_extract(lineage_json,'$.native_system_probabilities_json'),"
    " json_extract(lineage_json,'$.system_family_ids_json'),"
    " json_extract(lineage_json,'$.synoptic_cooling_json')"
    " FROM basket_probability_envelope INDEXED BY basket_probability_envelope_asof_idx"
    " WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P0'"
    " ORDER BY decision_at_utc"
)


def band(hours: float) -> str | None:
    if hours > 36.0:
        return "far"
    if hours >= 12.0:
        return "near"
    if hours > 0.0:
        return "lt12"
    return None


def collect(days: int) -> tuple[list[dict], dict]:
    conn = sqlite3.connect(f"file:{FIT.DB}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")
    today = datetime.now(timezone.utc).date()
    start, end = (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()
    stations = [r[0] for r in conn.execute("SELECT station_id FROM calibration_station_registry ORDER BY station_id")]
    out: list[dict] = []
    counts = {"station_days": 0, "picked": 0, "no_systems": 0, "no_winner": 0}
    for station in stations:
        truth = FIT._truth(conn, station, start, end)
        for day in sorted(truth):
            picked: dict[str, tuple] = {}
            for row in conn.execute(SQL, (station, day)):
                try:
                    hours = float(hours_to_settlement(day, as_of_utc=row[0], settlement_timezone=row[1]))
                except Exception:  # noqa: BLE001
                    continue
                b = band(hours)
                if b is not None and b not in picked:
                    picked[b] = (hours, row)
                if len(picked) == 3:
                    break
            if not picked:
                continue
            counts["station_days"] += 1
            rows = runs_for(conn, station_id=station, event_date=day, as_of_utc="9999-12-31T23:59:59Z")
            for b, (hours, (dec, tz, pj, oj, ivj, cj, sysj, famj, radj)) in picked.items():
                counts["picked"] += 1
                served = {k: float(v) for k, v in json.loads(pj).items()}
                pre = FIT._pre_shift(cj)
                if pre is not None:
                    served = pre
                ordinals, intervals = json.loads(oj), json.loads(ivj)
                ordered = sorted(ordinals, key=lambda k: ordinals[k])
                try:
                    per_system = json.loads(sysj) if sysj else {}
                    family = json.loads(famj) if famj else {}
                except ValueError:
                    per_system, family = {}, {}
                vectors = {}
                for sid, vec in per_system.items():
                    if isinstance(vec, dict) and set(vec) >= set(ordered):
                        vectors[str(family.get(sid, sid))] = [float(vec[k]) for k in ordered]
                if not vectors:
                    counts["no_systems"] += 1
                    continue
                winner = FIT._winning_bucket(official_tmax_c=truth[day], bucket_intervals=intervals)
                if winner is None or winner not in served:
                    counts["no_winner"] += 1
                    continue
                try:
                    rad = json.loads(radj) if radj else {}
                except ValueError:
                    rad = {}
                z = rad.get("standardised") if isinstance(rad, dict) else None
                read = DCS.consensus_read(conn, station_id=station, event_date=day, as_of_utc=dec,
                                          settlement_timezone=tz, rows=rows)
                out.append({
                    "station": station, "day": day, "family": b, "decision_at_utc": dec, "hours": hours,
                    "p": [served[k] for k in ordered], "ordered": ordered,
                    "intervals": [intervals[k] for k in ordered], "winner": ordered.index(winner),
                    "y_c": truth[day], "family_vectors": vectors,
                    "rad_z": float(z) if isinstance(z, (int, float)) else 0.0,
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

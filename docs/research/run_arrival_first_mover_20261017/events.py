"""Every rule-R event (PROTOCOL.md 1-2) for settled station-days in a date
range, with its |z| so the evaluation can apply a threshold and dedupe.
Read-only; uses only append-only, time-stamped records, so a later run over
a day reproduces what a live recorder would have seen.

    uv run python docs/research/run_arrival_first_mover_20261017/events.py SINCE UNTIL OUT.jsonl.gz
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from bayes_basket_target import direction_signal as ds  # noqa: E402
from bayes_basket_target import run_change  # noqa: E402
from bayes_basket_target.basket_kernel.valuation_v1 import market_reference_v1  # noqa: E402
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL  # noqa: E402
from bayes_basket_target.station_day_board import bucket_of, ladder_axis  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
FEE = 0.05
FIRST_WITHIN = timedelta(minutes=10)
MIN_Z = 1.0
LEAD_H = (0.0, 48.0)


def _t(s) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _z(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def main(since: str, until: str, out_path: str) -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=1")
    truth = {}
    for s, d, v in conn.execute(
        "SELECT station_id, local_date, official_tmax_c FROM calibration_station_daily_high_truth"
        f" WHERE source_lane IN {FORMAL_TRUTH_LANES_SQL} AND qc_status='ACCEPTED' AND local_date BETWEEN ? AND ?"
        " ORDER BY revision_no", (since, until),
    ):
        truth[(str(s), str(d))] = float(v)
    scopes = conn.execute(
        "SELECT DISTINCT station_id, event_date, settlement_timezone FROM basket_official_ws_snapshot"
        " WHERE state='COMMITTED' AND event_date BETWEEN ? AND ?", (since, until)).fetchall()
    spread = {system: sd for _n, system, _b, sd in ds.MOMENTUM_COMPONENTS}
    back = {system: b for _n, system, b, _sd in ds.MOMENTUM_COMPONENTS}
    counts = {"scopes": len(scopes), "unsettled": 0, "no_axis": 0, "events": 0, "entered": 0}
    with gzip.open(out_path, "wt", encoding="utf-8") as out:
        for station, day, tz in sorted(scopes):
            if (station, day) not in truth:
                counts["unsettled"] += 1
                continue
            env = conn.execute(
                "SELECT bucket_ordinals_json, json_extract(lineage_json,'$.bucket_intervals_c_json')"
                " FROM basket_probability_envelope WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                " AND json_extract(lineage_json,'$.bucket_intervals_c_json') IS NOT NULL LIMIT 1",
                (station, day)).fetchone()
            if env is None:
                counts["no_axis"] += 1
                continue
            axis = ladder_axis(json.loads(env[0]), json.loads(env[1]))
            truth_bucket = bucket_of(axis, truth[(station, day)])
            end = datetime.combine(date.fromisoformat(day) + timedelta(days=1), time(0),
                                   ZoneInfo(tz)).astimezone(timezone.utc)
            runs = run_change.family_runs(conn, station_id=station, event_date=day, systems=ds.MOMENTUM_SYSTEMS)
            for system, rs in runs.items():
                k = back[system]
                for i in range(k, len(rs)):
                    had, init, value = rs[i]
                    delta = float(value) - float(rs[i - k][2])
                    z = delta / spread[system]
                    lead = (end - had).total_seconds() / 3600
                    if abs(z) < MIN_Z or not LEAD_H[0] <= lead <= LEAD_H[1]:
                        continue
                    counts["events"] += 1
                    ev = {"station_id": station, "event_date": day, "system": system, "run_init": str(init),
                          "had": _z(had), "z": round(z, 4), "lead_h": round(lead, 2), "entry": None}
                    row = conn.execute(
                        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=?"
                        " AND event_date=? AND state='COMMITTED' AND observed_at>=? AND observed_at<=?"
                        " ORDER BY observed_at LIMIT 1", (station, day, _z(had), _z(had + FIRST_WITHIN))).fetchone()
                    if row is not None:
                        buckets = json.loads(row[1])
                        ref = market_reference_v1(buckets)
                        if ref.get("state") == "complete":
                            vec = {str(b): float(v) for b, v in ref["vector"].items()}
                            order = sorted(buckets, key=lambda b: int(b["ordinal"]))
                            labels = [str(b["bucket"]) for b in order]
                            mode = max(labels, key=lambda b: (vec.get(b, 0.0), -labels.index(b)))
                            j = labels.index(mode) + (1 if delta > 0 else -1)
                            if 0 <= j < len(labels):
                                asks = [float(x["price"]) for x in order[j].get("ask_levels") or []
                                        if float(x.get("shares") or 0) > 0]
                                if asks and 0 < min(asks) < 1:
                                    a = min(asks)
                                    hit = int(labels[j] == truth_bucket)
                                    cost = a + FEE * a * (1 - a)
                                    ev["entry"] = {"at": str(row[0]), "bucket": labels[j], "ask": a, "hit": hit,
                                                   "cost": round(cost, 6), "pnl": round(hit - cost, 6)}
                                    counts["entered"] += 1
                    out.write(json.dumps(ev, ensure_ascii=False) + "\n")
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:4]))

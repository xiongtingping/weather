"""Re-run the bias-window sweep on the scopes production actually serves.

The session's first attempt at this measured almost nothing about the NWP
families.  ``archive_lead_bucket`` carries two vocabularies at once -- the
retired d0/d1/d2 and the hour buckets production fits on -- and grouping
without filtering split every scope across both, halving its samples until a
minimum-training filter dropped it.  What survived was the TAF and METAR
lanes, which is not what P0's centre is made of.

This restricts to the hour-bucket vocabulary and to the ensemble families,
then walks each scope forward day by day: fit the bias on the trailing
window under several half-lives, score the day that follows, never look at
it first.

Reported per arm: the systematic bias that survives (the thing a centre cares
about) and the mean absolute error (the cost of chasing).
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

HOUR_BUCKETS = ("h00_12", "h12_24", "h24_48", "h48_plus")
POINT_FORECASTS = ("OFFICIAL_AIRPORT_TAF_TX", "METAR_PHYSICAL_PATH_TMAX")

WINDOW = 60
LAG = 2
MIN_TRAIN = 12
HALF_LIVES = (5.0, 10.0, 15.0, 25.0)

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)

truth: dict[tuple[str, str], float] = {}
for station, day, value in conn.execute(
    "SELECT station_id, local_date, official_tmax_c"
    " FROM calibration_station_daily_high_truth t"
    " WHERE revision_no = (SELECT MAX(revision_no)"
    "   FROM calibration_station_daily_high_truth x"
    "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
):
    truth[(str(station), str(day))] = float(value)

placeholders = ",".join("?" for _ in HOUR_BUCKETS)
streams: dict[tuple[str, ...], dict[str, float]] = defaultdict(dict)
for system, station, day, lead, cycle, forecast in conn.execute(
    "SELECT ensemble_system_id, station_id, target_local_date,"
    " archive_lead_bucket, run_cycle, AVG(raw_tmax_c)"
    " FROM calibration_native_ensemble_historical_member_run"
    f" WHERE qc_status='ACCEPTED' AND archive_lead_bucket IN ({placeholders})"
    " GROUP BY 1,2,3,4,5",
    HOUR_BUCKETS,
):
    if str(system) in POINT_FORECASTS:
        continue
    actual = truth.get((str(station), str(day)))
    if actual is None or forecast is None:
        continue
    key = (str(system), str(station), str(lead), str(cycle))
    streams[key][str(day)] = actual - float(forecast)

print(f"NWP scopes on the hour vocabulary: {len(streams)}")
print(f"  station-days across them:"
      f" {sum(len(v) for v in streams.values())}")

epoch = date(2026, 1, 1)
DECISION_DAYS = [
    (date(2026, 8, 1) + timedelta(days=k)).isoformat() for k in range(26)
]
STREAK = {
    (date(2026, 8, 17) + timedelta(days=k)).isoformat() for k in range(10)
}


def weighted(history, half_life):
    anchor = history[-1][0]
    weights = [0.5 ** ((anchor - index) / half_life) for index, _e in history]
    total = sum(weights)
    return sum(w * e for w, (_i, e) in zip(weights, history)) / total


scored: dict[str, dict[str, list[float]]] = {
    slice_name: defaultdict(list) for slice_name in ("all", "streak")
}
coverage: dict[str, set] = {"all": set(), "streak": set()}

for key, series in streams.items():
    days = sorted(series)
    index = {d: (date.fromisoformat(d) - epoch).days for d in days}
    for target in DECISION_DAYS:
        if target not in series:
            continue
        stamp = date.fromisoformat(target)
        low = (stamp - timedelta(days=WINDOW + LAG) - epoch).days
        high = (stamp - timedelta(days=LAG) - epoch).days
        history = [
            (index[d], series[d]) for d in days if low <= index[d] <= high
        ]
        if len(history) < MIN_TRAIN:
            continue
        error = series[target]
        slices = ["all"] + (["streak"] if target in STREAK else [])
        for slice_name in slices:
            coverage[slice_name].add((key[0], key[1], target))
            scored[slice_name]["RAW"].append(error)
            scored[slice_name]["EQ"].append(
                error - mean(e for _i, e in history)
            )
            for half_life in HALF_LIVES:
                scored[slice_name][f"H{int(half_life)}"].append(
                    error - weighted(history, half_life)
                )

for slice_name in ("all", "streak"):
    rows = scored[slice_name]
    if not rows["RAW"]:
        print(f"\n== {slice_name}: no coverage ==")
        continue
    systems = {s for s, _st, _d in coverage[slice_name]}
    print(f"\n== {slice_name} == n={len(rows['RAW'])}"
          f"  systems={len(systems)}  {sorted(systems)}")
    for arm in ["RAW", "EQ"] + [f"H{int(h)}" for h in HALF_LIVES]:
        values = rows[arm]
        print(f"  {arm:5s} 系统性偏差 {mean(values):+.3f}C"
              f"   MAE {mean(abs(v) for v in values):.3f}C")

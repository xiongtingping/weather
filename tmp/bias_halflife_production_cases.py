"""Sweep the bias half-life on production's own cases and own fit function.

Two earlier attempts this session measured the wrong rows -- once by pooling
the retired d0/d1/d2 lead vocabulary with the hour buckets, once by dropping
the authority, holdout and transport filters the real loader applies.  Both
answered a question about the TAF and METAR lanes while appearing to answer
one about the ensemble families.

So nothing is rebuilt here.  ``_load_cases`` supplies the cases exactly as
the fit sees them, ``_fit_parameters`` does the fitting, and the only thing
that varies between arms is ``BIAS_HALF_LIFE_DAYS``.  An arm that disagrees
with production therefore disagrees about the constant and nothing else.

Walk-forward and strictly as-of: for each scope and each target day, fit on
that scope's cases strictly before the day, then score the day.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import native_ensemble_oos as OOS

HALF_LIVES = (5.0, 7.0, 10.0, 15.0, 25.0)
MIN_TRAIN = 10
WINDOW = OOS.ROLLING_TRAIN_WINDOW_DAYS
POINT = set(OOS.POINT_FORECAST_SYSTEMS)

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=120.0,
)
stations = [
    row[0] for row in conn.execute(
        "SELECT DISTINCT station_id FROM calibration_station_registry"
        " ORDER BY station_id"
    )
]
as_of = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

truth: dict[tuple[str, str], float] = {}
for station, day, value in conn.execute(
    "SELECT station_id, local_date, official_tmax_c"
    " FROM calibration_station_daily_high_truth t"
    " WHERE revision_no = (SELECT MAX(revision_no)"
    "   FROM calibration_station_daily_high_truth x"
    "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
):
    truth[(str(station), str(day))] = float(value)

STREAK = {
    (date(2026, 8, 17) + timedelta(days=k)).isoformat() for k in range(10)
}

scored: dict[str, dict[str, list[float]]] = {
    name: defaultdict(list) for name in ("all", "streak")
}
seen_systems: set[str] = set()
scope_count = 0
loaded = 0

for station in stations:
    try:
        by_scope = OOS._load_cases(conn, station_id=station, as_of_utc=as_of)
    except Exception:
        continue
    loaded += 1
    for scope, cases in by_scope.items():
        system_id = str(scope[0])
        if system_id in POINT:
            continue
        dated = []
        for case in cases:
            day = str(case.get("event_date") or "")[:10]
            actual = truth.get((station, day))
            if actual is None or not case.get("values"):
                continue
            values = [float(v) for v in case["values"]]
            dated.append((day, case, actual - sum(values) / len(values)))
        if len(dated) < MIN_TRAIN + 1:
            continue
        dated.sort(key=lambda item: item[0])
        scope_count += 1
        for index, (day, _case, error) in enumerate(dated):
            if not day.startswith("2026-08"):
                continue
            floor = (
                date.fromisoformat(day) - timedelta(days=WINDOW)
            ).isoformat()
            history = [
                item[1] for item in dated[:index] if item[0] >= floor
            ]
            if len(history) < MIN_TRAIN:
                continue
            seen_systems.add(system_id)
            slices = ["all"] + (["streak"] if day in STREAK else [])
            for name in slices:
                scored[name]["RAW"].append(error)
            for half_life in HALF_LIVES:
                original = OOS.BIAS_HALF_LIFE_DAYS
                OOS.BIAS_HALF_LIFE_DAYS = half_life
                try:
                    fitted = OOS._fit_parameters(history)
                finally:
                    OOS.BIAS_HALF_LIFE_DAYS = original
                residual = error - float(fitted["bias_c"])
                for name in slices:
                    scored[name][f"H{int(half_life)}"].append(residual)

print(f"stations loaded {loaded}/{len(stations)}, scopes scored"
      f" {scope_count}, systems {sorted(seen_systems)}")

for name in ("all", "streak"):
    rows = scored[name]
    if not rows.get("RAW"):
        print(f"\n== {name}: no coverage ==")
        continue
    print(f"\n== {name} == n={len(rows['RAW'])}")
    base_mae = None
    for arm in ["RAW"] + [f"H{int(h)}" for h in HALF_LIVES]:
        values = rows[arm]
        if not values:
            continue
        bias = mean(values)
        mae = mean(abs(v) for v in values)
        if arm == "H15":
            base_mae = mae
        print(f"  {arm:5s} 系统性偏差 {bias:+.3f}C   MAE {mae:.3f}C")
    if base_mae:
        print("  (相对 H15：", ", ".join(
            f"{arm} MAE {100*(mean(abs(v) for v in rows[arm])-base_mae)/base_mae:+.1f}%"
            for arm in [f"H{int(h)}" for h in HALF_LIVES] if rows[arm]
        ), ")")

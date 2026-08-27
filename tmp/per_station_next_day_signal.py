"""Is there a per-station next-day signal, once the fleet's is removed?

The fleet arm failed because one number was applied to forty-five stations
that were not all wrong in the same direction.  The obvious repair is to give
each station its own number -- but that only works if a station's own
yesterday says something about its own today, and a single station-day is one
sample where the fleet mean was forty-five.

So decompose the residual:

    r[s,d] = fleet[d] + station[s] + interaction[s,d]

``fleet[d]`` is what the rejected arm used.  ``station[s]`` is what the
existing per-scope ``bias_c`` already fits, slowly.  Everything left is
``interaction[s,d]`` -- this station being unusually wrong *today*, relative
to both its own habit and the fleet's day.  A per-station next-day layer is
worth building only if that term persists into tomorrow.

Reported for each level: pooled lag-1 autocorrelation over consecutive
station-days, and how much of the total variance it carries.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.center_width_store import _truth, serving_cells_by_day
from bayes_basket_target.fleet_regime_shift import cell_residual_c

STAGE = sys.argv[1] if len(sys.argv) > 1 else "P0"
SINCE = sys.argv[2] if len(sys.argv) > 2 else "2026-08-05T00:00:00Z"

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)
truth = _truth(conn)
by_day = serving_cells_by_day(
    conn, truth, stage=STAGE, min_decision_at_utc=SINCE, pre_band=False
)

# One residual per station-day: average the station's own cells first, so a
# station that re-decided all day does not outvote one that decided once.
resid: dict[tuple[str, str], float] = {}
for day, cells in by_day.items():
    per_station: dict[str, list[float]] = defaultdict(list)
    for cell in cells:
        actual = truth.get((cell["station_id"], cell["event_date"]))
        if actual is None:
            continue
        value = cell_residual_c(cell, actual)
        if value is not None:
            per_station[str(cell["station_id"])].append(value)
    for station, values in per_station.items():
        resid[(station, str(day))] = sum(values) / len(values)

days = sorted({d for _s, d in resid})
stations = sorted({s for s, _d in resid})
print(f"stage {STAGE}: {len(resid)} station-days,"
      f" {len(stations)} stations, {len(days)} days"
      f" ({days[0]}..{days[-1]})")

fleet = {
    day: mean([resid[(s, day)] for s in stations if (s, day) in resid])
    for day in days
}
station_mean = {
    s: mean([resid[(s, d)] for d in days if (s, d) in resid])
    for s in stations
}

levels = {
    "raw  r[s,d]": lambda s, d: resid[(s, d)],
    "minus fleet day": lambda s, d: resid[(s, d)] - fleet[d],
    "minus fleet and station": (
        lambda s, d: resid[(s, d)] - fleet[d] - station_mean[s]
    ),
}


def lag1(values: list[tuple[float, float]]) -> tuple[float, int]:
    if len(values) < 8:
        return float("nan"), len(values)
    xs = [a for a, _ in values]
    ys = [b for _, b in values]
    mx, my = mean(xs), mean(ys)
    cov = mean((a - mx) * (b - my) for a, b in values)
    sx = math.sqrt(mean((a - mx) ** 2 for a in xs))
    sy = math.sqrt(mean((b - my) ** 2 for b in ys))
    return (cov / (sx * sy) if sx * sy > 0 else float("nan")), len(values)


print("\nlevel                      variance   lag-1 r   pairs"
      "   sign persists")
for label, transform in levels.items():
    pairs: list[tuple[float, float]] = []
    values: list[float] = []
    for s in stations:
        for d in days:
            if (s, d) not in resid:
                continue
            values.append(transform(s, d))
            nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
            if (s, nxt) in resid and nxt in fleet:
                pairs.append((transform(s, d), transform(s, nxt)))
    r, n = lag1(pairs)
    same = sum(1 for a, b in pairs if a * b > 0)
    print(f"  {label:24s} {mean(v*v for v in values):7.3f}"
          f"   {r:+6.2f}   {n:5d}   {same}/{len(pairs)}"
          f" = {100*same/max(len(pairs),1):.0f}%")

# The fleet's own day-to-day signal, for the comparison that matters: this is
# the term the rejected arm rode, measured the same way.
fleet_pairs = [
    (fleet[d], fleet[(date.fromisoformat(d) + timedelta(days=1)).isoformat()])
    for d in days
    if (date.fromisoformat(d) + timedelta(days=1)).isoformat() in fleet
]
r, n = lag1(fleet_pairs)
same = sum(1 for a, b in fleet_pairs if a * b > 0)
print(f"  {'fleet[d] itself':24s} {mean(v*v for v in fleet.values()):7.3f}"
      f"   {r:+6.2f}   {n:5d}   {same}/{len(fleet_pairs)}"
      f" = {100*same/max(len(fleet_pairs),1):.0f}%")

total = mean(resid[k] ** 2 for k in resid)
fleet_var = mean(fleet[d] ** 2 for _s, d in resid)
station_var = mean(station_mean[s] ** 2 for s, _d in resid)
inter = mean(
    (resid[(s, d)] - fleet[d] - station_mean[s]) ** 2 for s, d in resid
)
print(f"\nvariance split of the residual (mean square, C^2):")
print(f"  total                     {total:.3f}")
print(f"  fleet-common component    {fleet_var:.3f}"
      f"  ({100*fleet_var/total:.0f}%)")
print(f"  station-persistent        {station_var:.3f}"
      f"  ({100*station_var/total:.0f}%)")
print(f"  station-day interaction   {inter:.3f}"
      f"  ({100*inter/total:.0f}%)")

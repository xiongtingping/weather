"""Would a per-station standing-bias shift help, and does it self-target?

The two next-day arms both failed the same way: they corrected station-days
that did not need correcting.  A standing-bias correction should not be able
to do that -- a station whose served posterior is already unbiased has a
trailing mean of about zero, so the correction it receives is about zero.

That is the property under test here, not the pooled average.  Three groups,
the same split the next-day arms were judged on:

  cold   the station really was running cold that day
  warm   it was running warm
  fine   it was already inside half a degree

A correction worth building leaves ``fine`` alone.

Strict as-of throughout: the shift for station s on day d is fitted only on
days strictly before d, and shrunk toward zero by the same n0 convention the
per-station width layer uses, so a station with two days of history does not
get a full-strength correction from them.
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.center_width_store import _truth, serving_cells_by_day
from bayes_basket_target.fleet_regime_shift import cell_residual_c

STAGE = sys.argv[1] if len(sys.argv) > 1 else "P0"

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)
truth = _truth(conn)
by_day = serving_cells_by_day(
    conn, truth, stage=STAGE,
    min_decision_at_utc="2026-08-01T00:00:00Z", pre_band=False,
)
resid: dict[tuple[str, str], float] = {}
for day, cells in by_day.items():
    per: dict[str, list[float]] = defaultdict(list)
    for cell in cells:
        actual = truth.get((cell["station_id"], cell["event_date"]))
        if actual is None:
            continue
        value = cell_residual_c(cell, actual)
        if value is not None:
            per[str(cell["station_id"])].append(value)
    for station, values in per.items():
        resid[(station, str(day))] = sum(values) / len(values)

days = sorted({d for _s, d in resid})
stations = sorted({s for s, _d in resid})
print(f"stage {STAGE}: {len(resid)} station-days,"
      f" {len(stations)} stations, {days[0]}..{days[-1]}")


def arm(window: int, n0: float, cap: float, min_days: int) -> None:
    groups: dict[str, list[tuple[float, float]]] = defaultdict(list)
    fired = 0
    total = 0
    for station in stations:
        own = sorted(
            (d for d in days if (station, d) in resid)
        )
        for index, day in enumerate(own):
            # Strictly earlier days only, most recent `window` of them.
            history = [resid[(station, d)] for d in own[:index]][-window:]
            total += 1
            if len(history) < min_days:
                shift = 0.0
            else:
                raw = mean(history)
                # Shrink toward zero: n/(n+n0), the convention the station
                # width layer uses so a thin history cannot claim a full
                # correction.
                shrunk = raw * len(history) / (len(history) + n0)
                shift = max(-cap, min(cap, shrunk))
            if shift != 0.0:
                fired += 1
            value = resid[(station, day)]
            key = (
                "cold" if value > 0.5
                else "warm" if value < -0.5 else "fine"
            )
            groups[key].append((value, shift))

    print(f"\n── window={window}d  n0={n0}  cap={cap}C  min_days={min_days}")
    print(f"   fired on {fired}/{total} = {100*fired/max(total,1):.0f}%"
          f" of station-days")
    before_all: list[float] = []
    after_all: list[float] = []
    for key, label in (("cold", "该救(偏冷>0.5)"),
                       ("warm", "推反(偏热<-0.5)"),
                       ("fine", "本来就准(<0.5)")):
        rows = groups.get(key) or []
        if not rows:
            continue
        before = [abs(r) for r, _s in rows]
        after = [abs(r - s) for r, s in rows]
        before_all += before
        after_all += after
        applied = [abs(s) for _r, s in rows if s != 0.0]
        print(f"   {label:18s} n={len(rows):3d}"
              f"  平均施加 {mean(applied) if applied else 0.0:.2f}C"
              f"  {mean(before):.2f} -> {mean(after):.2f}C"
              f"  ({100*(mean(before)-mean(after))/mean(before):+.0f}%)")
    hurt = sum(1 for b, a in zip(before_all, after_all) if a > b + 1e-9)
    print(f"   {'全体':18s} n={len(before_all):3d}"
          f"                {mean(before_all):.2f} ->"
          f" {mean(after_all):.2f}C"
          f"  ({100*(mean(before_all)-mean(after_all))/mean(before_all):+.1f}%)")
    print(f"   被弄更差: {hurt}/{len(before_all)}"
          f" = {100*hurt/max(len(before_all),1):.0f}%")


for window, n0, cap, min_days in (
    (7, 3.0, 1.5, 3),
    (10, 3.0, 1.5, 3),
    (14, 3.0, 1.5, 3),
    (14, 5.0, 1.5, 4),
    (21, 3.0, 2.0, 3),
):
    arm(window, n0, cap, min_days)

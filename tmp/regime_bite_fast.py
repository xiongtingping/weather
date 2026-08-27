"""Does the next-day shift move what a mass-ranked selector reads?

Same question as the slow probe, taken through ``serving_cells_by_day``
instead of resolving each envelope payload by hand -- the store's own cell
assembly already carries probabilities, bucket centres and the settled
bucket, and it does the whole stage in about three seconds.

The transform is production's ``apply_center_width_calibration`` and the
shift is production's ``derive_offset`` over the real daily series.  Nothing
is reimplemented here.
"""

import sqlite3
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target.center_width_calibration import (
    apply_center_width_calibration,
)
from bayes_basket_target.center_width_store import (
    _truth,
    serving_cells_by_day,
)
from bayes_basket_target.fleet_regime_shift import (
    FLEET_REGIME_SERVED_STAGES,
    derive_offset,
    visible_daily_series,
)

STAGE = sys.argv[1] if len(sys.argv) > 1 else "P0"

conn = sqlite3.connect(
    f"file:{ROOT / 'data' / 'bayes_basket_target_ssot.db'}?mode=ro",
    uri=True, timeout=60.0,
)

truth = _truth(conn)
by_day = serving_cells_by_day(
    conn, truth, stage=STAGE,
    min_decision_at_utc="2026-08-14T00:00:00Z", pre_band=False,
)

series_rows = [
    {
        "event_date": row[0], "residual_c_mean": row[1],
        "station_count": row[2], "cell_count": row[3],
        "truth_visible_at_utc": row[4],
    }
    for row in conn.execute(
        "SELECT event_date, residual_c_mean, station_count, cell_count,"
        " truth_visible_at_utc FROM calibration_fleet_regime_daily"
        " WHERE stage=? ORDER BY event_date", (STAGE,),
    )
]
known = {row["event_date"]: index for index, row in enumerate(series_rows)}


def shift_for(day: str) -> float:
    if STAGE not in FLEET_REGIME_SERVED_STAGES:
        return 0.0
    index = known.get(day)
    if index is None or index == 0:
        return 0.0
    return float(
        derive_offset(series_rows[:index], event_date=day)["regime_delta_c"]
    )


mean_moves, truth_p_moves, rank_moves = [], [], []
better = worse = same = 0
covered_before = covered_after = 0
top3_gained = top3_lost = 0
examined = 0
per_day = Counter()

for day in sorted(by_day):
    delta = shift_for(day)
    if delta == 0.0:
        continue
    for cell in by_day[day]:
        keys = list(cell["ordered_keys"])
        centers = list(cell["key_centers_c"])
        probs = dict(cell["probabilities"])
        landed = cell["settled_bucket"]
        if landed not in probs or len(keys) < 3:
            continue
        try:
            shifted, _ = apply_center_width_calibration(
                probs, delta_c=0.0, width_lambda=1.0,
                ordered_keys=keys, key_centers_c=centers,
                excluded_buckets=tuple(cell.get("excluded_buckets") or ()),
                regime_delta_c=delta,
            )
        except ValueError:
            continue
        examined += 1
        per_day[day] += 1

        total_b = sum(probs.values())
        total_a = sum(shifted.values())
        mb = sum(centers[i] * probs[k] for i, k in enumerate(keys)) / total_b
        ma = sum(
            centers[i] * shifted[k] for i, k in enumerate(keys)
        ) / total_a
        mean_moves.append(ma - mb)

        truth_p_moves.append(shifted[landed] / total_a - probs[landed] / total_b)
        rb = sorted(keys, key=lambda k: -probs[k]).index(landed)
        ra = sorted(keys, key=lambda k: -shifted[k]).index(landed)
        rank_moves.append(ra - rb)
        if ra < rb:
            better += 1
        elif ra > rb:
            worse += 1
        else:
            same += 1
        # The cheapest proxy for "would a three-wide mass window have
        # covered it": is the truth inside the top three by mass?
        in_b = rb < 3
        in_a = ra < 3
        covered_before += in_b
        covered_after += in_a
        if in_a and not in_b:
            top3_gained += 1
        if in_b and not in_a:
            top3_lost += 1

print(f"stage {STAGE}: {examined} station-day cells on days with a nonzero"
      f" shift ({len(per_day)} days)")
if not examined:
    raise SystemExit(0)
print(f"\ndistribution mean moved: median {median(mean_moves):+.3f}C"
      f"  (mean {mean(mean_moves):+.3f}C)")
print(f"truth-bucket probability: median {median(truth_p_moves):+.4f}"
      f"  mean {mean(truth_p_moves):+.4f}")
print(f"truth-bucket rank by mass: better {better}, worse {worse},"
      f" unchanged {same}")
print(f"\ntruth inside the top three by mass:"
      f" {covered_before} -> {covered_after}"
      f"  (gained {top3_gained}, lost {top3_lost},"
      f" net {top3_gained - top3_lost:+d} of {examined})")

"""Round 42c: calibrate for hits (0-1 loss on the truth bucket) instead of
likelihood.  On every roster station-day with a ladder (forward: fitted on
days ended by D-4), choose F's shift delta and station share lambda that
maximise hits; apply to the cluster's picks.  And the skew of the errors:
truth minus F's centre -- mean, median, mode bin.
"""
import collections
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

here = Path(__file__)
src = here.with_name("market_calibrated_pick_r42b.py").read_text()
ns = {"__name__": "r42b", "__file__": str(here.with_name("market_calibrated_pick_r42b.py"))}
exec(compile(src.split("# (2) market-calibrated centre")[0], "r42b_head", "exec"), ns)
exec(compile(src[src.index("def ladder_of"):src.index("targets = {}")], "r42b_ladder", "exec"), ns)
events, bucket, H, truth, row, conn, tz = (ns[k] for k in ("events", "bucket", "H", "truth", "row", "conn", "tz"))
ladder_of = ns["ladder_of"]

rows = []
for (st, d), members in H.items():
    if (st, d) not in truth or not (date(2026, 8, 20) <= d <= date(2026, 10, 12)):
        continue
    lad = ladder_of(st, d)
    if lad is None:
        continue
    r = row(st, d, members, d - timedelta(days=4))
    raw_med = statistics.median(members.values())
    corr_med = statistics.median(v for v in (r["z"][i] for i in range(len(r["z"])) if r["mask"][i]))
    rows.append({"st": st, "d": d, "edges": lad[1], "raw": raw_med, "c": r["c"], "corr": corr_med - raw_med,
                 "y": truth[(st, d)]})
print(f"station-days with ladder and truth: {len(rows)}")
errs = [r["y"] - (r["raw"] + r["c"] + 0.5 * r["corr"]) for r in rows]
hist = collections.Counter(round(e * 2) / 2 for e in errs)
print(f"truth - F: mean {statistics.mean(errs):+.2f} C, median {statistics.median(errs):+.2f} C, most common half-degree bin {max(hist, key=hist.get):+.1f} C,"
      f" skew {float(np.mean(((np.array(errs) - np.mean(errs)) / np.std(errs)) ** 3)):+.2f}")

GRID = [(dl / 10, lam) for dl in range(-8, 5) for lam in (0.3, 0.5, 0.7)]


def best_for(D):
    train = [r for r in rows if r["d"] <= D - timedelta(days=4)]
    return max(GRID, key=lambda g: sum(bucket(r["edges"], r["raw"] + r["c"] + g[1] * r["corr"] + g[0]) == bucket(r["edges"], r["y"]) for r in train))


stat = collections.Counter()
chosen = collections.Counter()
for ev in events:
    D = date.fromisoformat(ev["ed"])
    g = best_for(D)
    chosen[g] += 1
    e = ns["r40"]["prod_f"](ev["ts"])["station_days"][f"{ev['st']}|{ev['ed']}"]
    b = bucket(ev["edges"], e["raw_median_c"] + e["fleet_c"] + g[1] * e["station_corr_c"] + g[0])
    stat["n"] += 1
    stat["same"] += b == ev["choice"]
    stat["F"] += ev["fb"] == ev["choice"]
    stat["hit"] += ev["truth"] is not None and b == ev["truth"]
    stat["Fhit"] += ev["truth"] is not None and ev["fb"] == ev["truth"]
    stat["w"] += b == ev["choice"] and ev["fb"] != ev["choice"]
    stat["l"] += ev["fb"] == ev["choice"] and b != ev["choice"]
n = stat["n"]
d_, h_ = (stat["w"] - stat["l"]) / n, 1.96 * (stat["w"] + stat["l"]) ** 0.5 / n
print(f"hit-calibrated F (shift, lambda chosen forward to maximise truth hits): = cluster {stat['same'] / n:.1%} vs F {stat['F'] / n:.1%}"
      f" ({d_:+.3f} [{d_ - h_:+.3f}, {d_ + h_:+.3f}]); hits the truth {stat['hit'] / n:.1%} vs F {stat['Fhit'] / n:.1%}")
print("   grids chosen (shift C, lambda):", dict(chosen.most_common(5)))

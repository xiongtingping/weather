"""Charter 131 seats on skewed days: how often each window covers the settled bucket.

Follow-up to truth_lean.py (user: 「分别计算最后的真值落位的偏向…」).  Basket distribution, P0 opening
and P1 first decision; c = mean bucket, d = tail side, mo = mode bucket.  Coverage only -- prices
and the ledger decide whether a window pays.  Read-only, prints.
"""
import sys, pickle, sqlite3
from collections import Counter
import numpy as np
sys.path.insert(0, "/Users/xiong/weather/docs/research/p0_conditional_calibration_walkforward_20260926")
import walkforward as W, centres as C, truth_lean as T
from bayes_basket_target.basket_kernel.support import mean_mode_skew_reading_v1
CACHE = sys.argv[1]
rows, _ = W.build_rows(CACHE)
days = sorted({r["date"] for r in rows}); tested = set()
for day in days:
    test = [r for r in rows if r["date"] == day]; cutoff = min(W.instant(r["at"]) for r in test)
    if len({(r["station"], r["date"]) for r in rows if r["date"] < day and r["known_at"] <= cutoff}) >= W.MIN_TRAIN_STATION_DAYS: tested.add(day)
opening = {}
for r in sorted((r for r in rows if r["date"] in tested), key=lambda r: r["at"]): opening.setdefault((r["station"], r["date"]), r)
served, _s, _t, _ts = pickle.load(open(CACHE, "rb"))
conn = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60); conn.execute("PRAGMA query_only=ON")
formal = W.D.load_labels(conn); conn.close()
first_p1 = {}
for d in sorted((d for d in served if d["stage"] == "P1" and d["date"] >= "2026-09-01" and d["date"] in tested), key=lambda d: d["at"]):
    first_p1.setdefault((d["station"], d["date"]), d)
p1rows = []
for d in first_p1.values():
    lab = formal.get((d["station"], d["date"]))
    if lab is None or lab[0] not in d["keys"]: continue
    row = {**d, "hit": d["keys"].index(lab[0])}
    if np.asarray(row["p"])[row["hit"]] == 0.0: continue
    p1rows.append(row)
def seats(sample, name):
    cov = Counter(); n = 0; split = 0
    for r in sample:
        keys = r["keys"]; ords = {k: i for i, k in enumerate(keys)}; p = np.asarray(r["p"])
        rd = mean_mode_skew_reading_v1({k: float(v) for k, v in zip(keys, p)}, ords)
        if rd["sign"] == 0: continue
        n += 1; d = rd["sign"]; c = int(round(rd["mean_ordinal"])); mo = int(rd["mode_ordinal"]); h = r["hit"]
        split += mo != c
        cov["131_three {c,c+d,c+2d}"] += h in (c, c + d, c + 2 * d)
        cov["131_two {c,c+d}"] += h in (c, c + d)
        cov["centred {c-d,c,c+d}"] += h in (c - d, c, c + d)
        cov["mode-centred {mo-1,mo,mo+1}"] += h in (mo - 1, mo, mo + 1)
    print(f"{name}: skewed station-days {n} (mean bucket != mode bucket in {split})")
    for k, v in cov.items(): print(f"   {k:28s} covers {v/n:.1%}")
seats(list(opening.values()), "P0 opening, basket distribution")
seats(p1rows, "P1 first decision, basket distribution")
